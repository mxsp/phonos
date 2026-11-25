"""
Phonos HPC Compute API — classical compute clusters, quantum-style.

Foundation for ``phonos.hpc_cluster1.compute("a + b", a=1, b=2)``:

  Worker side (semi-external machines, containerized):
    Workers dial OUT to this API over HTTPS — no inbound ports needed on the
    worker, so machines behind NAT/firewalls work. Auth is a per-cluster
    bearer token (only the SHA-256 hash is stored server-side).

    POST /api/hpc/worker/register       announce capabilities, heartbeat
    POST /api/hpc/worker/lease          long-poll: atomically claim next task
    POST /api/hpc/worker/result/<uuid>  deliver result/error

  User side (login session or notebook kernel token via login_required):
    GET  /api/hpc/clusters                     list clusters + online status
    POST /api/hpc/clusters/<name>/tasks        submit {code, args, timeout}
    GET  /api/hpc/tasks/<uuid>                 poll status/result (owner only)
    POST /api/hpc/tasks/<uuid>/cancel          cancel a queued task

  Admin:
    POST   /api/hpc/clusters                   create cluster -> one-time token
    DELETE /api/hpc/clusters/<name>            deactivate cluster

Tasks are Python source strings executed by the worker in a subprocess with
numpy/scipy/pandas available; ``args`` become variables in the task
namespace. Results must be JSON-serializable (the worker converts numpy
types and falls back to repr()).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime

from flask import Blueprint, g, jsonify, request, session

from phonos_app.helpers.auth import admin_required, login_required
from phonos_app.helpers.database import get_db

logger = logging.getLogger(__name__)

hpc_bp = Blueprint("hpc", __name__, url_prefix="/api/hpc")

# A worker is considered online if it registered or leased within this window.
ONLINE_WINDOW_SECONDS = 90
# Long-poll cap for /worker/lease (eventlet-friendly: time.sleep yields).
MAX_LEASE_WAIT = 25
# Payload caps
MAX_CODE_BYTES = 100 * 1024
MAX_ARGS_BYTES = 1 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
DEFAULT_TASK_TIMEOUT = 300
MAX_TASK_TIMEOUT = 3600


# ---------------------------------------------------------------------------
# Schema (idempotent self-heal, mirrors the api_quantum pattern)
# ---------------------------------------------------------------------------

def _ensure_tables():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS hpc_clusters (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT,
            description TEXT,
            token_hash TEXT NOT NULL,
            capabilities JSONB DEFAULT '{}'::jsonb,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            last_seen TIMESTAMP,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS hpc_tasks (
            id SERIAL PRIMARY KEY,
            task_uuid TEXT NOT NULL UNIQUE,
            cluster_id INTEGER NOT NULL REFERENCES hpc_clusters(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'queued',
            code TEXT NOT NULL,
            args JSONB DEFAULT '{}'::jsonb,
            timeout_seconds INTEGER NOT NULL DEFAULT 300,
            result JSONB,
            error TEXT,
            stdout TEXT,
            duration_ms INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_hpc_tasks_lease
        ON hpc_tasks (cluster_id, status, id)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_hpc_tasks_user
        ON hpc_tasks (user_id, created_at DESC)
    """)
    db.commit()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cluster_from_bearer():
    """Resolve the worker's cluster from the Authorization bearer token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    token_hash = _hash_token(token)
    db = get_db()
    row = db.execute(
        "SELECT * FROM hpc_clusters WHERE active = TRUE"
    ).fetchall()
    for cluster in row:
        if hmac.compare_digest(cluster["token_hash"], token_hash):
            return cluster
    return None


def _touch_cluster(cluster_id: int, capabilities=None):
    db = get_db()
    if capabilities is not None:
        db.execute(
            "UPDATE hpc_clusters SET last_seen = CURRENT_TIMESTAMP, capabilities = %s WHERE id = %s",
            (json.dumps(capabilities), cluster_id),
        )
    else:
        db.execute(
            "UPDATE hpc_clusters SET last_seen = CURRENT_TIMESTAMP WHERE id = %s",
            (cluster_id,),
        )
    db.commit()


def _is_online(last_seen) -> bool:
    if not last_seen:
        return False
    try:
        return (datetime.utcnow() - last_seen).total_seconds() < ONLINE_WINDOW_SECONDS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Worker endpoints (bearer-token auth)
# ---------------------------------------------------------------------------

@hpc_bp.route("/worker/register", methods=["POST"])
def worker_register():
    """Worker announces itself (heartbeat + capabilities)."""
    _ensure_tables()
    cluster = _cluster_from_bearer()
    if not cluster:
        return jsonify({"error": "Invalid worker token"}), 401
    data = request.get_json(silent=True) or {}
    caps = data.get("capabilities") or {}
    if not isinstance(caps, dict):
        caps = {}
    _touch_cluster(cluster["id"], capabilities=caps)
    logger.info("HPC worker registered for cluster '%s' (caps: %s)", cluster["name"], list(caps)[:8])
    return jsonify({
        "cluster": cluster["name"],
        "lease_endpoint": "/api/hpc/worker/lease",
        "max_lease_wait": MAX_LEASE_WAIT,
    })


@hpc_bp.route("/worker/lease", methods=["POST"])
def worker_lease():
    """Long-poll for the next queued task; claims it atomically.

    ``FOR UPDATE SKIP LOCKED`` makes multiple workers per cluster safe.
    """
    cluster = _cluster_from_bearer()
    if not cluster:
        return jsonify({"error": "Invalid worker token"}), 401

    data = request.get_json(silent=True) or {}
    try:
        wait = min(MAX_LEASE_WAIT, max(0, int(data.get("wait", 20))))
    except (TypeError, ValueError):
        wait = 20

    db = get_db()
    deadline = time.monotonic() + wait
    while True:
        row = db.execute("""
            UPDATE hpc_tasks
            SET status = 'running', started_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM hpc_tasks
                WHERE cluster_id = %s AND status = 'queued'
                ORDER BY id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING task_uuid, code, args, timeout_seconds
        """, (cluster["id"],)).fetchone()
        db.commit()
        if row:
            _touch_cluster(cluster["id"])
            return jsonify({
                "task": {
                    "task_uuid": row["task_uuid"],
                    "code": row["code"],
                    "args": row["args"] or {},
                    "timeout_seconds": row["timeout_seconds"],
                }
            })
        if time.monotonic() >= deadline:
            _touch_cluster(cluster["id"])
            return jsonify({"task": None})
        time.sleep(1.0)


@hpc_bp.route("/worker/result/<task_uuid>", methods=["POST"])
def worker_result(task_uuid):
    """Worker delivers the outcome of a leased task."""
    cluster = _cluster_from_bearer()
    if not cluster:
        return jsonify({"error": "Invalid worker token"}), 401

    if request.content_length and request.content_length > MAX_RESULT_BYTES:
        # Mark the task failed rather than dropping it silently.
        db = get_db()
        db.execute("""
            UPDATE hpc_tasks SET status = 'error', error = 'Result exceeded size limit',
                   finished_at = CURRENT_TIMESTAMP
            WHERE task_uuid = %s AND cluster_id = %s AND status = 'running'
        """, (task_uuid, cluster["id"]))
        db.commit()
        return jsonify({"error": "Result too large"}), 413

    data = request.get_json(silent=True) or {}
    status = "done" if data.get("status") == "done" else "error"
    result = data.get("result")
    error = (data.get("error") or "")[:10000] or None
    stdout = (data.get("stdout") or "")[:20000] or None
    try:
        duration_ms = int(data.get("duration_ms", 0))
    except (TypeError, ValueError):
        duration_ms = 0

    db = get_db()
    updated = db.execute("""
        UPDATE hpc_tasks
        SET status = %s, result = %s, error = %s, stdout = %s,
            duration_ms = %s, finished_at = CURRENT_TIMESTAMP
        WHERE task_uuid = %s AND cluster_id = %s AND status = 'running'
        RETURNING id
    """, (status, json.dumps(result) if result is not None else None,
          error, stdout, duration_ms, task_uuid, cluster["id"])).fetchone()
    db.commit()
    _touch_cluster(cluster["id"])
    if not updated:
        return jsonify({"error": "Task not found or not running"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# User endpoints (login session or notebook kernel token)
# ---------------------------------------------------------------------------

@hpc_bp.route("/clusters", methods=["GET"])
@login_required
def list_clusters():
    """Clusters visible to users, with live online/queue info."""
    _ensure_tables()
    db = get_db()
    rows = db.execute("""
        SELECT c.name, c.display_name, c.description, c.capabilities, c.last_seen,
               COUNT(t.id) FILTER (WHERE t.status = 'queued')  AS queued,
               COUNT(t.id) FILTER (WHERE t.status = 'running') AS running
        FROM hpc_clusters c
        LEFT JOIN hpc_tasks t ON t.cluster_id = c.id AND t.status IN ('queued', 'running')
        WHERE c.active = TRUE
        GROUP BY c.id
        ORDER BY c.name
    """).fetchall()
    return jsonify({"clusters": [{
        "name": r["name"],
        "display_name": r["display_name"] or r["name"],
        "description": r["description"] or "",
        "capabilities": r["capabilities"] or {},
        "online": _is_online(r["last_seen"]),
        "queued": int(r["queued"] or 0),
        "running": int(r["running"] or 0),
    } for r in rows]})


@hpc_bp.route("/clusters/<name>/tasks", methods=["POST"])
@login_required
def submit_task(name):
    """Queue a compute task on a cluster."""
    _ensure_tables()
    db = get_db()
    cluster = db.execute(
        "SELECT id, last_seen FROM hpc_clusters WHERE name = %s AND active = TRUE", (name,)
    ).fetchone()
    if not cluster:
        return jsonify({"error": f"Unknown HPC cluster '{name}'"}), 404

    data = request.get_json(silent=True) or {}
    code = data.get("code") or ""
    if not isinstance(code, str) or not code.strip():
        return jsonify({"error": "code is required"}), 400
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return jsonify({"error": "code exceeds size limit"}), 413

    args = data.get("args") or {}
    if not isinstance(args, dict):
        return jsonify({"error": "args must be an object"}), 400
    try:
        args_json = json.dumps(args)
    except (TypeError, ValueError) as e:
        return jsonify({"error": f"args must be JSON-serializable: {e}"}), 400
    if len(args_json.encode("utf-8")) > MAX_ARGS_BYTES:
        return jsonify({"error": "args exceed size limit"}), 413

    try:
        timeout = min(MAX_TASK_TIMEOUT, max(1, int(data.get("timeout", DEFAULT_TASK_TIMEOUT))))
    except (TypeError, ValueError):
        timeout = DEFAULT_TASK_TIMEOUT

    task_uuid = secrets.token_hex(16)
    db.execute("""
        INSERT INTO hpc_tasks (task_uuid, cluster_id, user_id, code, args, timeout_seconds)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (task_uuid, cluster["id"], session["user_id"], code, args_json, timeout))
    db.commit()
    return jsonify({
        "task_uuid": task_uuid,
        "status": "queued",
        "cluster": name,
        "cluster_online": _is_online(cluster["last_seen"]),
    })


@hpc_bp.route("/tasks/<task_uuid>", methods=["GET"])
@login_required
def get_task(task_uuid):
    db = get_db()
    row = db.execute("""
        SELECT t.*, c.name AS cluster_name FROM hpc_tasks t
        JOIN hpc_clusters c ON c.id = t.cluster_id
        WHERE t.task_uuid = %s AND t.user_id = %s
    """, (task_uuid, session["user_id"])).fetchone()
    if not row:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({
        "task_uuid": row["task_uuid"],
        "cluster": row["cluster_name"],
        "status": row["status"],
        "result": row["result"],
        "error": row["error"],
        "stdout": row["stdout"],
        "duration_ms": row["duration_ms"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    })


@hpc_bp.route("/tasks/<task_uuid>/cancel", methods=["POST"])
@login_required
def cancel_task(task_uuid):
    db = get_db()
    row = db.execute("""
        UPDATE hpc_tasks SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP
        WHERE task_uuid = %s AND user_id = %s AND status = 'queued'
        RETURNING id
    """, (task_uuid, session["user_id"])).fetchone()
    db.commit()
    if not row:
        return jsonify({"error": "Task not found or not cancellable"}), 404
    return jsonify({"ok": True, "status": "cancelled"})


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@hpc_bp.route("/clusters", methods=["POST"])
@admin_required
def create_cluster():
    """Create a cluster; returns the worker token ONCE (only its hash is stored)."""
    _ensure_tables()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip().lower()
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        return jsonify({"error": "name must be alphanumeric (plus - and _)"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM hpc_clusters WHERE name = %s", (name,)).fetchone()
    if existing:
        return jsonify({"error": f"Cluster '{name}' already exists"}), 409

    token = "phc_" + secrets.token_urlsafe(32)
    db.execute("""
        INSERT INTO hpc_clusters (name, display_name, description, token_hash, created_by)
        VALUES (%s, %s, %s, %s, %s)
    """, (name, data.get("display_name") or name, data.get("description") or "",
          _hash_token(token), session["user_id"]))
    db.commit()
    logger.info("HPC cluster '%s' created by user %s", name, session["user_id"])
    return jsonify({
        "name": name,
        "worker_token": token,
        "note": "Store this token now — it is not retrievable later.",
    }), 201


@hpc_bp.route("/clusters/<name>", methods=["DELETE"])
@admin_required
def deactivate_cluster(name):
    db = get_db()
    row = db.execute(
        "UPDATE hpc_clusters SET active = FALSE WHERE name = %s RETURNING id", (name,)
    ).fetchone()
    db.commit()
    if not row:
        return jsonify({"error": "Cluster not found"}), 404
    return jsonify({"ok": True})


def register_hpc_routes(app):
    app.register_blueprint(hpc_bp)
    app.logger.info("HPC compute API routes registered")
