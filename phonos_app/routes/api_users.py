"""API routes for profiles, users, and related settings."""
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




@app.route('/api/profile/settings', methods=['GET', 'PUT'])
@login_required
@csrf_protect
def manage_profile_settings():
    """Get or update user's public profile settings"""
    from ..helpers.profile_settings import get_user_profile_settings, update_user_profile_settings

    user_id = session['user_id']

    if request.method == 'GET':
        settings = get_user_profile_settings(None, user_id)
        return jsonify({
            'success': True,
            'settings': settings
        })
    else:  # PUT
        data = request.json or {}
        success = update_user_profile_settings(None, user_id, data)
        return jsonify({
            'success': success,
            'message': 'Settings updated' if success else 'Failed to update settings'
        })


@app.route('/api/user/storage', methods=['GET'])
@login_required
def get_user_storage():
    """Get current user's storage information"""
    from ..helpers.storage import get_user_storage_state

    db = get_db()
    user_id = session['user_id']

    storage_state = get_user_storage_state(db, user_id)

    return jsonify({
        'success': True,
        'used': storage_state['used'],
        'quota': storage_state['quota'],
        'available': storage_state['available'],
        'overage': storage_state['overage']
    })


@app.route('/api/profile/<int:profile_user_id>', methods=['GET'])
def get_public_profile(profile_user_id):
    """Get a user's public profile (no login required for public viewing)"""
    from ..helpers.profile_settings import get_user_profile_settings

    conn = get_db()

    # Get user basic info
    user = conn.execute('''
        SELECT id, username, profile_picture_url, company, university, created_at
        FROM users WHERE id = %s
    ''', (profile_user_id,)).fetchone()

    if not user:
        return jsonify({
            'success': False,
            'message': 'User not found'
        }), 404

    # Get profile settings
    settings = get_user_profile_settings(None, profile_user_id)

    # Get public projects
    projects = conn.execute('''
        SELECT p.id, p.name, p.description, p.view_count, p.vote_score, p.created_at
        FROM projects p
        JOIN project_assignments pa ON p.id = pa.project_id
        WHERE pa.user_id = %s AND p.is_private = 0
        ORDER BY p.created_at DESC
        LIMIT 10
    ''', (profile_user_id,)).fetchall()

    # Get statistics
    stats = {
        'public_projects': len(projects),
        'total_views': sum(p['view_count'] for p in projects),
        'total_votes': sum(p['vote_score'] for p in projects)
    }

    return jsonify({
        'success': True,
        'user': dict(user),
        'settings': settings,
        'projects': [dict(p) for p in projects],
        'stats': stats
    })


@app.route('/api/projects/<int:project_id>/profile/settings', methods=['GET', 'PUT'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def manage_project_profile_settings(project_id):
    """Get or update project's public profile settings"""
    from ..helpers.profile_settings import get_project_profile_settings, update_project_profile_settings

    if request.method == 'GET':
        settings = get_project_profile_settings(None, project_id)
        return jsonify({
            'success': True,
            'settings': settings
        })
    else:  # PUT
        data = request.json or {}
        success = update_project_profile_settings(None, project_id, data)
        return jsonify({
            'success': success,
            'message': 'Settings updated' if success else 'Failed to update settings'
        })


@app.route('/api/projects/<int:project_id>/collaborators', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_VIEWER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_MANAGER})
def get_project_collaborators(project_id):
    """Get all collaborators for a project"""
    import logging

    logger = logging.getLogger(__name__)

    try:
        db = get_db()

        # Get all project members
        cursor = db.execute('''
            SELECT u.id, u.username, pm.role
            FROM project_members pm
            JOIN users u ON pm.user_id = u.id
            WHERE pm.project_id = %s
            ORDER BY pm.role DESC, u.username ASC
        ''', (project_id,))

        members = []
        for row in cursor.fetchall():
            members.append({
                'id': row['id'],
                'username': row['username'],
                'role': row['role']
            })

        return jsonify({
            'success': True,
            'collaborators': members
        })

    except Exception as e:
        logger.error(f"Error fetching collaborators for project {project_id}: {e}")
        return jsonify({
            'success': False,
            'message': 'Failed to fetch collaborators'
        }), 500


# Project Groups API
@app.route('/api/project-groups', methods=['GET', 'POST'])
@login_required
@csrf_protect
def handle_project_groups():
    """Get all project groups for the current user or create a new one."""
    db = get_db()
    user_id = session['user_id']

    if request.method == 'POST':
        data = request.json or {}
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        color = data.get('color', '#6366f1').strip()
        team_id = data.get('team_id')

        if not name:
            return jsonify({'status': 'error', 'message': 'Group name is required.'}), 400

        cursor = db.execute(
            'INSERT INTO project_groups (user_id, team_id, name, description, color) VALUES (%s, %s, %s, %s, %s)',
            (user_id, team_id, name, description, color)
        )
        db.commit()

        return jsonify({
            'status': 'success',
            'id': cursor.fetchone()['id'],
            'name': name,
            'description': description,
            'color': color
        }), 201

    # GET: Return all groups for user
    groups = db.execute(
        'SELECT id, name, description, color, created_at FROM project_groups WHERE user_id = %s ORDER BY name',
        (user_id,)
    ).fetchall()

    return jsonify([dict(g) for g in groups])


@app.route('/api/project-groups/<int:group_id>', methods=['PATCH', 'DELETE'])
@login_required
@csrf_protect
def handle_project_group(group_id):
    """Update or delete a project group."""
    db = get_db()
    user_id = session['user_id']

    group = db.execute('SELECT * FROM project_groups WHERE id = %s AND user_id = %s', (group_id, user_id)).fetchone()
    if not group:
        return jsonify({'status': 'error', 'message': 'Group not found.'}), 404

    if request.method == 'DELETE':
        # Remove group assignment from projects
        db.execute('UPDATE projects SET group_id = NULL WHERE group_id = %s', (group_id,))
        db.execute('DELETE FROM project_groups WHERE id = %s', (group_id,))
        db.commit()
        return jsonify({'status': 'success', 'message': 'Group deleted successfully.'})

    # PATCH: Update group
    data = request.json or {}
    name = data.get('name', group['name']).strip()
    description = data.get('description', group['description']).strip()
    color = data.get('color', group['color']).strip()

    if not name:
        return jsonify({'status': 'error', 'message': 'Group name is required.'}), 400

    db.execute(
        'UPDATE project_groups SET name = %s, description = %s, color = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
        (name, description, color, group_id)
    )
    db.commit()

    return jsonify({'status': 'success', 'message': 'Group updated successfully.'})


@app.route('/api/projects/<int:project_id>/group', methods=['PATCH'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def update_project_group(project_id):
    """Assign a project to a group."""
    db = get_db()
    data = request.json or {}
    group_id = data.get('group_id')

    if group_id is not None:
        # Verify group exists and belongs to user
        group = db.execute('SELECT id FROM project_groups WHERE id = %s AND user_id = %s',
                          (group_id, session['user_id'])).fetchone()
        if not group:
            return jsonify({'status': 'error', 'message': 'Invalid group ID.'}), 400

    db.execute('UPDATE projects SET group_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
               (group_id, project_id))
    db.commit()

    return jsonify({'status': 'success', 'message': 'Project group updated successfully.'})


# Privacy Settings API
@app.route('/api/privacy-settings', methods=['GET', 'POST'])
@login_required
@csrf_protect
def handle_privacy_settings():
    """Get or update user privacy settings."""
    db = get_db()
    user_id = session['user_id']

    if request.method == 'POST':
        data = request.json or {}

        # Check if settings exist
        existing = db.execute('SELECT id FROM user_privacy_settings WHERE user_id = %s', (user_id,)).fetchone()

        settings_fields = [
            'allow_invitations', 'allow_project_chat_messages', 'allow_direct_messages',
            'show_online_status', 'show_profile_picture', 'show_last_seen',
            'allow_team_discovery', 'allow_project_discovery', 'allow_email_notifications',
            'allow_collaboration_requests', 'profile_visibility', 'project_visibility_default',
            'data_sharing_analytics', 'data_sharing_third_party'
        ]

        if existing:
            # Update existing settings
            set_clauses = []
            params = []
            for field in settings_fields:
                if field in data:
                    set_clauses.append(f"{field} = ?")
                    params.append(data[field])

            if set_clauses:
                set_clauses.append("updated_at = CURRENT_TIMESTAMP")
                params.append(user_id)
                query = f"UPDATE user_privacy_settings SET {', '.join(set_clauses)} WHERE user_id = %s"
                db.execute(query, tuple(params))
        else:
            # Insert new settings with defaults
            columns = ['user_id'] + [f for f in settings_fields if f in data]
            placeholders = ['?'] * len(columns)
            values = [user_id] + [data[f] for f in settings_fields if f in data]

            query = f"INSERT INTO user_privacy_settings ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
            db.execute(query, tuple(values))

        db.commit()
        return jsonify({'status': 'success', 'message': 'Privacy settings updated successfully.'})

    # GET: Return current settings
    settings = db.execute('SELECT * FROM user_privacy_settings WHERE user_id = %s', (user_id,)).fetchone()

    if not settings:
        # Return defaults
        return jsonify({
            'allow_invitations': 1,
            'allow_project_chat_messages': 1,
            'allow_direct_messages': 1,
            'show_online_status': 1,
            'show_profile_picture': 1,
            'show_last_seen': 0,
            'allow_team_discovery': 1,
            'allow_project_discovery': 0,
            'allow_email_notifications': 1,
            'allow_collaboration_requests': 1,
            'profile_visibility': 'public',
            'project_visibility_default': 'private',
            'data_sharing_analytics': 1,
            'data_sharing_third_party': 0
        })

    return jsonify(dict(settings))


@app.route('/api/users/online', methods=['GET'])
@login_required
def get_online_users():
    """Get list of currently online users."""
    from ..socket_handlers import connected_users

    db = get_db()

    # Get unique user IDs from connected users
    online_user_ids = set()
    for sid, user_info in connected_users.items():
        user_id = user_info.get('user_id')
        if user_id:
            online_user_ids.add(user_id)

    # Filter based on privacy settings - only include users who allow showing online status
    filtered_user_ids = []
    for user_id in online_user_ids:
        # Check privacy settings
        privacy = db.execute(
            'SELECT show_online_status FROM user_privacy_settings WHERE user_id = %s',
            (user_id,)
        ).fetchone()

        # If no privacy settings exist, default is 1 (show online status)
        # If privacy settings exist and show_online_status is 1, include the user
        if privacy is None or privacy['show_online_status'] == 1:
            filtered_user_ids.append(user_id)

    return jsonify({
        'success': True,
        'online_users': filtered_user_ids
    })
