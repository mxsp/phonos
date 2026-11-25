"""Runtime worker health and usage metrics for model availability."""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

import requests as http_requests

from config import Config

from ..app import get_redis_connection, logger

_QUEUE_PREFIX = "queue:job:"

# ── NIM endpoint health pool ─────────────────────────────────────────────
# Tracks NVIDIA NIM API health per endpoint. Each probe adjusts the pool
# score: +1 on success (capped at NIM_POOL_MAX), -2 on failure (floored at
# NIM_POOL_MIN).  A model whose endpoint score is negative is treated as
# runtime-unavailable regardless of worker heartbeat count.
#
# Pool scores live in a Redis hash so all app processes share the same view.

NIM_HEALTH_POOL_HASH = "phonos:nim_health_pool"
NIM_HEALTH_LAST_CHECK_KEY = "phonos:nim_health_last_check"
NIM_HEALTH_CHECK_INTERVAL = 1800   # 30 minutes between probes
NIM_POOL_MAX = 5
NIM_POOL_MIN = -5
_NIM_PROBE_TIMEOUT = (5, 20)       # (connect, read) seconds

# Map model IDs → shared NIM endpoint key.  Models sharing the same NVIDIA
# backend share a pool score so one probe covers all of them.
NIM_ENDPOINT_MAP: dict[str, str] = {
    # ESMFold
    "type-0": "esmfold",
    "esmfold-re": "esmfold",
    # AlphaFold2
    "type-2": "alphafold2",
    "alphafold2-re": "alphafold2",
    # AlphaFold2 Multimer
    "alphafold2-re-multimer": "alphafold2-multimer",
    # DiffDock
    "type-3": "diffdock",
    "diffdock-re": "diffdock",
    # Evo2
    "evo2": "evo2",
    # Boltz2
    "type-9": "boltz2",
    # OpenFold3
    "openfold3": "openfold3",
    # RFDiffusion
    "type-12": "rfdiffusion",
    # ProteinMPNN
    "proteinmpnn-re": "proteinmpnn",
    # MolMIM
    "molmim": "molmim",
    # GenMol
    "genmol": "genmol",
}

# Endpoint key → NVIDIA health-check URL
NIM_ENDPOINT_URLS: dict[str, str] = {
    "esmfold":            "https://health.api.nvidia.com/v1/biology/meta/esmfold",
    "alphafold2":         "https://health.api.nvidia.com/v1/biology/deepmind/alphafold2",
    "alphafold2-multimer": "https://health.api.nvidia.com/v1/biology/deepmind/alphafold2-multimer",
    "diffdock":           "https://health.api.nvidia.com/v1/biology/mit/diffdock",
    "evo2":               "https://health.api.nvidia.com/v1/biology/arc/evo2-40b/generate",
    "boltz2":             "https://health.api.nvidia.com/v1/biology/mit/boltz2/predict",
    "openfold3":          "https://health.api.nvidia.com/v1/biology/openfold/openfold3/predict",
    "rfdiffusion":        "https://health.api.nvidia.com/v1/biology/ipd/rfdiffusion/generate",
    "proteinmpnn":        "https://health.api.nvidia.com/v1/biology/ipd/proteinmpnn/predict",
    "molmim":             "https://health.api.nvidia.com/v1/biology/nvidia/molmim/generate",
    "genmol":             "https://health.api.nvidia.com/v1/biology/nvidia/genmol/generate",
    "msa-search":         "https://health.api.nvidia.com/v1/biology/colabfold/msa-search",
}

# Minimal real payloads per endpoint.  Empty-body probes always return
# NVCF-STATUS: errored on NVIDIA's side even when the endpoint is fully
# operational, so we send the smallest valid request for each model.
# A fast 422/400 with a non-errored NVCF status, or 200/202, means healthy.
# A timeout or connection error means the endpoint is truly down.
_NIM_PROBE_PAYLOADS: dict[str, dict] = {
    "molmim":             {"smi": "C"},
    "genmol":             {"smiles": "C124CN3C1.S3(=O)(=O)CC.C4C#N.[*{20-20}]", "num_molecules": 1, "step_size": 1},
    "diffdock":           {"protein": "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND", "ligand": "C", "ligand_file_type": "smiles", "num_poses": 1, "time_divisions": 1, "steps": 1},
    "esmfold":            {"sequence": "AA"},
    "alphafold2":         {"sequence": "AA"},
    "alphafold2-multimer": {"sequences": ["AA", "AA"]},
    "evo2":               {"sequence": "ATCG", "num_tokens": 8},
    "boltz2":             {"sequences": [{"protein": {"id": "A", "sequence": "AA"}}]},
    "openfold3":          {"sequences": [{"protein": {"id": "A", "sequence": "AA"}}]},
    "rfdiffusion":        {"contigs": ["10"]},
    "proteinmpnn":        {"pdb": "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND"},
    "msa-search":         {"sequence": "AA", "databases": ["uniref90"], "max_msa_sequences": 10},
}


def _probe_nim_endpoint(endpoint_key: str) -> bool:
    """Probe a single NIM endpoint.  Returns True when operational.

    Uses a minimal real payload instead of an empty body because NVIDIA's
    gateway always returns NVCF-STATUS: errored for empty requests even when
    the underlying function is healthy.  A fast response (any HTTP status with
    NVCF-STATUS != errored, or a 200/202) counts as healthy.  A timeout or
    missing NVCF-REQID counts as down.
    """
    url = NIM_ENDPOINT_URLS.get(endpoint_key)
    if not url:
        return False
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = _NIM_PROBE_PAYLOADS.get(endpoint_key, {})
    try:
        resp = http_requests.post(url, json=payload, headers=headers, timeout=_NIM_PROBE_TIMEOUT)
        nvcf_reqid = resp.headers.get("NVCF-REQID") or resp.headers.get("nvcf-reqid")
        nvcf_status = (
            resp.headers.get("NVCF-STATUS")
            or resp.headers.get("Nvcf-Status")
            or ""
        ).strip().lower()
        # No NVCF-REQID at all → gateway/auth failure, not an endpoint issue.
        # Treat as unknown (return False to be safe).
        if not nvcf_reqid:
            return False
        # Successful completion
        if nvcf_status in ("fulfilled", ""):
            if resp.status_code in (200, 202, 400, 422):
                return True
        # Explicitly errored — endpoint received request but function failed
        if nvcf_status in ("errored", "error", "failed"):
            return False
        # Any other status with an NVCF-REQID means the gateway is live
        # and the function infrastructure is responding.
        if resp.status_code in (200, 202, 400, 422):
            return True
        if resp.status_code == 500 and nvcf_reqid:
            return True
        return False
    except http_requests.exceptions.Timeout:
        return False
    except Exception:
        return False


def _run_nim_health_probes(redis_conn) -> dict[str, bool]:
    """Probe all NIM endpoints in parallel, update pool scores in Redis."""
    results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=len(NIM_ENDPOINT_URLS)) as pool:
        futures = {pool.submit(_probe_nim_endpoint, ek): ek for ek in NIM_ENDPOINT_URLS}
        for fut in as_completed(futures):
            ek = futures[fut]
            try:
                results[ek] = fut.result(timeout=15)
            except Exception:
                results[ek] = False

    # Update pool scores atomically
    pipe = redis_conn.pipeline()
    for ek, ok in results.items():
        current_raw = redis_conn.hget(NIM_HEALTH_POOL_HASH, ek)
        try:
            current = int(current_raw) if current_raw is not None else 0
        except (TypeError, ValueError):
            current = 0
        new_score = min(current + 1, NIM_POOL_MAX) if ok else max(current - 2, NIM_POOL_MIN)
        pipe.hset(NIM_HEALTH_POOL_HASH, ek, new_score)
    pipe.set(NIM_HEALTH_LAST_CHECK_KEY, str(time.time()))
    pipe.execute()

    logger.info(
        "NIM health pool updated: %s",
        ", ".join(f"{ek}={'OK' if ok else 'FAIL'}" for ek, ok in sorted(results.items())),
    )
    return results


def _maybe_refresh_nim_pool(redis_conn) -> None:
    """Trigger a NIM health probe cycle if the last one was >30 min ago."""
    try:
        last_raw = redis_conn.get(NIM_HEALTH_LAST_CHECK_KEY)
        last_check = float(last_raw) if last_raw else 0.0
    except (TypeError, ValueError):
        last_check = 0.0

    if time.time() - last_check >= NIM_HEALTH_CHECK_INTERVAL:
        _run_nim_health_probes(redis_conn)


def get_nim_availability(redis_conn, model_ids: Iterable[str]) -> dict[str, bool]:
    """Return {model_id: True/False} based on NIM endpoint pool scores.

    Disabled: NVIDIA's empty-body and minimal-payload probe responses are
    unreliable (always return NVCF-STATUS: errored even for healthy
    endpoints).  All NIM-backed models are treated as available; actual
    failures are caught at job-execution time instead.
    """
    return {mid: True for mid in model_ids}


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None:
        return ""
    return str(value)


def _decode_json(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    text = _decode_text(value)
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return data
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_job_model(active_job_state: dict[str, Any]) -> str:
    job_data = active_job_state.get("job_data")

    if isinstance(job_data, str):
        try:
            job_data = json.loads(job_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    if isinstance(job_data, dict):
        model_id = job_data.get("model")
        if model_id:
            return str(model_id)
    return None


def collect_model_runtime_status(models: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build runtime health/usage metrics keyed by model identifier."""
    model_ids = {str(model.get("id")) for model in models if model.get("id")}
    now = time.time()
    heartbeat_timeout = int(getattr(Config, "WORKER_HEARTBEAT_TIMEOUT", 120) or 120)
    stale_cutoff = now - heartbeat_timeout

    runtime: dict[str, dict[str, Any]] = {
        model_id: {
            "model_id": model_id,
            "worker_count": 0,
            "active_jobs": 0,
            "queue_depth": 0,
            "last_seen": 0.0,
            "uptime_seconds_total": 0.0,
            "uptime_seconds_max": 0.0,
            "worker_ids": [],
        }
        for model_id in model_ids
    }

    if not runtime:
        return {}

    redis_conn = get_redis_connection()

    try:
        worker_health_data = redis_conn.hgetall("hash:worker_health") or {}
        for worker_id_raw, worker_info_raw in worker_health_data.items():
            worker_id = _decode_text(worker_id_raw)
            worker_info = _decode_json(worker_info_raw)
            if not worker_info:
                continue

            worker_type = _decode_text(worker_info.get("type")).strip()
            if not worker_type or worker_type not in runtime:
                continue

            last_seen = _safe_float(worker_info.get("last_seen", worker_info.get("timestamp", 0.0)))
            if last_seen < stale_cutoff:
                continue

            first_seen = _safe_float(worker_info.get("first_seen", last_seen), default=last_seen)
            uptime_seconds = max(0.0, now - first_seen)

            model_runtime = runtime[worker_type]
            model_runtime["worker_count"] += 1
            model_runtime["last_seen"] = max(model_runtime["last_seen"], last_seen)
            model_runtime["uptime_seconds_total"] += uptime_seconds
            model_runtime["uptime_seconds_max"] = max(model_runtime["uptime_seconds_max"], uptime_seconds)
            model_runtime["worker_ids"].append(worker_id)
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        logger.warning("Failed to collect worker heartbeat metrics: %s", exc)

    try:
        active_jobs_data = redis_conn.hgetall("hash:active_jobs") or {}
        for active_job_raw in active_jobs_data.values():
            active_job_state = _decode_json(active_job_raw)
            if not active_job_state:
                continue
            model_id = _extract_job_model(active_job_state)
            if model_id and model_id in runtime:
                runtime[model_id]["active_jobs"] += 1
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        logger.warning("Failed to collect active job metrics: %s", exc)

    try:
        for key_raw in redis_conn.scan_iter(match=f"{_QUEUE_PREFIX}*"):
            key = _decode_text(key_raw)
            if not key.startswith(_QUEUE_PREFIX):
                continue
            model_id = key[len(_QUEUE_PREFIX):].split(":", 1)[0]
            if model_id not in runtime:
                continue
            try:
                runtime[model_id]["queue_depth"] += int(redis_conn.llen(key_raw))
            except Exception:
                continue
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        logger.warning("Failed to collect queue depth metrics: %s", exc)

    # ── NIM health pool (disabled — probes are unreliable) ──────────────
    nim_available = {mid: True for mid in model_ids}

    for model_id, model_runtime in runtime.items():
        worker_count = int(model_runtime["worker_count"])
        active_jobs = int(model_runtime["active_jobs"])
        queue_depth = int(model_runtime["queue_depth"])

        # NIM endpoint health gates runtime availability for NIM-backed models
        nim_ok = nim_available.get(model_id, True)

        if not nim_ok and model_id in NIM_ENDPOINT_MAP:
            # NVIDIA endpoint is down — workers exist but can't process
            status = "nim_down"
            runtime_avail = False
        elif worker_count > 0 and (active_jobs > 0 or queue_depth > 0):
            status = "busy"
            runtime_avail = True
        elif worker_count > 0:
            status = "online"
            runtime_avail = True
        elif active_jobs > 0 or queue_depth > 0:
            status = "degraded"
            runtime_avail = False
        else:
            status = "offline"
            runtime_avail = False

        avg_uptime = model_runtime["uptime_seconds_total"] / worker_count if worker_count else 0.0
        usage_ratio = (active_jobs / worker_count) if worker_count else None

        model_runtime.update(
            {
                "status": status,
                "runtime_available": runtime_avail,
                "nim_available": nim_ok,
                "avg_uptime_seconds": avg_uptime,
                "usage_ratio": usage_ratio,
            }
        )

    return runtime


def apply_runtime_status_to_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach runtime metrics to model metadata without hardcoded model IDs."""
    runtime = collect_model_runtime_status(models)
    updated_models: list[dict[str, Any]] = []

    for model in models:
        model_id = str(model.get("id") or "")
        runtime_info = runtime.get(model_id, {})
        normalized = dict(model)

        normalized["worker_count"] = int(runtime_info.get("worker_count", 0))
        normalized["active_jobs"] = int(runtime_info.get("active_jobs", 0))
        normalized["queue_depth"] = int(runtime_info.get("queue_depth", 0))
        normalized["runtime_status"] = runtime_info.get("status", "offline")
        normalized["runtime_available"] = bool(runtime_info.get("runtime_available", False))
        normalized["nim_available"] = bool(runtime_info.get("nim_available", True))
        normalized["worker_uptime_avg_seconds"] = float(runtime_info.get("avg_uptime_seconds", 0.0))
        normalized["worker_uptime_max_seconds"] = float(runtime_info.get("uptime_seconds_max", 0.0))
        normalized["worker_usage_ratio"] = runtime_info.get("usage_ratio")

        runtime_managed = bool(normalized.get("runtime_managed", False))
        declared_available = bool(normalized.get("available", True))
        declared_active = normalized.get("is_active")
        explicitly_active = declared_active is not None and bool(declared_active)
        if declared_active is None:
            declared_active = declared_available

        runtime_available = normalized["runtime_available"]
        if runtime_managed:
            normalized["available"] = bool(declared_available and runtime_available)
            normalized["is_active"] = bool(bool(declared_active) and runtime_available)
        # Models with explicit is_active: True in config (e.g. NIM-direct
        # models with no workers) stay active regardless of worker status.
        elif explicitly_active:
            normalized["available"] = declared_available
            normalized["is_active"] = True
        else:
            normalized["available"] = bool(declared_available and runtime_available)
            normalized["is_active"] = bool(bool(declared_active) and runtime_available)

        if normalized.get("greyed_out"):
            normalized["available"] = False
            normalized["is_active"] = False

        updated_models.append(normalized)

    return updated_models
