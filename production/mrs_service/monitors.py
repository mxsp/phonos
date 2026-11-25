"""Monitoring threads for the Management & Routing Service."""
from __future__ import annotations

import json
import threading
import time

import redis

from encryption_util import canonical_dumps, verify_signature

from .key_cache import get_public_key_for_node
from .runtime import (
    ACTIVE_JOBS_HASH,
    JOB_TIMEOUT,
    WORKER_HEARTBEAT_CHANNEL,
    WORKER_HEALTH_HASH,
    WORKER_TIMEOUT,
    get_redis_connection,
    logger,
)
from .status import handle_job_failure


def monitor_jobs() -> None:
    """Periodically check for job timeouts and trigger retries."""
    logger.info("Job monitor started.")
    r = get_redis_connection()
    while True:
        try:
            active_jobs = r.hgetall(ACTIVE_JOBS_HASH)
            current_time = time.time()

            for job_id, job_info_json in active_jobs.items():
                job_info = json.loads(job_info_json)
                start_time = job_info.get("start_time", 0)

                if current_time - start_time > JOB_TIMEOUT:
                    logger.warning(
                        "Job %s timed out after %s seconds.",
                        job_id,
                        JOB_TIMEOUT,
                    )
                    job_data = json.loads(job_info.get("job_data", "{}"))
                    handle_job_failure(job_id, job_data, "Job processing timeout")
        except redis.exceptions.RedisError as exc:
            logger.error("Redis error in job monitor: %s", exc, exc_info=True)
        except Exception as exc:  # pragma: no cover - defensive coding
            logger.error("Unexpected error in job monitor: %s", exc, exc_info=True)

        time.sleep(30)


def monitor_worker_health() -> None:
    """Monitor worker heartbeats and prune unresponsive workers."""
    logger.info("Worker health monitor started.")
    r = get_redis_connection()
    pubsub = r.pubsub(ignore_subscribe_messages=True)

    try:
        pubsub.subscribe(WORKER_HEARTBEAT_CHANNEL)
        logger.info("Subscribed to worker heartbeat channel: %s", WORKER_HEARTBEAT_CHANNEL)
    except redis.exceptions.RedisError as exc:
        logger.critical("Could not subscribe to worker heartbeats: %s", exc)
        return

    def process_heartbeats() -> None:
        for message in pubsub.listen():
            try:
                signed_message = json.loads(message["data"])
                payload = signed_message.get("payload")
                signature = signed_message.get("signature")

                if not all([payload, signature]):
                    logger.warning("Invalid signed heartbeat format: %s", signed_message)
                    continue

                worker_id = payload.get("worker_id")
                public_key = payload.get("public_key") or get_public_key_for_node(worker_id)

                if not public_key:
                    logger.warning(
                        "Heartbeat from unknown or unauthorized worker '%s'. Ignoring.",
                        worker_id,
                    )
                    continue

                payload_bytes = canonical_dumps(payload).encode("utf-8")
                if not verify_signature(public_key, payload_bytes, signature):
                    logger.error(
                        "Invalid signature for heartbeat from worker '%s'. Ignoring.",
                        worker_id,
                    )
                    continue

                worker_type = payload.get("worker_type")
                logger.debug("Received verified heartbeat from %s worker: %s", worker_type, worker_id)

                current_time = time.time()

                existing_health: dict[str, float | str] | None = None
                try:
                    existing_raw = r.hget(WORKER_HEALTH_HASH, worker_id)
                    if isinstance(existing_raw, (bytes, bytearray)):
                        existing_raw = existing_raw.decode("utf-8")
                    if existing_raw:
                        existing_health = json.loads(existing_raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_health = None

                first_seen_value = None
                if existing_health:
                    first_seen_value = existing_health.get("first_seen")
                if first_seen_value is None:
                    first_seen_value = payload.get("timestamp")
                try:
                    first_seen = float(first_seen_value) if first_seen_value is not None else current_time
                except (TypeError, ValueError):
                    first_seen = current_time

                health_info = {
                    "first_seen": first_seen,
                    "last_seen": current_time,
                    "type": worker_type,
                    "worker_id": worker_id,
                }
                r.hset(WORKER_HEALTH_HASH, worker_id, json.dumps(health_info))

            except json.JSONDecodeError:
                logger.error("Could not decode heartbeat message: %s", message["data"])
            except Exception as exc:  # pragma: no cover - defensive coding
                logger.error("Error processing heartbeat: %s", exc, exc_info=True)

    heartbeat_thread = threading.Thread(
        target=process_heartbeats,
        daemon=True,
        name="HeartbeatProcessor",
    )
    heartbeat_thread.start()

    while True:
        try:
            workers = r.hgetall(WORKER_HEALTH_HASH)
            current_time = time.time()
            dead_workers = [
                worker_id
                for worker_id, health_json in workers.items()
                if current_time - json.loads(health_json).get("last_seen", 0) > WORKER_TIMEOUT
            ]
            if dead_workers:
                logger.warning("Pruning dead workers: %s", dead_workers)
                r.hdel(WORKER_HEALTH_HASH, *dead_workers)
        except redis.exceptions.RedisError as exc:
            logger.error("Redis error in worker health monitor: %s", exc, exc_info=True)
        except Exception as exc:  # pragma: no cover - defensive coding
            logger.error("Unexpected error in worker health monitor: %s", exc, exc_info=True)
        time.sleep(WORKER_TIMEOUT / 2)


__all__ = ["monitor_jobs", "monitor_worker_health"]
