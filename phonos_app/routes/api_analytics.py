"""API routes for analytics, budget alerts, pipelines, and templates."""
from __future__ import annotations

import gzip
import json
import math
import os
import secrets
import shlex
import shutil
import psycopg2
import time
import uuid
import requests as http_requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import bcrypt
import redis
from flask import (
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import join_room
from werkzeug.utils import secure_filename

from config import Config

# Import encryption for secure job payloads
try:
    from encryption_util import encrypt
except ImportError:
    encrypt = None

TEAM_MUTABLE_ROLES = Config.TEAM_MUTABLE_ROLES
TEAM_ROLE_ADMIN = Config.TEAM_ROLE_ADMIN
TEAM_ROLE_BILLING = Config.TEAM_ROLE_BILLING
TEAM_ROLE_MEMBER = Config.TEAM_ROLE_MEMBER
TEAM_ROLE_OWNER = Config.TEAM_ROLE_OWNER
PROJECT_MUTABLE_ROLES = Config.PROJECT_MUTABLE_ROLES
PROJECT_ROLE_CONTRIBUTOR = Config.PROJECT_ROLE_CONTRIBUTOR
PROJECT_ROLE_MANAGER = Config.PROJECT_ROLE_MANAGER
PROJECT_ROLE_VIEWER = Config.PROJECT_ROLE_VIEWER
from plan_service import DEFAULT_PLAN, get_plan_definition, is_valid_plan, normalize_plan
from model_overrides import apply_overrides_to_models, fetch_model_overrides, get_model_state

from ..app import NODE_ID_PATTERN, SUPPORTED_MODEL_IDS, app, get_redis_connection, logger, socketio
from ..api_cache import api_cache
from ..helpers import (
    admin_required,
    allowed_file,
    apply_plan_to_user,
    build_plan_catalog,
    create_user_notification,
    delete_job_file_and_update_storage,
    determine_job_target_queue,
    ensure_monthly_credit_reset,
    ensure_notifications_table,
    ensure_personal_team,
    ensure_user_layout_columns,
    externalize_project_role,
    format_storage_bytes,
    generate_unique_team_slug,
    get_db,
    get_team_membership,
    get_team_node_rows,
    get_project_assignment,
    get_project_storage_roots,
    get_safe_project_path,
    get_user_storage_state,
    is_valid_public_key,
    login_required,
    normalize_project_role,
    normalize_worker_types,
    team_has_team_plan,
    team_is_enterprise,
    team_role_required,
    parse_sqlite_timestamp,
    parse_worker_types,
    project_role_required,
    record_job_notification,
    send_email,
    serialize_datetime,
    serialize_team,
    serialize_team_member,
    serialize_team_node,
    user_has_team_plan,
    tags as tags_helper,
)

# Import security decorators
from ..helpers.security import csrf_protect, rate_limit, sanitize_json_inputs, sanitize_html, sanitize_input, validate_sql_identifier
from ..helpers.file_scanner import scan_upload

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}

# ── Model health-check configuration ─────────────────────────────────────
MODEL_HEALTH_ENDPOINTS = {
    'type-0':                  'https://health.api.nvidia.com/v1/biology/meta/esmfold',
    'type-2':                  'https://health.api.nvidia.com/v1/biology/deepmind/alphafold2',
    'type-3':                  'https://health.api.nvidia.com/v1/biology/mit/diffdock',
    'evo2':                    'https://health.api.nvidia.com/v1/biology/arc/evo2-40b/generate',
    'type-9':                  'https://health.api.nvidia.com/v1/biology/mit/boltz2/predict',
    'openfold3':               'https://health.api.nvidia.com/v1/biology/openfold/openfold3/predict',
    'type-12':                 'https://health.api.nvidia.com/v1/biology/ipd/rfdiffusion/generate',
    # NIM RE-variants (share upstream endpoints with type-X siblings)
    'alphafold2-re':           'https://health.api.nvidia.com/v1/biology/deepmind/alphafold2',
    'alphafold2-re-multimer':  'https://health.api.nvidia.com/v1/biology/deepmind/alphafold2-multimer',
    'esmfold-re':              'https://health.api.nvidia.com/v1/biology/meta/esmfold',
    'diffdock-re':             'https://health.api.nvidia.com/v1/biology/mit/diffdock',
    'proteinmpnn-re':          'https://health.api.nvidia.com/v1/biology/ipd/proteinmpnn/predict',
    'molmim':                  'https://health.api.nvidia.com/v1/biology/nvidia/molmim/generate',
}
HEALTH_CACHE_TTL = 300              # 5 min cache per model
HEALTH_CHECK_TIMEOUT = (3, 8)       # (connect, read) seconds
HEALTH_CACHE_KEY = 'phonos:model_health'
HEALTH_HISTORY_KEY = 'phonos:model_health_history'
HEALTH_HISTORY_RETENTION = 86400    # keep 24 h of probes for uptime %


def _normalize_model_status_name(value: str) -> str:
    """Normalize model names for case-insensitive status lookups."""
    return " ".join(value.strip().lower().split())


# ── NVIDIA health-probe helpers ───────────────────────────────────────────

def _probe_nvidia_endpoint(model_id: str) -> dict[str, Any]:
    """Probe a single NVIDIA NIM endpoint and return a health dict."""
    url = MODEL_HEALTH_ENDPOINTS.get(model_id)
    if not url:
        return {
            'model_id': model_id, 'status': 'no_endpoint',
            'http_status': 0, 'response_time_ms': 0,
            'checked_at': datetime.utcnow().isoformat() + 'Z',
        }

    api_key = os.environ.get('NVIDIA_API_KEY', '')
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'NVCF-POLL-SECONDS': '5',
    }

    start = time.time()
    try:
        resp = http_requests.post(
            url, json={}, headers=headers, timeout=HEALTH_CHECK_TIMEOUT,
        )
        elapsed_ms = round((time.time() - start) * 1000)

        nvcf = (
            resp.headers.get('NVCF-STATUS')
            or resp.headers.get('Nvcf-Status')
            or ''
        ).strip().lower()

        # 422 = input validation ran → model is loaded and OPERATIONAL
        # (NVCF-STATUS may still say "errored" for rejected input)
        if resp.status_code == 422:
            status = 'operational'
        elif resp.status_code in (200, 202):
            # Check NVCF-STATUS: fulfilled=ok, errored=model failed
            status = 'down' if nvcf in ('errored', 'error', 'failed') else 'operational'
        elif resp.status_code == 400:
            status = 'operational'          # reachable, bad request
        elif resp.status_code == 401:
            status = 'auth_error'
        elif resp.status_code in (503, 504):
            status = 'down'
        elif resp.status_code == 500:
            status = 'degraded'             # reachable but erroring
        else:
            status = 'down'

        return {
            'model_id': model_id, 'status': status,
            'http_status': resp.status_code, 'nvcf_status': nvcf,
            'response_time_ms': elapsed_ms,
            'checked_at': datetime.utcnow().isoformat() + 'Z',
        }
    except http_requests.exceptions.Timeout:
        return {
            'model_id': model_id, 'status': 'timeout',
            'http_status': 0, 'response_time_ms': round((time.time() - start) * 1000),
            'checked_at': datetime.utcnow().isoformat() + 'Z',
        }
    except Exception:
        return {
            'model_id': model_id, 'status': 'unreachable',
            'http_status': 0, 'response_time_ms': round((time.time() - start) * 1000),
            'checked_at': datetime.utcnow().isoformat() + 'Z',
        }


def _cache_health_result(r, result: dict) -> None:
    """Store a single health result in Redis + append to history + update NIM pool."""
    from ..helpers.worker_runtime import (
        NIM_ENDPOINT_MAP,
        NIM_HEALTH_POOL_HASH,
        NIM_POOL_MAX,
        NIM_POOL_MIN,
    )

    mid = result['model_id']
    r.setex(f'{HEALTH_CACHE_KEY}:{mid}', HEALTH_CACHE_TTL, json.dumps(result))
    now = time.time()
    hist_key = f'{HEALTH_HISTORY_KEY}:{mid}'
    r.zadd(hist_key, {json.dumps(result): now})
    r.zremrangebyscore(hist_key, '-inf', now - HEALTH_HISTORY_RETENTION)

    # Sync to NIM status pool so worker_runtime picks up latest health
    endpoint_key = NIM_ENDPOINT_MAP.get(mid)
    if endpoint_key:
        is_ok = result.get('status') in ('operational',)
        current_raw = r.hget(NIM_HEALTH_POOL_HASH, endpoint_key)
        try:
            current = int(current_raw) if current_raw is not None else 0
        except (TypeError, ValueError):
            current = 0
        new_score = min(current + 1, NIM_POOL_MAX) if is_ok else max(current - 2, NIM_POOL_MIN)
        r.hset(NIM_HEALTH_POOL_HASH, endpoint_key, new_score)


def _get_cached_health(r, model_id: str) -> dict:
    raw = r.get(f'{HEALTH_CACHE_KEY}:{model_id}')
    return json.loads(raw) if raw else None


def _compute_uptime_percent(r, model_id: str) -> float:
    """Uptime % from last 24 h of health probes."""
    entries = r.zrangebyscore(
        f'{HEALTH_HISTORY_KEY}:{model_id}',
        time.time() - HEALTH_HISTORY_RETENTION, '+inf',
    )
    if not entries:
        return None
    total = len(entries)
    ok = sum(
        1 for raw in entries
        if json.loads(raw).get('status') == 'operational'
    )
    return round(ok / total * 100, 2)


def _check_all_models_health(force: bool = False) -> list[dict[str, Any]]:
    """Probe all models in parallel; uses Redis cache unless *force*."""
    try:
        r = get_redis_connection()
    except Exception:
        return []

    results: list[dict] = []
    to_check: list[str] = []

    for mid in MODEL_HEALTH_ENDPOINTS:
        if not force:
            cached = _get_cached_health(r, mid)
            if cached:
                results.append(cached)
                continue
        to_check.append(mid)

    if to_check:
        with ThreadPoolExecutor(max_workers=len(to_check)) as pool:
            futs = {pool.submit(_probe_nvidia_endpoint, m): m for m in to_check}
            for fut in as_completed(futs):
                try:
                    res = fut.result(timeout=15)
                except Exception:
                    res = {
                        'model_id': futs[fut], 'status': 'unknown',
                        'http_status': 0, 'response_time_ms': 0,
                        'checked_at': datetime.utcnow().isoformat() + 'Z',
                    }
                try:
                    _cache_health_result(r, res)
                except Exception:
                    pass
                results.append(res)

    return results


# ── Model uptime builder (now backed by live probes) ─────────────────────

def _build_usage_model_uptime() -> list[dict[str, Any]]:
    """Build per-model uptime payload using cached NVIDIA health probes + runtime worker data."""
    from ..helpers.worker_runtime import collect_model_runtime_status

    deduped_models: list[dict[str, str]] = []
    seen_names: set[str] = set()
    hidden_model_ids = set(getattr(Config, "UI_HIDDEN_MODEL_IDS", set()))

    for model in Config.SUPPORTED_MODELS:
        model_id = model.get("id")
        if model_id in hidden_model_ids:
            continue

        display_name = (model.get("short_name") or model.get("name") or model.get("id") or "Unknown").strip()
        if not display_name:
            continue

        normalized_name = _normalize_model_status_name(display_name)
        if normalized_name in seen_names:
            continue

        seen_names.add(normalized_name)
        deduped_models.append({
            "model_id": model_id or normalized_name,
            "model_name": display_name,
            "normalized_name": normalized_name,
            "greyed_out": bool(model.get("greyed_out")),
            "status_override": model.get("status_override"),
        })

    # Collect runtime worker metrics (worker count, NIM health, etc.)
    runtime_map: dict[str, dict] = {}
    try:
        runtime_map = collect_model_runtime_status(Config.SUPPORTED_MODELS)
    except Exception:
        pass

    # Collect cached health + uptime history
    health_map: dict[str, dict] = {}
    try:
        r = get_redis_connection()
        for m in deduped_models:
            mid = m["model_id"]
            cached = _get_cached_health(r, mid)
            if cached:
                health_map[mid] = cached
            uptime = _compute_uptime_percent(r, mid)
            if uptime is not None:
                health_map.setdefault(mid, {})['_uptime_computed'] = uptime
    except Exception:
        pass

    rows: list[dict[str, Any]] = []
    for m in deduped_models:
        mid = m["model_id"]
        h = health_map.get(mid, {})
        rt = runtime_map.get(mid, {})
        live = h.get('status', '')
        rt_status = rt.get('status', '')

        # Determine display status combining endpoint health + worker availability
        if m.get("greyed_out"):
            status, label = 'inoperable', 'Offline'
        elif rt_status == 'nim_down':
            status, label = 'inoperable', 'Unavailable'
        elif live == 'operational':
            status, label = 'operational', 'Online'
        elif live in ('down', 'unreachable', 'timeout'):
            status, label = 'inoperable', 'Offline'
        elif live == 'auth_error':
            status, label = 'degraded', 'Auth Error'
        elif live == 'no_endpoint':
            status, label = 'inoperable', 'N/A'
        else:
            # No cached probe yet — mark unchecked
            has_endpoint = mid in MODEL_HEALTH_ENDPOINTS
            status = 'operational' if has_endpoint else 'inoperable'
            label = 'Unchecked' if has_endpoint else 'Unavailable'

        # Apply config-level status override (e.g. force 'degraded')
        status_override = m.get("status_override")
        if status_override == 'degraded':
            status, label = 'degraded', 'Degraded'
        elif status_override == 'offline':
            status, label = 'inoperable', 'Offline'

        uptime_pct = h.get('_uptime_computed', 0.0) or 0.0
        worker_count = int(rt.get('worker_count', 0))

        row = {
            "model_id": mid,
            "model_name": m["model_name"],
            "status": status,
            "status_label": label,
            "uptime_percent": uptime_pct,
            "health_status": live or 'unchecked',
            "runtime_status": rt_status or 'unknown',
            "worker_count": worker_count,
            "response_time_ms": h.get('response_time_ms', 0),
            "checked_at": h.get('checked_at', ''),
            "http_status": h.get('http_status', 0),
        }
        if m.get("greyed_out"):
            row["greyed_out"] = True
        rows.append(row)

    rows.sort(key=lambda item: (
        0 if item["status"] == "operational" else 1,
        item["model_name"].lower(),
    ))
    return rows


@app.route('/api/analytics/usage', methods=['GET'])
@login_required
@api_cache(ttl=60, make_key=lambda: f"usage:{session.get('user_id', 'anon')}:{request.args.get('days', '30')}")
def get_usage_analytics():
    """Get usage analytics for the authenticated user."""
    from ..helpers.analytics import get_usage_summary, get_daily_usage

    user_id = session['user_id']
    days = request.args.get('days', 30, type=int)
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    try:
        # Get summary using optimized helper
        summary_raw = get_usage_summary(user_id, start_date, end_date)
        
        # Map summary keys to frontend expectations
        summary = {
            'total_events': summary_raw.get('total_events', 0) or 0,
            'total_credits_used': summary_raw.get('total_credits', 0) or 0,
            'total_compute_time': summary_raw.get('total_compute_time', 0) or 0
        }

        # Get job status breakdown from helper result
        # Helper returns list of dicts: {'job_status': '...', 'count': ..., 'credits': ...}
        # Frontend expects: {'job_status': '...', 'job_count': ..., 'credits_used': ...}
        by_job_status = []
        for item in summary_raw.get('by_job_status', []):
            by_job_status.append({
                'job_status': item.get('job_status', 'unknown'),
                'job_count': item.get('count', 0),
                'credits_used': item.get('credits', 0)
            })

        # Get event type breakdown from helper result
        # Helper returns list of dicts: {'event_type': '...', 'count': ..., 'credits': ...}
        # Frontend expects: {'event_type': '...', 'event_count': ..., 'credits_used': ...}
        by_event_type = []
        for item in summary_raw.get('by_event_type', []):
            by_event_type.append({
                'event_type': item.get('event_type', 'unknown'),
                'event_count': item.get('count', 0),
                'credits_used': item.get('credits', 0)
            })

        # Get model breakdown from helper result
        # Helper returns list of dicts: {'model': '...', 'count': ..., 'credits': ..., 'compute_time': ...}
        # Frontend expects: {'model': '...', 'job_count': ..., 'credits_used': ..., 'compute_time': ...}
        # Merge alias ids onto their canonical model (e.g. "boltz2" / "type-boltz2"
        # -> "type-9") and attach a human-readable name. Without this the same model
        # can appear as two separate buckets and show a raw id like "type-9" in the
        # statistics, depending on which id was persisted for each job.
        from ..helpers.models import canonical_model_id, get_supported_model_name
        merged_models: dict[str, dict] = {}
        for item in summary_raw.get('by_model', []):
            raw_id = item.get('model') or 'unknown'
            canonical = canonical_model_id(raw_id) or raw_id
            bucket = merged_models.get(canonical)
            if bucket is None:
                bucket = {
                    'model': canonical,
                    'model_name': get_supported_model_name(canonical, short=True),
                    'job_count': 0,
                    'credits_used': 0,
                    'compute_time': 0,
                }
                merged_models[canonical] = bucket
            bucket['job_count'] += item.get('count', 0) or 0
            bucket['credits_used'] += item.get('credits', 0) or 0
            bucket['compute_time'] += item.get('compute_time', 0) or 0
        by_model = sorted(
            merged_models.values(), key=lambda b: b['credits_used'], reverse=True
        )

        # Get daily usage using optimized helper
        daily_raw = get_daily_usage(user_id, days)
        
        # Map daily keys to frontend expectations
        # Helper: date, events, credits, compute_time
        # Frontend: date, event_count, credits_used, compute_time
        daily_usage = []
        for item in daily_raw:
            daily_usage.append({
                'date': item.get('date'),
                'event_count': item.get('events', 0),
                'credits_used': item.get('credits', 0),
                'compute_time': item.get('compute_time', 0)
            })

        return jsonify({
            'status': 'success',
            'summary': summary,
            'daily_usage': daily_usage,
            'by_event_type': by_event_type,
            'by_job_status': by_job_status,
            'by_model': by_model,
            'model_uptime': _build_usage_model_uptime(),
        })

    except Exception as e:
        logger.error(f"Usage analytics query failed: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'summary': {
                'total_events': 0,
                'total_credits_used': 0,
                'total_compute_time': 0
            },
            'daily_usage': [],
            'by_event_type': [],
            'by_job_status': [],
            'by_model': [],
            'model_uptime': _build_usage_model_uptime(),
            'message': 'Usage analytics data not available'
        })


@app.route('/api/analytics/quantum-status', methods=['GET'])
@login_required
@api_cache(ttl=300, make_key=lambda: f"quantum-status:{session.get('user_id', 'anon')}")
def get_quantum_status():
    """IBM Quantum reachability for the Usage Analytics page.

    Reports whether IBM Quantum answers with the user's resolved credentials
    and which quantum computers are reachable right now (live queue depth,
    qubit count, operational flag). Backed by the 60s-cached live backend
    query in quantum_runtime, so repeated page loads don't hammer IBM.
    """
    from ..helpers.quantum_runtime import get_available_backends, get_runtime_status

    user_id = session['user_id']
    credential_source = 'None'
    execution_mode = None
    try:
        rt = get_runtime_status(user_id)
        credential_source = rt.get('credential_source', 'None')
        execution_mode = rt.get('execution_mode')
    except Exception:
        pass

    try:
        data = get_available_backends(user_id, refresh=request.args.get('refresh') in ('1', 'true'))
    except Exception as e:
        logger.warning("Quantum status: backend query failed for user %s: %s", user_id, e)
        data = {'live': False, 'hardware': []}

    live = bool(data.get('live'))
    backends = data.get('hardware', []) if live else []

    def _is_active(b):
        # Matches IBM's own dashboard semantics: a device only executes jobs
        # when operational AND status_msg == 'active' (otherwise it shows as
        # paused / maintenance / dedicated there).
        return b.get('operational', True) and (b.get('status_msg') or 'active') == 'active'

    return jsonify({
        'reachable': live,
        'credential_source': credential_source,
        'execution_mode': execution_mode,
        'backends': backends,
        'active_count': sum(1 for b in backends if _is_active(b)),
        'operational_count': sum(1 for b in backends if b.get('operational', True)),
        'checked_at': datetime.now().isoformat(),
    })


@app.route('/api/model-health/refresh', methods=['POST'])
@login_required
@rate_limit(limit=3, window=60, action='model_health_refresh')
def refresh_model_health():
    """Force-refresh all NVIDIA model health probes."""
    try:
        results = _check_all_models_health(force=True)
        return jsonify({'status': 'success', 'models': results})
    except Exception as e:
        logger.error(f"Model health refresh failed: {e}", exc_info=True)
        return jsonify({'status': 'error', 'models': [], 'message': str(e)[:200]}), 500


@app.route('/api/model-runtime-status', methods=['GET'])
@login_required
@api_cache(ttl=30, make_key=lambda: f"runtime:{session.get('user_id', 'anon')}")
def get_model_runtime_status():
    """Return live runtime status for all models (worker counts, NIM health).

    Called by the frontend on a 30-minute polling interval so model cards
    can be greyed out dynamically when workers go offline.
    """
    from ..helpers.worker_runtime import apply_runtime_status_to_models

    try:
        enriched = apply_runtime_status_to_models(
            [dict(m) for m in Config.SUPPORTED_MODELS]
        )
        payload = []
        for m in enriched:
            payload.append({
                'id': m.get('id'),
                'worker_count': m.get('worker_count', 0),
                'active_jobs': m.get('active_jobs', 0),
                'queue_depth': m.get('queue_depth', 0),
                'runtime_status': m.get('runtime_status', 'offline'),
                'runtime_available': m.get('runtime_available', False),
                'nim_available': m.get('nim_available', True),
                'available': m.get('available', False),
                'is_active': m.get('is_active', False),
                'worker_uptime_avg_seconds': m.get('worker_uptime_avg_seconds', 0),
                'worker_uptime_max_seconds': m.get('worker_uptime_max_seconds', 0),
            })
        return jsonify({'status': 'success', 'models': payload})
    except Exception as e:
        logger.error("Model runtime status fetch failed: %s", e, exc_info=True)
        return jsonify({'status': 'error', 'models': []}), 500


@app.route('/api/budget/alerts', methods=['GET'])
@login_required
def get_budget_alerts():
    """Get budget alerts for the authenticated user."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    try:
        result = db.execute('''
            SELECT id, team_id, alert_type, threshold_value, current_value,
                   is_triggered, last_triggered_at, is_active, created_at
            FROM budget_alerts
            WHERE user_id = %s AND is_active = 1
            ORDER BY created_at DESC
        ''', (user_id,))
    except Exception as e:
        # Table may not exist or other database error
        logger.warning(f"Budget alerts query failed: {e}")
        return jsonify({'status': 'success', 'alerts': [], 'message': 'Budget alerts not available'})

    alerts = []
    for row in result.fetchall():
        alerts.append({
            'id': row['id'],
            'team_id': row['team_id'],
            'alert_type': row['alert_type'],
            'threshold_value': row['threshold_value'],
            'current_value': row['current_value'],
            'is_triggered': row['is_triggered'],
            'last_triggered_at': row['last_triggered_at'],
            'is_active': row['is_active'],
            'created_at': row['created_at']
        })

    return jsonify({'status': 'success', 'alerts': alerts})


@app.route('/api/budget/alerts', methods=['POST'])
@login_required
@csrf_protect
def create_budget_alert():
    """Create a new budget alert."""
    user_id = session['user_id']
    data = request.get_json()

    alert_type = data.get('alert_type')
    threshold_value = data.get('threshold_value')
    email_notification = data.get('email_notification', True)

    if not alert_type or not threshold_value:
        return jsonify({'status': 'error', 'message': 'Alert type and threshold are required'}), 400

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute('''
        INSERT INTO budget_alerts (user_id, alert_type, threshold_value, current_value,
                                    is_triggered, is_active, created_at)
        VALUES (%s, %s, %s, 0, FALSE, TRUE, CURRENT_TIMESTAMP)
        RETURNING id
    ''', (user_id, alert_type, threshold_value))

    row = result.fetchone()
    alert_id = row['id'] if row else None
    db.commit()

    return jsonify({
        'status': 'success',
        'message': 'Budget alert created successfully',
        'alert_id': alert_id
    })


@app.route('/api/budget/alerts/<int:alert_id>', methods=['PUT'])
@login_required
@csrf_protect
def update_budget_alert(alert_id):
    """Update a budget alert."""
    user_id = session['user_id']
    data = request.get_json()

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Check ownership
    result = db.execute('SELECT id FROM budget_alerts WHERE id = %s AND user_id = %s', (alert_id, user_id))
    if not result.fetchone():
        return jsonify({'status': 'error', 'message': 'Budget alert not found'}), 404

    threshold_value = data.get('threshold_value')
    if threshold_value:
        db.execute('UPDATE budget_alerts SET threshold_value = %s WHERE id = %s',
                       (threshold_value, alert_id))
        db.commit()

    return jsonify({'status': 'success', 'message': 'Budget alert updated successfully'})


@app.route('/api/budget/alerts/<int:alert_id>', methods=['DELETE'])
@login_required
@csrf_protect
def delete_budget_alert(alert_id):
    """Delete a budget alert."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Check ownership
    result = db.execute('SELECT id FROM budget_alerts WHERE id = %s AND user_id = %s', (alert_id, user_id))
    if not result.fetchone():
        return jsonify({'status': 'error', 'message': 'Budget alert not found'}), 404

    db.execute('UPDATE budget_alerts SET is_active = 0 WHERE id = %s', (alert_id,))
    db.commit()

    return jsonify({'status': 'success', 'message': 'Budget alert deleted successfully'})


@app.route('/api/pipelines/all', methods=['GET'])
@login_required
def get_all_pipelines():
    """Get all job pipelines across all projects the user has access to."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute('''
        SELECT DISTINCT jp.id, jp.pipeline_id, jp.project_id, jp.name,
               jp.description, jp.status, jp.created_at, jp.updated_at,
               p.name as project_name
        FROM job_pipelines jp
        INNER JOIN projects p ON jp.project_id = p.id
        INNER JOIN project_assignments pa ON p.id = pa.project_id
        WHERE pa.user_id = %s
        ORDER BY jp.created_at DESC
    ''', (user_id,))

    pipelines = []
    for row in result.fetchall():
        pipelines.append({
            'id': row['id'],
            'pipeline_id': row['pipeline_id'],
            'project_id': row['project_id'],
            'project_name': row['project_name'],
            'name': row['name'],
            'description': row['description'],
            'status': row['status'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at']
        })

    return jsonify(pipelines)


@app.route('/api/templates/all', methods=['GET'])
@login_required
def get_all_templates():
    """Get all job templates the user has access to."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Get user's own templates, team templates, and public templates
    result = db.execute('''
        SELECT DISTINCT jt.id, jt.template_id, jt.name, jt.description,
               jt.model, jt.config, jt.is_public, jt.use_count,
               jt.project_id, jt.team_id, jt.created_at, jt.updated_at,
               p.name as project_name, t.name as team_name
        FROM job_templates jt
        LEFT JOIN projects p ON jt.project_id = p.id
        LEFT JOIN teams t ON jt.team_id = t.id
        WHERE jt.creator_id = %s
           OR jt.is_public = 1
           OR jt.team_id IN (SELECT team_id FROM team_members WHERE user_id = %s)
        ORDER BY jt.created_at DESC
    ''', (user_id, user_id))

    templates = []
    for row in result.fetchall():
        templates.append({
            'id': row['id'],
            'template_id': row['template_id'],
            'name': row['name'],
            'description': row['description'],
            'model': row['model'],
            'config': row['config'],
            'is_public': row['is_public'],
            'use_count': row['use_count'],
            'project_id': row['project_id'],
            'project_name': row['project_name'],
            'team_id': row['team_id'],
            'team_name': row['team_name'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at']
        })

    return jsonify(templates)


# =============================================================================
# PIPELINE ANALYTICS (SOTA Big Data Features)
# =============================================================================

@app.route('/api/analytics/pipeline', methods=['GET'])
@login_required
@admin_required
def get_pipeline_analytics():
    """Get real-time pipeline analytics (admin only).

    Returns comprehensive statistics about the job processing pipeline including:
    - Queue depths per model and priority tier
    - Active jobs count
    - Worker count and health
    - Cache hit rates
    - SLA compliance metrics
    - Throughput statistics
    """
    try:
        from production.mrs_service.runtime import (
            get_pipeline_stats,
            get_priority_queue_stats,
            get_model_latency_estimate,
            JobPriority,
            HOURLY_STATS_PREFIX,
        )

        # Get base pipeline stats
        stats = get_pipeline_stats()

        # Get detailed priority queue stats per model
        priority_stats = {}
        for model in Config.SUPPORTED_MODELS:
            model_id = model.get("id")
            model_stats = get_priority_queue_stats(model_id)
            priority_stats[model_id] = [
                {
                    "tier": s.tier.name,
                    "queue_depth": s.queue_depth,
                    "oldest_job_age_seconds": round(s.oldest_job_age_seconds, 1),
                    "sla_breaches": s.sla_breaches
                }
                for s in model_stats
            ]

        # Get model latency estimates
        latency_estimates = {}
        for model in Config.SUPPORTED_MODELS:
            model_id = model.get("id")
            latency_estimates[model_id] = round(get_model_latency_estimate(model_id) / 1000, 1)  # Convert to seconds

        # Get hourly throughput from Redis
        r = get_redis_connection()
        hour_key = time.strftime("%Y%m%d%H")
        hourly_stats_key = f"{HOURLY_STATS_PREFIX}{hour_key}"
        hourly_raw = r.hgetall(hourly_stats_key)

        hourly_throughput = {}
        for key, value in hourly_raw.items():
            if isinstance(key, bytes):
                key = key.decode('utf-8')
            if isinstance(value, bytes):
                value = value.decode('utf-8')
            hourly_throughput[key] = value

        # Calculate overall SLA compliance
        total_breaches = 0
        total_queues = 0
        for model_id, model_priority_stats in priority_stats.items():
            for ps in model_priority_stats:
                total_queues += 1
                total_breaches += ps["sla_breaches"]

        sla_compliance = (1 - total_breaches / max(total_queues, 1)) * 100

        return jsonify({
            "status": "success",
            "pipeline_stats": {
                "queue_depths": stats.get("queue_depths", {}),
                "active_jobs": stats.get("active_jobs", 0),
                "worker_count": stats.get("worker_count", 0),
                "cache_hit_rate": round(stats.get("cache_hit_rate", 0) * 100, 1),
            },
            "priority_queues": priority_stats,
            "latency_estimates_seconds": latency_estimates,
            "hourly_throughput": hourly_throughput,
            "sla_compliance_percent": round(sla_compliance, 1),
            "timestamp": stats.get("timestamp", time.time())
        })

    except Exception as e:
        logger.error(f"Pipeline analytics query failed: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": "Pipeline analytics not available",
            "error": str(e)
        }), 500


@app.route('/api/analytics/pipeline/history', methods=['GET'])
@login_required
@admin_required
def get_pipeline_history():
    """Get historical pipeline metrics (admin only).

    Query parameters:
    - hours: Number of hours to look back (default: 24, max: 168)
    """
    try:
        from production.mrs_service.runtime import HOURLY_STATS_PREFIX

        hours = min(request.args.get('hours', 24, type=int), 168)  # Max 7 days

        r = get_redis_connection()
        history = []

        # Get stats for each hour
        current_time = time.time()
        for i in range(hours):
            hour_offset = current_time - (i * 3600)
            hour_key = time.strftime("%Y%m%d%H", time.localtime(hour_offset))
            stats_key = f"{HOURLY_STATS_PREFIX}{hour_key}"

            hourly_raw = r.hgetall(stats_key)
            if hourly_raw:
                hourly_data = {"hour": hour_key}
                for key, value in hourly_raw.items():
                    if isinstance(key, bytes):
                        key = key.decode('utf-8')
                    if isinstance(value, bytes):
                        value = value.decode('utf-8')
                    try:
                        hourly_data[key] = float(value) if '.' in str(value) else int(value)
                    except (ValueError, TypeError):
                        hourly_data[key] = value
                history.append(hourly_data)

        return jsonify({
            "status": "success",
            "hours_requested": hours,
            "history": history
        })

    except Exception as e:
        logger.error(f"Pipeline history query failed: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": "Pipeline history not available",
            "error": str(e)
        }), 500


@app.route('/api/analytics/cache', methods=['GET'])
@login_required
@admin_required
def get_cache_analytics():
    """Get cache analytics (admin only).

    Returns information about the sequence deduplication cache.
    """
    try:
        from production.mrs_service.runtime import RESULT_CACHE_PREFIX, SEQUENCE_PENDING_HASH

        r = get_redis_connection()

        # Count cached results
        cache_keys = r.keys(f"{RESULT_CACHE_PREFIX}*")
        cached_results_count = len(cache_keys) if cache_keys else 0

        # Count pending computations
        pending_keys = r.keys(f"{SEQUENCE_PENDING_HASH}:*")
        pending_count = len(pending_keys) if pending_keys else 0

        # Sample some cache entries for size estimation
        total_size = 0
        sample_size = min(100, cached_results_count)
        if cache_keys and sample_size > 0:
            for key in list(cache_keys)[:sample_size]:
                value = r.get(key)
                if value:
                    total_size += len(value) if isinstance(value, (str, bytes)) else 0

        avg_entry_size = total_size / sample_size if sample_size > 0 else 0
        estimated_total_size = avg_entry_size * cached_results_count

        return jsonify({
            "status": "success",
            "cache_stats": {
                "cached_results_count": cached_results_count,
                "pending_computations": pending_count,
                "estimated_cache_size_bytes": int(estimated_total_size),
                "estimated_cache_size_mb": round(estimated_total_size / (1024 * 1024), 2),
                "avg_entry_size_bytes": int(avg_entry_size)
            }
        })

    except Exception as e:
        logger.error(f"Cache analytics query failed: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": "Cache analytics not available",
            "error": str(e)
        }), 500
