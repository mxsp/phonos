"""API routes for projects, accounts, jobs, files, settings, and help desk."""
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
from ..helpers.persistent_auth import revoke_all_remember_tokens, revoke_all_trust_devices

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}


def _delete_project_logic(db, project_id, team_id):
    """Internal logic to delete a project and its associated data."""
    project_path = get_safe_project_path(project_id, team_id)
    if not project_path:
        logger.warning(f"Attempt to delete non-existent or unauthorized project {project_id} in team {team_id}")
        return

    # Delete associated jobs and their files first, reclaiming storage from each creator
    jobs_in_project = db.execute('SELECT job_id, output_file, output_file_size, creator_id FROM jobs WHERE project_id = %s', (project_id,)).fetchall()
    for job in jobs_in_project:
        delete_job_file_and_update_storage(db, job['creator_id'], job['job_id'], job['output_file'], job['output_file_size'])

    # Delete uploaded files, reclaiming storage from each creator
    files_in_project = db.execute('SELECT id, path, size, creator_id FROM files WHERE project_id = %s', (project_id,)).fetchall()
    for file_item in files_in_project:
        if os.path.exists(file_item['path']):
            os.remove(file_item['path'])
        db.execute('UPDATE users SET storage_used = GREATEST(0, storage_used - %s) WHERE id = %s', (file_item['size'], file_item['creator_id']))


    # Delete the physical project folder
    if os.path.exists(project_path):
        try:
            shutil.rmtree(project_path)
        except OSError:
            logger.error(f"Error deleting project directory {project_path}.", exc_info=True)

    # Finally, delete the project record from the database, which cascades to jobs, files, assignments
    db.execute('DELETE FROM projects WHERE id = %s', (project_id,))


@app.route('/api/account/delete', methods=['POST'])
@login_required
@csrf_protect
def delete_account():
    """Completely delete user account and all associated data."""
    from production.account_deletion import AccountDeletionService

    user_id = session['user_id']

    # Get user info for logging
    db = get_db()
    user = db.execute('SELECT username FROM users WHERE id = %s', (user_id,)).fetchone()
    username = user['username'] if user else f'User {user_id}'

    # Verify password for security
    data = request.get_json() or {}
    password = data.get('password')

    if not password:
        return jsonify({'status': 'error', 'message': 'Password confirmation required'}), 400

    # Verify password
    import bcrypt
    user_cred = db.execute('SELECT password FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()
    # Convert memoryview to bytes if needed
    stored_password = bytes(user_cred['password']) if user_cred else None
    if not user_cred or not bcrypt.checkpw(password.encode('utf-8'), stored_password):
        return jsonify({'status': 'error', 'message': 'Invalid password'}), 401

    # Delete the account
    success, message = AccountDeletionService.delete_account(user_id, initiated_by=username)

    if success:
        session.clear()
        logger.info(f"Account deleted: {username} (ID: {user_id})")
        return jsonify({'status': 'success', 'message': message}), 200
    else:
        logger.error(f"Failed to delete account: {username} (ID: {user_id}) - {message}")
        return jsonify({'status': 'error', 'message': message}), 500

@app.route('/api/account/export', methods=['GET'])
@login_required
def export_account_data():
    """
    GDPR Article 20: Right to data portability
    Export all user data in a machine-readable JSON format
    """
    from datetime import datetime

    user_id = session['user_id']
    db = get_db()

    # Collect all user data
    export_data = {
        'export_info': {
            'export_date': datetime.utcnow().isoformat() + 'Z',
            'format': 'JSON',
            'gdpr_article': 'Article 20 - Right to data portability'
        }
    }

    # User profile data
    user = db.execute('''
        SELECT u.id, u.username, u.legal_name, u.address, u.university, u.age,
               u.plan, u.credits, u.storage_used, u.storage_quota, u.created_at,
               u.profile_picture_url, u.layout_density, u.table_style,
               uc.email, uc.email_notifications_enabled
        FROM users u
        LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id
        WHERE u.id = %s
    ''', (user_id,)).fetchone()

    if user:
        export_data['profile'] = dict(user)

    # Projects
    projects = db.execute('''
        SELECT p.id, p.name, p.description, p.created_at, p.updated_at
        FROM projects p
        JOIN project_members pm ON p.id = pm.project_id
        WHERE pm.user_id = %s AND pm.role = 'Owner'
    ''', (user_id,)).fetchall()
    export_data['projects'] = [dict(p) for p in projects]

    # Jobs
    jobs = db.execute('''
        SELECT job_id, name, model, status, created_at, updated_at,
               message, cost, project_id
        FROM jobs
        WHERE creator_id = %s
        ORDER BY created_at DESC
    ''', (user_id,)).fetchall()
    export_data['jobs'] = [dict(j) for j in jobs]

    # Files (metadata only, not content)
    files = db.execute('''
        SELECT id, name, size, created_at, project_id, path
        FROM files
        WHERE creator_id = %s
        ORDER BY created_at DESC
    ''', (user_id,)).fetchall()
    export_data['files'] = [dict(f) for f in files]

    # API keys (masked)
    api_keys = db.execute('''
        SELECT id, name, created_at, last_used_at, is_active
        FROM api_keys
        WHERE user_id = %s
    ''', (user_id,)).fetchall()
    export_data['api_keys'] = [dict(k) for k in api_keys]

    # Team memberships
    teams = db.execute('''
        SELECT t.id, t.name, t.description, tm.role, tm.joined_at
        FROM teams t
        JOIN team_members tm ON t.id = tm.team_id
        WHERE tm.user_id = %s
    ''', (user_id,)).fetchall()
    export_data['team_memberships'] = [dict(t) for t in teams]

    # Create response with proper headers for download
    response = jsonify(export_data)
    response.headers['Content-Disposition'] = f'attachment; filename=phonos_data_export_{user_id}_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.json'
    response.headers['Content-Type'] = 'application/json'

    logger.info(f"Data export generated for user {user_id}")
    return response

@app.route('/api/account/update', methods=['POST'])
@login_required
@csrf_protect
def update_account():
    data = request.json
    db = get_db()
    user_id = session['user_id']

    sensitive_changes = {}
    non_sensitive_changes = {}

    # Segregate changes. Every profile or security-relevant field (identity,
    # credentials, and personal profile details) must be confirmed with an
    # emailed 2FA code before it is applied. Only low-risk preference toggles
    # are applied directly.
    for key, value in data.items():
        if key in ['email', 'password', 'username', 'legal_name', 'address', 'company', 'university', 'age']:
            if key == 'email':
                if db.execute('SELECT id FROM user_credentials WHERE email = %s AND user_id != %s', (value, user_id)).fetchone():
                    return jsonify({'status': 'error', 'message': 'Email already in use.'}), 409
                sensitive_changes['email'] = value
            elif key == 'password':
                if value:
                    hashed_password = bcrypt.hashpw(value.encode('utf-8'), bcrypt.gensalt())
                    sensitive_changes['password'] = hashed_password.decode('utf-8')
            elif key == 'username':
                if db.execute('SELECT id FROM users WHERE username = %s AND id != %s', (value, user_id)).fetchone():
                    return jsonify({'status': 'error', 'message': 'Username already in use.'}), 409
                sensitive_changes['username'] = sanitize_input(value, max_length=100)
            elif key in ('legal_name', 'address'):
                sensitive_changes[key] = sanitize_input(value, max_length=1000)
            elif key in ('company', 'university'):
                sensitive_changes[key] = sanitize_input(str(value), max_length=500)
            elif key == 'age':
                sensitive_changes[key] = value
        elif key == 'email_notifications_enabled':
            non_sensitive_changes[key] = value

    # Apply low-risk preference changes immediately (no 2FA required). These are
    # applied even when sensitive changes are pending so the toggle is never lost.
    if non_sensitive_changes:
        if 'email_notifications_enabled' in non_sensitive_changes:
            db.execute(
                'UPDATE user_credentials SET email_notifications_enabled = %s WHERE user_id = %s',
                (non_sensitive_changes['email_notifications_enabled'], user_id),
            )
        db.commit()

    # When ENABLE_2FA is False (default), apply profile & password changes directly
    # while keeping the Redis/email 2FA branch intact below as dormant code.
    if sensitive_changes and not getattr(Config, 'ENABLE_2FA', False):
        if 'email' in sensitive_changes:
            new_email = sensitive_changes['email']
            db.execute('UPDATE user_credentials SET email = %s WHERE user_id = %s', (new_email, user_id))
        if 'password' in sensitive_changes:
            pw_bytes = sensitive_changes['password'].encode('utf-8') if isinstance(sensitive_changes['password'], str) else sensitive_changes['password']
            db.execute('UPDATE user_credentials SET password = %s WHERE user_id = %s', (pw_bytes, user_id))
            revoke_all_remember_tokens(db, user_id)
            revoke_all_trust_devices(db, user_id)
        user_fields = ['username', 'legal_name', 'address', 'company', 'university', 'age']
        fields_to_update = []
        params = []
        for field in user_fields:
            if field in sensitive_changes and validate_sql_identifier(field):
                fields_to_update.append(f"{field} = %s")
                params.append(sensitive_changes[field])
        if fields_to_update:
            params.append(user_id)
            db.execute(f"UPDATE users SET {', '.join(fields_to_update)} WHERE id = %s", tuple(params))
        if 'username' in sensitive_changes:
            session['username'] = sensitive_changes['username']
        db.commit()
        return jsonify({'status': 'success', 'message': 'Account updated successfully.'})

    # Dormant 2FA branch (active only when ENABLE_2FA=true):
    if sensitive_changes:
        user_credential = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()
        if not user_credential or not user_credential['email']:
            return jsonify({'status': 'error', 'message': 'Unable to send verification code: no email address on file.'}), 400

        r = get_redis_connection()
        two_fa_code = secrets.token_hex(3).upper()
        change_token = secrets.token_urlsafe(32)

        pending_data = {
            'user_id': user_id,
            'changes': sensitive_changes,
            '2fa_code': two_fa_code,
        }
        r.setex(f"pending_change:{change_token}", timedelta(minutes=15), json.dumps(pending_data))

        send_email(
            user_credential['email'],
            'Verify Your Account Change',
            f'Your verification code is: {two_fa_code}\n\nThis code will expire in 15 minutes.\n\n'
            'If you did not request this change, please secure your account immediately.',
        )

        return jsonify({'status': '2fa_required', 'token': change_token})

    return jsonify({'status': 'success', 'message': 'Account updated successfully.'})

@app.route('/verify_change_2fa/<token>', methods=['GET', 'POST'])
@login_required
@csrf_protect
def verify_change_2fa(token):
    r = get_redis_connection()
    pending_data_json = r.get(f"pending_change:{token}")

    if not pending_data_json:
        return render_template('message.html', message='Invalid or expired verification token.')

    if request.method == 'POST':
        code = request.form.get('2fa_code', '').strip().upper()
        pending_data = json.loads(pending_data_json)

        if code and secrets.compare_digest(code, pending_data['2fa_code']):
            db = get_db()
            user_id = pending_data['user_id']
            changes = pending_data['changes']

            # The verified code must belong to the currently logged-in user.
            if user_id != session.get('user_id'):
                return render_template('message.html', message='Session mismatch. Please try again.')

            # Apply all changes stored in Redis
            if 'email' in changes:
                new_email = changes['email']
                db.execute('UPDATE user_credentials SET email = %s WHERE user_id = %s', (new_email, user_id))

                # Update Stripe customer email if applicable
                try:
                    from stripe_service import StripeService
                    StripeService.update_customer_email(user_id, new_email)
                except Exception as e:
                    logger.warning(f"Failed to update Stripe customer email for user {user_id}: {e}")
                    # Don't fail the whole operation if Stripe update fails

            if 'password' in changes:
                db.execute('UPDATE user_credentials SET password = %s WHERE user_id = %s', (changes['password'], user_id))
                # A password change invalidates all "stay signed in" / 2FA-trust sessions.
                revoke_all_remember_tokens(db, user_id)
                revoke_all_trust_devices(db, user_id)

            user_fields = ['username', 'legal_name', 'address', 'company', 'university', 'age']
            fields_to_update = []
            params = []
            for field in user_fields:
                if field in changes:
                    # Validate SQL identifier to prevent injection
                    if not validate_sql_identifier(field):
                        return render_template('message.html', message='Invalid field name in update request.')
                    fields_to_update.append(f"{field} = %s")
                    # Values are already sanitized when stored, but double-check
                    params.append(changes[field])

            if fields_to_update:
                params.append(user_id)
                db.execute(f"UPDATE users SET {', '.join(fields_to_update)} WHERE id = %s", tuple(params))

            db.commit()
            r.delete(f"pending_change:{token}")
            return render_template('message.html', message='Your changes have been successfully applied.')
        else:
            return render_template('verify_change_2fa.html', token=token, error='Invalid verification code.')

    return render_template('verify_change_2fa.html', token=token)

@app.route('/resend_change_2fa/<token>', methods=['POST'])
@login_required
@csrf_protect
def resend_change_2fa(token):
    """Resend 2FA code for account change verification."""
    r = get_redis_connection()
    pending_data_json = r.get(f"pending_change:{token}")

    if not pending_data_json:
        return render_template('verify_change_2fa.html', token=token, error='Invalid or expired verification token.')

    try:
        pending_data = json.loads(pending_data_json)
        user_id = pending_data['user_id']

        # Verify user matches
        if user_id != session.get('user_id'):
            return render_template('verify_change_2fa.html', token=token, error='Session mismatch.')

        # Generate new 2FA code
        new_code = secrets.token_hex(3).upper()
        pending_data['2fa_code'] = new_code

        # Update Redis with new code (keep same expiration)
        ttl = r.ttl(f"pending_change:{token}")
        if ttl > 0:
            r.setex(f"pending_change:{token}", ttl, json.dumps(pending_data))
        else:
            # If TTL expired during processing, set a fresh 15 minutes
            r.setex(f"pending_change:{token}", timedelta(minutes=15), json.dumps(pending_data))

        # Get user email and send new code
        db = get_db()
        user_credential = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()

        if user_credential:
            send_email(user_credential['email'], 'Verify Your Account Change', f'Your new verification code is: {new_code}\n\nThis code will expire in 15 minutes.')
            return render_template('verify_change_2fa.html', token=token, success='A new verification code has been sent to your email.')
        else:
            return render_template('verify_change_2fa.html', token=token, error='Unable to send verification code.')
    except Exception as e:
        logger.error(f"Error resending change 2FA for token {token}: {e}", exc_info=True)
        return render_template('verify_change_2fa.html', token=token, error='Failed to resend verification code.')

@app.route('/api/user', methods=['GET'])
@app.route('/api/user_info', methods=['GET'])
@login_required
def get_user_info():
    db = get_db()
    user_id = session['user_id']
    ensure_monthly_credit_reset(db, user_id)
    ensure_user_layout_columns(db)
    user = db.execute('SELECT u.storage_used, u.credits, u.university, u.age, u.plan, u.storage_quota, u.legal_name, u.address, u.profile_picture_url, u.credits_last_reset, u.layout_density, u.table_style, uc.email, uc.email_notifications_enabled FROM users u LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE u.id = %s', (user_id,)).fetchone()
    if user is None:
        session.clear()
        return jsonify({'status': 'error', 'message': 'User not found.'}), 404
    assigned_projects_q = db.execute('SELECT project_id FROM project_assignments WHERE user_id = %s', (user_id,)).fetchall()
    assigned_project_ids = [row['project_id'] for row in assigned_projects_q]
    project_count = len(assigned_project_ids)

    active_job_count = 0
    completed_job_count = 0
    failed_job_count = 0

    if assigned_project_ids:
        placeholders = ','.join('%s' for _ in assigned_project_ids)
        active_job_count = db.execute(f"SELECT COUNT(job_id) as count FROM jobs WHERE project_id IN ({placeholders}) AND status IN ('submitted', 'processing')", tuple(assigned_project_ids)).fetchone()['count']
        completed_job_count = db.execute(f"SELECT COUNT(job_id) as count FROM jobs WHERE project_id IN ({placeholders}) AND status = 'completed'", tuple(assigned_project_ids)).fetchone()['count']
        failed_job_count = db.execute(f"SELECT COUNT(job_id) as count FROM jobs WHERE project_id IN ({placeholders}) AND status = 'failed'", tuple(assigned_project_ids)).fetchone()['count']
    normalized_plan = normalize_plan(user['plan'])

    if normalized_plan != user['plan']:
        db.execute('UPDATE users SET plan = %s WHERE id = %s', (normalized_plan, user_id))
        db.commit()

    last_reset_dt = parse_sqlite_timestamp(user['credits_last_reset'])
    next_reset_at = (last_reset_dt + timedelta(days=30)).isoformat() if last_reset_dt else None

    storage_state = get_user_storage_state(db, user_id)

    # Calculate storage percentage for progress bar
    storage_percent = 0
    if storage_state['quota'] > 0:
        storage_percent = min(100, (storage_state['used'] / storage_state['quota']) * 100)

    return jsonify({
        'id': user_id,
        'username': session['username'], 'email': user['email'] or '',
        'storage_used': storage_state['used'],
        'storage_quota': storage_state['quota'],
        'storage_available': storage_state['available'],
        'storage_overage': storage_state['overage'],
        'storage_over_quota': storage_state['overage'] > 0,
        'storage_percent': round(storage_percent, 1),
        'credits': user['credits'], 'project_count': project_count,
        'active_job_count': active_job_count, 'completed_job_count': completed_job_count,
        'failed_job_count': failed_job_count,
        'university': user['university'], 'age': user['age'],
        'plan': normalized_plan,
        'legal_name': user['legal_name'],
        'address': user['address'],
        'profile_picture_url': user['profile_picture_url'],
        'next_credit_reset': next_reset_at,
        'layout_density': user['layout_density'] or 'cozy',
        'table_style': user['table_style'] or 'zebra',
        'email_notifications_enabled': bool(user['email_notifications_enabled']) if user['email_notifications_enabled'] is not None else False
    })


@app.route('/api/account/plan', methods=['GET', 'POST'])
@login_required
@csrf_protect
def account_plan():
    db = get_db()
    user_id = session['user_id']

    if request.method == 'GET':
        user = db.execute('SELECT plan, credits FROM users WHERE id = %s', (user_id,)).fetchone()
        if not user:
            session.clear()
            return jsonify({'status': 'error', 'message': 'User not found.'}), 404

        plan = normalize_plan(user['plan'])
        if plan != user['plan']:
            db.execute('UPDATE users SET plan = %s WHERE id = %s', (plan, user_id))
            db.commit()

        storage_state = get_user_storage_state(db, user_id)
        return jsonify({
            'status': 'success',
            'plan': plan,
            'credits': user['credits'] if user['credits'] is not None else 0,
            'storage': storage_state,
            'plans': build_plan_catalog(),
        })

    if request.method == 'POST':
        # Collaborators should not be able to change the plan.
        # Check the user's role in their personal team.
        personal_team_id = ensure_personal_team(db, user_id)
        if not personal_team_id:
            return jsonify({'status': 'error', 'message': 'Could not determine personal team for permission check.'}), 500

        team_role = get_team_membership(db, user_id, personal_team_id)
        if not team_role or team_role.lower() not in {'owner', 'admin'}:
            return jsonify({'status': 'error', 'message': 'You do not have permission to change the account plan.'}), 403

        payload = request.get_json(silent=True) or {}
        target_plan = payload.get('plan')
    if not is_valid_plan(target_plan):
        return jsonify({'status': 'error', 'message': 'Invalid plan specified.'}), 400

    current_row = db.execute('SELECT plan FROM users WHERE id = %s', (user_id,)).fetchone()
    current_plan = normalize_plan(current_row['plan']) if current_row else DEFAULT_PLAN

    target_plan_def = get_plan_definition(target_plan)
    if not target_plan_def.self_service and target_plan_def.name != current_plan:
        message = (
            'Enterprise enrollments are no longer paused—they are coordinated with our success team. '
            'Please reach out to hello@phonos.ai to tailor a deployment plan.'
        )
        return (
            jsonify({
                'status': 'error',
                'message': message,
                'plans': build_plan_catalog(),
            }),
            403,
        )

    plan_def, storage_state, warning = apply_plan_to_user(db, user_id, target_plan)

    response = {
        'status': 'success',
        'plan': plan_def.name,
        'message': f'Switched to {plan_def.name} plan.',
        'credits': plan_def.monthly_credits,
        'storage': storage_state,
        'plans': build_plan_catalog(),
    }
    if warning:
        response['warning'] = warning

    socketio.emit('user_info_update', room=f"user_{user_id}")

    return jsonify(response)
