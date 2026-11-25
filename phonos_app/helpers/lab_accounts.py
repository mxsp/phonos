"""Gimel-style Local Account Manager, 4-Tier Lab Roles & Default Lab Team helpers.

Unifies instance-level Lab Roles (``admin``, ``scientist``, ``operator``, ``viewer``)
with Phonos's established per-project roles (``Project Manager``, ``Project Contributor``,
``Project Viewer``) and supports zero-SMTP plain-username local accounts.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import bcrypt
from config import Config

logger = logging.getLogger(__name__)

# Canonical 4 Lab Roles
ROLE_ADMIN = "admin"
ROLE_SCIENTIST = "scientist"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

CANONICAL_SYSTEM_ROLES = (ROLE_ADMIN, ROLE_SCIENTIST, ROLE_OPERATOR, ROLE_VIEWER)

# Accept Gimel-compatible aliases seamlessly in API & CLI
ROLE_ALIASES = {
    "admin": ROLE_ADMIN,
    "administrator": ROLE_ADMIN,
    "scientist": ROLE_SCIENTIST,
    "researcher": ROLE_SCIENTIST,
    "staff": ROLE_SCIENTIST,
    "operator": ROLE_OPERATOR,
    "member": ROLE_OPERATOR,
    "worker": ROLE_OPERATOR,
    "viewer": ROLE_VIEWER,
    "guest": ROLE_VIEWER,
    "customer": ROLE_VIEWER,
    "spectator": ROLE_VIEWER,
}

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._@+-]{1,119}$")

_WEAK_PASSWORDS = {
    "password", "passwort", "12345678", "123456789", "1234567890",
    "qwertyuiop", "qwertzuiop", "changeme", "letmein1", "welcome1", "admin123",
}

DEFAULT_LOCAL_LAB_CREDITS = 100000

_SCHEMA_READY_CONNS: set[int] = set()
_DUMMY_HASH: bytes | None = None


def constant_time_burn(raw_password: str) -> None:
    """Run one bcrypt verification on unknown/inactive users to prevent timing enumeration."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = bcrypt.hashpw(b"phonos-dummy-timing-guard", bcrypt.gensalt(rounds=10))
    try:
        bcrypt.checkpw((raw_password or "").encode("utf-8"), _DUMMY_HASH)
    except Exception:
        pass


def normalize_system_role(raw: Optional[str], *, default: str = ROLE_SCIENTIST) -> str:
    if not raw:
        return default
    cleaned = str(raw).strip().lower()
    if cleaned not in ROLE_ALIASES:
        raise ValueError(f"Invalid role '{raw}'. Allowed roles: admin, scientist (staff), operator (worker), viewer.")
    return ROLE_ALIASES[cleaned]


def normalize_username(raw: str) -> str:
    value = (raw or "").strip().lower()
    if len(value) < 2 or len(value) > 120:
        raise ValueError("Username must be between 2 and 120 characters.")
    if not USERNAME_RE.match(value):
        raise ValueError(
            "Username may only contain lowercase letters, digits, and . _ - + @, starting with a letter or digit."
        )
    return value


def synthesize_local_email(username: str, explicit_email: Optional[str] = None) -> str:
    email = (explicit_email or "").strip().lower()
    if email and "@" in email:
        return email
    if "@" in username:
        return username.lower()
    return f"{username.lower()}@local.phonos"


def check_password_strength(password: str, *, username: str = "", email: str = "") -> None:
    pw = password or ""
    if len(pw) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if len(pw) > 200:
        raise ValueError("Password must be at most 200 characters.")
    low = pw.strip().lower()
    if low in _WEAK_PASSWORDS:
        raise ValueError("This password is on common wordlists — please choose a stronger passphrase.")
    if len(set(low)) < 4:
        raise ValueError("Password contains too few distinct characters.")
    local_part = (email or "").split("@")[0].lower()
    for token in filter(None, [username.lower(), local_part]):
        if len(token) >= 4 and token in low:
            raise ValueError("Password must not contain your username or email.")


def local_lan_ips() -> List[str]:
    ips: List[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except Exception:
        pass
    return ips


def _row_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return {}


def ensure_lab_accounts_schema(db) -> None:
    """Idempotently ensure ``users`` role/active columns, ``lab_settings``, and ``account_audit_log``."""
    conn_id = id(db)
    if conn_id in _SCHEMA_READY_CONNS:
        try:
            db.execute("SELECT system_role FROM users LIMIT 1")
            db.execute("SELECT 1 FROM lab_settings LIMIT 1")
            return
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            _SCHEMA_READY_CONNS.discard(conn_id)

    for col_sql in (
        "ALTER TABLE users ADD COLUMN system_role TEXT NOT NULL DEFAULT 'scientist'",
        "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN credit_limit INTEGER",
    ):
        try:
            db.execute(col_sql)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS lab_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS account_audit_log (
                id SERIAL PRIMARY KEY,
                at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target_user_id INTEGER,
                detail TEXT
            )
            """
        )
        # Keep existing admin accounts synced to system_role='admin'
        db.execute("UPDATE users SET system_role = 'admin' WHERE is_admin = 1 AND (system_role IS NULL OR system_role != 'admin')")
        db.commit()
        _SCHEMA_READY_CONNS.add(conn_id)
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("ensure_lab_accounts_schema note: %s", exc)


def get_lab_setting(db, key: str, default: str = "") -> str:
    ensure_lab_accounts_schema(db)
    try:
        row = db.execute("SELECT setting_value FROM lab_settings WHERE setting_key = %s", (key,)).fetchone()
        if row:
            d = _row_dict(row)
            return str(d.get("setting_value") if "setting_value" in d else row[0])
    except Exception:
        pass
    return default


def set_lab_setting(db, key: str, value: str) -> None:
    ensure_lab_accounts_schema(db)
    existing = db.execute("SELECT 1 FROM lab_settings WHERE setting_key = %s", (key,)).fetchone()
    if existing:
        db.execute(
            "UPDATE lab_settings SET setting_value = %s, updated_at = CURRENT_TIMESTAMP WHERE setting_key = %s",
            (str(value), key),
        )
    else:
        db.execute(
            "INSERT INTO lab_settings (setting_key, setting_value) VALUES (%s, %s)",
            (key, str(value)),
        )


def record_account_audit(db, actor: str, action: str, target_user_id: Optional[int], detail: Optional[dict] = None) -> None:
    ensure_lab_accounts_schema(db)
    try:
        db.execute(
            "INSERT INTO account_audit_log (actor, action, target_user_id, detail) VALUES (%s, %s, %s, %s)",
            (actor or "system", action, target_user_id, json.dumps(detail or {})),
        )
    except Exception as exc:
        logger.warning("Failed to record account audit log (%s): %s", action, exc)


def get_user_auth_state(db, user_id: Optional[int]) -> Dict[str, Any]:
    """Return ``{'exists': bool, 'active': bool, 'is_admin': bool, 'system_role': str}``."""
    if not user_id:
        return {"exists": False, "active": False, "is_admin": False, "system_role": ROLE_VIEWER}
    ensure_lab_accounts_schema(db)
    try:
        row = db.execute(
            "SELECT id, is_admin, system_role, is_active FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
    except Exception:
        row = db.execute("SELECT id, is_admin FROM users WHERE id = %s", (user_id,)).fetchone()
    if not row:
        return {"exists": False, "active": False, "is_admin": False, "system_role": ROLE_VIEWER}
    d = _row_dict(row)
    is_admin = bool(d.get("is_admin", 0))
    raw_role = d.get("system_role") or (ROLE_ADMIN if is_admin else ROLE_SCIENTIST)
    try:
        role = normalize_system_role(raw_role)
    except ValueError:
        role = ROLE_ADMIN if is_admin else ROLE_SCIENTIST
    if is_admin:
        role = ROLE_ADMIN
    active = bool(d.get("is_active", 1) if d.get("is_active") is not None else 1)
    return {"exists": True, "active": active, "is_admin": is_admin or (role == ROLE_ADMIN), "system_role": role}


def can_create_projects(db, user_id: int) -> bool:
    state = get_user_auth_state(db, user_id)
    return state["active"] and state["system_role"] in (ROLE_ADMIN, ROLE_SCIENTIST)


def can_run_compute(db, user_id: int) -> bool:
    state = get_user_auth_state(db, user_id)
    return state["active"] and state["system_role"] in (ROLE_ADMIN, ROLE_SCIENTIST, ROLE_OPERATOR)


def ensure_default_lab_team(db, creator_id: int, lab_name: str = "Phonos Lab") -> Optional[int]:
    """Ensure the shared Default Lab Team exists and return its ``team_id``."""
    ensure_lab_accounts_schema(db)
    saved_id = get_lab_setting(db, "default_team_id", "")
    if saved_id.isdigit():
        row = db.execute("SELECT id FROM teams WHERE id = %s", (int(saved_id),)).fetchone()
        if row:
            return int(_row_dict(row).get("id") or row[0])

    slug = "lab-workspace"
    existing = db.execute("SELECT id FROM teams WHERE slug = %s", (slug,)).fetchone()
    if existing:
        tid = int(_row_dict(existing).get("id") or existing[0])
        set_lab_setting(db, "default_team_id", str(tid))
        return tid

    clean_name = (lab_name or "Phonos Lab").strip()[:120] or "Phonos Lab"
    cur = db.execute(
        "INSERT INTO teams (name, slug, description, creator_id) VALUES (%s, %s, %s, %s) RETURNING id",
        (clean_name, slug, "Default shared laboratory workspace", creator_id),
    )
    fetched = cur.fetchone()
    tid = int(_row_dict(fetched).get("id") if fetched else cur.lastrowid)
    db.execute(
        "INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, 'owner')",
        (tid, creator_id),
    )
    set_lab_setting(db, "default_team_id", str(tid))
    set_lab_setting(db, "instance_name", clean_name)
    return tid


def get_default_lab_team_id(db) -> Optional[int]:
    saved_id = get_lab_setting(db, "default_team_id", "")
    if saved_id.isdigit():
        row = db.execute("SELECT id FROM teams WHERE id = %s", (int(saved_id),)).fetchone()
        if row:
            return int(_row_dict(row).get("id") or row[0])
    row = db.execute("SELECT id FROM teams WHERE slug = 'lab-workspace'").fetchone()
    if row:
        return int(_row_dict(row).get("id") or row[0])
    return None


def set_user_lab_team_role(db, user_id: int, lab_team_role: Optional[str]) -> Optional[str]:
    """Assign or remove user membership in the Default Lab Team.

    ``lab_team_role`` may be ``'none'``/``''`` (assigned projects only), or
    ``'owner' | 'admin' | 'member' | 'billing'`` (aliases ``'contributor' -> 'member'``,
    ``'viewer' -> 'billing'``).
    """
    tid = get_default_lab_team_id(db)
    if not tid:
        return None
    raw = (lab_team_role or "").strip().lower()
    if raw in ("", "none", "assigned_only"):
        db.execute("DELETE FROM team_members WHERE team_id = %s AND user_id = %s AND role != 'owner'", (tid, user_id))
        return None
    role_map = {
        "owner": "owner",
        "admin": "admin",
        "manager": "admin",
        "member": "member",
        "contributor": "member",
        "billing": "billing",
        "viewer": "billing",
        "spectator": "billing",
    }
    mapped = role_map.get(raw, "member")
    existing = db.execute("SELECT role FROM team_members WHERE team_id = %s AND user_id = %s", (tid, user_id)).fetchone()
    if existing:
        db.execute("UPDATE team_members SET role = %s WHERE team_id = %s AND user_id = %s", (mapped, tid, user_id))
    else:
        db.execute("INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, %s)", (tid, user_id, mapped))
    return mapped


def assert_not_last_admin(db, target_user_id: int) -> None:
    ensure_lab_accounts_schema(db)
    rows = db.execute(
        "SELECT id FROM users WHERE (is_admin = 1 OR system_role = 'admin') AND is_active = 1"
    ).fetchall()
    active_admin_ids = {int(_row_dict(r).get("id") or r[0]) for r in rows}
    if target_user_id in active_admin_ids and len(active_admin_ids) <= 1:
        raise ValueError("Cannot remove, deactivate, or demote the last active administrator.")


def create_local_account(
    db,
    *,
    username: str,
    password: str,
    system_role: str = ROLE_SCIENTIST,
    email: Optional[str] = None,
    legal_name: Optional[str] = None,
    credits: Optional[int] = None,
    lab_team_role: Optional[str] = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """Create a local user account immediately (no SMTP or 2FA required)."""
    from plan_service import DEFAULT_PLAN
    from .teams import ensure_personal_team
    from .trusted_tester import initial_access_tier

    ensure_lab_accounts_schema(db)
    norm_username = normalize_username(username)
    norm_role = normalize_system_role(system_role)
    norm_email = synthesize_local_email(norm_username, email)
    check_password_strength(password, username=norm_username, email=norm_email)

    if db.execute("SELECT 1 FROM users WHERE LOWER(username) = %s", (norm_username,)).fetchone():
        raise ValueError(f"Username '{norm_username}' is already taken.")
    if db.execute("SELECT 1 FROM user_credentials WHERE LOWER(email) = %s", (norm_email,)).fetchone():
        raise ValueError(f"Email '{norm_email}' is already registered.")

    is_admin_int = 1 if norm_role == ROLE_ADMIN else 0
    if credits is None:
        credits = DEFAULT_LOCAL_LAB_CREDITS if getattr(Config, "LOCAL_LAB_MODE", True) else 100

    user_plan = "Enterprise" if (getattr(Config, "LOCAL_LAB_MODE", True) and norm_role in (ROLE_ADMIN, ROLE_SCIENTIST)) else DEFAULT_PLAN
    access_tier = "full" if getattr(Config, "LOCAL_LAB_MODE", True) else initial_access_tier()
    hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    user_uuid = str(uuid.uuid4())
    display_name = (legal_name or norm_username).strip()[:160]

    cur = db.execute(
        """
        INSERT INTO users (user_id, username, legal_name, plan, credits, is_admin, system_role, is_active, access_tier)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s)
        RETURNING id
        """,
        (user_uuid, norm_username, display_name, user_plan, int(credits), is_admin_int, norm_role, access_tier),
    )
    fetched = cur.fetchone()
    user_id = int(_row_dict(fetched).get("id") if fetched else cur.lastrowid)

    db.execute(
        """
        INSERT INTO user_credentials (user_id, email, password, email_verified, plan, tos_accepted, tos_accepted_at, tos_ip_address)
        VALUES (%s, %s, %s, 1, %s, 1, CURRENT_TIMESTAMP, 'local')
        """,
        (user_id, norm_email, hashed_pw, user_plan),
    )
    db.execute(
        "UPDATE users SET user_credential_id = (SELECT id FROM user_credentials WHERE user_id = %s) WHERE id = %s",
        (user_id, user_id),
    )
    if lab_team_role and lab_team_role not in ("none", "assigned_only"):
        set_user_lab_team_role(db, user_id, lab_team_role)

    record_account_audit(
        db,
        actor=actor,
        action="account_create",
        target_user_id=user_id,
        detail={"username": norm_username, "system_role": norm_role, "lab_team_role": lab_team_role or "none"},
    )
    db.commit()

    try:
        ensure_personal_team(db, user_id)
    except Exception as exc:
        logger.debug("Personal workspace creation note for %s: %s", norm_username, exc)

    return {
        "id": user_id,
        "username": norm_username,
        "email": norm_email,
        "system_role": norm_role,
        "is_admin": bool(is_admin_int),
        "active": True,
        "credits": int(credits),
    }
