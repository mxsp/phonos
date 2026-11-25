"""API authentication and authorization helpers."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from .database import get_db


def generate_api_key() -> tuple[str, str]:
    """
    Generate an API key and its hash.

    Returns:
        Tuple of (plain_key, key_hash)
    """
    # Generate a secure random key
    key = f"pk_{secrets.token_urlsafe(32)}"

    # Hash the key for storage
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    return key, key_hash


def generate_key_id() -> str:
    """Generate a unique key ID."""
    return f"key_{secrets.token_urlsafe(16)}"


def create_api_key(
    user_id: int,
    name: str,
    scopes: list[str] | None = None,
    expires_in_days: int = None,
) -> tuple[str, dict]:
    """
    Create a new API key for a user.

    Returns:
        Tuple of (plain_key, key_info_dict)
    """
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    plain_key, key_hash = generate_api_key()
    key_id = generate_key_id()

    scopes_str = ",".join(scopes) if scopes else None
    expires_at = None

    if expires_in_days:
        expires_at = datetime.now() + timedelta(days=expires_in_days)

    db.execute(
        """
        INSERT INTO api_keys (key_id, key_hash, user_id, name, scopes, expires_at, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, 1)
        """,
        (key_id, key_hash, user_id, name, scopes_str, expires_at),
    )

    db.commit()

    key_info = {
        "key_id": key_id,
        "user_id": user_id,
        "name": name,
        "scopes": scopes,
        "expires_at": expires_at,
        "is_active": True,
    }

    return plain_key, key_info


def validate_api_key(api_key: str) -> dict:
    """
    Validate an API key and return associated user info.

    Returns:
        User info dict if valid, None otherwise
    """
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Hash the provided key
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    result = db.execute(
        """
        SELECT ak.id, ak.key_id, ak.user_id, ak.name, ak.scopes, ak.expires_at, ak.is_active, u.username, u.email
        FROM api_keys ak
        JOIN users u ON ak.user_id = u.id
        JOIN user_credentials uc ON u.id = uc.user_id
        WHERE ak.key_hash = %s AND ak.is_active = 1
        """,
        (key_hash,),
    )

    row = result.fetchone()
    if not row:
        return None

    key_info = dict(row)

    # Check if key is expired
    if key_info["expires_at"]:
        expires_at = datetime.fromisoformat(key_info["expires_at"])
        if expires_at < datetime.now():
            return None

    # Update last_used_at
    db.execute(
        """
        UPDATE api_keys
        SET last_used_at = CURRENT_TIMESTAMP
        WHERE key_id = %s
        """,
        (key_info["key_id"],),
    )
    db.commit()

    # Parse scopes
    if key_info["scopes"]:
        key_info["scopes"] = key_info["scopes"].split(",")
    else:
        key_info["scopes"] = []

    return key_info


def get_user_api_keys(user_id: int) -> list[dict]:
    """Get all API keys for a user."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, key_id, user_id, name, scopes, last_used_at, expires_at, is_active, created_at
        FROM api_keys
        WHERE user_id = %s
        ORDER BY created_at DESC
        """,
        (user_id,),
    )

    keys = []
    for row in result.fetchall():
        key_info = dict(row)
        key_info["is_active"] = bool(key_info["is_active"])

        if key_info["scopes"]:
            key_info["scopes"] = key_info["scopes"].split(",")
        else:
            key_info["scopes"] = []

        # Check if expired
        key_info["is_expired"] = False
        if key_info["expires_at"]:
            expires_at = datetime.fromisoformat(key_info["expires_at"])
            key_info["is_expired"] = expires_at < datetime.now()
        else:
            key_info["is_expired"] = False

        keys.append(key_info)

    return keys


def revoke_api_key(key_id: str) -> bool:
    """Revoke (deactivate) an API key."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        UPDATE api_keys
        SET is_active = 0
        WHERE key_id = %s
        """,
        (key_id,),
    )

    db.commit()
    return result.rowcount > 0


def delete_api_key(key_id: str) -> bool:
    """Permanently delete an API key."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        DELETE FROM api_keys
        WHERE key_id = %s
        """,
        (key_id,),
    )

    db.commit()
    return result.rowcount > 0


def check_rate_limit(key_id: str, endpoint: str, limit: int = 100, window_minutes: int = 60) -> bool:
    """
    Check if an API key has exceeded rate limit for an endpoint.

    Returns:
        True if within rate limit, False if exceeded
    """
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    window_start = datetime.now() - timedelta(minutes=window_minutes)

    result = db.execute(
        """
        SELECT request_count
        FROM api_rate_limits
        WHERE key_id = %s AND endpoint = %s AND window_start > %s
        """,
        (key_id, endpoint, window_start),
    )

    row = result.fetchone()

    if not row:
        # Create new rate limit record
        db.execute(
            """
            INSERT INTO api_rate_limits (key_id, endpoint, request_count, window_start)
            VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
            """,
            (key_id, endpoint),
        )
        db.commit()
        return True

    request_count = row["request_count"]

    if request_count >= limit:
        return False

    # Increment count
    db.execute(
        """
        UPDATE api_rate_limits
        SET request_count = request_count + 1
        WHERE key_id = %s AND endpoint = %s
        """,
        (key_id, endpoint),
    )
    db.commit()

    return True


def reset_rate_limit(key_id: str, endpoint: str) -> None:
    """Reset rate limit for a specific key and endpoint."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    db.execute(
        """
        DELETE FROM api_rate_limits
        WHERE key_id = %s AND endpoint = %s
        """,
        (key_id, endpoint),
    )

    db.commit()
