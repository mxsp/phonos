"""Team domain helpers."""
from __future__ import annotations

import re
import psycopg2
import logging
from datetime import datetime
from typing import Iterable, Optional

from ..app import app

logger = logging.getLogger(__name__)

TEAM_ROLE_OWNER = "owner"
TEAM_ROLE_ADMIN = "admin"
TEAM_ROLE_MEMBER = "member"
TEAM_ROLE_BILLING = "billing"
TEAM_MUTABLE_ROLES = {
    TEAM_ROLE_ADMIN,
    TEAM_ROLE_MEMBER,
    TEAM_ROLE_BILLING,
}


def _slugify(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"[^a-z0-9\s-]", "", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-")


def generate_unique_team_slug(db, name: str) -> str:
    base = _slugify(name) or "team"
    slug = base
    suffix = 1
    while db.execute("SELECT 1 FROM teams WHERE slug = %s", (slug,)).fetchone():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def generate_unique_team_name(db, name: str) -> str:
    base = name or "Team"
    team_name = base
    suffix = 1
    while db.execute("SELECT 1 FROM teams WHERE name = %s", (team_name,)).fetchone():
        team_name = f"{base} {suffix}"
        suffix += 1
    return team_name


def serialize_datetime(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def serialize_team(row):
    data = dict(row)
    data.setdefault("team_id", data.get("id"))
    data.setdefault("organization_id", data.get("team_id") or data.get("id"))
    data["created_at"] = serialize_datetime(data.get("created_at"))
    data["updated_at"] = serialize_datetime(data.get("updated_at"))
    row_keys = row.keys() if hasattr(row, "keys") else data.keys()
    if "home_node_count" in row_keys:
        data["home_node_count"] = row["home_node_count"] or 0
    else:
        data.setdefault("home_node_count", 0)
    if "primary_node_id" in row_keys:
        data["primary_node_id"] = row["primary_node_id"]
    return data


def parse_worker_types(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item for item in (chunk.strip() for chunk in value.split(",")) if item]


def _supported_model_ids() -> set[str]:
    from phonos_app import helpers

    return helpers.SUPPORTED_MODEL_IDS


def normalize_worker_types(worker_types: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for worker_type in worker_types:
        candidate = (worker_type or "").strip()
        if not candidate or candidate not in _supported_model_ids():
            continue
        if candidate not in unique:
            unique.append(candidate)
    return unique


def serialize_team_node(row) -> dict:
    data = dict(row)
    data["worker_types"] = parse_worker_types(data.get("worker_types"))
    data["is_primary"] = bool(data.get("is_primary"))
    data["created_at"] = serialize_datetime(data.get("created_at"))
    team_id = data.get("team_id")
    if team_id is not None:
        data["queues"] = [
            f"queue:job:{worker_type}:team:{team_id}"
            for worker_type in data["worker_types"]
        ]
    return data


def get_team_node_rows(db, team_id: int):
    return db.execute(
        """
        SELECT id, team_id, node_id, display_name, worker_types, is_primary, created_at
        FROM team_nodes
        WHERE team_id = %s
        ORDER BY is_primary DESC, created_at ASC
        """,
        (team_id,),
    ).fetchall()


def team_has_home_node_for_model(
    db, team_id: Optional[int], model_type: str
) -> bool:
    if not team_id:
        return False
    rows = db.execute(
        "SELECT worker_types FROM team_nodes WHERE team_id = %s",
        (team_id,),
    ).fetchall()
    for row in rows:
        if model_type in parse_worker_types(row["worker_types"]):
            return True
    return False


def determine_job_target_queue(
    db, team_id: Optional[int], model_type: str
) -> Optional[str]:
    if not team_id:
        return None
    if not team_has_home_node_for_model(db, team_id, model_type):
        return None
    try:
        team_fragment = str(int(team_id))
    except (TypeError, ValueError):
        return None
    return f"queue:job:{model_type}:team:{team_fragment}"


def is_valid_public_key(pem: str) -> bool:
    if not pem:
        return False
    stripped = pem.strip()
    return stripped.startswith("-----BEGIN PUBLIC KEY-----") and stripped.endswith(
        "-----END PUBLIC KEY-----"
    )


def get_team_membership(db, user_id: int, team_id: int) -> str:
    if not user_id or not team_id:
        return None
    row = db.execute(
        "SELECT role FROM team_members WHERE team_id = %s AND user_id = %s",
        (team_id, user_id),
    ).fetchone()
    if not row:
        team_row = db.execute(
            "SELECT creator_id FROM teams WHERE id = %s",
            (team_id,),
        ).fetchone()
        if team_row and team_row["creator_id"] == user_id:
            return TEAM_ROLE_OWNER
    return row["role"] if row else None


def serialize_team_member(row):
    data = dict(row)
    data["joined_at"] = serialize_datetime(data.get("joined_at"))
    if "role" in data and data["role"] is not None:
        data["role"] = str(data["role"]).lower()
    return data


def ensure_personal_team(db, user_id: int) -> int:
    """Ensure the user has a personal team and return its identifier."""
    if not user_id:
        return None

    personal_slug = f"user-{user_id}-personal"
    existing = db.execute(
        "SELECT id FROM teams WHERE slug = %s", (personal_slug,)
    ).fetchone()
    if existing:
        db.execute(
            "INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, %s) ON CONFLICT (team_id, user_id) DO NOTHING",
            (existing["id"], user_id, TEAM_ROLE_OWNER),
        )
        db.commit()
        return existing["id"]

    user_row = db.execute(
        "SELECT username, legal_name FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if not user_row:
        logger.error("Failed to load user %s when creating personal team.", user_id)
        return None

    if user_row["legal_name"]:
        base_name = user_row["legal_name"].split()[0]
    else:
        base_name = user_row["username"] or "Member"

    initial_workspace_name = f"{base_name}'s Workspace"
    workspace_name = generate_unique_team_name(db, initial_workspace_name)

    db.execute("BEGIN")
    try:
        cursor = db.execute(
            "INSERT INTO teams (name, slug, description, creator_id) VALUES (%s, %s, %s, %s)",
            (workspace_name, personal_slug, "Personal workspace", user_id),
        )
        team_id = cursor.lastrowid
        db.execute(
            "INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, %s)",
            (team_id, user_id, TEAM_ROLE_OWNER),
        )
        db.commit()
        return team_id
    except psycopg2.IntegrityError:
        db.rollback()
        existing = db.execute(
            "SELECT id FROM teams WHERE slug = %s", (personal_slug,)
        ).fetchone()
        if existing:
            db.execute(
                "INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, %s) ON CONFLICT (team_id, user_id) DO NOTHING",
                (existing["id"], user_id, TEAM_ROLE_OWNER),
            )
            db.commit()
            return existing["id"]
        logger.error(
            "Failed to recover personal team for user %s after integrity error.",
            user_id,
            exc_info=True,
        )
        return None
    except Exception:
        db.rollback()
        logger.error(
            "Failed to create personal team for user %s.", user_id, exc_info=True
        )
        return None
