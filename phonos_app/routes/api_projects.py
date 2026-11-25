"""API routes for projects, accounts, jobs, files, settings, and help desk."""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import secrets
import shlex
import shutil
import zipfile
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
    send_file,
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
from plan_service import DEFAULT_PLAN, get_plan_definition, get_plan_max_projects, is_valid_plan, normalize_plan
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
    is_path_within,
    is_valid_public_key,
    list_job_artifact_dirs,
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
    resolve_job_branch_path,
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
from ..helpers import data_crypto

DM_CONTENT = 'direct_messages.content'  # data_crypto label; never rename

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}


def _send_project_email(*args, **kwargs):
    """Route test-mode email sends through the consolidated API module mock."""
    if app.config.get('TESTING'):
        from . import api as api_routes
        return api_routes.send_email(*args, **kwargs)
    return send_email(*args, **kwargs)


def _file_view_counts_by_project() -> dict:
    """Files per project exactly as the Files view lists them.

    The Files view (/api/all_files_view) shows uploads plus job outputs plus
    job artifacts; the project cards used to count only the `files` table, so
    the dashboard said 0 while the Files footer said 7. Reuse the view's own
    query so both numbers always agree. Falls back to {} (plain upload counts).
    """
    try:
        from .api_files import get_all_files_view
        resp = get_all_files_view()
        if isinstance(resp, tuple):
            return {}
        rows = resp.get_json(silent=True)
        if not isinstance(rows, list):
            return {}
        counts: dict = defaultdict(int)
        for row in rows:
            if row.get('project_id') is not None:
                counts[row['project_id']] += 1
        return dict(counts)
    except Exception as exc:  # never break the project list over a count
        logger.warning("File-view count for project list failed: %s", exc)
        return {}


@app.route('/api/projects', methods=['GET', 'POST'])
@login_required
@csrf_protect
def handle_projects():
    """
    Handles project creation and retrieval.
    GET: Returns a list of all projects a user is assigned to.
    POST: Creates a new project within a team.
    """
    db = get_db()
    user_id = session['user_id']

    if request.method == 'POST':
        from ..helpers.lab_accounts import can_create_projects
        if not can_create_projects(db, user_id):
            return jsonify({
                'status': 'error',
                'message': 'Your global lab role only permits working inside assigned projects. Ask a Scientist or Administrator to create or assign a project.'
            }), 403

        # Manual validation for POST requests (route handles both GET and POST)
        if not request.is_json:
            return jsonify({'status': 'error', 'message': 'Content-Type must be application/json'}), 400

        data = request.json or {}

        # Validate required fields
        if not data.get('name') or not data.get('name').strip():
            return jsonify({'status': 'error', 'message': 'Project name is required'}), 400

        project_name = sanitize_input(data['name'].strip(), max_length=200)
        raw_project_type = data.get('project_type')
        has_team_identifier = any(
            data.get(key) is not None for key in ('team_id', 'organization_id')
        )
        if raw_project_type is None:
            project_type = 'team' if has_team_identifier else 'legacy'
        else:
            project_type = str(raw_project_type).lower()

        # Validate project type
        if project_type not in ['legacy', 'private', 'team']:
            return jsonify({'status': 'error', 'message': "Project type must be 'legacy', 'private', or 'team'"}), 400

        team_id = None

        if project_type == 'legacy':
            is_private = 1
            team_id = ensure_personal_team(db, user_id)
            if not team_id:
                return jsonify({'status': 'error', 'message': 'Could not create a personal workspace for the project'}), 500
        elif project_type == 'private':
            is_private = 1
            team_id = ensure_personal_team(db, user_id)
            if not team_id:
                return jsonify({'status': 'error', 'message': 'Could not create a personal workspace for the private project'}), 500
        else:  # project_type is 'team'
            is_private = 1
            raw_team_id = data.get('team_id')
            if raw_team_id is None:
                raw_team_id = data.get('organization_id')
            if not raw_team_id:
                return jsonify({'status': 'error', 'message': 'A team ID is required for team projects'}), 400
            try:
                team_id = int(raw_team_id)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'A valid Team ID is required'}), 400

            team_role = get_team_membership(db, user_id, team_id)
            if not team_role:
                return jsonify({'status': 'error', 'message': 'You must be a member of the team to create projects'}), 403

        if db.execute('SELECT 1 FROM projects WHERE team_id = %s AND lower(name) = lower(%s)', (team_id, project_name)).fetchone():
            return jsonify({'status': 'error', 'message': f'A project named "{project_name}" already exists in this team'}), 409

        # Check user's current project count against their plan limit (unlocked in LOCAL_LAB_MODE)
        if not getattr(Config, 'LOCAL_LAB_MODE', True):
            count_result = db.execute(
                'SELECT COUNT(*) as count FROM project_assignments WHERE user_id = %s AND role = %s',
                (user_id, Config.PROJECT_ROLE_MANAGER)
            ).fetchone()
            current_project_count = count_result['count'] if count_result else 0

            user_plan_row = db.execute('SELECT plan FROM users WHERE id = %s', (user_id,)).fetchone()
            user_plan = normalize_plan(user_plan_row['plan'] if user_plan_row else None)
            max_projects = get_plan_max_projects(user_plan)

            if current_project_count >= max_projects:
                return jsonify({
                    'status': 'error',
                    'message': f'You have reached the maximum limit of {max_projects} projects for your {user_plan} plan. Upgrade to create more projects'
                }), 403

        # Database transaction - let global error handler catch any DB errors
        db.execute('BEGIN')

        project_columns = {
            row['column_name']
            for row in db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'").fetchall()
        }

        insert_columns = ['team_id', 'name', 'description', 'is_private', 'display_anonymous']
        description = sanitize_html(data.get('description', '')) if data.get('description') else None
        insert_values = [team_id, project_name, description, is_private, 1 if data.get('display_anonymous') else 0]

        if 'organization_id' in project_columns:
            insert_columns.insert(1, 'organization_id')
            insert_values.insert(1, team_id)

        if 'project_type' in project_columns:
            insert_columns.append('project_type')
            insert_values.append(project_type)

        placeholders = ', '.join('%s' for _ in insert_columns)
        column_clause = ', '.join(insert_columns)

        result = db.execute(
            f'INSERT INTO projects ({column_clause}) VALUES ({placeholders})',
            insert_values,
        )

        project_id = result.lastrowid
        db.execute('INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, %s)',
                   (project_id, user_id, PROJECT_ROLE_MANAGER))
        project_path = get_safe_project_path(project_id, team_id)
        if project_path:
            os.makedirs(os.path.join(project_path, 'uploads'), exist_ok=True)
            os.makedirs(os.path.join(project_path, 'jobs'), exist_ok=True)
        db.commit()

        new_project = db.execute('SELECT * FROM projects WHERE id = %s', (project_id,)).fetchone()
        project_payload = dict(new_project)
        project_payload.setdefault('organization_id', project_payload.get('team_id'))
        project_payload['is_private'] = bool(project_payload.get('is_private'))
        project_payload['display_anonymous'] = bool(project_payload.get('display_anonymous'))
        return jsonify(project_payload), 201

    # GET Method: Fetch projects assigned directly, via shared Lab/Team membership, or Admin ?scope=all_lab
    from ..helpers.lab_accounts import get_user_auth_state, ROLE_VIEWER
    auth_state = get_user_auth_state(db, user_id)
    scope = (request.args.get('scope') or '').strip().lower()

    if scope == 'all_lab' and auth_state.get('is_admin'):
        projects_q = db.execute('''
            SELECT p.id, p.name, p.description, p.created_at, p.team_id,
                   COALESCE(pa.role, %s) as role,
                   p.collaboration_billing_model, p.auto_open_chat, p.is_private, p.display_anonymous, p.project_type,
                   1 as is_manager,
                   (SELECT COUNT(*) FROM project_assignments pa3 WHERE pa3.project_id = p.id AND pa3.role = %s) as total_managers,
                   (SELECT u2.username FROM project_assignments pa_own JOIN users u2 ON u2.id = pa_own.user_id
                    WHERE pa_own.project_id = p.id AND pa_own.role = %s LIMIT 1) as owner_username
            FROM projects p
            LEFT JOIN project_assignments pa ON p.id = pa.project_id AND pa.user_id = %s
            WHERE p.is_archived = 0
            ORDER BY p.created_at DESC
        ''', (PROJECT_ROLE_MANAGER, PROJECT_ROLE_MANAGER, PROJECT_ROLE_MANAGER, user_id)).fetchall()
    else:
        projects_q = db.execute('''
            SELECT DISTINCT p.id, p.name, p.description, p.created_at, p.team_id,
                   COALESCE(
                       pa.role,
                       CASE tm.role
                           WHEN 'owner' THEN %s
                           WHEN 'admin' THEN %s
                           WHEN 'member' THEN %s
                           ELSE %s
                       END
                   ) as role,
                   p.collaboration_billing_model, p.auto_open_chat, p.is_private, p.display_anonymous, p.project_type,
                   (SELECT COUNT(*) FROM project_assignments pa2 WHERE pa2.project_id = p.id AND pa2.user_id = %s AND pa2.role = %s) as is_manager,
                   (SELECT COUNT(*) FROM project_assignments pa3 WHERE pa3.project_id = p.id AND pa3.role = %s) as total_managers
            FROM projects p
            LEFT JOIN project_assignments pa ON p.id = pa.project_id AND pa.user_id = %s
            LEFT JOIN teams t ON p.team_id = t.id
            LEFT JOIN team_members tm ON p.team_id = tm.team_id AND tm.user_id = %s AND (t.slug IS NULL OR t.slug NOT LIKE 'personal-%%')
            WHERE (pa.user_id IS NOT NULL OR tm.user_id IS NOT NULL) AND p.is_archived = 0
            ORDER BY p.created_at DESC
        ''', (
            PROJECT_ROLE_MANAGER, PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER,
            user_id, PROJECT_ROLE_MANAGER, PROJECT_ROLE_MANAGER, user_id, user_id,
        )).fetchall()

    projects = [dict(p) for p in projects_q]
    for project in projects:
        eff_role = project.get('role')
        if auth_state.get('system_role') == ROLE_VIEWER and not auth_state.get('is_admin'):
            eff_role = PROJECT_ROLE_VIEWER
        project['is_owner'] = eff_role == PROJECT_ROLE_MANAGER
        project['role'] = externalize_project_role(eff_role)

        # Show "Leave" button if:
        # - User is not a manager (contributor/viewer), OR
        # - User is a manager but there are other managers (not the sole manager)
        is_not_manager = project.get('is_manager', 0) == 0
        is_not_sole_manager = project.get('total_managers', 1) > 1

        # Show leave button if user is not a manager OR (is a manager but not the only one)
        project['is_invited'] = is_not_manager or is_not_sole_manager

        project.setdefault('organization_id', project.get('team_id'))
        project.setdefault('collaboration_billing_model', project.get('collaboration_billing_model'))
        project['is_private'] = bool(project.get('is_private'))
        project['display_anonymous'] = bool(project.get('display_anonymous'))

    project_ids = [p['id'] for p in projects]
    members_by_project: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if project_ids:
        placeholders = ','.join('%s' for _ in project_ids)
        assignments_q = db.execute(
            f'''
                SELECT
                    pa.project_id,
                    u.id AS user_id,
                    u.username,
                    u.legal_name,
                    u.profile_picture_url,
                    pa.role
                FROM project_assignments pa
                JOIN users u ON u.id = pa.user_id
                WHERE pa.project_id IN ({placeholders})
                ORDER BY
                    pa.project_id,
                    CASE pa.role
                        WHEN %s THEN 0
                        WHEN %s THEN 1
                        WHEN %s THEN 2
                        ELSE 3
                    END,
                    lower(u.username)
            ''',
            (*project_ids, PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER),
        ).fetchall()

        for row in assignments_q:
            role = externalize_project_role(row['role'])
            members_by_project[row['project_id']].append({
                'user_id': row['user_id'],
                'username': row['username'],
                'display_name': row['legal_name'] or row['username'],
                'profile_picture_url': row['profile_picture_url'],
                'role': role,
            })

    # Get file, job, notebook, and device counts per project
    file_counts = {}
    job_counts = {}
    notebook_counts = {}
    notebook_list_by_project = defaultdict(list)
    device_counts = {}
    device_list_by_project = defaultdict(list)
    if project_ids:
        placeholders = ','.join('%s' for _ in project_ids)
        file_counts_q = db.execute(
            f'SELECT project_id, COUNT(id) as count FROM files WHERE project_id IN ({placeholders}) GROUP BY project_id',
            project_ids
        ).fetchall()
        file_counts = {row['project_id']: row['count'] for row in file_counts_q}

        job_counts_q = db.execute(
            f'SELECT project_id, COUNT(id) as count FROM jobs WHERE project_id IN ({placeholders}) GROUP BY project_id',
            project_ids
        ).fetchall()
        job_counts = {row['project_id']: row['count'] for row in job_counts_q}

        # Get notebooks per project
        notebooks_q = db.execute(f'''
            SELECT n.id, n.name, n.project_id, n.created_at, n.updated_at, n.metadata,
                   u.username as creator_name,
                   (SELECT COUNT(*) FROM notebook_sessions ns
                    WHERE ns.notebook_id = n.id AND ns.kernel_status IN ('idle', 'busy')) as active_sessions
            FROM notebooks n
            LEFT JOIN users u ON n.creator_id = u.id
            WHERE n.project_id IN ({placeholders})
        ''', project_ids).fetchall()
        for nb in notebooks_q:
            metadata = nb['metadata']
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (ValueError, TypeError):
                    metadata = {}
            elif not isinstance(metadata, dict):
                metadata = {}

            notebook_list_by_project[nb['project_id']].append({
                'id': nb['id'],
                'name': nb['name'],
                'description': metadata.get('description', ''),
                'creator_name': nb['creator_name'],
                'active_sessions': nb['active_sessions'],
                'cell_count': 0,
                'output_count': 0,
                'created_at': nb['created_at'].isoformat() if nb['created_at'] else None,
                'updated_at': nb['updated_at'].isoformat() if nb['updated_at'] else None,
            })
        notebook_counts = {pid: len(nbs) for pid, nbs in notebook_list_by_project.items()}

        # Get IoT devices per project (IoT is optional — degrade gracefully if
        # the iot_devices table is absent, e.g. on fresh/test installs that only
        # run _create_schema and not the unrun migrations/*.sql files).
        if project_ids:
            placeholders = ','.join('%s' for _ in project_ids)
            try:
                devices_q = db.execute(f'''
                    SELECT id, device_id, name, device_type, description,
                           status, last_seen, current_state, schema, tags,
                           dashboard_enabled, created_at, updated_at, project_id
                    FROM iot_devices
                    WHERE project_id IN ({placeholders})
                    ORDER BY name ASC
                ''', project_ids).fetchall()
                for d in devices_q:
                    device_list_by_project[d['project_id']].append({
                        'id': d['id'],
                        'device_id': d['device_id'],
                        'name': d['name'],
                        'device_type': d['device_type'],
                        'description': d['description'],
                        'status': d['status'],
                        'is_online': d['status'] == 'online',
                        'last_seen': d['last_seen'].isoformat() if d['last_seen'] else None,
                        'dashboard_enabled': d['dashboard_enabled'],
                        'created_at': d['created_at'].isoformat() if d['created_at'] else None,
                    })
            except Exception:
                logger.debug("iot_devices table unavailable; skipping device counts", exc_info=True)
        device_counts = {pid: len(devs) for pid, devs in device_list_by_project.items()}

    file_view_counts = _file_view_counts_by_project()
    for project in projects:
        members = members_by_project.get(project['id'], [])
        project['members'] = members
        project['member_count'] = len(members)
        project['collaborator_count'] = sum(
            1 for member in members if (member.get('role') or '').lower() != 'spectator'
        )
        project['file_count'] = file_view_counts.get(project['id'], file_counts.get(project['id'], 0))
        project['job_count'] = job_counts.get(project['id'], 0)
        project['notebook_count'] = notebook_counts.get(project['id'], 0)
        project['notebooks'] = notebook_list_by_project.get(project['id'], [])
        project['device_count'] = device_counts.get(project['id'], 0)
        project['devices'] = device_list_by_project.get(project['id'], [])

    return jsonify(projects)

# Status → HTTP code mapping for a single invitation attempt.
_INVITE_STATUS_CODES = {
    'invited': 200,
    'already_member': 409,
    'already_invited': 409,
    'no_account': 404,
    'email_failed': 500,
    'invalid': 400,
}


def _create_single_invitation(db, project_id, project_name, inviter_id, inviter_name,
                              invitee_email, normalized_role, allow_unregistered=False):
    """Create one pending project invitation and email the invitee.

    Returns a ``(status, message)`` tuple where ``status`` is one of the keys in
    ``_INVITE_STATUS_CODES``. The invitation row is written into the current
    transaction but NOT committed here — the caller commits once (single invite)
    or after processing a whole batch (bulk invite). On a recoverable failure
    (bad email, delivery failure) the just-inserted row is deleted so a later
    commit leaves no orphan.

    When ``allow_unregistered`` is True, an email that has no Phonos account
    yet still gets an invitation + a sign-up email. The dashboard's
    pending-invitations panel matches by email, so the invite is waiting for
    the invitee the moment they register with that address.
    """
    invitee_email = (invitee_email or '').strip()
    if not invitee_email or '@' not in invitee_email:
        return 'invalid', f'"{invitee_email}" is not a valid email address.'

    invitee = db.execute(
        'SELECT u.id as user_id FROM users u JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE lower(uc.email) = lower(%s)',
        (invitee_email,),
    ).fetchone()
    if not invitee and not allow_unregistered:
        return 'no_account', f'No Phonos account exists for {invitee_email}.'

    if invitee:
        already_assigned = db.execute(
            'SELECT 1 FROM project_assignments WHERE project_id = %s AND user_id = %s',
            (project_id, invitee['user_id']),
        ).fetchone()
        if already_assigned:
            return 'already_member', f'{invitee_email} is already a member of this project.'

    pending_invitation = db.execute(
        "SELECT 1 FROM invitations WHERE project_id = %s AND lower(invitee_email) = lower(%s) AND status = 'pending'",
        (project_id, invitee_email),
    ).fetchone()
    if pending_invitation:
        return 'already_invited', f'{invitee_email} already has a pending invitation.'

    token = secrets.token_urlsafe(32)
    result = db.execute(
        'INSERT INTO invitations (project_id, inviter_id, token, role, invitee_email) VALUES (%s, %s, %s, %s, %s)',
        (project_id, inviter_id, token, normalized_role, invitee_email),
    )
    invitation_id = result.lastrowid
    role_display = externalize_project_role(normalized_role) or normalized_role

    # In-app notification only makes sense for people who already have accounts.
    if invitee:
        create_user_notification(
            db,
            invitee['user_id'],
            'Project invitation',
            f"{inviter_name} invited you to join {project_name} as a {role_display}.",
            category='project',
            metadata={'invitation_id': invitation_id, 'invitation_token': token, 'project_id': project_id},
        )

    invitation_link = url_for('accept_invitation', token=token, _external=True)
    email_subject = f"You're invited to collaborate on '{project_name}'"
    if invitee:
        email_body = (
            f"<p>You have been invited by {inviter_name} to join the project '{project_name}' as a {role_display}.</p>"
            f"<p>Click the link below to accept the invitation:</p>"
            f"<a href='{invitation_link}'>Accept Invitation</a>"
        )
    else:
        # Invitee without an account yet: point them at sign-up first. Once
        # they register with this address, get_pending_invitations() surfaces
        # the invite on their dashboard automatically (it matches by email).
        signup_link = url_for('register', _external=True)
        email_body = (
            f"<p>{inviter_name} invited you to join '{project_name}' as a {role_display} on Phonos.</p>"
            f"<p>Create your account using this email address (<strong>{invitee_email}</strong>) to get started:</p>"
            f"<p><a href='{signup_link}'>Create your Phonos account</a></p>"
            f"<p>After you sign in, the invitation will be waiting on your dashboard. You can also open it "
            f"directly once signed in:</p>"
            f"<a href='{invitation_link}'>Accept Invitation</a>"
        )

    if not _send_project_email(invitee_email, email_subject, email_body):
        # Undo the row we just inserted so a batch commit doesn't persist a
        # pending invite the student will never receive an email for.
        db.execute('DELETE FROM invitations WHERE id = %s', (invitation_id,))
        logger.error('Failed to send project invitation email to %s', invitee_email)
        return 'email_failed', f'Could not send the invitation email to {invitee_email}.'

    if invitee:
        return 'invited', f'Invitation sent to {invitee_email}.'
    return 'invited', f'Invitation emailed to {invitee_email}. They will need to create an account with that address.'


@app.route('/api/projects/<int:project_id>/invitations', methods=['POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def invite_to_project(project_id):
    db = get_db()
    project = db.execute('SELECT name, is_private, project_type FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found'}), 404

    user_id = session['user_id']
    data = request.json or {}
    invitee_email = (data.get('email') or '').strip()
    if not invitee_email:
        return jsonify({'status': 'error', 'message': 'Email address is required'}), 400

    normalized_role = normalize_project_role(data.get('role', PROJECT_ROLE_CONTRIBUTOR))
    if not normalized_role or normalized_role not in PROJECT_MUTABLE_ROLES:
        return jsonify({'status': 'error', 'message': 'Invalid role specified. Choose collaborator or spectator'}), 400

    inviter_row = db.execute('SELECT username FROM users WHERE id = %s', (user_id,)).fetchone()
    inviter_name = inviter_row['username'] if inviter_row else 'A teammate'
    project_name = project['name'] or 'Project'

    status, message = _create_single_invitation(
        db, project_id, project_name, user_id, inviter_name,
        invitee_email, normalized_role, allow_unregistered=False,
    )
    if status != 'invited':
        db.rollback()
        return jsonify({'status': 'error', 'message': message}), _INVITE_STATUS_CODES.get(status, 400)

    db.commit()
    return jsonify({'status': 'success', 'message': message})


@app.route('/api/projects/<int:project_id>/invitations/bulk', methods=['POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def bulk_invite_to_project(project_id):
    """Invite several collaborators at once from a pasted list of emails.

    Accepts ``emails`` as either a list or a free-form string (split on commas,
    semicolons, or whitespace/newlines). Each email is processed independently
    and reported back so the inviter can see exactly who was invited, who was
    already a member, and which addresses failed.
    """
    db = get_db()
    project = db.execute('SELECT name, project_type FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found'}), 404

    user_id = session['user_id']
    data = request.json or {}

    raw = data.get('emails')
    if isinstance(raw, list):
        candidates = [str(e) for e in raw]
    elif isinstance(raw, str):
        candidates = re.split(r'[\s,;]+', raw)
    else:
        candidates = []

    # De-duplicate case-insensitively while preserving the teacher's order.
    seen, emails = set(), []
    for entry in (e.strip() for e in candidates):
        if not entry:
            continue
        key = entry.lower()
        if key not in seen:
            seen.add(key)
            emails.append(entry)

    if not emails:
        return jsonify({'status': 'error', 'message': 'Provide at least one email address.'}), 400
    if len(emails) > 200:
        return jsonify({'status': 'error', 'message': 'You can invite at most 200 people at a time.'}), 400

    normalized_role = normalize_project_role(data.get('role', PROJECT_ROLE_CONTRIBUTOR))
    if not normalized_role or normalized_role not in PROJECT_MUTABLE_ROLES:
        return jsonify({'status': 'error', 'message': 'Invalid role specified. Choose collaborator or spectator'}), 400

    inviter_row = db.execute('SELECT username FROM users WHERE id = %s', (user_id,)).fetchone()
    inviter_name = inviter_row['username'] if inviter_row else 'A teammate'
    project_name = project['name'] or 'Project'

    # Commit after each invite (rather than once at the end) and isolate any
    # unexpected exception to the single email that raised it -- otherwise one
    # bad row anywhere in the batch would roll back every invite already
    # created (and already emailed) in this same transaction, silently
    # breaking the "each email is processed independently" contract above.
    results, invited = [], 0
    for email in emails:
        try:
            status, message = _create_single_invitation(
                db, project_id, project_name, user_id, inviter_name,
                email, normalized_role, allow_unregistered=False,
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception('Bulk invite failed unexpectedly for %s in project %s', email, project_id)
            status, message = 'error', f'Could not process {email} due to an unexpected error.'
        results.append({'email': email, 'status': status, 'message': message})
        if status == 'invited':
            invited += 1

    return jsonify({
        'status': 'success',
        'invited': invited,
        'total': len(emails),
        'results': results,
    })


@app.route('/api/projects/<int:project_id>', methods=['PATCH'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def update_project_settings(project_id):
    db = get_db()
    user_id = session['user_id']

    project = db.execute('SELECT id, team_id, is_private, display_anonymous, category_id, published_at FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found'}), 404

    data = request.json or {}
    update_fields = []
    update_params = []
    current_role = g.current_project_role
    manager_only_fields = {'collaboration_billing_model', 'is_private', 'display_anonymous', 'category_id'}

    if current_role != PROJECT_ROLE_MANAGER and any(field in data for field in manager_only_fields):
        return jsonify({'status': 'error', 'message': 'Only project managers can change project settings.'}), 403

    if 'name' in data:
        name = sanitize_input(data['name'].strip())
        if not name:
            return jsonify({'status': 'error', 'message': 'Project name is required'}), 400
        update_fields.append('name = %s')
        update_params.append(name)

    if 'description' in data:
        description = sanitize_html(data.get('description', '').strip())
        update_fields.append('description = %s')
        update_params.append(description)

    if 'collaboration_billing_model' in data:
        billing_model = data['collaboration_billing_model']
        if billing_model not in ['admin_credits', 'collaborator_credits']:
            return jsonify({'status': 'error', 'message': 'Invalid billing model'}), 400
        update_fields.append('collaboration_billing_model = %s')
        update_params.append(billing_model)

    if 'is_private' in data:
        # Public projects are disabled for now: all projects are forced private,
        # regardless of the requested value, and never published.
        update_fields.append('is_private = %s')
        update_params.append(1)

    if 'display_anonymous' in data:
        display_anonymous = bool(data['display_anonymous'])
        update_fields.append('display_anonymous = %s')
        update_params.append(1 if display_anonymous else 0)

    if 'category_id' in data:
        category_id = data['category_id']
        if category_id is not None:
            # Validate category exists
            cat = db.execute('SELECT id FROM project_categories WHERE id = %s', (category_id,)).fetchone()
            if not cat:
                return jsonify({'status': 'error', 'message': 'Invalid category'}), 400
            update_fields.append('category_id = %s')
            update_params.append(category_id)

    if not update_fields:
        return jsonify({'status': 'success', 'message': 'No changes provided.'}), 200

    update_params.append(project_id)
    db.execute(f"UPDATE projects SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s", tuple(update_params))
    db.commit()

    return jsonify({'status': 'success', 'message': 'Project settings updated successfully.'})


@app.route('/api/projects/archived', methods=['GET'])
@login_required
def list_archived_projects():
    """Return all archived projects the current user manages."""
    db = get_db()
    user_id = session['user_id']

    rows = db.execute('''
        SELECT p.id, p.name, p.description, p.archived_at, p.team_id, p.created_at
        FROM projects p
        JOIN project_assignments pa ON pa.project_id = p.id AND pa.user_id = %s AND pa.role = %s
        WHERE p.is_archived = 1
        ORDER BY p.archived_at DESC
    ''', (user_id, PROJECT_ROLE_MANAGER)).fetchall()

    archived = []
    for row in rows:
        p = dict(row)
        project_path = get_safe_project_path(p['id'], p['team_id'])
        zip_path = os.path.join(project_path, 'archives', f"{p['id']}.zip") if project_path else None
        p['has_archive'] = bool(zip_path and os.path.isfile(zip_path))
        p['archive_size'] = os.path.getsize(zip_path) if p['has_archive'] else 0
        p['archived_at'] = serialize_datetime(p['archived_at'])
        p['created_at'] = serialize_datetime(p['created_at'])
        archived.append(p)

    return jsonify({'status': 'success', 'projects': archived})



@app.route('/api/projects/<int:project_id>/unarchive', methods=['POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def unarchive_project(project_id):
    """Restore an archived project."""
    db = get_db()
    project = db.execute('SELECT id, name, team_id, is_archived FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found'}), 404
    if not project['is_archived']:
        return jsonify({'status': 'error', 'message': 'Project is not archived'}), 400

    db.execute(
        'UPDATE projects SET is_archived = 0, archived_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
        (project_id,)
    )
    db.commit()
    return jsonify({'status': 'success', 'message': 'Project restored successfully.'})


@app.route('/api/projects/<int:project_id>/archive/download', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
def download_project_archive(project_id):
    """Download the zip archive of a project."""
    db = get_db()
    project = db.execute('SELECT id, name, team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found'}), 404

    project_path = get_safe_project_path(project_id, project['team_id'])
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Project storage path not found'}), 404

    zip_path = os.path.join(project_path, 'archives', f'{project_id}.zip')
    if not os.path.isfile(zip_path):
        return jsonify({'status': 'error', 'message': 'Archive not found. Archive the project first.'}), 404

    safe_name = secure_filename(project['name']) or f'project_{project_id}'
    return send_file(zip_path, as_attachment=True, download_name=f'{safe_name}_archive.zip', mimetype='application/zip')


@app.route('/api/diffdock/submit', methods=['POST'])
@login_required
@csrf_protect
def diffdock_submit():
    """Submit a DiffDock molecular docking job using NVIDIA API."""
    data = request.get_json()
    protein = data.get('protein')  # Can be sequence, file_id, or PDB content
    ligand = data.get('ligand')
    pdb_content = data.get('pdb_content')  # Direct PDB content
    sdf_content = data.get('sdf_content')  # Direct SDF content
    samples_per_complex = data.get('samples_per_complex', 10)
    inference_steps = data.get('inference_steps', 20)

    # Enhanced input validation - check for protein input
    if not protein and not pdb_content:
        return jsonify({'status': 'error', 'message': 'Protein sequence, PDB content, or file is required'}), 400

    if not ligand and not sdf_content:
        return jsonify({'status': 'error', 'message': 'Ligand (SMILES, SDF content, or file path) is required'}), 400

    # Determine protein input type and validate accordingly
    protein_is_pdb_content = False

    if pdb_content:
        # Direct PDB content provided - skip sequence validation
        protein_is_pdb_content = True
        protein = None  # Don't use the protein field
    elif protein:
        protein_str = str(protein).strip()

        # Check if protein looks like PDB file content (not a file path or sequence)
        pdb_indicators = ['HEADER', 'ATOM', 'HETATM', 'MODEL', 'REMARK', 'CRYST1']
        if any(protein_str.upper().startswith(indicator) for indicator in pdb_indicators):
            # This is PDB content, not a sequence
            pdb_content = protein_str
            protein_is_pdb_content = True
            protein = None
        # No amino acid validation for DiffDock - it accepts sequences, file paths, file IDs, and PDB content
        # The DiffDock worker will handle input validation

    # Determine ligand input type and validate accordingly
    ligand_is_sdf_content = False

    if sdf_content:
        # Direct SDF content provided - skip SMILES validation
        ligand_is_sdf_content = True
        ligand = None  # Don't use the ligand field
    elif ligand:
        ligand_str = str(ligand).strip()

        # Check if ligand looks like SDF file content
        if ligand_str.strip().count('\n') > 5 and ('V2000' in ligand_str or 'V3000' in ligand_str or 'M  END' in ligand_str):
            # This is SDF content, not a SMILES string
            sdf_content = ligand_str
            ligand_is_sdf_content = True
            ligand = None
        elif not ligand_str.startswith('/') and not ligand_str.endswith(('.sdf', '.mol', '.mol2')):
            # Validate SMILES format using basic rules
            if len(ligand_str) == 0:
                return jsonify({'status': 'error', 'message': 'Ligand SMILES string is empty'}), 400

            # Basic SMILES validation - check for common chemical symbols
            valid_smiles_chars = set('CNOPSFClBrI[]()=#-+@0123456789cnopsfbri\\/')
            invalid_chars = set(c for c in ligand_str if c not in valid_smiles_chars)

            if invalid_chars:
                # Only warn, don't reject - SMILES can have other characters
                logger.warning(f'Potentially invalid SMILES characters: {invalid_chars}')

    # Validate and clamp parameters
    try:
        samples_per_complex = int(samples_per_complex)
        samples_per_complex = max(1, min(40, samples_per_complex))  # Clamp to 1-40
    except (ValueError, TypeError):
        samples_per_complex = 10

    try:
        inference_steps = int(inference_steps)
        inference_steps = max(10, min(50, inference_steps))  # Clamp to 10-50
    except (ValueError, TypeError):
        inference_steps = 20

    # Create a new job
    db = get_db()
    user_id = session['user_id']
    job_id = str(uuid.uuid4())

    # Get and validate the project_id from the request
    try:
        project_id = int(data.get('project_id', 0))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Invalid project_id'}), 400

    if not project_id:
        return jsonify({'status': 'error', 'message': 'project_id is required'}), 400

    # Verify the user is a member of the requested project
    role = get_project_assignment(db, user_id, project_id)
    if not role:
        return jsonify({'status': 'error', 'message': 'Project not found or access denied'}), 403

    # Store job in database with type-3 model
    # Use protein for sequence field if available, otherwise use a placeholder
    db_sequence = protein if protein else '[PDB Content]'
    db.execute(
        'INSERT INTO jobs (project_id, creator_id, job_id, model, sequence, status) VALUES (%s, %s, %s, %s, %s, %s)',
        (project_id, user_id, job_id, 'type-3', db_sequence, 'submitted')
    )
    db.commit()

    # Create output directory path using the configured job output root
    output_path = os.path.join(Config.JOB_OUTPUT_DIR, job_id, 'output.json')

    # Prepare job payload for type-3 worker (DiffDock with NVIDIA API)
    params = {
        'samples_per_complex': samples_per_complex,
        'inference_steps': inference_steps,
    }

    # Add protein input (priority: pdb_content > protein path > sequence)
    if pdb_content:
        params['pdb_content'] = pdb_content
    elif protein:
        params['sequence'] = protein

    # Add ligand input (priority: sdf_content > ligand SMILES/path)
    if sdf_content:
        params['sdf_content'] = sdf_content
    elif ligand:
        params['ligand'] = ligand

    job_payload = {
        'job_id': job_id,
        'model': 'type-3',
        'task': 'molecular_docking',
        'organization_id': str(user_id),
        'params': params,
        'output_path': output_path,
    }

    # Encrypt and send to the correct queue
    if not encrypt:
        return jsonify({'status': 'error', 'message': 'Encryption not available'}), 500

    try:
        r = get_redis_connection()
        job_json = json.dumps(job_payload)
        encrypted_job = encrypt(job_json.encode('utf-8'))

        if not encrypted_job:
            return jsonify({'status': 'error', 'message': 'Failed to encrypt job payload'}), 500

        # Push to type-3 worker queue
        r.lpush('queue:job:type-3', encrypted_job)

        logger.info(f"Submitted DiffDock job {job_id} to queue:job:type-3")

        return jsonify({'status': 'success', 'job_id': job_id})

    except Exception as e:
        logger.error(f"Failed to submit DiffDock job: {e}")
        db.execute('UPDATE jobs SET status = %s WHERE job_id = %s', ('error', job_id))
        db.commit()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/simplefold/submit', methods=['POST'])
@login_required
@csrf_protect
def simplefold_submit():
    data = request.get_json()
    sequence = data.get('sequence')

    # Create a new job
    db = get_db()
    user_id = session['user_id']
    job_id = str(uuid.uuid4())

    # Get and validate the project_id from the request
    try:
        project_id = int(data.get('project_id', 0))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Invalid project_id'}), 400

    if not project_id:
        return jsonify({'status': 'error', 'message': 'project_id is required'}), 400

    # Verify the user is a member of the requested project
    role = get_project_assignment(db, user_id, project_id)
    if not role:
        return jsonify({'status': 'error', 'message': 'Project not found or access denied'}), 403

    db.execute(
        'INSERT INTO jobs (project_id, creator_id, job_id, model, sequence, status) VALUES (%s, %s, %s, %s, %s, %s)',
        (project_id, user_id, job_id, 'simplefold', sequence, 'submitted')
    )
    db.commit()

    # Send the job to the worker
    r = get_redis_connection()
    job_payload = {
        'job_id': job_id,
        'model': 'simplefold',
        'task': 'protein_folding',
        'params': {
            'sequence': sequence,
        },
        'output_path': f'/app/job_outputs/{job_id}',
        'output_display_name': 'Folded Protein (PDB)',
    }
    r.lpush('queue:new_jobs', json.dumps(job_payload))

    return jsonify({'status': 'success', 'job_id': job_id})


@app.route('/api/public_projects', methods=['GET'])
def get_public_projects():
    """
    Fetch public projects with filtering, sorting, and pagination.
    Query params:
    - category: filter by category slug
    - search: search term for project name/description
    - sort: hot, new, top
    - limit: number of results (default 50)
    - offset: pagination offset (default 0)
    """
    db = get_db()
    category = request.args.get('category')
    search_term = request.args.get('search', '').strip()
    sort = request.args.get('sort', 'hot')
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'limit and offset must be integers'}), 400

    # Get current user's votes if logged in
    user_id = session.get('user_id')

    # Base query for public projects
    query = '''
        SELECT
            p.id, p.name, p.description, p.notes, p.category_id, p.view_count, p.download_count,
            p.vote_score, p.created_at, p.published_at, p.display_anonymous,
            pc.name as category_name, pc.slug as category_slug, pc.color as category_color, pc.icon as category_icon,
            u.username as creator_username, u.profile_picture_url,
            (SELECT COUNT(*) FROM project_comments WHERE project_id = p.id AND is_deleted = 0) as comment_count,
            (SELECT COUNT(*) FROM jobs WHERE project_id = p.id) as total_jobs,
            (SELECT COUNT(*) FROM jobs WHERE project_id = p.id AND status = 'completed') as completed_jobs,
            (SELECT COUNT(*) FROM jobs WHERE project_id = p.id AND status = 'processing') as running_jobs,
            (SELECT COUNT(*) FROM jobs WHERE project_id = p.id AND status = 'failed') as failed_jobs,
            (SELECT COUNT(*) FROM files WHERE project_id = p.id) as file_count
        FROM projects p
        LEFT JOIN project_categories pc ON p.category_id = pc.id
        LEFT JOIN project_assignments pa ON p.id = pa.project_id AND pa.role = %s
        LEFT JOIN users u ON pa.user_id = u.id
        WHERE p.is_private = 0 AND p.is_archived = 0
    '''
    params = [PROJECT_ROLE_MANAGER]

    # Filter by category
    if category:
        query += ' AND pc.slug = %s'
        params.append(category)

    # Filter by search term
    if search_term:
        query += ' AND (lower(p.name) LIKE %s OR lower(p.description) LIKE %s)'
        search_pattern = f'%{search_term.lower()}%'
        params.extend([search_pattern, search_pattern])

    # Sorting (removed controversial)
    if sort == 'new':
        query += ' ORDER BY p.published_at DESC, p.created_at DESC'
    elif sort == 'top':
        query += ' ORDER BY p.vote_score DESC, p.created_at DESC'
    else:  # hot (default)
        # Reddit-style hot algorithm: score / time_decay
        # Age in days via EXTRACT(EPOCH ...) (PostgreSQL; julianday() is SQLite-only).
        query += (' ORDER BY (p.vote_score * 1.0) / (1 + (EXTRACT(EPOCH FROM (NOW() - COALESCE(p.published_at, p.created_at))) / 86400.0) / 7) DESC')

    query += ' LIMIT %s OFFSET %s'
    params.extend([limit, offset])

    projects_q = db.execute(query, params).fetchall()
    projects = []

    for row in projects_q:
        project = dict(row)
        # Get user's vote if logged in
        if user_id:
            vote_row = db.execute(
                'SELECT vote_type FROM project_votes WHERE project_id = %s AND user_id = %s',
                (project['id'], user_id)
            ).fetchone()
            project['user_vote'] = vote_row['vote_type'] if vote_row else 0
        else:
            project['user_vote'] = 0

        # Hide creator if display_anonymous
        if project.get('display_anonymous'):
            project['creator_username'] = 'Anonymous'
            project['profile_picture_url'] = None

        projects.append(project)

    # Get total count for pagination
    count_query = 'SELECT COUNT(*) as total FROM projects p LEFT JOIN project_categories pc ON p.category_id = pc.id WHERE p.is_private = 0 AND p.is_archived = 0'
    count_params = []
    if category:
        count_query += ' AND pc.slug = %s'
        count_params.append(category)
    if search_term:
        count_query += ' AND (lower(p.name) LIKE %s OR lower(p.description) LIKE %s)'
        search_pattern = f'%{search_term.lower()}%'
        count_params.extend([search_pattern, search_pattern])

    total = db.execute(count_query, count_params).fetchone()['total']

    # Return direct array for frontend compatibility
    return jsonify(projects)


@app.route('/api/projects/<int:project_id>', methods=['GET'])
def get_public_project_detail(project_id):
    """Get details for a single public project."""
    db = get_db()
    user_id = session.get('user_id')

    # Fetch project details
    project = db.execute('''
        SELECT
            p.id, p.name, p.description, p.category_id, p.view_count, p.download_count,
            p.vote_score, p.created_at, p.published_at, p.display_anonymous,
            pc.name as category_name, pc.slug as category_slug, pc.color as category_color, pc.icon as category_icon,
            u.username, u.profile_picture_url,
            (SELECT COUNT(*) FROM project_comments WHERE project_id = p.id AND is_deleted = 0) as comment_count,
            (SELECT COUNT(*) FROM jobs WHERE project_id = p.id) as total_jobs,
            (SELECT COUNT(*) FROM jobs WHERE project_id = p.id AND status = 'completed') as completed_jobs,
            (SELECT COUNT(*) FROM files WHERE project_id = p.id) as file_count
        FROM projects p
        LEFT JOIN project_categories pc ON p.category_id = pc.id
        LEFT JOIN project_assignments pa ON p.id = pa.project_id AND pa.role = %s
        LEFT JOIN users u ON pa.user_id = u.id
        WHERE p.id = %s AND p.is_private = 0 AND p.is_archived = 0
    ''', (PROJECT_ROLE_MANAGER, project_id)).fetchone()

    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    project_dict = dict(project)

    # Get user's vote if logged in
    if user_id:
        vote_row = db.execute(
            'SELECT vote_type FROM project_votes WHERE project_id = %s AND user_id = %s',
            (project_id, user_id)
        ).fetchone()
        project_dict['user_vote'] = vote_row['vote_type'] if vote_row else 0
    else:
        project_dict['user_vote'] = 0

    # Hide creator if display_anonymous
    if project_dict.get('display_anonymous'):
        project_dict['username'] = 'Anonymous'
        project_dict['profile_picture_url'] = None

    # Increment view count
    db.execute('UPDATE projects SET view_count = view_count + 1 WHERE id = %s', (project_id,))
    db.commit()

    return jsonify({'status': 'success', 'project': project_dict})


@app.route('/api/public_projects/<int:project_id>/files', methods=['GET'])
def get_public_project_files(project_id):
    """Get files for a public project."""
    db = get_db()

    # Check if project exists and is public
    project = db.execute(
        'SELECT id, team_id FROM projects WHERE id = %s AND is_private = 0',
        (project_id,)
    ).fetchone()

    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    # Fetch uploaded files
    uploaded_files = db.execute(
        "SELECT id, name, size, created_at, notes FROM files WHERE project_id = %s ORDER BY created_at DESC",
        (project_id,)
    ).fetchall()

    # Fetch output files from jobs
    output_files = db.execute(
        "SELECT job_id as id, output_file as name, output_display_name, output_file_size as size, created_at, 'Output from ' || model as notes FROM jobs WHERE project_id = %s AND output_file IS NOT NULL ORDER BY created_at DESC",
        (project_id,)
    ).fetchall()

    # Strip file paths from filenames
    import os
    all_files = []
    for f in uploaded_files:
        file_dict = dict(f)
        # Strip path from name if it exists
        if file_dict.get('name'):
            file_dict['name'] = os.path.basename(file_dict['name'])
        all_files.append(file_dict)

    for f in output_files:
        file_dict = dict(f)
        # Use output_display_name if available, otherwise strip path from output_file
        if file_dict.get('output_display_name'):
            file_dict['name'] = file_dict['output_display_name']
        elif file_dict.get('name'):
            file_dict['name'] = os.path.basename(file_dict['name'])
        all_files.append(file_dict)

    return jsonify({'status': 'success', 'files': all_files})


@app.route('/api/public_projects/<int:project_id>/files/<int:file_id>/content', methods=['GET'])
def get_public_project_file_content(project_id, file_id):
    """Get file content for a public project file."""
    db = get_db()

    # Check if project exists and is public
    project = db.execute(
        'SELECT id, team_id FROM projects WHERE id = %s AND is_private = 0',
        (project_id,)
    ).fetchone()

    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    # Fetch the file
    file = db.execute(
        "SELECT id, name, path, size FROM files WHERE id = %s AND project_id = %s",
        (file_id, project_id)
    ).fetchone()

    if not file:
        return jsonify({'status': 'error', 'message': 'File not found.'}), 404

    try:
        # Read file content
        project_path = get_safe_project_path(project_id, project['team_id'])
        if not project_path:
            return jsonify({'status': 'error', 'message': 'Project storage path unavailable.'}), 404

        file_path = file['path']
        if not is_path_within(file_path, project_path):
            logger.warning("Blocked public project file outside project root: project=%s file=%s", project_id, file_id)
            return jsonify({'status': 'error', 'message': 'Access to file path is forbidden.'}), 403
        if not os.path.exists(file_path):
            return jsonify({'status': 'error', 'message': 'File not found on disk.'}), 404

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        return jsonify({'status': 'success', 'content': content})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/public_projects/<int:project_id>/files/<int:file_id>/download', methods=['GET'])
def download_public_project_file(project_id, file_id):
    """Download a file from a public project."""
    db = get_db()

    # Check if project exists and is public
    project = db.execute(
        'SELECT id, team_id FROM projects WHERE id = %s AND is_private = 0',
        (project_id,)
    ).fetchone()

    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    # Fetch the file
    file = db.execute(
        "SELECT id, name, path, size FROM files WHERE id = %s AND project_id = %s",
        (file_id, project_id)
    ).fetchone()

    if not file:
        return jsonify({'status': 'error', 'message': 'File not found.'}), 404

    try:
        # Send file
        project_path = get_safe_project_path(project_id, project['team_id'])
        if not project_path:
            return jsonify({'status': 'error', 'message': 'Project storage path unavailable.'}), 404

        file_path = file['path']
        if not is_path_within(file_path, project_path):
            logger.warning("Blocked public project file outside project root: project=%s file=%s", project_id, file_id)
            return jsonify({'status': 'error', 'message': 'Access to file path is forbidden.'}), 403
        if not os.path.exists(file_path):
            return jsonify({'status': 'error', 'message': 'File not found on disk.'}), 404

        from flask import send_file
        return send_file(file_path, as_attachment=True, download_name=os.path.basename(file['name']))
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/project_categories', methods=['GET', 'POST'])
@csrf_protect
def manage_project_categories():
    """Get all active project categories or create a new one."""
    db = get_db()

    if request.method == 'GET':
        categories_q = db.execute('''
            SELECT id, name, slug, description, icon, color, display_order
            FROM project_categories
            WHERE is_active = 1
            ORDER BY display_order ASC
        ''').fetchall()

        categories = []
        for row in categories_q:
            cat = dict(row)
            # Get project count for each category
            count = db.execute(
                'SELECT COUNT(*) as count FROM projects WHERE category_id = %s AND is_private = 0 AND is_archived = 0',
                (cat['id'],)
            ).fetchone()['count']
            cat['project_count'] = count
            categories.append(cat)

        return jsonify({'categories': categories})

    elif request.method == 'POST':
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'message': 'Please log in to create categories.'}), 401

        data = request.json or {}
        name = (data.get('name') or '').strip()
        description = (data.get('description') or '').strip()
        icon = (data.get('icon') or '').strip()
        color = (data.get('color') or '#6B7280').strip()

        if not name:
            return jsonify({'status': 'error', 'message': 'Category name is required.'}), 400

        if len(name) > 50:
            return jsonify({'status': 'error', 'message': 'Category name must be 50 characters or less.'}), 400

        # Generate slug from name
        import re
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

        # Check if slug already exists
        existing = db.execute('SELECT id FROM project_categories WHERE slug = %s', (slug,)).fetchone()
        if existing:
            return jsonify({'status': 'error', 'message': 'A category with this name already exists.'}), 400

        # Get max display_order
        max_order = db.execute('SELECT MAX(display_order) as max FROM project_categories').fetchone()['max'] or 0

        try:
            cursor = db.execute('''
                INSERT INTO project_categories (name, slug, description, icon, color, display_order, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, 1)
            ''', (name, slug, description, icon, color, max_order + 1))
            db.commit()

            new_category = {
                'id': cursor.fetchone()['id'],
                'name': name,
                'slug': slug,
                'description': description,
                'icon': icon,
                'color': color,
                'display_order': max_order + 1,
                'project_count': 0
            }

            return jsonify({'status': 'success', 'message': 'Category created successfully!', 'category': new_category}), 201
        except Exception as e:
            logger.error(f"Error creating category: {e}")
            return jsonify({'status': 'error', 'message': 'Failed to create category.'}), 500


@app.route('/api/projects/<int:project_id>/vote', methods=['POST'])
@login_required
@csrf_protect
def vote_on_project(project_id):
    """
    Upvote or downvote a project.
    Body: { "vote_type": 1 for upvote, -1 for downvote, 0 to remove vote }
    """
    db = get_db()
    user_id = session['user_id']
    data = request.json or {}
    vote_type = data.get('vote_type', 0)

    if vote_type not in [-1, 0, 1]:
        return jsonify({'status': 'error', 'message': 'Invalid vote type. Must be -1, 0, or 1.'}), 400

    # Check if project exists and is public
    project = db.execute(
        'SELECT id, vote_score FROM projects WHERE id = %s AND is_private = 0',
        (project_id,)
    ).fetchone()

    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    # Get existing vote
    existing_vote = db.execute(
        'SELECT vote_type FROM project_votes WHERE project_id = %s AND user_id = %s',
        (project_id, user_id)
    ).fetchone()

    old_vote = existing_vote['vote_type'] if existing_vote else 0

    # Calculate score change
    score_delta = vote_type - old_vote

    # Update or insert vote
    if vote_type == 0:
        # Remove vote
        db.execute('DELETE FROM project_votes WHERE project_id = %s AND user_id = %s', (project_id, user_id))
    else:
        # Upsert vote
        db.execute('''
            INSERT INTO project_votes (project_id, user_id, vote_type, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(project_id, user_id) DO UPDATE SET vote_type = %s, updated_at = CURRENT_TIMESTAMP
        ''', (project_id, user_id, vote_type, vote_type))

    # Update project vote score
    new_score = project['vote_score'] + score_delta
    db.execute('UPDATE projects SET vote_score = %s WHERE id = %s', (new_score, project_id))

    db.commit()

    return jsonify({
        'status': 'success',
        'vote_score': new_score,
        'user_vote': vote_type
    })


@app.route('/api/projects/<int:project_id>/comments', methods=['GET', 'POST'])
@csrf_protect
@sanitize_json_inputs({'content': 'html', 'parent_comment_id': 'integer'})
def handle_project_comments(project_id):
    """Get or add comments for a public project."""
    db = get_db()

    # Check if project exists and is public
    project = db.execute(
        'SELECT id FROM projects WHERE id = %s AND is_private = 0',
        (project_id,)
    ).fetchone()

    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    if request.method == 'GET':
        # Fetch comments
        comments_q = db.execute('''
            SELECT
                c.id, c.content, c.vote_score, c.created_at, c.updated_at, c.parent_comment_id,
                c.is_deleted, u.username, u.profile_picture_url
            FROM project_comments c
            LEFT JOIN users u ON c.user_id = u.id
            WHERE c.project_id = %s
            ORDER BY c.created_at ASC
        ''', (project_id,)).fetchall()

        comments = []
        user_id = session.get('user_id')

        for row in comments_q:
            comment = dict(row)
            # Get user's vote on this comment if logged in
            if user_id:
                vote_row = db.execute(
                    'SELECT vote_type FROM project_comment_votes WHERE comment_id = %s AND user_id = %s',
                    (comment['id'], user_id)
                ).fetchone()
                comment['user_vote'] = vote_row['vote_type'] if vote_row else 0
            else:
                comment['user_vote'] = 0

            # Hide content if deleted
            if comment['is_deleted']:
                comment['content'] = '[deleted]'
                comment['username'] = '[deleted]'

            comments.append(comment)

        return jsonify({'comments': comments})

    else:  # POST
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'message': 'You must be logged in to comment.'}), 401

        user_id = session['user_id']
        data = request.json or {}
        content = (data.get('content') or '').strip()
        parent_comment_id = data.get('parent_comment_id')

        if not content:
            return jsonify({'status': 'error', 'message': 'Comment content is required.'}), 400

        if len(content) > 10000:
            return jsonify({'status': 'error', 'message': 'Comment is too long (max 10000 characters).'}), 400

        # Validate parent comment if provided
        if parent_comment_id:
            parent = db.execute(
                'SELECT id FROM project_comments WHERE id = %s AND project_id = %s',
                (parent_comment_id, project_id)
            ).fetchone()
            if not parent:
                return jsonify({'status': 'error', 'message': 'Parent comment not found.'}), 404

        # Insert comment
        result = db.execute('''
            INSERT INTO project_comments (project_id, user_id, parent_comment_id, content)
            VALUES (%s, %s, %s, %s)
        ''', (project_id, user_id, parent_comment_id, content))

        comment_id = result.lastrowid
        db.commit()

        # Fetch the created comment
        new_comment = db.execute('''
            SELECT
                c.id, c.content, c.vote_score, c.created_at, c.updated_at, c.parent_comment_id,
                u.username, u.profile_picture_url
            FROM project_comments c
            LEFT JOIN users u ON c.user_id = u.id
            WHERE c.id = %s
        ''', (comment_id,)).fetchone()

        return jsonify({
            'status': 'success',
            'comment': dict(new_comment)
        }), 201


@app.route('/api/comments/<int:comment_id>/vote', methods=['POST'])
@login_required
@csrf_protect
def vote_on_comment(comment_id):
    """
    Upvote or downvote a comment.
    Body: { "vote_type": 1 for upvote, -1 for downvote, 0 to remove vote }
    """
    db = get_db()
    user_id = session['user_id']
    data = request.json or {}
    vote_type = data.get('vote_type', 0)

    if vote_type not in [-1, 0, 1]:
        return jsonify({'status': 'error', 'message': 'Invalid vote type. Must be -1, 0, or 1.'}), 400

    # Check if comment exists
    comment = db.execute(
        'SELECT id, vote_score FROM project_comments WHERE id = %s',
        (comment_id,)
    ).fetchone()

    if not comment:
        return jsonify({'status': 'error', 'message': 'Comment not found.'}), 404

    # Get existing vote
    existing_vote = db.execute(
        'SELECT vote_type FROM project_comment_votes WHERE comment_id = %s AND user_id = %s',
        (comment_id, user_id)
    ).fetchone()

    old_vote = existing_vote['vote_type'] if existing_vote else 0
    score_delta = vote_type - old_vote

    # Update or insert vote
    if vote_type == 0:
        db.execute('DELETE FROM project_comment_votes WHERE comment_id = %s AND user_id = %s', (comment_id, user_id))
    else:
        db.execute('''
            INSERT INTO project_comment_votes (comment_id, user_id, vote_type)
            VALUES (%s, %s, %s)
            ON CONFLICT(comment_id, user_id) DO UPDATE SET vote_type = %s
        ''', (comment_id, user_id, vote_type, vote_type))

    # Update comment vote score
    new_score = comment['vote_score'] + score_delta
    db.execute('UPDATE project_comments SET vote_score = %s WHERE id = %s', (new_score, comment_id))

    db.commit()

    return jsonify({
        'status': 'success',
        'vote_score': new_score,
        'user_vote': vote_type
    })


@app.route('/api/public_projects/<int:project_id>/jobs', methods=['GET'])
def get_project_jobs(project_id):
    """Get jobs for a public project with statistics."""
    db = get_db()

    # Check if project exists and is public
    project = db.execute(
        'SELECT id, name FROM projects WHERE id = %s AND is_private = 0',
        (project_id,)
    ).fetchone()

    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    # Fetch jobs with statistics
    jobs_q = db.execute('''
        SELECT
            j.id, j.job_id, j.model as model_name, j.name, j.status, j.created_at, j.updated_at,
            j.execution_time, j.output_display_name, j.output_file_size, j.message
        FROM jobs j
        WHERE j.project_id = %s
        ORDER BY j.created_at DESC
        LIMIT 50
    ''', (project_id,)).fetchall()

    jobs = [dict(row) for row in jobs_q]

    # Get job statistics
    stats = db.execute('''
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'running' OR status = 'processing' THEN 1 ELSE 0 END) as running,
            AVG(CASE WHEN execution_time IS NOT NULL THEN execution_time ELSE NULL END) as avg_execution_time
        FROM jobs
        WHERE project_id = %s
    ''', (project_id,)).fetchone()

    return jsonify({
        'jobs': jobs,
        'statistics': dict(stats)
    })


@app.route('/api/public_projects/<job_id>/output', methods=['GET'])
def get_public_job_output(job_id):
    """
    Get job output content for public projects (in-page viewing).
    Returns JSON with output content and metadata.
    """
    db = get_db()

    # Fetch job and verify project is public
    job_row = db.execute('''
        SELECT j.*, p.is_private, p.name as project_name, p.team_id
        FROM jobs j
        JOIN projects p ON j.project_id = p.id
        WHERE j.job_id = %s
    ''', (job_id,)).fetchone()

    if not job_row:
        return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

    job = dict(job_row)

    # Check if project is public
    if job['is_private'] != 0:
        return jsonify({'status': 'error', 'message': 'This project is private.'}), 403

    # Check if job is completed
    if job['status'] != 'completed':
        return jsonify({'status': 'error', 'message': 'Job has not completed yet.'}), 400

    # Get project storage roots
    storage_roots = get_project_storage_roots(job['project_id'], job['team_id'])
    project_path = storage_roots[0] if storage_roots else None

    if not project_path:
        return jsonify({'status': 'error', 'message': 'Project storage not found.'}), 404

    # Find output file
    candidate_paths = []

    stored_path = job.get('output_file')
    if stored_path:
        candidate_paths.append(os.path.abspath(stored_path))

    job_output_dir = resolve_job_branch_path(
        project_path=project_path,
        job_id=job_id,
        output_file=stored_path,
        policy=None,
        branch='main',
        create=False,
    ) or os.path.join(project_path, 'jobs', job_id)
    display_name = job.get('output_display_name')
    if display_name:
        for scan_dir in list_job_artifact_dirs(
            project_path=project_path,
            job_id=job_id,
            output_file=stored_path,
            policy=None,
        ):
            candidate_paths.append(os.path.abspath(os.path.join(scan_dir, display_name)))

    for scan_dir in list_job_artifact_dirs(
        project_path=project_path,
        job_id=job_id,
        output_file=stored_path,
        policy=None,
    ):
        try:
            job_files = sorted(
                (os.path.abspath(os.path.join(scan_dir, entry))
                 for entry in os.listdir(scan_dir)),
                key=os.path.getmtime,
                reverse=True
            )
            candidate_paths.extend(job_files)
        except (FileNotFoundError, PermissionError, OSError):
            pass

    file_path = next(
        (path for path in candidate_paths if os.path.exists(path) and os.path.isfile(path)),
        None
    )

    if not file_path:
        return jsonify({'status': 'error', 'message': 'Job output file not found.'}), 404

    # Security check: ensure file is within allowed roots
    file_path = os.path.abspath(file_path)
    job_outputs_root = os.path.abspath(app.config.get('JOB_OUTPUT_DIR', ''))

    if not file_path.startswith(job_outputs_root):
        return jsonify({'status': 'error', 'message': 'Access denied.'}), 403

    # Read file content
    try:
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()

        _, extension = os.path.splitext(file_path)
        normalized_extension = extension.lower().lstrip('.')

        # Try to decode as text
        content = None
        is_text = True
        encoding = 'utf-8'

        try:
            content = raw_bytes.decode('utf-8')
        except UnicodeDecodeError:
            try:
                content = raw_bytes.decode('latin-1')
                encoding = 'latin-1'
            except:
                is_text = False
                content = f"Binary file ({len(raw_bytes)} bytes). Download to view."

        # Get file metadata
        file_size = len(raw_bytes)
        file_name = os.path.basename(file_path)

        return jsonify({
            'status': 'success',
            'content': content,
            'is_text': is_text,
            'encoding': encoding,
            'file_size': file_size,
            'file_name': file_name,
            'extension': normalized_extension,
            'job': {
                'job_id': job['job_id'],
                'model': job['model'],
                'status': job['status'],
                'created_at': job['created_at'],
                'execution_time': job['execution_time'],
                'project_name': job['project_name']
            }
        })

    except Exception as e:
        logger.error(f"Error reading job output {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to read output file.'}), 500


@app.route('/api/projects/<int:project_id>/fork', methods=['POST'])
@login_required
@csrf_protect
def fork_project(project_id):
    """Fork a public project to the current user's account."""
    db = get_db()
    user_id = session['user_id']

    # Check if source project exists and is public
    source_project = db.execute(
        'SELECT id, name, description, category_id FROM projects WHERE id = %s AND is_private = 0',
        (project_id,)
    ).fetchone()

    if not source_project:
        return jsonify({'status': 'error', 'message': 'Project not found or is private.'}), 404

    # Check if user already forked this project
    existing_fork = db.execute('''
        SELECT p.id FROM projects p
        WHERE p.name LIKE %s AND EXISTS (
            SELECT 1 FROM project_assignments pa
            WHERE pa.project_id = p.id AND pa.user_id = %s AND pa.role = %s
        )
    ''', (f"{source_project['name']} (fork)%", user_id, PROJECT_ROLE_MANAGER)).fetchone()

    if existing_fork:
        return jsonify({'status': 'error', 'message': 'You have already forked this project.'}), 400

    import shutil
    import os
    from pathlib import Path
    from phonos_app.helpers.storage import get_user_storage_state, format_storage_bytes

    # Get user's current storage state
    storage_state = get_user_storage_state(db, user_id)

    # Calculate total size of files to be copied
    source_files = db.execute(
        'SELECT id, name, path, size, notes FROM files WHERE project_id = %s',
        (project_id,)
    ).fetchall()

    source_jobs = db.execute('''
        SELECT job_id, model, sequence, status, message, output_file, output_file_size,
               output_display_name, execution_time, input_file_id, policy
        FROM jobs
        WHERE project_id = %s AND status = 'completed'
    ''', (project_id,)).fetchall()

    total_files_size = sum(max(0, int(f['size'] or 0)) for f in source_files)
    total_jobs_size = sum(max(0, int(j['output_file_size'] or 0)) for j in source_jobs)
    total_size_needed = total_files_size + total_jobs_size

    # Check if user has enough storage quota
    if storage_state['available'] < total_size_needed:
        shortage = total_size_needed - storage_state['available']
        return jsonify({
            'status': 'error',
            'message': f'Insufficient storage. Fork requires {format_storage_bytes(total_size_needed)} but you only have {format_storage_bytes(storage_state["available"])} available. You need {format_storage_bytes(shortage)} more.',
            'required': total_size_needed,
            'available': storage_state['available'],
            'shortage': shortage
        }), 400

    # Create new project
    fork_name = f"{source_project['name']} (fork)"
    fork_description = f"Forked from project #{project_id}\n\n{source_project['description'] or ''}"

    try:
        # Insert new project
        cursor = db.execute('''
            INSERT INTO projects (name, description, category_id, is_private, created_at, updated_at)
            VALUES (%s, %s, %s, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''', (fork_name, fork_description, source_project['category_id']))

        result = cursor.fetchone()


        new_project_id = result["id"] if result else None

        # Assign current user as project manager
        db.execute('''
            INSERT INTO project_assignments (project_id, user_id, role)
            VALUES (%s, %s, %s)
        ''', (new_project_id, user_id, PROJECT_ROLE_MANAGER))

        # Copy files with old_id -> new_id mapping
        file_id_mapping = {}  # Map old file IDs to new file IDs
        files_copied = 0
        total_files_copied_size = 0

        for file_row in source_files:
            source_path = file_row['path']
            if os.path.exists(source_path):
                # Create new path for forked file
                file_name = os.path.basename(source_path)
                dest_dir = os.path.join(os.path.dirname(os.path.dirname(source_path)), f'project_{new_project_id}')
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, file_name)

                # Copy file
                shutil.copy2(source_path, dest_path)

                # Track file size
                file_size = max(0, int(file_row['size'] or 0))
                total_files_copied_size += file_size

                # Insert file record
                cursor = db.execute('''
                    INSERT INTO files (project_id, creator_id, name, path, size, notes, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ''', (new_project_id, user_id, file_row['name'], dest_path, file_size, file_row['notes']))

                file_id_mapping[file_row['id']] = cursor.lastrowid
                files_copied += 1

        # Copy jobs (only completed jobs to preserve outputs)
        import uuid
        jobs_copied = 0
        total_jobs_copied_size = 0

        for job_row in source_jobs:
            # Generate new job ID
            new_job_id = str(uuid.uuid4())

            # Map input_file_id to new file if it exists
            new_input_file_id = file_id_mapping.get(job_row['input_file_id']) if job_row['input_file_id'] else None

            # Copy output file if it exists
            new_output_file = None
            output_size = max(0, int(job_row['output_file_size'] or 0))

            if job_row['output_file'] and os.path.exists(job_row['output_file']):
                output_file_name = os.path.basename(job_row['output_file'])
                new_project_path = get_safe_project_path(new_project_id, None)
                output_dest_dir = None
                if new_project_path:
                    output_dest_dir = resolve_job_branch_path(
                        project_path=new_project_path,
                        job_id=new_job_id,
                        branch='main',
                        create=True,
                    )
                if not output_dest_dir:
                    output_dest_dir = os.path.join(os.path.dirname(os.path.dirname(job_row['output_file'])), f'project_{new_project_id}')
                os.makedirs(output_dest_dir, exist_ok=True)
                new_output_file = os.path.join(output_dest_dir, output_file_name)
                shutil.copy2(job_row['output_file'], new_output_file)

                # Track job output size
                total_jobs_copied_size += output_size

            # Insert job record
            db.execute('''
                INSERT INTO jobs (job_id, project_id, creator_id, model, sequence, status, message,
                                output_file, output_file_size, output_display_name, execution_time,
                                input_file_id, policy, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ''', (new_job_id, new_project_id, user_id, job_row['model'], job_row['sequence'],
                  job_row['status'], job_row['message'], new_output_file, output_size,
                  job_row['output_display_name'], job_row['execution_time'], new_input_file_id,
                  job_row['policy']))

            jobs_copied += 1

        # Update user's storage_used
        total_copied_size = total_files_copied_size + total_jobs_copied_size
        db.execute(
            'UPDATE users SET storage_used = storage_used + %s WHERE id = %s',
            (total_copied_size, user_id)
        )

        db.commit()

        # Get updated storage state
        updated_storage = get_user_storage_state(db, user_id)

        return jsonify({
            'status': 'success',
            'message': f'Project forked successfully! {files_copied} files and {jobs_copied} jobs copied.',
            'project_id': new_project_id,
            'project_name': fork_name,
            'storage_used': format_storage_bytes(total_copied_size),
            'storage_remaining': format_storage_bytes(updated_storage['available'])
        }), 201

    except Exception as e:
        db.rollback()
        logger.error(f"Error forking project {project_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fork project.'}), 500


@app.route('/api/projects/<int:project_id>/assignments', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
def get_project_assignments(project_id):
    """Get project members and pending invitations."""
    db = get_db()
    current_user_id = session['user_id']

    # Get project members
    assignments_q = db.execute(
        '''
        SELECT u.id as user_id, u.username, uc.email, u.legal_name, u.profile_picture_url, pa.role
        FROM users u
        JOIN project_assignments pa ON u.id = pa.user_id
        LEFT JOIN user_credentials uc ON u.id = uc.user_id
        WHERE pa.project_id = %s
        ORDER BY
            CASE
                WHEN pa.role = %s THEN 1
                WHEN pa.role = %s THEN 2
                WHEN pa.role = %s THEN 3
                ELSE 4
            END,
            lower(u.username)
        ''',
        (project_id, PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER),
    ).fetchall()

    assignments = []
    for row in assignments_q:
        assignments.append({
            'user_id': row['user_id'],
            'username': row['username'],
            'email': row['email'],
            'display_name': row['legal_name'] or row['username'],
            'profile_picture_url': row['profile_picture_url'],
            'role': externalize_project_role(row['role']),
        })

    # Get pending invitations
    invitations_q = db.execute(
        '''
        SELECT inv.id, inv.invitee_email, inv.role, inv.created_at, inv.inviter_id,
               inviter.username AS inviter_username,
               inviter.legal_name AS inviter_legal_name
        FROM invitations inv
        LEFT JOIN users inviter ON inviter.id = inv.inviter_id
        WHERE inv.project_id = %s AND inv.status = 'pending'
        ORDER BY inv.created_at DESC
        ''',
        (project_id,),
    ).fetchall()

    pending_invitations = []
    for invitation in invitations_q:
        inviter_name = invitation['inviter_legal_name'] or invitation['inviter_username']
        pending_invitations.append({
            'id': invitation['id'],
            'invitation_id': invitation['id'],
            'email': invitation['invitee_email'],
            'role': externalize_project_role(invitation['role']),
            'status': 'pending',
            'created_at': invitation['created_at'],
            'invited_at': invitation['created_at'],
            'inviter_name': inviter_name,
        })

    # Get collaboration billing model
    project_q = db.execute('SELECT collaboration_billing_model, team_id, organization_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    collaboration_billing_model = project_q['collaboration_billing_model'] if project_q else 'admin_credits'

    # Check if user is a direct project manager
    manager_q = db.execute("SELECT 1 FROM project_assignments WHERE project_id = %s AND user_id = %s AND role = %s", (project_id, current_user_id, PROJECT_ROLE_MANAGER)).fetchone()
    is_owner = True if manager_q else False

    # Also check if user is team owner/admin
    if not is_owner and project_q:
        team_id = project_q['team_id'] or project_q['organization_id']
        if team_id:
            team_role_q = db.execute("SELECT role FROM team_members WHERE team_id = %s AND user_id = %s", (team_id, current_user_id)).fetchone()
            if team_role_q and team_role_q['role'] in ('owner', 'admin'):
                is_owner = True

    return jsonify({
        'members': assignments,
        'pending_invitations': pending_invitations,
        'roster': {'members': assignments, 'pending_invitations': pending_invitations},
        'is_owner': is_owner,
        'collaboration_billing_model': collaboration_billing_model
    })


@app.route('/api/projects/<int:project_id>/assignments', methods=['PATCH', 'DELETE'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def update_project_assignment(project_id):
    db = get_db()
    user_id = session['user_id']

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    data = request.json or {}
    target_user_id = data.get('user_id')
    if not target_user_id:
        return jsonify({'status': 'error', 'message': 'User ID is required.'}), 400

    if request.method == 'PATCH':
        new_role = data.get('role')
        normalized_new_role = normalize_project_role(new_role)

        if not normalized_new_role or normalized_new_role not in PROJECT_MUTABLE_ROLES:
            return jsonify({'status': 'error', 'message': 'Invalid role specified.'}), 400

        # Prevent changing the role of the project creator if they are the only manager
        current_assignment = db.execute('SELECT role FROM project_assignments WHERE project_id = %s AND user_id = %s', (project_id, target_user_id)).fetchone()
        if current_assignment and current_assignment['role'] == PROJECT_ROLE_MANAGER:
            other_managers = db.execute('SELECT 1 FROM project_assignments WHERE project_id = %s AND role = %s AND user_id != %s', (project_id, PROJECT_ROLE_MANAGER, target_user_id)).fetchone()
            if not other_managers and normalized_new_role != PROJECT_ROLE_MANAGER:
                return jsonify({'status': 'error', 'message': 'Cannot change the role of the sole project manager.'}), 403

        db.execute(
            'UPDATE project_assignments SET role = %s WHERE project_id = %s AND user_id = %s',
            (normalized_new_role, project_id, target_user_id)
        )
        db.commit()

        updated_assignment = db.execute(
            '''
            SELECT u.id as user_id, u.username, uc.email, u.legal_name, u.profile_picture_url, pa.role
            FROM users u
            JOIN project_assignments pa ON u.id = pa.user_id
            LEFT JOIN user_credentials uc ON u.id = uc.user_id
            WHERE pa.project_id = %s AND pa.user_id = %s
            ''',
            (project_id, target_user_id)
        ).fetchone()

        return jsonify({
            'status': 'success',
            'message': 'Project assignment updated.',
            'assignment': {
                'user_id': updated_assignment['user_id'],
                'username': updated_assignment['username'],
                'email': updated_assignment['email'],
                'legal_name': updated_assignment['legal_name'],
                'profile_picture_url': updated_assignment['profile_picture_url'],
                'role': externalize_project_role(updated_assignment['role']),
                'display_name': updated_assignment['legal_name'] or updated_assignment['username'],
            }
        })

    if request.method == 'DELETE':
        # Prevent removing the project creator if they are the only manager
        current_assignment = db.execute('SELECT role FROM project_assignments WHERE project_id = %s AND user_id = %s', (project_id, target_user_id)).fetchone()
        if current_assignment and current_assignment['role'] == PROJECT_ROLE_MANAGER:
            other_managers = db.execute('SELECT 1 FROM project_assignments WHERE project_id = %s AND role = %s AND user_id != %s', (project_id, PROJECT_ROLE_MANAGER, target_user_id)).fetchone()
            if not other_managers:
                return jsonify({'status': 'error', 'message': 'Cannot remove the sole project manager.'}), 403

        db.execute('DELETE FROM project_assignments WHERE project_id = %s AND user_id = %s', (project_id, target_user_id))
        db.commit()
        return jsonify({'status': 'success', 'message': 'User removed from project.'})


@app.route('/api/projects/<int:project_id>/invitations/<int:invitation_id>', methods=['DELETE'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def delete_project_invitation(project_id, invitation_id):
    db = get_db()
    invitation = db.execute(
        'SELECT invitee_email, status FROM invitations WHERE id = %s AND project_id = %s',
        (invitation_id, project_id),
    ).fetchone()
    if not invitation:
        return jsonify({'status': 'error', 'message': 'Invitation not found.'}), 404

    if invitation['status'] != 'pending':
        return jsonify({'status': 'error', 'message': 'Only pending invitations can be cancelled.'}), 400

    db.execute('UPDATE invitations SET status = %s, accepted_at = NULL WHERE id = %s', ('cancelled', invitation_id))

    # Fully retract the invite: remove the invitee's pending-invite notification and
    # push a live update so it disappears from their dashboard / notification bell.
    # The accept/decline/landing routes already gate on status='pending', so flipping
    # the status above already invalidates the invitation link itself.
    invitee = db.execute(
        'SELECT u.id AS user_id FROM users u JOIN user_credentials uc ON u.user_credential_id = uc.id '
        'WHERE lower(uc.email) = lower(%s)',
        (invitation['invitee_email'],),
    ).fetchone()
    if invitee:
        db.execute(
            "DELETE FROM notifications WHERE user_id = %s AND category = 'project' "
            "AND metadata IS NOT NULL AND metadata::jsonb->>'invitation_id' = %s",
            (invitee['user_id'], str(invitation_id)),
        )

    db.commit()

    if invitee:
        socketio.emit('notification_update', room=f"user_{invitee['user_id']}")

    return jsonify({'status': 'success', 'message': 'Invitation cancelled.'})


@app.route('/api/projects/<int:project_id>/assignments/<int:user_id>', methods=['DELETE'])
@login_required
@csrf_protect
def remove_project_assignment(project_id, user_id):
    db = get_db()
    current_user_id = session['user_id']

    # Check if the user to be removed is assigned to the project
    assignment_to_remove = db.execute('SELECT role FROM project_assignments WHERE project_id = %s AND user_id = %s',
                                  (project_id, user_id)).fetchone()
    if not assignment_to_remove:
        return jsonify({'status': 'error', 'message': 'User not found in this project.'}), 404

    # Check permissions:
    # - Users can always remove themselves (leave project)
    # - Only managers can remove other users
    is_self_removal = (current_user_id == user_id)
    if not is_self_removal:
        # Removing another user requires manager role
        current_user_role = get_project_assignment(db, current_user_id, project_id)
        if not current_user_role or normalize_project_role(current_user_role) != PROJECT_ROLE_MANAGER:
            return jsonify({'status': 'error', 'message': 'Only project managers can remove other users.'}), 403

    # Prevent the manager from removing themselves if they are the only one
    if assignment_to_remove['role'] == PROJECT_ROLE_MANAGER:
        manager_count = db.execute("SELECT COUNT(*) as count FROM project_assignments WHERE project_id = %s AND role = %s",
                                   (project_id, PROJECT_ROLE_MANAGER)).fetchone()['count']
        if manager_count <= 1:
            return jsonify({'status': 'error', 'message': 'Cannot remove the last manager from the project.'}), 400

    # Remove the assignment
    db.execute('DELETE FROM project_assignments WHERE project_id = %s AND user_id = %s', (project_id, user_id))
    db.commit()

    return jsonify({'status': 'success', 'message': 'User removed from project successfully.'})


@app.route('/api/projects/<int:project_id>/assignments/<int:user_id>', methods=['PATCH'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def update_assignment_role(project_id, user_id):
    db = get_db()
    current_user_id = session['user_id']
    data = request.json or {}
    requested_role = data.get('role')
    normalized_role = normalize_project_role(requested_role)

    # Allow Manager, Contributor, and Viewer roles
    allowed_roles = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER}
    if not normalized_role or normalized_role not in allowed_roles:
        return jsonify({'status': 'error', 'message': 'Invalid role specified.'}), 400

    # Prevent users from changing their own role
    if current_user_id == user_id:
        return jsonify({'status': 'error', 'message': 'You cannot change your own role.'}), 400

    # Update the user's role
    db.execute('UPDATE project_assignments SET role = %s WHERE project_id = %s AND user_id = %s',
               (normalized_role, project_id, user_id))
    db.commit()

    return jsonify({'status': 'success', 'message': 'User role updated successfully.'})


@app.route('/api/projects/<int:project_id>/invitations/<int:invitation_id>/resend', methods=['POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def resend_project_invitation(project_id, invitation_id):
    db = get_db()
    invitation = db.execute(
        'SELECT invitee_email, role, inviter_id, status FROM invitations WHERE id = %s AND project_id = %s',
        (invitation_id, project_id),
    ).fetchone()

    if not invitation:
        return jsonify({'status': 'error', 'message': 'Invitation not found.'}), 404
    if invitation['status'] != 'pending':
        return jsonify({'status': 'error', 'message': 'Only pending invitations can be resent.'}), 400

    new_token = secrets.token_urlsafe(32)
    db.execute(
        'UPDATE invitations SET token = %s, created_at = CURRENT_TIMESTAMP WHERE id = %s',
        (new_token, invitation_id),
    )

    invitee = db.execute(
        'SELECT u.id as user_id FROM users u JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE lower(uc.email) = lower(%s)',
        (invitation['invitee_email'],),
    ).fetchone()

    project_row = db.execute('SELECT name FROM projects WHERE id = %s', (project_id,)).fetchone()
    inviter_row = db.execute('SELECT username FROM users WHERE id = %s', (invitation['inviter_id'],)).fetchone()
    project_name = project_row['name'] if project_row else 'Project'
    inviter_name = inviter_row['username'] if inviter_row else 'A teammate'

    if invitee:
        role_display = externalize_project_role(invitation['role']) or invitation['role']
        create_user_notification(
            db,
            invitee['user_id'],
            'Project invitation resent',
            f"{inviter_name} resent an invitation to join {project_name} as a {role_display}.",
            category='project',
        )

    db.commit()

    invitation_link = url_for('accept_invitation', token=new_token, _external=True)
    role_display = externalize_project_role(invitation['role']) or invitation['role']
    email_subject = f"You're invited to collaborate on '{project_name}'"
    if invitee:
        email_body = (
            f"<p>You have been invited by {inviter_name} to join the project '{project_name}' as a {role_display}.</p>"
            f"<p>Use the updated link below to accept the invitation:</p>"
            f"<a href='{invitation_link}'>Accept Invitation</a>"
        )
    else:
        # Mirrors _create_single_invitation's unregistered-invitee branch: the
        # invitee never had an account to send an "Accept Invitation" link to
        # in the first place, so resending that variant would just be a dead
        # link for them -- point them at sign-up instead.
        signup_link = url_for('register', _external=True)
        email_body = (
            f"<p>{inviter_name} invited you to join '{project_name}' as a {role_display} on Phonos.</p>"
            f"<p>Create your account using this email address (<strong>{invitation['invitee_email']}</strong>) to get started:</p>"
            f"<p><a href='{signup_link}'>Create your Phonos account</a></p>"
            f"<p>After you sign in, the invitation will be waiting on your dashboard. You can also open it "
            f"directly once signed in:</p>"
            f"<a href='{invitation_link}'>Accept Invitation</a>"
        )
    _send_project_email(invitation['invitee_email'], email_subject, email_body)

    return jsonify({'status': 'success', 'message': 'Invitation resent.'})


@app.route('/api/invitations/pending', methods=['GET'])
@login_required
def get_pending_invitations_api():
    """Get all pending invitations for the current user."""
    from phonos_app.helpers.collaboration import get_pending_invitations

    user_id = session['user_id']
    invitations = get_pending_invitations(user_id)

    return jsonify(invitations)


@app.route('/api/invitations/<int:invitation_id>/accept', methods=['POST'])
@login_required
@csrf_protect
def accept_invitation_api(invitation_id):
    """Accept a project invitation from the dashboard."""
    from phonos_app.helpers.activity_log import log_activity
    from phonos_app.helpers.notifications import create_user_notification

    db = get_db()
    user_id = session['user_id']

    # Get user email
    user_email_row = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()
    if not user_email_row:
        return jsonify({'status': 'error', 'message': 'User not found.'}), 404

    user_email = user_email_row['email']

    # Get invitation
    invitation = db.execute(
        'SELECT * FROM invitations WHERE id = %s AND status = %s',
        (invitation_id, 'pending')
    ).fetchone()

    if not invitation:
        return jsonify({'status': 'error', 'message': 'Invalid or expired invitation.'}), 404

    # Verify invitation is for this user
    if user_email.lower() != invitation['invitee_email'].lower():
        return jsonify({'status': 'error', 'message': 'This invitation is for a different user.'}), 403

    # Add user to project assignments
    db.execute(
        'INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, %s) '
        'ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role',
        (invitation['project_id'], user_id, invitation['role']),
    )

    # Update invitation status
    db.execute(
        'UPDATE invitations SET status = %s, accepted_at = CURRENT_TIMESTAMP WHERE id = %s',
        ('accepted', invitation_id),
    )

    # Get project name for notifications
    project_row = db.execute('SELECT name FROM projects WHERE id = %s', (invitation['project_id'],)).fetchone()
    project_name = project_row['name'] if project_row else 'Project'
    role_display = externalize_project_role(invitation['role']) or invitation['role']

    # Log activity
    log_activity(
        db=db,
        user_id=user_id,
        activity_type='project_invitation_accepted',
        activity_description=f'Joined project {project_name} as {role_display}',
        resource_type='project',
        resource_id=invitation['project_id'],
        metadata={'role': invitation['role'], 'project_name': project_name}
    )

    # Notify inviter
    create_user_notification(
        db,
        invitation['inviter_id'],
        'Invitation accepted',
        f"{session['username']} accepted the invitation to join {project_name} as a {role_display}.",
        category='project',
    )

    db.commit()

    return jsonify({'status': 'success', 'message': f'You have joined {project_name}!'})


@app.route('/api/invitations/<int:invitation_id>/decline', methods=['POST'])
@login_required
@csrf_protect
def decline_invitation_api(invitation_id):
    """Decline a project invitation from the dashboard."""
    from phonos_app.helpers.notifications import create_user_notification

    db = get_db()
    user_id = session['user_id']

    # Get user email
    user_email_row = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()
    if not user_email_row:
        return jsonify({'status': 'error', 'message': 'User not found.'}), 404

    user_email = user_email_row['email']

    # Get invitation
    invitation = db.execute(
        'SELECT * FROM invitations WHERE id = %s AND status = %s',
        (invitation_id, 'pending')
    ).fetchone()

    if not invitation:
        return jsonify({'status': 'error', 'message': 'Invalid or expired invitation.'}), 404

    # Verify invitation is for this user
    if user_email.lower() != invitation['invitee_email'].lower():
        return jsonify({'status': 'error', 'message': 'This invitation is for a different user.'}), 403

    # Update invitation status
    db.execute(
        'UPDATE invitations SET status = %s WHERE id = %s',
        ('declined', invitation_id),
    )

    # Get project name for notification
    project_row = db.execute('SELECT name FROM projects WHERE id = %s', (invitation['project_id'],)).fetchone()
    project_name = project_row['name'] if project_row else 'Project'
    role_display = externalize_project_role(invitation['role']) or invitation['role']

    # Notify inviter
    create_user_notification(
        db,
        invitation['inviter_id'],
        'Invitation declined',
        f"{session['username']} declined the invitation to join {project_name}.",
        category='project',
    )

    db.commit()

    return jsonify({'status': 'success', 'message': 'Invitation declined.'})


@app.route('/invitations/accept/<token>')
@login_required
def accept_invitation(token):
    from phonos_app.helpers.activity_log import log_activity

    db = get_db()
    invitation = db.execute('SELECT * FROM invitations WHERE token = %s AND status = %s', (token, 'pending')).fetchone()

    if not invitation:
        return render_template('message.html', message='Invalid or expired invitation token.'), 404

    user_id = session['user_id']
    user_email = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()['email']

    if user_email.lower() != invitation['invitee_email'].lower():
        return render_template('message.html', message='This invitation is for a different user.'), 403

    # Add user to project assignments
    db.execute(
        'INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, %s) '
        'ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role',
        (invitation['project_id'], user_id, invitation['role']),
    )
    # Update invitation status
    db.execute(
        'UPDATE invitations SET status = %s, accepted_at = CURRENT_TIMESTAMP WHERE id = %s',
        ('accepted', invitation['id']),
    )

    # Get project name for notifications and activity log
    project_row = db.execute('SELECT name FROM projects WHERE id = %s', (invitation['project_id'],)).fetchone()
    project_name = project_row['name'] if project_row else 'Project'
    role_display = externalize_project_role(invitation['role']) or invitation['role']

    # Log activity to dashboard operations log
    log_activity(
        db=db,
        user_id=user_id,
        activity_type='project_invitation_accepted',
        activity_description=f'Joined project {project_name} as {role_display}',
        resource_type='project',
        resource_id=invitation['project_id'],
        metadata={'role': invitation['role'], 'project_name': project_name}
    )

    inviter = db.execute('SELECT inviter_id FROM invitations WHERE id = %s', (invitation['id'],)).fetchone()
    if inviter:
        create_user_notification(
            db,
            inviter['inviter_id'],
            'Invitation accepted',
            f"{session['username']} accepted the invitation to join {project_name} as a {role_display}.",
            category='project',
        )
    db.commit()

    return redirect(url_for('dashboard'))


@app.route('/invitations/decline/<token>')
def decline_invitation(token):
    db = get_db()
    invitation = db.execute('SELECT * FROM invitations WHERE token = %s AND status = %s', (token, 'pending')).fetchone()

    if not invitation:
        return render_template('message.html', message='Invalid or expired invitation token.'), 404

    if 'user_id' not in session:
        session['redirect_after_login'] = url_for('decline_invitation', token=token)
        return redirect(url_for('login'))

    user_id = session['user_id']
    user_email = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()['email']
    if user_email.lower() != invitation['invitee_email'].lower():
        return render_template('message.html', message='This invitation is for a different user.'), 403

    db.execute('UPDATE invitations SET status = %s WHERE id = %s', ('declined', invitation['id']))
    db.commit()

    return render_template('message.html', message='You have declined the invitation.')


@app.route('/api/projects/<int:project_id>', methods=['PATCH'])
@login_required
# Project contributors should be able to help maintain project metadata like
# the name or description. Restricting edits to managers only made
# collaboration awkward, so we allow both managers and contributors here.
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def update_project(project_id):
    """
    Updates a project's name and description. 'Project Manager' and
    'Project Contributor' roles can edit.
    """
    db = get_db()
    project_info = db.execute('SELECT id, name, description, notes, team_id, auto_open_chat FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project_info:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    data = request.json or {}
    new_name = sanitize_input(data.get('name', project_info['name']).strip(), max_length=200)
    new_description = sanitize_html(data.get('description', project_info['description'] or '').strip())
    new_notes = sanitize_html(data.get('notes', project_info['notes'] or '').strip()) if 'notes' in data else None
    new_billing_model = data.get('collaboration_billing_model')
    new_auto_open_chat = data.get('auto_open_chat')

    if not new_name:
        return jsonify({'status': 'error', 'message': 'Project name cannot be empty.'}), 400

    if new_billing_model and new_billing_model not in ['admin_credits', 'collaborator_credits']:
        return jsonify({'status': 'error', 'message': 'Invalid collaboration billing model.'}), 400

    # Check for name uniqueness within the team's projects
    team_id = project_info['team_id']
    if new_name != project_info['name']:
        if db.execute('SELECT 1 FROM projects WHERE team_id = %s AND name = %s AND id != %s', (team_id, new_name, project_id)).fetchone():
            return jsonify({'status': 'error', 'message': f'A project named "{new_name}" already exists in this team.'}), 409

    update_fields = ["name = %s", "description = %s"]
    update_params = [new_name, new_description]

    if new_notes is not None:
        update_fields.append("notes = %s")
        update_params.append(new_notes)

    if new_billing_model:
        update_fields.append("collaboration_billing_model = %s")
        update_params.append(new_billing_model)

    if new_auto_open_chat is not None:
        # Convert to integer 0 or 1 for SQLite boolean
        auto_open_chat_value = 1 if new_auto_open_chat else 0
        update_fields.append("auto_open_chat = %s")
        update_params.append(auto_open_chat_value)

    update_params.append(project_id)

    db.execute(f"UPDATE projects SET {', '.join(update_fields)} WHERE id = %s", tuple(update_params))
    db.commit()
    return jsonify({'status': 'success', 'message': 'Project updated successfully.'})


@socketio.on('join_project_room')
def handle_join_project_room(data):
    project_id = data.get('project_id')
    user_id = session.get('user_id')
    db = get_db()
    if get_project_assignment(db, user_id, project_id):
        join_room(f"project_{project_id}")
        logger.info(f"[SOCKETIO] User {user_id} joined project room {project_id}")


@app.route('/api/direct_messages/<int:recipient_id>', methods=['GET'])
@login_required
def get_direct_messages(recipient_id):
    db = get_db()
    sender_id = session['user_id']

    messages_q = db.execute('''
        SELECT dm.id, dm.content, dm.created_at, dm.sender_id, dm.message_type,
               dm.attachment_url, dm.attachment_name, dm.attachment_size,
               s.username as sender_username, r.username as recipient_username
        FROM direct_messages dm
        JOIN users s ON dm.sender_id = s.id
        JOIN users r ON dm.recipient_id = r.id
        WHERE (dm.sender_id = %s AND dm.recipient_id = %s) OR (dm.sender_id = %s AND dm.recipient_id = %s)
        ORDER BY dm.created_at ASC
    ''', (sender_id, recipient_id, recipient_id, sender_id)).fetchall()

    messages = [dict(row) for row in messages_q]
    for m in messages:
        m['content'] = data_crypto.unseal_or_placeholder(DM_CONTENT, m['content'])
    return jsonify(messages)

@socketio.on('send_direct_message')
def handle_send_direct_message(data):
    recipient_id = data.get('recipient_id')
    content = data.get('content')
    message_type = data.get('message_type', 'text')
    attachment_url = data.get('attachment_url')
    attachment_name = data.get('attachment_name')
    attachment_size = data.get('attachment_size')
    sender_id = session.get('user_id')
    db = get_db()

    if not all([recipient_id, sender_id]):
        return

    if not content and not attachment_url:
        return

    # Sanitize message content for XSS protection
    sanitized_content = sanitize_html(content) if content else ''

    cursor = db.execute('''INSERT INTO direct_messages
                          (sender_id, recipient_id, content, message_type, attachment_url, attachment_name, attachment_size)
                          VALUES (%s, %s, %s, %s, %s, %s, %s)''',
               (sender_id, recipient_id, data_crypto.seal(DM_CONTENT, sanitized_content), message_type, attachment_url, attachment_name, attachment_size))
    db.commit()
    result = cursor.fetchone()

    message_id = result["id"] if result else None

    new_message_q = db.execute('''
        SELECT dm.id, dm.content, dm.created_at, dm.sender_id, dm.message_type,
               dm.attachment_url, dm.attachment_name, dm.attachment_size,
               s.username as sender_username, r.username as recipient_username
        FROM direct_messages dm
        JOIN users s ON dm.sender_id = s.id
        JOIN users r ON dm.recipient_id = r.id
        WHERE dm.id = %s
    ''', (message_id,)).fetchone()

    if new_message_q:
        sender_room = f"user_{sender_id}"
        recipient_room = f"user_{recipient_id}"
        new_message = dict(new_message_q)
        new_message['content'] = data_crypto.unseal_or_placeholder(DM_CONTENT, new_message['content'])
        socketio.emit('new_direct_message', new_message, room=sender_room)
        socketio.emit('new_direct_message', new_message, room=recipient_room)

@app.route('/api/direct-messages/upload', methods=['POST'])
@login_required
@csrf_protect
def upload_dm_attachment():
    """Upload an attachment for direct messages"""
    user_id = session['user_id']

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part in the request.'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file.'}), 400

    # Define allowed extensions for DM attachments
    DM_ALLOWED_EXTENSIONS = {
        'txt', 'pdf', 'doc', 'docx', 'xls', 'xlsx',
        'png', 'jpg', 'jpeg', 'gif', 'bmp', 'svg',
        'zip', 'tar', 'gz',
        'pdb', 'cif', 'mol2', 'sdf', 'fasta', 'fa'
    }

    if file and allowed_file(file.filename, DM_ALLOWED_EXTENSIONS):
        filename = secure_filename(file.filename)

        # Create uploads directory for DMs if it doesn't exist
        upload_dir = os.path.join(Config.UPLOAD_FOLDER, 'dm_attachments', str(user_id))
        os.makedirs(upload_dir, exist_ok=True)

        # Generate unique filename to prevent conflicts
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        file_path = os.path.join(upload_dir, unique_filename)

        # Check file size
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        # Check storage quota
        db = get_db()
        storage_state = get_user_storage_state(db, user_id)

        if storage_state['available'] <= 0:
            return jsonify({
                'status': 'error',
                'message': 'You have no storage remaining.',
                'storage': storage_state
            }), 507

        if file_size > storage_state['available']:
            return jsonify({
                'status': 'error',
                'message': f"File size exceeds available storage.",
                'storage': storage_state
            }), 507

        # Save file
        try:
            # Save to temporary location first
            temp_path = file_path + '.tmp'
            file.save(temp_path)

            # Scan the file
            is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
            if not is_safe:
                # temp_path is automatically deleted by scan_upload
                return jsonify({'status': 'error', 'message': message}), 400

            # Move to final location only if safe
            os.rename(temp_path, file_path)

            # Update user storage
            db.execute('UPDATE users SET storage_used = storage_used + %s WHERE id = %s', (file_size, user_id))
            db.commit()

            # Return the file URL (relative path for security)
            file_url = f"/api/direct-messages/attachments/{user_id}/{unique_filename}"

            return jsonify({
                'status': 'success',
                'attachment_url': file_url,
                'attachment_name': filename,
                'attachment_size': file_size
            }), 201

        except Exception as e:
            logger.error(f"Failed to upload DM attachment for user {user_id}: {e}", exc_info=True)
            if os.path.exists(file_path):
                os.remove(file_path)
            return jsonify({'status': 'error', 'message': 'Failed to upload file.'}), 500
    else:
        return jsonify({'status': 'error', 'message': 'File type not allowed.'}), 400

@app.route('/api/direct-messages/attachments/<int:user_id>/<filename>')
@login_required
def get_dm_attachment(user_id, filename):
    """Download a DM attachment"""
    sender_id = session['user_id']

    # Check if user has access to this attachment (must be sender or have received messages from this user)
    db = get_db()
    access_check = db.execute('''
        SELECT COUNT(*) as count FROM direct_messages
        WHERE (sender_id = %s OR recipient_id = %s)
        AND (sender_id = %s OR recipient_id = %s)
    ''', (sender_id, sender_id, user_id, user_id)).fetchone()

    if not access_check or access_check['count'] == 0:
        return jsonify({'status': 'error', 'message': 'Access denied.'}), 403

    # Serve the file
    upload_dir = os.path.join(Config.UPLOAD_FOLDER, 'dm_attachments', str(user_id))
    return send_from_directory(upload_dir, filename)
