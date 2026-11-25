"""API routes for settings, notifications, and misc account functionality."""
from __future__ import annotations

import gzip
import hashlib
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
    get_disk_manager,
    get_team_membership,
    get_team_node_rows,
    get_project_assignment,
    get_project_storage_roots,
    get_safe_project_path,
    get_all_job_output_roots,
    get_allowed_job_output_prefixes,
    get_user_storage_state,
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
    resolve_file_path,
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
from ..helpers.persistent_auth import revoke_all_remember_tokens, revoke_all_trust_devices

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS

# Import _handle_job_submission from main api module
from .api import _handle_job_submission


DRAWING_BOARD_MODULES = {
    'file-manager',
    'projects',
    'jobs',
    '3d-viewer',
    'notes',
}


def ensure_theme_preferences_table(db):
    db.execute(
        '''CREATE TABLE IF NOT EXISTS user_theme_preferences (
               user_id INTEGER PRIMARY KEY,
               ambient_enabled INTEGER NOT NULL DEFAULT 1,
               accent_style TEXT NOT NULL DEFAULT 'aurora',
               surface_style TEXT NOT NULL DEFAULT 'glass',
               glow_intensity REAL NOT NULL DEFAULT 55,
               table_glow INTEGER NOT NULL DEFAULT 1,
               welcome_message TEXT DEFAULT '',
               panel_tint TEXT NOT NULL DEFAULT 'cool',
               background_pattern TEXT NOT NULL DEFAULT 'none',
               card_shape TEXT NOT NULL DEFAULT 'rounded',
               navigation_style TEXT NOT NULL DEFAULT 'minimal',
               vignette_enabled INTEGER NOT NULL DEFAULT 0,
               bleak_mode INTEGER NOT NULL DEFAULT 0,
               drawing_board_enabled INTEGER NOT NULL DEFAULT 0,
               drawing_board_layout TEXT DEFAULT '[]',
               FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
           )'''
    )
    db.commit()
    columns = {col['column_name'] for col in db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'user_theme_preferences'").fetchall()}
    altered = False
    if 'table_glow' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN table_glow INTEGER NOT NULL DEFAULT 1")
        altered = True
    if 'welcome_message' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN welcome_message TEXT DEFAULT ''")
        altered = True
    if 'panel_tint' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN panel_tint TEXT NOT NULL DEFAULT 'cool'")
        altered = True
    if 'background_pattern' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN background_pattern TEXT NOT NULL DEFAULT 'none'")
        altered = True
    if 'card_shape' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN card_shape TEXT NOT NULL DEFAULT 'rounded'")
        altered = True
    if 'navigation_style' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN navigation_style TEXT NOT NULL DEFAULT 'minimal'")
        altered = True
    if 'vignette_enabled' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN vignette_enabled INTEGER NOT NULL DEFAULT 0")
        altered = True
    if 'bleak_mode' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN bleak_mode INTEGER NOT NULL DEFAULT 0")
        altered = True
    if 'drawing_board_enabled' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN drawing_board_enabled INTEGER NOT NULL DEFAULT 0")
        altered = True
    if 'drawing_board_layout' not in columns:
        db.execute("ALTER TABLE user_theme_preferences ADD COLUMN drawing_board_layout TEXT DEFAULT '[]'")
        altered = True
    if altered:
        db.commit()


def ensure_user_education_settings_table(db):
    """Idempotently ensure the user_education_settings table exists."""
    db.execute(
        '''CREATE TABLE IF NOT EXISTS user_education_settings (
               id SERIAL PRIMARY KEY,
               user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
               education_enabled INTEGER NOT NULL DEFAULT 0,
               default_late_policy TEXT NOT NULL DEFAULT 'none',
               default_time_limit_minutes INTEGER,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )'''
    )
    db.commit()
    columns = {col['column_name'] for col in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_education_settings'"
    ).fetchall()}
    altered = False
    if 'default_late_policy' not in columns:
        db.execute("ALTER TABLE user_education_settings ADD COLUMN default_late_policy TEXT NOT NULL DEFAULT 'none'")
        altered = True
    if 'default_time_limit_minutes' not in columns:
        db.execute("ALTER TABLE user_education_settings ADD COLUMN default_time_limit_minutes INTEGER")
        altered = True
    if altered:
        db.commit()


def get_education_settings(db, user_id):
    """Return the user's education flag row, creating a default row if absent."""
    ensure_user_education_settings_table(db)
    row = db.execute(
        'SELECT education_enabled, default_late_policy, default_time_limit_minutes '
        'FROM user_education_settings WHERE user_id = %s',
        (user_id,),
    ).fetchone()
    if row is None:
        db.execute(
            'INSERT INTO user_education_settings (user_id) VALUES (%s)',
            (user_id,),
        )
        db.commit()
        row = db.execute(
            'SELECT education_enabled, default_late_policy, default_time_limit_minutes '
            'FROM user_education_settings WHERE user_id = %s',
            (user_id,),
        ).fetchone()
    return row


def get_theme_preferences(db, user_id):
    ensure_theme_preferences_table(db)
    prefs = db.execute(
        (
            'SELECT ambient_enabled, accent_style, surface_style, glow_intensity, table_glow, '
            'welcome_message, panel_tint, background_pattern, card_shape, navigation_style, vignette_enabled, bleak_mode, '
            'drawing_board_enabled, drawing_board_layout '
            'FROM user_theme_preferences WHERE user_id = %s'
        ),
        (user_id,),
    ).fetchone()
    if prefs is None:
        db.execute('INSERT INTO user_theme_preferences (user_id) VALUES (%s)', (user_id,))
        db.commit()
        prefs = db.execute(
        (
            'SELECT ambient_enabled, accent_style, surface_style, glow_intensity, table_glow, '
            'welcome_message, panel_tint, background_pattern, card_shape, navigation_style, vignette_enabled, bleak_mode, '
            'drawing_board_enabled, drawing_board_layout '
            'FROM user_theme_preferences WHERE user_id = %s'
        ),
            (user_id,),
        ).fetchone()
    return prefs


def sanitize_drawing_board_layout(raw_layout):
    if not isinstance(raw_layout, list):
        return []

    sanitized = []
    for item in raw_layout:
        if not isinstance(item, dict):
            continue
        module_name = str(item.get('module') or '').strip()
        if module_name not in DRAWING_BOARD_MODULES:
            continue

        try:
            left = float(item.get('left', 0))
            top = float(item.get('top', 0))
        except (TypeError, ValueError):
            left = 0.0
            top = 0.0

        entry = {
            'module': module_name,
            'left': left,
            'top': top,
        }

        for key in ('width', 'height'):
            try:
                value = item.get(key)
                if value is None:
                    continue
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                entry[key] = numeric

        payload = item.get('payload')
        if isinstance(payload, (dict, list)):
            entry['payload'] = payload
        elif isinstance(payload, str):
            entry['payload'] = payload

        sanitized.append(entry)

    return sanitized


@app.route('/submit_job', methods=['POST'])
@login_required
@csrf_protect
def submit_job():
    payload = request.json or {}
    raw_project_id = payload.get('project_id')
    if not raw_project_id:
        return jsonify({'status': 'error', 'message': 'Missing project selection.'}), 400

    try:
        project_id = int(raw_project_id)
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Invalid project identifier.'}), 400

    return _handle_job_submission(project_id, payload)


@app.route('/upgrade_to_premium', methods=['POST'])
@login_required
@csrf_protect
def upgrade_to_premium():
    # Redirect to Stripe payment link
    stripe_payment_link = ''

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'status': 'redirect',
            'url': stripe_payment_link,
            'message': 'Redirecting to payment...'
        })
    return redirect(stripe_payment_link)

@app.route('/upgrade_to_enterprise', methods=['POST'])
@login_required
@csrf_protect
def upgrade_to_enterprise():
    db = get_db()
    user_id = session['user_id']
    plan_def = get_plan_definition('Enterprise')
    if not plan_def.self_service:
        message = (
            'Enterprise enrollments are no longer paused—they are coordinated with our success team. '
            'Please reach out to hello@phonos.ai to tailor a deployment plan.'
        )
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': message, 'plan': plan_def.name}), 403
        abort(403, description=message)

    plan_def, storage_state, warning = apply_plan_to_user(db, user_id, 'Enterprise')
    socketio.emit('user_info_update', room=f"user_{user_id}")
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        response = {
            'status': 'success',
            'message': 'Upgraded to Enterprise for testing.',
            'storage': storage_state,
            'plan': plan_def.name
        }
        if warning:
            response['warning'] = warning
        return jsonify(response)
    return redirect(url_for('dashboard'))

@app.route('/downgrade_to_standard', methods=['POST'])
@login_required
@csrf_protect
def downgrade_to_standard():
    db = get_db()
    user_id = session['user_id']
    plan_def, storage_state, warning = apply_plan_to_user(db, user_id, 'Standard')
    socketio.emit('user_info_update', room=f"user_{user_id}")
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        response = {
            'status': 'success',
            'message': 'Downgraded to Standard successfully.',
            'storage': storage_state,
            'plan': plan_def.name
        }
        if warning:
            response['warning'] = warning
        return jsonify(response)
    return redirect(url_for('dashboard'))


@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
@csrf_protect
@rate_limit(limit=20, window=300, action='settings_update')
def handle_settings():
    try:
        db = get_db()
        user_id = session['user_id']
        plan_row = db.execute('SELECT plan FROM users WHERE id = %s', (user_id,)).fetchone()
        if plan_row is None:
            session.clear()
            return jsonify({'status': 'error', 'message': 'User not found.'}), 404

        current_plan = normalize_plan(plan_row['plan'])
        drawing_board_allowed = current_plan in {'Premium', 'Enterprise'}
        if request.method == 'POST':
            data = request.json or {}

            # BYPASS 2FA: Apply settings directly
            try:
                # Get current plan for drawing board validation
                plan_row = db.execute('SELECT plan FROM users WHERE id = %s', (user_id,)).fetchone()
                current_plan = normalize_plan(plan_row['plan']) if plan_row else 'Standard'
                drawing_board_allowed = current_plan in {'Premium', 'Enterprise'}

                # Apply basic settings
                primary_color = data.get('primary_color')
                background_image = data.get('background_image')
                ensure_user_layout_columns(db)
                layout_density = (data.get('layout_density') or 'cozy').lower()
                table_style = (data.get('table_style') or 'zebra').lower()
                allowed_layouts = {'cozy', 'compact', 'spacious'}
                allowed_tables = {'zebra', 'minimal', 'outlined'}
                if layout_density not in allowed_layouts:
                    layout_density = 'cozy'
                if table_style not in allowed_tables:
                    table_style = 'zebra'

                db.execute(
                    'UPDATE users SET primary_color = %s, background_image = %s, layout_density = %s, table_style = %s WHERE id = %s',
                    (primary_color, background_image, layout_density, table_style, user_id)
                )

                # Apply pro customizations if present
                pro_customizations = data.get('pro_customizations') if isinstance(data, dict) else None
                if pro_customizations is not None:
                    ambient_enabled = 1 if pro_customizations.get('ambient_enabled', True) else 0
                    accent_style = (pro_customizations.get('accent_style') or 'aurora').lower()
                    allowed_accents = {'none', 'aurora', 'nebula', 'prism', 'solstice', 'ion'}
                    if accent_style not in allowed_accents:
                        accent_style = 'aurora'

                    surface_style = (pro_customizations.get('surface_style') or 'glass').lower()
                    allowed_surfaces = {'none', 'glass', 'matte', 'carbon', 'opal', 'velvet', 'professional'}
                    if surface_style not in allowed_surfaces:
                        surface_style = 'glass'

                    try:
                        glow_intensity = float(pro_customizations.get('glow_intensity', 55))
                    except (TypeError, ValueError):
                        glow_intensity = 55

                    welcome_message = sanitize_html((pro_customizations.get('welcome_message') or '').strip())
                    table_glow = 1 if pro_customizations.get('table_glow', True) else 0

                    panel_tint = (pro_customizations.get('panel_tint') or 'cool').lower()
                    allowed_panels = {'none', 'cool', 'warm', 'midnight', 'neutral'}
                    if panel_tint not in allowed_panels:
                        panel_tint = 'cool'

                    background_pattern = (pro_customizations.get('background_pattern') or 'none').lower()
                    allowed_patterns = {'none', 'grid', 'mesh', 'circuit', 'dna_helix'}
                    if background_pattern not in allowed_patterns:
                        background_pattern = 'none'

                    card_shape = (pro_customizations.get('card_shape') or 'rounded').lower()
                    allowed_card_shapes = {'none', 'rounded', 'angular', 'pill'}
                    if card_shape not in allowed_card_shapes:
                        card_shape = 'rounded'

                    navigation_style = (pro_customizations.get('navigation_style') or 'minimal').lower()
                    allowed_nav_styles = {'none', 'minimal', 'floating', 'highlighted'}
                    if navigation_style not in allowed_nav_styles:
                        navigation_style = 'minimal'

                    vignette_enabled = 1 if pro_customizations.get('vignette_enabled', pro_customizations.get('vignette')) else 0
                    bleak_mode = 1 if pro_customizations.get('bleak_mode') else 0
                    drawing_board_enabled = 1 if pro_customizations.get('drawing_board_enabled') else 0
                    drawing_board_layout = sanitize_drawing_board_layout(
                        pro_customizations.get('drawing_board_layout', [])
                    )
                    if not drawing_board_allowed:
                        drawing_board_enabled = 0
                        existing_prefs = get_theme_preferences(db, user_id)
                        try:
                            previous_layout = json.loads(existing_prefs['drawing_board_layout'] or '[]')
                        except (TypeError, ValueError):
                            previous_layout = []
                        drawing_board_layout = sanitize_drawing_board_layout(previous_layout)
                    drawing_board_layout_json = json.dumps(drawing_board_layout)
                    ensure_theme_preferences_table(db)
                    db.execute(
                        '''INSERT INTO user_theme_preferences (user_id, ambient_enabled, accent_style, surface_style, glow_intensity, table_glow, welcome_message, panel_tint, background_pattern, card_shape, navigation_style, vignette_enabled, bleak_mode, drawing_board_enabled, drawing_board_layout)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT(user_id) DO UPDATE SET
                               ambient_enabled = excluded.ambient_enabled,
                               accent_style = excluded.accent_style,
                               surface_style = excluded.surface_style,
                               glow_intensity = excluded.glow_intensity,
                               table_glow = excluded.table_glow,
                               welcome_message = excluded.welcome_message,
                               panel_tint = excluded.panel_tint,
                               background_pattern = excluded.background_pattern,
                               card_shape = excluded.card_shape,
                               navigation_style = excluded.navigation_style,
                               vignette_enabled = excluded.vignette_enabled,
                               bleak_mode = excluded.bleak_mode,
                               drawing_board_enabled = excluded.drawing_board_enabled,
                               drawing_board_layout = excluded.drawing_board_layout''',
                        (
                            user_id,
                            ambient_enabled,
                            accent_style,
                            surface_style,
                            glow_intensity,
                            table_glow,
                            welcome_message,
                            panel_tint,
                            background_pattern,
                            card_shape,
                            navigation_style,
                            vignette_enabled,
                            bleak_mode,
                            drawing_board_enabled,
                            drawing_board_layout_json,
                        )
                    )

                # Education flag (self-service toggle — only the on/off flag
                # lives here; education-specific defaults like late policy and
                # default time limit are managed via /api/education/settings).
                edu_payload = data.get('education') if isinstance(data, dict) else None
                if edu_payload is not None and isinstance(edu_payload, dict):
                    ensure_user_education_settings_table(db)
                    edu_enabled = 1 if edu_payload.get('enabled') else 0
                    db.execute(
                        '''INSERT INTO user_education_settings (user_id, education_enabled)
                           VALUES (%s, %s)
                           ON CONFLICT(user_id) DO UPDATE SET
                               education_enabled = excluded.education_enabled''',
                        (user_id, edu_enabled),
                    )

                db.commit()
                return jsonify({'status': 'success', 'message': 'Your settings have been successfully updated.'})
            except Exception as e:
                logger.error(f"Error applying settings for user {user_id}: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': 'Failed to apply settings. Please try again.'}), 500
        else:
            ensure_user_layout_columns(db)
            user = db.execute(
                'SELECT primary_color, background_image, layout_density, table_style, plan FROM users WHERE id = %s',
                (user_id,),
            ).fetchone()
            if user is None:
                session.clear()
                return jsonify({'status': 'error', 'message': 'User not found.'}), 404
            prefs = get_theme_preferences(db, user_id)
            try:
                drawing_board_layout = json.loads(prefs['drawing_board_layout'] or '[]')
            except (TypeError, ValueError):
                drawing_board_layout = []
            plan_from_row = normalize_plan(user['plan']) if 'plan' in user.keys() else current_plan
            plan_allows_drawing_board = plan_from_row in {'Premium', 'Enterprise'}
            vignette_val = bool(prefs['vignette_enabled'])
            edu_row = get_education_settings(db, user_id)
            return jsonify({
                'primary_color': user['primary_color'] or '#4f46e5',
                'background_image': user['background_image'] or '',
                'layout_density': user['layout_density'] or 'cozy',
                'table_style': user['table_style'] or 'zebra',
                'education': {
                    'enabled': bool(edu_row['education_enabled']),
                },
                'pro_customizations': {
                    'ambient_enabled': bool(prefs['ambient_enabled']),
                    'accent_style': prefs['accent_style'] or 'aurora',
                    'surface_style': prefs['surface_style'] or 'glass',
                    'glow_intensity': prefs['glow_intensity'] if prefs['glow_intensity'] is not None else 55,
                    'table_glow': bool(prefs['table_glow']),
                    'welcome_message': prefs['welcome_message'] or '',
                    'panel_tint': prefs['panel_tint'] or 'cool',
                    'background_pattern': prefs['background_pattern'] or 'none',
                    'card_shape': prefs['card_shape'] or 'rounded',
                    'navigation_style': prefs['navigation_style'] or 'minimal',
                    'vignette': vignette_val,  # For backwards compatibility
                    'vignette_enabled': vignette_val,  # Correct key
                    'bleak_mode': bool(prefs['bleak_mode']),
                    'drawing_board_enabled': bool(prefs['drawing_board_enabled']) and plan_allows_drawing_board,
                    'drawing_board_layout': drawing_board_layout,
                }
            })
    except Exception as e:
        import traceback
        app.logger.error(f"Error in handle_settings: {str(e)}")
        app.logger.error(traceback.format_exc())
        return jsonify({'status': 'error', 'message': 'An unexpected error occurred. Please try again.'}), 500

@app.route('/verify_settings_2fa/<token>', methods=['GET', 'POST'])
@login_required
@csrf_protect
def verify_settings_2fa(token):
    """Verify 2FA code and apply settings changes."""
    r = get_redis_connection()
    pending_data_json = r.get(f"pending_settings:{token}")

    if not pending_data_json:
        return render_template('message.html', message='Invalid or expired verification token. Settings changes were not applied.')

    if request.method == 'POST':
        code = request.form.get('2fa_code', '').strip().upper()
        pending_data = json.loads(pending_data_json)

        if secrets.compare_digest(code, pending_data['2fa_code']):
            db = get_db()
            user_id = pending_data['user_id']
            data = pending_data['settings']

            # Verify user matches
            if user_id != session.get('user_id'):
                return render_template('message.html', message='Session mismatch. Please try again.')

            try:
                # Get current plan for drawing board validation
                plan_row = db.execute('SELECT plan FROM users WHERE id = %s', (user_id,)).fetchone()
                current_plan = normalize_plan(plan_row['plan']) if plan_row else 'Standard'
                drawing_board_allowed = current_plan in {'Premium', 'Enterprise'}

                # Apply basic settings
                primary_color = data.get('primary_color')
                background_image = data.get('background_image')
                ensure_user_layout_columns(db)
                layout_density = (data.get('layout_density') or 'cozy').lower()
                table_style = (data.get('table_style') or 'zebra').lower()
                allowed_layouts = {'cozy', 'compact', 'spacious'}
                allowed_tables = {'zebra', 'minimal', 'outlined'}
                if layout_density not in allowed_layouts:
                    layout_density = 'cozy'
                if table_style not in allowed_tables:
                    table_style = 'zebra'

                db.execute(
                    'UPDATE users SET primary_color = %s, background_image = %s, layout_density = %s, table_style = %s WHERE id = %s',
                    (primary_color, background_image, layout_density, table_style, user_id)
                )

                # Apply pro customizations if present
                pro_customizations = data.get('pro_customizations') if isinstance(data, dict) else None
                if pro_customizations is not None:
                    ambient_enabled = 1 if pro_customizations.get('ambient_enabled', True) else 0
                    accent_style = (pro_customizations.get('accent_style') or 'aurora').lower()
                    allowed_accents = {'none', 'aurora', 'nebula', 'prism', 'solstice', 'ion'}
                    if accent_style not in allowed_accents:
                        accent_style = 'aurora'

                    surface_style = (pro_customizations.get('surface_style') or 'glass').lower()
                    allowed_surfaces = {'none', 'glass', 'matte', 'carbon', 'opal', 'velvet', 'professional'}
                    if surface_style not in allowed_surfaces:
                        surface_style = 'glass'

                    try:
                        glow_intensity = float(pro_customizations.get('glow_intensity', 55))
                    except (TypeError, ValueError):
                        glow_intensity = 55

                    welcome_message = sanitize_html((pro_customizations.get('welcome_message') or '').strip())
                    table_glow = 1 if pro_customizations.get('table_glow', True) else 0

                    panel_tint = (pro_customizations.get('panel_tint') or 'cool').lower()
                    allowed_panels = {'none', 'cool', 'warm', 'midnight', 'neutral'}
                    if panel_tint not in allowed_panels:
                        panel_tint = 'cool'

                    background_pattern = (pro_customizations.get('background_pattern') or 'none').lower()
                    allowed_patterns = {'none', 'grid', 'mesh', 'circuit', 'dna_helix'}
                    if background_pattern not in allowed_patterns:
                        background_pattern = 'none'

                    card_shape = (pro_customizations.get('card_shape') or 'rounded').lower()
                    allowed_card_shapes = {'none', 'rounded', 'angular', 'pill'}
                    if card_shape not in allowed_card_shapes:
                        card_shape = 'rounded'

                    navigation_style = (pro_customizations.get('navigation_style') or 'minimal').lower()
                    allowed_nav_styles = {'none', 'minimal', 'floating', 'highlighted'}
                    if navigation_style not in allowed_nav_styles:
                        navigation_style = 'minimal'

                    vignette_enabled = 1 if pro_customizations.get('vignette_enabled', pro_customizations.get('vignette')) else 0
                    bleak_mode = 1 if pro_customizations.get('bleak_mode') else 0
                    drawing_board_enabled = 1 if pro_customizations.get('drawing_board_enabled') else 0
                    drawing_board_layout = sanitize_drawing_board_layout(
                        pro_customizations.get('drawing_board_layout', [])
                    )
                    if not drawing_board_allowed:
                        drawing_board_enabled = 0
                        existing_prefs = get_theme_preferences(db, user_id)
                        try:
                            previous_layout = json.loads(existing_prefs['drawing_board_layout'] or '[]')
                        except (TypeError, ValueError):
                            previous_layout = []
                        drawing_board_layout = sanitize_drawing_board_layout(previous_layout)
                    drawing_board_layout_json = json.dumps(drawing_board_layout)
                    ensure_theme_preferences_table(db)
                    db.execute(
                        '''INSERT INTO user_theme_preferences (user_id, ambient_enabled, accent_style, surface_style, glow_intensity, table_glow, welcome_message, panel_tint, background_pattern, card_shape, navigation_style, vignette_enabled, bleak_mode, drawing_board_enabled, drawing_board_layout)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT(user_id) DO UPDATE SET
                               ambient_enabled = excluded.ambient_enabled,
                               accent_style = excluded.accent_style,
                               surface_style = excluded.surface_style,
                               glow_intensity = excluded.glow_intensity,
                               table_glow = excluded.table_glow,
                               welcome_message = excluded.welcome_message,
                               panel_tint = excluded.panel_tint,
                               background_pattern = excluded.background_pattern,
                               card_shape = excluded.card_shape,
                               navigation_style = excluded.navigation_style,
                               vignette_enabled = excluded.vignette_enabled,
                               bleak_mode = excluded.bleak_mode,
                               drawing_board_enabled = excluded.drawing_board_enabled,
                               drawing_board_layout = excluded.drawing_board_layout''',
                        (
                            user_id,
                            ambient_enabled,
                            accent_style,
                            surface_style,
                            glow_intensity,
                            table_glow,
                            welcome_message,
                            panel_tint,
                            background_pattern,
                            card_shape,
                            navigation_style,
                            vignette_enabled,
                            bleak_mode,
                            drawing_board_enabled,
                            drawing_board_layout_json,
                        )
                    )

                db.commit()
                r.delete(f"pending_settings:{token}")
                return render_template('message.html', message='Your settings have been successfully updated.')
            except Exception as e:
                logger.error(f"Error applying settings for user {user_id}: {e}", exc_info=True)
                return render_template('message.html', message=f'Error applying settings: {str(e)}')
        else:
            return render_template('verify_settings_2fa.html', token=token, error='Invalid verification code. Please try again.')

    return render_template('verify_settings_2fa.html', token=token)


@app.route('/resend_settings_2fa/<token>', methods=['POST'])
@login_required
@csrf_protect
def resend_settings_2fa(token):
    """Resend 2FA code for settings verification."""
    r = get_redis_connection()
    pending_data_json = r.get(f"pending_settings:{token}")

    if not pending_data_json:
        return render_template('verify_settings_2fa.html', token=token, error='Invalid or expired verification token.')

    try:
        pending_data = json.loads(pending_data_json)
        user_id = pending_data['user_id']

        # Verify user matches
        if user_id != session.get('user_id'):
            return render_template('verify_settings_2fa.html', token=token, error='Session mismatch.')

        # Generate new 2FA code
        new_code = secrets.token_hex(3).upper()
        pending_data['2fa_code'] = new_code

        # Update Redis with new code (keep same expiration)
        ttl = r.ttl(f"pending_settings:{token}")
        if ttl > 0:
            r.setex(f"pending_settings:{token}", ttl, json.dumps(pending_data))
        else:
            # If TTL expired during processing, set a fresh 15 minutes
            r.setex(f"pending_settings:{token}", timedelta(minutes=15), json.dumps(pending_data))

        # Get user email and send new code
        db = get_db()
        user_credential = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()

        if user_credential:
            send_email(user_credential['email'], 'Verify Settings Change', f'Your new verification code is: {new_code}\n\nThis code will expire in 15 minutes.')
            return render_template('verify_settings_2fa.html', token=token, success='A new verification code has been sent to your email.')
        else:
            return render_template('verify_settings_2fa.html', token=token, error='Unable to send verification code.')
    except Exception as e:
        logger.error(f"Error resending settings 2FA for token {token}: {e}", exc_info=True)
        return render_template('verify_settings_2fa.html', token=token, error='Failed to resend verification code.')


@app.route('/api/settings/reset', methods=['POST'])
@login_required
@csrf_protect
def reset_settings():
    db = get_db()
    user_id = session['user_id']
    ensure_user_layout_columns(db)
    db.execute(
        'UPDATE users SET primary_color = %s, background_image = %s, layout_density = %s, table_style = %s WHERE id = %s',
        ('#4f46e5', '', 'cozy', 'zebra', user_id)
    )
    ensure_theme_preferences_table(db)
    db.execute(
        """INSERT INTO user_theme_preferences (user_id, ambient_enabled, accent_style, surface_style, glow_intensity, table_glow, welcome_message, panel_tint, background_pattern, card_shape, navigation_style, vignette_enabled, bleak_mode, drawing_board_enabled, drawing_board_layout)
           VALUES (%s, 1, 'aurora', 'glass', 55, 1, '', 'cool', 'none', 'rounded', 'minimal', 0, 0, 0, '[]')
           ON CONFLICT(user_id) DO UPDATE SET
               ambient_enabled = 1,
               accent_style = 'aurora',
               surface_style = 'glass',
               glow_intensity = 55,
               table_glow = 1,
               welcome_message = '',
               panel_tint = 'cool',
               background_pattern = 'none',
               card_shape = 'rounded',
               navigation_style = 'minimal',
               vignette_enabled = 0,
               bleak_mode = 0,
               drawing_board_enabled = 0,
               drawing_board_layout = '[]'""",
        (user_id,)
    )
    db.commit()
    return jsonify({'status': 'success', 'message': 'Settings reset successfully.'})


@app.route('/api/drawing-board/layout', methods=['GET', 'POST'])
@login_required
@csrf_protect
def drawing_board_layout():
    db = get_db()
    user_id = session['user_id']
    plan_row = db.execute('SELECT plan FROM users WHERE id = %s', (user_id,)).fetchone()
    if plan_row is None:
        session.clear()
        return jsonify({'status': 'error', 'message': 'User not found.'}), 404
    current_plan = normalize_plan(plan_row['plan'])
    drawing_board_allowed = current_plan in {'Premium', 'Enterprise'}
    ensure_theme_preferences_table(db)
    if request.method == 'GET':
        if not drawing_board_allowed:
            return (
                jsonify(
                    {
                        'layout': [],
                        'status': 'error',
                        'message': 'Drawing board is available on Premium plans.',
                    }
                ),
                403,
            )
        prefs = db.execute(
            'SELECT drawing_board_layout FROM user_theme_preferences WHERE user_id = %s',
            (user_id,),
        ).fetchone()
        layout = []
        if prefs:
            try:
                layout = json.loads(prefs['drawing_board_layout'] or '[]')
            except (TypeError, ValueError):
                layout = []
        return jsonify({'layout': layout})

    if not drawing_board_allowed:
        return (
            jsonify({'status': 'error', 'message': 'Drawing board is available on Premium plans.'}),
            403,
        )

    data = request.json or {}
    raw_layout = data.get('layout')
    sanitized = sanitize_drawing_board_layout(raw_layout)
    db.execute(
        '''INSERT INTO user_theme_preferences (user_id, drawing_board_layout)
               VALUES (%s, %s)
               ON CONFLICT(user_id) DO UPDATE SET
                   drawing_board_layout = excluded.drawing_board_layout''',
        (user_id, json.dumps(sanitized)),
    )
    db.commit()
    return jsonify({'status': 'success', 'layout': sanitized})


@app.route('/api/notifications', methods=['GET'])
@login_required
def get_notifications():
    db = get_db()
    user_id = session['user_id']
    ensure_notifications_table(db)
    limit = request.args.get('limit', 20)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20

    twenty_minutes_ago = datetime.utcnow() - timedelta(minutes=20)
    user = db.execute('SELECT notifications_last_cleared FROM users WHERE id = %s', (user_id,)).fetchone()
    last_cleared_time = parse_sqlite_timestamp(user['notifications_last_cleared']) if user and user['notifications_last_cleared'] else None

    query = 'SELECT id, title, message, category, related_job_id, is_read, created_at, metadata FROM notifications WHERE user_id = %s AND created_at > %s'
    params = [user_id, twenty_minutes_ago]

    if last_cleared_time:
        query += ' AND created_at > %s'
        params.append(last_cleared_time)

    query += ' ORDER BY created_at DESC LIMIT %s'
    params.append(limit)

    rows = db.execute(query, tuple(params)).fetchall()

    unread_query = 'SELECT COUNT(*) as count FROM notifications WHERE user_id = %s AND is_read = 0 AND created_at > %s'
    unread_params = [user_id, twenty_minutes_ago]
    if last_cleared_time:
        unread_query += ' AND created_at > %s'
        unread_params.append(last_cleared_time)

    unread_count = db.execute(unread_query, tuple(unread_params)).fetchone()['count']

    notifications = [
        {
            'id': row['id'],
            'title': row['title'],
            'message': row['message'],
            'category': row['category'],
            'related_job_id': row['related_job_id'],
            'is_read': bool(row['is_read']),
            'created_at': row['created_at'],
            'metadata': json.loads(row['metadata']) if row['metadata'] else None
        }
        for row in rows
    ]
    return jsonify({'notifications': notifications, 'unread_count': unread_count})


@app.route('/api/notifications/clear', methods=['POST'])
@login_required
@csrf_protect
def clear_notifications():
    db = get_db()
    user_id = session['user_id']
    db.execute('UPDATE users SET notifications_last_cleared = CURRENT_TIMESTAMP WHERE id = %s', (user_id,))
    db.commit()
    return jsonify({'status': 'success', 'message': 'Notifications cleared.'})

@app.route('/api/notifications/mark_read', methods=['POST'])
@login_required
@csrf_protect
def mark_notifications_read():
    db = get_db()
    user_id = session['user_id']
    ensure_notifications_table(db)
    data = request.get_json(silent=True) or {}
    mark_all = data.get('all')
    ids = data.get('ids') or []

    if mark_all:
        db.execute('UPDATE notifications SET is_read = 1 WHERE user_id = %s', (user_id,))
    elif isinstance(ids, list) and ids:
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'Invalid notification identifiers.'}), 400
        placeholders = ','.join('%s' for _ in ids)
        db.execute(
            f'UPDATE notifications SET is_read = 1 WHERE user_id = %s AND id IN ({placeholders})',
            (user_id, *ids)
        )
    else:
        return jsonify({'status': 'error', 'message': 'No notifications specified.'}), 400

    db.commit()
    unread_count = db.execute(
        'SELECT COUNT(*) as count FROM notifications WHERE user_id = %s AND is_read = 0',
        (user_id,)
    ).fetchone()['count']
    return jsonify({'status': 'success', 'unread_count': unread_count})

@app.route('/api/jobs/<job_id>/output_content', methods=['GET', 'PUT'])
@login_required
@csrf_protect
def handle_job_output_content(job_id):
    db = get_db()
    user_id = session['user_id']
    job = db.execute(
        'SELECT output_file, output_display_name, project_id, creator_id FROM jobs WHERE job_id = %s',
        (job_id,),
    ).fetchone()
    if not job:
        return jsonify({'status': 'error', 'message': 'Job output not found or access denied.'}), 404

    role = get_project_assignment(db, user_id, job['project_id'])
    if not role and job.get('creator_id') != user_id:
        return jsonify({'status': 'error', 'message': 'You do not have permission to access this content.'}), 403

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    storage_roots = get_project_storage_roots(job['project_id'], project['team_id'])
    project_path = storage_roots[0] if storage_roots else None
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Access denied.'}), 403

    candidate_paths = []

    stored_path = job['output_file'] if 'output_file' in job.keys() else None
    if stored_path:
        candidate_paths.append(os.path.abspath(stored_path))
        resolved_path = resolve_file_path(stored_path)
        if resolved_path:
            candidate_paths.append(os.path.abspath(resolved_path))

    job_output_dir = resolve_job_branch_path(
        project_path=project_path,
        job_id=job_id,
        output_file=stored_path,
        policy=None,
        branch='main',
        create=False,
    ) or os.path.join(project_path, 'jobs', job_id)
    # Legacy databases stored only the filename or display name. Try both before
    # falling back to the newest file in the job directory.
    display_name = (
        job['output_display_name'] if 'output_display_name' in job.keys() else None
    )
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
                (
                    os.path.abspath(os.path.join(scan_dir, entry))
                    for entry in os.listdir(scan_dir)
                ),
                key=os.path.getmtime,
                reverse=True,
            )
            candidate_paths.extend(job_files)
        except (FileNotFoundError, PermissionError, OSError):
            logger.warning(
                "Failed to enumerate job output directory for %s at %s.",
                job_id,
                scan_dir,
                exc_info=True,
            )

    file_path = next(
        (path for path in candidate_paths if os.path.exists(path) and os.path.isfile(path)),
        None,
    )

    if not file_path:
        return jsonify({'status': 'error', 'message': 'Job output not found or access denied.'}), 404

    file_path = os.path.abspath(file_path)
    real_file_path = os.path.realpath(file_path)
    allowed_prefixes = get_allowed_job_output_prefixes(storage_roots)

    def _is_within(candidate_path: str, root_path: str) -> bool:
        try:
            return os.path.commonpath([candidate_path, root_path]) == root_path
        except ValueError:
            return False

    is_safe = any(
        _is_within(file_path, root) or _is_within(real_file_path, os.path.realpath(root))
        for root in allowed_prefixes
    )

    if not is_safe:
        logger.warning(f"Access forbidden - job output outside allowed roots. File: {file_path}, Job: {job_id}")
        return jsonify({'status': 'error', 'message': 'Access to file path is forbidden.'}), 403

    # If the persisted output path is outdated but we found a valid file, update
    # the database so subsequent requests hit the canonical location directly.
    if stored_path != file_path:
        db.execute(
            'UPDATE jobs SET output_file = %s WHERE job_id = %s',
            (file_path, job_id),
        )
        db.commit()

    if request.method == 'GET':
        try:
            with open(file_path, 'rb') as f:
                raw_bytes = f.read()

            _, extension = os.path.splitext(file_path)
            normalized_extension = extension.lower().lstrip('.')

            if normalized_extension in {'gz', 'gzip'}:
                try:
                    raw_bytes = gzip.decompress(raw_bytes)
                except OSError:
                    logger.error(
                        "Failed to decompress gzipped job output %s for user %s.",
                        job_id,
                        user_id,
                        exc_info=True,
                    )
                    return jsonify({'status': 'error', 'message': 'Unable to decompress the job output file.'}), 500
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                _, secondary_ext = os.path.splitext(base_name)
                normalized_extension = secondary_ext.lower().lstrip('.') or 'pdb'

            try:
                decoded_content = raw_bytes.decode('utf-8')
                encoding_used = 'utf-8'
            except UnicodeDecodeError:
                decoded_content = raw_bytes.decode('latin-1')
                encoding_used = 'latin-1'

            # Check for Boltz2 docking-style output (separate protein and ligand files)
            docking_style = False
            protein_file = None
            ligand_file = None

            if os.path.isdir(job_output_dir):
                try:
                    job_files = os.listdir(job_output_dir)
                    # Look for protein-only and ligand files
                    protein_files = [f for f in job_files if '_protein' in f and (f.endswith('.cif') or f.endswith('.pdb'))]
                    ligand_files = [f for f in job_files if '_ligand' in f and f.endswith('.sdf')]

                    if protein_files and ligand_files:
                        docking_style = True
                        protein_file = protein_files[0]
                        ligand_file = ligand_files[0]
                except Exception:
                    pass

            return jsonify({
                'status': 'success',
                'content': decoded_content,
                'format': normalized_extension or 'pdb',
                'encoding': encoding_used,
                'size': len(raw_bytes),
                'docking_style': docking_style,
                'protein_file': protein_file,
                'ligand_file': ligand_file,
            })
        except FileNotFoundError:
            return jsonify({'status': 'error', 'message': 'Output file is missing from the server.'}), 404
        except Exception:
            logger.error(f"Error reading job output {job_id} for user {user_id}.", exc_info=True)
            return jsonify({'status': 'error', 'message': 'An internal error occurred while reading the job output.'}), 500

    if request.method == 'PUT':
        if role not in [PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR]:
            return jsonify({'status': 'error', 'message': 'You do not have permission to edit this file.'}), 403
        try:
            new_content = request.json.get('content', '')
            with open(file_path, 'w', errors='ignore') as f:
                f.write(new_content)
            return jsonify({'status': 'success', 'message': 'File saved successfully.'})
        except Exception:
            logger.error(f"Error writing to job output {job_id} for user {user_id}.", exc_info=True)
            return jsonify({'status': 'error', 'message': 'An internal error occurred while saving the file.'}), 500


@app.route('/api/user/settings', methods=['PATCH'])
@login_required
@csrf_protect
def update_user_settings():
    """Update user settings like email notification preferences."""
    try:
        db = get_db()
        user_id = session['user_id']
        data = request.json or {}

        # Get the user's credential ID
        user_row = db.execute('SELECT user_credential_id FROM users WHERE id = %s', (user_id,)).fetchone()
        if not user_row or not user_row['user_credential_id']:
            return jsonify({'status': 'error', 'message': 'User credentials not found.'}), 404

        credential_id = user_row['user_credential_id']

        # Update email notifications setting if provided
        if 'email_notifications_enabled' in data:
            enabled = 1 if data['email_notifications_enabled'] else 0
            db.execute(
                'UPDATE user_credentials SET email_notifications_enabled = %s WHERE id = %s',
                (enabled, credential_id)
            )
            db.commit()

        return jsonify({'status': 'success', 'message': 'Settings updated successfully.'})
    except Exception as e:
        logger.error(f"Error updating user settings for user {session.get('user_id')}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to update settings.'}), 500


def ensure_user_preferences_table(db):
    """Ensure user_preferences table exists with all columns."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            keyboard_shortcuts_enabled INTEGER NOT NULL DEFAULT 1,
            command_palette_enabled INTEGER NOT NULL DEFAULT 1,
            desktop_notifications_enabled INTEGER NOT NULL DEFAULT 0,
            sound_notifications_enabled INTEGER NOT NULL DEFAULT 0,
            email_digest_frequency TEXT NOT NULL DEFAULT 'none',
            auto_retry_failed_jobs INTEGER NOT NULL DEFAULT 0,
            auto_retry_max_attempts INTEGER NOT NULL DEFAULT 3,
            auto_retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
            default_project_id INTEGER,
            sidebar_collapsed INTEGER NOT NULL DEFAULT 0,
            sidebar_width INTEGER NOT NULL DEFAULT 240,
            theme_mode TEXT NOT NULL DEFAULT 'system',
            saved_searches TEXT DEFAULT '[]',
            recent_searches TEXT DEFAULT '[]',
            pinned_items TEXT DEFAULT '[]',
            onboarding_checklist TEXT DEFAULT '{}',
            preferences JSONB NOT NULL DEFAULT '{}',
            remember_me_enabled INTEGER NOT NULL DEFAULT 1,
            two_fa_trust_ip_enabled INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()

    columns = {
        col['column_name']
        for col in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_preferences'"
        ).fetchall()
    }
    # Self-heal drifted/legacy tables: CREATE TABLE IF NOT EXISTS is a no-op
    # when the table already exists, so add any missing feature column here.
    # Definitions must stay in sync with the CREATE TABLE block above.
    expected_columns = {
        'keyboard_shortcuts_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'command_palette_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'desktop_notifications_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'sound_notifications_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'email_digest_frequency': "TEXT NOT NULL DEFAULT 'none'",
        'auto_retry_failed_jobs': 'INTEGER NOT NULL DEFAULT 0',
        'auto_retry_max_attempts': 'INTEGER NOT NULL DEFAULT 3',
        'auto_retry_delay_minutes': 'INTEGER NOT NULL DEFAULT 5',
        'default_project_id': 'INTEGER',
        'sidebar_collapsed': 'INTEGER NOT NULL DEFAULT 0',
        'sidebar_width': 'INTEGER NOT NULL DEFAULT 240',
        'theme_mode': "TEXT NOT NULL DEFAULT 'system'",
        'saved_searches': "TEXT DEFAULT '[]'",
        'recent_searches': "TEXT DEFAULT '[]'",
        'pinned_items': "TEXT DEFAULT '[]'",
        'onboarding_checklist': "TEXT DEFAULT '{}'",
        'preferences': "JSONB NOT NULL DEFAULT '{}'",
        'remember_me_enabled': 'INTEGER NOT NULL DEFAULT 1',
        'two_fa_trust_ip_enabled': 'INTEGER NOT NULL DEFAULT 0',
        'developer_options_encrypted': 'TEXT',
        'developer_options_updated_at': 'TIMESTAMP',
    }
    altered = False
    for column_name, definition in expected_columns.items():
        if column_name not in columns:
            db.execute(f'ALTER TABLE user_preferences ADD COLUMN {column_name} {definition}')
            altered = True
    if altered:
        db.commit()


def get_user_preferences(db, user_id):
    """Get user preferences, creating default record if needed."""
    ensure_user_preferences_table(db)

    prefs = db.execute(
        'SELECT * FROM user_preferences WHERE user_id = %s',
        (user_id,)
    ).fetchone()

    if prefs is None:
        db.execute(
            'INSERT INTO user_preferences (user_id) VALUES (%s)',
            (user_id,)
        )
        db.commit()
        prefs = db.execute(
            'SELECT * FROM user_preferences WHERE user_id = %s',
            (user_id,)
        ).fetchone()

    return prefs


def _sanitize_developer_options(raw_options: Any) -> dict[str, Any]:
    """Validate and normalize developer options before encryption."""
    options = raw_options if isinstance(raw_options, dict) else {}

    transport = str(options.get('default_worker_transport') or 'auto').strip().lower()
    if transport not in {'auto', 'encrypted', 'signed'}:
        transport = 'auto'

    timeout_raw = options.get('request_timeout_seconds', 30)
    try:
        timeout_seconds = int(timeout_raw)
    except (TypeError, ValueError):
        timeout_seconds = 30
    timeout_seconds = max(5, min(300, timeout_seconds))

    return {
        'developer_mode': bool(options.get('developer_mode', False)),
        'show_debug_panels': bool(options.get('show_debug_panels', False)),
        'verbose_api_logging': bool(options.get('verbose_api_logging', False)),
        'allow_experimental_features': bool(options.get('allow_experimental_features', False)),
        'default_worker_transport': transport,
        'request_timeout_seconds': timeout_seconds,
    }


def _is_valid_user_api_key(db, user_id: int, api_key: str) -> bool:
    """Ensure API key belongs to the current user and is active."""
    if not isinstance(api_key, str):
        return False

    key = api_key.strip()
    if len(key) < 16:
        return False

    key_hash = hashlib.sha256(key.encode('utf-8')).hexdigest()
    try:
        public_or_worker = db.execute(
            '''
            SELECT id
            FROM api_keys
            WHERE user_id = %s AND key_hash = %s AND is_active = 1
            LIMIT 1
            ''',
            (user_id, key_hash),
        ).fetchone()
    except Exception:
        public_or_worker = None
    if public_or_worker:
        return True

    try:
        model_key = db.execute(
            '''
            SELECT id
            FROM user_model_api_keys
            WHERE user_id = %s AND key_hash = %s AND is_active = 1
            LIMIT 1
            ''',
            (user_id, key_hash),
        ).fetchone()
    except Exception:
        model_key = None
    return bool(model_key)


@app.route('/api/user/preferences', methods=['GET', 'POST'])
@login_required
@csrf_protect
def handle_user_preferences():
    """Get or update user preferences for QoL features."""
    try:
        db = get_db()
        user_id = session['user_id']

        if request.method == 'GET':
            prefs = get_user_preferences(db, user_id)

            # Parse JSON fields
            try:
                saved_searches = json.loads(prefs['saved_searches'] or '[]')
            except (TypeError, ValueError):
                saved_searches = []

            try:
                recent_searches = json.loads(prefs['recent_searches'] or '[]')
            except (TypeError, ValueError):
                recent_searches = []

            try:
                pinned_items = json.loads(prefs['pinned_items'] or '[]')
            except (TypeError, ValueError):
                pinned_items = []

            try:
                onboarding_checklist = json.loads(prefs['onboarding_checklist'] or '{}')
            except (TypeError, ValueError):
                onboarding_checklist = {}

            return jsonify({
                'keyboard_shortcuts_enabled': bool(prefs['keyboard_shortcuts_enabled']),
                'command_palette_enabled': bool(prefs['command_palette_enabled']),
                'desktop_notifications_enabled': bool(prefs['desktop_notifications_enabled']),
                'sound_notifications_enabled': bool(prefs['sound_notifications_enabled']),
                'email_digest_frequency': prefs['email_digest_frequency'] or 'none',
                'auto_retry_failed_jobs': bool(prefs['auto_retry_failed_jobs']),
                'auto_retry_max_attempts': prefs['auto_retry_max_attempts'] or 3,
                'auto_retry_delay_minutes': prefs['auto_retry_delay_minutes'] or 5,
                'default_project_id': prefs['default_project_id'],
                'sidebar_collapsed': bool(prefs['sidebar_collapsed']),
                'sidebar_width': prefs['sidebar_width'] or 240,
                'theme_mode': prefs['theme_mode'] or 'system',
                'saved_searches': saved_searches,
                'recent_searches': recent_searches,
                'pinned_items': pinned_items,
                'onboarding_checklist': onboarding_checklist,
                'remember_me_enabled': bool(prefs['remember_me_enabled']),
                'two_fa_trust_ip_enabled': bool(prefs['two_fa_trust_ip_enabled'])
            })

        # POST - update preferences
        data = request.json or {}
        ensure_user_preferences_table(db)

        # Build update query dynamically based on provided fields
        allowed_fields = {
            'keyboard_shortcuts_enabled': lambda v: 1 if v else 0,
            'command_palette_enabled': lambda v: 1 if v else 0,
            'desktop_notifications_enabled': lambda v: 1 if v else 0,
            'sound_notifications_enabled': lambda v: 1 if v else 0,
            'email_digest_frequency': lambda v: str(v) if v in ('none', 'daily', 'weekly') else 'none',
            'auto_retry_failed_jobs': lambda v: 1 if v else 0,
            'auto_retry_max_attempts': lambda v: max(1, min(5, int(v))) if v else 3,
            'auto_retry_delay_minutes': lambda v: max(1, min(60, int(v))) if v else 5,
            'default_project_id': lambda v: int(v) if v else None,
            'sidebar_collapsed': lambda v: 1 if v else 0,
            'sidebar_width': lambda v: max(180, min(400, int(v))) if v else 240,
            'theme_mode': lambda v: str(v) if v in ('system', 'light', 'dark') else 'system',
            'saved_searches': lambda v: json.dumps(v) if isinstance(v, list) else '[]',
            'recent_searches': lambda v: json.dumps(v) if isinstance(v, list) else '[]',
            'pinned_items': lambda v: json.dumps(v) if isinstance(v, list) else '[]',
            'onboarding_checklist': lambda v: json.dumps(v) if isinstance(v, dict) else '{}',
            'remember_me_enabled': lambda v: 1 if v else 0,
            'two_fa_trust_ip_enabled': lambda v: 1 if v else 0
        }

        updates = []
        values = []

        for field, converter in allowed_fields.items():
            if field in data:
                try:
                    updates.append(f'{field} = %s')
                    values.append(converter(data[field]))
                except (TypeError, ValueError):
                    pass

        if updates:
            updates.append('updated_at = CURRENT_TIMESTAMP')
            values.append(user_id)

            # Upsert
            db.execute(f'''
                INSERT INTO user_preferences (user_id)
                VALUES (%s)
                ON CONFLICT(user_id) DO UPDATE SET {', '.join(updates[:-1])}, updated_at = CURRENT_TIMESTAMP
            ''', (user_id, *values[:-1]))
            db.commit()

        # Disabling these security toggles must immediately revoke the associated
        # tokens server-side (source of truth), so it works even when toggled from
        # a different browser than the one holding the cookie.
        if 'remember_me_enabled' in data and not data['remember_me_enabled']:
            revoke_all_remember_tokens(db, user_id)
        if 'two_fa_trust_ip_enabled' in data and not data['two_fa_trust_ip_enabled']:
            revoke_all_trust_devices(db, user_id)

        return jsonify({'status': 'success', 'message': 'Preferences updated.'})

    except Exception as e:
        logger.error(f"Error handling user preferences for user {session.get('user_id')}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to handle preferences.'}), 500


@app.route('/api/user/developer-options/meta', methods=['GET'])
@login_required
def developer_options_meta():
    """Return whether encrypted developer options exist for the current user."""
    try:
        db = get_db()
        user_id = session['user_id']
        prefs = get_user_preferences(db, user_id)
        updated_at = prefs['developer_options_updated_at'] if 'developer_options_updated_at' in prefs.keys() else None

        return jsonify({
            'status': 'success',
            'has_options': bool(prefs['developer_options_encrypted']) if 'developer_options_encrypted' in prefs.keys() else False,
            'updated_at': serialize_datetime(updated_at),
        })
    except Exception as e:
        logger.error(f"Error loading developer options meta for user {session.get('user_id')}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to load developer options metadata.'}), 500


@app.route('/api/user/developer-options', methods=['POST'])
@login_required
@csrf_protect
@rate_limit(limit=20, window=300, action='developer_options_save')
def save_developer_options():
    """Persist developer options encrypted with a user-owned API key."""
    try:
        from phonos_app.helpers.user_model_crypto import encrypt_data

        db = get_db()
        user_id = session['user_id']
        data = request.get_json(silent=True) or {}
        api_key = str(data.get('api_key') or '').strip()
        raw_options = data.get('options')

        if not api_key:
            return jsonify({'status': 'error', 'message': 'API key is required.'}), 400
        if not isinstance(raw_options, dict):
            return jsonify({'status': 'error', 'message': 'Developer options object is required.'}), 400
        if not _is_valid_user_api_key(db, user_id, api_key):
            return jsonify({'status': 'error', 'message': 'Invalid or inactive API key.'}), 403

        options = _sanitize_developer_options(raw_options)
        encrypted_options = encrypt_data(options, api_key)

        ensure_user_preferences_table(db)
        db.execute(
            '''
            INSERT INTO user_preferences (user_id, developer_options_encrypted, developer_options_updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                developer_options_encrypted = excluded.developer_options_encrypted,
                developer_options_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (user_id, encrypted_options),
        )
        db.commit()

        return jsonify({
            'status': 'success',
            'message': 'Developer options saved with API-key encryption.',
            'options': options,
        })
    except Exception as e:
        logger.error(f"Error saving developer options for user {session.get('user_id')}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to save developer options securely.'}), 500


@app.route('/api/user/developer-options/reveal', methods=['POST'])
@login_required
@csrf_protect
@rate_limit(limit=30, window=300, action='developer_options_reveal')
def reveal_developer_options():
    """Decrypt and return developer options with a valid user API key."""
    try:
        from phonos_app.helpers.user_model_crypto import decrypt_data

        db = get_db()
        user_id = session['user_id']
        data = request.get_json(silent=True) or {}
        api_key = str(data.get('api_key') or '').strip()

        if not api_key:
            return jsonify({'status': 'error', 'message': 'API key is required.'}), 400
        if not _is_valid_user_api_key(db, user_id, api_key):
            return jsonify({'status': 'error', 'message': 'Invalid or inactive API key.'}), 403

        prefs = get_user_preferences(db, user_id)
        encrypted_options = prefs['developer_options_encrypted'] if 'developer_options_encrypted' in prefs.keys() else None
        updated_at = prefs['developer_options_updated_at'] if 'developer_options_updated_at' in prefs.keys() else None
        if not encrypted_options:
            return jsonify({'status': 'error', 'message': 'No developer options have been saved yet.'}), 404

        try:
            decrypted = decrypt_data(encrypted_options, api_key)
        except Exception:
            return jsonify({'status': 'error', 'message': 'Unable to decrypt with the provided API key.'}), 403

        if isinstance(decrypted, bytes):
            try:
                decrypted = json.loads(decrypted.decode('utf-8'))
            except (TypeError, ValueError, UnicodeDecodeError):
                decrypted = {}
        if not isinstance(decrypted, dict):
            decrypted = {}

        options = _sanitize_developer_options(decrypted)
        return jsonify({
            'status': 'success',
            'options': options,
            'updated_at': serialize_datetime(updated_at),
        })
    except Exception as e:
        logger.error(f"Error revealing developer options for user {session.get('user_id')}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to decrypt developer options.'}), 500


@app.route('/api/user/developer-options/clear', methods=['POST'])
@login_required
@csrf_protect
def clear_developer_options():
    """Clear encrypted developer options for the current user."""
    try:
        db = get_db()
        user_id = session['user_id']
        ensure_user_preferences_table(db)

        db.execute(
            '''
            INSERT INTO user_preferences (user_id, developer_options_encrypted, developer_options_updated_at)
            VALUES (%s, NULL, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                developer_options_encrypted = NULL,
                developer_options_updated_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (user_id,),
        )
        db.commit()
        return jsonify({'status': 'success', 'message': 'Developer options cleared.'})
    except Exception as e:
        logger.error(f"Error clearing developer options for user {session.get('user_id')}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to clear developer options.'}), 500


@app.route('/api/search/history', methods=['GET', 'POST', 'DELETE'])
@login_required
@csrf_protect
def handle_search_history():
    """Manage search history."""
    try:
        db = get_db()
        user_id = session['user_id']

        # Ensure table exists
        db.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                query TEXT NOT NULL,
                search_type TEXT NOT NULL DEFAULT 'general',
                filters TEXT,
                result_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

        if request.method == 'GET':
            limit = min(50, max(1, int(request.args.get('limit', 20))))
            rows = db.execute('''
                SELECT id, query, search_type, filters, result_count, created_at
                FROM search_history
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            ''', (user_id, limit)).fetchall()

            return jsonify({
                'history': [
                    {
                        'id': row['id'],
                        'query': row['query'],
                        'search_type': row['search_type'],
                        'filters': json.loads(row['filters']) if row['filters'] else None,
                        'result_count': row['result_count'],
                        'created_at': serialize_datetime(row['created_at'])
                    }
                    for row in rows
                ]
            })

        if request.method == 'POST':
            data = request.json or {}
            query = sanitize_input(data.get('query', ''))
            search_type = sanitize_input(data.get('search_type', 'general'))
            filters = json.dumps(data.get('filters')) if data.get('filters') else None
            result_count = data.get('result_count')

            if query:
                db.execute('''
                    INSERT INTO search_history (user_id, query, search_type, filters, result_count)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (user_id, query, search_type, filters, result_count))
                db.commit()

            return jsonify({'status': 'success'})

        if request.method == 'DELETE':
            data = request.json or {}
            history_id = data.get('id')

            if history_id:
                db.execute('DELETE FROM search_history WHERE id = %s AND user_id = %s', (history_id, user_id))
            else:
                # Clear all history
                db.execute('DELETE FROM search_history WHERE user_id = %s', (user_id,))

            db.commit()
            return jsonify({'status': 'success', 'message': 'Search history cleared.'})

    except Exception as e:
        logger.error(f"Error handling search history: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to handle search history.'}), 500


@app.route('/api/search/saved', methods=['GET', 'POST', 'DELETE'])
@login_required
@csrf_protect
def handle_saved_searches():
    """Manage saved searches."""
    try:
        db = get_db()
        user_id = session['user_id']

        # Ensure table exists
        db.execute("""
            CREATE TABLE IF NOT EXISTS saved_searches (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                query TEXT NOT NULL,
                search_type TEXT NOT NULL DEFAULT 'general',
                filters TEXT,
                is_pinned INTEGER NOT NULL DEFAULT 0,
                use_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

        if request.method == 'GET':
            rows = db.execute('''
                SELECT id, name, query, search_type, filters, is_pinned, use_count, created_at
                FROM saved_searches
                WHERE user_id = %s
                ORDER BY is_pinned DESC, use_count DESC, created_at DESC
            ''', (user_id,)).fetchall()

            return jsonify({
                'saved_searches': [
                    {
                        'id': row['id'],
                        'name': row['name'],
                        'query': row['query'],
                        'search_type': row['search_type'],
                        'filters': json.loads(row['filters']) if row['filters'] else None,
                        'is_pinned': bool(row['is_pinned']),
                        'use_count': row['use_count'],
                        'created_at': serialize_datetime(row['created_at'])
                    }
                    for row in rows
                ]
            })

        if request.method == 'POST':
            data = request.json or {}
            name = sanitize_input(data.get('name', 'Saved Search'))
            query = sanitize_input(data.get('query', ''))
            search_type = sanitize_input(data.get('search_type', 'general'))
            filters = json.dumps(data.get('filters')) if data.get('filters') else None

            if query:
                db.execute('''
                    INSERT INTO saved_searches (user_id, name, query, search_type, filters)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (user_id, name, query, search_type, filters))
                db.commit()

                return jsonify({'status': 'success', 'message': 'Search saved.'})

            return jsonify({'status': 'error', 'message': 'Query is required.'}), 400

        if request.method == 'DELETE':
            data = request.json or {}
            search_id = data.get('id')

            if search_id:
                db.execute('DELETE FROM saved_searches WHERE id = %s AND user_id = %s', (search_id, user_id))
                db.commit()
                return jsonify({'status': 'success', 'message': 'Saved search deleted.'})

            return jsonify({'status': 'error', 'message': 'Search ID required.'}), 400

    except Exception as e:
        logger.error(f"Error handling saved searches: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to handle saved searches.'}), 500


@app.route('/api/admin/disk-health', methods=['GET'])
@login_required
@admin_required
def get_disk_health():
    """Get health status of all configured storage disks.

    Returns disk usage, availability, and alerts for administrators.
    """
    try:
        dm = get_disk_manager()
        health = dm.get_health_status()
        return jsonify(health)
    except Exception as e:
        logger.error(f"Error getting disk health: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/admin/disk-stats', methods=['GET'])
@login_required
@admin_required
def get_disk_stats():
    """Get aggregate storage statistics across all disks.

    Returns total storage, usage, and per-disk breakdown.
    """
    try:
        dm = get_disk_manager()
        stats = dm.get_total_storage_stats()
        return jsonify(stats)
    except Exception as e:
        logger.error(f"Error getting disk stats: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/admin/disk-config/reload', methods=['POST'])
@login_required
@admin_required
@csrf_protect
def reload_disk_config():
    """Reload disk configuration from file.

    Useful after modifying /etc/phonos/disk_config.json.
    """
    try:
        dm = get_disk_manager()
        dm.reload_config()
        return jsonify({
            'status': 'success',
            'message': 'Disk configuration reloaded',
            'config_path': dm.config_path
        })
    except Exception as e:
        logger.error(f"Error reloading disk config: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500
