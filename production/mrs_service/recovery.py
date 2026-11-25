"""Startup recovery logic for the Management & Routing Service."""
from __future__ import annotations

import json
import time

from .runtime import (
    ACTIVE_JOBS_HASH,
    IN_PROGRESS_JOBS_LIST,
    NEW_JOBS_QUEUE,
    get_redis_connection,
    logger,
)


def recover_interrupted_jobs() -> None:
    """Move jobs from the in-progress list back to the new jobs queue."""
    r = get_redis_connection()
    interrupted_jobs = r.lrange(IN_PROGRESS_JOBS_LIST, 0, -1)
    if not interrupted_jobs:
        logger.info("No interrupted jobs found to recover.")
        return

    logger.warning("Found %s interrupted job ticket(s). Reconciling...", len(interrupted_jobs))
    for job_json in interrupted_jobs:
        try:
            job_data = json.loads(job_json)
        except json.JSONDecodeError:
            logger.error(
                "Could not decode interrupted job payload: %s. Dropping from in-progress list.",
                job_json,
            )
            r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)
            continue

        job_id = job_data.get("job_id")
        if not job_id:
            logger.error(
                "Interrupted job payload missing job_id: %s. Dropping from in-progress list.",
                job_data,
            )
            r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)
            continue

        active_job_json = r.hget(ACTIVE_JOBS_HASH, job_id)
        active_job_state = {}
        if active_job_json:
            try:
                active_job_state = json.loads(active_job_json)
            except json.JSONDecodeError:
                active_job_state = {}

        ticket_status = active_job_state.get("status")

        if ticket_status in {None, "queued", "requeued"}:
            logger.info(
                "Re-issuing ticket for job %s from recovery path (status=%s).",
                job_id,
                ticket_status,
            )
            now = time.time()
            if "job_data" not in active_job_state:
                active_job_state["job_data"] = json.dumps(job_data)
            active_job_state.update(
                {
                    "status": "queued",
                    "start_time": now,
                    "ticket_issued_at": now,
                    "recovered_at": now,
                    "updated_at": now,
                }
            )
            r.hset(ACTIVE_JOBS_HASH, job_id, json.dumps(active_job_state))
            r.lpush(NEW_JOBS_QUEUE, json.dumps(job_data))
        else:
            logger.info(
                "Skipping requeue for job %s; current ticket status is '%s'.",
                job_id,
                ticket_status,
            )

        r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)
    logger.info("Finished reconciling interrupted job tickets.")


__all__ = ["recover_interrupted_jobs"]
