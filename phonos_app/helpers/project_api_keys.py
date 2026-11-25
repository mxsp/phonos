"""
Project API keys: issuance, verification, rate limiting and the encrypted
payload store for the project-scoped API (``/api/project/v1``).

A project API key (``pj_`` prefix) is bound to exactly one project and is
*dual use*: it authenticates the caller and, via :mod:`project_api_crypto`,
derives the AES-256 data key used to encrypt the job inputs/params and results
the API persists. Only ``sha256(key)`` is stored for auth; the data key is
re-derived from the plaintext key on each request, so a DB dump alone cannot
decrypt the payload store.

Tables (created lazily, idempotently, portable across PostgreSQL/SQLite):
  - project_api_keys          : the keys + per-key salt + verifier
  - project_api_job_payloads  : enc_input / enc_result per API-submitted job
  - project_api_usage         : request log, also drives the 10/min rate limit
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional

from .database import get_db, _use_sqlite
from . import project_api_crypto as crypto

logger = logging.getLogger(__name__)

KEY_PREFIX = "pj_"
# Fixed rate limit for the project API: 10 requests per minute per key.
RATE_LIMIT_PER_MINUTE = 10

_ensured = False


def _pk_type() -> str:
    return "INTEGER PRIMARY KEY AUTOINCREMENT" if _use_sqlite() else "SERIAL PRIMARY KEY"


def ensure_tables(force: bool = False) -> None:
    """Create the project-API tables if they don't exist (idempotent)."""
    global _ensured
    # Always (re)ensure under SQLite because tests spin up fresh DB files; in
    # production (PostgreSQL) a process-level guard avoids needless DDL churn.
    if _ensured and not force and not _use_sqlite():
        return

    db = get_db()
    pk = _pk_type()

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS project_api_keys (
            id {pk},
            key_id TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL,
            key_prefix TEXT,
            user_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            name TEXT,
            enc_salt TEXT NOT NULL,
            enc_verifier TEXT NOT NULL,
            rate_limit_per_minute INTEGER DEFAULT 10,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            total_requests INTEGER DEFAULT 0
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_pak_hash ON project_api_keys(key_hash)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pak_project ON project_api_keys(project_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pak_user ON project_api_keys(user_id)")

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS project_api_job_payloads (
            id {pk},
            job_db_id INTEGER,
            job_uuid TEXT NOT NULL,
            api_key_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            enc_input TEXT,
            enc_result TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_pajp_uuid ON project_api_job_payloads(job_uuid)")

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS project_api_usage (
            id {pk},
            api_key_id INTEGER NOT NULL,
            endpoint TEXT,
            status_code INTEGER,
            ts_epoch INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_pau_key_time ON project_api_usage(api_key_id, ts_epoch)")
    db.commit()
    _ensured = True


# --- Issuance / management -------------------------------------------------
def create_project_api_key(user_id: int, project_id: int, name: str = None) -> Dict[str, Any]:
    """
    Create a new project-scoped API key.

    Returns the plaintext key (shown once) plus metadata. The plaintext key is
    never stored; only its SHA-256 hash, salt and a verifier are persisted.
    """
    ensure_tables()
    db = get_db()

    raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash = crypto.auth_hash(raw_key)
    key_prefix = raw_key[:11]  # 'pj_' + 8 chars
    key_uuid = str(uuid.uuid4())

    salt = crypto.new_salt()
    data_key = crypto.derive_data_key(raw_key, salt)
    verifier = crypto.make_verifier(data_key)

    result = db.execute(
        """
        INSERT INTO project_api_keys
            (key_id, key_hash, key_prefix, user_id, project_id, name,
             enc_salt, enc_verifier, rate_limit_per_minute, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
        RETURNING id, created_at
        """,
        (key_uuid, key_hash, key_prefix, user_id, project_id,
         name or "Project API Key", crypto.salt_to_text(salt), verifier,
         RATE_LIMIT_PER_MINUTE),
    )
    row = result.fetchone()
    db.commit()

    return {
        "id": row["id"],
        "key_id": key_uuid,
        "api_key": raw_key,  # only returned here, once
        "key_prefix": key_prefix,
        "name": name or "Project API Key",
        "project_id": project_id,
        "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else row["created_at"],
    }


def list_project_api_keys(user_id: int, project_id: int = None) -> List[Dict[str, Any]]:
    """List a user's project API keys (never returns the secret)."""
    ensure_tables()
    db = get_db()
    query = """
        SELECT id, key_id, key_prefix, name, project_id, rate_limit_per_minute,
               is_active, created_at, last_used_at, total_requests
        FROM project_api_keys
        WHERE user_id = %s
    """
    params: list = [user_id]
    if project_id is not None:
        query += " AND project_id = %s"
        params.append(project_id)
    query += " ORDER BY created_at DESC"

    keys = []
    for row in db.execute(query, tuple(params)).fetchall():
        keys.append({
            "id": row["id"],
            "key_id": row["key_id"],
            "key_prefix": row["key_prefix"],
            "name": row["name"],
            "project_id": row["project_id"],
            "rate_limit_per_minute": row["rate_limit_per_minute"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else row["created_at"],
            "last_used_at": row["last_used_at"].isoformat() if hasattr(row["last_used_at"], "isoformat") else row["last_used_at"],
            "total_requests": row["total_requests"] or 0,
        })
    return keys


def revoke_project_api_key(user_id: int, key_id: int) -> bool:
    """Deactivate a project API key the user owns."""
    ensure_tables()
    db = get_db()
    result = db.execute(
        "UPDATE project_api_keys SET is_active = 0 WHERE id = %s AND user_id = %s RETURNING id",
        (key_id, user_id),
    )
    ok = result.fetchone() is not None
    db.commit()
    return ok


# --- Verification ----------------------------------------------------------
def verify_project_api_key(raw_key: str) -> Optional[Dict[str, Any]]:
    """
    Verify a ``pj_`` key and return its auth context, including the derived
    ``data_key`` (bytes) used for payload encryption/decryption.

    Returns None if the key is unknown, inactive, or the derived key fails the
    stored verifier (corruption / mismatch).
    """
    if not raw_key or not raw_key.startswith(KEY_PREFIX):
        return None

    ensure_tables()
    db = get_db()
    key_hash = crypto.auth_hash(raw_key)

    row = db.execute(
        """
        SELECT id, key_id, user_id, project_id, enc_salt, enc_verifier,
               rate_limit_per_minute
        FROM project_api_keys
        WHERE key_hash = %s AND is_active = 1
        """,
        (key_hash,),
    ).fetchone()

    if not row:
        return None

    try:
        salt = crypto.salt_from_text(row["enc_salt"])
        data_key = crypto.derive_data_key(raw_key, salt)
    except Exception as e:
        logger.error("project_api_keys: failed to derive data key: %s", e)
        return None

    if not crypto.check_verifier(data_key, row["enc_verifier"]):
        logger.error("project_api_keys: verifier mismatch for key id=%s", row["id"])
        return None

    db.execute(
        "UPDATE project_api_keys SET last_used_at = CURRENT_TIMESTAMP, "
        "total_requests = total_requests + 1 WHERE id = %s",
        (row["id"],),
    )
    db.commit()

    return {
        "key_id": row["id"],
        "key_uuid": row["key_id"],
        "user_id": row["user_id"],
        "project_id": row["project_id"],
        "data_key": data_key,
        "rate_limit": row["rate_limit_per_minute"] or RATE_LIMIT_PER_MINUTE,
    }


# --- Rate limiting + usage log --------------------------------------------
def check_rate_limit(api_key_id: int, limit: int = RATE_LIMIT_PER_MINUTE) -> bool:
    """Return True if the key is under the per-minute limit (sliding 60s)."""
    db = get_db()
    cutoff = int(time.time()) - 60
    row = db.execute(
        "SELECT COUNT(*) AS c FROM project_api_usage WHERE api_key_id = %s AND ts_epoch > %s",
        (api_key_id, cutoff),
    ).fetchone()
    count = (row["c"] if row else 0) or 0
    return count < limit


def log_usage(api_key_id: int, endpoint: str, status_code: int) -> None:
    """Record a request (drives rate limiting + light audit)."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO project_api_usage (api_key_id, endpoint, status_code, ts_epoch) "
            "VALUES (%s, %s, %s, %s)",
            (api_key_id, endpoint, status_code, int(time.time())),
        )
        db.commit()
    except Exception as e:  # logging must never break the response
        logger.warning("project_api_keys: failed to log usage: %s", e)


# --- Encrypted payload store ----------------------------------------------
def store_job_input(job_db_id: int, job_uuid: str, api_key_id: int,
                    project_id: int, enc_input: str) -> None:
    """Persist the encrypted input blob for an API-submitted job."""
    db = get_db()
    db.execute(
        """
        INSERT INTO project_api_job_payloads
            (job_db_id, job_uuid, api_key_id, project_id, enc_input)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (job_db_id, job_uuid, api_key_id, project_id, enc_input),
    )
    db.commit()


def get_payload(job_uuid: str) -> Optional[Dict[str, Any]]:
    """Fetch the encrypted payload row for a job, if any."""
    db = get_db()
    row = db.execute(
        "SELECT id, job_db_id, job_uuid, api_key_id, project_id, enc_input, enc_result "
        "FROM project_api_job_payloads WHERE job_uuid = %s",
        (job_uuid,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "job_db_id": row["job_db_id"],
        "job_uuid": row["job_uuid"],
        "api_key_id": row["api_key_id"],
        "project_id": row["project_id"],
        "enc_input": row["enc_input"],
        "enc_result": row["enc_result"],
    }


def set_enc_result(job_uuid: str, enc_result: str) -> None:
    """Cache the encrypted result blob for a job (at-rest result encryption)."""
    db = get_db()
    db.execute(
        "UPDATE project_api_job_payloads SET enc_result = %s, "
        "updated_at = CURRENT_TIMESTAMP WHERE job_uuid = %s",
        (enc_result, job_uuid),
    )
    db.commit()
