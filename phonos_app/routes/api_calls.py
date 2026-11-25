"""API routes for video/voice calls, contacts, and call invitations."""
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


@app.route('/api/projects/<int:project_id>/calls', methods=['GET', 'POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def handle_project_calls(project_id):
    """Get active calls or start a new call for a project"""
    from ..helpers.calls import (create_call_session, get_active_calls_for_project,
                                 add_call_participant, force_leave_all_calls,
                                 cleanup_empty_calls, clear_stale_calls_for_user)
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)

    if request.method == 'GET':
        # Get active calls
        try:
            calls = get_active_calls_for_project(Config.DATABASE_FILE, project_id)
            return jsonify({
                'status': 'success',
                'calls': calls
            })
        except Exception as e:
            logger.error(f"Error fetching calls for project {project_id}: {e}")
            return jsonify({'status': 'error', 'message': 'Failed to fetch calls'}), 500

    elif request.method == 'POST':
        # Start a new call
        data = request.json or {}
        call_type = data.get('call_type', 'project')

        try:
            # STEP 1: Aggressive cleanup - clear stale calls first
            logger.info(f"User {user_id} starting call for project {project_id}")
            clear_stale_calls_for_user(Config.DATABASE_FILE, user_id)

            # STEP 2: Force leave ALL active calls to ensure clean state
            calls_left = force_leave_all_calls(Config.DATABASE_FILE, user_id)
            if calls_left > 0:
                logger.info(f"Force-left user {user_id} from {calls_left} active call(s)")

            # STEP 3: Cleanup any empty calls
            cleanup_empty_calls(Config.DATABASE_FILE)

            # STEP 4: Create new call session
            call_id = create_call_session(Config.DATABASE_FILE, call_type, user_id, project_id)
            logger.info(f"Created call session {call_id} for project {project_id}")

            # STEP 5: Add user as participant (idempotent)
            participant_id, was_already_in = add_call_participant(Config.DATABASE_FILE, call_id, user_id)
            logger.info(f"Added participant {participant_id} to call {call_id} (already_in={was_already_in})")

            # STEP 6: Notify project members
            socketio.emit('call_started', {
                'call_id': call_id,
                'project_id': project_id,
                'initiator_id': user_id,
                'call_type': call_type
            }, room=f'project_{project_id}')

            return jsonify({
                'status': 'success',
                'call_id': call_id
            }), 201

        except Exception as e:
            logger.error(f"Error starting call for user {user_id}, project {project_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to start call. Please try again.'}), 500


@app.route('/api/calls/<int:call_id>/join', methods=['POST'])
@login_required
@csrf_protect
def join_call(call_id):
    """
    Join an existing call (COMPLETELY REDESIGNED - IDEMPOTENT & ROBUST)

    This endpoint now:
    1. Aggressively cleans up stale participations
    2. Force-leaves other calls
    3. Allows re-joining the same call (idempotent)
    4. Uses transactions to prevent race conditions
    5. Provides detailed logging
    """
    from ..helpers.calls import (get_call_session, add_call_participant,
                                 force_leave_all_calls, clear_stale_calls_for_user,
                                 cleanup_empty_calls)
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)

    try:
        # STEP 1: Verify call exists and is active
        call = get_call_session(Config.DATABASE_FILE, call_id)
        if not call:
            logger.warning(f"User {user_id} tried to join non-existent call {call_id}")
            return jsonify({'status': 'error', 'message': 'Call not found.'}), 404

        if call['status'] != 'active':
            logger.warning(f"User {user_id} tried to join ended call {call_id}")
            return jsonify({'status': 'error', 'message': 'This call has already ended.'}), 400

        # STEP 2: Aggressive cleanup - clear all stale participations
        logger.info(f"User {user_id} joining call {call_id}")
        stale_cleared = clear_stale_calls_for_user(Config.DATABASE_FILE, user_id)
        if stale_cleared > 0:
            logger.info(f"Cleared {stale_cleared} stale participation(s) for user {user_id}")

        # STEP 3: Force leave ALL OTHER calls (not this one)
        calls_left = force_leave_all_calls(Config.DATABASE_FILE, user_id, exclude_call_id=call_id)
        if calls_left > 0:
            logger.info(f"Force-left user {user_id} from {calls_left} other active call(s)")

        # STEP 4: Cleanup any empty calls created by leaving
        empty_calls = cleanup_empty_calls(Config.DATABASE_FILE)
        if empty_calls > 0:
            logger.info(f"Cleaned up {empty_calls} empty call(s)")

        # STEP 5: Add participant (IDEMPOTENT - won't fail if already in call)
        participant_id, was_already_in = add_call_participant(Config.DATABASE_FILE, call_id, user_id)

        if was_already_in:
            logger.info(f"User {user_id} re-joined call {call_id} (already participant)")
        else:
            logger.info(f"User {user_id} newly joined call {call_id} as participant {participant_id}")

        # STEP 6: Notify other participants (only if newly joined)
        if not was_already_in:
            socketio.emit('participant_joined', {
                'call_id': call_id,
                'user_id': user_id
            }, room=f'call_{call_id}')

        return jsonify({
            'status': 'success',
            'call': call,
            'was_already_in': was_already_in,
            'participant_id': participant_id
        })

    except Exception as e:
        logger.error(f"Error joining call {call_id} for user {user_id}: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': 'Failed to join call. Please refresh and try again.'
        }), 500


@app.route('/api/calls/<int:call_id>/leave', methods=['POST'])
@login_required
@csrf_protect
def leave_call(call_id):
    """Leave a call (improved with better cleanup)"""
    from ..helpers.calls import remove_call_participant, get_call_participants, end_call_session
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)

    try:
        logger.info(f"User {user_id} leaving call {call_id}")

        # Remove participant from call
        remove_call_participant(Config.DATABASE_FILE, call_id, user_id)

        # Check if call is empty
        participants = get_call_participants(Config.DATABASE_FILE, call_id)

        if len(participants) == 0:
            # End call if no one left
            logger.info(f"Call {call_id} is now empty, ending call")
            end_call_session(Config.DATABASE_FILE, call_id)
            socketio.emit('call_ended', {'call_id': call_id}, room=f'call_{call_id}')
        else:
            # Notify other participants
            logger.info(f"User {user_id} left call {call_id}, {len(participants)} participant(s) remaining")
            socketio.emit('participant_left', {
                'call_id': call_id,
                'user_id': user_id
            }, room=f'call_{call_id}')

        return jsonify({'status': 'success'})

    except Exception as e:
        logger.error(f"Error leaving call {call_id} for user {user_id}: {e}", exc_info=True)
        # Even if there's an error, try to return success since user wants to leave
        return jsonify({'status': 'success', 'warning': 'Left call with errors'})


@app.route('/api/calls/<int:call_id>/participants', methods=['GET'])
@login_required
def get_call_participants_route(call_id):
    """Get all participants in a call"""
    from ..helpers.calls import get_call_participants

    participants = get_call_participants(Config.DATABASE_FILE, call_id)

    return jsonify({
        'status': 'success',
        'participants': participants
    })


@app.route('/api/calls/<int:call_id>/state', methods=['PUT'])
@login_required
@csrf_protect
def update_call_state(call_id):
    """Update participant's call state (mute/video)"""
    from ..helpers.calls import update_participant_state

    user_id = session['user_id']
    data = request.json or {}

    is_muted = data.get('is_muted')
    is_video_on = data.get('is_video_on')

    update_participant_state(Config.DATABASE_FILE, call_id, user_id, is_muted, is_video_on)

    # Notify other participants
    socketio.emit('participant_state_changed', {
        'call_id': call_id,
        'user_id': user_id,
        'is_muted': is_muted,
        'is_video_on': is_video_on
    }, room=f'call_{call_id}')

    return jsonify({'status': 'success'})


@app.route('/api/admin/calls/cleanup', methods=['POST'])
@login_required
@admin_required
@csrf_protect
def admin_cleanup_calls():
    """
    Admin endpoint to manually trigger call cleanup.
    Useful for debugging or immediate cleanup needs.
    """
    from ..helpers.calls import cleanup_all_stale_calls
    import logging

    logger = logging.getLogger(__name__)

    try:
        stats = cleanup_all_stale_calls()
        logger.info(f"Manual cleanup triggered by admin: {stats}")

        return jsonify({
            'status': 'success',
            'message': 'Cleanup completed',
            'stats': stats
        })
    except Exception as e:
        logger.error(f"Error in manual cleanup: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/calls/active', methods=['GET'])
@login_required
def get_all_active_calls():
    """Get all active calls across all projects for the current user"""
    from ..helpers.calls import get_user_active_calls
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)

    try:
        calls = get_user_active_calls(Config.DATABASE_FILE, user_id)
        return jsonify({
            'success': True,
            'calls': calls
        })
    except Exception as e:
        logger.error(f"Error fetching active calls for user {user_id}: {e}")
        return jsonify({
            'success': False,
            'message': 'Failed to fetch active calls'
        }), 500


@app.route('/api/calls/instant', methods=['POST'])
@login_required
@csrf_protect
def create_instant_meeting():
    """Create an instant meeting without a project"""
    from ..helpers.calls import create_call_session, add_call_participant, force_leave_all_calls, clear_stale_calls_for_user, cleanup_empty_calls
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)

    try:
        # Cleanup existing calls
        clear_stale_calls_for_user(Config.DATABASE_FILE, user_id)
        calls_left = force_leave_all_calls(Config.DATABASE_FILE, user_id)
        if calls_left > 0:
            logger.info(f"Force-left user {user_id} from {calls_left} active call(s) before instant meeting")

        cleanup_empty_calls(Config.DATABASE_FILE)

        # Create instant meeting (no project)
        call_id = create_call_session(Config.DATABASE_FILE, 'instant', user_id, None)
        logger.info(f"Created instant meeting {call_id}")

        # Add user as participant
        participant_id, was_already_in = add_call_participant(Config.DATABASE_FILE, call_id, user_id)
        logger.info(f"Added participant {participant_id} to instant meeting {call_id}")

        return jsonify({
            'success': True,
            'call': {
                'id': call_id,
                'call_type': 'instant'
            }
        }), 201

    except Exception as e:
        logger.error(f"Error creating instant meeting for user {user_id}: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Failed to create instant meeting'
        }), 500


# ============================================================================
# USER CONNECTIONS / CONTACTS
# ============================================================================

@app.route('/api/users/contacts', methods=['GET'])
@login_required
def get_user_contacts_route():
    """Get all contacts for the current user"""
    from ..helpers.user_connections import get_user_contacts

    user_id = session['user_id']
    contacts = get_user_contacts(Config.DATABASE_FILE, user_id)

    return jsonify({
        'success': True,
        'contacts': contacts
    })


@app.route('/api/users/all', methods=['GET'])
@login_required
def get_all_users_for_calling_route():
    """Get all users that can be called"""
    from ..helpers.user_connections import get_all_users_for_calling

    user_id = session['user_id']
    users = get_all_users_for_calling(Config.DATABASE_FILE, user_id)

    return jsonify({
        'success': True,
        'users': users
    })


@app.route('/api/users/contacts/<int:contact_user_id>', methods=['POST', 'DELETE'])
@login_required
@csrf_protect
def manage_user_contact(contact_user_id):
    """Add or remove a contact"""
    from ..helpers.user_connections import add_user_connection, remove_user_connection

    user_id = session['user_id']

    if request.method == 'POST':
        success = add_user_connection(Config.DATABASE_FILE, user_id, contact_user_id)
        return jsonify({
            'success': success,
            'message': 'Contact added' if success else 'Failed to add contact'
        })
    else:  # DELETE
        success = remove_user_connection(Config.DATABASE_FILE, user_id, contact_user_id)
        return jsonify({
            'success': success,
            'message': 'Contact removed' if success else 'Failed to remove contact'
        })


# ============================================================================
# DIRECT CALLS
# ============================================================================

@app.route('/api/calls/direct/<int:target_user_id>', methods=['POST'])
@login_required
@csrf_protect
def start_direct_call(target_user_id):
    """Start a direct call to another user"""
    from ..helpers.calls import (create_call_session, add_call_participant,
                                 force_leave_all_calls, clear_stale_calls_for_user,
                                 cleanup_empty_calls)
    from ..helpers.call_invitations import create_call_invitation
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)

    try:
        # Cleanup
        clear_stale_calls_for_user(Config.DATABASE_FILE, user_id)
        force_leave_all_calls(Config.DATABASE_FILE, user_id)
        cleanup_empty_calls(Config.DATABASE_FILE)

        # Create direct call session
        call_id = create_call_session(Config.DATABASE_FILE, 'direct', user_id, None)
        logger.info(f"Created direct call {call_id} from user {user_id} to user {target_user_id}")

        # Add initiator as participant
        add_call_participant(Config.DATABASE_FILE, call_id, user_id)

        # Create invitation for target user
        invitation_id = create_call_invitation(Config.DATABASE_FILE, call_id, user_id, target_user_id)

        # Send real-time notification to target user
        socketio.emit('incoming_call', {
            'call_id': call_id,
            'invitation_id': invitation_id,
            'caller_id': user_id,
            'caller_username': session.get('username', 'Someone'),
            'call_type': 'direct'
        }, room=f'user_{target_user_id}')

        return jsonify({
            'success': True,
            'call': {
                'id': call_id,
                'call_type': 'direct',
                'invitation_id': invitation_id
            }
        }), 201

    except Exception as e:
        logger.error(f"Error starting direct call: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Failed to start call'
        }), 500


@app.route('/api/calls/group', methods=['POST'])
@login_required
@csrf_protect
def start_group_call():
    """Start a group call with selected users"""
    from ..helpers.calls import (create_call_session, add_call_participant,
                                 force_leave_all_calls, clear_stale_calls_for_user,
                                 cleanup_empty_calls)
    from ..helpers.call_invitations import create_call_invitation
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)
    data = request.json or {}
    invited_user_ids = data.get('user_ids', [])

    if not invited_user_ids:
        return jsonify({
            'success': False,
            'message': 'No users selected'
        }), 400

    try:
        # Cleanup
        clear_stale_calls_for_user(Config.DATABASE_FILE, user_id)
        force_leave_all_calls(Config.DATABASE_FILE, user_id)
        cleanup_empty_calls(Config.DATABASE_FILE)

        # Create group call session
        call_id = create_call_session(Config.DATABASE_FILE, 'group', user_id, None)
        logger.info(f"Created group call {call_id} by user {user_id}")

        # Add initiator as participant
        add_call_participant(Config.DATABASE_FILE, call_id, user_id)

        # Create invitations for all invited users
        for invited_user_id in invited_user_ids:
            invitation_id = create_call_invitation(Config.DATABASE_FILE, call_id, user_id, invited_user_id)

            # Send real-time notification
            socketio.emit('incoming_call', {
                'call_id': call_id,
                'invitation_id': invitation_id,
                'caller_id': user_id,
                'caller_username': session.get('username', 'Someone'),
                'call_type': 'group',
                'participant_count': len(invited_user_ids) + 1
            }, room=f'user_{invited_user_id}')

        return jsonify({
            'success': True,
            'call': {
                'id': call_id,
                'call_type': 'group'
            }
        }), 201

    except Exception as e:
        logger.error(f"Error starting group call: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Failed to start group call'
        }), 500


# ============================================================================
# CALL INVITATIONS
# ============================================================================

@app.route('/api/calls/invitations', methods=['GET'])
@login_required
def get_call_invitations():
    """Get pending call invitations for current user"""
    from ..helpers.call_invitations import get_pending_invitations

    user_id = session['user_id']
    invitations = get_pending_invitations(Config.DATABASE_FILE, user_id)

    return jsonify({
        'success': True,
        'invitations': invitations
    })


@app.route('/api/calls/invitations/<int:invitation_id>/respond', methods=['POST'])
@login_required
@csrf_protect
def respond_to_call_invitation(invitation_id):
    """Accept or decline a call invitation"""
    from ..helpers.call_invitations import respond_to_invitation, get_invitation_by_id
    from ..helpers.calls import add_call_participant
    import logging

    user_id = session['user_id']
    logger = logging.getLogger(__name__)
    data = request.json or {}
    response = data.get('response')  # 'accepted' or 'declined'

    if response not in ['accepted', 'declined']:
        return jsonify({
            'success': False,
            'message': 'Invalid response'
        }), 400

    try:
        # Get invitation details
        invitation = get_invitation_by_id(Config.DATABASE_FILE, invitation_id)
        if not invitation or invitation['invitee_id'] != user_id:
            return jsonify({
                'success': False,
                'message': 'Invitation not found'
            }), 404

        # Update invitation status
        respond_to_invitation(Config.DATABASE_FILE, invitation_id, response)

        if response == 'accepted':
            # Add user to call
            add_call_participant(Config.DATABASE_FILE, invitation['call_id'], user_id)

            # Notify call participants
            socketio.emit('participant_joined', {
                'call_id': invitation['call_id'],
                'user_id': user_id,
                'username': session.get('username', 'User')
            }, room=f'call_{invitation["call_id"]}')

        # Notify caller
        socketio.emit('call_invitation_response', {
            'invitation_id': invitation_id,
            'user_id': user_id,
            'username': session.get('username', 'User'),
            'response': response
        }, room=f'user_{invitation["inviter_id"]}')

        return jsonify({
            'success': True,
            'call_id': invitation['call_id'] if response == 'accepted' else None
        })

    except Exception as e:
        logger.error(f"Error responding to invitation: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Failed to respond to invitation'
        }), 500
