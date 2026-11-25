import json
import os
import re
import uuid
import logging
import time
from dataclasses import dataclass
from pathlib import Path
import base64
from typing import Any, ClassVar, Dict, Optional, Tuple

import redis

from encryption_util import (
    canonical_dumps,
    encrypt,
    generate_key_pair,
    sign_message,
)
from config import Config

# Import subdomain verification (best-effort — workers may not have it)
try:
    from phonos_app.helpers.subdomain_verify import (
        TRUSTED_DOMAIN,
        extract_worker_id,
        validate_subdomain,
    )
    SUBDOMAIN_AVAILABLE = True
except ImportError:
    SUBDOMAIN_AVAILABLE = False

# --- Constants ---
NODE_CONFIG_FILE = 'node_config.json'
PRIVATE_KEY_FILE = 'node_private_key.pem'
NODE_CONFIG_PATH = Path(NODE_CONFIG_FILE)
PRIVATE_KEY_PATH = Path(PRIVATE_KEY_FILE)
SECURE_FILE_MODE = 0o600
REGISTRATION_CHANNEL = "pubsub:node_registration"
REGISTRATION_CONFIRMATION_CHANNEL_PREFIX = "pubsub:node_registration_confirmation:"

logger = logging.getLogger(__name__)

# --- Redis Connection Pool ---
_redis_pool_kwargs = {
    "host": Config.REDIS_HOST,
    "port": Config.REDIS_PORT,
    "db": Config.REDIS_DB,
    "decode_responses": True,
    "max_connections": 50,
    "socket_connect_timeout": 5,
    "socket_timeout": 5,
    "socket_keepalive": True,
    "health_check_interval": 30,
    "retry_on_timeout": True,
}

# Add password if configured
if hasattr(Config, 'REDIS_PASSWORD') and Config.REDIS_PASSWORD:
    _redis_pool_kwargs["password"] = Config.REDIS_PASSWORD

# Add SSL/TLS if configured
if hasattr(Config, 'REDIS_SSL') and Config.REDIS_SSL:
    import ssl
    _redis_pool_kwargs["connection_class"] = redis.SSLConnection
    _redis_pool_kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED

redis_pool = redis.ConnectionPool(**_redis_pool_kwargs)


class NodeConfigError(RuntimeError):
    """Raised when node configuration files cannot be safely read or written."""


def _ensure_secure_permissions(path: Path) -> None:
    """Best effort attempt to keep the node files readable only by the owner."""
    try:
        os.chmod(path, SECURE_FILE_MODE)
    except OSError:
        # On some platforms (e.g., Windows) chmod may not be supported – that's OK.
        logger.debug("Unable to set restrictive permissions on %s", path)


def _write_secure_json(path: Path, payload: Dict[str, object]) -> None:
    """Atomically write JSON content with restrictive permissions."""
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECURE_FILE_MODE)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.error("Failed to persist node configuration to %s", path, exc_info=True)
        raise NodeConfigError(str(exc)) from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _write_secure_text(path: Path, contents: str) -> None:
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECURE_FILE_MODE)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.error("Failed to persist secure text to %s", path, exc_info=True)
        raise NodeConfigError(str(exc)) from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _load_secure_text(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        with path.open('r', encoding='utf-8') as handle:
            data = handle.read()
    except OSError as exc:
        logger.error("Could not read secure text from %s", path, exc_info=True)
        return None
    _ensure_secure_permissions(path)
    return data


@dataclass
class NodeIdentity:
    """Representation of a node's persisted identity information."""

    worker_id: str
    public_key: str
    organization_id: Optional[int] = None

    CONFIG_PATH: ClassVar[Path] = NODE_CONFIG_PATH

    def to_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            'worker_id': self.worker_id,
            'public_key': self.public_key,
        }
        if self.organization_id is not None:
            payload['organization_id'] = self.organization_id
        return payload

    def save(self) -> None:
        _write_secure_json(self.CONFIG_PATH, self.to_dict())
        _ensure_secure_permissions(self.CONFIG_PATH)

    @classmethod
    def load(cls) -> Optional['NodeIdentity']:
        path = cls.CONFIG_PATH
        if not path.exists():
            return None
        try:
            with path.open('r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load node configuration from %s: %s", path, exc)
            return None

        worker_id = data.get('worker_id')
        public_key = data.get('public_key')
        if not worker_id or not public_key:
            logger.warning("Node configuration at %s is missing identity fields.", path)
            return None

        raw_org = data.get('organization_id')
        organization_id: Optional[int]
        if raw_org is None:
            organization_id = None
        else:
            try:
                organization_id = int(raw_org)
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid organization ID %r from node configuration.", raw_org)
                organization_id = None

        identity = cls(worker_id=worker_id, public_key=public_key, organization_id=organization_id)
        _ensure_secure_permissions(path)
        return identity

def get_redis_connection():
    """Gets a Redis connection from a shared pool."""
    return redis.Redis(connection_pool=redis_pool)

def _generate_new_node_identity() -> Tuple[str, str, str]:
    """Generates a new worker ID and RSA key pair."""
    worker_id = f"node-{uuid.uuid4()}"
    private_key, public_key = generate_key_pair()
    logger.info(f"Generated new identity for worker: {worker_id}")
    return worker_id, private_key, public_key

def _persist_identity(worker_id: str, private_key: str, public_key: str, organization_id: Optional[int] = None) -> None:
    """Persist the node's credentials securely."""
    _write_secure_text(PRIVATE_KEY_PATH, private_key)
    _ensure_secure_permissions(PRIVATE_KEY_PATH)
    NodeIdentity(worker_id=worker_id, public_key=public_key, organization_id=organization_id).save()
    logger.info("Saved node identity and credentials to disk.")


def set_node_organization(organization_id: Optional[int]) -> bool:
    """Persist the organization identifier for the node.

    Passing ``None`` detaches the node from any organization and makes it public again.
    """

    identity = NodeIdentity.load()
    if not identity:
        logger.error("Cannot update organization – node identity has not been initialized yet.")
        return False

    try:
        identity.organization_id = int(organization_id) if organization_id is not None else None
    except (TypeError, ValueError):
        logger.error("Organization identifier %r is invalid.", organization_id)
        return False

    try:
        identity.save()
    except NodeConfigError as exc:
        logger.error("Failed to persist organization update: %s", exc)
        return False

    if identity.organization_id is None:
        logger.info("Node %s marked as a public node.", identity.worker_id)
    else:
        logger.info("Node %s associated with organization %s.", identity.worker_id, identity.organization_id)
    return True


def clear_node_organization() -> bool:
    """Helper to explicitly reset the node to a public configuration."""
    return set_node_organization(None)


def load_or_initialize_node() -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Load node credentials from disk, generating fresh ones if necessary."""

    identity = NodeIdentity.load()
    private_key = _load_secure_text(PRIVATE_KEY_PATH)

    if identity and private_key:
        org_fragment = f"Organization: {identity.organization_id}" if identity.organization_id else "Public Node"
        logger.info("Loaded existing identity for worker %s (%s)", identity.worker_id, org_fragment)
        return identity.worker_id, private_key, identity.organization_id

    if identity or private_key:
        logger.warning(
            "Node identity files were partially available – regenerating credentials to ensure a consistent and secure state."
        )

    try:
        worker_id, private_key_value, public_key = _generate_new_node_identity()
        _persist_identity(worker_id, private_key_value, public_key)
        return worker_id, private_key_value, None
    except NodeConfigError:
        return None, None, None
    except Exception as exc:
        logger.critical("FATAL: Could not create and save a new node identity: %s", exc, exc_info=True)
        return None, None, None


def build_subdomain_worker_id(label: str) -> str:
    """Build a canonical worker ID for an external worker from its DNS label.

    The label must be a valid DNS label (63 chars, alphanumeric + hyphens).
    The returned worker_id is the label itself — the subdomain is derived
    as ``<worker_id>.fyreflysystems.de`` at registration time.

    Raises ``ValueError`` if the label is invalid.
    """
    pattern = r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$"
    if not re.match(pattern, label):
        raise ValueError(
            f"Invalid worker DNS label '{label}'. "
            f"Must be 1-63 lowercase alphanumeric characters or hyphens, "
            f"not starting or ending with hyphen."
        )
    return label


def request_registration(
    worker_id: str,
    public_key: str,
    worker_type: str,
    *,
    subdomain: Optional[str] = None,
) -> Tuple[bool, Optional[int]]:
    """
    Publishes a registration request to the manager and waits for approval.

    External workers (those with a ``*.fyreflysystems.de`` subdomain) should
    pass their subdomain via the *subdomain* parameter. The MRS will validate
    the subdomain before approving the registration.

    :return: A tuple of (is_approved, organization_id).
    """
    r = get_redis_connection()
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    confirmation_channel = f"{REGISTRATION_CONFIRMATION_CHANNEL_PREFIX}{worker_id}"

    try:
        pubsub.subscribe(confirmation_channel)
        logger.info(f"Subscribed to registration confirmation channel: {confirmation_channel}")

        registration_payload: Dict[str, object] = {
            'worker_id': worker_id,
            'public_key': public_key,
            'worker_type': worker_type,
            'timestamp': time.time(),
        }

        # External workers include their verified subdomain
        if subdomain:
            registration_payload['subdomain'] = subdomain
            logger.info(
                "External worker %s registering via subdomain %s",
                worker_id,
                subdomain,
            )

        # Publish the request
        r.publish(REGISTRATION_CHANNEL, json.dumps(registration_payload))
        logger.info(f"Sent registration request for worker {worker_id} to channel '{REGISTRATION_CHANNEL}'.")
        logger.info("Waiting for administrator approval...")

        # Wait for a confirmation message from the manager
        while True:
            message = pubsub.get_message(timeout=30) # Check every 30s
            if message:
                try:
                    data = json.loads(message['data'])
                    if data.get('status') == 'approved':
                        organization_id = data.get('organization_id')
                        if organization_id:
                            logger.info(f"Registration approved for organization {organization_id}. Node can now start.")
                        else:
                            logger.info("Registration approved as a public node. Node can now start.")
                        pubsub.unsubscribe()
                        return True, organization_id
                    elif data.get('status') == 'denied':
                         logger.error("Registration denied by administrator. Shutting down.")
                         pubsub.unsubscribe()
                         return False, None
                except json.JSONDecodeError:
                    logger.warning("Received malformed confirmation message.")

            print(".", end="", flush=True) # Visual indicator of waiting

    except redis.exceptions.RedisError as e:
        logger.error(f"Redis error during registration: {e}")
        return False, None
    except Exception as e:
        logger.error(f"An unexpected error occurred during registration: {e}", exc_info=True)
        return False, None
    finally:
        try:
            pubsub.close()
        except Exception:
            pass


def send_job_result(
    result_payload: Dict[str, Any],
    private_key: str,
    redis_client: redis.Redis,
) -> bool:
    """
    Signs, encrypts, and sends a job result payload to the results queue.

    This function now handles reading file content, base64 encoding it,
    and embedding it directly into the payload. Also adds timestamp and nonce
    for replay protection.
    """
    job_id = result_payload.get("job_id", "unknown")
    payload = result_payload.copy()

    # Add timestamp for replay protection
    payload["timestamp"] = time.time()

    # Add unique nonce to prevent replay attacks
    import secrets
    payload["nonce"] = secrets.token_urlsafe(32)

    # --- Embed Output File Content ---
    output_file_path = payload.pop("output_file", None)
    if output_file_path and os.path.exists(output_file_path):
        try:
            with open(output_file_path, "rb") as f:
                content_bytes = f.read()
            payload["output_file_content"] = base64.b64encode(content_bytes).decode("utf-8")
            payload["output_filename"] = os.path.basename(output_file_path)
            logger.info("Embedded content of '%s' for job %s.", output_file_path, job_id)
        except IOError as e:
            logger.error("Failed to read output file %s for job %s: %s", output_file_path, job_id, e)
            payload["status"] = "failed"
            payload["message"] = f"Node failed to read output file: {e}"

    # --- Embed Artifacts Content ---
    artifacts_to_embed = payload.pop("artifacts", [])
    embedded_artifacts = []
    if artifacts_to_embed:
        for artifact in artifacts_to_embed:
            artifact_path = artifact.get("path")
            if artifact_path and os.path.exists(artifact_path):
                try:
                    with open(artifact_path, "rb") as f:
                        content_bytes = f.read()
                    artifact["content_b64"] = base64.b64encode(content_bytes).decode("utf-8")
                    # No longer need the local path
                    del artifact["path"]
                    embedded_artifacts.append(artifact)
                    logger.info("Embedded content of artifact '%s' for job %s.", artifact_path, job_id)
                except IOError as e:
                    logger.error("Failed to read artifact file %s for job %s: %s", artifact_path, job_id, e)
                    # We can still send the result, just without this artifact.
            else:
                # If path is missing or file doesn't exist, just pass the artifact metadata
                embedded_artifacts.append(artifact)
        payload["artifacts"] = embedded_artifacts


    # --- Sign, Encrypt, and Send ---
    try:
        payload_bytes = canonical_dumps(payload).encode("utf-8")
        signature = sign_message(private_key, payload_bytes)
        if not signature:
            logger.error("Failed to sign result for job %s. Cannot send.", job_id)
            return False

        signed_result = {"payload": payload, "signature": signature}
        encrypted_result = encrypt(json.dumps(signed_result).encode("utf-8"))

        if not encrypted_result:
            logger.error("Failed to encrypt signed result for job %s. Cannot send.", job_id)
            return False

        redis_client.lpush(Config.REDIS_JOB_RESULTS_QUEUE, encrypted_result)
        logger.info(
            "Job %s result sent to queue '%s' with status: %s",
            job_id,
            Config.REDIS_JOB_RESULTS_QUEUE,
            payload.get("status"),
        )
        return True
    except Exception as e:
        logger.error(
            "An unexpected error occurred while sending job result for %s: %s",
            job_id,
            e,
            exc_info=True,
        )
        return False
