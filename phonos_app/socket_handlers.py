"""WebSocket/Socket.IO event handlers for real-time collaboration."""
from __future__ import annotations

import json
from flask import session, request
from flask_socketio import emit, join_room, leave_room, rooms

from .app import socketio, logger
from .helpers.collaboration import (
    get_collaboration_session,
    add_participant,
    remove_participant,
    get_active_participants,
)


# Track connected users and their sessions
connected_users = {}


@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    if 'user_id' not in session:
        return False  # Reject connection if not authenticated

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')
    sid = request.sid

    connected_users[sid] = {
        'user_id': user_id,
        'username': username,
        'sessions': []
    }

    # Join user's personal room for direct notifications
    join_room(f'user_{user_id}')

    logger.info(f"User {username} (ID: {user_id}) connected with SID {sid}, joined room: user_{user_id}")
    emit('connection_established', {'user_id': user_id, 'username': username})

    # Broadcast user online status to all clients
    emit('user_status_changed', {
        'user_id': user_id,
        'username': username,
        'status': 'online'
    }, broadcast=True)


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    sid = request.sid

    if sid in connected_users:
        user_info = connected_users[sid]
        user_id = user_info['user_id']
        username = user_info['username']

        # Broadcast user offline status to all clients before removing
        emit('user_status_changed', {
            'user_id': user_id,
            'username': username,
            'status': 'offline'
        }, broadcast=True)

        # Leave all collaboration sessions
        for session_id in user_info['sessions']:
            leave_collab_session(session_id, broadcast=True)

        del connected_users[sid]
        logger.info(f"User {username} disconnected")


# ============================================================================
# COLLABORATION SESSION EVENTS
# ============================================================================

@socketio.on('join_collaboration')
def handle_join_collaboration(data):
    """Handle user joining a collaboration session."""
    session_id = data.get('session_id')

    if not session_id:
        emit('error', {'message': 'session_id is required'})
        return

    collab_session = get_collaboration_session(session_id)

    if not collab_session:
        emit('error', {'message': 'Session not found'})
        return

    if not collab_session['is_active']:
        emit('error', {'message': 'Session is not active'})
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')
    sid = request.sid

    # Add participant to database
    success = add_participant(session_id, user_id)

    if not success:
        # User is already a participant
        pass

    # Join Socket.IO room
    join_room(session_id)

    # Track session for this user
    if sid in connected_users:
        connected_users[sid]['sessions'].append(session_id)

    # Get all active participants
    participants = get_active_participants(session_id)

    # Notify all participants in the session
    emit('user_joined', {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'participants': participants
    }, room=session_id)

    logger.info(f"User {username} joined collaboration session {session_id}")


@socketio.on('leave_collaboration')
def handle_leave_collaboration(data):
    """Handle user leaving a collaboration session."""
    session_id = data.get('session_id')

    if not session_id:
        emit('error', {'message': 'session_id is required'})
        return

    leave_collab_session(session_id, broadcast=True)


def leave_collab_session(session_id: str, broadcast: bool = False):
    """Helper function to leave a collaboration session."""
    user_id = session.get('user_id')
    username = session.get('username', f'User {user_id}')
    sid = request.sid

    # Remove participant from database
    remove_participant(session_id, user_id)

    # Leave Socket.IO room
    leave_room(session_id)

    # Remove session from user's tracked sessions
    if sid in connected_users and session_id in connected_users[sid]['sessions']:
        connected_users[sid]['sessions'].remove(session_id)

    if broadcast:
        # Get remaining participants
        participants = get_active_participants(session_id)

        # Notify all participants in the session
        emit('user_left', {
            'session_id': session_id,
            'user_id': user_id,
            'username': username,
            'participants': participants
        }, room=session_id)

    logger.info(f"User {username} left collaboration session {session_id}")


# ============================================================================
# 3D VIEWER COLLABORATION EVENTS
# ============================================================================

@socketio.on('viewer_update')
def handle_viewer_update(data):
    """Handle 3D viewer state updates."""
    session_id = data.get('session_id')

    if not session_id:
        emit('error', {'message': 'session_id is required'})
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    update_data = {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'update_type': data.get('update_type'),
        'data': data.get('data', {})
    }

    # Broadcast to all participants except sender
    emit('viewer_state_changed', update_data, room=session_id, skip_sid=request.sid)


@socketio.on('camera_update')
def handle_camera_update(data):
    """Handle camera position/orientation updates."""
    session_id = data.get('session_id')

    if not session_id:
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    camera_data = {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'position': data.get('position'),
        'rotation': data.get('rotation'),
        'zoom': data.get('zoom')
    }

    # Broadcast to all participants except sender
    emit('camera_synced', camera_data, room=session_id, skip_sid=request.sid)


@socketio.on('selection_update')
def handle_selection_update(data):
    """Handle molecular selection updates."""
    session_id = data.get('session_id')

    if not session_id:
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    selection_data = {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'selected_atoms': data.get('selected_atoms', []),
        'selected_residues': data.get('selected_residues', [])
    }

    # Broadcast to all participants except sender
    emit('selection_synced', selection_data, room=session_id, skip_sid=request.sid)


@socketio.on('annotation_added')
def handle_annotation_added(data):
    """Handle annotation added event."""
    session_id = data.get('session_id')

    if not session_id:
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    annotation_data = {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'annotation': data.get('annotation')
    }

    # Broadcast to all participants
    emit('new_annotation', annotation_data, room=session_id)


# ============================================================================
# CURSOR/POINTER TRACKING
# ============================================================================

@socketio.on('cursor_move')
def handle_cursor_move(data):
    """Handle cursor movement updates for collaborative pointer."""
    session_id = data.get('session_id')

    if not session_id:
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    cursor_data = {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'x': data.get('x'),
        'y': data.get('y'),
        'viewport_width': data.get('viewport_width'),
        'viewport_height': data.get('viewport_height')
    }

    # Broadcast to all participants except sender
    emit('cursor_updated', cursor_data, room=session_id, skip_sid=request.sid)


# ============================================================================
# CHAT/MESSAGING
# ============================================================================

@socketio.on('send_message')
def handle_send_message(data):
    """Handle chat messages within collaboration session."""
    session_id = data.get('session_id')
    message = data.get('message', '').strip()

    if not session_id or not message:
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    message_data = {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'message': message,
        'timestamp': data.get('timestamp')
    }

    # Broadcast to all participants
    emit('new_message', message_data, room=session_id)


# ============================================================================
# PRESENCE/HEARTBEAT
# ============================================================================

@socketio.on('heartbeat')
def handle_heartbeat(data):
    """Handle heartbeat to maintain presence."""
    session_id = data.get('session_id')

    if not session_id:
        return

    user_id = session['user_id']

    # Update presence (could be stored in Redis for more sophisticated tracking)
    emit('heartbeat_ack', {'user_id': user_id, 'session_id': session_id})


# ============================================================================
# DRAWING BOARD COLLABORATION
# ============================================================================

@socketio.on('drawing_update')
def handle_drawing_update(data):
    """Handle drawing board updates."""
    session_id = data.get('session_id')

    if not session_id:
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    drawing_data = {
        'session_id': session_id,
        'user_id': user_id,
        'username': username,
        'action': data.get('action'),  # 'draw', 'erase', 'clear', 'undo'
        'data': data.get('data')
    }

    # Broadcast to all participants except sender
    emit('drawing_synced', drawing_data, room=session_id, skip_sid=request.sid)


# ============================================================================
# FILE EDITING COLLABORATION
# ============================================================================

@socketio.on('file_edit')
def handle_file_edit(data):
    """Handle collaborative file editing."""
    session_id = data.get('session_id')
    file_id = data.get('file_id')

    if not session_id or not file_id:
        return

    user_id = session['user_id']
    username = session.get('username', f'User {user_id}')

    edit_data = {
        'session_id': session_id,
        'file_id': file_id,
        'user_id': user_id,
        'username': username,
        'operation': data.get('operation'),  # 'insert', 'delete', 'replace'
        'position': data.get('position'),
        'content': data.get('content')
    }

    # Broadcast to all participants except sender
    emit('file_edited', edit_data, room=session_id, skip_sid=request.sid)


# ============================================================================
# JOB STATUS UPDATES
# ============================================================================

def broadcast_job_status_update(job_id: str, status: str, message: str = None):
    """
    Broadcast job status update to all interested clients.
    This can be called from job processing code.
    """
    update_data = {
        'job_id': job_id,
        'status': status,
        'message': message
    }

    # Broadcast to job-specific room
    socketio.emit('job_status_update', update_data, room=f'job_{job_id}')


@socketio.on('subscribe_job')
def handle_subscribe_job(data):
    """Subscribe to job status updates."""
    job_id = data.get('job_id')

    if not job_id:
        emit('error', {'message': 'job_id is required'})
        return

    join_room(f'job_{job_id}')
    emit('subscribed', {'job_id': job_id})


@socketio.on('unsubscribe_job')
def handle_unsubscribe_job(data):
    """Unsubscribe from job status updates."""
    job_id = data.get('job_id')

    if not job_id:
        return

    leave_room(f'job_{job_id}')
    emit('unsubscribed', {'job_id': job_id})


# ============================================================================
# NOTIFICATION EVENTS
# ============================================================================

def send_notification_to_user(user_id: int, notification: dict):
    """
    Send a real-time notification to a specific user.
    This can be called from anywhere in the application.
    """
    # Broadcast to user-specific room
    socketio.emit('notification', notification, room=f'user_{user_id}')


@socketio.on('subscribe_notifications')
def handle_subscribe_notifications():
    """Subscribe to personal notifications."""
    if 'user_id' not in session:
        return

    user_id = session['user_id']
    join_room(f'user_{user_id}')
    emit('notifications_subscribed', {'user_id': user_id})


# ============================================================================
# WEBRTC SIGNALING FOR CALLS
# ============================================================================

@socketio.on('join_call_room')
def handle_join_call_room(data):
    """Join a call room for WebRTC signaling"""
    call_id = data.get('call_id')

    if not call_id:
        emit('error', {'message': 'call_id is required'})
        return

    user_id = session.get('user_id')
    username = session.get('username', f'User {user_id}')

    join_room(f'call_{call_id}')

    # Notify others in the call
    emit('user_joined_call', {
        'user_id': user_id,
        'username': username
    }, room=f'call_{call_id}', include_self=False)

    emit('joined_call_room', {'call_id': call_id})


@socketio.on('leave_call_room')
def handle_leave_call_room(data):
    """Leave a call room"""
    call_id = data.get('call_id')

    if not call_id:
        return

    user_id = session.get('user_id')

    leave_room(f'call_{call_id}')

    # Notify others in the call
    emit('user_left_call', {
        'user_id': user_id
    }, room=f'call_{call_id}')


@socketio.on('webrtc_offer')
def handle_webrtc_offer(data):
    """Forward WebRTC offer to specific peer (legacy)"""
    target_user_id = data.get('target_user_id')
    offer = data.get('offer')
    call_id = data.get('call_id')

    if not all([target_user_id, offer, call_id]):
        emit('error', {'message': 'Missing required fields'})
        return

    sender_user_id = session.get('user_id')
    sender_username = session.get('username', f'User {sender_user_id}')

    # Send offer to target user
    emit('webrtc_offer', {
        'from_user_id': sender_user_id,
        'offer': offer,
        'call_id': call_id
    }, room=f'user_{target_user_id}')


@socketio.on('video_offer')
def handle_video_offer(data):
    """Forward WebRTC video offer to specific peer"""
    target_user_id = data.get('target_user_id')
    offer = data.get('offer')
    call_id = data.get('call_id')

    if not all([target_user_id, offer, call_id]):
        emit('error', {'message': 'Missing required fields'})
        return

    sender_user_id = session.get('user_id')
    sender_username = session.get('username', f'User {sender_user_id}')

    # Send offer to target user with correct event name
    emit('video_offer_received', {
        'from_user_id': sender_user_id,
        'from_username': sender_username,
        'offer': offer,
        'call_id': call_id
    }, room=f'user_{target_user_id}')


@socketio.on('webrtc_answer')
def handle_webrtc_answer(data):
    """Forward WebRTC answer to specific peer (legacy)"""
    target_user_id = data.get('target_user_id')
    answer = data.get('answer')
    call_id = data.get('call_id')

    if not all([target_user_id, answer, call_id]):
        emit('error', {'message': 'Missing required fields'})
        return

    sender_user_id = session.get('user_id')

    # Send answer to target user
    emit('webrtc_answer', {
        'from_user_id': sender_user_id,
        'answer': answer,
        'call_id': call_id
    }, room=f'user_{target_user_id}')


@socketio.on('video_answer')
def handle_video_answer(data):
    """Forward WebRTC video answer to specific peer"""
    target_user_id = data.get('target_user_id')
    answer = data.get('answer')
    call_id = data.get('call_id')

    if not all([target_user_id, answer, call_id]):
        emit('error', {'message': 'Missing required fields'})
        return

    sender_user_id = session.get('user_id')
    sender_username = session.get('username', f'User {sender_user_id}')

    # Send answer to target user with correct event name
    emit('video_answer_received', {
        'from_user_id': sender_user_id,
        'from_username': sender_username,
        'answer': answer,
        'call_id': call_id
    }, room=f'user_{target_user_id}')


@socketio.on('webrtc_ice_candidate')
def handle_ice_candidate(data):
    """Forward ICE candidate to specific peer (legacy)"""
    target_user_id = data.get('target_user_id')
    candidate = data.get('candidate')
    call_id = data.get('call_id')

    if not all([target_user_id, candidate, call_id]):
        return

    sender_user_id = session.get('user_id')

    # Send ICE candidate to target user
    emit('webrtc_ice_candidate', {
        'from_user_id': sender_user_id,
        'candidate': candidate,
        'call_id': call_id
    }, room=f'user_{target_user_id}')


@socketio.on('ice_candidate')
def handle_video_ice_candidate(data):
    """Forward ICE candidate for video calls"""
    target_user_id = data.get('target_user_id')
    candidate = data.get('candidate')
    call_id = data.get('call_id')

    if not all([target_user_id, candidate, call_id]):
        return

    sender_user_id = session.get('user_id')

    # Send ICE candidate to target user with correct event name
    emit('ice_candidate_received', {
        'from_user_id': sender_user_id,
        'candidate': candidate,
        'call_id': call_id
    }, room=f'user_{target_user_id}')


@socketio.on('call_invite')
def handle_call_invite(data):
    """Send call invitation to specific users"""
    call_id = data.get('call_id')
    invited_user_ids = data.get('user_ids', [])
    call_type = data.get('call_type', 'voice')

    if not call_id or not invited_user_ids:
        emit('error', {'message': 'Missing required fields'})
        return

    initiator_id = session.get('user_id')
    initiator_name = session.get('username', f'User {initiator_id}')

    # Send invitation to each user
    for user_id in invited_user_ids:
        socketio.emit('incoming_call', {
            'call_id': call_id,
            'initiator_id': initiator_id,
            'initiator_name': initiator_name,
            'call_type': call_type
        }, room=f'user_{user_id}')


@socketio.on('video_call_start')
def handle_video_call_start(data):
    """Handle starting a video call"""
    from ..helpers.calls import create_call_session, add_call_participant, get_call_participants
    from ..config import Config

    project_id = data.get('project_id')
    voice_only = data.get('voice_only', False)

    if not project_id:
        emit('error', {'message': 'project_id is required'})
        return

    user_id = session.get('user_id')
    username = session.get('username', f'User {user_id}')

    # Create call session
    call_type = 'voice' if voice_only else 'video'
    call_id = create_call_session(Config.DATABASE_FILE, call_type, user_id, project_id)

    if not call_id:
        emit('error', {'message': 'Failed to create call session'})
        return

    # Add initiator as participant
    add_call_participant(Config.DATABASE_FILE, call_id, user_id)

    # Join call room
    join_room(f'call_{call_id}')

    # Get all participants
    participants = get_call_participants(Config.DATABASE_FILE, call_id)

    # Send confirmation to initiator
    emit('video_call_joined', {
        'call_id': call_id,
        'project_id': project_id,
        'participants': participants
    })

    # Notify all participants in the call room (except sender)
    emit('video_call_user_joined', {
        'user_id': user_id,
        'username': username
    }, room=f'call_{call_id}', include_self=False)

    logger.info(f"User {username} started {call_type} call {call_id} for project {project_id}")


@socketio.on('video_call_toggle_audio')
def handle_video_call_toggle_audio(data):
    """Handle audio mute/unmute"""
    from ..helpers.calls import update_participant_state
    from ..config import Config

    call_id = data.get('call_id')
    is_muted = data.get('is_muted', False)

    if not call_id:
        return

    user_id = session.get('user_id')

    # Update participant state in database
    update_participant_state(Config.DATABASE_FILE, call_id, user_id, is_muted=is_muted)

    # Broadcast to all participants in the call
    emit('video_call_audio_toggled', {
        'user_id': user_id,
        'is_muted': is_muted
    }, room=f'call_{call_id}', include_self=False)


@socketio.on('video_call_toggle_video')
def handle_video_call_toggle_video(data):
    """Handle video on/off"""
    from ..helpers.calls import update_participant_state
    from ..config import Config

    call_id = data.get('call_id')
    is_video_off = data.get('is_video_off', False)

    if not call_id:
        return

    user_id = session.get('user_id')

    # Update participant state in database
    update_participant_state(Config.DATABASE_FILE, call_id, user_id, is_video_on=not is_video_off)

    # Broadcast to all participants in the call
    emit('video_call_video_toggled', {
        'user_id': user_id,
        'is_video_off': is_video_off
    }, room=f'call_{call_id}', include_self=False)


@socketio.on('video_call_leave')
def handle_video_call_leave(data):
    """Handle user leaving a video call"""
    from ..helpers.calls import remove_call_participant, get_call_participants, end_call_session
    from ..config import Config

    call_id = data.get('call_id')

    if not call_id:
        return

    user_id = session.get('user_id')
    username = session.get('username', f'User {user_id}')

    # Remove participant from call
    remove_call_participant(Config.DATABASE_FILE, call_id, user_id)

    # Leave call room
    leave_room(f'call_{call_id}')

    # Notify others
    emit('video_call_user_left', {
        'user_id': user_id,
        'username': username
    }, room=f'call_{call_id}')

    # Check if call is now empty and end it
    participants = get_call_participants(Config.DATABASE_FILE, call_id)
    if not participants or len(participants) == 0:
        end_call_session(Config.DATABASE_FILE, call_id)
        logger.info(f"Call {call_id} ended (no participants remaining)")

    logger.info(f"User {username} left call {call_id}")


# ============================================================================
# ERROR HANDLING
# ============================================================================

@socketio.on_error_default
def default_error_handler(e):
    """Handle Socket.IO errors."""
    logger.error(f"Socket.IO error: {str(e)}")
    emit('error', {'message': 'An error occurred', 'details': str(e)})


# ============================================================================
# IOT DEVICE BRIDGE - REAL-TIME EVENTS
# ============================================================================

# Track connected IoT devices
connected_iot_devices = {}


@socketio.on('iot_subscribe')
def handle_iot_subscribe():
    """Subscribe user to IoT updates."""
    if 'user_id' not in session:
        emit('error', {'message': 'Not authenticated'})
        return

    user_id = session['user_id']
    room_name = f'iot_user_{user_id}'
    join_room(room_name)

    logger.info(f"User {user_id} subscribed to IoT updates in room {room_name}")
    emit('iot_subscribed', {'user_id': user_id, 'room': room_name})


@socketio.on('iot_unsubscribe')
def handle_iot_unsubscribe():
    """Unsubscribe user from IoT updates."""
    if 'user_id' not in session:
        return

    user_id = session['user_id']
    room_name = f'iot_user_{user_id}'
    leave_room(room_name)

    logger.info(f"User {user_id} unsubscribed from IoT updates")
    emit('iot_unsubscribed', {'user_id': user_id})


@socketio.on('iot_device_connect')
def handle_iot_device_connect(data):
    """Handle IoT device connecting via WebSocket.

    Devices authenticate with their API key and join a device-specific room.
    """
    from .helpers import get_db

    api_key = data.get('api_key')
    device_id = data.get('device_id')

    if not api_key or not device_id:
        emit('iot_error', {'message': 'api_key and device_id required'})
        return

    if not api_key.startswith('iot_'):
        emit('iot_error', {'message': 'Invalid API key format'})
        return

    db = get_db()

    try:
        # Validate API key and get user
        user = db.execute(
            'SELECT id FROM users WHERE iot_api_key = %s',
            (api_key,)
        ).fetchone()

        if not user:
            emit('iot_error', {'message': 'Invalid API key'})
            return

        # Find device
        device = db.execute(
            'SELECT id FROM iot_devices WHERE user_id = %s AND device_id = %s',
            (user['id'], device_id)
        ).fetchone()

        if not device:
            emit('iot_error', {'message': 'Device not found'})
            return

        device_db_id = device['id']
        sid = request.sid

        # Join device room
        device_room = f'iot_device_{device_db_id}'
        join_room(device_room)

        # Track connected device
        connected_iot_devices[sid] = {
            'device_id': device_db_id,
            'device_name': device_id,
            'user_id': user['id']
        }

        # Update device status in database
        db.execute('''
            UPDATE iot_devices
            SET websocket_connected = true, status = 'online', last_seen = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (device_db_id,))
        db.commit()

        # Notify user that device is online
        emit('iot_device_connected', {
            'device_id': device_db_id,
            'device_name': device_id,
            'status': 'online'
        }, room=f'iot_user_{user["id"]}')

        # Send pending commands to device
        pending_commands = db.execute('''
            SELECT command_id, command_type, input_id, value
            FROM iot_command_queue
            WHERE device_id = %s AND status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 10
        ''', (device_db_id,)).fetchall()

        for cmd in pending_commands:
            value = cmd['value']
            if isinstance(value, str):
                import json as json_module
                value = json_module.loads(value)

            emit('iot_command', {
                'command_id': cmd['command_id'],
                'command_type': cmd['command_type'],
                'input_id': cmd['input_id'],
                'value': value
            })

            # Mark as sent
            db.execute(
                'UPDATE iot_command_queue SET status = %s, sent_at = CURRENT_TIMESTAMP WHERE command_id = %s',
                ('sent', cmd['command_id'])
            )
        db.commit()

        emit('iot_connected', {
            'device_id': device_db_id,
            'pending_commands': len(pending_commands)
        })

        logger.info(f"IoT device {device_id} (DB ID: {device_db_id}) connected via WebSocket")

    except Exception as e:
        logger.error(f"IoT device connect error: {e}")
        emit('iot_error', {'message': str(e)})


@socketio.on('iot_device_disconnect')
def handle_iot_device_disconnect():
    """Handle IoT device disconnecting."""
    from .helpers import get_db

    sid = request.sid

    if sid not in connected_iot_devices:
        return

    device_info = connected_iot_devices[sid]
    device_db_id = device_info['device_id']
    user_id = device_info['user_id']

    db = get_db()

    try:
        # Update device status
        db.execute('''
            UPDATE iot_devices
            SET websocket_connected = false, status = 'offline'
            WHERE id = %s
        ''', (device_db_id,))
        db.commit()

        # Notify user
        emit('iot_device_disconnected', {
            'device_id': device_db_id,
            'status': 'offline'
        }, room=f'iot_user_{user_id}')

        # Leave room and cleanup
        leave_room(f'iot_device_{device_db_id}')
        del connected_iot_devices[sid]

        logger.info(f"IoT device {device_info['device_name']} disconnected")

    except Exception as e:
        logger.error(f"IoT device disconnect error: {e}")


@socketio.on('iot_send_command')
def handle_iot_send_command(data):
    """Send command from web UI to device via WebSocket."""
    import uuid
    from .helpers import get_db

    if 'user_id' not in session:
        emit('error', {'message': 'Not authenticated'})
        return

    user_id = session['user_id']
    device_id = data.get('device_id')
    input_id = data.get('input_id')
    value = data.get('value')
    command_type = data.get('command_type', 'set_input')

    if not device_id or not input_id:
        emit('error', {'message': 'device_id and input_id required'})
        return

    db = get_db()

    try:
        # Verify device ownership
        device = db.execute(
            'SELECT * FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            emit('error', {'message': 'Device not found'})
            return

        command_id = str(uuid.uuid4())

        # Insert command into queue
        import json as json_module
        db.execute('''
            INSERT INTO iot_command_queue
            (device_id, user_id, command_id, command_type, input_id, value, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '5 minutes')
        ''', (device_id, user_id, command_id, command_type, input_id, json_module.dumps(value)))

        db.execute(
            'UPDATE iot_devices SET last_command_at = CURRENT_TIMESTAMP WHERE id = %s',
            (device_id,)
        )
        db.commit()

        # If device is connected via WebSocket, send immediately
        if device['websocket_connected']:
            emit('iot_command', {
                'command_id': command_id,
                'command_type': command_type,
                'input_id': input_id,
                'value': value
            }, room=f'iot_device_{device_id}')

            # Mark as sent
            db.execute(
                'UPDATE iot_command_queue SET status = %s, sent_at = CURRENT_TIMESTAMP WHERE command_id = %s',
                ('sent', command_id)
            )
            db.commit()

        emit('iot_command_sent', {
            'command_id': command_id,
            'status': 'sent' if device['websocket_connected'] else 'pending'
        })

        logger.info(f"Command {command_id} sent to device {device_id}: {input_id} = {value}")

    except Exception as e:
        logger.error(f"IoT send command error: {e}")
        emit('error', {'message': str(e)})


@socketio.on('iot_command_ack')
def handle_iot_command_ack(data):
    """Handle command acknowledgment from device."""
    from .helpers import get_db
    import json as json_module

    sid = request.sid

    if sid not in connected_iot_devices:
        emit('iot_error', {'message': 'Device not connected'})
        return

    device_info = connected_iot_devices[sid]
    command_id = data.get('command_id')
    success = data.get('success', True)
    response = data.get('response')
    error_message = data.get('error_message')

    if not command_id:
        return

    db = get_db()

    try:
        # Verify command belongs to this device
        cmd = db.execute(
            'SELECT * FROM iot_command_queue WHERE command_id = %s AND device_id = %s',
            (command_id, device_info['device_id'])
        ).fetchone()

        if not cmd:
            emit('iot_error', {'message': 'Command not found'})
            return

        # Update command status
        status = 'acknowledged' if success else 'failed'
        db.execute('''
            UPDATE iot_command_queue
            SET status = %s, acknowledged_at = CURRENT_TIMESTAMP,
                response = %s, error_message = %s
            WHERE command_id = %s
        ''', (status, json_module.dumps(response), error_message, command_id))

        # Update device state if set_input command succeeded
        if cmd['command_type'] == 'set_input' and cmd['input_id'] and success:
            db.execute('''
                UPDATE iot_devices
                SET current_state = COALESCE(current_state, '{}'::jsonb) || %s
                WHERE id = %s
            ''', (json_module.dumps({cmd['input_id']: cmd['value']}), device_info['device_id']))

        db.commit()

        # Notify user
        emit('iot_command_ack', {
            'command_id': command_id,
            'success': success,
            'response': response,
            'error_message': error_message
        }, room=f'iot_user_{device_info["user_id"]}')

        logger.info(f"Command {command_id} acknowledged: success={success}")

    except Exception as e:
        logger.error(f"IoT command ack error: {e}")


@socketio.on('iot_reading')
def handle_iot_reading(data):
    """Handle incoming reading from device (WebSocket mode)."""
    from .helpers import get_db
    import json as json_module
    import time

    sid = request.sid

    if sid not in connected_iot_devices:
        emit('iot_error', {'message': 'Device not connected'})
        return

    device_info = connected_iot_devices[sid]
    readings = data.get('readings', data.get('data', {}))

    if not readings:
        return

    db = get_db()

    try:
        device_db_id = device_info['device_id']
        user_id = device_info['user_id']

        # Update device state
        db.execute('''
            UPDATE iot_devices
            SET current_state = COALESCE(current_state, '{}'::jsonb) || %s,
                last_seen = CURRENT_TIMESTAMP,
                status = 'online'
            WHERE id = %s
        ''', (json_module.dumps(readings), device_db_id))

        # Store in messages table
        db.execute('''
            INSERT INTO iot_messages (user_id, device_id, data, metadata)
            VALUES (%s, %s, %s, %s)
        ''', (
            user_id,
            device_info['device_name'],
            json_module.dumps(readings),
            json_module.dumps({'source': 'websocket'})
        ))
        db.commit()

        # Broadcast to user
        emit('iot_reading', {
            'device_id': device_db_id,
            'device_name': device_info['device_name'],
            'readings': readings,
            'timestamp': time.time()
        }, room=f'iot_user_{user_id}')

    except Exception as e:
        logger.error(f"IoT reading error: {e}")
