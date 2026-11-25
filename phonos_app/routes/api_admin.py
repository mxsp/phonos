"""Admin API routes for workers, dashboard stats, and user management."""
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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
from .api import _delete_project_logic
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
    get_job_status_counts, # NEW: Import get_job_status_counts
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
from ..helpers.worker_runtime import collect_model_runtime_status

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}


@app.route('/api/workers', methods=['GET'])
@login_required
@api_cache(ttl=30)
def get_worker_status():
    """Return dynamic worker uptime/usage status for each model."""
    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        if row is None:
            return default
        try:
            return row.get(key, default)
        except AttributeError:
            try:
                return row[key]
            except Exception:
                return default

    def _normalize_timestamp(value: Any) -> Optional[datetime]:
        if not value:
            return None
        parsed = parse_sqlite_timestamp(value)
        if not parsed:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def _collect_processing_job_metrics(db, stuck_threshold_seconds: int) -> dict[str, dict[str, float | int]]:
        now_utc = datetime.utcnow()
        metrics_by_model: dict[str, dict[str, float | int]] = {}

        try:
            processing_rows = db.execute(
                """
                SELECT
                    j.job_id,
                    j.model,
                    j.created_at,
                    j.updated_at,
                    (
                        SELECT MAX(jl.timestamp)
                        FROM job_logs jl
                        WHERE jl.job_id = j.job_id AND jl.status = 'processing'
                    ) AS processing_started_at
                FROM jobs j
                WHERE j.status = 'processing'
                """
            ).fetchall()
        except Exception as exc:
            logger.warning("Failed to fetch processing job metrics: %s", exc)
            return metrics_by_model

        for row in processing_rows:
            model_id = str(_row_get(row, "model") or "").strip()
            if not model_id:
                continue

            started_at = (
                _normalize_timestamp(_row_get(row, "processing_started_at"))
                or _normalize_timestamp(_row_get(row, "updated_at"))
                or _normalize_timestamp(_row_get(row, "created_at"))
            )

            age_seconds = 0.0
            if started_at:
                age_seconds = max(0.0, (now_utc - started_at).total_seconds())

            model_metrics = metrics_by_model.setdefault(
                model_id,
                {
                    "processing_jobs": 0,
                    "stuck_processing_jobs": 0,
                    "oldest_processing_age_seconds": 0.0,
                    "oldest_stuck_age_seconds": 0.0,
                },
            )

            model_metrics["processing_jobs"] = int(model_metrics["processing_jobs"]) + 1
            model_metrics["oldest_processing_age_seconds"] = max(
                float(model_metrics["oldest_processing_age_seconds"]),
                age_seconds,
            )

            if age_seconds >= float(stuck_threshold_seconds):
                model_metrics["stuck_processing_jobs"] = int(model_metrics["stuck_processing_jobs"]) + 1
                model_metrics["oldest_stuck_age_seconds"] = max(
                    float(model_metrics["oldest_stuck_age_seconds"]),
                    age_seconds,
                )

        return metrics_by_model

    runtime = collect_model_runtime_status(Config.SUPPORTED_MODELS)
    model_names = {m.get('id'): m.get('name', m.get('id', '')) for m in Config.SUPPORTED_MODELS}
    db = get_db()
    stuck_threshold_seconds = int(getattr(Config, "MODEL_STATUS_STUCK_THRESHOLD_SECONDS", 3600) or 3600)
    processing_metrics = _collect_processing_job_metrics(db, stuck_threshold_seconds)

    workers_payload = []
    for model in Config.SUPPORTED_MODELS:
        model_id = model.get('id')
        if not model_id:
            continue
        entry = runtime.get(model_id, {})
        processing_entry = processing_metrics.get(model_id, {})
        count = int(entry.get('worker_count', 0))
        status = entry.get('status', 'offline')
        status_override = model.get('status_override')
        if status_override:
            status = status_override
        processing_jobs = int(processing_entry.get('processing_jobs', 0))
        stuck_processing_jobs = int(processing_entry.get('stuck_processing_jobs', 0))
        workers_payload.append(
            {
                'model_id': model_id,
                'model_name': model_names.get(model_id, str(model_id).upper()),
                'count': count,
                'status': status_override or ('online' if count > 0 else 'offline'),
                'runtime_status': status,
                'active_jobs': int(entry.get('active_jobs', 0)),
                'queue_depth': int(entry.get('queue_depth', 0)),
                'uptime_seconds_avg': float(entry.get('avg_uptime_seconds', 0.0)),
                'uptime_seconds_max': float(entry.get('uptime_seconds_max', 0.0)),
                'usage_ratio': entry.get('usage_ratio'),
                'processing_jobs': processing_jobs,
                'stuck_processing_jobs': stuck_processing_jobs,
                'oldest_processing_age_seconds': float(processing_entry.get('oldest_processing_age_seconds', 0.0)),
                'oldest_stuck_age_seconds': float(processing_entry.get('oldest_stuck_age_seconds', 0.0)),
                'stuck_threshold_seconds': stuck_threshold_seconds,
                'has_stuck_processing_jobs': bool(stuck_processing_jobs > 0),
            }
        )

    return jsonify(workers_payload)

@app.route('/api/dashboard_stats', methods=['GET'])
@login_required
@api_cache(ttl=30, make_key=lambda: f"dashboard:{session.get('user_id', 'anon')}")
def get_dashboard_stats():
    db = get_db()
    user_id = session['user_id']

    # Get all project IDs the user is assigned to
    assigned_projects_q = db.execute('SELECT project_id FROM project_assignments WHERE user_id = %s', (user_id,)).fetchall()
    project_ids = [row['project_id'] for row in assigned_projects_q]

    # Get job status counts (newly added)
    job_status_counts = get_job_status_counts(db)
    processing_jobs = job_status_counts.get('processing', 0)
    running_jobs = job_status_counts.get('running', 0)
    queued_jobs = job_status_counts.get('queued', 0)
    submitted_jobs = job_status_counts.get('submitted', 0)
    total_active_jobs_system_wide = processing_jobs + running_jobs + queued_jobs + submitted_jobs

    if not project_ids:
        # Even if no projects, still get user's activity log
        recent_activities = []
        try:
            activities_q = db.execute('''
                SELECT activity_type, activity_description, created_at, resource_type, resource_id
                FROM activity_log
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 5
            ''', (user_id,)).fetchall()
            recent_activities = [dict(row) for row in activities_q]
        except Exception:
            pass

        # IoT summary even when no projects
        iot_summary_early = {'total_devices': 0, 'online_devices': 0, 'device_types': 0}
        try:
            iot_t = db.execute('SELECT COUNT(*) as count FROM iot_devices WHERE user_id = %s', (user_id,)).fetchone()
            iot_o = db.execute("SELECT COUNT(*) as count FROM iot_devices WHERE user_id = %s AND status = 'online'", (user_id,)).fetchone()
            iot_dt = db.execute('SELECT COUNT(DISTINCT device_type) as count FROM iot_devices WHERE user_id = %s', (user_id,)).fetchone()
            iot_summary_early = {
                'total_devices': iot_t['count'] if iot_t else 0,
                'online_devices': iot_o['count'] if iot_o else 0,
                'device_types': iot_dt['count'] if iot_dt else 0
            }
        except Exception:
            pass

        # Get user's credit and storage info
        user_info = db.execute('SELECT credits, storage_used, storage_quota, plan FROM users WHERE id = %s', (user_id,)).fetchone()
        credits_remaining = user_info['credits'] if user_info else 0
        plan_name = user_info['plan'] if user_info else DEFAULT_PLAN
        plan_def = get_plan_definition(plan_name)
        credits_total = plan_def.monthly_credits if plan_def else 1000
        total_credits_used = max(0, credits_total - credits_remaining)
        storage_used = user_info['storage_used'] if user_info else 0
        storage_quota = user_info['storage_quota'] if user_info else 0
        storage_used_gb = round(storage_used / (1024 * 1024 * 1024), 2)
        storage_quota_gb = round(storage_quota / (1024 * 1024 * 1024), 2)

        return jsonify({
            'total_projects': 0,
            'total_jobs': 0,
            'running_jobs': 0,
            'total_credits_used': total_credits_used,
            'credits_remaining': credits_remaining,
            'credits_total': credits_total,
            'storage_used_gb': storage_used_gb,
            'storage_quota_gb': storage_quota_gb,
            'model_usage': [],
            'jobs_per_project': [],
            'recent_jobs': [],
            'recent_activities': recent_activities,
            'iot_summary': iot_summary_early,
            'system_wide_job_counts': {
                'processing': processing_jobs,
                'running': running_jobs,
                'queued': queued_jobs,
                'submitted': submitted_jobs,
                'total_active': total_active_jobs_system_wide
            }
        })

    placeholders = ','.join('%s' for _ in project_ids)

    # Get total projects count
    total_projects = len(project_ids)

    # Get total jobs count
    total_jobs_q = db.execute(f'SELECT COUNT(id) as count FROM jobs WHERE project_id IN ({placeholders})', tuple(project_ids)).fetchone()
    total_jobs = total_jobs_q['count'] if total_jobs_q else 0

    # Get running jobs count (pending, running, processing)
    running_jobs_q = db.execute(f'''
        SELECT COUNT(id) as count FROM jobs
        WHERE project_id IN ({placeholders})
        AND status IN ('pending', 'running', 'processing')
    ''', tuple(project_ids)).fetchone()
    running_jobs = running_jobs_q['count'] if running_jobs_q else 0

    # Aggregate stats from all accessible projects
    model_usage_q = db.execute(f'SELECT model, COUNT(id) as count FROM jobs WHERE project_id IN ({placeholders}) GROUP BY model', tuple(project_ids)).fetchall()
    model_usage = [dict(row) for row in model_usage_q]

    jobs_per_project_q = db.execute(f'''
        SELECT p.name, COUNT(j.id) as count
        FROM projects p
        LEFT JOIN jobs j ON p.id = j.project_id
        WHERE p.id IN ({placeholders})
        GROUP BY p.id, p.name
        ORDER BY count DESC
        LIMIT 7
    ''', tuple(project_ids)).fetchall()
    jobs_per_project = [dict(row) for row in jobs_per_project_q]

    recent_jobs_q = db.execute(f'''
        SELECT j.job_id, j.model as model_name, j.status, j.created_at, j.name, p.name as project_name
        FROM jobs j
        JOIN projects p ON j.project_id = p.id
        WHERE j.project_id IN ({placeholders})
        ORDER BY j.created_at DESC
        LIMIT 5
    ''', tuple(project_ids)).fetchall()
    recent_jobs = [dict(row) for row in recent_jobs_q]

    # Get recent activities for this user
    recent_activities = []
    try:
        activities_q = db.execute('''
            SELECT activity_type, activity_description, created_at, resource_type, resource_id
            FROM activity_log
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 10
        ''', (user_id,)).fetchall()
        recent_activities = [dict(row) for row in activities_q]
    except Exception:
        pass

    # Get IoT device summary for dashboard widget
    iot_summary = {'total_devices': 0, 'online_devices': 0, 'device_types': 0}
    try:
        iot_total = db.execute(
            'SELECT COUNT(*) as count FROM iot_devices WHERE user_id = %s', (user_id,)
        ).fetchone()
        iot_online = db.execute(
            "SELECT COUNT(*) as count FROM iot_devices WHERE user_id = %s AND status = 'online'", (user_id,)
        ).fetchone()
        iot_types = db.execute(
            'SELECT COUNT(DISTINCT device_type) as count FROM iot_devices WHERE user_id = %s', (user_id,)
        ).fetchone()
        iot_summary = {
            'total_devices': iot_total['count'] if iot_total else 0,
            'online_devices': iot_online['count'] if iot_online else 0,
            'device_types': iot_types['count'] if iot_types else 0
        }
    except Exception:
        pass  # IoT table may not exist for all users

    # Get user's credit and storage info
    user_info = db.execute('SELECT credits, storage_used, storage_quota, plan FROM users WHERE id = %s', (user_id,)).fetchone()
    credits_remaining = user_info['credits'] if user_info else 0
    plan_name = user_info['plan'] if user_info else DEFAULT_PLAN
    plan_def = get_plan_definition(plan_name)
    credits_total = plan_def.monthly_credits if plan_def else 1000
    total_credits_used = max(0, credits_total - credits_remaining)
    storage_used = user_info['storage_used'] if user_info else 0
    storage_quota = user_info['storage_quota'] if user_info else 0
    storage_used_gb = round(storage_used / (1024 * 1024 * 1024), 2)
    storage_quota_gb = round(storage_quota / (1024 * 1024 * 1024), 2)

    return jsonify({
        'total_projects': total_projects,
        'total_jobs': total_jobs,
        'running_jobs': running_jobs,
        'total_credits_used': total_credits_used,
        'credits_remaining': credits_remaining,
        'credits_total': credits_total,
        'storage_used_gb': storage_used_gb,
        'storage_quota_gb': storage_quota_gb,
        'model_usage': model_usage,
        'jobs_per_project': jobs_per_project,
        'recent_jobs': recent_jobs,
        'recent_activities': recent_activities,
        'iot_summary': iot_summary,
        'system_wide_job_counts': {
            'processing': processing_jobs,
            'running': running_jobs,
            'queued': queued_jobs,
            'submitted': submitted_jobs,
            'total_active': total_active_jobs_system_wide
        }
    })

@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def get_admin_stats():
    """Fetches system-wide statistics for the admin dashboard."""
    db = get_db()
    r = get_redis_connection()

    # --- Database Stats ---
    total_users = db.execute('SELECT COUNT(id) as count FROM users').fetchone()['count']
    total_projects = db.execute('SELECT COUNT(id) as count FROM projects').fetchone()['count']
    total_jobs = db.execute('SELECT COUNT(id) as count FROM jobs').fetchone()['count']
    total_files = db.execute('SELECT COUNT(id) as count FROM files').fetchone()['count']
    total_storage_used = db.execute('SELECT SUM(storage_used) as total FROM users').fetchone()['total'] or 0
    job_status_counts = db.execute('SELECT status, COUNT(id) as count FROM jobs GROUP BY status').fetchall()
    jobs_by_status = {row['status']: row['count'] for row in job_status_counts}

    # --- User Data for Management ---
    users_q = db.execute('''
        SELECT u.id, u.username, uc.email, u.plan, u.credits, u.storage_used
        FROM users u
        JOIN user_credentials uc ON u.user_credential_id = uc.id
        ORDER BY u.id
    ''').fetchall()
    users = [dict(row) for row in users_q]


    # --- Redis Stats for System Health ---
    worker_stats = []
    health_issues = []
    active_workers_count = 0
    try:
        worker_health_data = r.hgetall("hash:worker_health")
        now = time.time()
        for worker_id_bytes, worker_info_json_bytes in worker_health_data.items():
            worker_id = worker_id_bytes.decode('utf-8')
            try:
                info = json.loads(worker_info_json_bytes.decode('utf-8'))
                last_seen_ago = now - info.get('timestamp', now)
                status = 'active'
                if last_seen_ago > 60: # 1 minute threshold for being disconnected
                    status = 'disconnected'
                    health_issues.append(f"Worker {info.get('type', 'Unknown')} ({worker_id[:8]}) has been disconnected for over a minute.")

                if status == 'active':
                    active_workers_count += 1

                worker_stats.append({
                    'worker_id': worker_id,
                    'worker_type': info.get('type', 'N/A'),
                    'status': status,
                    'jobs_completed': info.get('completed', 0),
                    'jobs_failed': info.get('failed', 0),
                    'last_seen': info.get('timestamp', 0)
                })
            except (json.JSONDecodeError, UnicodeDecodeError):
                health_issues.append(f"Could not parse health data for worker {worker_id[:8]}.")

        queue_status = {q: r.llen(f"queue:{q}") for q in ['new_jobs', 'completed_jobs']}

    except redis.RedisError as e:
        health_issues.append(f"Could not connect to Redis: {str(e)}")
        queue_status = {}
        active_workers_count = 'N/A'

    # --- System-level Stats ---
    total_jobs_processed = db.execute("SELECT COUNT(id) as count FROM jobs WHERE status IN ('completed', 'failed')").fetchone()['count']
    failed_jobs = jobs_by_status.get('failed', 0)
    error_rate = failed_jobs / total_jobs_processed if total_jobs_processed > 0 else 0
    system_uptime = (datetime.utcnow() - app.start_time).total_seconds()

    # Simplified throughput calculation
    uptime_minutes = system_uptime / 60
    throughput = total_jobs_processed / uptime_minutes if uptime_minutes > 0 else 0

    return jsonify({
        'database_stats': {
            'total_users': total_users,
            'total_projects': total_projects,
            'total_jobs': total_jobs,
            'jobs_by_status': jobs_by_status,
            'total_files': total_files,
            'total_storage_used_bytes': total_storage_used,
            'total_storage_used_formatted': format_storage_bytes(total_storage_used),
        },
        'system_stats': {
            'system_uptime': system_uptime,
            'jobs_queued': queue_status.get('new_jobs', 'N/A'),
            'jobs_processing': jobs_by_status.get('processing', 0),
            'active_workers': active_workers_count,
            'throughput': throughput,
            'error_rate': error_rate,
        },
        'worker_stats': sorted(worker_stats, key=lambda x: x['worker_type']),
        'queue_status': queue_status,
        'health_issues': health_issues,
        'users': users
    })

@app.route('/api/admin/user/<int:user_id>', methods=['PATCH'])
@admin_required
@csrf_protect
@rate_limit(limit=30, window=3600, action='admin_user_update')
def admin_update_user(user_id):
    """Updates a user's details from the admin panel."""
    data = request.json
    db = get_db()

    # Validate plan
    plan = data.get('plan')
    if not is_valid_plan(plan):
        return jsonify({'status': 'error', 'message': 'Invalid plan specified.'}), 400
    normalized_plan = normalize_plan(plan)

    # Validate credits and storage
    try:
        credits = int(data.get('credits'))
        storage_quota_gb = int(data.get('storage_quota'))
        storage_quota_bytes = storage_quota_gb * 1024 * 1024 * 1024
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': 'Credits and storage quota must be valid integers.'}), 400

    db.execute(
        'UPDATE users SET plan = %s, credits = %s, storage_quota = %s WHERE id = %s',
        (normalized_plan, credits, storage_quota_bytes, user_id)
    )
    db.commit()

    # It's good practice to emit an update to the user whose data was changed
    socketio.emit('user_info_update', room=f"user_{user_id}")

    return jsonify({'status': 'success', 'message': f'User {user_id} updated successfully.'})


@app.route('/api/admin/user/<int:user_id>', methods=['DELETE'])
@admin_required
@csrf_protect
@rate_limit(limit=10, window=3600, action='admin_user_delete')
def admin_delete_user(user_id):
    """Deletes a user and all their associated data from the system."""
    db = get_db()

    # Prevent admin from deleting their own account via this endpoint
    if user_id == session['user_id']:
        return jsonify({'status': 'error', 'message': 'You cannot delete your own account via the admin panel.'}), 400

    user_exists = db.execute('SELECT 1 FROM users WHERE id = %s', (user_id,)).fetchone()
    if not user_exists:
        return jsonify({'status': 'error', 'message': 'User not found.'}), 404

    db.execute('BEGIN')
    try:
        # 1. Delete all projects owned/managed by the user
        projects_to_delete = db.execute(
            'SELECT p.id, p.team_id FROM projects p JOIN project_assignments pa ON p.id = pa.project_id WHERE pa.user_id = %s AND pa.role = %s',
            (user_id, PROJECT_ROLE_MANAGER)
        ).fetchall()

        for project in projects_to_delete:
            # Check if there are other managers for the project
            other_managers = db.execute(
                "SELECT 1 FROM project_assignments WHERE project_id = %s AND role = %s AND user_id != %s",
                (project['id'], PROJECT_ROLE_MANAGER, user_id)
            ).fetchone()

            # Only delete the project if the user is the sole manager
            if not other_managers:
                _delete_project_logic(db, project['id'], project['team_id'])
            else:
                # If there are other managers, just remove the user's assignment
                db.execute('DELETE FROM project_assignments WHERE project_id = %s AND user_id = %s', (project['id'], user_id))


        # 2. Remove user from all team memberships
        db.execute('DELETE FROM team_members WHERE user_id = %s', (user_id,))

        # 3. Delete any pending team invitations sent by or to this user
        db.execute('DELETE FROM team_invitations WHERE inviter_id = %s OR invitee_email IN (SELECT email FROM user_credentials WHERE user_id = %s)', (user_id, user_id))

        # 4. Delete any pending project invitations sent by or to this user
        db.execute('DELETE FROM invitations WHERE inviter_id = %s OR invitee_email IN (SELECT email FROM user_credentials WHERE user_id = %s)', (user_id, user_id))

        # 5. Delete user's credentials (email, password hash)
        db.execute('DELETE FROM user_credentials WHERE user_id = %s', (user_id,))

        # 6. Delete user's notifications
        db.execute('DELETE FROM notifications WHERE user_id = %s', (user_id,))

        # 7. Finally, delete the user record itself
        db.execute('DELETE FROM users WHERE id = %s', (user_id,))

        db.commit()
        socketio.emit('user_info_update', room=f"user_{user_id}") # Notify the deleted user's client to log out
        return jsonify({'status': 'success', 'message': f'User {user_id} and all associated data deleted successfully.'}), 200
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting user {user_id} by admin: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while deleting the user.'}), 500


@app.route('/api/failed_jobs', methods=['GET'])
@login_required
def get_failed_jobs():
    """
    Returns a list of all failed jobs for projects the current user is assigned to.
    """
    db = get_db()
    user_id = session['user_id']

    assigned_projects_q = db.execute('SELECT project_id FROM project_assignments WHERE user_id = %s', (user_id,)).fetchall()
    project_ids = [row['project_id'] for row in assigned_projects_q]

    if not project_ids:
        return jsonify([])

    placeholders = ','.join('%s' for _ in project_ids)
    failed_jobs_q = db.execute(f'''
        SELECT j.job_id, j.model as model_name, j.name, j.status, j.created_at, p.name as project_name, j.message
        FROM jobs j JOIN projects p ON j.project_id = p.id
        WHERE j.project_id IN ({placeholders}) AND j.status = 'failed'
        ORDER BY j.created_at DESC
    ''', tuple(project_ids)).fetchall()

    failed_jobs = [dict(row) for row in failed_jobs_q]
    return jsonify(failed_jobs)


@app.route('/api/admin/warnings', methods=['GET'])
@admin_required
def get_admin_warnings():
    """Get all system warnings."""
    db = get_db()
    warnings = db.execute('SELECT * FROM system_warnings ORDER BY created_at DESC').fetchall()
    return jsonify([dict(w) for w in warnings])


@app.route('/api/admin/warnings', methods=['POST'])
@admin_required
@csrf_protect
def create_system_warning():
    """Create a new system warning."""
    data = request.json
    message = data.get('message')
    level = data.get('level', 'warning')
    is_active = int(data.get('is_active', 1))

    if not message:
        return jsonify({'status': 'error', 'message': 'Message is required'}), 400

    db = get_db()
    db.execute(
        'INSERT INTO system_warnings (message, level, is_active) VALUES (%s, %s, %s)',
        (message, level, is_active)
    )
    db.commit()
    return jsonify({'status': 'success', 'message': 'Warning created'})


@app.route('/api/admin/warnings/<int:warning_id>', methods=['PUT'])
@admin_required
@csrf_protect
def update_system_warning(warning_id):
    """Update a system warning."""
    data = request.json
    db = get_db()
    
    # Build update query dynamically based on provided fields
    fields = []
    values = []
    if 'message' in data:
        fields.append('message = %s')
        values.append(data['message'])
    if 'level' in data:
        fields.append('level = %s')
        values.append(data['level'])
    if 'is_active' in data:
        fields.append('is_active = %s')
        values.append(int(data['is_active']))
        
    if not fields:
        return jsonify({'status': 'error', 'message': 'No fields to update'}), 400
        
    values.append(warning_id)
    query = f"UPDATE system_warnings SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s"
    
    db.execute(query, tuple(values))
    db.commit()
    return jsonify({'status': 'success', 'message': 'Warning updated'})


@app.route('/api/admin/warnings/<int:warning_id>', methods=['DELETE'])
@admin_required
@csrf_protect
def delete_system_warning(warning_id):
    """Delete a system warning."""
    db = get_db()
    db.execute('DELETE FROM system_warnings WHERE id = %s', (warning_id,))
    db.commit()
    return jsonify({'status': 'success', 'message': 'Warning deleted'})


@app.route('/api/admin/model-overrides', methods=['GET'])
@admin_required
def get_model_overrides():
    """Get all model overrides."""
    db = get_db()
    overrides = fetch_model_overrides(db)
    return jsonify({'status': 'success', 'overrides': overrides})


@app.route('/api/admin/model-overrides/<model_id>', methods=['PUT'])
@admin_required
@csrf_protect
def update_model_override(model_id):
    """Update a model override."""
    data = request.get_json()
    is_active = data.get('is_active', True)
    is_visible = data.get('is_visible', True)

    db = get_db()
    db.execute(
        """
        INSERT INTO model_overrides (model_id, is_active, is_visible, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT(model_id) DO UPDATE SET
            is_active = excluded.is_active,
            is_visible = excluded.is_visible,
            updated_at = CURRENT_TIMESTAMP
        """,
        (model_id, int(bool(is_active)), int(bool(is_visible)))
    )
    db.commit()
    return jsonify({'status': 'success', 'message': f'Model override for {model_id} updated'})


@app.route('/api/admin/model-overrides/<model_id>', methods=['DELETE'])
@admin_required
@csrf_protect
def delete_model_override(model_id):
    """Delete a model override."""
    db = get_db()
    db.execute('DELETE FROM model_overrides WHERE model_id = %s', (model_id,))
    db.commit()
    return jsonify({'status': 'success', 'message': f'Model override for {model_id} deleted'})
