"""API routes for project messages."""
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


@app.route('/api/projects/<int:project_id>/messages', methods=['GET', 'POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
@csrf_protect
def handle_project_messages(project_id):
    """Get or send messages in a project chat."""
    from ..helpers.messages import create_project_message, get_project_messages, get_message_count

    db = get_db()
    user_id = session['user_id']

    if request.method == 'GET':
        # Get messages for this project
        limit = request.args.get('limit', 50, type=int)
        before_id = request.args.get('before_id', None, type=int)

        messages = get_project_messages(project_id, limit=limit, before_id=before_id)
        total_count = get_message_count(project_id)

        return jsonify({
            'status': 'success',
            'messages': messages,
            'total_count': total_count
        })

    elif request.method == 'POST':
        # Send a new message
        data = request.json or {}
        content = data.get('content', '').strip()
        message_type = data.get('message_type', 'text')
        file_id = data.get('file_id')

        if not content:
            return jsonify({'status': 'error', 'message': 'Message content is required.'}), 400

        # Create the user message
        user_message = create_project_message(
            project_id=project_id,
            sender_id=user_id,
            content=content,
            message_type=message_type,
            file_id=file_id
        )

        # Emit real-time update via SocketIO
        socketio.emit('new_project_message', {
            'project_id': project_id,
            'message': user_message
        }, room=f'project_{project_id}')

        return jsonify({
            'status': 'success',
            'message': user_message
        }), 201


@app.route('/api/projects/<int:project_id>/messages/<int:message_id>', methods=['DELETE'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def delete_project_message(project_id, message_id):
    """Delete a project message (only sender can delete)."""
    from ..helpers.messages import delete_project_message as delete_msg

    user_id = session['user_id']

    success = delete_msg(message_id, user_id)

    if not success:
        return jsonify({
            'status': 'error',
            'message': 'Message not found or you do not have permission to delete it.'
        }), 404

    # Emit real-time update
    socketio.emit('project_message_deleted', {
        'project_id': project_id,
        'message_id': message_id
    }, room=f'project_{project_id}')

    return jsonify({'status': 'success'})


@app.route('/api/projects/<int:project_id>/messages/upload', methods=['POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def upload_message_attachment(project_id):
    """Upload file attachment for messages"""
    from ..helpers.attachments import allowed_file, MAX_FILE_SIZE, is_image, generate_thumbnail
    from ..helpers.analytics import track_event
    import os
    import hashlib

    db = get_db()
    user_id = session['user_id']

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file provided.'}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No file selected.'}), 400

    if not allowed_file(file.filename):
        return jsonify({'status': 'error', 'message': 'File type not allowed.'}), 400

    # Check file size
    file.seek(0, os.SEEK_END)
    file_size_check = file.tell()
    file.seek(0)

    if file_size_check > MAX_FILE_SIZE:
        return jsonify({'status': 'error', 'message': f'File too large. Max size is {MAX_FILE_SIZE / 1024 / 1024}MB.'}), 400

    # Check storage quota before uploading
    storage_state = get_user_storage_state(db, user_id)
    if file_size_check > storage_state['available']:
        return jsonify({
            'status': 'error',
            'message': f'Insufficient storage. Need {format_storage_bytes(file_size_check - storage_state["available"])} more space.'
        }), 507

    # Prepare file saving with security scanning
    filename = secure_filename(file.filename)
    file_type = filename.rsplit('.', 1)[1].lower() if '.' in filename else 'unknown'

    # Create unique filename with timestamp and hash
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    unique_name = f"{timestamp}_{hashlib.md5(filename.encode()).hexdigest()[:8]}_{filename}"

    # Create directory structure
    upload_folder = os.path.join(os.path.dirname(__file__), '..', '..', 'uploads')
    folder_path = os.path.join(upload_folder, 'attachments', str(project_id))
    os.makedirs(folder_path, exist_ok=True)

    # Save to temporary location first
    file_path = os.path.join(folder_path, unique_name)
    temp_path = file_path + '.tmp'
    file.save(temp_path)

    # Scan the file
    is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
    if not is_safe:
        # temp_path is automatically deleted by scan_upload
        return jsonify({'status': 'error', 'message': message}), 400

    # Move to final location only if safe
    os.rename(temp_path, file_path)

    # Get file size
    file_size = os.path.getsize(file_path)

    # Generate thumbnail for images
    thumbnail_path = None
    thumbnail_size = 0
    if is_image(filename):
        thumbnail_name = f"thumb_{unique_name.rsplit('.', 1)[0]}.jpg"
        thumbnail_path = os.path.join(folder_path, thumbnail_name)
        if generate_thumbnail(file_path, thumbnail_path):
            thumbnail_size = os.path.getsize(thumbnail_path)
        else:
            thumbnail_path = None

    # Total size includes thumbnail
    total_size = file_size + thumbnail_size

    # Update user's storage usage
    try:
        db.execute('UPDATE users SET storage_used = storage_used + %s WHERE id = %s', (total_size, user_id))
        db.commit()

        # Track storage event
        track_event(
            user_id=user_id,
            event_type='attachment_uploaded',
            storage_delta=total_size,
            project_id=project_id,
            metadata={
                'file_name': filename,
                'file_size': file_size,
                'thumbnail_size': thumbnail_size
            }
        )

        # Notify user of storage change
        socketio.emit('user_info_update', room=f"user_{user_id}")

    except Exception as e:
        logger.warning(f"Failed to update storage for attachment upload: {e}")

    # Return file info (will be attached to message when sent)
    return jsonify({
        'status': 'success',
        'file': {
            'name': file.filename,
            'path': file_path,
            'type': file_type,
            'size': file_size,
            'thumbnail': thumbnail_path,
            'uploader_id': user_id
        }
    }), 201


@app.route('/api/projects/<int:project_id>/messages/<int:message_id>/attachments', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
def get_message_attachments(project_id, message_id):
    """Get attachments for a specific message"""
    from ..helpers.attachments import get_message_attachments as get_attachments

    attachments = get_attachments(Config.DATABASE_FILE, message_id)

    return jsonify({
        'status': 'success',
        'attachments': attachments
    })


@app.route('/api/projects/<int:project_id>/messages/<int:message_id>/reactions', methods=['POST', 'DELETE'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
@csrf_protect
def handle_message_reaction(project_id, message_id):
    """Add or remove a reaction to/from a message."""
    from ..helpers.messages import add_message_reaction, remove_message_reaction

    user_id = session['user_id']
    data = request.get_json()

    if not data or 'reaction' not in data:
        return jsonify({
            'status': 'error',
            'message': 'Reaction emoji is required.'
        }), 400

    reaction = data['reaction']

    if request.method == 'POST':
        result = add_message_reaction(message_id, user_id, reaction)

        # Emit real-time update
        socketio.emit('message_reaction_added', {
            'project_id': project_id,
            'message_id': message_id,
            'reaction': reaction,
            'user_id': user_id,
            'reactions': result['reactions']
        }, room=f'project_{project_id}')

        return jsonify({
            'status': 'success',
            'data': result
        }), 201
    else:  # DELETE
        result = remove_message_reaction(message_id, user_id, reaction)

        # Emit real-time update
        socketio.emit('message_reaction_removed', {
            'project_id': project_id,
            'message_id': message_id,
            'reaction': reaction,
            'user_id': user_id,
            'reactions': result['reactions']
        }, room=f'project_{project_id}')

        return jsonify({
            'status': 'success',
            'data': result
        })


@app.route('/api/projects/<int:project_id>/messages/<int:message_id>/edit', methods=['PATCH'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def edit_message(project_id, message_id):
    """Edit a message (only sender can edit)."""
    from ..helpers.messages import edit_project_message

    user_id = session['user_id']
    data = request.get_json()

    if not data or 'content' not in data:
        return jsonify({
            'status': 'error',
            'message': 'Message content is required.'
        }), 400

    new_content = data['content'].strip()

    if not new_content:
        return jsonify({
            'status': 'error',
            'message': 'Message content cannot be empty.'
        }), 400

    updated_message = edit_project_message(message_id, user_id, new_content)

    if not updated_message:
        return jsonify({
            'status': 'error',
            'message': 'Message not found or you do not have permission to edit it.'
        }), 404

    # Emit real-time update
    socketio.emit('message_edited', {
        'project_id': project_id,
        'message': updated_message
    }, room=f'project_{project_id}')

    return jsonify({
        'status': 'success',
        'message': updated_message
    })


@app.route('/api/projects/<int:project_id>/messages/<int:message_id>/pin', methods=['POST', 'DELETE'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def handle_pin_message(project_id, message_id):
    """Pin or unpin a message."""
    from ..helpers.messages import pin_project_message, unpin_project_message

    if request.method == 'POST':
        success = pin_project_message(message_id, project_id)

        if not success:
            return jsonify({
                'status': 'error',
                'message': 'Message not found or does not belong to this project.'
            }), 404

        # Emit real-time update
        socketio.emit('message_pinned', {
            'project_id': project_id,
            'message_id': message_id
        }, room=f'project_{project_id}')

        return jsonify({'status': 'success', 'pinned': True})
    else:  # DELETE
        success = unpin_project_message(message_id, project_id)

        if not success:
            return jsonify({
                'status': 'error',
                'message': 'Pinned message not found.'
            }), 404

        # Emit real-time update
        socketio.emit('message_unpinned', {
            'project_id': project_id,
            'message_id': message_id
        }, room=f'project_{project_id}')

        return jsonify({'status': 'success', 'pinned': False})


@app.route('/api/projects/<int:project_id>/messages/pinned', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
def get_pinned_project_messages(project_id):
    """Get all pinned messages for a project."""
    from ..helpers.messages import get_pinned_messages

    pinned_messages = get_pinned_messages(project_id)

    return jsonify({
        'status': 'success',
        'messages': pinned_messages
    })


@app.route('/api/projects/<int:project_id>/messages/search', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
def search_project_messages(project_id):
    """Search messages in a project."""
    from ..helpers.database import get_db

    query = request.args.get('q', '').strip()

    if not query:
        return jsonify({
            'status': 'error',
            'message': 'Search query is required.'
        }), 400

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT
            pm.id, pm.project_id, pm.sender_id, pm.content,
            pm.message_type, pm.file_id, pm.gemini_response,
            pm.parent_message_id, pm.created_at,
            u.username as sender_username,
            u.profile_picture_url as sender_picture,
            f.name as file_name
        FROM project_messages pm
        JOIN users u ON pm.sender_id = u.id
        LEFT JOIN files f ON pm.file_id = f.id
        WHERE pm.project_id = %s AND pm.content ILIKE %s
        ORDER BY pm.created_at DESC
        LIMIT 50
        """,
        (project_id, f'%{query}%'),
    )

    messages = []
    for row in result.fetchall():
        message = dict(row)
        message_id = message['id']

        # Get attachments
        att_result = db.execute(
            """
            SELECT id, file_name, file_path, file_type, file_size, thumbnail_path, uploaded_at
            FROM message_attachments
            WHERE message_id = %s
            """,
            (message_id,),
        )
        message['attachments'] = [dict(att) for att in att_result.fetchall()]

        # Get reactions
        react_result = db.execute(
            """
            SELECT mr.reaction, COUNT(*) as count,
                   STRING_AGG(u.username, ',') as users
            FROM message_reactions mr
            JOIN users u ON mr.user_id = u.id
            WHERE mr.message_id = %s
            GROUP BY mr.reaction
            """,
            (message_id,),
        )
        message['reactions'] = [dict(r) for r in react_result.fetchall()]

        messages.append(message)

    return jsonify({
        'status': 'success',
        'messages': messages,
        'count': len(messages)
    })
