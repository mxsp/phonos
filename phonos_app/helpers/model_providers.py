"""Unified Encrypted API Key & Model Endpoint Provider Manager.

Provides Gimel-grade encrypted storage (via ``data_crypto``) for bio-model and AI
provider keys with clear precedence:
  1. Per-user BYOK override (when ``user_id`` has stored their own key)
  2. Environment variable (``NVIDIA_API_KEY``, ``HUGGINGFACE_API_KEY``, etc.)
  3. Instance-wide database key configured by an Administrator in the UI

Plaintext keys are NEVER returned by public/UI helper functions.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import data_crypto
from .database import get_db

logger = logging.getLogger(__name__)

SEAL_LABEL = "phonos.model_provider_key"

BIO_PROVIDER_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "nvidia-nim",
        "label": "NVIDIA NIM / BioNeMo",
        "category": "bio_cloud",
        "base_url": "https://health.api.nvidia.com/v1",
        "test_url": "https://api.nvcf.nvidia.com/v2/nvcf/functions",
        "env_var": "NVIDIA_API_KEY",
        "key_label": "NVIDIA API Key",
        "key_placeholder": "nvapi-...",
        "url_label": "NVIDIA NIM Base URL",
        "auth_type": "bearer",
        "keyless": False,
        "description": "Powers Boltz-2, OpenFold3, DiffDock, RFdiffusion, ProteinMPNN, Evo 2, GenMol & MolMIM.",
    },
    {
        "id": "wolfram",
        "label": "Wolfram Engine / Cloud",
        "category": "compute",
        "base_url": "http://localhost:5100",
        "test_url": "http://localhost:5100/health",
        "env_var": "WOLFRAM_LICENSE",
        "key_label": "Wolfram License / API Key",
        "key_placeholder": "Enter Wolfram license or API key",
        "url_label": "Wolfram Service URL",
        "auth_type": "wolfram",
        "keyless": False,
        "description": "Powers Wolfram Language notebook cells, symbolic algebra, and scientific evaluation.",
    },
    {
        "id": "ibm-cloud",
        "label": "IBM Cloud / IBM Quantum",
        "category": "quantum",
        "base_url": "https://quantum.cloud.ibm.com",
        "test_url": "https://iam.cloud.ibm.com/identity/token",
        "env_var": "IBM_QUANTUM_API_KEY",
        "key_label": "IBM Cloud API Key",
        "key_placeholder": "Enter IBM Cloud API key",
        "url_label": "IBM Cloud Endpoint URL",
        "extra_field": "instance_crn",
        "extra_label": "IBM Instance CRN (Optional)",
        "extra_placeholder": "crn:v1:bluemix:public:quantum-computing:...",
        "auth_type": "ibm_iam",
        "keyless": False,
        "description": "Powers IBM Quantum Runtime hardware execution and platform-managed quantum sessions.",
    },
]

PRESET_BY_ID = {p["id"]: p for p in BIO_PROVIDER_PRESETS}
PROVIDER_ALIASES = {
    "nvidia": "nvidia-nim",
    "nvidia-nim": "nvidia-nim",
    "wolfram": "wolfram",
    "ibm": "ibm-cloud",
    "ibm-cloud": "ibm-cloud",
    "ibm-quantum": "ibm-cloud",
}
_SCHEMA_DONE_CONNS: set[int] = set()
_SYNCED_ENV_KEYS: set[str] = set()


def _rdict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return {}


def ensure_provider_schema(db) -> None:
    conn_id = id(db)
    if conn_id in _SCHEMA_DONE_CONNS:
        try:
            db.execute("SELECT 1 FROM phonos_model_providers LIMIT 1")
            return
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            _SCHEMA_DONE_CONNS.discard(conn_id)
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS phonos_model_providers (
                provider_id TEXT PRIMARY KEY,
                base_url TEXT NOT NULL DEFAULT '',
                api_key_enc TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            db.execute("ALTER TABLE phonos_model_providers ADD COLUMN extra_json TEXT NOT NULL DEFAULT '{}'")
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS phonos_user_provider_keys (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                provider_id TEXT NOT NULL,
                api_key_enc TEXT NOT NULL DEFAULT '',
                base_url_override TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, provider_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS phonos_model_endpoint_overrides (
                model_id TEXT PRIMARY KEY,
                endpoint_url TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT 'nvidia-nim',
                updated_by INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.commit()
        _SCHEMA_DONE_CONNS.add(conn_id)
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("ensure_provider_schema note: %s", exc)


def seal_secret(raw: str) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    return data_crypto.seal(SEAL_LABEL, val)


def open_secret(sealed: str) -> str:
    if not sealed:
        return ""
    try:
        return data_crypto.unseal(SEAL_LABEL, sealed) or ""
    except Exception:
        return ""


def validate_endpoint_url(raw_url: str, *, allow_private: bool = True) -> str:
    url = (raw_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Endpoint URL cannot be empty.")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Endpoint URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise ValueError("Endpoint URL must not embed credentials.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Endpoint URL is missing a hostname.")
    # Block cloud metadata SSRF targets even when allow_private=True
    if host in ("169.254.169.254", "metadata.google.internal", "100.100.100.200"):
        raise ValueError("Cloud metadata addresses are blocked.")
    if not allow_private and host in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Loopback addresses are not allowed for this endpoint.")
    return url


def resolve_provider_credentials(provider_id: str = "nvidia-nim", user_id: Optional[int] = None, db=None) -> Dict[str, Any]:
    """Resolve effective ``api_key``, ``key_source``, and ``base_url`` for ``provider_id``."""
    preset = PRESET_BY_ID.get(provider_id, {})
    env_name = preset.get("env_var") or ""
    env_val = os.environ.get(env_name, "").strip() if env_name else ""
    if env_val == "nvapi-YOUR_API_KEY_HERE":
        env_val = ""
    base_url = preset.get("base_url") or ""

    own_db = db
    if own_db is None:
        try:
            own_db = get_db()
        except Exception:
            own_db = None

    user_key = ""
    user_url = ""
    inst_key = ""
    inst_url = ""

    if own_db is not None:
        ensure_provider_schema(own_db)
        if user_id:
            try:
                urow = own_db.execute(
                    "SELECT api_key_enc, base_url_override FROM phonos_user_provider_keys WHERE user_id = %s AND provider_id = %s",
                    (int(user_id), provider_id),
                ).fetchone()
                if urow:
                    ud = _rdict(urow)
                    user_key = open_secret(ud.get("api_key_enc") or "")
                    user_url = (ud.get("base_url_override") or "").strip()
            except Exception:
                pass
        try:
            prow = own_db.execute(
                "SELECT base_url, api_key_enc, extra_json FROM phonos_model_providers WHERE provider_id = %s",
                (provider_id,),
            ).fetchone()
            if prow:
                pd = _rdict(prow)
                inst_key = open_secret(pd.get("api_key_enc") or "")
                inst_url = (pd.get("base_url") or "").strip()
                try:
                    inst_extra = json.loads(pd.get("extra_json") or "{}")
                    if not isinstance(inst_extra, dict):
                        inst_extra = {}
                except Exception:
                    inst_extra = {}
            else:
                inst_extra = {}
        except Exception:
            inst_extra = {}
    else:
        inst_extra = {}

    effective_url = user_url or inst_url or base_url
    env_is_external = bool(env_val) and (env_name not in _SYNCED_ENV_KEYS)
    if user_key:
        return {"api_key": user_key, "key_source": "user", "has_key": True, "base_url": effective_url, "extra": inst_extra}
    if env_is_external:
        return {"api_key": env_val, "key_source": "environment", "has_key": True, "base_url": effective_url, "extra": inst_extra}
    if inst_key:
        return {"api_key": inst_key, "key_source": "instance", "has_key": True, "base_url": effective_url, "extra": inst_extra}
    if env_val:
        return {"api_key": env_val, "key_source": "environment", "has_key": True, "base_url": effective_url, "extra": inst_extra}
    return {"api_key": "", "key_source": "none", "has_key": bool(preset.get("keyless")), "base_url": effective_url, "extra": inst_extra}


def resolve_nvidia_api_key(user_id: Optional[int] = None, db=None) -> str:
    """Drop-in runtime helper for all NVIDIA NIM routes & workers."""
    creds = resolve_provider_credentials("nvidia-nim", user_id=user_id, db=db)
    return creds["api_key"]


def resolve_wolfram_api_key(db=None) -> str:
    """Drop-in runtime helper for Wolfram license / API key."""
    creds = resolve_provider_credentials("wolfram", db=db)
    return creds["api_key"]


def resolve_ibm_cloud_credentials(db=None) -> Dict[str, Any]:
    """Drop-in runtime helper for IBM Cloud / IBM Quantum platform credentials."""
    creds = resolve_provider_credentials("ibm-cloud", db=db)
    extra = creds.get("extra") or {}
    return {
        "api_key": creds["api_key"],
        "instance_crn": (extra.get("instance_crn") or os.environ.get("IBM_QUANTUM_INSTANCE") or "").strip() or None,
        "base_url": creds["base_url"],
        "key_source": creds["key_source"],
    }


def resolve_builtin_model_endpoint(model_id: str, default_url: str, db=None) -> str:
    """Return admin-configured endpoint URL override for a built-in model, else ``default_url``."""
    own_db = db
    if own_db is None:
        try:
            own_db = get_db()
        except Exception:
            return default_url
    ensure_provider_schema(own_db)
    try:
        row = own_db.execute(
            "SELECT endpoint_url FROM phonos_model_endpoint_overrides WHERE model_id = %s",
            (model_id,),
        ).fetchone()
        if row:
            url = (_rdict(row).get("endpoint_url") or "").strip()
            if url:
                return url
    except Exception:
        pass
    return default_url


def list_providers_public(db, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return safe provider cards for UI (never includes plaintext keys)."""
    ensure_provider_schema(db)
    out: List[Dict[str, Any]] = []
    for preset in BIO_PROVIDER_PRESETS:
        pid = preset["id"]
        resolved = resolve_provider_credentials(pid, user_id=user_id, db=db)
        out.append({
            "id": pid,
            "label": preset["label"],
            "category": preset["category"],
            "base_url": resolved["base_url"],
            "default_base_url": preset["base_url"],
            "env_var": preset["env_var"],
            "key_label": preset.get("key_label", "API Key"),
            "key_placeholder": preset.get("key_placeholder", "Paste API key"),
            "url_label": preset.get("url_label", "Base URL"),
            "extra_field": preset.get("extra_field", ""),
            "extra_label": preset.get("extra_label", ""),
            "extra_placeholder": preset.get("extra_placeholder", ""),
            "extra": resolved.get("extra") or {},
            "keyless": bool(preset.get("keyless")),
            "has_key": bool(resolved["api_key"]) or bool(preset.get("keyless")),
            "key_source": resolved["key_source"],
            "description": preset["description"],
        })
    return out


def save_instance_provider(
    db,
    provider_id: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    extra_config: Optional[Dict[str, Any]] = None,
    clear_key: bool = False,
    actor_id: Optional[int] = None,
) -> None:
    ensure_provider_schema(db)
    existing = db.execute("SELECT base_url, api_key_enc, extra_json FROM phonos_model_providers WHERE provider_id = %s", (provider_id,)).fetchone()
    ed = _rdict(existing)
    preset = PRESET_BY_ID.get(provider_id, {})

    new_url = ed.get("base_url") or preset.get("base_url") or ""
    if base_url is not None and str(base_url).strip():
        new_url = validate_endpoint_url(base_url)

    new_enc = ed.get("api_key_enc") or ""
    if clear_key:
        new_enc = ""
    elif api_key is not None and str(api_key).strip():
        new_enc = seal_secret(api_key)

    try:
        cur_extra = json.loads(ed.get("extra_json") or "{}")
        if not isinstance(cur_extra, dict):
            cur_extra = {}
    except Exception:
        cur_extra = {}
    if isinstance(extra_config, dict):
        for k, v in extra_config.items():
            if v is None:
                continue
            cur_extra[str(k)] = str(v).strip()
    new_extra_json = json.dumps(cur_extra)

    if existing:
        db.execute(
            "UPDATE phonos_model_providers SET base_url = %s, api_key_enc = %s, extra_json = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE provider_id = %s",
            (new_url, new_enc, new_extra_json, actor_id, provider_id),
        )
    else:
        db.execute(
            "INSERT INTO phonos_model_providers (provider_id, base_url, api_key_enc, extra_json, updated_by) VALUES (%s, %s, %s, %s, %s)",
            (provider_id, new_url, new_enc, new_extra_json, actor_id),
        )
    # Sync key into process env so background workers & services pick it up immediately
    # without shadowing the DB "instance" source badge or preventing Clear from working.
    def _sync_env(env_name: str, value: str, *, clearing: bool) -> None:
        if value:
            if not os.environ.get(env_name) or env_name in _SYNCED_ENV_KEYS:
                os.environ[env_name] = value
                _SYNCED_ENV_KEYS.add(env_name)
        elif clearing and env_name in _SYNCED_ENV_KEYS:
            os.environ.pop(env_name, None)
            _SYNCED_ENV_KEYS.discard(env_name)

    plain = open_secret(new_enc)
    if provider_id == "nvidia-nim":
        _sync_env("NVIDIA_API_KEY", plain, clearing=clear_key)
    elif provider_id == "wolfram":
        _sync_env("WOLFRAM_LICENSE", plain, clearing=clear_key)
        if new_url:
            _sync_env("WOLFRAM_SERVICE_URL", new_url, clearing=False)
    elif provider_id == "ibm-cloud":
        _sync_env("IBM_QUANTUM_API_KEY", plain, clearing=clear_key)
        crn = (cur_extra.get("instance_crn") or "").strip()
        _sync_env("IBM_QUANTUM_INSTANCE", crn, clearing=("instance_crn" in cur_extra and not crn))
    db.commit()


def save_user_provider_key(db, user_id: int, provider_id: str, *, api_key: Optional[str] = None, base_url_override: Optional[str] = None, clear_key: bool = False) -> None:
    ensure_provider_schema(db)
    existing = db.execute(
        "SELECT id, api_key_enc, base_url_override FROM phonos_user_provider_keys WHERE user_id = %s AND provider_id = %s",
        (user_id, provider_id),
    ).fetchone()
    ed = _rdict(existing)
    new_url = ed.get("base_url_override") or ""
    if base_url_override is not None:
        new_url = validate_endpoint_url(base_url_override) if str(base_url_override).strip() else ""

    new_enc = ed.get("api_key_enc") or ""
    if clear_key:
        new_enc = ""
    elif api_key is not None and str(api_key).strip():
        new_enc = seal_secret(api_key)

    if not new_enc and not new_url:
        if existing:
            db.execute("DELETE FROM phonos_user_provider_keys WHERE user_id = %s AND provider_id = %s", (user_id, provider_id))
        db.commit()
        return

    if existing:
        db.execute(
            "UPDATE phonos_user_provider_keys SET api_key_enc = %s, base_url_override = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s AND provider_id = %s",
            (new_enc, new_url, user_id, provider_id),
        )
    else:
        db.execute(
            "INSERT INTO phonos_user_provider_keys (user_id, provider_id, api_key_enc, base_url_override) VALUES (%s, %s, %s, %s)",
            (user_id, provider_id, new_enc, new_url),
        )
    db.commit()


def test_provider_connection(provider_id: str, user_id: Optional[int] = None, db=None, custom_url: Optional[str] = None, raw_key: Optional[str] = None) -> Dict[str, Any]:
    provider_id = PROVIDER_ALIASES.get(provider_id, provider_id)
    preset = PRESET_BY_ID.get(provider_id, {})
    creds = resolve_provider_credentials(provider_id, user_id=user_id, db=db)
    api_key = (raw_key or "").strip() or creds["api_key"]
    auth_type = preset.get("auth_type", "bearer")

    if not api_key and not preset.get("keyless"):
        return {"ok": False, "error": "No API key configured for this provider."}

    t0 = time.monotonic()
    if auth_type == "ibm_iam":
        # Validate IBM Cloud API key via IBM Cloud IAM OAuth token exchange
        iam_url = "https://iam.cloud.ibm.com/identity/token"
        body = urllib.parse.urlencode({
            "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
            "apikey": api_key,
        }).encode("utf-8")
        req = urllib.request.Request(
            iam_url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "phonos-provider-probe/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
                ms = int((time.monotonic() - t0) * 1000)
                return {"ok": True, "status_code": resp.status, "latency_ms": ms, "key_source": creds["key_source"], "detail": "IBM Cloud IAM token verified."}
        except urllib.error.HTTPError as he:
            ms = int((time.monotonic() - t0) * 1000)
            return {"ok": False, "status_code": he.code, "latency_ms": ms, "error": f"IBM Cloud IAM rejected key (HTTP {he.code})."}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if auth_type == "wolfram":
        base = (custom_url or "").strip() or creds["base_url"] or preset.get("base_url") or "http://localhost:5100"
        target_url = base.rstrip("/") + "/health"
        try:
            validate_endpoint_url(target_url)
        except ValueError as ve:
            return {"ok": False, "error": str(ve)}
        req = urllib.request.Request(target_url, headers={"Accept": "application/json", "User-Agent": "phonos-provider-probe/1.0"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
                ms = int((time.monotonic() - t0) * 1000)
                return {"ok": True, "status_code": resp.status, "latency_ms": ms, "key_source": creds["key_source"], "detail": "Wolfram service reachable."}
        except Exception as exc:
            return {"ok": False, "error": f"Wolfram service at {target_url} unreachable: {exc}"}

    target_url = (custom_url or "").strip() or preset.get("test_url") or creds["base_url"]
    if not target_url:
        return {"ok": False, "error": "No test URL configured for this provider."}
    try:
        validate_endpoint_url(target_url)
    except ValueError as ve:
        return {"ok": False, "error": str(ve)}

    headers = {"Accept": "application/json", "User-Agent": "phonos-provider-probe/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(target_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            ms = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "status_code": resp.status, "latency_ms": ms, "key_source": creds["key_source"]}
    except urllib.error.HTTPError as he:
        ms = int((time.monotonic() - t0) * 1000)
        if he.code == 405:
            return {"ok": True, "status_code": 405, "latency_ms": ms, "detail": "Endpoint reachable (POST-only)."}
        return {"ok": False, "status_code": he.code, "latency_ms": ms, "error": f"HTTP {he.code}: {he.reason}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
