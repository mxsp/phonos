"""Per-user Wolfram Cloud credential storage.

Each non-admin user can register their own Wolfram Cloud account (username/password)
via the Notebook IDE Settings panel. Admin users continue to use the container's
global WOLFRAM_USERNAME / WOLFRAM_PASSWORD environment variables.

Credentials are stored AES-256-GCM encrypted (same scheme as runtime_levels.config
and user_model_crypto) keyed by the host's ENCRYPTION_KEY.
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


def save_credentials(user_id: int, username: str, password: str, email: Optional[str] = None) -> None:
    """Insert or update the user's Wolfram credentials (encrypted at rest)."""
    if not username or not password:
        raise ValueError("username and password are required")
    key = _MASTER_KEY()
    if not key:
        raise EnvironmentError("ENCRYPTION_KEY not set on host")

    username_enc = _encrypt_str(username, key)
    password_enc = _encrypt_str(password, key)
    wolfram_id_email = email or username
    db = get_db()
    with _LOCK:
        db.execute("""
            INSERT INTO user_wolfram_credentials
                (user_id, username_enc, password_enc, wolfram_id_email, last_error, updated_at)
            VALUES (%s, %s, %s, %s, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                username_enc = EXCLUDED.username_enc,
                password_enc = EXCLUDED.password_enc,
                wolfram_id_email = COALESCE(EXCLUDED.wolfram_id_email, user_wolfram_credentials.wolfram_id_email),
                last_error = NULL,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, username_enc, password_enc, wolfram_id_email))
        db.commit()


def delete_credentials(user_id: int) -> bool:
    db = get_db()
    with _LOCK:
        cur = db.execute(
            "DELETE FROM user_wolfram_credentials WHERE user_id = %s",
            (user_id,),
        )
        db.commit()
        return cur.rowcount > 0


def has_credentials(user_id: int) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM user_wolfram_credentials WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return bool(row)


def get_credentials(user_id: int) -> Optional[Tuple[str, str]]:
    """Return (username, password) decrypted, or None if no record."""
    key = _MASTER_KEY()
    if not key:
        return None
    db = get_db()
    row = db.execute(
        "SELECT username_enc, password_enc FROM user_wolfram_credentials WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return _decrypt_str(row["username_enc"], key), _decrypt_str(row["password_enc"], key)
    except Exception as e:
        logger.warning(f"Failed to decrypt Wolfram creds for user {user_id}: {e}")
        return None


def get_credentials_metadata(user_id: int) -> dict:
    """Return non-sensitive info about the user's Wolfram setup (for Settings UI)."""
    db = get_db()
    row = db.execute("""
        SELECT username_enc, wolfram_id_email, last_verified_at, last_error, updated_at
        FROM user_wolfram_credentials WHERE user_id = %s
    """, (user_id,)).fetchone()
    if not row:
        return {"configured": False}

    email = row["wolfram_id_email"]
    # Populate from decrypted username for accounts saved before wolfram_id_email was tracked
    if not email and row["username_enc"]:
        key = _MASTER_KEY()
        if key:
            try:
                decrypted = _decrypt_str(row["username_enc"], key)
                if decrypted:
                    email = decrypted
            except Exception:
                pass

    return {
        "configured": True,
        "wolfram_id_email": email,
        "last_verified_at": row["last_verified_at"].isoformat() if row["last_verified_at"] else None,
        "last_error": row["last_error"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def record_verification(user_id: int, ok: bool, error: Optional[str] = None, email: Optional[str] = None) -> None:
    """Update last_verified_at / last_error after a Wolfram cloud attempt."""
    db = get_db()
    with _LOCK:
        if ok:
            db.execute("""
                UPDATE user_wolfram_credentials
                SET last_verified_at = CURRENT_TIMESTAMP,
                    last_error = NULL,
                    wolfram_id_email = COALESCE(%s, wolfram_id_email)
                WHERE user_id = %s
            """, (email, user_id))
        else:
            db.execute("""
                UPDATE user_wolfram_credentials
                SET last_error = %s,
                    wolfram_id_email = COALESCE(%s, wolfram_id_email)
                WHERE user_id = %s
            """, (error, email, user_id))
        db.commit()
