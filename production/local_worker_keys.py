"""Auto-provisioned local RSA keypair for Phonos worker node signing.

Ensures zero private keys are tracked in Git while allowing fresh self-hosted,
Docker, Linux, and Windows installations to sign and verify worker results
out of the box. Keys are persisted under ``PHONOS_DATA_DIR/worker-keys`` (or
``.data/worker-keys``) with ``0600`` permissions.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_PUBLISHER_NODE_IDS = (
    "publisher-node-type-0",
    "publisher-node-type-2-colabfold",
    "publisher-node-type-3",
    "publisher-node-type-5",
    "evo2-worker-1",
    "alphafold2-re-worker-1",
    "alphafold2-re-multimer-worker-1",
    "boltz2-enhanced-worker-1",
    "boltz2-worker-1",
    "esmfold-re-worker-1",
    "diffdock-re-worker-1",
    "proteinmpnn-re-worker-1",
    "genmol-worker-1",
    "molmim-worker-1",
    "openfold3-worker-1",
    "rfdiffusion-worker-1",
)


def worker_keys_dir() -> Path:
    explicit = os.environ.get("PHONOS_WORKER_KEYS_DIR", "").strip()
    if explicit:
        return Path(explicit)
    base_data = os.environ.get("PHONOS_DATA_DIR", "").strip()
    if base_data:
        return Path(base_data) / "worker-keys"
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / ".data" / "worker-keys"


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ensure_local_worker_keypair() -> Tuple[Optional[str], Dict[str, str]]:
    """Return ``(private_key_pem, {worker_id: public_key_pem})``, generating once if absent."""
    env_priv = os.environ.get("NODE_PRIVATE_KEY", "").strip()
    keys_dir = worker_keys_dir()
    priv_path = keys_dir / "node_private_key.pem"
    pub_map_path = keys_dir / "publisher_keys.json"

    if priv_path.exists() and pub_map_path.exists():
        try:
            priv_pem = env_priv or priv_path.read_text(encoding="utf-8")
            pub_map = json.loads(pub_map_path.read_text(encoding="utf-8"))
            if isinstance(pub_map, dict) and priv_pem:
                return priv_pem, {str(k): str(v) for k, v in pub_map.items()}
        except Exception as exc:
            logger.warning("Failed reading cached local worker keys: %s", exc)

    try:
        from encryption_util import generate_key_pair
    except ImportError:
        try:
            from production.encryption_util import generate_key_pair
        except ImportError:
            return (env_priv or None), {}

    try:
        keys_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(keys_dir, 0o700)
        except OSError:
            pass
        priv_pem, pub_pem = generate_key_pair()
        if env_priv:
            priv_pem = env_priv
            try:
                from Crypto.PublicKey import RSA

                pub_pem = RSA.import_key(priv_pem).publickey().export_key().decode("utf-8")
            except Exception:
                pass

        pub_map = {wid: pub_pem for wid in DEFAULT_PUBLISHER_NODE_IDS}
        tmp_priv = priv_path.with_suffix(".pem.tmp")
        tmp_pub = pub_map_path.with_suffix(".json.tmp")
        tmp_priv.write_text(priv_pem, encoding="utf-8")
        _chmod_600(tmp_priv)
        os.replace(tmp_priv, priv_path)

        tmp_pub.write_text(json.dumps(pub_map, indent=2), encoding="utf-8")
        _chmod_600(tmp_pub)
        os.replace(tmp_pub, pub_map_path)
        return priv_pem, pub_map
    except Exception as exc:
        logger.warning("Could not initialize local worker keypair in %s: %s", keys_dir, exc)
        return (env_priv or None), {}


def load_local_worker_private_key() -> Optional[str]:
    priv_pem, _ = ensure_local_worker_keypair()
    return priv_pem


def load_local_publisher_public_keys() -> Dict[str, str]:
    _, pub_map = ensure_local_worker_keypair()
    explicit_file = os.environ.get("PHONOS_PUBLISHER_KEYS_FILE", "").strip()
    if explicit_file and os.path.exists(explicit_file):
        try:
            with open(explicit_file, encoding="utf-8") as fh:
                extra = json.load(fh)
            if isinstance(extra, dict):
                pub_map.update({str(k): str(v) for k, v in extra.items()})
        except Exception as exc:
            logger.warning("Could not load PHONOS_PUBLISHER_KEYS_FILE %s: %s", explicit_file, exc)
    return pub_map
