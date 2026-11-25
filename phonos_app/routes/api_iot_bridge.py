"""
IoT Bridge API v2

Complete device management system with:
- Device registration with unique API keys
- Encrypted data transmission
- Command queue for device control
- Web client access tokens
- Project-scoping and user isolation for notebook integration

Endpoints:
  Device API (uses device API key):
    POST /api/iot/v2/device/register     - Register new device
    POST /api/iot/v2/device/data         - Submit device data
    GET  /api/iot/v2/device/commands     - Get pending commands
    POST /api/iot/v2/device/commands/ack - Acknowledge command
    POST /api/iot/v2/device/heartbeat    - Device heartbeat

  Web API (uses user auth or access token):
    GET  /api/iot/v2/devices             - List user's devices
    GET  /api/iot/v2/devices/<id>        - Get device details
    POST /api/iot/v2/devices             - Create device (web)
    PUT  /api/iot/v2/devices/<id>        - Update device
    DELETE /api/iot/v2/devices/<id>      - Delete device
    GET  /api/iot/v2/devices/<id>/data   - Get device data history
    POST /api/iot/v2/devices/<id>/command - Send command to device
    POST /api/iot/v2/devices/<id>/token  - Generate access token
"""

import json
import uuid
import time
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import jsonify, request, g, session
from ..app import app, socketio
from ..helpers.auth import login_required
from ..helpers.iot_crypto import (
    generate_device_api_key,
    hash_api_key,
    encrypt_data,
    decrypt_data,
    verify_request,
    generate_access_token
)
from production.database import get_db_connection

logger = logging.getLogger(__name__)

API_PREFIX = '/api/iot/v2'


def validate_project_scope(project_id_param='project_id'):
    """
    Decorator to validate that the user has access to the specified project.
    Ensures user-project isolation for all project-scoped operations.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # Get project_id from kwargs or request
            project_id = kwargs.get(project_id_param)
            if not project_id:
                data = request.get_json(silent=True) or {}
                project_id = data.get(project_id_param)

            if not project_id:
                return jsonify({"error": f"{project_id_param} is required"}), 400

            user_id = g.get('user_id')
            if not user_id:
                # Try to get from session
                user_id = session.get('user_id')

            if not user_id:
                return jsonify({"error": "Authentication required"}), 401

            conn = get_db_connection()
            cur = conn.cursor()
            try:
                # Check if user has access to this project
                cur.execute("""
                    SELECT role FROM project_assignments
                    WHERE user_id = %s AND project_id = %s
                """, (user_id, project_id))
                assignment = cur.fetchone()

                if not assignment:
                    # Also check team membership
                    cur.execute("""
                        SELECT tm.role FROM team_members tm
                        JOIN projects p ON (p.team_id = tm.team_id OR p.organization_id = tm.team_id)
                        WHERE tm.user_id = %s AND p.id = %s
                    """, (user_id, project_id))
                    team_role = cur.fetchone()
                    if not team_role:
                        return jsonify({"error": "Access denied to project"}), 403

                g.project_id = project_id
                g.project_role = assignment['role'] if assignment else team_role['role']

                return f(*args, **kwargs)

            finally:
                cur.close()
                conn.close()

        return decorated
    return decorator


# =============================================================================
# AUTHENTICATION DECORATORS
# =============================================================================

def device_auth_required(f):
    """Authenticate device using API key and signature."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-Device-Key') or request.args.get('api_key')

        if not api_key:
            return jsonify({"error": "Missing API key"}), 401

        # Find device by API key hash
        api_key_hash = hash_api_key(api_key)

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT id, user_id, device_id, name, encryption_enabled, schema, config
                FROM iot_devices
                WHERE api_key_hash = %s
            """, (api_key_hash,))
            device = cur.fetchone()

            if not device:
                return jsonify({"error": "Invalid API key"}), 401

            # Verify signature if present
            signature = request.headers.get('X-Signature')
            timestamp = request.headers.get('X-Timestamp')

            if signature and timestamp:
                try:
                    ts = int(timestamp)
                    payload = request.get_data(as_text=True) or ''
                    if not verify_request(api_key, ts, payload, signature):
                        return jsonify({"error": "Invalid signature"}), 401
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid timestamp"}), 401

            # Store device info in request context
            g.device = {
                "id": device['id'],
                "user_id": device['user_id'],
                "device_id": device['device_id'],
                "device_name": device['name'],
                "encryption_enabled": device['encryption_enabled'],
                "schema": device['schema'] or {},
                "config": device['config'] or {}
            }
            g.api_key = api_key

            return f(*args, **kwargs)

        finally:
            cur.close()
            conn.close()

    return decorated


def access_token_or_auth(f):
    """Authenticate using access token or user session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check for access token
        token = request.headers.get('X-Access-Token') or request.args.get('token')

        if token:
            token_hash = hash_api_key(token)
            conn = get_db_connection()
            cur = conn.cursor()
            try:
                cur.execute("""
                    SELECT t.id, t.device_id, t.user_id, t.can_read, t.can_write, t.can_admin,
                           d.device_id as device_id_str, d.name as device_name
                    FROM iot_access_tokens t
                    JOIN iot_devices d ON d.id = t.device_id
                    WHERE t.token_hash = %s
                    AND (t.expires_at IS NULL OR t.expires_at > NOW())
                """, (token_hash,))
                token_data = cur.fetchone()

                if not token_data:
                    return jsonify({"error": "Invalid or expired access token"}), 401

                # Update last used
                cur.execute("UPDATE iot_access_tokens SET last_used_at = NOW() WHERE id = %s",
                           (token_data['id'],))
                conn.commit()

                g.access_token = {
                    "id": token_data['id'],
                    "device_id": token_data['device_id'],
                    "user_id": token_data['user_id'],
                    "can_read": token_data['can_read'],
                    "can_write": token_data['can_write'],
                    "can_admin": token_data['can_admin'],
                    "device_id_str": token_data['device_id_str'],
                    "device_name": token_data['device_name']
                }
                g.user_id = token_data['user_id']

                return f(*args, **kwargs)
            finally:
                cur.close()
                conn.close()

        # Fall back to user authentication
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({"error": "Authentication required"}), 401

        g.user_id = user_id
        g.access_token = None

        return f(*args, **kwargs)

    return decorated


# =============================================================================
# DEVICE API ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/device/register', methods=['POST'])
def iot_v2_device_register():
    """Register a new device."""
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    user_api_key = data.get('user_api_key')
    device_id = data.get('device_id')
    device_name = data.get('device_name')

    if not all([user_api_key, device_id, device_name]):
        return jsonify({"error": "user_api_key, device_id, and device_name required"}), 400

    # Validate device_id format
    if not device_id.replace('-', '').replace('_', '').isalnum():
        return jsonify({"error": "device_id must be alphanumeric with hyphens/underscores"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Find user by API key (check api_keys table)
        # We assume the key provided is the raw key, so we need to hash it
        key_hash = hash_api_key(user_api_key)
        
        cur.execute("""
            SELECT user_id FROM api_keys 
            WHERE key_hash = %s AND is_active = 1 
            AND (expires_at IS NULL OR expires_at > NOW())
        """, (key_hash,))
        user_key_data = cur.fetchone()

        if not user_key_data:
            # Fallback: Check if it's the old-style key (if any migration happened)
            # or try to match against user_credential_id if that was the intent.
            # But strictly speaking, we should check api_keys.
            return jsonify({"error": "Invalid user API key"}), 401

        user_id = user_key_data['user_id']

        # Check if device_id already exists for this user
        cur.execute("""
            SELECT id FROM iot_devices WHERE user_id = %s AND device_id = %s
        """, (user_id, device_id))

        if cur.fetchone():
            return jsonify({"error": "Device ID already exists"}), 409

        # Generate device API key
        device_api_key = generate_device_api_key()
        api_key_hash = hash_api_key(device_api_key)

        # Insert device
        device_type = data.get('device_type', 'generic')
        schema = data.get('schema', {"inputs": [], "outputs": [], "config": {}})
        description = data.get('description', '')
        project_id = data.get('project_id')
        encryption_enabled = data.get('encryption_enabled', True)

        cur.execute("""
            INSERT INTO iot_devices (
                user_id, device_id, name, device_type, api_key, api_key_hash,
                encryption_enabled, schema, description, project_id, status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'offline', NOW())
            RETURNING id
        """, (
            user_id, device_id, device_name, device_type, device_api_key, api_key_hash,
            encryption_enabled, json.dumps(schema), description, project_id
        ))

        new_id = cur.fetchone()['id']
        conn.commit()

        return jsonify({
            "success": True,
            "device_api_key": device_api_key,
            "device_id": device_id,
            "internal_id": new_id,
            "message": "Device registered. Save your API key securely - it cannot be retrieved later."
        }), 201

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT device registration error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/device/data', methods=['POST'])
@device_auth_required
def iot_v2_device_submit_data():
    """Submit data from device."""
    device = g.device
    api_key = g.api_key

    body = request.get_json()
    if not body:
        return jsonify({"error": "Request body required"}), 400

    # Handle encrypted data
    if body.get('encrypted'):
        try:
            data = decrypt_data(body.get('data'), api_key)
        except ValueError as e:
            return jsonify({"error": f"Decryption failed: {str(e)}"}), 400
    else:
        data = body.get('data', {})

    if not isinstance(data, dict):
        return jsonify({"error": "Data must be an object"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Store data
        data_type = body.get('type', 'reading')
        source = body.get('source')
        device_timestamp = body.get('device_timestamp')

        if device_timestamp:
            try:
                device_timestamp = datetime.fromtimestamp(device_timestamp)
            except (ValueError, TypeError):
                device_timestamp = None

        cur.execute("""
            INSERT INTO iot_device_data (
                device_id, data, data_type, source, device_timestamp, received_at
            ) VALUES (%s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (device['id'], json.dumps(data), data_type, source, device_timestamp))

        data_id = cur.fetchone()['id']

        # Update device state and last_seen
        cur.execute("""
            UPDATE iot_devices
            SET current_state = current_state || %s,
                last_seen = NOW(),
                last_data_at = NOW(),
                status = 'online',
                updated_at = NOW()
            WHERE id = %s
        """, (json.dumps(data), device['id']))

        conn.commit()

        # Check for pending commands to return
        cur.execute("""
            SELECT id, command_id, command_type, target, payload
            FROM iot_commands
            WHERE device_id = %s AND status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 5
        """, (device['id'],))

        pending_commands = []
        cmd_ids = []
        for cmd in cur.fetchall():
            pending_commands.append({
                "command_id": cmd['command_id'],
                "type": cmd['command_type'],
                "target": cmd['target'],
                "payload": cmd['payload']
            })
            cmd_ids.append(cmd['id'])

        # Mark as delivered
        if cmd_ids:
            cur.execute("""
                UPDATE iot_commands SET status = 'delivered', delivered_at = NOW()
                WHERE id = ANY(%s)
            """, (cmd_ids,))
            conn.commit()

        response = {
            "success": True,
            "data_id": data_id,
            "timestamp": int(time.time())
        }

        if pending_commands:
            if device.get('encryption_enabled'):
                response["commands"] = {
                    "encrypted": True,
                    "data": encrypt_data({"commands": pending_commands}, api_key)
                }
            else:
                response["commands"] = pending_commands

        return jsonify(response)

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT data submit error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/device/commands', methods=['GET'])
@device_auth_required
def iot_v2_device_get_commands():
    """Get pending commands for device."""
    device = g.device
    api_key = g.api_key

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, command_id, command_type, target, payload
            FROM iot_commands
            WHERE device_id = %s AND status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 10
        """, (device['id'],))

        commands = []
        command_ids = []
        for cmd in cur.fetchall():
            commands.append({
                "command_id": cmd['command_id'],
                "type": cmd['command_type'],
                "target": cmd['target'],
                "payload": cmd['payload']
            })
            command_ids.append(cmd['id'])

        # Mark as delivered
        if command_ids:
            cur.execute("""
                UPDATE iot_commands SET status = 'delivered', delivered_at = NOW()
                WHERE id = ANY(%s)
            """, (command_ids,))
            conn.commit()

        # Update device heartbeat
        cur.execute("""
            UPDATE iot_devices SET last_seen = NOW(), status = 'online' WHERE id = %s
        """, (device['id'],))
        conn.commit()

        response = {"commands": commands, "count": len(commands)}

        if device.get('encryption_enabled') and commands:
            response = {
                "encrypted": True,
                "data": encrypt_data(response, api_key)
            }

        return jsonify(response)

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/device/commands/ack', methods=['POST'])
@device_auth_required
def iot_v2_device_ack_command():
    """Acknowledge command execution."""
    device = g.device
    body = request.get_json()

    if not body:
        return jsonify({"error": "Request body required"}), 400

    command_id = body.get('command_id')
    success = body.get('success', True)
    response_data = body.get('response')
    error_message = body.get('error')

    if not command_id:
        return jsonify({"error": "command_id required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        status = 'acknowledged' if success else 'failed'

        cur.execute("""
            UPDATE iot_commands
            SET status = %s,
                acknowledged_at = NOW(),
                response = %s,
                error_message = %s
            WHERE command_id = %s AND device_id = %s
            RETURNING id
        """, (status, json.dumps(response_data) if response_data else None,
              error_message, command_id, device['id']))

        if not cur.fetchone():
            return jsonify({"error": "Command not found"}), 404

        conn.commit()

        socketio.emit('iot_command_ack', {
            'device_id': device['id'],
            'command_id': command_id,
            'success': success,
            'response': response_data,
            'error': error_message
        }, room=f'iot_user_{device["user_id"]}')

        return jsonify({"success": True})

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/device/heartbeat', methods=['POST'])
@device_auth_required
def iot_v2_device_heartbeat():
    """Device heartbeat to maintain online status."""
    device = g.device

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE iot_devices
            SET last_seen = NOW(), status = 'online'
            WHERE id = %s
        """, (device['id'],))
        conn.commit()

        return jsonify({
            "success": True,
            "server_time": int(time.time())
        })

    finally:
        cur.close()
        conn.close()


# =============================================================================
# WEB API ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/devices', methods=['GET'])
@access_token_or_auth
def iot_v2_list_devices():
    """List all devices for the authenticated user."""
    user_id = g.user_id

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, device_id, name, device_type, status, last_seen,
                   current_state, schema, project_id, encryption_enabled, created_at, api_key
            FROM iot_devices
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (user_id,))

        devices = []
        for d in cur.fetchall():
            status = d['status']
            devices.append({
                "id": d['id'],
                "device_id": d['device_id'],
                "name": d['name'],  # Frontend expects 'name' not 'device_name'
                "device_name": d['name'],  # Keep for backward compatibility
                "type": d['device_type'],  # Frontend expects 'type' not 'device_type'
                "device_type": d['device_type'],  # Keep for backward compatibility
                "status": status,
                "is_online": status == 'online',  # Frontend expects is_online boolean
                "last_seen": d['last_seen'].isoformat() if d['last_seen'] else None,
                "last_data": d['current_state'] or {},  # Frontend expects 'last_data'
                "current_state": d['current_state'] or {},  # Keep for backward compatibility
                "schema": d['schema'] or {},
                "project_id": d['project_id'],
                "encryption_enabled": d['encryption_enabled'],
                "created_at": d['created_at'].isoformat() if d['created_at'] else None,
                "api_key": d['api_key']  # Device API key for authentication
            })

        return jsonify({"devices": devices, "count": len(devices)})

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/summary', methods=['GET'])
@access_token_or_auth
def iot_v2_devices_summary():
    """Return lightweight device summary (counts and types, no full device list)."""
    user_id = g.user_id

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Get counts by status
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'online') as online,
                COUNT(*) FILTER (WHERE status != 'online') as offline
            FROM iot_devices
            WHERE user_id = %s
        """, (user_id,))
        counts = cur.fetchone()

        # Get distinct device types
        cur.execute("""
            SELECT DISTINCT device_type
            FROM iot_devices
            WHERE user_id = %s AND device_type IS NOT NULL
            ORDER BY device_type
        """, (user_id,))
        types = [row['device_type'] for row in cur.fetchall()]

        # Get recent alerts (devices that went offline in last 24h)
        cur.execute("""
            SELECT device_id, name, last_seen
            FROM iot_devices
            WHERE user_id = %s AND status != 'online'
                AND last_seen > NOW() - INTERVAL '24 hours'
            ORDER BY last_seen DESC
            LIMIT 5
        """, (user_id,))
        recent_alerts = []
        for row in cur.fetchall():
            recent_alerts.append({
                'device_id': row['device_id'],
                'name': row['name'],
                'last_seen': row['last_seen'].isoformat() if row['last_seen'] else None
            })

        return jsonify({
            'total': counts['total'] if counts else 0,
            'online': counts['online'] if counts else 0,
            'offline': counts['offline'] if counts else 0,
            'types': types,
            'recent_alerts': recent_alerts
        })

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices', methods=['POST'])
@access_token_or_auth
def iot_v2_create_device():
    """Create a new device from web interface."""
    user_id = g.user_id
    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    device_id = data.get('device_id')
    device_name = data.get('device_name')

    if not all([device_id, device_name]):
        return jsonify({"error": "device_id and device_name required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Check if device_id already exists
        cur.execute("""
            SELECT id FROM iot_devices WHERE user_id = %s AND device_id = %s
        """, (user_id, device_id))

        if cur.fetchone():
            return jsonify({"error": "Device ID already exists"}), 409

        # Generate API key
        device_api_key = generate_device_api_key()
        api_key_hash = hash_api_key(device_api_key)

        device_type = data.get('device_type', 'generic')
        schema = data.get('schema', {"inputs": [], "outputs": [], "config": {}})
        description = data.get('description', '')
        project_id = data.get('project_id')
        encryption_enabled = data.get('encryption_enabled', True)

        cur.execute("""
            INSERT INTO iot_devices (
                user_id, device_id, name, device_type, api_key, api_key_hash,
                encryption_enabled, schema, description, project_id, status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'offline', NOW())
            RETURNING id
        """, (
            user_id, device_id, device_name, device_type, device_api_key, api_key_hash,
            encryption_enabled, json.dumps(schema), description, project_id
        ))

        new_id = cur.fetchone()['id']
        conn.commit()

        return jsonify({
            "success": True,
            "id": new_id,
            "device_id": device_id,
            "device_api_key": device_api_key,
            "message": "Save the API key securely - it cannot be retrieved later."
        }), 201

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT device creation error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>', methods=['GET'])
@access_token_or_auth
def iot_v2_get_device(device_id):
    """Get device details."""
    user_id = g.user_id

    # Check access token permissions
    if hasattr(g, 'access_token') and g.access_token and g.access_token['device_id'] != device_id:
        return jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, device_id, name, device_type, status, last_seen,
                   current_state, schema, project_id, encryption_enabled, description,
                   config, created_at, updated_at, last_data_at, api_key
            FROM iot_devices
            WHERE id = %s AND user_id = %s
        """, (device_id, user_id))

        d = cur.fetchone()
        if not d:
            return jsonify({"error": "Device not found"}), 404

        return jsonify({
            "id": d['id'],
            "device_id": d['device_id'],
            "device_name": d['name'],
            "device_type": d['device_type'],
            "status": d['status'],
            "last_seen": d['last_seen'].isoformat() if d['last_seen'] else None,
            "current_state": d['current_state'] or {},
            "schema": d['schema'] or {},
            "project_id": d['project_id'],
            "encryption_enabled": d['encryption_enabled'],
            "description": d['description'],
            "config": d['config'] or {},
            "created_at": d['created_at'].isoformat() if d['created_at'] else None,
            "updated_at": d['updated_at'].isoformat() if d['updated_at'] else None,
            "last_data_at": d['last_data_at'].isoformat() if d['last_data_at'] else None,
            "api_key": d['api_key']  # Device API key for authentication
        })

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>', methods=['PUT'])
@access_token_or_auth
def iot_v2_update_device(device_id):
    """Update device settings."""
    user_id = g.user_id

    # Check access token permissions
    if hasattr(g, 'access_token') and g.access_token:
        if g.access_token['device_id'] != device_id or not g.access_token['can_admin']:
            return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify ownership
        cur.execute("SELECT id FROM iot_devices WHERE id = %s AND user_id = %s",
                   (device_id, user_id))
        if not cur.fetchone():
            return jsonify({"error": "Device not found"}), 404

        # Build update query
        updates = []
        params = []

        if 'device_name' in data:
            updates.append("name = %s")
            params.append(data['device_name'])

        if 'device_type' in data:
            updates.append("device_type = %s")
            params.append(data['device_type'])

        if 'description' in data:
            updates.append("description = %s")
            params.append(data['description'])

        if 'schema' in data:
            updates.append("schema = %s")
            params.append(json.dumps(data['schema']))

        if 'config' in data:
            updates.append("config = %s")
            params.append(json.dumps(data['config']))

        if 'encryption_enabled' in data:
            updates.append("encryption_enabled = %s")
            params.append(data['encryption_enabled'])

        if 'project_id' in data:
            updates.append("project_id = %s")
            params.append(data['project_id'])

        if not updates:
            return jsonify({"error": "No fields to update"}), 400

        updates.append("updated_at = NOW()")
        params.append(device_id)

        cur.execute(f"""
            UPDATE iot_devices SET {', '.join(updates)} WHERE id = %s
        """, params)

        conn.commit()
        return jsonify({"success": True})

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>', methods=['DELETE'])
@access_token_or_auth
def iot_v2_delete_device(device_id):
    """Delete a device."""
    user_id = g.user_id

    if hasattr(g, 'access_token') and g.access_token:
        return jsonify({"error": "Cannot delete via access token"}), 403

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM iot_devices WHERE id = %s AND user_id = %s RETURNING id
        """, (device_id, user_id))

        if not cur.fetchone():
            return jsonify({"error": "Device not found"}), 404

        conn.commit()
        return jsonify({"success": True})

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/data', methods=['GET'])
@access_token_or_auth
def iot_v2_list_data():
    """List data from all devices (or filtered)."""
    user_id = g.user_id

    # Check access token
    if hasattr(g, 'access_token') and g.access_token:
        # Access tokens are scoped to a single device
        # Redirect to device-specific endpoint if they try to list all
        if not request.args.get('device_id'):
             return jsonify({"error": "Access tokens must specify device_id"}), 403
        if int(request.args.get('device_id')) != g.access_token['device_id']:
             return jsonify({"error": "Access denied"}), 403

    limit = min(int(request.args.get('limit', 100)), 1000)
    offset = int(request.args.get('offset', 0))
    device_id = request.args.get('device_id')
    data_type = request.args.get('type')
    search = request.args.get('search')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = """
            SELECT d.id, d.data, d.data_type, d.source, d.device_timestamp, d.received_at,
                   dev.device_id, dev.name
            FROM iot_device_data d
            JOIN iot_devices dev ON d.device_id = dev.id
            WHERE dev.user_id = %s
        """
        params = [user_id]

        if device_id:
            query += " AND dev.device_id = %s"
            params.append(device_id)

        if data_type:
            query += " AND d.data_type = %s"
            params.append(data_type)
        
        if search:
             query += " AND (d.data::text ILIKE %s)"
             params.append(f"%{search}%")

        query += " ORDER BY d.received_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cur.execute(query, params)

        messages = []
        for row in cur.fetchall():
            messages.append({
                "id": row['id'],
                "data": row['data'],
                "type": row['data_type'],
                "source": row['source'],
                "device_timestamp": row['device_timestamp'].isoformat() if row['device_timestamp'] else None,
                "created_at": row['received_at'].isoformat() if row['received_at'] else None, # key used by frontend
                "device_id": row['device_id'],
                "device_name": row['name']
            })

        return jsonify({"messages": messages, "count": len(messages)})

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>/data', methods=['GET'])
@access_token_or_auth
def iot_v2_get_device_data(device_id):
    """Get device data history."""
    user_id = g.user_id

    if hasattr(g, 'access_token') and g.access_token:
        if g.access_token['device_id'] != device_id or not g.access_token['can_read']:
            return jsonify({"error": "Access denied"}), 403

    # Query parameters
    limit = min(int(request.args.get('limit', 100)), 1000)
    offset = int(request.args.get('offset', 0))
    data_type = request.args.get('type')
    since = request.args.get('since')  # ISO timestamp

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify ownership
        cur.execute("SELECT id FROM iot_devices WHERE id = %s AND user_id = %s",
                   (device_id, user_id))
        if not cur.fetchone():
            return jsonify({"error": "Device not found"}), 404

        query = """
            SELECT id, data, data_type, source, device_timestamp, received_at
            FROM iot_device_data
            WHERE device_id = %s
        """
        params = [device_id]

        if data_type:
            query += " AND data_type = %s"
            params.append(data_type)

        if since:
            query += " AND received_at >= %s"
            params.append(since)

        query += " ORDER BY received_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cur.execute(query, params)

        data_points = []
        for row in cur.fetchall():
            data_points.append({
                "id": row['id'],
                "data": row['data'],
                "type": row['data_type'],
                "source": row['source'],
                "device_timestamp": row['device_timestamp'].isoformat() if row['device_timestamp'] else None,
                "received_at": row['received_at'].isoformat() if row['received_at'] else None
            })

        return jsonify({"data": data_points, "count": len(data_points)})

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>/command', methods=['POST'])
@access_token_or_auth
def iot_v2_send_device_command(device_id):
    """Send a command to a device."""
    user_id = g.user_id

    if hasattr(g, 'access_token') and g.access_token:
        if g.access_token['device_id'] != device_id or not g.access_token['can_write']:
            return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    command_type = data.get('type', 'set_value')
    target = data.get('target')
    payload = data.get('payload', {})
    priority = data.get('priority', 0)
    ttl_seconds = data.get('ttl', 300)  # 5 minutes default

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify ownership
        cur.execute("""
            SELECT id, encryption_enabled, api_key FROM iot_devices
            WHERE id = %s AND user_id = %s
        """, (device_id, user_id))
        device = cur.fetchone()

        if not device:
            return jsonify({"error": "Device not found"}), 404

        command_id = str(uuid.uuid4())
        expires_at = datetime.now() + timedelta(seconds=ttl_seconds)

        # Encrypt payload if device has encryption enabled
        encrypted = False
        if device['encryption_enabled'] and device['api_key']:  # encryption_enabled and api_key
            encrypted_payload = encrypt_data(payload, device['api_key'])
            payload_to_store = {"encrypted": True, "data": encrypted_payload}
            encrypted = True
        else:
            payload_to_store = payload

        cur.execute("""
            INSERT INTO iot_commands (
                device_id, user_id, command_id, command_type, target, payload,
                encrypted, priority, expires_at, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (device_id, user_id, command_id, command_type, target,
              json.dumps(payload_to_store), encrypted, priority, expires_at))

        new_id = cur.fetchone()['id']
        conn.commit()

        socketio.emit('iot_command_sent', {
            'device_id': device_id,
            'command_id': command_id,
            'command_type': command_type,
            'target': target,
            'payload': payload,
            'priority': priority
        }, room=f'iot_device_{device_id}')

        return jsonify({
            "success": True,
            "command_id": command_id,
            "id": new_id,
            "expires_at": expires_at.isoformat()
        }), 201

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT command error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>/token', methods=['POST'])
@access_token_or_auth
def iot_v2_create_access_token(device_id):
    """Create an access token for a device."""
    user_id = g.user_id

    if hasattr(g, 'access_token') and g.access_token:
        return jsonify({"error": "Cannot create token via access token"}), 403

    data = request.get_json() or {}

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify ownership
        cur.execute("SELECT id FROM iot_devices WHERE id = %s AND user_id = %s",
                   (device_id, user_id))
        if not cur.fetchone():
            return jsonify({"error": "Device not found"}), 404

        # Generate token
        token = generate_access_token()
        token_hash = hash_api_key(token)

        # Parse options
        name = data.get('name', 'Access Token')
        can_read = data.get('can_read', True)
        can_write = data.get('can_write', False)
        can_admin = data.get('can_admin', False)
        expires_days = data.get('expires_days')

        expires_at = None
        if expires_days:
            expires_at = datetime.now() + timedelta(days=expires_days)

        cur.execute("""
            INSERT INTO iot_access_tokens (
                device_id, user_id, token, token_hash, name,
                can_read, can_write, can_admin, expires_at, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (device_id, user_id, token, token_hash, name,
              can_read, can_write, can_admin, expires_at))

        new_id = cur.fetchone()['id']
        conn.commit()

        return jsonify({
            "success": True,
            "id": new_id,
            "token": token,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "permissions": {
                "read": can_read,
                "write": can_write,
                "admin": can_admin
            },
            "message": "Save the token securely - it cannot be retrieved later."
        }), 201

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT token creation error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>/regenerate-key', methods=['POST'])
@access_token_or_auth
def iot_v2_regenerate_device_key(device_id):
    """Regenerate the device API key."""
    user_id = g.user_id

    if hasattr(g, 'access_token') and g.access_token:
        return jsonify({"error": "Cannot regenerate key via access token"}), 403

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify ownership
        cur.execute("SELECT id FROM iot_devices WHERE id = %s AND user_id = %s",
                   (device_id, user_id))
        if not cur.fetchone():
            return jsonify({"error": "Device not found"}), 404

        # Generate new API key
        new_api_key = generate_device_api_key()
        new_hash = hash_api_key(new_api_key)

        cur.execute("""
            UPDATE iot_devices
            SET api_key = %s, api_key_hash = %s, updated_at = NOW()
            WHERE id = %s
        """, (new_api_key, new_hash, device_id))

        conn.commit()

        return jsonify({
            "success": True,
            "device_api_key": new_api_key,
            "message": "API key regenerated. Update your device with the new key."
        })

    finally:
        cur.close()
        conn.close()


# =============================================================================
# DASHBOARD API ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/devices/<int:device_id>/dashboard', methods=['GET'])
@access_token_or_auth
def iot_v2_get_device_dashboard(device_id):
    """Get device dashboard layout."""
    user_id = g.user_id

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Get device and verify access
        cur.execute("""
            SELECT d.id, d.user_id, d.device_id, d.name, d.schema,
                   d.current_state, d.project_id, d.dashboard_enabled
            FROM iot_devices d
            WHERE d.id = %s
        """, (device_id,))
        device = cur.fetchone()

        if not device:
            return jsonify({"error": "Device not found"}), 404

        # Check access: owner or project member
        has_access = device['user_id'] == user_id
        if not has_access and device['project_id']:
            cur.execute("""
                SELECT role FROM project_assignments
                WHERE user_id = %s AND project_id = %s
            """, (user_id, device['project_id']))
            assignment = cur.fetchone()
            has_access = assignment is not None

        if not has_access:
            return jsonify({"error": "Access denied"}), 403

        # Get dashboard layout
        cur.execute("""
            SELECT id, name, layout, is_default, created_at, updated_at
            FROM iot_dashboards
            WHERE device_id = %s
            ORDER BY is_default DESC, updated_at DESC
            LIMIT 1
        """, (device_id,))
        dashboard = cur.fetchone()

        # Return default layout if no dashboard exists
        default_layout = {
            "widgets": [],
            "grid": {"columns": 4, "rowHeight": 100, "gap": 8}
        }

        return jsonify({
            "success": True,
            "dashboard": {
                "id": dashboard['id'] if dashboard else None,
                "name": dashboard['name'] if dashboard else "Default Dashboard",
                "layout": dashboard['layout'] if dashboard else default_layout,
                "is_default": dashboard['is_default'] if dashboard else True
            },
            "device": {
                "id": device['id'],
                "device_id": device['device_id'],
                "device_name": device['name'],
                "schema": device['schema'] or {"inputs": [], "outputs": []},
                "current_state": device['current_state'] or {},
                "dashboard_enabled": device['dashboard_enabled']
            }
        })

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>/dashboard', methods=['PUT'])
@access_token_or_auth
def iot_v2_save_device_dashboard(device_id):
    """Save device dashboard layout."""
    user_id = g.user_id

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    layout = data.get('layout')
    name = data.get('name', 'Default Dashboard')

    if not layout:
        return jsonify({"error": "layout required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify ownership or project contributor access
        cur.execute("""
            SELECT d.id, d.user_id, d.project_id
            FROM iot_devices d
            WHERE d.id = %s
        """, (device_id,))
        device = cur.fetchone()

        if not device:
            return jsonify({"error": "Device not found"}), 404

        can_edit = device['user_id'] == user_id
        if not can_edit and device['project_id']:
            cur.execute("""
                SELECT role FROM project_assignments
                WHERE user_id = %s AND project_id = %s
            """, (user_id, device['project_id']))
            assignment = cur.fetchone()
            if assignment and assignment['role'] in ('Project Manager', 'Project Contributor', 'owner', 'collaborator'):
                can_edit = True

        if not can_edit:
            return jsonify({"error": "Access denied"}), 403

        # Upsert dashboard
        cur.execute("""
            INSERT INTO iot_dashboards (device_id, name, layout, is_default)
            VALUES (%s, %s, %s, true)
            ON CONFLICT (device_id, name)
            DO UPDATE SET layout = EXCLUDED.layout, updated_at = NOW()
            RETURNING id
        """, (device_id, name, json.dumps(layout)))

        dashboard_id = cur.fetchone()['id']
        conn.commit()

        return jsonify({
            "success": True,
            "dashboard_id": dashboard_id,
            "message": "Dashboard saved successfully"
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT dashboard save error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>/dashboard/data', methods=['GET'])
@access_token_or_auth
def iot_v2_get_dashboard_data(device_id):
    """Get current device state for dashboard (polling endpoint)."""
    user_id = g.user_id

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Get device state
        cur.execute("""
            SELECT d.id, d.user_id, d.current_state, d.status, d.last_seen, d.project_id
            FROM iot_devices d
            WHERE d.id = %s
        """, (device_id,))
        device = cur.fetchone()

        if not device:
            return jsonify({"error": "Device not found"}), 404

        # Check access
        has_access = device['user_id'] == user_id
        if not has_access and device['project_id']:
            cur.execute("""
                SELECT 1 FROM project_assignments
                WHERE user_id = %s AND project_id = %s
            """, (user_id, device['project_id']))
            has_access = cur.fetchone() is not None

        if not has_access:
            return jsonify({"error": "Access denied"}), 403

        return jsonify({
            "success": True,
            "state": device['current_state'] or {},
            "status": device['status'],
            "last_seen": device['last_seen'].isoformat() if device['last_seen'] else None
        })

    finally:
        cur.close()
        conn.close()


# =============================================================================
# PROJECT-SCOPED DEVICE ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/projects/<int:project_id>/devices', methods=['GET'])
@access_token_or_auth
def iot_v2_list_project_devices(project_id):
    """List all devices in a project."""
    user_id = g.user_id

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Check project access
        cur.execute("""
            SELECT role FROM project_assignments
            WHERE user_id = %s AND project_id = %s
        """, (user_id, project_id))
        assignment = cur.fetchone()

        if not assignment:
            # Check team membership
            cur.execute("""
                SELECT tm.role FROM team_members tm
                JOIN projects p ON (p.team_id = tm.team_id OR p.organization_id = tm.team_id)
                WHERE tm.user_id = %s AND p.id = %s
            """, (user_id, project_id))
            team_role = cur.fetchone()
            if not team_role:
                return jsonify({"error": "Project not found or access denied"}), 404

        # Get devices
        cur.execute("""
            SELECT id, device_id, name, device_type, description,
                   status, last_seen, current_state, schema, tags,
                   dashboard_enabled, created_at, updated_at
            FROM iot_devices
            WHERE project_id = %s
            ORDER BY name ASC
        """, (project_id,))
        devices = cur.fetchall()

        return jsonify({
            "success": True,
            "project_id": project_id,
            "devices": [{
                "id": d['id'],
                "device_id": d['device_id'],
                "device_name": d['name'],
                "name": d['name'],
                "device_type": d['device_type'],
                "type": d['device_type'],
                "description": d['description'],
                "status": d['status'],
                "is_online": d['status'] == 'online',
                "last_seen": d['last_seen'].isoformat() if d['last_seen'] else None,
                "current_state": d['current_state'] or {},
                "schema": d['schema'] or {"inputs": [], "outputs": []},
                "tags": d['tags'] or [],
                "dashboard_enabled": d['dashboard_enabled'],
                "created_at": d['created_at'].isoformat() if d['created_at'] else None
            } for d in devices],
            "count": len(devices)
        })

    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/projects/<int:project_id>/devices', methods=['POST'])
@access_token_or_auth
def iot_v2_create_project_device(project_id):
    """Create a device in a project."""
    user_id = g.user_id

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    device_id = data.get('device_id')
    device_name = data.get('device_name')
    device_type = data.get('device_type', 'generic')

    if not device_id or not device_name:
        return jsonify({"error": "device_id and device_name required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Check project edit access (Manager or Contributor)
        cur.execute("""
            SELECT role FROM project_assignments
            WHERE user_id = %s AND project_id = %s
        """, (user_id, project_id))
        assignment = cur.fetchone()

        if not assignment or assignment['role'] not in ('Project Manager', 'Project Contributor', 'owner', 'collaborator'):
            return jsonify({"error": "Project not found or insufficient permissions"}), 403

        # Check for duplicate device_id in this project
        cur.execute("""
            SELECT id FROM iot_devices
            WHERE project_id = %s AND device_id = %s
        """, (project_id, device_id))
        if cur.fetchone():
            return jsonify({"error": "Device ID already exists in this project"}), 400

        # Generate API key
        api_key = generate_device_api_key()
        api_key_hash = hash_api_key(api_key)

        # Create device
        schema = data.get('schema', {"inputs": [], "outputs": []})
        description = data.get('description', '')
        tags = data.get('tags', [])

        cur.execute("""
            INSERT INTO iot_devices (
                user_id, project_id, device_id, device_name, device_type,
                description, api_key, api_key_hash, schema, tags, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'offline')
            RETURNING id
        """, (user_id, project_id, device_id, device_name, device_type,
              description, api_key, api_key_hash, json.dumps(schema), tags))

        new_id = cur.fetchone()['id']
        conn.commit()

        return jsonify({
            "success": True,
            "id": new_id,
            "device_id": device_id,
            "device_api_key": api_key,
            "message": "Device created. Save the API key - it cannot be retrieved later."
        }), 201

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT project device creation error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route(f'{API_PREFIX}/devices/<int:device_id>/project', methods=['PUT'])
@access_token_or_auth
def iot_v2_move_device_to_project(device_id):
    """Move a device to a different project."""
    user_id = g.user_id

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    new_project_id = data.get('project_id')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify device ownership
        cur.execute("""
            SELECT id, project_id FROM iot_devices
            WHERE id = %s AND user_id = %s
        """, (device_id, user_id))
        device = cur.fetchone()

        if not device:
            return jsonify({"error": "Device not found"}), 404

        # Check new project access if specified
        if new_project_id:
            cur.execute("""
                SELECT role FROM project_assignments
                WHERE user_id = %s AND project_id = %s
            """, (user_id, new_project_id))
            assignment = cur.fetchone()
            if not assignment or assignment['role'] not in ('Project Manager', 'Project Contributor', 'owner', 'collaborator'):
                return jsonify({"error": "Target project not found or insufficient permissions"}), 403

        # Update device project
        cur.execute("""
            UPDATE iot_devices SET project_id = %s, updated_at = NOW()
            WHERE id = %s
        """, (new_project_id, device_id))
        conn.commit()

        return jsonify({
            "success": True,
            "message": "Device moved successfully"
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT device move error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


# =============================================================================
# NOTEBOOK SDK INTERNAL ENDPOINTS
# =============================================================================

def notebook_session_auth(f):
    """Authenticate requests from notebook kernels and validate project scope."""
    @wraps(f)
    def decorated(*args, **kwargs):
        session_token = request.headers.get('X-Notebook-Session')
        project_id = request.headers.get('X-Project-ID')

        if not session_token or not project_id:
            return jsonify({"error": "Notebook authentication required"}), 401

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Validate session token and get notebook's project
            cur.execute("""
                SELECT ns.id, ns.notebook_id, ns.user_id, n.project_id
                FROM notebook_sessions ns
                JOIN notebooks n ON n.id = ns.notebook_id
                WHERE ns.session_token = %s
                AND n.project_id = %s
                AND ns.kernel_status NOT IN ('dead', 'error')
            """, (session_token, project_id))
            session = cur.fetchone()

            if not session:
                return jsonify({"error": "Invalid or expired notebook session"}), 401

            # Additional security: verify the requested project_id matches
            # This ensures notebooks can only access their own project's resources
            requested_project_id = request.headers.get('X-Project-ID')
            if str(session['project_id']) != str(requested_project_id):
                return jsonify({"error": "Project scope violation: notebook cannot access resources outside its project"}), 403

            g.notebook_session = {
                "id": session['id'],
                "notebook_id": session['notebook_id'],
                "user_id": session['user_id'],
                "project_id": session['project_id']
            }

            return f(*args, **kwargs)

        finally:
            cur.close()
            conn.close()

    return decorated


@app.route('/api/iot/internal/devices', methods=['GET'])
@notebook_session_auth
def iot_internal_list_devices():
    """List devices in the notebook's project (for SDK use)."""
    project_id = g.notebook_session['project_id']

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, device_id, name, device_type, description,
                   status, last_seen, current_state, schema
            FROM iot_devices
            WHERE project_id = %s
            ORDER BY name ASC
        """, (project_id,))
        devices = cur.fetchall()

        return jsonify({
            "success": True,
            "devices": [{
                "id": d['id'],
                "device_id": d['device_id'],
                "device_name": d['name'],
                "device_type": d['device_type'],
                "description": d['description'],
                "status": d['status'],
                "last_seen": d['last_seen'].isoformat() if d['last_seen'] else None,
                "current_state": d['current_state'] or {},
                "schema": d['schema'] or {"inputs": [], "outputs": []}
            } for d in devices]
        })

    finally:
        cur.close()
        conn.close()


@app.route('/api/iot/internal/devices/<int:device_db_id>/read', methods=['GET'])
@notebook_session_auth
def iot_internal_read_device(device_db_id):
    """Read device state (for SDK use)."""
    project_id = g.notebook_session['project_id']

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT current_state, status, last_seen
            FROM iot_devices
            WHERE id = %s AND project_id = %s
        """, (device_db_id, project_id))
        device = cur.fetchone()

        if not device:
            return jsonify({"error": "Device not found in project"}), 404

        return jsonify({
            "success": True,
            "state": device['current_state'] or {},
            "status": device['status'],
            "last_seen": device['last_seen'].isoformat() if device['last_seen'] else None
        })

    finally:
        cur.close()
        conn.close()


@app.route('/api/iot/internal/devices/<int:device_db_id>/write', methods=['POST'])
@notebook_session_auth
def iot_internal_write_device(device_db_id):
    """Send command to device (for SDK use)."""
    project_id = g.notebook_session['project_id']
    user_id = g.notebook_session['user_id']

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    input_id = data.get('input_id')
    value = data.get('value')

    if input_id is None:
        return jsonify({"error": "input_id required"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify device in project
        cur.execute("""
            SELECT id, encryption_enabled, api_key
            FROM iot_devices
            WHERE id = %s AND project_id = %s
        """, (device_db_id, project_id))
        device = cur.fetchone()

        if not device:
            return jsonify({"error": "Device not found in project"}), 404

        # Create command
        command_id = str(uuid.uuid4())
        payload = {"value": value}
        expires_at = datetime.now() + timedelta(seconds=300)

        cur.execute("""
            INSERT INTO iot_commands (
                device_id, user_id, command_id, command_type, target, payload,
                encrypted, priority, expires_at, created_at
            ) VALUES (%s, %s, %s, 'set_value', %s, %s, false, 0, %s, NOW())
            RETURNING id
        """, (device_db_id, user_id, command_id, input_id, json.dumps(payload), expires_at))

        conn.commit()

        return jsonify({
            "success": True,
            "command_id": command_id,
            "message": "Command queued for device"
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"IoT internal write error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route('/api/iot/internal/devices/<int:device_db_id>/history', methods=['GET'])
@notebook_session_auth
def iot_internal_device_history(device_db_id):
    """Get device data history (for SDK use)."""
    project_id = g.notebook_session['project_id']

    limit = request.args.get('limit', 100, type=int)
    output_id = request.args.get('output_id')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify device in project
        cur.execute("""
            SELECT id FROM iot_devices WHERE id = %s AND project_id = %s
        """, (device_db_id, project_id))
        if not cur.fetchone():
            return jsonify({"error": "Device not found in project"}), 404

        # Get history
        cur.execute("""
            SELECT data, data_type, received_at, device_timestamp
            FROM iot_device_data
            WHERE device_id = %s
            ORDER BY received_at DESC
            LIMIT %s
        """, (device_db_id, min(limit, 10000)))
        history = cur.fetchall()

        # Filter by output_id if specified
        results = []
        for h in history:
            record = {
                "timestamp": h['received_at'].isoformat() if h['received_at'] else None,
                "device_timestamp": h['device_timestamp'].isoformat() if h['device_timestamp'] else None,
                "data_type": h['data_type']
            }
            data = h['data'] or {}
            if output_id:
                if output_id in data:
                    record[output_id] = data[output_id]
                    results.append(record)
            else:
                record.update(data)
                results.append(record)

        return jsonify({
            "success": True,
            "data": results,
            "count": len(results)
        })

    finally:
        cur.close()
        conn.close()
