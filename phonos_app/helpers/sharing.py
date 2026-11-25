"""Shared links management helpers."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from .database import get_db


def generate_share_token() -> str:
    """Generate a cryptographically secure share token."""
    return secrets.token_urlsafe(32)


def create_shared_link(
    resource_type: str,
    resource_id: int,
    creator_id: int,
    permissions: str = "read",
    expires_in_days: int = None,
) -> dict:
    """Create a shared link for a resource."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    token = generate_share_token()
    expires_at = None

    if expires_in_days:
        expires_at = datetime.now() + timedelta(days=expires_in_days)

    db.execute(
        """
        INSERT INTO shared_links (token, resource_type, resource_id, creator_id, permissions, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (token, resource_type, resource_id, creator_id, permissions, expires_at),
    )

    db.commit()

    return {
        "token": token,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "creator_id": creator_id,
        "permissions": permissions,
        "expires_at": expires_at,
        "access_count": 0,
        "is_active": True,
    }


def get_shared_link(token: str) -> dict:
    """Get shared link by token."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, token, resource_type, resource_id, creator_id, permissions,
               expires_at, access_count, is_active, created_at
        FROM shared_links
        WHERE token = %s
        """,
        (token,),
    )

    row = result.fetchone()
    if not row:
        return None

    link = dict(row)

    # Check if link is expired
    if link["expires_at"]:
        expires_at = datetime.fromisoformat(link["expires_at"])
        if expires_at < datetime.now():
            link["is_expired"] = True
        else:
            link["is_expired"] = False
    else:
        link["is_expired"] = False

    link["is_active"] = bool(link["is_active"])

    return link


def increment_access_count(token: str) -> None:
    """Increment the access count for a shared link."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    db.execute(
        """
        UPDATE shared_links
        SET access_count = access_count + 1
        WHERE token = %s
        """,
        (token,),
    )

    db.commit()


def get_user_shared_links(user_id: int) -> list[dict]:
    """Get all shared links created by a user."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, token, resource_type, resource_id, creator_id, permissions,
               expires_at, access_count, is_active, created_at
        FROM shared_links
        WHERE creator_id = %s
        ORDER BY created_at DESC
        """,
        (user_id,),
    )

    links = []
    for row in result.fetchall():
        link = dict(row)
        link["is_active"] = bool(link["is_active"])

        # Check if link is expired
        if link["expires_at"]:
            expires_at = datetime.fromisoformat(link["expires_at"])
            link["is_expired"] = expires_at < datetime.now()
        else:
            link["is_expired"] = False

        links.append(link)

    return links


def revoke_shared_link(token: str) -> bool:
    """Revoke (deactivate) a shared link."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        UPDATE shared_links
        SET is_active = 0
        WHERE token = %s
        """,
        (token,),
    )

    db.commit()
    return result.rowcount > 0


def delete_shared_link(token: str) -> bool:
    """Permanently delete a shared link."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        DELETE FROM shared_links
        WHERE token = %s
        """,
        (token,),
    )

    db.commit()
    return result.rowcount > 0


def validate_shared_link(token: str) -> tuple[bool, str | None]:
    """
    Validate a shared link.

    Returns:
        Tuple of (is_valid, error_message)
    """
    link = get_shared_link(token)

    if not link:
        return False, "Link not found"

    if not link["is_active"]:
        return False, "Link has been revoked"

    if link.get("is_expired", False):
        return False, "Link has expired"

    return True, None
