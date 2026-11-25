"""API routes for teams and organizations."""
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


def _normalize_team_role(role: str) -> str:
    mapping = {
        'owner': TEAM_ROLE_OWNER,
        'admin': TEAM_ROLE_ADMIN,
        'member': TEAM_ROLE_MEMBER,
        'billing': TEAM_ROLE_BILLING,
    }
    normalized = (role or '').strip().lower()
    return mapping.get(normalized)


def _send_team_email(*args, **kwargs):
    """Route test-mode email sends through the consolidated API module mock."""
    if app.config.get('TESTING'):
        from . import api as api_routes
        return api_routes.send_email(*args, **kwargs)
    return send_email(*args, **kwargs)


def _get_team_redis_connection():
    """Route test-mode Redis access through the consolidated API module mock."""
    if app.config.get('TESTING'):
        from . import api as api_routes
        return api_routes.get_redis_connection()
    return get_redis_connection()


def _get_team_member_rows(db, team_id: int):
    return db.execute(
        '''
        SELECT om.user_id, om.role, om.joined_at, u.username, u.legal_name, u.profile_picture_url
        FROM team_members om
        JOIN users u ON om.user_id = u.id
        WHERE om.team_id = %s
        ORDER BY CASE om.role WHEN %s THEN 0 WHEN %s THEN 1 ELSE 2 END, u.username
        ''',
        (team_id, TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN)
    ).fetchall()


@app.route('/api/teams', methods=['GET', 'POST'])
@login_required
@csrf_protect
def handle_teams():
    db = get_db()
    user_id = session['user_id']
    has_team_access = user_has_team_plan(db, user_id)

    if request.method == 'POST':
        if not has_team_access:
            return jsonify({'status': 'error', 'message': 'Team management requires a Managed Premium or Enterprise subscription.'}), 403
        data = request.json or {}
        name = sanitize_input((data.get('name') or '').strip(), max_length=200)
        description = sanitize_html((data.get('description') or '').strip()) if data.get('description') else None

        if len(name) < 3:
            return jsonify({'status': 'error', 'message': 'Team name must be at least 3 characters.'}), 400

        slug = generate_unique_team_slug(db, name)

        db.execute('BEGIN')
        try:
            cursor = db.execute(
                'INSERT INTO teams (name, slug, description, creator_id) VALUES (%s, %s, %s, %s)',
                (name, slug, description, user_id)
            )
            result = cursor.fetchone()

            team_id = result["id"] if result else None
            db.execute(
                'INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, %s)',
                (team_id, user_id, TEAM_ROLE_OWNER)
            )
            db.commit()
        except psycopg2.IntegrityError:
            db.rollback()
            return jsonify({'status': 'error', 'message': 'A team with similar details already exists.'}), 409
        except Exception:
            db.rollback()
            logger.error('Failed to create team for user %s', user_id, exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to create team.'}), 500

        team_row = db.execute(
            '''
            SELECT t.id, t.name, t.slug, t.description, t.creator_id, t.created_at, t.updated_at, tm.role,
                   (
                       SELECT COUNT(*) FROM team_nodes tnodes WHERE tnodes.team_id = t.id
                   ) AS home_node_count,
                   (
                       SELECT node_id
                       FROM team_nodes tnodes
                       WHERE tnodes.team_id = t.id AND tnodes.is_primary = 1
                       ORDER BY tnodes.created_at ASC
                       LIMIT 1
                   ) AS primary_node_id
            FROM teams t
            JOIN team_members tm ON t.id = tm.team_id
            WHERE t.id = %s AND tm.user_id = %s
            ''',
            (team_id, user_id)
        ).fetchone()
        team = serialize_team(team_row)
        team['role'] = (team_row['role'] or '').lower()
        return jsonify(team), 201

    teams_rows = db.execute(
        '''
        SELECT t.id, t.name, t.slug, t.description, t.creator_id, t.created_at, t.updated_at, tm.role,
               (
                   SELECT COUNT(*) FROM team_nodes onodes WHERE onodes.team_id = t.id
               ) AS home_node_count,
               (
                   SELECT node_id
                   FROM team_nodes onodes
                   WHERE onodes.team_id = t.id AND onodes.is_primary = 1
                   ORDER BY onodes.created_at ASC
                   LIMIT 1
               ) AS primary_node_id
        FROM teams t
        JOIN team_members tm ON t.id = tm.team_id
        WHERE tm.user_id = %s
        ORDER BY t.created_at DESC
        ''',
        (user_id,)
    ).fetchall()
    teams = []
    for row in teams_rows:
        team = serialize_team(row)
        team['role'] = (row['role'] or '').lower()
        teams.append(team)
    return jsonify(teams)


@app.route('/api/organizations', methods=['GET', 'POST'])
@login_required
@csrf_protect
def handle_organizations():
    return handle_teams()


@app.route('/api/teams/<int:team_id>', methods=['GET'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN, TEAM_ROLE_MEMBER})
def get_team_detail(team_id):
    db = get_db()
    team_row = db.execute(
        'SELECT id, name, slug, description, creator_id, created_at, updated_at FROM teams WHERE id = %s',
        (team_id,)
    ).fetchone()
    if not team_row:
        return jsonify({'status': 'error', 'message': 'Team not found.'}), 404

    members_rows = _get_team_member_rows(db, team_id)
    members = [serialize_team_member(row) for row in members_rows]

    team = serialize_team(team_row)
    team['role'] = (g.current_team_role or '').lower()
    return jsonify({'team': team, 'members': members})


@app.route('/api/teams/<int:team_id>/members', methods=['GET'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN, TEAM_ROLE_MEMBER})
def get_team_members(team_id):
    db = get_db()
    members_rows = _get_team_member_rows(db, team_id)
    members = [serialize_team_member(row) for row in members_rows]
    return jsonify(members)


@app.route('/api/organizations/<int:organization_id>', methods=['GET'])
def get_organization_detail(organization_id):
    return get_team_detail(organization_id)


@app.route('/api/organizations/<int:organization_id>/members', methods=['GET', 'POST'])
@csrf_protect
def handle_organization_members(organization_id):
    if request.method == 'GET':
        return get_team_members(organization_id)
    return create_team_member(organization_id)


@app.route('/api/teams/<int:team_id>/nodes', methods=['GET', 'POST'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN, TEAM_ROLE_MEMBER})
@csrf_protect
def manage_team_nodes(team_id):
    db = get_db()
    role = g.current_team_role

    if request.method == 'GET':
        rows = get_team_node_rows(db, team_id)
        nodes = [serialize_team_node(row) for row in rows]
        return jsonify(nodes)

    if role not in {TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN}:
        return jsonify({'status': 'error', 'message': 'You do not have permission to register home nodes for this team.'}), 403

    if not team_is_enterprise(db, team_id):
        return jsonify({'status': 'error', 'message': 'Home nodes are available only to Enterprise teams.'}), 403

    data = request.json or {}
    node_id = (data.get('node_id') or '').strip()
    display_name = (data.get('display_name') or '').strip() or None
    if display_name and len(display_name) > 80:
        return jsonify({'status': 'error', 'message': 'Display name must be 80 characters or fewer.'}), 400

    if not NODE_ID_PATTERN.match(node_id):
        return jsonify({'status': 'error', 'message': 'Node identifier must be 4-64 characters using letters, numbers, hyphens, or underscores.'}), 400
    if node_id.lower().startswith('publisher-') or node_id in Config.PUBLISHER_NODES:
        return jsonify({'status': 'error', 'message': 'Node identifier collides with a reserved publisher node.'}), 400

    worker_types_input = data.get('worker_types') or []
    if isinstance(worker_types_input, str):
        worker_types_input = [chunk.strip() for chunk in worker_types_input.split(',')]
    elif not isinstance(worker_types_input, Iterable):
        worker_types_input = []
    worker_types = normalize_worker_types(worker_types_input)
    if not worker_types:
        return jsonify({'status': 'error', 'message': 'Select at least one supported worker type for the node.'}), 400

    public_key = (data.get('public_key') or '').strip()
    if not is_valid_public_key(public_key):
        return jsonify({'status': 'error', 'message': 'A valid PEM-formatted public key is required.'}), 400

    is_primary = bool(data.get('is_primary'))

    db.execute('BEGIN')
    try:
        # 1. Register the node's public key if it's not already there.
        # It's keyed by node_id, so we can use INSERT ... ON CONFLICT DO NOTHING.
        db.execute(
            'INSERT INTO customer_nodes (user_id, node_id, public_key) VALUES (%s, %s, %s) ON CONFLICT (node_id) DO NOTHING',
            (session['user_id'], node_id, public_key)
        )
        # If it exists, update the key and ownership.
        db.execute(
            'UPDATE customer_nodes SET public_key = %s, user_id = %s WHERE node_id = %s',
            (public_key, session['user_id'], node_id)
        )

        # 2. Add the node to the team.
        cursor = db.execute(
            'INSERT INTO team_nodes (team_id, node_id, display_name, worker_types, is_primary) VALUES (%s, %s, %s, %s, %s)',
            (team_id, node_id, display_name, ','.join(worker_types), 1 if is_primary else 0)
        )
        result = cursor.fetchone()

        record_id = result["id"] if result else None

        # 3. If this node is primary, ensure no others are.
        if is_primary:
            db.execute(
                'UPDATE team_nodes SET is_primary = 0 WHERE team_id = %s AND id != %s',
                (team_id, record_id)
            )
        db.commit()
    except psycopg2.IntegrityError:
        db.rollback()
        # This could be a race condition or a unique constraint violation on (team_id, node_id).
        # We check if the node is already registered to this org to provide a better error message.
        existing = db.execute(
            'SELECT 1 FROM team_nodes WHERE team_id = %s AND node_id = %s',
            (team_id, node_id)
        ).fetchone()
        if existing:
            return jsonify({'status': 'error', 'message': 'This node is already registered to your team.'}), 409
        return jsonify({'status': 'error', 'message': 'Node identifier is already registered by another user or team.'}), 409
    except Exception:
        db.rollback()
        logger.error('Failed to register home node %s for team %s', node_id, team_id, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to register home node.'}), 500

    # 4. Send confirmation message to the waiting node worker.
    try:
        r = _get_team_redis_connection()
        confirmation_channel = f"pubsub:node_registration_confirmation:{node_id}"
        payload = json.dumps({'status': 'approved', 'organization_id': team_id})
        r.publish(confirmation_channel, payload)
        logger.info(f"Sent approval confirmation to node {node_id} on channel {confirmation_channel}")
    except redis.RedisError as e:
        logger.error(f"Failed to send registration confirmation to node {node_id}: {e}")
        # The registration is in the DB, but the node doesn't know. The user may need to restart the node.
        # We can still return success to the UI but include a warning.
        response = {
            'status': 'warning',
            'message': 'Node registered, but failed to send confirmation. The node may need a restart to connect.',
        }
    else:
        response = {'status': 'success'}


    node_row = db.execute(
        'SELECT id, team_id, node_id, display_name, worker_types, is_primary, created_at FROM team_nodes WHERE id = %s',
        (record_id,)
    ).fetchone()
    node = serialize_team_node(node_row)
    response['node'] = node
    return jsonify(response), 201


@app.route('/api/organizations/<int:organization_id>/nodes', methods=['GET', 'POST'])
@csrf_protect
def handle_organization_nodes(organization_id):
    return manage_team_nodes(organization_id)


@app.route('/api/teams/<int:team_id>/nodes/<int:node_record_id>', methods=['PATCH', 'DELETE'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN, TEAM_ROLE_MEMBER})
@csrf_protect
def update_team_node(team_id, node_record_id):
    db = get_db()
    role = g.current_team_role

    node_row = db.execute(
        'SELECT id, team_id, node_id, display_name, worker_types, is_primary, created_at FROM team_nodes WHERE id = %s AND team_id = %s',
        (node_record_id, team_id)
    ).fetchone()
    if not node_row:
        return jsonify({'status': 'error', 'message': 'Home node not found.'}), 404

    node_id = node_row['node_id']

    if request.method == 'PATCH':
        if role not in {TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN}:
            return jsonify({'status': 'error', 'message': 'You do not have permission to modify this home node.'}), 403

        data = request.json or {}
        updates: dict[str, object] = {}

        if 'display_name' in data:
            display_name = (data.get('display_name') or '').strip() or None
            if display_name and len(display_name) > 80:
                return jsonify({'status': 'error', 'message': 'Display name must be 80 characters or fewer.'}), 400
            updates['display_name'] = display_name

        if 'worker_types' in data:
            worker_types_input = data.get('worker_types')
            if isinstance(worker_types_input, str):
                worker_types_input = [chunk.strip() for chunk in worker_types_input.split(',')]
            elif not isinstance(worker_types_input, Iterable):
                worker_types_input = []
            worker_types = normalize_worker_types(worker_types_input)
            if not worker_types:
                return jsonify({'status': 'error', 'message': 'Select at least one supported worker type for the node.'}), 400
            updates['worker_types'] = ','.join(worker_types)

        if 'is_primary' in data:
            updates['is_primary'] = 1 if bool(data.get('is_primary')) else 0

        public_key = None
        if 'public_key' in data:
            public_key = (data.get('public_key') or '').strip()
            if not is_valid_public_key(public_key):
                return jsonify({'status': 'error', 'message': 'A valid PEM-formatted public key is required.'}), 400

        if not updates and public_key is None:
            node = serialize_team_node(node_row)
            return jsonify({'status': 'success', 'node': node})

        db.execute('BEGIN')
        try:
            if updates:
                # Validate all column names to prevent SQL injection
                for column in updates.keys():
                    if not validate_sql_identifier(column):
                        db.rollback()
                        return jsonify({'status': 'error', 'message': 'Invalid field name.'}), 400
                set_clause = ', '.join(f"{column} = %s" for column in updates)
                params = list(updates.values()) + [node_record_id]
                db.execute(f'UPDATE team_nodes SET {set_clause} WHERE id = %s', params)
            if public_key is not None:
                db.execute(
                    'UPDATE customer_nodes SET public_key = %s, user_id = %s WHERE node_id = %s',
                    (public_key, session['user_id'], node_id)
                )
            if updates.get('is_primary') == 1:
                db.execute(
                    'UPDATE team_nodes SET is_primary = 0 WHERE team_id = %s AND id != %s',
                    (team_id, node_record_id)
                )
            db.commit()
        except Exception:
            db.rollback()
            logger.error('Failed to update home node %s for team %s', node_id, team_id, exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to update home node.'}), 500

        updated_row = db.execute(
            'SELECT id, team_id, node_id, display_name, worker_types, is_primary, created_at FROM team_nodes WHERE id = %s',
            (node_record_id,)
        ).fetchone()
        node = serialize_team_node(updated_row)
        return jsonify({'status': 'success', 'node': node})

    # DELETE
    if role not in {TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN}:
        return jsonify({'status': 'error', 'message': 'You do not have permission to remove this home node.'}), 403

    db.execute('BEGIN')
    try:
        db.execute('DELETE FROM team_nodes WHERE id = %s', (node_record_id,))
        db.execute('DELETE FROM customer_nodes WHERE node_id = %s', (node_id,))
        db.commit()
    except Exception:
        db.rollback()
        logger.error('Failed to delete home node %s for team %s', node_id, team_id, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to delete home node.'}), 500

    return jsonify({'status': 'success', 'message': 'Home node removed.'})


@app.route('/api/organizations/<int:organization_id>/nodes/<int:node_record_id>', methods=['PATCH', 'DELETE'])
@csrf_protect
def update_organization_node(organization_id, node_record_id):
    return update_team_node(organization_id, node_record_id)


@app.route('/api/teams/<int:team_id>/members', methods=['POST'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN})
@csrf_protect
def create_team_member(team_id):
    db = get_db()
    current_role = g.current_team_role

    if not team_has_team_plan(db, team_id):
        return jsonify({'status': 'error', 'message': 'Team management requires a Managed Premium or Enterprise subscription.'}), 403

    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    if not email:
        return jsonify({'status': 'error', 'message': 'Email address is required.'}), 400

    target_role = _normalize_team_role(data.get('role')) or TEAM_ROLE_MEMBER
    if target_role not in TEAM_MUTABLE_ROLES:
        return jsonify({'status': 'error', 'message': 'Invalid role specified.'}), 400
    if target_role == TEAM_ROLE_ADMIN and current_role != TEAM_ROLE_OWNER:
        return jsonify({'status': 'error', 'message': 'Only the team owner can assign administrator roles.'}), 403

    user_row = db.execute(
        'SELECT u.id FROM user_credentials uc JOIN users u ON uc.user_id = u.id WHERE lower(uc.email) = %s',
        (email,),
    ).fetchone()
    if not user_row:
        return jsonify({'status': 'error', 'message': 'User not found.'}), 404

    member_id = user_row['id']

    if get_team_membership(db, member_id, team_id):
        return jsonify({'status': 'error', 'message': 'User is already a member of this team.'}), 409

    if target_role == TEAM_ROLE_BILLING:
        team = db.execute(
            'SELECT creator_id FROM teams WHERE id = %s',
            (team_id,)
        ).fetchone()
        if not team or team['creator_id'] != member_id:
            return jsonify({'status': 'error', 'message': 'The billing role can only be assigned to the team owner.'}), 403

    db.execute(
        'INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, %s)',
        (team_id, member_id, target_role),
    )
    db.commit()

    member_row = db.execute(
        '''
        SELECT tm.user_id, tm.role, tm.joined_at, u.username, u.legal_name, u.profile_picture_url
        FROM team_members tm
        JOIN users u ON tm.user_id = u.id
        WHERE tm.team_id = %s AND tm.user_id = %s
        ''',
        (team_id, member_id),
    ).fetchone()

    socketio.emit('user_info_update', room=f"user_{member_id}")

    return jsonify({'status': 'success', 'member': serialize_team_member(member_row)}), 201


@app.route('/api/teams/<int:team_id>/invitations', methods=['POST'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN})
@csrf_protect
def invite_team_member(team_id):
    db = get_db()
    inviter_id = session['user_id']
    current_role = g.current_team_role

    if not team_has_team_plan(db, team_id):
        return jsonify({'status': 'error', 'message': 'Team management requires a Managed Premium or Enterprise subscription.'}), 403

    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    if not email:
        return jsonify({'status': 'error', 'message': 'Email address is required.'}), 400

    target_role = _normalize_team_role(data.get('role')) or TEAM_ROLE_MEMBER
    if target_role not in TEAM_MUTABLE_ROLES:
        return jsonify({'status': 'error', 'message': 'Invalid role specified.'}), 400
    if target_role == TEAM_ROLE_ADMIN and current_role != TEAM_ROLE_OWNER:
        return jsonify({'status': 'error', 'message': 'Only owners can assign admin roles.'}), 403

    # Check if user exists
    user_to_invite = db.execute(
        'SELECT u.id FROM user_credentials uc JOIN users u ON uc.user_id = u.id WHERE lower(uc.email) = %s', (email,)
    ).fetchone()
    if not user_to_invite:
        return jsonify({'status': 'error', 'message': 'Invitee does not have an account.'}), 404

    # Check if user is already a member
    if get_team_membership(db, user_to_invite['id'], team_id):
        return jsonify({'status': 'error', 'message': 'User is already a member of this team.'}), 409

    # Check for a pending invitation
    pending_invite = db.execute(
        'SELECT 1 FROM team_invitations WHERE team_id = %s AND invitee_email = %s AND status = %s',
        (team_id, email, 'pending')
    ).fetchone()
    if pending_invite:
        return jsonify({'status': 'error', 'message': 'An invitation has already been sent to this email address.'}), 409

    token = secrets.token_urlsafe(32)
    db.execute('BEGIN')
    try:
        db.execute(
            'INSERT INTO team_invitations (team_id, inviter_id, invitee_email, token, role) VALUES (%s, %s, %s, %s, %s)',
            (team_id, inviter_id, email, token, target_role)
        )

        team = db.execute('SELECT name FROM teams WHERE id = %s', (team_id,)).fetchone()
        inviter = db.execute('SELECT username FROM users WHERE id = %s', (inviter_id,)).fetchone()

        notification_message = (
            f"{inviter['username']} invited you to join {team['name']} as a {target_role}."
        )
        create_user_notification(
            db,
            user_to_invite['id'],
            'Organization invitation',
            notification_message,
            category='organization',
            metadata={'invitation_token': token, 'team_id': team_id}
        )

        # Prepare and send email BEFORE committing
        invitation_link = url_for('handle_team_invitation_landing', token=token, _external=True)
        email_subject = f"You are invited to join the '{team['name']}' team"
        email_body = (
            f"<p>{inviter['username']} has invited you to join the '{team['name']}' team as a {target_role}.</p>"
            f"<p>Click the link to accept or decline: <a href='{invitation_link}'>{invitation_link}</a></p>"
        )

        # Try sending email before committing
        if not _send_team_email(email, email_subject, email_body):
            db.rollback()
            logger.error('Failed to send team invitation email to %s', email)
            return jsonify({'status': 'error', 'message': 'Failed to send invitation email. Please check the email address and try again.'}), 500

        # Only commit if email was sent successfully
        db.commit()

    except Exception as e:
        db.rollback()
        logger.error('Failed to create team invitation for %s: %s', email, str(e), exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to send invitation.'}), 500

    return jsonify({'status': 'success', 'message': f'Invitation sent to {email}.'}), 201


@app.route('/api/organizations/<int:organization_id>/invitations', methods=['POST'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN})
@csrf_protect
def invite_organization_member(organization_id):
    return invite_team_member(organization_id)


@app.route('/api/teams/<int:team_id>/members/<int:member_id>', methods=['PATCH', 'DELETE'])
@login_required
@team_role_required({TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN, TEAM_ROLE_MEMBER, TEAM_ROLE_BILLING})
@csrf_protect
def update_team_member(team_id, member_id):
    db = get_db()
    user_id = session['user_id']
    current_role = g.current_team_role

    if not team_has_team_plan(db, team_id):
        return jsonify({'status': 'error', 'message': 'Team management requires a Managed Premium or Enterprise subscription.'}), 403

    member_row = db.execute(
        'SELECT role FROM team_members WHERE team_id = %s AND user_id = %s',
        (team_id, member_id)
    ).fetchone()
    if not member_row:
        return jsonify({'status': 'error', 'message': 'Member not found.'}), 404

    target_role = member_row['role']

    if request.method == 'PATCH':
        data = request.json or {}
        new_role = _normalize_team_role(data.get('role'))

        # --- Granular Role Validation ---
        if new_role not in TEAM_MUTABLE_ROLES:
            return jsonify({'status': 'error', 'message': 'Invalid role specified.'}), 400

        # Rule: Only owners and admins can modify roles.
        if current_role not in {TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN}:
            return jsonify({'status': 'error', 'message': 'You do not have permission to modify member roles.'}), 403

        # Rule: The owner's role cannot be changed by anyone.
        if target_role == TEAM_ROLE_OWNER:
            return jsonify({'status': 'error', 'message': 'The team owner\'s role cannot be changed.'}), 400

        # Rule: Admins cannot modify their own role.
        if current_role == TEAM_ROLE_ADMIN and user_id == member_id:
            return jsonify({'status': 'error', 'message': 'Administrators cannot change their own role.'}), 403

        # Rule: Only the owner can manage administrator roles.
        if (new_role == TEAM_ROLE_ADMIN or target_role == TEAM_ROLE_ADMIN) and current_role != TEAM_ROLE_OWNER:
            return jsonify({'status': 'error', 'message': 'Only the team owner can manage administrator roles.'}), 403
        # --- End Validation ---

        db.execute(
            'UPDATE team_members SET role = %s WHERE team_id = %s AND user_id = %s',
            (new_role, team_id, member_id)
        )
        db.commit()

        updated_row = db.execute(
            '''
            SELECT tm.user_id, tm.role, tm.joined_at, u.username, u.legal_name, u.profile_picture_url
            FROM team_members tm
            JOIN users u ON tm.user_id = u.id
            WHERE tm.team_id = %s AND tm.user_id = %s
            ''',
            (team_id, member_id)
        ).fetchone()
        return jsonify({'member': serialize_team_member(updated_row)})

    # DELETE path
    # Rule: The owner cannot be removed.
    if target_role == TEAM_ROLE_OWNER:
        return jsonify({'status': 'error', 'message': 'The team owner cannot be removed.'}), 400

    # Rule: Any member can leave the team (remove themselves), unless they are the owner.
    is_leaving = (member_id == user_id)
    if is_leaving:
        if current_role == TEAM_ROLE_OWNER:
             return jsonify({'status': 'error', 'message': 'The owner cannot leave the team.'}), 400
    else:
        # Rule: Only owners or admins can remove other users.
        if current_role not in {TEAM_ROLE_OWNER, TEAM_ROLE_ADMIN}:
            return jsonify({'status': 'error', 'message': 'You do not have permission to remove this member.'}), 403
        # Rule: Admins can only remove members and billing contacts.
        if current_role == TEAM_ROLE_ADMIN and target_role not in {TEAM_ROLE_MEMBER, TEAM_ROLE_BILLING}:
            return jsonify({'status': 'error', 'message': 'Admins can only remove members and billing contacts.'}), 403

    db.execute(
        'DELETE FROM team_members WHERE team_id = %s AND user_id = %s',
        (team_id, member_id)
    )
    db.commit()
    return jsonify({'status': 'success'})


@app.route('/api/organizations/<int:organization_id>/members/<int:member_id>', methods=['PATCH', 'DELETE'])
@csrf_protect
def update_organization_member(organization_id, member_id):
    return update_team_member(organization_id, member_id)


@app.route('/api/team_invitations', methods=['GET'])
@login_required
def get_team_invitations():
    db = get_db()
    user_id = session['user_id']
    user_email_row = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()
    if not user_email_row:
        return jsonify([])

    user_email = user_email_row['email']
    invitations = db.execute(
        '''
        SELECT
            ti.token,
            ti.role,
            ti.created_at,
            t.name as team_name,
            u.username as inviter_name
        FROM team_invitations ti
        JOIN teams t ON ti.team_id = t.id
        JOIN users u ON ti.inviter_id = u.id
        WHERE lower(ti.invitee_email) = lower(%s) AND ti.status = 'pending'
        ORDER BY ti.created_at DESC
        ''',
        (user_email,)
    ).fetchall()

    return jsonify([dict(row) for row in invitations])


@app.route('/team_invitations/<token>', methods=['GET'])
@login_required
def handle_team_invitation_landing(token):
    db = get_db()
    user_id = session['user_id']

    invitation = db.execute(
        '''
        SELECT ti.id, ti.team_id, ti.invitee_email, ti.status, t.name as team_name
        FROM team_invitations ti
        JOIN teams t ON ti.team_id = t.id
        WHERE ti.token = %s
        ''',
        (token,)
    ).fetchone()

    if not invitation:
        return render_template('message.html', message='This invitation is invalid or has expired.'), 404

    if invitation['status'] != 'pending':
        return render_template('message.html', message=f"This invitation has already been {invitation['status']}."), 400

    user_email = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()['email']
    if user_email.lower() != invitation['invitee_email'].lower():
        return render_template('message.html', message='This invitation is intended for a different user.'), 403

    return render_template(
        'handle_team_invitation.html',
        invitation=dict(invitation),
        token=token
    )


@app.route('/api/team_invitations/<token>/accept', methods=['POST'])
@login_required
@csrf_protect
def accept_team_invitation(token):
    from phonos_app.helpers.activity_log import log_activity

    db = get_db()
    user_id = session['user_id']

    invitation = db.execute('SELECT * FROM team_invitations WHERE token = %s', (token,)).fetchone()

    if not invitation:
        return jsonify({'status': 'error', 'message': 'Invitation not found.'}), 404
    if invitation['status'] != 'pending':
        return jsonify({'status': 'error', 'message': f"Invitation has already been {invitation['status']}."}), 400

    user_email = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()['email']
    if user_email.lower() != invitation['invitee_email'].lower():
        return jsonify({'status': 'error', 'message': 'This invitation is for another user.'}), 403

    team_id = invitation['team_id']
    role = invitation['role']

    if get_team_membership(db, user_id, team_id):
        db.execute('UPDATE team_invitations SET status = %s WHERE id = %s', ('accepted', invitation['id']))
        db.commit()
        return jsonify({'status': 'error', 'message': 'You are already a member of this team.'}), 409

    db.execute('BEGIN')
    try:
        db.execute(
            'INSERT INTO team_members (team_id, user_id, role) VALUES (%s, %s, %s)',
            (team_id, user_id, role)
        )
        db.execute('UPDATE team_invitations SET status = %s WHERE id = %s', ('accepted', invitation['id']))

        # Get team name for activity log
        team = db.execute('SELECT name FROM teams WHERE id = %s', (team_id,)).fetchone()
        team_name = team['name'] if team else f'Team {team_id}'

        # Log activity to dashboard operations log
        log_activity(
            db=db,
            user_id=user_id,
            activity_type='team_invitation_accepted',
            activity_description=f'Joined {team_name} as {role}',
            resource_type='team',
            resource_id=team_id,
            metadata={'role': role, 'team_name': team_name}
        )

        db.commit()
    except Exception:
        db.rollback()
        logger.error(
            'Failed to accept team invitation for user %s to org %s',
            user_id, team_id, exc_info=True
        )
        return jsonify({'status': 'error', 'message': 'Failed to join team.'}), 500

    socketio.emit('user_info_update', room=f"user_{user_id}")
    return jsonify({'status': 'success', 'message': 'Invitation accepted, you have joined the team.'})


@app.route('/api/team_invitations/<token>/decline', methods=['POST'])
@login_required
@csrf_protect
def decline_team_invitation(token):
    db = get_db()
    user_id = session['user_id']
    invitation = db.execute('SELECT * FROM team_invitations WHERE token = %s', (token,)).fetchone()

    if not invitation:
        return jsonify({'status': 'error', 'message': 'Invitation not found.'}), 404
    if invitation['status'] != 'pending':
        return jsonify({'status': 'error', 'message': f"Invitation has already been {invitation['status']}."}), 400

    user_email = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()['email']
    if user_email.lower() != invitation['invitee_email'].lower():
        return jsonify({'status': 'error', 'message': 'This invitation is for another user.'}), 403

    db.execute('UPDATE team_invitations SET status = %s WHERE id = %s', ('declined', invitation['id']))
    db.commit()
    return jsonify({'status': 'success', 'message': 'Invitation declined.'})
