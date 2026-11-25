"""Job status tracking utilities for the MRS."""
from __future__ import annotations

import json
import time
from typing import Dict, Optional

from .runtime import (
    ACTIVE_JOBS_HASH,
    DEAD_LETTER_QUEUE,
    MAX_RETRY_ATTEMPTS,
    NEW_JOBS_QUEUE,
    JOB_STATUS_CHANNEL_PREFIX,
    drain_pending_sequence,
    get_redis_connection,
    logger,
)


def publish_job_status(
    job_id: str,
    status: str,
    message: Optional[str] = None,
    *,
    data: Optional[Dict] = None,
) -> None:
    """Publish a job status update over Redis."""
    r = get_redis_connection()
    channel = f"{JOB_STATUS_CHANNEL_PREFIX}{job_id}"
    payload = {
        "job_id": job_id,
        "status": status,
        "message": message,
        "data": data or {},
    }
    r.publish(channel, json.dumps(payload))
    logger.info("Published status '%s' for job %s to channel %s", status, job_id, channel)


def handle_job_failure(job_id: str, job_data: dict, error_message: str) -> None:
    """Handle retries and notifications for a failed job."""
    r = get_redis_connection()
    retry_count = r.hincrby("hash:job_retries", job_id, 1)

    active_job_json = r.hget(ACTIVE_JOBS_HASH, job_id)
    active_job_state = {}
    if active_job_json:
        try:
            active_job_state = json.loads(active_job_json)
        except json.JSONDecodeError:
            active_job_state = {}

    if retry_count <= MAX_RETRY_ATTEMPTS:
        logger.info(
            "Retrying job %s (attempt %s/%s)",
            job_id,
            retry_count,
            MAX_RETRY_ATTEMPTS,
        )
        now = time.time()
        if "job_data" not in active_job_state:
            active_job_state["job_data"] = json.dumps(job_data)
        active_job_state.update(
            {
                "status": "queued",
                "last_error": error_message,
                "retry_count": retry_count,
                "start_time": now,
                "ticket_issued_at": now,
                "updated_at": now,
            }
        )
        r.hset(ACTIVE_JOBS_HASH, job_id, json.dumps(active_job_state))
        r.lpush(NEW_JOBS_QUEUE, json.dumps(job_data))
    else:
        logger.error(
            "Job %s failed permanently after %s attempts: %s",
            job_id,
            retry_count - 1,
            error_message,
        )
        sequence_hash = active_job_state.get("sequence_hash") or job_data.get("_sequence_hash")
        if sequence_hash:
            waiting_jobs = drain_pending_sequence(str(sequence_hash))
            for waiting_job_id in waiting_jobs:
                if waiting_job_id == job_id:
                    continue
                r.hdel(ACTIVE_JOBS_HASH, waiting_job_id)
                r.hdel("hash:job_retries", waiting_job_id)
                publish_job_status(
                    waiting_job_id,
                    "failed",
                    "Identical computation failed permanently.",
                )

        dead_letter_payload = {
            "job_id": job_id,
            "job_data": job_data,
            "final_error": error_message,
            "failed_at": time.time(),
        }
        r.lpush(DEAD_LETTER_QUEUE, json.dumps(dead_letter_payload))
        r.hdel(ACTIVE_JOBS_HASH, job_id)
        r.hdel("hash:job_retries", job_id)
        publish_job_status(job_id, "failed", "Job failed permanently after multiple retries.")


__all__ = ["handle_job_failure", "publish_job_status"]
