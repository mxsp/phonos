"""Shared runtime configuration for the Management & Routing Service (MRS).

SOTA Big Data & Pipeline Features:
- Priority Queue System (Tier 0-3 with SLA guarantees)
- Sequence Deduplication (content-addressable cache)
- Job Batching (smart micro-batching for small sequences)
- Pipeline Analytics (real-time metrics collection)
- Speculative Execution Tracking
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Final, Optional, Dict, List
from enum import IntEnum
from collections import defaultdict

import redis

from config import Config

# --- Configuration ---
JOB_TIMEOUT: Final[int] = 3600  # 60 minutes
MAX_RETRY_ATTEMPTS: Final[int] = 3
WORKER_TIMEOUT: Final[int] = Config.WORKER_HEARTBEAT_TIMEOUT

# --- Priority Queue Configuration ---
class JobPriority(IntEnum):
    """Job priority tiers with SLA guarantees."""
    CRITICAL = 0    # API SLA jobs - max 30s wait
    PREMIUM = 1     # Premium users - max 2min wait
    STANDARD = 2    # Standard users - best effort
    BACKGROUND = 3  # Flow steps, batch jobs - idle capacity

PRIORITY_SLA_SECONDS: Final[Dict[int, int]] = {
    JobPriority.CRITICAL: 30,
    JobPriority.PREMIUM: 120,
    JobPriority.STANDARD: 600,
    JobPriority.BACKGROUND: 3600,
}

# --- Redis Keys ---
NEW_JOBS_QUEUE: Final[str] = "queue:new_jobs"
FLOW_EXECUTION_QUEUE: Final[str] = "queue:flow_executions"
IN_PROGRESS_JOBS_LIST: Final[str] = "list:in_progress_jobs"
DEAD_LETTER_QUEUE: Final[str] = "queue:dead_letter"
JOB_RESULTS_QUEUE: Final[str] = "queue:job_results"
ACTIVE_JOBS_HASH: Final[str] = "hash:active_jobs"
WORKER_HEALTH_HASH: Final[str] = "hash:worker_health"
WORKER_HEARTBEAT_CHANNEL: Final[str] = "pubsub:worker_heartbeats"
NODE_REGISTRATION_CHANNEL: Final[str] = "pubsub:node_registration"
NODE_REGISTRATION_CONFIRMATION_CHANNEL_PREFIX: Final[str] = "pubsub:node_registration_confirmation:"
JOB_STATUS_CHANNEL_PREFIX: Final[str] = "pubsub:job_status:"

# --- Priority Queue Keys ---
PRIORITY_QUEUE_PREFIX: Final[str] = "queue:priority"  # queue:priority:{tier}:{model}
JOB_BATCH_QUEUE: Final[str] = "queue:job_batches"

# --- Caching & Deduplication Keys ---
SEQUENCE_CACHE_HASH: Final[str] = "hash:sequence_cache"  # hash of sequence -> job_id with result
SEQUENCE_PENDING_HASH: Final[str] = "hash:sequence_pending"  # hash -> list of waiting job_ids
RESULT_CACHE_PREFIX: Final[str] = "cache:result:"  # cache:result:{hash} -> cached output path
CACHE_TTL_SECONDS: Final[int] = 86400  # 24 hours for cached results

# --- Analytics Keys ---
METRICS_STREAM: Final[str] = "stream:metrics"  # Redis stream for real-time metrics
METRICS_HASH: Final[str] = "hash:metrics"  # Aggregated metrics
HOURLY_STATS_PREFIX: Final[str] = "stats:hourly:"  # stats:hourly:{YYYYMMDDHH}
MODEL_LATENCY_HASH: Final[str] = "hash:model_latency"  # Model -> avg latency

# --- Batching Configuration ---
BATCH_SIZE_THRESHOLD: Final[int] = 8  # Max jobs per batch
BATCH_SEQUENCE_LENGTH_THRESHOLD: Final[int] = 500  # Sequences <= this can be batched
BATCH_FILE_SIZE_THRESHOLD: Final[int] = 20  # Max files per batch
BATCH_WAIT_MS: Final[int] = 500  # Max wait time before dispatching partial batch

QUEUE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^queue:job:[a-z0-9-]+(?::org:[0-9]+)?$")


# =============================================================================
# SEQUENCE HASHING & DEDUPLICATION
# =============================================================================

def compute_sequence_hash(sequence: str, model_type: str, params: Optional[Dict] = None) -> str:
    """Compute a deterministic hash for a sequence + model + params combination.

    This enables content-addressable caching - identical inputs always produce
    the same hash, allowing us to skip redundant computation.
    """
    # Normalize sequence (uppercase, strip whitespace)
    normalized_seq = sequence.upper().strip().replace(" ", "").replace("\n", "")

    # Create canonical representation
    hash_input = {
        "sequence": normalized_seq,
        "model": model_type,
        "params": params or {}
    }

    # Sort keys for deterministic output
    canonical = json.dumps(hash_input, sort_keys=True, separators=(',', ':'))

    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]


def check_sequence_cache(sequence_hash: str) -> Optional[Dict[str, Any]]:
    """Check if a sequence result is already cached.

    Returns cached result info if available, None otherwise.
    """
    r = get_redis_connection()

    # Check for completed cached result
    cache_key = f"{RESULT_CACHE_PREFIX}{sequence_hash}"
    cached = r.get(cache_key)

    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            return None

    return None


def cache_sequence_result(sequence_hash: str, result_data: Dict[str, Any], ttl: int = CACHE_TTL_SECONDS) -> None:
    """Cache a sequence computation result."""
    r = get_redis_connection()
    cache_key = f"{RESULT_CACHE_PREFIX}{sequence_hash}"
    r.setex(cache_key, ttl, json.dumps(result_data))
    logger.info("Cached result for sequence hash %s (TTL: %ds)", sequence_hash[:8], ttl)


def register_pending_sequence(sequence_hash: str, job_id: str) -> List[str]:
    """Register a job as waiting for a sequence computation.

    Returns list of all job_ids waiting for this sequence (including this one).
    If list has only this job_id, this is the first request and should be computed.
    """
    r = get_redis_connection()

    # Use a Redis list to track all jobs waiting for this hash
    pending_key = f"{SEQUENCE_PENDING_HASH}:{sequence_hash}"

    # Atomic push and get all
    pipe = r.pipeline()
    pipe.rpush(pending_key, job_id)
    pipe.lrange(pending_key, 0, -1)
    pipe.expire(pending_key, 3600)  # 1 hour TTL for pending list
    results = pipe.execute()

    waiting_jobs = results[1]
    return [j.decode('utf-8') if isinstance(j, bytes) else j for j in waiting_jobs]


def resolve_pending_sequence(sequence_hash: str, result_data: Dict[str, Any]) -> List[str]:
    """Resolve all jobs waiting for a sequence computation.

    Returns list of job_ids that were waiting and should now be notified.
    """
    r = get_redis_connection()
    pending_key = f"{SEQUENCE_PENDING_HASH}:{sequence_hash}"

    # Get all waiting jobs and delete the key atomically
    pipe = r.pipeline()
    pipe.lrange(pending_key, 0, -1)
    pipe.delete(pending_key)
    results = pipe.execute()

    waiting_jobs = results[0]

    # Cache the result for future requests
    cache_sequence_result(sequence_hash, result_data)

    return [j.decode('utf-8') if isinstance(j, bytes) else j for j in waiting_jobs]


def drain_pending_sequence(sequence_hash: str) -> List[str]:
    """Return and clear all pending jobs for ``sequence_hash`` without caching."""
    r = get_redis_connection()
    pending_key = f"{SEQUENCE_PENDING_HASH}:{sequence_hash}"

    pipe = r.pipeline()
    pipe.lrange(pending_key, 0, -1)
    pipe.delete(pending_key)
    results = pipe.execute()

    waiting_jobs = results[0]
    return [j.decode('utf-8') if isinstance(j, bytes) else j for j in waiting_jobs]


def has_active_sequence_computation(sequence_hash: str, *, exclude_job_id: Optional[str] = None) -> bool:
    """Return True when at least one non-deferred active job computes ``sequence_hash``."""
    r = get_redis_connection()
    try:
        active_jobs = r.hgetall(ACTIVE_JOBS_HASH)
    except Exception:
        return False

    for active_job_id, active_state_json in active_jobs.items():
        if isinstance(active_job_id, bytes):
            active_job_id = active_job_id.decode("utf-8")
        if exclude_job_id and active_job_id == exclude_job_id:
            continue

        try:
            if isinstance(active_state_json, bytes):
                active_state_json = active_state_json.decode("utf-8")
            active_state = json.loads(active_state_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

        if active_state.get("sequence_hash") != sequence_hash:
            continue

        status = active_state.get("status")
        if status != "pending_dedup":
            return True

    return False


# =============================================================================
# PRIORITY QUEUE SYSTEM
# =============================================================================

def get_priority_queue_name(priority: JobPriority, model_type: str) -> str:
    """Get the Redis key for a priority queue."""
    return f"{PRIORITY_QUEUE_PREFIX}:{priority}:{model_type}"


def determine_job_priority(job_data: Dict[str, Any]) -> JobPriority:
    """Determine the priority tier for a job based on context.

    Priority factors:
    - Explicit priority field
    - User plan (premium users get higher priority)
    - Job source (API jobs get higher priority than UI)
    - Flow context (background flow steps get lower priority)
    """
    # Check explicit priority
    explicit_priority = job_data.get("priority")
    if explicit_priority is not None:
        try:
            return JobPriority(int(explicit_priority))
        except (ValueError, TypeError):
            pass

    # Check if this is a flow step (background priority)
    if job_data.get("flow_execution_id") or job_data.get("is_flow_step"):
        return JobPriority.BACKGROUND

    # Check user plan
    user_plan = job_data.get("user_plan", "standard")
    if user_plan in ("premium", "enterprise", "team"):
        return JobPriority.PREMIUM

    # Check if API request (higher priority for external integrations)
    if job_data.get("api_request") or job_data.get("external_api"):
        return JobPriority.CRITICAL

    return JobPriority.STANDARD


@dataclass
class PriorityQueueStats:
    """Statistics for priority queue monitoring."""
    tier: JobPriority
    model_type: str
    queue_depth: int
    oldest_job_age_seconds: float
    sla_breaches: int = 0


def get_priority_queue_stats(model_type: str) -> List[PriorityQueueStats]:
    """Get statistics for all priority queues for a model."""
    r = get_redis_connection()
    stats = []

    for priority in JobPriority:
        queue_name = get_priority_queue_name(priority, model_type)
        
        try:
            queue_depth = r.llen(queue_name)
        except (redis.exceptions.ResponseError, UnicodeDecodeError):
            queue_depth = 0

        oldest_age = 0.0
        if queue_depth > 0:
            try:
                oldest_job_json = r.lindex(queue_name, -1)
                if oldest_job_json:
                    try:
                        oldest_job = json.loads(oldest_job_json)
                        submitted_at = oldest_job.get("submitted_at", time.time())
                        oldest_age = time.time() - submitted_at
                    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                        pass
            except (redis.exceptions.ResponseError, UnicodeDecodeError):
                pass

        sla_limit = PRIORITY_SLA_SECONDS.get(priority, 3600)
        sla_breaches = 1 if oldest_age > sla_limit else 0

        stats.append(PriorityQueueStats(
            tier=priority,
            model_type=model_type,
            queue_depth=queue_depth,
            oldest_job_age_seconds=oldest_age,
            sla_breaches=sla_breaches
        ))

    return stats


# =============================================================================
# JOB BATCHING
# =============================================================================

@dataclass
class JobBatch:
    """A batch of jobs to be processed together."""
    batch_id: str
    model_type: str
    jobs: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    total_sequence_length: int = 0

    def can_add_job(self, job: Dict[str, Any]) -> bool:
        """Check if a job can be added to this batch."""
        if len(self.jobs) >= BATCH_SIZE_THRESHOLD:
            return False

        seq_length = len(job.get("params", {}).get("sequence", ""))
        if seq_length > BATCH_SEQUENCE_LENGTH_THRESHOLD:
            return False

        # Check file limit - treat each job as one file unit
        if len(self.jobs) >= BATCH_FILE_SIZE_THRESHOLD:
            return False

        # Check if batch has been waiting too long
        if (time.time() - self.created_at) * 1000 > BATCH_WAIT_MS:
            return False

        return True

    def add_job(self, job: Dict[str, Any]) -> bool:
        """Add a job to the batch. Returns True if successful."""
        if not self.can_add_job(job):
            return False

        self.jobs.append(job)
        self.total_sequence_length += len(job.get("params", {}).get("sequence", ""))
        return True

    def is_ready_to_dispatch(self) -> bool:
        """Check if batch should be dispatched."""
        if len(self.jobs) >= BATCH_SIZE_THRESHOLD:
            return True
        if (time.time() - self.created_at) * 1000 > BATCH_WAIT_MS:
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize batch for queue."""
        return {
            "batch_id": self.batch_id,
            "model_type": self.model_type,
            "jobs": self.jobs,
            "created_at": self.created_at,
            "total_sequence_length": self.total_sequence_length,
            "job_count": len(self.jobs)
        }


def is_batchable_job(job: Dict[str, Any]) -> bool:
    """Determine if a job can be batched with others."""
    # Check sequence length
    seq_length = len(job.get("params", {}).get("sequence", ""))
    if seq_length > BATCH_SEQUENCE_LENGTH_THRESHOLD:
        return False

    # Don't batch high-priority jobs
    priority = determine_job_priority(job)
    if priority == JobPriority.CRITICAL:
        return False

    # Don't batch jobs with special requirements
    if job.get("no_batch") or job.get("dedicated_gpu"):
        return False

    return True


# =============================================================================
# PIPELINE ANALYTICS & METRICS
# =============================================================================

@dataclass
class JobMetrics:
    """Metrics for a single job execution."""
    job_id: str
    model_type: str
    sequence_length: int
    queue_time_ms: float
    processing_time_ms: float
    total_time_ms: float
    priority: JobPriority
    cached: bool = False
    batched: bool = False
    retried: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "model_type": self.model_type,
            "sequence_length": self.sequence_length,
            "queue_time_ms": self.queue_time_ms,
            "processing_time_ms": self.processing_time_ms,
            "total_time_ms": self.total_time_ms,
            "priority": int(self.priority),
            "cached": self.cached,
            "batched": self.batched,
            "retried": self.retried,
            "timestamp": time.time()
        }


def record_job_metrics(metrics: JobMetrics) -> None:
    """Record job metrics to Redis stream for real-time analytics."""
    r = get_redis_connection()

    try:
        # Add to metrics stream (capped at 10000 entries)
        r.xadd(METRICS_STREAM, metrics.to_dict(), maxlen=10000)

        # Update aggregated metrics
        hour_key = time.strftime("%Y%m%d%H")
        stats_key = f"{HOURLY_STATS_PREFIX}{hour_key}"

        pipe = r.pipeline()
        pipe.hincrby(stats_key, f"{metrics.model_type}:count", 1)
        pipe.hincrbyfloat(stats_key, f"{metrics.model_type}:total_time", metrics.total_time_ms)
        pipe.hincrby(stats_key, f"{metrics.model_type}:cached", 1 if metrics.cached else 0)
        pipe.hincrby(stats_key, f"{metrics.model_type}:batched", 1 if metrics.batched else 0)
        pipe.expire(stats_key, 86400 * 7)  # Keep 7 days of hourly stats
        pipe.execute()

        # Update model latency tracking (exponential moving average)
        _update_model_latency(r, metrics.model_type, metrics.processing_time_ms)

    except redis.exceptions.RedisError as e:
        logger.warning("Failed to record job metrics: %s", e)


def _update_model_latency(r: redis.Redis, model_type: str, latency_ms: float, alpha: float = 0.1) -> None:
    """Update exponential moving average of model latency."""
    current_avg = r.hget(MODEL_LATENCY_HASH, model_type)

    if current_avg:
        try:
            current = float(current_avg)
            new_avg = alpha * latency_ms + (1 - alpha) * current
        except (ValueError, TypeError):
            new_avg = latency_ms
    else:
        new_avg = latency_ms

    r.hset(MODEL_LATENCY_HASH, model_type, new_avg)


def get_model_latency_estimate(model_type: str) -> float:
    """Get estimated latency for a model based on historical data."""
    r = get_redis_connection()
    latency = r.hget(MODEL_LATENCY_HASH, model_type)

    if latency:
        try:
            return float(latency)
        except (ValueError, TypeError):
            pass

    # Default estimates by model type
    defaults = {
        "type-0": 30000,   # ESMFold ~30s
        "type-2": 120000,  # AlphaFold2 ~2min
        "type-3": 60000,   # DiffDock ~1min
        "type-9": 90000,   # Boltz2 ~1.5min
        "evo2": 45000,     # Evo2 ~45s
    }

    return defaults.get(model_type, 60000)


def get_pipeline_stats() -> Dict[str, Any]:
    """Get comprehensive pipeline statistics."""
    r = get_redis_connection()

    # Get current queue depths
    queue_depths = {}
    for model in Config.SUPPORTED_MODELS:
        model_id = model.get("id")
        total_depth = 0
        for priority in JobPriority:
            queue_name = get_priority_queue_name(priority, model_id)
            total_depth += r.llen(queue_name)
        queue_depths[model_id] = total_depth

    # Get active jobs count
    active_jobs = r.hlen(ACTIVE_JOBS_HASH)

    # Get worker count
    worker_count = r.hlen(WORKER_HEALTH_HASH)

    # Get cache stats
    cache_hits = 0
    cache_misses = 0
    hour_key = time.strftime("%Y%m%d%H")
    stats_key = f"{HOURLY_STATS_PREFIX}{hour_key}"
    hourly_stats = r.hgetall(stats_key)

    for key, value in hourly_stats.items():
        if isinstance(key, bytes):
            key = key.decode('utf-8')
        if ':cached' in key:
            try:
                cache_hits += int(value)
            except (ValueError, TypeError):
                pass

    return {
        "queue_depths": queue_depths,
        "active_jobs": active_jobs,
        "worker_count": worker_count,
        "cache_hit_rate": cache_hits / max(cache_hits + cache_misses, 1),
        "hourly_stats": {k.decode('utf-8') if isinstance(k, bytes) else k: v for k, v in hourly_stats.items()},
        "timestamp": time.time()
    }

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(threadName)s - %(message)s",
    handlers=[
        logging.FileHandler("manager.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("phonos.mrs")

# --- Redis Connection ---
def _build_redis_pool_kwargs(decode_responses: bool) -> dict:
    """Build Redis connection pool kwargs with security settings."""
    kwargs = {
        "host": Config.REDIS_HOST,
        "port": Config.REDIS_PORT,
        "db": Config.REDIS_DB,
        "decode_responses": decode_responses,
        "encoding_errors": "ignore",
        "max_connections": 50,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "socket_keepalive": True,
        "health_check_interval": 30,
        "retry_on_timeout": True,
    }

    # Add password if configured
    if hasattr(Config, 'REDIS_PASSWORD') and Config.REDIS_PASSWORD:
        kwargs["password"] = Config.REDIS_PASSWORD

    # Add SSL/TLS if configured
    if hasattr(Config, 'REDIS_SSL') and Config.REDIS_SSL:
        import ssl
        kwargs["connection_class"] = redis.SSLConnection
        kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED

    return kwargs

redis_pool = redis.ConnectionPool(**_build_redis_pool_kwargs(decode_responses=True))
redis_binary_pool = redis.ConnectionPool(**_build_redis_pool_kwargs(decode_responses=False))


def get_redis_connection(*, binary_mode: bool = False) -> redis.Redis:
    """Return a Redis client from the appropriate connection pool."""
    pool = redis_binary_pool if binary_mode else redis_pool
    return redis.Redis(connection_pool=pool)


def derive_model_id_from_queue_name(queue_name: str) -> Optional[str]:
    """Extract the model identifier encoded in a Redis queue name.

    Queue names follow the pattern ``queue:job:<model_id>[:suffix]`` where the
    optional suffix may contain organization or team scoping information. This
    helper strips the prefix and returns the ``model_id`` component when the
    queue name matches the expected format.
    """

    if not isinstance(queue_name, str):
        return None
    if not queue_name.startswith("queue:job:"):
        return None
    parts = queue_name.split(":", 3)
    if len(parts) < 3:
        return None
    model_id = parts[2].strip()
    return model_id or None


def _extract_model_from_job_payload(job_payload: Mapping[str, Any]) -> Optional[str]:
    model_id = job_payload.get("model")
    if isinstance(model_id, str) and model_id:
        return model_id
    target_queue = job_payload.get("target_queue")
    if isinstance(target_queue, str):
        return derive_model_id_from_queue_name(target_queue)
    return None


def infer_model_id_from_active_job_state(active_job_state: Mapping[str, Any]) -> Optional[str]:
    """Attempt to resolve the model identifier for an active job record."""

    if not isinstance(active_job_state, Mapping):
        return None

    job_data = active_job_state.get("job_data")
    if isinstance(job_data, Mapping):
        model_id = _extract_model_from_job_payload(job_data)
        if model_id:
            return model_id
    elif isinstance(job_data, str):
        try:
            parsed = json.loads(job_data)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            model_id = _extract_model_from_job_payload(parsed)
            if model_id:
                return model_id

    fallback_queue = active_job_state.get("target_queue")
    if isinstance(fallback_queue, str):
        model_id = derive_model_id_from_queue_name(fallback_queue)
        if model_id:
            return model_id

    queue_name = active_job_state.get("queue")
    if isinstance(queue_name, str):
        model_id = derive_model_id_from_queue_name(queue_name)
        if model_id:
            return model_id

    return None


__all__ = [
    # Constants
    "ACTIVE_JOBS_HASH",
    "DEAD_LETTER_QUEUE",
    "IN_PROGRESS_JOBS_LIST",
    "JOB_RESULTS_QUEUE",
    "JOB_STATUS_CHANNEL_PREFIX",
    "JOB_TIMEOUT",
    "MAX_RETRY_ATTEMPTS",
    "NEW_JOBS_QUEUE",
    "NODE_REGISTRATION_CHANNEL",
    "NODE_REGISTRATION_CONFIRMATION_CHANNEL_PREFIX",
    "QUEUE_NAME_PATTERN",
    "WORKER_HEARTBEAT_CHANNEL",
    "WORKER_HEALTH_HASH",
    "WORKER_TIMEOUT",
    "FLOW_EXECUTION_QUEUE",
    # Priority Queue System
    "PRIORITY_QUEUE_PREFIX",
    "JOB_BATCH_QUEUE",
    "JobPriority",
    "PRIORITY_SLA_SECONDS",
    "get_priority_queue_name",
    "determine_job_priority",
    "get_priority_queue_stats",
    "PriorityQueueStats",
    # Sequence Caching & Deduplication
    "SEQUENCE_CACHE_HASH",
    "SEQUENCE_PENDING_HASH",
    "RESULT_CACHE_PREFIX",
    "CACHE_TTL_SECONDS",
    "compute_sequence_hash",
    "check_sequence_cache",
    "cache_sequence_result",
    "register_pending_sequence",
    "resolve_pending_sequence",
    "drain_pending_sequence",
    "has_active_sequence_computation",
    # Job Batching
    "BATCH_SIZE_THRESHOLD",
    "BATCH_SEQUENCE_LENGTH_THRESHOLD",
    "BATCH_WAIT_MS",
    "JobBatch",
    "is_batchable_job",
    # Analytics & Metrics
    "METRICS_STREAM",
    "METRICS_HASH",
    "HOURLY_STATS_PREFIX",
    "MODEL_LATENCY_HASH",
    "JobMetrics",
    "record_job_metrics",
    "get_model_latency_estimate",
    "get_pipeline_stats",
    # Core
    "Config",
    "derive_model_id_from_queue_name",
    "infer_model_id_from_active_job_state",
    "get_redis_connection",
    "logger",
]
