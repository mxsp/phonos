"""API routes for shared links and related functionality."""
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
    resolve_job_artifact_path,
    resolve_job_branch_path,
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
from ..helpers.paths import resolve_file_path_cached

# Import security decorators
from ..helpers.security import csrf_protect, rate_limit, sanitize_json_inputs, sanitize_html, sanitize_input, validate_sql_identifier
from ..helpers.file_scanner import scan_upload

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}


@app.route('/shared/<token>', methods=['GET'])
def access_shared_link(token):
    """Public route to access a shared resource via token."""
    db = get_db()

    # Look up the shared link
    link = db.execute('''
        SELECT sl.*, p.name as project_name, p.team_id
        FROM shared_links sl
        LEFT JOIN projects p ON sl.resource_type = 'project' AND sl.resource_id = p.id
        WHERE sl.token = %s AND sl.is_active = TRUE
    ''', (token,)).fetchone()

    if not link:
        return render_template('error.html',
                             error='Shared link not found or has been deactivated'), 404

    # Check if expired
    if link['expires_at']:
        expires_at = datetime.fromisoformat(link['expires_at']) if isinstance(link['expires_at'], str) else link['expires_at']
        if expires_at < datetime.now():
            return render_template('error.html',
                                 error='This shared link has expired'), 410

    # Update access count
    db.execute('UPDATE shared_links SET access_count = access_count + 1 WHERE token = %s', (token,))
    db.commit()

    # Handle different resource types
    resource_type = link['resource_type']
    resource_id = link['resource_id']

    if resource_type == 'project':
        # Redirect to public project view or render a public project page
        return render_template('shared/project.html',
                             project_id=resource_id,
                             project_name=link['project_name'],
                             token=token,
                             permissions=link['permissions'])

    elif resource_type == 'file':
        # Allow file download
        file_info = db.execute('SELECT * FROM files WHERE id = %s', (resource_id,)).fetchone()
        if not file_info:
            return render_template('error.html', error='File not found'), 404

        if link['permissions'] == 'download' or link['permissions'] == 'read_write':
            # Validate and resolve file path for security
            resolved_path = resolve_file_path_cached(file_info['path'])
            if not resolved_path or not os.path.exists(resolved_path):
                return render_template('error.html', error='File not found on disk'), 404

            # Ensure path is absolute and sanitized
            if not os.path.isabs(resolved_path):
                return render_template('error.html', error='Invalid file path'), 403

            # Get the directory and filename separately for send_from_directory (safer)
            file_dir = os.path.dirname(resolved_path)
            filename = os.path.basename(resolved_path)

            return send_from_directory(file_dir, filename,
                                      as_attachment=True,
                                      download_name=file_info['name'])
        else:
            return render_template('shared/file.html',
                                 file_info=dict(file_info),
                                 token=token,
                                 permissions=link['permissions'])

    elif resource_type == 'job':
        # Show job details
        job_info = db.execute('SELECT * FROM jobs WHERE id = %s', (resource_id,)).fetchone()
        if not job_info:
            return render_template('error.html', error='Job not found'), 404

        return render_template('shared/job.html',
                             job=dict(job_info),
                             token=token,
                             permissions=link['permissions'])

    return render_template('error.html', error='Invalid resource type'), 400


@app.route('/api/shared-links/all', methods=['GET'])
@login_required
def get_all_shared_links():
    """Get all shared links created by the authenticated user."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute('''
        SELECT id, token, resource_type, resource_id, permissions,
               expires_at, access_count, is_active, created_at
        FROM shared_links
        WHERE creator_id = %s
        ORDER BY created_at DESC
    ''', (user_id,))

    links = []
    for row in result.fetchall():
        # Determine if link is expired
        is_expired = False
        if row['expires_at']:
            expires_at = datetime.fromisoformat(row['expires_at']) if isinstance(row['expires_at'], str) else row['expires_at']
            is_expired = expires_at < datetime.now()

        # Build share URL
        share_url = f"{request.url_root}shared/{row['token']}"

        # Get resource name
        resource_name = row['resource_id']
        if row['resource_type'] == 'project':
            res = db.execute('SELECT name FROM projects WHERE id = %s', (row['resource_id'],))
            proj = res.fetchone()
            if proj:
                resource_name = proj['name']
        elif row['resource_type'] == 'file':
            res = db.execute('SELECT name FROM files WHERE id = %s', (row['resource_id'],))
            f = res.fetchone()
            if f:
                resource_name = f['name']
        elif row['resource_type'] == 'job':
            res = db.execute('SELECT job_id FROM jobs WHERE id = %s', (row['resource_id'],))
            j = res.fetchone()
            if j:
                resource_name = j['job_id']

        links.append({
            'link_id': row['token'],
            'id': row['id'],
            'token': row['token'],
            'resource_type': row['resource_type'],
            'resource_id': row['resource_id'],
            'resource_name': resource_name,
            'access_level': row['permissions'],
            'permissions': row['permissions'],
            'expires_at': row['expires_at'],
            'access_count': row['access_count'] or 0,
            'is_active': row['is_active'],
            'is_expired': is_expired,
            'created_at': row['created_at'],
            'last_accessed_at': None,
            'password': None,
            'description': None,
            'allow_downloads': True,
            'share_url': share_url
        })

    return jsonify({'status': 'success', 'links': links})


@app.route('/api/shared-links', methods=['POST'])
@login_required
@csrf_protect
def create_shared_link():
    """Create a new shared link."""
    user_id = session['user_id']
    data = request.get_json()

    resource_type = data.get('resource_type')
    resource_id = data.get('resource_id')
    access_level = data.get('access_level', 'read_only')
    expires_in_hours = data.get('expires_in_hours')
    password = data.get('password')
    allow_downloads = data.get('allow_downloads', True)
    description = data.get('description', '')

    if not resource_type or not resource_id:
        return jsonify({'status': 'error', 'message': 'Resource type and ID are required'}), 400

    # Generate token
    token = secrets.token_urlsafe(32)

    # Calculate expiration
    expires_at = None
    if expires_in_hours:
        expires_at = datetime.now() + timedelta(hours=int(expires_in_hours))

    # Hash password if provided
    password_hash = None
    if password:
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    db.execute('''
        INSERT INTO shared_links (creator_id, token, resource_type, resource_id,
                                   permissions, expires_at, password, allow_downloads, description,
                                   is_active, access_count, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, 0, CURRENT_TIMESTAMP)
    ''', (user_id, token, resource_type, resource_id, access_level, expires_at, password_hash,
          allow_downloads, description))

    db.commit()

    share_url = f"{request.url_root}shared/{token}"

    return jsonify({
        'status': 'success',
        'message': 'Share link created successfully',
        'link': {
            'link_id': token,
            'token': token,
            'share_url': share_url,
            'resource_type': resource_type,
            'resource_id': resource_id,
            'access_level': access_level,
            'expires_at': expires_at.isoformat() if expires_at else None,
            'password_protected': bool(password),
        }
    })


@app.route('/api/shared-links/<link_id>', methods=['DELETE'])
@login_required
@csrf_protect
def delete_shared_link(link_id):
    """Delete or revoke a shared link."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Check ownership
    result = db.execute('SELECT id FROM shared_links WHERE token = %s AND creator_id = %s', (link_id, user_id))
    if not result.fetchone():
        return jsonify({'status': 'error', 'message': 'Share link not found'}), 404

    # Mark as inactive
    db.execute('UPDATE shared_links SET is_active = 0 WHERE token = %s', (link_id,))
    db.commit()

    return jsonify({'status': 'success', 'message': 'Share link deleted successfully'})


@app.route('/api/shared-links/<link_id>/renew', methods=['POST'])
@login_required
@csrf_protect
def renew_shared_link(link_id):
    """Renew/extend a shared link's expiration."""
    user_id = session['user_id']
    data = request.get_json()
    try:
        extend_by_days = int(data.get('extend_by_days', 30))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'extend_by_days must be a number'}), 400
    if extend_by_days < 1 or extend_by_days > 365:
        return jsonify({'status': 'error', 'message': 'extend_by_days must be between 1 and 365'}), 400

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Check ownership
    result = db.execute('SELECT id, expires_at FROM shared_links WHERE token = %s AND creator_id = %s',
                   (link_id, user_id))
    link = result.fetchone()
    if not link:
        return jsonify({'status': 'error', 'message': 'Share link not found'}), 404

    # Calculate new expiration
    current_expires = datetime.fromisoformat(link['expires_at']) if link['expires_at'] else datetime.now()
    new_expires = max(current_expires, datetime.now()) + timedelta(days=extend_by_days)

    db.execute('UPDATE shared_links SET expires_at = %s, is_active = TRUE WHERE token = %s',
                   (new_expires, link_id))
    db.commit()

    return jsonify({
        'status': 'success',
        'message': 'Share link renewed successfully',
        'new_expires_at': new_expires.isoformat()
    })


@app.route('/api/shared-links/<link_id>/stats', methods=['GET'])
@login_required
def get_shared_link_stats(link_id):
    """Get statistics for a shared link."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Check ownership
    result = db.execute('''
        SELECT access_count, last_accessed_at, created_at
        FROM shared_links
        WHERE token = %s AND creator_id = %s
    ''', (link_id, user_id))

    link = result.fetchone()
    if not link:
        return jsonify({'status': 'error', 'message': 'Share link not found'}), 404

    return jsonify({
        'status': 'success',
        'stats': {
            'total_views': link['access_count'] or 0,
            'unique_visitors': link['access_count'] or 0,  # Could be enhanced with tracking
            'last_accessed': link['last_accessed_at']
        }
    })


@app.route('/api/files/all', methods=['GET'])
@login_required
def get_all_files():
    """Get all files across all projects the user has access to."""
    user_id = session['user_id']
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute('''
        SELECT DISTINCT f.id, f.project_id, f.name, f.path, f.size,
               f.notes, f.created_at, f.updated_at, p.name as project_name
        FROM files f
        INNER JOIN projects p ON f.project_id = p.id
        INNER JOIN project_assignments pa ON p.id = pa.project_id
        WHERE pa.user_id = %s
        ORDER BY f.created_at DESC
    ''', (user_id,))

    files = []
    for row in result.fetchall():
        files.append({
            'id': row['id'],
            'project_id': row['project_id'],
            'project_name': row['project_name'],
            'name': row['name'],
            'filename': row['name'],
            'path': row['path'],
            'size': row['size'],
            'notes': row['notes'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at']
        })

    return jsonify({'status': 'success', 'files': files})


@app.route('/api/jobs', methods=['GET'])
@app.route('/api/jobs/all', methods=['GET'])
@login_required
def get_all_jobs():
    """Get all jobs across all projects the user has access to."""
    user_id = None
    try:
        user_id = session['user_id']
        db = get_db()

        # Get jobs from projects user has access to AND jobs user created (including API jobs)
        # Include batch information for hierarchical display
        cursor = db.execute('''
            SELECT DISTINCT j.id, j.job_id, j.project_id, j.name, j.model,
                   LEFT(j.sequence, 64) AS sequence, LENGTH(j.sequence) AS sequence_length,
                   j.status, j.message, j.output_file, j.output_file_size,
                   j.output_display_name, j.execution_time, j.created_at,
                   j.updated_at, j.batch_id, p.name as project_name,
                   COALESCE(b.name, 'Unnamed Batch') as batch_name
            FROM jobs j
            LEFT JOIN projects p ON j.project_id = p.id
            LEFT JOIN project_assignments pa ON p.id = pa.project_id
            LEFT JOIN batches b ON j.batch_id = b.id
            WHERE (pa.user_id = %s OR j.creator_id = %s)
              AND j.pipeline_id IS NULL
            ORDER BY j.batch_id NULLS LAST, j.created_at DESC
        ''', (user_id, user_id))

        jobs = []
        for row in cursor.fetchall():
            model_type = row['model']
            model_info = Config.get_model_definition(model_type)
            model_name = model_info.get('name', model_type) if model_info else model_type

            jobs.append({
                'id': row['id'],
                'job_id': row['job_id'],
                'project_id': row['project_id'],
                'project_name': row['project_name'],
                'name': row['name'],
                'model': model_type,
                'model_name': model_name,
                # Trimmed preview only on this heavily-polled list endpoint; the
                # full sequence is served by the per-job detail endpoint.
                'sequence': row['sequence'],
                'sequence_length': row['sequence_length'],
                'status': row['status'],
                'message': row['message'],
                'output_file': row['output_file'],
                'output_file_size': row['output_file_size'],
                'output_display_name': row['output_display_name'],
                'execution_time': row['execution_time'],
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
                'batch_id': row['batch_id'],
                'batch_name': row['batch_name']
            })

        return jsonify(jobs)
    except Exception as e:
        user_id_str = user_id if user_id else 'unknown'
        logger.error(f"Error fetching all jobs for user {user_id_str}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch jobs.'}), 500


@app.route('/api/jobs/api-stats', methods=['GET'])
@login_required
def get_api_jobs_stats():
    """Get statistics for API-submitted jobs (jobs in the auto-generated 'API Requests' project)."""
    try:
        user_id = session['user_id']
        db = get_db()

        # Get jobs where the project is the auto-generated "API Requests" project
        # This project is created automatically when jobs are submitted via External API without a project_id
        cursor = db.execute('''
            SELECT
                COUNT(*) as total_jobs,
                SUM(CASE WHEN j.status IN ('queued', 'running', 'processing', 'submitted') THEN 1 ELSE 0 END) as running_jobs,
                SUM(CASE WHEN j.status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
                SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,
                MAX(j.created_at) as last_activity
            FROM jobs j
            INNER JOIN projects p ON j.project_id = p.id
            WHERE j.creator_id = %s AND p.name = 'API Requests' AND p.creator_id = %s
        ''', (user_id, user_id))

        row = cursor.fetchone()

        return jsonify({
            'status': 'success',
            'api_jobs': {
                'total': row['total_jobs'] or 0,
                'running': row['running_jobs'] or 0,
                'completed': row['completed_jobs'] or 0,
                'failed': row['failed_jobs'] or 0,
                'files': 0,  # API jobs don't have direct file associations
                'last_activity': row['last_activity'].isoformat() if row['last_activity'] else None
            }
        })
    except Exception as e:
        logger.error(f"Error fetching API jobs stats for user {user_id}: {e}", exc_info=True)
        return jsonify({'status': 'success', 'api_jobs': {'total': 0, 'running': 0, 'completed': 0, 'failed': 0, 'files': 0}})


@app.route('/api/jobs/api-list', methods=['GET'])
@login_required
def get_api_jobs_list():
    """Get list of API-submitted jobs (jobs in the auto-generated 'API Requests' project)."""
    try:
        user_id = session['user_id']
        db = get_db()

        # Get jobs from the auto-generated "API Requests" project
        # This project is created automatically when jobs are submitted via External API without a project_id
        cursor = db.execute('''
            SELECT j.id, j.job_id, j.project_id, j.name, j.model, j.sequence,
                   j.status, j.message, j.output_file, j.output_file_size,
                   j.output_display_name, j.execution_time, j.created_at,
                   j.updated_at, p.name as project_name
            FROM jobs j
            INNER JOIN projects p ON j.project_id = p.id
            WHERE j.creator_id = %s AND p.name = 'API Requests' AND p.creator_id = %s
            ORDER BY j.created_at DESC
        ''', (user_id, user_id))

        jobs = []
        for row in cursor.fetchall():
            model_type = row['model']
            model_info = Config.get_model_definition(model_type)
            model_name = model_info.get('name', model_type) if model_info else model_type

            jobs.append({
                'id': row['id'],
                'job_id': row['job_id'],
                'project_id': row['project_id'],
                'project_name': row['project_name'],
                'name': row['name'],
                'model': model_type,
                'model_name': model_name,
                'sequence': row['sequence'],
                'status': row['status'],
                'message': row['message'],
                'output_file': row['output_file'],
                'output_file_size': row['output_file_size'],
                'output_display_name': row['output_display_name'],
                'execution_time': row['execution_time'],
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
                'is_api_job': True
            })

        return jsonify(jobs)
    except Exception as e:
        logger.error(f"Error fetching API jobs list for user {user_id}: {e}", exc_info=True)
        return jsonify([])


@app.route('/api/external/stats', methods=['GET'])
@login_required
def get_external_api_stats():
    """Get comprehensive stats for External API dashboard."""
    try:
        user_id = session['user_id']
        db = get_db()

        # Get the API Requests project for this user
        project = db.execute('''
            SELECT id FROM projects
            WHERE creator_id = %s AND name = 'API Requests'
        ''', (user_id,)).fetchone()

        if not project:
            return jsonify({
                'status': 'success',
                'stats': {
                    'total_jobs': 0,
                    'running_jobs': 0,
                    'completed_jobs': 0,
                    'failed_jobs': 0,
                    'total_files': 0,
                    'total_output_size': 0,
                    'last_activity': None,
                    'jobs_today': 0,
                    'jobs_this_week': 0
                }
            })

        project_id = project['id']

        # Get job statistics
        job_stats = db.execute('''
            SELECT
                COUNT(*) as total_jobs,
                SUM(CASE WHEN status IN ('queued', 'running', 'processing', 'submitted') THEN 1 ELSE 0 END) as running_jobs,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,
                MAX(created_at) as last_activity,
                SUM(CASE WHEN created_at >= CURRENT_DATE THEN 1 ELSE 0 END) as jobs_today,
                SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '7 days' THEN 1 ELSE 0 END) as jobs_this_week,
                SUM(COALESCE(output_file_size, 0)) as total_output_size
            FROM jobs
            WHERE project_id = %s
        ''', (project_id,)).fetchone()

        # Get file count from the API Requests project
        file_stats = db.execute('''
            SELECT COUNT(*) as total_files
            FROM files
            WHERE project_id = %s
        ''', (project_id,)).fetchone()

        return jsonify({
            'status': 'success',
            'stats': {
                'total_jobs': job_stats['total_jobs'] or 0,
                'running_jobs': job_stats['running_jobs'] or 0,
                'completed_jobs': job_stats['completed_jobs'] or 0,
                'failed_jobs': job_stats['failed_jobs'] or 0,
                'total_files': file_stats['total_files'] or 0,
                'total_output_size': job_stats['total_output_size'] or 0,
                'last_activity': job_stats['last_activity'].isoformat() if job_stats['last_activity'] else None,
                'jobs_today': job_stats['jobs_today'] or 0,
                'jobs_this_week': job_stats['jobs_this_week'] or 0
            }
        })
    except Exception as e:
        logger.error(f"Error fetching external API stats: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch stats'}), 500


@app.route('/api/external/files', methods=['GET'])
@login_required
def get_external_api_files():
    """Get files from the API Requests project."""
    try:
        user_id = session['user_id']
        db = get_db()

        # Get the API Requests project for this user
        project = db.execute('''
            SELECT id FROM projects
            WHERE creator_id = %s AND name = 'API Requests'
        ''', (user_id,)).fetchone()

        if not project:
            return jsonify({'status': 'success', 'files': []})

        project_id = project['id']

        # Get files
        cursor = db.execute('''
            SELECT f.id, f.name, f.path, f.size, f.created_at, f.updated_at,
                   u.username as creator_name
            FROM files f
            LEFT JOIN users u ON f.creator_id = u.id
            WHERE f.project_id = %s
            ORDER BY f.created_at DESC
        ''', (project_id,))

        files = []
        for row in cursor.fetchall():
            # Extract file type from name
            name = row['name'] or ''
            file_type = name.split('.')[-1].upper() if '.' in name else 'FILE'

            files.append({
                'id': row['id'],
                'name': name,
                'file_type': file_type,
                'file_size': row['size'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None,
                'uploader_name': row['creator_name']
            })

        return jsonify({'status': 'success', 'files': files})
    except Exception as e:
        logger.error(f"Error fetching external API files: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch files'}), 500


@app.route('/api/external/activity', methods=['GET'])
@login_required
def get_external_api_activity():
    """Get recent activity for External API dashboard."""
    try:
        user_id = session['user_id']
        db = get_db()
        limit = request.args.get('limit', 20, type=int)

        # Get the API Requests project for this user
        project = db.execute('''
            SELECT id FROM projects
            WHERE creator_id = %s AND name = 'API Requests'
        ''', (user_id,)).fetchone()

        if not project:
            return jsonify({'status': 'success', 'activity': []})

        project_id = project['id']

        # Get recent job activity
        cursor = db.execute('''
            SELECT j.id, j.job_id, j.name, j.model, j.status, j.created_at, j.updated_at,
                   CASE
                       WHEN j.status = 'completed' THEN 'Job completed'
                       WHEN j.status = 'failed' THEN 'Job failed'
                       WHEN j.status IN ('queued', 'submitted') THEN 'Job submitted'
                       WHEN j.status IN ('running', 'processing') THEN 'Job running'
                       ELSE 'Job updated'
                   END as activity_type
            FROM jobs j
            WHERE j.project_id = %s
            ORDER BY j.updated_at DESC
            LIMIT %s
        ''', (project_id, limit))

        activity = []
        for row in cursor.fetchall():
            activity.append({
                'id': row['id'],
                'job_id': row['job_id'],
                'name': row['name'],
                'model': row['model'],
                'status': row['status'],
                'activity_type': row['activity_type'],
                'timestamp': row['updated_at'].isoformat() if row['updated_at'] else row['created_at'].isoformat()
            })

        return jsonify({'status': 'success', 'activity': activity})
    except Exception as e:
        logger.error(f"Error fetching external API activity: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch activity'}), 500


@app.route('/api/projects/<int:project_id>/api-jobs', methods=['GET'])
@login_required
def get_project_api_jobs(project_id):
    """Get jobs submitted via API for a specific project."""
    try:
        user_id = session['user_id']
        db = get_db()

        # Verify user has access to this project
        access = db.execute('''
            SELECT role FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        ''', (project_id, user_id)).fetchone()

        if not access:
            return jsonify({'status': 'error', 'message': 'Project not found or access denied'}), 404

        # Get jobs submitted via API (source = 'api')
        # For now, we identify API jobs by checking if they were created without a session
        # In the future, we'll use the source column
        cursor = db.execute('''
            SELECT j.id, j.job_id, j.name, j.model, j.sequence, j.status, j.message,
                   j.output_file, j.output_file_size, j.execution_time,
                   j.created_at, j.updated_at,
                   COALESCE(j.source, 'web') as source
            FROM jobs j
            WHERE j.project_id = %s AND COALESCE(j.source, 'web') = 'api'
            ORDER BY j.created_at DESC
        ''', (project_id,))

        jobs = []
        for row in cursor.fetchall():
            model_type = row['model']
            model_info = Config.get_model_definition(model_type)
            model_name = model_info.get('name', model_type) if model_info else model_type

            jobs.append({
                'id': row['id'],
                'job_id': row['job_id'],
                'name': row['name'],
                'model': model_type,
                'model_name': model_name,
                'status': row['status'],
                'message': row['message'],
                'output_file': row['output_file'],
                'execution_time': row['execution_time'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None,
                'source': row['source']
            })

        return jsonify({'status': 'success', 'jobs': jobs})
    except Exception as e:
        logger.error(f"Error fetching project API jobs: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch API jobs'}), 500


@app.route('/api/jobs/<string:job_id>', methods=['GET'])
@login_required
def get_job_details(job_id):
    """Get detailed information about a specific job."""
    try:
        user_id = session['user_id']
        db = get_db()

        # Get job first (without access check in query)
        row = db.execute('''
            SELECT j.id, j.job_id, j.project_id, j.name, j.model, j.sequence,
                   j.status, j.message, j.output_file, j.output_file_size,
                   j.output_display_name, j.execution_time, j.created_at,
                   j.updated_at, j.policy, j.creator_id, j.input_file_id,
                   p.name as project_name, u.username as creator_username
            FROM jobs j
            INNER JOIN projects p ON j.project_id = p.id
            LEFT JOIN users u ON j.creator_id = u.id
            WHERE j.job_id = %s
        ''', (job_id,)).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': 'Job not found'}), 404

        # Check access: user must be the job creator OR have project assignment
        # This allows access to API-submitted jobs where user might not have formal project assignment
        is_creator = row['creator_id'] == user_id

        from ..helpers.projects import get_project_assignment
        role = get_project_assignment(db, user_id, row['project_id'])

        if not role and not is_creator:
            return jsonify({'status': 'error', 'message': 'Access denied'}), 403

        model_type = row['model']
        model_info = Config.get_model_definition(model_type)
        model_name = model_info.get('name', model_type) if model_info else model_type

        # Parse policy metadata and convert artifacts to artifacts_info if needed
        metadata = {}
        if row['policy']:
            try:
                metadata = json.loads(row['policy'])
            except (TypeError, json.JSONDecodeError):
                metadata = {}

        # Convert artifacts to artifacts_info if not already present (for backward compatibility)
        if not metadata.get('artifacts_info') and metadata.get('artifacts'):
            project = db.execute('SELECT team_id FROM projects WHERE id = %s', (row['project_id'],)).fetchone()
            if project:
                project_path = get_safe_project_path(row['project_id'], project['team_id'])
                if project_path:
                    job_output_dir = resolve_job_branch_path(
                        project_path=project_path,
                        job_id=job_id,
                        output_file=row.get('output_file'),
                        policy=metadata,
                        branch='main',
                        create=False,
                    )
                    artifacts_info = []
                    for artifact in metadata['artifacts']:
                        artifact_file = artifact.get('filename')
                        if artifact_file:
                            artifact_path = resolve_job_artifact_path(
                                project_path=project_path,
                                job_id=job_id,
                                artifact_name=artifact_file,
                                output_file=row.get('output_file'),
                                policy=metadata,
                                preferred_branch=artifact.get('branch') or 'main',
                            )
                            if not artifact_path and job_output_dir:
                                artifact_path = os.path.join(job_output_dir, artifact_file)
                            artifact_size = os.path.getsize(artifact_path) if os.path.exists(artifact_path) else 0
                            artifacts_info.append({
                                'name': artifact.get('name'),
                                'filename': artifact_file,
                                'display_name': artifact.get('display_name'),
                                'size': artifact_size,
                                'confidence': artifact.get('confidence'),
                                'path': artifact_path,
                                'branch': artifact.get('branch'),
                                'relative_path': artifact.get('relative_path'),
                            })
                    if artifacts_info:
                        metadata['artifacts_info'] = artifacts_info

        job_data = {
            'id': row['id'],
            'job_id': row['job_id'],
            'project_id': row['project_id'],
            'project_name': row['project_name'],
            'name': row['name'],
            'model': model_type,
            'model_name': model_name,
            'sequence': row['sequence'],
            'status': row['status'],
            'message': row['message'],
            'output_file': row['output_file'],
            'output_file_size': row['output_file_size'],
            'output_display_name': row['output_display_name'],
            'execution_time': row['execution_time'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'policy': metadata,  # Return parsed and enriched metadata object
            'artifacts_info': metadata.get('artifacts_info', []),  # Top-level for dashboard visualizer
            'creator_id': row['creator_id'],
            'creator_username': row['creator_username'],
            'input_file_id': row['input_file_id']
        }

        return jsonify(job_data)
    except Exception as e:
        logger.error(f"Error fetching job details for {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch job details.'}), 500
