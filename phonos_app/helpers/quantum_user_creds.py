"""Per-user Quantum execution settings + IBM Quantum credential storage.

Two related concerns live here:

1. ``user_quantum_settings`` — non-secret per-user preferences surfaced by the
   Quantum Settings UI (execution mode, credential source, preferred backend,
   session options). Spec defaults: simulator / platform-managed /
   automatic / lazy session / 60s timeout.

2. ``user_ibm_credentials`` — a user's *personal* IBM Quantum API key, stored
   AES-256-GCM encrypted at rest (same scheme as ``user_wolfram_credentials``).
   The plaintext key is NEVER returned by any function in this module and is
   only fetched internally at job-dispatch time. ``get_credentials_metadata``
   exposes only ``has_key`` + non-secret fields for the Settings UI.

Platform-managed credentials live in the host ``.env`` (``IBM_QUANTUM_API_KEY``)
and are never persisted to these tables.

Per-user isolation: every helper takes ``user_id`` and scopes its SQL with
``WHERE user_id = %s``; there are no cross-user lookups and the FK has
``ON DELETE CASCADE`` so a deleted user's secrets are purged with them.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Tuple

from phonos_app.helpers.database import get_db

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MASTER_KEY = lambda: os.environ.get("ENCRYPTION_KEY")

VALID_EXECUTION_MODES = ("simulator", "hardware")
VALID_CREDENTIAL_SOURCES = ("platform", "personal")
MIN_SESSION_TIMEOUT = 30
MAX_SESSION_TIMEOUT = 300
DEFAULT_SESSION_TIMEOUT = 60
VALID_SIMULATOR_BACKENDS = ("ibm_simulator", "aer_simulator", "local_testing")
HARDWARE_BACKENDS = (
    "ibm_fez", "ibm_marrakesh", "ibm_kingston",
)


def _encrypt_str(value: str, key: str) -> str:
    from phonos_app.helpers.user_model_crypto import encrypt_data
    return encrypt_data(value, key)


def _decrypt_str(encrypted: str, key: str) -> str:
    from phonos_app.helpers.user_model_crypto import decrypt_data
    result = decrypt_data(encrypted, key)
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    if isinstance(result, str):
        return result
    raise ValueError("Unexpected decrypt result type")


# ---------------------------------------------------------------------------
# Quantum settings (non-secret)
# ---------------------------------------------------------------------------

def get_settings(user_id: int) -> dict:
    """Return the user's quantum settings, falling back to spec defaults."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT execution_mode, credential_source, preferred_backend, "
            "auto_start_session, session_timeout_sec, last_error, updated_at "
            "FROM user_quantum_settings WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    except Exception as e:
        logger.warning("quantum settings lookup failed for user %s: %s", user_id, e)
        return _default_settings()
    if not row:
        return _default_settings()
    return {
        "execution_mode": row["execution_mode"],
        "credential_source": row["credential_source"],
        "preferred_backend": row["preferred_backend"],
        "auto_start_session": bool(row["auto_start_session"]),
        "session_timeout_sec": int(row["session_timeout_sec"]),
        "last_error": row["last_error"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def save_settings(
    user_id: int,
    execution_mode: str = "simulator",
    credential_source: str = "platform",
    preferred_backend: str = "automatic",
    auto_start_session: bool = False,
    session_timeout_sec: int = DEFAULT_SESSION_TIMEOUT,
) -> dict:
    if execution_mode not in VALID_EXECUTION_MODES:
        raise ValueError(f"execution_mode must be one of {VALID_EXECUTION_MODES}")
    if credential_source not in VALID_CREDENTIAL_SOURCES:
        raise ValueError(f"credential_source must be one of {VALID_CREDENTIAL_SOURCES}")
    if not preferred_backend or not isinstance(preferred_backend, str):
        raise ValueError("preferred_backend must be a non-empty string")
    timeout = max(MIN_SESSION_TIMEOUT, min(MAX_SESSION_TIMEOUT, int(session_timeout_sec)))
    db = get_db()
    with _LOCK:
        db.execute("""
            INSERT INTO user_quantum_settings
                (user_id, execution_mode, credential_source, preferred_backend,
                 auto_start_session, session_timeout_sec, last_error, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                execution_mode = EXCLUDED.execution_mode,
                credential_source = EXCLUDED.credential_source,
                preferred_backend = EXCLUDED.preferred_backend,
                auto_start_session = EXCLUDED.auto_start_session,
                session_timeout_sec = EXCLUDED.session_timeout_sec,
                updated_at = CURRENT_TIMESTAMP
        """, (
            user_id, execution_mode, credential_source, preferred_backend,
            1 if auto_start_session else 0, timeout,
        ))
        db.commit()
    return get_settings(user_id)


def _default_settings() -> dict:
    return {
        "execution_mode": "simulator",
        "credential_source": "platform",
        "preferred_backend": "automatic",
        "auto_start_session": False,
        "session_timeout_sec": DEFAULT_SESSION_TIMEOUT,
        "last_error": None,
        "updated_at": None,
    }


def record_settings_error(user_id: int, error: Optional[str]) -> None:
    db = get_db()
    with _LOCK:
        db.execute(
            "UPDATE user_quantum_settings SET last_error = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = %s",
            (error, user_id),
        )
        db.commit()


# ---------------------------------------------------------------------------
# IBM personal credentials (secret)
# ---------------------------------------------------------------------------

def save_credentials(
    user_id: int,
    api_key: str,
    instance_crn: Optional[str] = None,
    preferred_backend: Optional[str] = None,
) -> None:
    """Insert or update the user's personal IBM Quantum API key (encrypted)."""
    if not api_key:
        raise ValueError("api_key is required")
    key = _MASTER_KEY()
    if not key:
        raise EnvironmentError("ENCRYPTION_KEY not set on host")

    api_key_enc = _encrypt_str(api_key, key)
    db = get_db()
    with _LOCK:
        db.execute("""
            INSERT INTO user_ibm_credentials
                (user_id, api_key_enc, instance_crn, preferred_backend, last_error, updated_at)
            VALUES (%s, %s, %s, %s, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                api_key_enc = EXCLUDED.api_key_enc,
                instance_crn = EXCLUDED.instance_crn,
                preferred_backend = EXCLUDED.preferred_backend,
                last_error = NULL,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, api_key_enc, instance_crn, preferred_backend))
        db.commit()


def delete_credentials(user_id: int) -> bool:
    db = get_db()
    with _LOCK:
        cur = db.execute(
            "DELETE FROM user_ibm_credentials WHERE user_id = %s",
            (user_id,),
        )
        db.commit()
        return cur.rowcount > 0


def has_credentials(user_id: int) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM user_ibm_credentials WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return bool(row)


def get_credentials(user_id: int) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """Return (api_key, instance_crn, preferred_backend) decrypted, or None.

    INTERNAL ONLY — the plaintext key is for the runtime dispatch layer and
    must never be returned from an API response.
    """
    key = _MASTER_KEY()
    if not key:
        return None
    db = get_db()
    row = db.execute(
        "SELECT api_key_enc, instance_crn, preferred_backend FROM user_ibm_credentials WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    try:
        api_key = _decrypt_str(row["api_key_enc"], key)
    except Exception as e:
        logger.warning("Failed to decrypt IBM creds for user %s: %s", user_id, e)
        return None
    return api_key, row["instance_crn"], row["preferred_backend"]


def get_credentials_metadata(user_id: int) -> dict:
    """Return non-sensitive info about the user's IBM credentials (for UI).

    Never includes the API key value — frontend shows ``API Key Saved /
    **************`` based on ``has_key``.
    """
    db = get_db()
    row = db.execute(
        "SELECT instance_crn, preferred_backend, last_verified_at, last_error, updated_at "
        "FROM user_ibm_credentials WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if not row:
        return {"has_key": False, "instance_crn": None, "preferred_backend": None,
                "last_verified_at": None, "last_error": None, "updated_at": None}
    return {
        "has_key": True,
        "instance_crn": row["instance_crn"],
        "preferred_backend": row["preferred_backend"],
        "last_verified_at": row["last_verified_at"].isoformat() if row["last_verified_at"] else None,
        "last_error": row["last_error"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def record_verification(user_id: int, ok: bool, error: Optional[str] = None) -> None:
    db = get_db()
    with _LOCK:
        if ok:
            db.execute(
                "UPDATE user_ibm_credentials SET last_verified_at = CURRENT_TIMESTAMP, "
                "last_error = NULL, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
                (user_id,),
            )
        else:
            db.execute(
                "UPDATE user_ibm_credentials SET last_error = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = %s",
                (error, user_id),
            )
        db.commit()


def get_platform_credentials() -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """Return (api_key, instance_crn, default_backend) from host .env, or None.

    Platform-managed path: the IBM key in ``IBM_QUANTUM_API_KEY`` is used when a
    user selects "Platform Managed". Never persisted to the DB; never returned
    to the frontend. Returns None when the env var is unset.
    """
    api_key = os.environ.get("IBM_QUANTUM_API_KEY")
    if not api_key:
        return None
    instance = os.environ.get("IBM_QUANTUM_INSTANCE") or None
    default_backend = os.environ.get("IBM_QUANTUM_DEFAULT_BACKEND") or None
    return api_key, instance, default_backend


def resolve_credentials(user_id: int) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    """Resolve the credential source for a job dispatch.

    Returns ``(api_key_or_None, source, instance_crn, preferred_backend)`` where
    ``source`` is ``'personal'`` / ``'platform'`` / ``'none'``. A ``None``
    api_key with ``source='none'`` means no usable credential was found.
    """
    settings = get_settings(user_id)
    if settings["credential_source"] == "personal":
        creds = get_credentials(user_id)
        if creds:
            api_key, instance, pref = creds
            return api_key, "personal", instance, pref or settings["preferred_backend"]
        return None, "none", None, settings["preferred_backend"]
    # platform-managed
    plat = get_platform_credentials()
    if plat:
        api_key, instance, default_backend = plat
        return api_key, "platform", instance, default_backend or settings["preferred_backend"]
    return None, "none", None, settings["preferred_backend"]