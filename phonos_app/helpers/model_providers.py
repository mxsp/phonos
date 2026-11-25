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
        "label": "NVIDIA NIM / BioNeMo Cloud",
        "category": "bio_cloud",
        "base_url": "https://health.api.nvidia.com/v1",
        "test_url": "https://api.nvcf.nvidia.com/v2/nvcf/functions",
        "env_var": "NVIDIA_API_KEY",
        "auth_type": "bearer",
        "keyless": False,
        "description": "Powers Boltz-2, OpenFold3, DiffDock, RFdiffusion, ProteinMPNN, Evo 2, GenMol & MolMIM.",
    },
    {
        "id": "huggingface",
        "label": "Hugging Face Inference / Endpoints",
        "category": "bio_cloud",
        "base_url": "https://api-inference.huggingface.co/models",
        "test_url": "https://huggingface.co/api/whoami-v2",
        "env_var": "HUGGINGFACE_API_KEY",
        "auth_type": "bearer",
        "keyless": False,
        "description": "ESM-2, ProGen2, ChemBERTa, and dedicated Hugging Face GPU endpoints.",
    },
    {
        "id": "local-nim",
        "label": "Self-Hosted NVIDIA NIM Container (LAN / Local GPU)",
        "category": "local",
        "base_url": "http://127.0.0.1:8000/v1",
        "test_url": "http://127.0.0.1:8000/v1/health/ready",
        "env_var": "LOCAL_NIM_API_KEY",
        "auth_type": "none",
        "keyless": True,
        "description": "Local Docker/GPU NIM microservice for air-gapped structure prediction or docking.",
    },
    {
        "id": "local-colabfold",
        "label": "LocalColabFold / Custom FastAPI Bio Server",
        "category": "local",
        "base_url": "http://127.0.0.1:8090",
        "test_url": "http://127.0.0.1:8090/health",
        "env_var": "",
        "auth_type": "none",
        "keyless": True,
        "description": "Connect examples/custom_model_server.py or any FastAPI/PyTorch molecular service.",
    },
    {
        "id": "gemini",
        "label": "Google Gemini (Copilot & Multimodal Bio Analysis)",
        "category": "llm",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "test_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "env_var": "GEMINI_API_KEY",
        "auth_type": "bearer",
        "keyless": False,
        "description": "Default OpenAI-compatible reasoning engine for Phonos Copilot.",
    },
    {
        "id": "openai",
        "label": "OpenAI API",
        "category": "llm",
        "base_url": "https://api.openai.com/v1",
        "test_url": "https://api.openai.com/v1/models",
        "env_var": "OPENAI_API_KEY",
        "auth_type": "bearer",
        "keyless": False,
        "description": "GPT-4.1 / o-series for Copilot and OpenAI-compatible custom bio endpoints.",
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude",
        "category": "llm",
        "base_url": "https://api.anthropic.com/v1",
        "test_url": "https://api.anthropic.com/v1/models",
        "env_var": "ANTHROPIC_API_KEY",
        "auth_type": "x-api-key",
        "keyless": False,
        "description": "Claude Sonnet / Opus native streaming protocol for Discovery Copilot.",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "category": "llm",
        "base_url": "https://openrouter.ai/api/v1",
        "test_url": "https://openrouter.ai/api/v1/models",
        "env_var": "OPENROUTER_API_KEY",
        "auth_type": "bearer",
        "keyless": False,
        "description": "Unified gateway to 200+ open and frontier models.",
    },
    {
        "id": "ollama",
        "label": "Ollama / vLLM / LM Studio (Local Keyless)",
        "category": "local",
        "base_url": "http://127.0.0.1:11434/v1",
        "test_url": "http://127.0.0.1:11434/api/tags",
        "env_var": "",
        "auth_type": "none",
        "keyless": True,
        "description": "100% offline local LLM & scientific inference on your own workstation.",
    },
]

PRESET_BY_ID = {p["id"]: p for p in BIO_PROVIDER_PRESETS}
_SCHEMA_DONE_CONNS: set[int] = set()


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
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
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
                "SELECT base_url, api_key_enc FROM phonos_model_providers WHERE provider_id = %s",
                (provider_id,),
            ).fetchone()
            if prow:
                pd = _rdict(prow)
                inst_key = open_secret(pd.get("api_key_enc") or "")
                inst_url = (pd.get("base_url") or "").strip()
        except Exception:
            pass

    effective_url = user_url or inst_url or base_url
    if user_key:
        return {"api_key": user_key, "key_source": "user", "has_key": True, "base_url": effective_url}
    if env_val:
        return {"api_key": env_val, "key_source": "environment", "has_key": True, "base_url": effective_url}
    if inst_key:
        return {"api_key": inst_key, "key_source": "instance", "has_key": True, "base_url": effective_url}
    return {"api_key": "", "key_source": "none", "has_key": bool(preset.get("keyless")), "base_url": effective_url}


def resolve_nvidia_api_key(user_id: Optional[int] = None, db=None) -> str:
    """Drop-in runtime helper for all NVIDIA NIM routes & workers."""
    creds = resolve_provider_credentials("nvidia-nim", user_id=user_id, db=db)
    return creds["api_key"]


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
            "keyless": bool(preset.get("keyless")),
            "has_key": bool(resolved["api_key"]) or bool(preset.get("keyless")),
            "key_source": resolved["key_source"],
            "description": preset["description"],
        })
    return out


def save_instance_provider(db, provider_id: str, *, api_key: Optional[str] = None, base_url: Optional[str] = None, clear_key: bool = False, actor_id: Optional[int] = None) -> None:
    ensure_provider_schema(db)
    existing = db.execute("SELECT base_url, api_key_enc FROM phonos_model_providers WHERE provider_id = %s", (provider_id,)).fetchone()
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

    if existing:
        db.execute(
            "UPDATE phonos_model_providers SET base_url = %s, api_key_enc = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE provider_id = %s",
            (new_url, new_enc, actor_id, provider_id),
        )
    else:
        db.execute(
            "INSERT INTO phonos_model_providers (provider_id, base_url, api_key_enc, updated_by) VALUES (%s, %s, %s, %s)",
            (provider_id, new_url, new_enc, actor_id),
        )
    # Sync NVIDIA key into process env so background worker subprocesses also pick it up immediately
    if provider_id == "nvidia-nim":
        plain = open_secret(new_enc)
        if plain:
            os.environ["NVIDIA_API_KEY"] = plain
        elif clear_key and os.environ.get("NVIDIA_API_KEY") and not os.path.exists(".env"):
            os.environ.pop("NVIDIA_API_KEY", None)
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
    preset = PRESET_BY_ID.get(provider_id, {})
    creds = resolve_provider_credentials(provider_id, user_id=user_id, db=db)
    api_key = (raw_key or "").strip() or creds["api_key"]
    target_url = (custom_url or "").strip() or preset.get("test_url") or creds["base_url"]
    if not target_url:
        return {"ok": False, "error": "No test URL configured for this provider."}
    try:
        validate_endpoint_url(target_url)
    except ValueError as ve:
        return {"ok": False, "error": str(ve)}

    headers = {"Accept": "application/json", "User-Agent": "phonos-provider-probe/1.0"}
    auth_type = preset.get("auth_type", "bearer")
    if api_key:
        if auth_type == "x-api-key":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    elif not preset.get("keyless"):
        return {"ok": False, "error": "No API key configured (set an Instance key, Personal BYOK key, or environment variable)."}

    t0 = time.monotonic()
    req = urllib.request.Request(target_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            ms = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "status_code": resp.status, "latency_ms": ms, "key_source": creds["key_source"]}
    except urllib.error.HTTPError as he:
        ms = int((time.monotonic() - t0) * 1000)
        # Some POST-only inference endpoints return 405 on GET when reachable & authenticated
        if he.code == 405:
            return {"ok": True, "status_code": 405, "latency_ms": ms, "detail": "Endpoint reachable (POST-only)."}
        return {"ok": False, "status_code": he.code, "latency_ms": ms, "error": f"HTTP {he.code}: {he.reason}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
