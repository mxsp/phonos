"""API routes for help desk tickets."""
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
BUG_REPORT_RECIPIENT = os.environ.get('BUG_REPORT_RECIPIENT', '').strip()
BUG_REPORT_ALLOWED_AREAS = {'dashboard', 'projects', 'jobs', 'files', 'notebook', 'settings', 'account', 'other'}
BUG_REPORT_ALLOWED_SEVERITIES = {'low', 'medium', 'high', 'critical'}
BUG_REPORT_TICKET_SUBJECT_PREFIX = '[bug]'


def _coerce_bug_report_text(value: object, max_length: int) -> str:
    if not isinstance(value, str):
        return ''
    return sanitize_input(value, max_length=max_length)


def _send_fallback_bug_report_ticket_email(
    *,
    db,
    user_id: int,
    ticket_id: int,
    subject: str,
    message: str,
) -> bool:
    reporter_row = db.execute(
        """
        SELECT u.username, uc.email
        FROM users u
        LEFT JOIN user_credentials uc ON uc.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    ).fetchone()

    reporter_username = _coerce_bug_report_text(
        reporter_row['username'] if reporter_row and reporter_row['username'] else f'user-{user_id}',
        max_length=120,
    )
    reporter_email = _coerce_bug_report_text(
        reporter_row['email'] if reporter_row and reporter_row['email'] else 'Unknown',
        max_length=254,
    )

    submitted_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    safe_subject = _coerce_bug_report_text(subject, max_length=120)

    email_subject = f'[Phonos Bug Report] [Fallback] {safe_subject}'
    email_body = (
        '<h2>New Bug Report (Fallback via /api/tickets)</h2>'
        '<p>The bug report endpoint was unavailable, so this report was submitted through the support ticket API.</p>'
        '<h3>Reporter</h3>'
        '<ul>'
        f'<li><strong>User ID:</strong> {user_id}</li>'
        f'<li><strong>Username:</strong> {reporter_username}</li>'
        f'<li><strong>Email:</strong> {reporter_email}</li>'
        f'<li><strong>Ticket ID:</strong> {ticket_id or "Unknown"}</li>'
        f'<li><strong>Submitted At:</strong> {submitted_at}</li>'
        f'<li><strong>IP Address:</strong> {_coerce_bug_report_text(request.remote_addr or "", max_length=120)}</li>'
        '</ul>'
        '<h3>Submitted Subject</h3>'
        f'<p>{subject}</p>'
        '<h3>Submitted Message</h3>'
        f'<p>{message}</p>'
    )

    return send_email(BUG_REPORT_RECIPIENT, email_subject, email_body, async_=False)


@app.route('/api/tickets', methods=['GET', 'POST'])
@login_required
@csrf_protect
def handle_tickets():
    db = get_db()
    user_id = session['user_id']
    user = db.execute('SELECT is_admin FROM users WHERE id = %s', (user_id,)).fetchone()

    if request.method == 'POST':
        data = request.json or {}
        subject = data.get('subject')
        message = data.get('message')
        priority = data.get('priority', 'Medium')
        category = data.get('category', 'General')

        if not subject or not message:
            return jsonify({'status': 'error', 'message': 'Subject and message are required.'}), 400

        if priority not in ['Low', 'Medium', 'High']:
            return jsonify({'status': 'error', 'message': 'Invalid priority level.'}), 400

        if category not in ['General', 'Billing', 'Technical Support', 'Account']:
            return jsonify({'status': 'error', 'message': 'Invalid category.'}), 400

        # Sanitize user input for XSS protection
        sanitized_subject = sanitize_input(subject, max_length=500)
        sanitized_message = sanitize_html(message)
        is_bug_report_ticket = (
            category == 'Technical Support'
            and sanitized_subject.strip().lower().startswith(BUG_REPORT_TICKET_SUBJECT_PREFIX)
        )

        try:
            cursor = db.execute(
                'INSERT INTO help_tickets (user_id, subject, message, priority, category) VALUES (%s, %s, %s, %s, %s)',
                (user_id, sanitized_subject, sanitized_message, priority, category)
            )
            result = cursor.fetchone()

            ticket_id = result["id"] if result else None
            if is_bug_report_ticket:
                email_sent = _send_fallback_bug_report_ticket_email(
                    db=db,
                    user_id=user_id,
                    ticket_id=ticket_id,
                    subject=sanitized_subject,
                    message=sanitized_message,
                )
                if not email_sent:
                    db.rollback()
                    logger.error('Failed to send fallback bug report email for ticket %s (user %s)', ticket_id, user_id)
                    return jsonify({'status': 'error', 'message': 'Failed to send bug report email. Please try again later.'}), 500

            db.commit()
            return jsonify({'status': 'success', 'message': 'Ticket submitted successfully.', 'ticket_id': ticket_id}), 201
        except psycopg2.Error as e:
            logger.error(f"Database error while creating ticket for user {user_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to create ticket.'}), 500

    if request.method == 'GET':
        try:
            is_admin = bool(user and user['is_admin'])

            base_query = """
                SELECT t.id, t.user_id, t.subject, t.message, t.status, t.priority, t.category,
                       t.created_at, t.updated_at,
                       u.username AS requester_username,
                       a.username AS assignee_username
                FROM help_tickets t
                JOIN users u ON t.user_id = u.id
                LEFT JOIN users a ON t.assignee_id = a.id
            """

            base_conditions: list[str] = []
            base_params: list[object] = []

            if not is_admin:
                base_conditions.append('t.user_id = ?')
                base_params.append(user_id)

            priority_filter = request.args.get('priority')
            if priority_filter:
                base_conditions.append('t.priority = ?')
                base_params.append(priority_filter)

            category_filter = request.args.get('category')
            if category_filter:
                base_conditions.append('t.category = ?')
                base_params.append(category_filter)

            search_query = (request.args.get('search') or '').strip()
            if search_query:
                like = f"%{search_query.lower()}%"
                base_conditions.append('(lower(t.subject) LIKE %s OR lower(t.message) LIKE %s)')
                base_params.extend([like, like])

            status_filter = request.args.get('status')
            conditions = list(base_conditions)
            params = list(base_params)
            if status_filter:
                conditions.append('t.status = ?')
                params.append(status_filter)

            where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ''
            order_clause = ' ORDER BY t.updated_at DESC'

            tickets_rows = db.execute(base_query + where_clause + order_clause, params).fetchall()
            tickets: list[dict] = []
            for row in tickets_rows:
                ticket = dict(row)
                ticket['requester'] = ticket.pop('requester_username')
                ticket['assignee'] = ticket.pop('assignee_username')
                tickets.append(ticket)

            count_query = 'SELECT t.status, COUNT(*) as count FROM help_tickets t'
            if base_conditions:
                count_query += f" WHERE {' AND '.join(base_conditions)}"
            count_query += ' GROUP BY t.status'
            status_counts_rows = db.execute(count_query, base_params).fetchall()
            status_counts = {row['status']: row['count'] for row in status_counts_rows}
            total_count = sum(status_counts.values())

            response = {
                'tickets': tickets,
                'summary': {
                    'total': total_count,
                    'by_status': status_counts
                }
            }

            return jsonify(response)
        except psycopg2.Error as e:
            logger.error(f"Database error while fetching tickets for user {user_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to retrieve tickets.'}), 500


@app.route('/api/bug-reports', methods=['POST'])
@login_required
@csrf_protect
@rate_limit(limit=8, window=3600, action='bug_report_submit')
def submit_bug_report():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'status': 'error', 'message': 'Invalid request payload.'}), 400

    raw_title = (data.get('title') or '').strip() if isinstance(data.get('title'), str) else ''
    raw_steps = (data.get('steps_to_reproduce') or '').strip() if isinstance(data.get('steps_to_reproduce'), str) else ''
    raw_expected = (data.get('expected_behavior') or '').strip() if isinstance(data.get('expected_behavior'), str) else ''
    raw_actual = (data.get('actual_behavior') or '').strip() if isinstance(data.get('actual_behavior'), str) else ''

    if len(raw_title) < 8:
        return jsonify({'status': 'error', 'message': 'Title must be at least 8 characters.'}), 400
    if len(raw_steps) < 10:
        return jsonify({'status': 'error', 'message': 'Steps to reproduce are required.'}), 400
    if len(raw_expected) < 5:
        return jsonify({'status': 'error', 'message': 'Expected behavior is required.'}), 400
    if len(raw_actual) < 5:
        return jsonify({'status': 'error', 'message': 'Actual behavior is required.'}), 400

    area_value = (data.get('area') or '').strip().lower() if isinstance(data.get('area'), str) else 'other'
    severity_value = (data.get('severity') or '').strip().lower() if isinstance(data.get('severity'), str) else 'medium'

    area = area_value if area_value in BUG_REPORT_ALLOWED_AREAS else 'other'
    severity = severity_value if severity_value in BUG_REPORT_ALLOWED_SEVERITIES else 'medium'

    title = _coerce_bug_report_text(raw_title, max_length=140)
    steps_to_reproduce = _coerce_bug_report_text(raw_steps, max_length=4000)
    expected_behavior = _coerce_bug_report_text(raw_expected, max_length=2000)
    actual_behavior = _coerce_bug_report_text(raw_actual, max_length=2000)
    additional_context = _coerce_bug_report_text(data.get('additional_context'), max_length=2000)
    current_view = _coerce_bug_report_text(data.get('current_view'), max_length=120)
    page_url = _coerce_bug_report_text(data.get('page_url') or request.referrer or '', max_length=1000)

    client_details = data.get('client_details') if isinstance(data.get('client_details'), dict) else {}
    client_user_agent = _coerce_bug_report_text(client_details.get('user_agent'), max_length=1000)
    client_platform = _coerce_bug_report_text(client_details.get('platform'), max_length=120)
    client_language = _coerce_bug_report_text(client_details.get('language'), max_length=32)
    client_viewport = _coerce_bug_report_text(client_details.get('viewport'), max_length=64)
    client_screen = _coerce_bug_report_text(client_details.get('screen'), max_length=64)
    client_timezone = _coerce_bug_report_text(client_details.get('timezone'), max_length=100)

    user_id = session['user_id']
    db = get_db()
    reporter_row = db.execute(
        """
        SELECT u.username, uc.email
        FROM users u
        LEFT JOIN user_credentials uc ON uc.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    ).fetchone()

    reporter_username = _coerce_bug_report_text(
        reporter_row['username'] if reporter_row and reporter_row['username'] else f'user-{user_id}',
        max_length=120,
    )
    reporter_email = _coerce_bug_report_text(
        reporter_row['email'] if reporter_row and reporter_row['email'] else 'Unknown',
        max_length=254,
    )

    submitted_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    severity_label = severity.upper()
    area_label = area.replace('-', ' ').title()
    subject_title = raw_title.replace('\n', ' ').replace('\r', ' ')[:120]
    subject = f'[Phonos Bug Report] [{severity_label}] {subject_title}'

    def _text_block(text: str) -> str:
        return text.replace('\n', '<br>')

    metadata_rows = []
    if client_user_agent:
        metadata_rows.append(f'<li><strong>User Agent:</strong> {client_user_agent}</li>')
    if client_platform:
        metadata_rows.append(f'<li><strong>Platform:</strong> {client_platform}</li>')
    if client_language:
        metadata_rows.append(f'<li><strong>Language:</strong> {client_language}</li>')
    if client_viewport:
        metadata_rows.append(f'<li><strong>Viewport:</strong> {client_viewport}</li>')
    if client_screen:
        metadata_rows.append(f'<li><strong>Screen:</strong> {client_screen}</li>')
    if client_timezone:
        metadata_rows.append(f'<li><strong>Timezone:</strong> {client_timezone}</li>')
    metadata_html = ''.join(metadata_rows) if metadata_rows else '<li>No client metadata provided.</li>'

    additional_context_html = _text_block(additional_context) if additional_context else '<em>None provided.</em>'

    email_body = (
        '<h2>New Bug Report</h2>'
        '<p>A new bug report was submitted from the web app.</p>'
        '<h3>Reporter</h3>'
        '<ul>'
        f'<li><strong>User ID:</strong> {user_id}</li>'
        f'<li><strong>Username:</strong> {reporter_username}</li>'
        f'<li><strong>Email:</strong> {reporter_email}</li>'
        f'<li><strong>Submitted At:</strong> {submitted_at}</li>'
        f'<li><strong>IP Address:</strong> {_coerce_bug_report_text(request.remote_addr or "", max_length=120)}</li>'
        '</ul>'
        '<h3>Issue Summary</h3>'
        '<ul>'
        f'<li><strong>Title:</strong> {title}</li>'
        f'<li><strong>Affected Area:</strong> {area_label}</li>'
        f'<li><strong>Severity:</strong> {severity_label}</li>'
        f'<li><strong>Current View:</strong> {current_view or "Unknown"}</li>'
        f'<li><strong>Page URL:</strong> {page_url or "Unknown"}</li>'
        '</ul>'
        '<h3>Steps to Reproduce</h3>'
        f'<p>{_text_block(steps_to_reproduce)}</p>'
        '<h3>Expected Behavior</h3>'
        f'<p>{_text_block(expected_behavior)}</p>'
        '<h3>Actual Behavior</h3>'
        f'<p>{_text_block(actual_behavior)}</p>'
        '<h3>Additional Context</h3>'
        f'<p>{additional_context_html}</p>'
        '<h3>Client Metadata</h3>'
        f'<ul>{metadata_html}</ul>'
    )

    email_sent = send_email(BUG_REPORT_RECIPIENT, subject, email_body, async_=False)
    if not email_sent:
        logger.error('Failed to send bug report email for user %s', user_id)
        return jsonify({'status': 'error', 'message': 'Failed to send bug report email. Please try again later.'}), 500

    logger.info('Bug report submitted by user %s (%s)', user_id, reporter_username)
    return jsonify({'status': 'success', 'message': 'Bug report submitted successfully.'}), 201

@app.route('/api/tickets/<int:ticket_id>/attachments', methods=['POST'])
@login_required
@csrf_protect
def upload_ticket_attachment(ticket_id):
    db = get_db()
    user_id = session['user_id']

    ticket_info = db.execute('SELECT user_id FROM help_tickets WHERE id = %s', (ticket_id,)).fetchone()
    if not ticket_info:
        return jsonify({'status': 'error', 'message': 'Ticket not found.'}), 404

    user = db.execute('SELECT is_admin FROM users WHERE id = %s', (user_id,)).fetchone()
    if not user['is_admin'] and ticket_info['user_id'] != user_id:
        return jsonify({'status': 'error', 'message': 'You do not have permission to attach files to this ticket.'}), 403

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part in the request.'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file.'}), 400

    reply_id = request.form.get('reply_id')

    if file:
        filename = secure_filename(file.filename)
        upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'ticket_attachments')
        os.makedirs(upload_dir, exist_ok=True)

        unique_filename = f"{ticket_id}_{int(time.time())}_{filename}"
        filepath = os.path.join(upload_dir, unique_filename)

        # Save to temporary location first
        temp_path = filepath + '.tmp'
        file.save(temp_path)

        # Scan the file
        is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
        if not is_safe:
            # temp_path is automatically deleted by scan_upload
            return jsonify({'status': 'error', 'message': message}), 400

        # Move to final location only if safe
        os.rename(temp_path, filepath)

        db.execute(
            'INSERT INTO help_ticket_attachments (ticket_id, reply_id, file_path, friendly_name) VALUES (%s, %s, %s, %s)',
            (ticket_id, reply_id, filepath, filename)
        )
        db.commit()
        return jsonify({'status': 'success', 'message': 'File uploaded successfully.'})

    return jsonify({'status': 'error', 'message': 'File upload failed.'}), 400

@app.route('/api/tickets/<int:ticket_id>', methods=['GET'])
@login_required
def get_ticket(ticket_id):
    db = get_db()
    user_id = session['user_id']
    user = db.execute('SELECT is_admin FROM users WHERE id = %s', (user_id,)).fetchone()

    ticket_q = db.execute(
        """
        SELECT t.id, t.user_id, t.subject, t.message, t.status, t.priority, t.category, t.created_at, u.username, a.username as assignee_name
        FROM help_tickets t
        JOIN users u ON t.user_id = u.id
        LEFT JOIN users a ON t.assignee_id = a.id
        WHERE t.id = %s
        """,
        (ticket_id,)
    ).fetchone()

    if not ticket_q:
        return jsonify({'status': 'error', 'message': 'Ticket not found.'}), 404

    # Permission check: allow if user is admin or the ticket owner
    if not user['is_admin'] and ticket_q['user_id'] != user_id:
        return jsonify({'status': 'error', 'message': 'Access denied.'}), 403

    replies_q = db.execute(
        """
        SELECT r.id, r.message, r.created_at, u.username, u.is_admin
        FROM help_ticket_replies r
        JOIN users u ON r.user_id = u.id
        WHERE r.ticket_id = %s
        ORDER BY r.created_at ASC
        """,
        (ticket_id,)
    ).fetchall()

    attachments_q = db.execute(
        "SELECT id, reply_id, friendly_name, file_path FROM help_ticket_attachments WHERE ticket_id = %s",
        (ticket_id,)
    ).fetchall()

    ticket = dict(ticket_q)
    replies = [dict(row) for row in replies_q]
    attachments = [dict(row) for row in attachments_q]

    ticket_attachments = [a for a in attachments if a['reply_id'] is None]

    for reply in replies:
        reply['attachments'] = [a for a in attachments if a['reply_id'] == reply['id']]

    admins_q = db.execute("SELECT id, username FROM users WHERE is_admin = 1").fetchall()
    admins = [dict(row) for row in admins_q]

    return jsonify({
        'ticket': ticket,
        'replies': replies,
        'attachments': ticket_attachments,
        'admins': admins
    })

@app.route('/api/tickets/<int:ticket_id>/replies', methods=['POST'])
@login_required
@csrf_protect
def post_ticket_reply(ticket_id):
    db = get_db()
    user_id = session['user_id']
    message = request.form.get('message')

    if not message:
        return jsonify({'status': 'error', 'message': 'Reply message cannot be empty.'}), 400

    ticket_info = db.execute('SELECT user_id, subject FROM help_tickets WHERE id = %s', (ticket_id,)).fetchone()
    if not ticket_info:
        return jsonify({'status': 'error', 'message': 'Ticket not found.'}), 404

    ticket_creator_id = ticket_info['user_id']
    ticket_subject = ticket_info['subject']

    current_user = db.execute('SELECT is_admin FROM users WHERE id = %s', (user_id,)).fetchone()

    # Sanitize reply message for XSS protection
    sanitized_message = sanitize_html(message)

    cursor = db.execute(
        'INSERT INTO help_ticket_replies (ticket_id, user_id, message) VALUES (%s, %s, %s)',
        (ticket_id, user_id, sanitized_message)
    )
    result = cursor.fetchone()

    reply_id = result["id"] if result else None

    if 'file' in request.files:
        file = request.files['file']
        if file.filename != '':
            filename = secure_filename(file.filename)
            upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'ticket_attachments')
            os.makedirs(upload_dir, exist_ok=True)

            unique_filename = f"{ticket_id}_{reply_id}_{int(time.time())}_{filename}"
            filepath = os.path.join(upload_dir, unique_filename)

            # Save to temporary location first
            temp_path = filepath + '.tmp'
            file.save(temp_path)

            # Scan the file
            is_safe, message_scan = scan_upload(temp_path, filename, user_id, request.remote_addr)
            if not is_safe:
                # temp_path is automatically deleted by scan_upload
                return jsonify({'status': 'error', 'message': message_scan}), 400

            # Move to final location only if safe
            os.rename(temp_path, filepath)

            db.execute(
                'INSERT INTO help_ticket_attachments (ticket_id, reply_id, file_path, friendly_name) VALUES (%s, %s, %s, %s)',
                (ticket_id, reply_id, filepath, filename)
            )

    db.execute(
        "UPDATE help_tickets SET status = 'In Progress', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (ticket_id,)
    )

    if current_user and current_user['is_admin'] and user_id != ticket_creator_id:
        create_user_notification(
            db,
            ticket_creator_id,
            'New Reply on Your Ticket',
            f'A support agent has replied to your ticket: "{ticket_subject[:30]}..."',
            category='support'
        )

    db.commit()

    return jsonify({'status': 'success', 'message': 'Reply posted.'})

@app.route('/api/tickets/<int:ticket_id>', methods=['PATCH'])
@login_required
@csrf_protect
def update_ticket(ticket_id):
    db = get_db()
    user_id = session['user_id']
    user = db.execute('SELECT is_admin FROM users WHERE id = %s', (user_id,)).fetchone()

    if not user['is_admin']:
        return jsonify({'status': 'error', 'message': 'Access denied.'}), 403

    data = request.json
    status = data.get('status')
    priority = data.get('priority')
    assignee_id = data.get('assignee_id')

    if status:
        if status not in ['Open', 'In Progress', 'Closed', 'Awaiting User Response']:
            return jsonify({'status': 'error', 'message': 'Invalid status.'}), 400
        db.execute('UPDATE help_tickets SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s', (status, ticket_id))

    if priority:
        if priority not in ['Low', 'Medium', 'High']:
            return jsonify({'status': 'error', 'message': 'Invalid priority.'}), 400
        db.execute('UPDATE help_tickets SET priority = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s', (priority, ticket_id))

    if assignee_id:
        assignee = db.execute('SELECT is_admin FROM users WHERE id = %s', (assignee_id,)).fetchone()
        if not assignee or not assignee['is_admin']:
            return jsonify({'status': 'error', 'message': 'Assignee must be an admin.'}), 400
        db.execute('UPDATE help_tickets SET assignee_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s', (assignee_id, ticket_id))

    db.commit()
    return jsonify({'status': 'success', 'message': 'Ticket updated.'})
