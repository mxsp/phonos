"""API routes for tag management."""
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


@app.route('/api/teams/<int:team_id>/tags', methods=['GET', 'POST'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN, TEAM_ROLE_MEMBER})
@csrf_protect
def handle_team_tags(team_id):
    db = get_db()
    if request.method == 'POST':
        data = request.json or {}
        name = sanitize_input((data.get('name') or '').strip(), max_length=100)
        if not name:
            return jsonify({'status': 'error', 'message': 'Tag name is required.'}), 400

        tag_id = tags_helper.create_tag(team_id, name)
        if not tag_id:
            return jsonify({'status': 'error', 'message': 'A tag with this name already exists in this team.'}), 409

        return jsonify({'id': tag_id, 'name': name}), 201

    tags = tags_helper.get_tags_for_team(team_id)
    return jsonify([dict(tag) for tag in tags])

@app.route('/api/projects/<int:project_id>/tags', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
def list_project_tags(project_id):
    tags = tags_helper.get_tags_for_project(project_id)
    return jsonify([dict(tag) for tag in tags])


@app.route('/api/projects/<int:project_id>/tags/<int:tag_id>', methods=['POST', 'DELETE'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def handle_project_tags(project_id, tag_id):
    db = get_db()
    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    team_id = project['team_id']
    if team_id is None:
        return (
            jsonify({'status': 'error', 'message': 'Tags are only available for team workspaces.'}),
            400,
        )

    tag = tags_helper.get_tag(tag_id)
    if not tag:
        return jsonify({'status': 'error', 'message': 'Tag not found.'}), 404

    if tag['team_id'] != team_id:
        return (
            jsonify({'status': 'error', 'message': 'Tag does not belong to this team.'}),
            403,
        )

    if request.method == 'POST':
        tags_helper.add_tag_to_project(project_id, tag_id)
        return jsonify({'status': 'success'})

    tags_helper.remove_tag_from_project(project_id, tag_id)
    return jsonify({'status': 'success'})

@app.route('/api/jobs/<int:job_id>/tags/<int:tag_id>', methods=['POST', 'DELETE'])
@login_required
@csrf_protect
def handle_job_tags(job_id, tag_id):
    db = get_db()
    job_info = db.execute('SELECT project_id FROM jobs WHERE id = %s', (job_id,)).fetchone()
    if not job_info:
        return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job_info['project_id'],)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    team_id = project['team_id']
    if team_id is None:
        return jsonify({'status': 'error', 'message': 'Tags require a team-backed project.'}), 400

    tag = tags_helper.get_tag(tag_id)
    if not tag:
        return jsonify({'status': 'error', 'message': 'Tag not found.'}), 404
    if tag['team_id'] != team_id:
        return (
            jsonify({'status': 'error', 'message': 'Tag does not belong to this team.'}),
            403,
        )

    role = get_project_assignment(db, session['user_id'], job_info['project_id'])
    if not role:
        return jsonify({'status': 'error', 'message': 'You do not have permission to modify this job.'}), 403

    if request.method == 'POST':
        tags_helper.add_tag_to_job(job_id, tag_id)
        return jsonify({'status': 'success'})

    tags_helper.remove_tag_from_job(job_id, tag_id)
    return jsonify({'status': 'success'})


@app.route('/api/projects/<int:project_id>/bulk_schedules', methods=['GET', 'POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def handle_bulk_schedules(project_id):
    db = get_db()
    if request.method == 'POST':
        data = request.json or {}
        try:
            tag_id = int(data.get('tag_id'))
        except (TypeError, ValueError):
            tag_id = None

        cron_schedule = (data.get('cron_schedule') or '').strip()

        if not tag_id or not cron_schedule:
            return jsonify({'status': 'error', 'message': 'tag_id and cron_schedule are required.'}), 400

        if len(cron_schedule) > 120:
            return jsonify({'status': 'error', 'message': 'cron_schedule must be 120 characters or fewer.'}), 400

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
        if not project:
            return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

        team_id = project['team_id']
        if team_id is None:
            return (
                jsonify({'status': 'error', 'message': 'Bulk schedules require a team-backed project.'}),
                400,
            )

        tag = tags_helper.get_tag(tag_id)
        if not tag:
            return jsonify({'status': 'error', 'message': 'Tag not found.'}), 404
        if tag['team_id'] != team_id:
            return (
                jsonify({'status': 'error', 'message': 'Tag does not belong to this team.'}),
                403,
            )

        cursor = db.execute(
            'INSERT INTO bulk_job_schedules (project_id, creator_id, tag_id, cron_schedule) VALUES (%s, %s, %s, %s)',
            (project_id, session['user_id'], tag_id, cron_schedule)
        )
        db.commit()
        return jsonify({'id': cursor.fetchone()['id']}), 201

    schedules = db.execute(
        'SELECT s.id, s.tag_id, s.cron_schedule, s.is_active, s.last_run_at, t.name AS tag_name '
        'FROM bulk_job_schedules s '
        'LEFT JOIN tags t ON t.id = s.tag_id '
        'WHERE s.project_id = %s',
        (project_id,),
    ).fetchall()
    return jsonify([dict(schedule) for schedule in schedules])

@app.route('/api/bulk_schedules/<int:schedule_id>', methods=['PUT', 'DELETE'])
@login_required
@csrf_protect
def handle_bulk_schedule(schedule_id):
    db = get_db()
    schedule = db.execute('SELECT project_id FROM bulk_job_schedules WHERE id = %s', (schedule_id,)).fetchone()
    if not schedule:
        return jsonify({'status': 'error', 'message': 'Schedule not found.'}), 404

    role = get_project_assignment(db, session['user_id'], schedule['project_id'])
    if not role or role != PROJECT_ROLE_MANAGER:
        return jsonify({'status': 'error', 'message': 'You do not have permission to modify this schedule.'}), 403

    if request.method == 'PUT':
        data = request.json or {}
        is_active = data.get('is_active')
        if is_active is not None:
            db.execute(
                'UPDATE bulk_job_schedules SET is_active = %s WHERE id = %s',
                (1 if bool(is_active) else 0, schedule_id),
            )
            db.commit()
        return jsonify({'status': 'success'})

    db.execute('DELETE FROM bulk_job_schedules WHERE id = %s', (schedule_id,))
    db.commit()
    return jsonify({'status': 'success'})
