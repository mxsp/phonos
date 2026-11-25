"""Background processing loops for the MRS.

SOTA Big Data & Pipeline Features:
- Sequence deduplication (skip redundant computations)
- Priority queue routing (SLA-aware dispatching)
- Smart batching (micro-batch small sequences)
- Real-time metrics collection
- Cache-first result resolution
"""
from __future__ import annotations

import os
import json
import threading
import time
import uuid
from typing import Dict, Any, Optional, List

import redis

from encryption_util import canonical_dumps, decrypt, encrypt, verify_signature

from .authorization import get_job_organization, is_valid_worker_queue, worker_authorized_for_job
from .key_cache import get_public_key_for_node
from .runtime import Config
from .runtime import (
    ACTIVE_JOBS_HASH,
    IN_PROGRESS_JOBS_LIST,
    JOB_RESULTS_QUEUE,
    NEW_JOBS_QUEUE,
    JOB_BATCH_QUEUE,
    logger,
    get_redis_connection,
    # Priority Queue System
    JobPriority,
    determine_job_priority,
    get_priority_queue_name,
    PRIORITY_SLA_SECONDS,
    # Sequence Caching
    compute_sequence_hash,
    check_sequence_cache,
    cache_sequence_result,
    register_pending_sequence,
    resolve_pending_sequence,
    drain_pending_sequence,
    has_active_sequence_computation,
    # Batching
    JobBatch,
    is_batchable_job,
    BATCH_SIZE_THRESHOLD,
    BATCH_WAIT_MS,
    # Metrics
    JobMetrics,
    record_job_metrics,
    Config,
)
from .status import handle_job_failure, publish_job_status


# =============================================================================
# BUNCH STATUS UPDATES
# =============================================================================

def _update_bunch_for_job(job_id: str, status: str, error_message: str = None) -> None:
    """Update bunch status when a job in the bunch completes or fails.

    This is called from the result processor when a job finishes.
    It updates the job_bunch_items record and recalculates bunch status.
    """
    try:
        from database import get_db_connection

        conn = get_db_connection()
        cursor = conn.cursor()

        # Find the bunch item for this job
        cursor.execute("""
            SELECT jbi.id, jbi.bunch_id
            FROM job_bunch_items jbi
            JOIN jobs j ON jbi.job_id = j.id
            WHERE j.job_id = %s
        """, (job_id,))

        row = cursor.fetchone()
        if not row:
            # Job is not part of a bunch
            conn.close()
            return

        item_id = row['id']
        bunch_db_id = row['bunch_id']

        # Update the item status
        cursor.execute("""
            UPDATE job_bunch_items
            SET status = %s, error_message = %s
            WHERE id = %s
        """, (status, error_message, item_id))

        # Count job statuses to determine bunch status
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed,
                COUNT(CASE WHEN status IN ('pending', 'submitted', 'running') THEN 1 END) as in_progress
            FROM job_bunch_items
            WHERE bunch_id = %s
        """, (bunch_db_id,))

        counts = cursor.fetchone()
        total = counts['total']
        completed = counts['completed']
        failed = counts['failed']
        in_progress = counts['in_progress']

        # Determine new bunch status
        if in_progress > 0:
            new_status = 'running'
            completed_at_clause = ""
        elif completed == total:
            new_status = 'completed'
            completed_at_clause = ", completed_at = CURRENT_TIMESTAMP"
        elif failed == total:
            new_status = 'failed'
            completed_at_clause = ", completed_at = CURRENT_TIMESTAMP"
        else:
            new_status = 'mixed'
            completed_at_clause = ", completed_at = CURRENT_TIMESTAMP"

        # Update bunch
        cursor.execute(f"""
            UPDATE job_bunches
            SET status = %s, completed_jobs = %s, failed_jobs = %s, updated_at = CURRENT_TIMESTAMP{completed_at_clause}
            WHERE id = %s
        """, (new_status, completed, failed, bunch_db_id))

        conn.commit()
        conn.close()

        logger.debug("Updated bunch %d status to %s (completed=%d, failed=%d)",
                    bunch_db_id, new_status, completed, failed)

    except Exception as exc:
        logger.warning("Failed to update bunch status for job %s: %s", job_id, exc)


# =============================================================================
# JOB PREPROCESSING - Deduplication & Cache Check
# =============================================================================

def _check_cache_and_dedup(job_data: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    """Check if job result is cached or if another job is computing the same sequence.

    Returns cached result if available, None otherwise.
    If another job is computing the same sequence, registers this job as waiting.
    """
    # Extract sequence and params for hashing
    params = job_data.get("params", {})
    sequence = params.get("sequence", "")
    model_type = job_data.get("model")

    if not sequence or not model_type:
        return None

    # Compute content hash
    sequence_hash = compute_sequence_hash(sequence, model_type, params)
    job_data["_sequence_hash"] = sequence_hash  # Store for later use

    # Check cache first
    cached_result = check_sequence_cache(sequence_hash)
    if cached_result:
        has_embedded_main = bool(
            cached_result.get("output_file_content") and cached_result.get("output_filename")
        )
        has_embedded_artifacts = any(
            isinstance(artifact, dict) and artifact.get("content_b64") and artifact.get("filename")
            for artifact in cached_result.get("artifacts", [])
        )

        if has_embedded_main or has_embedded_artifacts:
            logger.info("Cache HIT for job %s (hash: %s)", job_id, sequence_hash[:8])
            return cached_result

        logger.warning(
            "Ignoring stale cache for job %s (hash: %s): missing embedded output payload.",
            job_id,
            sequence_hash[:8],
        )

    # Check if another job is computing this sequence
    waiting_jobs = register_pending_sequence(sequence_hash, job_id)

    if len(waiting_jobs) > 1:
        if not has_active_sequence_computation(sequence_hash, exclude_job_id=job_id):
            # Stale dedup state: no leader job exists anymore.
            # Reset pending list so current request can proceed as the new leader.
            drain_pending_sequence(sequence_hash)
            waiting_jobs = register_pending_sequence(sequence_hash, job_id)

        if len(waiting_jobs) > 1:
            # Another job is already computing this - we're now waiting for it
            logger.info("Job %s deferred - waiting for job computing hash %s", job_id, sequence_hash[:8])
            return {"_deferred": True, "waiting_for_hash": sequence_hash}

    # This is the first job for this sequence - proceed with computation
    logger.info("Job %s is first for hash %s - will compute", job_id, sequence_hash[:8])
    return None


def _resolve_cached_result(job_id: str, job_data: Dict[str, Any], cached_result: Dict[str, Any]) -> None:
    """Resolve a job with a cached result."""
    # Record metrics for cache hit
    sequence = job_data.get("params", {}).get("sequence", "")
    metrics = JobMetrics(
        job_id=job_id,
        model_type=job_data.get("model", "unknown"),
        sequence_length=len(sequence),
        queue_time_ms=0,
        processing_time_ms=0,
        total_time_ms=0,
        priority=determine_job_priority(job_data),
        cached=True,
        batched=False,
        retried=False
    )
    record_job_metrics(metrics)

    # Forward full cached payload so listeners can persist outputs exactly as
    # they would for a live worker completion.
    cached_payload = {
        "cached": True,
        "output_path": cached_result.get("output_path"),
        "output_file_content": cached_result.get("output_file_content"),
        "output_filename": cached_result.get("output_filename"),
        "artifacts": cached_result.get("artifacts", []),
        "execution_time": cached_result.get("execution_time", 0),
        "metrics": cached_result.get("metrics"),
    }

    if not cached_payload["output_filename"] and cached_payload["output_path"]:
        cached_payload["output_filename"] = os.path.basename(cached_payload["output_path"])

    # Publish completion
    publish_job_status(
        job_id,
        "completed",
        "Job resolved from cache.",
        data=cached_payload,
    )


# =============================================================================
# PRIORITY-AWARE JOB PROCESSOR
# =============================================================================

def _retired_reason(model_type: str) -> Optional[str]:
    """Reason text for a model switched off in Config (e.g. NVIDIA retired its endpoint), else None."""
    model = Config.get_model_definition(model_type) or {}
    if model.get("is_active") is False and model.get("offline_reason"):
        return model["offline_reason"]
    return None


def process_new_jobs() -> None:
    """Continuously process new jobs with priority routing and deduplication.

    SOTA Features:
    1. Cache check - skip computation if result already exists
    2. Deduplication - wait for identical sequences being computed
    3. Priority routing - route to priority-specific queues
    4. Metrics collection - track all job activity
    """
    logger.info("New job processor started (SOTA: priority routing, dedup, caching).")
    r = get_redis_connection()

    while True:
        job_json = None
        try:
            job_json = r.brpoplpush(NEW_JOBS_QUEUE, IN_PROGRESS_JOBS_LIST, timeout=1)
            if not job_json:
                continue  # Timeout expired, no job available

            job_data = json.loads(job_json)
            job_id = job_data.get("job_id")
            model_type = job_data.get("model")

            if not all([job_id, model_type]):
                logger.error("Invalid job received from queue: %s", job_data)
                r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)
                continue

            # Add submission timestamp for SLA tracking
            submitted_at = job_data.get("submitted_at", time.time())
            job_data["submitted_at"] = submitted_at

            logger.info("Processing new job %s for model '%s'.", job_id, model_type)

            retired = _retired_reason(model_type)
            if retired:
                logger.warning("Job %s rejected: model '%s' is retired.", job_id, model_type)
                publish_job_status(job_id, "failed", retired)
                r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)
                continue

            # === SOTA: Cache & Deduplication Check ===
            cache_result = _check_cache_and_dedup(job_data, job_id)

            if cache_result:
                if cache_result.get("_deferred"):
                    # Job is waiting for another computation - mark as pending
                    deferred_payload = {
                        "job_data": json.dumps(job_data),
                        "status": "pending_dedup",
                        "waiting_for_hash": cache_result.get("waiting_for_hash"),
                        "start_time": time.time(),
                        "manager_id": threading.current_thread().name,
                    }
                    r.hset(ACTIVE_JOBS_HASH, job_id, json.dumps(deferred_payload))
                    r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)
                    publish_job_status(job_id, "processing", "Job queued - identical computation in progress.")
                    continue
                else:
                    # Cache hit - resolve immediately
                    _resolve_cached_result(job_id, job_data, cache_result)
                    r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)
                    continue

            # === SOTA: Determine Priority ===
            priority = determine_job_priority(job_data)
            job_data["_priority"] = int(priority)

            issued_at = time.time()
            active_job_payload = {
                "job_data": json.dumps(job_data),
                "status": "queued",
                "start_time": issued_at,
                "manager_id": threading.current_thread().name,
                "ticket_issued_at": issued_at,
                "updated_at": issued_at,
                "priority": int(priority),
                "sequence_hash": job_data.get("_sequence_hash"),
            }
            r.hset(ACTIVE_JOBS_HASH, job_id, json.dumps(active_job_payload))

            # === Determine Target Queue ===
            target_queue = job_data.get("target_queue")
            if target_queue and is_valid_worker_queue(target_queue):
                model_queue = target_queue
            else:
                # Use priority queue for better SLA management
                # model_queue = get_priority_queue_name(priority, model_type)
                # Fall back to standard queue if priority queues not enabled
                model_queue = f"queue:job:{model_type}"

            # === Encrypt & Dispatch ===
            encrypted_job = encrypt(json.dumps(job_data).encode("utf-8"))
            if encrypted_job:
                r.lpush(model_queue, encrypted_job)
                logger.info("Job %s enqueued to '%s' (priority: %s).", job_id, model_queue, priority.name)

                dispatched_at = time.time()
                queue_time_ms = (dispatched_at - submitted_at) * 1000

                active_job_payload["status"] = "dispatched"
                active_job_payload["dispatched_at"] = dispatched_at
                active_job_payload["queue_time_ms"] = queue_time_ms
                active_job_payload["organization_id"] = job_data.get("organization_id")
                active_job_payload["updated_at"] = dispatched_at
                r.hset(ACTIVE_JOBS_HASH, job_id, json.dumps(active_job_payload))

                publish_job_status(job_id, "processing", f"Job dispatched (priority: {priority.name}).")
            else:
                logger.error("Failed to encrypt job %s. Cannot dispatch.", job_id)
                handle_job_failure(job_id, job_data, "Encryption failed")

            r.lrem(IN_PROGRESS_JOBS_LIST, 1, job_json)

        except redis.exceptions.RedisError as exc:
            logger.error("Redis error in new job processor: %s", exc, exc_info=True)
            time.sleep(5)
        except Exception as exc:  # pragma: no cover - defensive coding
            logger.error("Unexpected error in new job processor: %s", exc, exc_info=True)
            time.sleep(5)


def process_job_results() -> None:
    """Continuously process and verify completed job results from workers.

    SOTA Features:
    1. Deduplication resolution - notify all jobs waiting for same computation
    2. Result caching - cache successful results for future requests
    3. Metrics collection - track processing time, cache stats
    """
    logger.info("Job result processor started (SOTA: dedup resolution, caching, metrics).")
    r = get_redis_connection(binary_mode=True)
    r_text = get_redis_connection()  # For text operations

    while True:
        try:
            result = r.brpop(JOB_RESULTS_QUEUE, timeout=1)
            if not result:
                continue  # Timeout expired, no result available
            _, encrypted_result_bytes = result

            decrypted_json = decrypt(encrypted_result_bytes)
            if not decrypted_json:
                logger.error("Failed to decrypt result from worker. Discarding.")
                continue

            signed_result = json.loads(decrypted_json.decode("utf-8"))
            result_payload = signed_result.get("payload")
            signature = signed_result.get("signature")

            if not all([result_payload, signature]):
                logger.error("Invalid signed result format: %s", signed_result)
                continue

            worker_id = result_payload.get("worker_id")
            public_key = get_public_key_for_node(worker_id)

            if not public_key:
                logger.warning(
                    "Received result from unknown or unauthorized worker '%s'. Discarding.",
                    worker_id,
                )
                continue

            # Validate timestamp to prevent replay attacks
            timestamp = result_payload.get("timestamp")
            if not timestamp:
                logger.error("Result from worker '%s' missing timestamp. Discarding.", worker_id)
                continue

            current_time = time.time()
            message_age = current_time - timestamp
            MAX_MESSAGE_AGE = 300  # 5 minutes

            if message_age < 0:
                logger.error("Result from worker '%s' has future timestamp. Possible clock skew. Discarding.", worker_id)
                continue

            if message_age > MAX_MESSAGE_AGE:
                logger.error("Result from worker '%s' is too old (%d seconds). Possible replay attack. Discarding.", worker_id, int(message_age))
                continue

            # Verify signature
            payload_bytes = canonical_dumps(result_payload).encode("utf-8")
            if not verify_signature(public_key, payload_bytes, signature):
                logger.error("Invalid signature for result from worker '%s'. Discarding.", worker_id)
                continue

            # Check for nonce reuse (replay detection)
            nonce = result_payload.get("nonce")
            if nonce:
                nonce_key = f"nonce:{worker_id}:{nonce}"
                if r.exists(nonce_key):
                    logger.error("Nonce reuse detected from worker '%s'. Possible replay attack. Discarding.", worker_id)
                    continue
                # Store nonce with expiry to prevent reuse
                r.setex(nonce_key, MAX_MESSAGE_AGE + 60, "1")

            job_id = result_payload.get("job_id")
            if not job_id:
                logger.error("Result from worker '%s' is missing a job_id. Discarding.", worker_id)
                continue

            active_job_state_json = r.hget(ACTIVE_JOBS_HASH, job_id)
            active_job_state = {}
            job_data_for_failure = {}
            if active_job_state_json:
                try:
                    if isinstance(active_job_state_json, bytes):
                        active_job_state_json = active_job_state_json.decode('utf-8')
                    active_job_state = json.loads(active_job_state_json)
                    job_data_for_failure = json.loads(active_job_state.get("job_data", "{}"))
                except (json.JSONDecodeError, TypeError):
                    job_data_for_failure = {}

            organization_id = result_payload.get("organization_id")
            if organization_id is None and isinstance(active_job_state, dict):
                organization_id = active_job_state.get("organization_id")
            if organization_id is None:
                organization_id = get_job_organization(job_id)

            if not worker_authorized_for_job(worker_id, organization_id):
                logger.warning(
                    "Discarding result for job %s from worker %s - unauthorized for organization %s.",
                    job_id,
                    worker_id,
                    organization_id,
                )
                handle_job_failure(job_id, job_data_for_failure, "Unauthorized worker result")
                continue

            logger.info("Processing verified result for job %s from worker %s...", job_id, worker_id)

            # === SOTA: Calculate Metrics ===
            dispatched_at = active_job_state.get("dispatched_at", active_job_state.get("start_time", current_time))
            submitted_at = active_job_state.get("ticket_issued_at", dispatched_at)
            queue_time_ms = (dispatched_at - submitted_at) * 1000
            processing_time_ms = (current_time - dispatched_at) * 1000
            total_time_ms = (current_time - submitted_at) * 1000

            sequence_hash = active_job_state.get("sequence_hash")
            job_data = json.loads(active_job_state.get("job_data", "{}"))

            r.hdel(ACTIVE_JOBS_HASH, job_id)
            r.hdel("hash:job_retries", job_id)

            if result_payload.get("status") == "success":
                # === SOTA: Cache Result & Resolve Deduplicated Jobs ===
                if sequence_hash:
                    # Prepare cache data — include file content so deduplicated
                    # jobs have everything the listener needs to save outputs.
                    cache_data = {
                        "output_path": result_payload.get("output_path"),
                        "output_file_content": result_payload.get("output_file_content"),
                        "output_filename": result_payload.get("output_filename"),
                        "artifacts": result_payload.get("artifacts", []),
                        "execution_time": result_payload.get("execution_time", 0),
                        "metrics": result_payload.get("metrics"),
                        "model": job_data.get("model"),
                        "computed_at": current_time,
                        "processing_time_ms": processing_time_ms,
                    }

                    # Resolve all jobs waiting for this computation
                    waiting_jobs = resolve_pending_sequence(sequence_hash, cache_data)

                    if len(waiting_jobs) > 1:
                        logger.info("Resolving %d deduplicated jobs for hash %s", len(waiting_jobs) - 1, sequence_hash[:8])

                        # Notify all waiting jobs (except the one that computed)
                        for waiting_job_id in waiting_jobs:
                            if waiting_job_id != job_id:
                                # Clear waiting job from active hash
                                r_text.hdel(ACTIVE_JOBS_HASH, waiting_job_id)

                                # Record metrics for deduplicated job
                                dedup_metrics = JobMetrics(
                                    job_id=waiting_job_id,
                                    model_type=job_data.get("model", "unknown"),
                                    sequence_length=len(job_data.get("params", {}).get("sequence", "")),
                                    queue_time_ms=queue_time_ms,
                                    processing_time_ms=0,  # Didn't actually compute
                                    total_time_ms=total_time_ms,
                                    priority=JobPriority(active_job_state.get("priority", 2)),
                                    cached=True,  # Result from another job
                                    batched=False,
                                    retried=False
                                )
                                record_job_metrics(dedup_metrics)

                                # Publish completion for waiting job
                                publish_job_status(
                                    waiting_job_id,
                                    "completed",
                                    "Job resolved via deduplication.",
                                    data={"cached": True, "deduplicated_from": job_id, **cache_data}
                                )

                # Record metrics for the job that actually computed
                metrics = JobMetrics(
                    job_id=job_id,
                    model_type=job_data.get("model", "unknown"),
                    sequence_length=len(job_data.get("params", {}).get("sequence", "")),
                    queue_time_ms=queue_time_ms,
                    processing_time_ms=processing_time_ms,
                    total_time_ms=total_time_ms,
                    priority=JobPriority(active_job_state.get("priority", 2)),
                    cached=False,
                    batched=active_job_state.get("batched", False),
                    retried=active_job_state.get("retry_count", 0) > 0
                )
                record_job_metrics(metrics)

                publish_job_status(
                    job_id,
                    "completed",
                    "Job finished successfully.",
                    data=result_payload,
                )

                logger.info("Job %s completed in %.1fs (queue: %.1fs, compute: %.1fs)",
                           job_id, total_time_ms/1000, queue_time_ms/1000, processing_time_ms/1000)

                # === Update bunch status if this job belongs to a bunch ===
                _update_bunch_for_job(job_id, "completed")

            else:
                error_message = result_payload.get("message", "Unknown error from worker.")
                logger.error(
                    "Job %s failed on worker %s: %s",
                    job_id,
                    worker_id,
                    error_message,
                )
                publish_job_status(job_id, "failed", error_message, data=result_payload)

                # === Update bunch status if this job belongs to a bunch ===
                _update_bunch_for_job(job_id, "failed", error_message)

        except redis.exceptions.RedisError as exc:
            logger.error("Redis error in result processor: %s", exc, exc_info=True)
            time.sleep(5)
        except Exception as exc:  # pragma: no cover - defensive coding
            logger.error("Unexpected error in result processor: %s", exc, exc_info=True)


# =============================================================================
# SLA MONITOR - Track queue depths and alert on breaches
# =============================================================================

def monitor_sla_compliance() -> None:
    """Monitor priority queues for SLA breaches.

    Runs periodically to check if any jobs are waiting beyond their SLA limit.
    """
    logger.info("SLA compliance monitor started.")
    r = get_redis_connection()

    while True:
        try:
            from .runtime import get_priority_queue_stats

            # Check all models
            for model in Config.SUPPORTED_MODELS:
                model_id = model.get("id")
                stats = get_priority_queue_stats(model_id)

                for stat in stats:
                    if stat.sla_breaches > 0:
                        logger.warning(
                            "SLA BREACH: Model %s, Priority %s, Queue depth: %d, Oldest job: %.1fs (limit: %ds)",
                            stat.model_type,
                            stat.tier.name,
                            stat.queue_depth,
                            stat.oldest_job_age_seconds,
                            PRIORITY_SLA_SECONDS.get(stat.tier, 3600)
                        )

                        # Could trigger alerts here (email, Slack, PagerDuty, etc.)

            time.sleep(30)  # Check every 30 seconds

        except Exception as exc:
            logger.error("Error in SLA monitor: %s", exc, exc_info=True)
            time.sleep(30)


__all__ = ["process_job_results", "process_new_jobs", "monitor_sla_compliance"]
