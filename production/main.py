"""Application entry point for the Phonos web server."""
from __future__ import annotations

import atexit
import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

# Add production directory and parent directory to path for imports
_production_dir = os.path.dirname(os.path.abspath(__file__))
_phonos_dir = os.path.dirname(_production_dir)
sys.path.insert(0, _production_dir)
sys.path.insert(0, _phonos_dir)

import redis
from flask import jsonify, request

from config import Config
from database import get_db_connection, init_db
from host_resolution import resolve_host
from mrs_service.runtime import (
    ACTIVE_JOBS_HASH,
    WORKER_HEALTH_HASH,
    infer_model_id_from_active_job_state,
)
from phonos_app import app, logger, redis_pool, resolve_debug_mode, socketio
from phonos_app.database_updater import start_job_update_listener
from secure_tunnel import (
    decrypt_payload as tunnel_decrypt_payload,
    derive_tunnel_key,
    encrypt_payload as tunnel_encrypt_payload,
)
import phonos_app.routes  # noqa: F401 - Import routes to register them with Flask

PREFERRED_HOSTNAME = "0.0.0.0"
FALLBACK_HOSTS = ("127.0.0.1", "localhost")

_TUNNEL_KEY = derive_tunnel_key()

def _get_redis_client() -> redis.Redis | None:
    """Return a Redis client suitable for snapshot collection."""

    client = redis.Redis(connection_pool=redis_pool, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError as exc:  # pragma: no cover - runtime guard
        logger.warning("Secure tunnel snapshot cannot reach Redis: %s", exc)
        return None
    return client


def _count_model_queue_depth(redis_conn: redis.Redis | None, model_id: str) -> int:
    """Return total queued jobs across all shards for ``model_id``."""

    if redis_conn is None:
        return 0

    total = 0
    pattern = f"queue:job:{model_id}*"
    try:
        for key in redis_conn.scan_iter(match=pattern):
            if not isinstance(key, str):
                try:
                    key = key.decode("utf-8")  # type: ignore[assignment]
                except Exception:  # pragma: no cover - defensive decoding
                    continue
            if not key.startswith(f"queue:job:{model_id}"):
                continue
            try:
                total += redis_conn.llen(key)
            except redis.RedisError:
                continue
    except redis.RedisError:
        return 0
    return total


def _gather_active_job_metrics(
    redis_conn: redis.Redis | None,
) -> tuple[int, dict[str, int]]:
    """Return total active jobs and counts grouped by model identifier."""

    counts = {model["id"]: 0 for model in Config.SUPPORTED_MODELS}
    if redis_conn is None:
        return 0, counts

    try:
        active_job_entries = redis_conn.hgetall(ACTIVE_JOBS_HASH)
    except redis.RedisError as exc:  # pragma: no cover - runtime guard
        logger.warning("Failed to read active jobs for kicker snapshot: %s", exc)
        return 0, counts

    total = len(active_job_entries)
    for active_job_json in active_job_entries.values():
        if not active_job_json:
            continue
        try:
            active_job_state = json.loads(active_job_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        model_id = infer_model_id_from_active_job_state(active_job_state)
        if model_id and model_id in counts:
            counts[model_id] += 1

    return total, counts


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _get_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def _resolve_premium_monitoring(cursor: sqlite3.Cursor) -> bool:
    """Return True if any account is entitled to premium monitoring."""

    if not _table_exists(cursor, "user_credentials"):
        return False

    try:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM user_credentials
            WHERE LOWER(COALESCE(plan, '')) IN ('premium', 'managed premium', 'enterprise')
            """
        )
    except sqlite3.Error:
        return False

    row = cursor.fetchone()
    return bool(row and row[0])


def _resolve_online_user_count(cursor: sqlite3.Cursor, since: datetime) -> int:
    timestamp_str = since.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if _table_exists(cursor, "login_history"):
            cursor.execute(
                "SELECT COUNT(DISTINCT user_id) FROM login_history WHERE timestamp > ?",
                (timestamp_str,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0

        if _table_exists(cursor, "user_sessions"):
            columns = _get_columns(cursor, "user_sessions")
            if "last_seen_at" in columns:
                cursor.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM user_sessions WHERE last_seen_at > ?",
                    (timestamp_str,),
                )
            elif "updated_at" in columns:
                cursor.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM user_sessions WHERE updated_at > ?",
                    (timestamp_str,),
                )
            else:
                cursor.execute("SELECT COUNT(DISTINCT user_id) FROM user_sessions")
            row = cursor.fetchone()
            return row[0] if row else 0
    except sqlite3.Error as exc:  # pragma: no cover - runtime guard
        logger.warning("Failed to resolve online users for kicker snapshot: %s", exc)
    return 0


def _fetch_advanced_stats(cursor: sqlite3.Cursor) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    try:
        cursor.execute("SELECT COUNT(id) FROM users")
        stats["total_users"] = cursor.fetchone()[0]

        cursor.execute("SELECT status, COUNT(id) FROM jobs GROUP BY status")
        job_counts = {row[0]: row[1] for row in cursor.fetchall()}
        stats["jobs_completed"] = job_counts.get("completed", 0)
        stats["jobs_failed"] = job_counts.get("failed", 0)
        stats["jobs_queued"] = job_counts.get("queued", 0)

        total_finished = stats["jobs_completed"] + stats["jobs_failed"]
        if total_finished > 0:
            stats["job_success_rate"] = f"{(stats['jobs_completed'] / total_finished) * 100:.2f}%"
        else:
            stats["job_success_rate"] = "N/A"

        cursor.execute(
            "SELECT AVG(execution_time) FROM jobs WHERE status = 'completed' AND execution_time IS NOT NULL"
        )
        avg_time_row = cursor.fetchone()
        avg_time = avg_time_row[0] if avg_time_row else None
        stats["avg_processing_time"] = f"{avg_time:.2f}" if avg_time else "N/A"

        cursor.execute("SELECT COUNT(id) FROM projects")
        stats["total_projects"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(id) FROM files")
        stats["total_files"] = cursor.fetchone()[0]
    except sqlite3.Error as exc:  # pragma: no cover - runtime guard
        logger.warning("Failed to gather advanced stats for kicker snapshot: %s", exc)
    return stats


def _collect_kicker_snapshot() -> dict[str, Any]:
    redis_conn = _get_redis_client()

    online_workers = {model["id"]: 0 for model in Config.SUPPORTED_MODELS}
    worker_health: dict[str, str] = {}
    if redis_conn is not None:
        try:
            worker_health = redis_conn.hgetall(WORKER_HEALTH_HASH)  # type: ignore[assignment]
        except redis.RedisError as exc:  # pragma: no cover - runtime guard
            logger.warning("Failed to read worker health for kicker snapshot: %s", exc)

    cutoff = time.time() - Config.WORKER_HEARTBEAT_TIMEOUT
    worker_uptime_details: list[dict[str, Any]] = []
    for worker_info_json in worker_health.values():
        try:
            worker_info = json.loads(worker_info_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

        worker_type = worker_info.get("type")
        last_seen = worker_info.get("last_seen") or worker_info.get("timestamp")
        try:
            last_seen_ts = float(last_seen) if last_seen is not None else None
        except (TypeError, ValueError):
            last_seen_ts = None

        if last_seen_ts is not None and last_seen_ts < cutoff:
            continue

        if worker_type in online_workers:
            online_workers[worker_type] += 1

        first_seen = worker_info.get("first_seen")
        try:
            first_seen_ts = float(first_seen) if first_seen is not None else None
        except (TypeError, ValueError):
            first_seen_ts = None

        if first_seen_ts is None:
            first_seen_ts = last_seen_ts

        if last_seen_ts is not None and first_seen_ts is not None:
            uptime_seconds = max(0.0, last_seen_ts - first_seen_ts)
            worker_uptime_details.append(
                {
                    "worker_id": worker_info.get("worker_id"),
                    "worker_type": worker_type,
                    "first_seen": first_seen_ts,
                    "last_seen": last_seen_ts,
                    "uptime_seconds": uptime_seconds,
                }
            )

    needed_workers = {
        model["id"]: _count_model_queue_depth(redis_conn, model["id"])
        for model in Config.SUPPORTED_MODELS
    }

    active_jobs, active_jobs_by_model = _gather_active_job_metrics(redis_conn)

    five_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    online_users = 0
    advanced_stats: dict[str, Any] = {}
    premium_monitoring = False
    try:
        with closing(get_db_connection()) as conn:
            cursor = conn.cursor()
            online_users = _resolve_online_user_count(cursor, five_minutes_ago)
            advanced_stats = _fetch_advanced_stats(cursor)
            premium_monitoring = _resolve_premium_monitoring(cursor)
    except sqlite3.Error as exc:  # pragma: no cover - runtime guard
        logger.warning("Failed to collect database metrics for kicker snapshot: %s", exc)

    return {
        "online_workers": online_workers,
        "needed_workers": needed_workers,
        "online_users": online_users,
        "active_jobs": active_jobs,
        "active_jobs_by_model": active_jobs_by_model,
        "worker_uptime": worker_uptime_details,
        "flags": {"premium_monitoring": premium_monitoring},
        **advanced_stats,
    }


@app.route("/api/internal/kicker-snapshot", methods=["POST"])
def kicker_snapshot() -> tuple[Any, int] | Any:
    """Expose secure monitoring data for remote kicker deployments."""

    request_json = request.get_json(silent=True) or {}
    encrypted_request = request_json.get("payload")
    if not isinstance(encrypted_request, str) or not encrypted_request:
        return jsonify({"error": "authentication required"}), 401

    payload = tunnel_decrypt_payload(encrypted_request, key=_TUNNEL_KEY, logger=logger)
    if payload is None:
        logger.warning("Rejected kicker snapshot request due to invalid payload")
        return jsonify({"error": "invalid payload"}), 401

    if isinstance(payload, dict):
        logger.debug("Received kicker snapshot request: %s", payload.get("request"))

    snapshot = _collect_kicker_snapshot()
    response_body = {
        "data": snapshot,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    token = tunnel_encrypt_payload(response_body, key=_TUNNEL_KEY, logger=logger)
    return jsonify({"payload": token})


def ensure_database() -> None:
    """Initialise the PostgreSQL database and apply schema updates."""
    logger.info("Ensuring PostgreSQL database is initialized...")
    with app.app_context():
        init_db()
    logger.info("Database initialization/update complete.")


def _cleanup_on_shutdown():
    """Cleanup resources on application shutdown."""
    from database import close_connection_pool
    from phonos_app.helpers.database import close_connection_pool as close_flask_pool

    logger.info("Shutting down application, cleaning up resources...")

    try:
        close_connection_pool()
    except Exception as e:
        logger.error(f"Error closing production database pool: {e}")

    try:
        close_flask_pool()
    except Exception as e:
        logger.error(f"Error closing Flask database pool: {e}")


def _signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    _cleanup_on_shutdown()
    sys.exit(0)


def main() -> None:
    """Run the Socket.IO development server."""
    # Register cleanup handlers
    atexit.register(_cleanup_on_shutdown)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    app.start_time = datetime.utcnow()
    os.makedirs(app.config["JOB_OUTPUT_DIR"], exist_ok=True)
    ensure_database()

    start_job_update_listener()

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()

    debug_mode = resolve_debug_mode()
    host = resolve_host(
        PREFERRED_HOSTNAME,
        FALLBACK_HOSTS,
        logger=logger,
        context="Socket.IO server",
    )
    logger.info("Starting Phonos web server (debug=%s, host=%s, port=%s)", debug_mode, host, args.port)
    # Note: SERVER_NAME is already configured from .env for external URL generation
    # Do not override it here, as it's needed for email links and other external URLs

    try:
        socketio.run(
            app,
            debug=debug_mode,
            host=host,
            port=args.port,
            allow_unsafe_werkzeug=True,
        )
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    finally:
        _cleanup_on_shutdown()


if __name__ == "__main__":
    main()
