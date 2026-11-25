"""Routes for Notebook IDE and IoT Device Bridge tools."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from flask import jsonify, request, session, render_template

from ..app import app, logger
from ..helpers import get_db, login_required


# ============================================================================
# NOTEBOOK IDE PAGE
# ============================================================================




# NOTE: Notebook API endpoints have been moved to api_notebooks.py
# for enhanced project integration features. See that file for:
# - CRUD operations: GET/POST/PUT/DELETE /api/notebooks
# - Kernel management: start/stop/status/interrupt
# - Import/export: /api/notebooks/import, /api/notebooks/<id>/export
# - Project integration: /api/notebooks/project/<project_id>
# - Templates: /api/notebooks/templates



# ============================================================================
# IOT DEVICE BRIDGE PAGE
# ============================================================================

@app.route('/iot')
@login_required
def iot_page():
    """Render the IoT Device Bridge page."""
    db = get_db()
    user_id = session.get('user_id')

    api_key = 'iot_' + secrets.token_hex(24)  # Default temporary key

    try:
        # Get or create IoT API key for user
        user = db.execute(
            'SELECT iot_api_key FROM users WHERE id = %s', (user_id,)
        ).fetchone()

        if user and user.get('iot_api_key'):
            api_key = user.get('iot_api_key')
        else:
            # Generate new API key and save it
            try:
                db.execute(
                    'UPDATE users SET iot_api_key = %s WHERE id = %s',
                    (api_key, user_id)
                )
                db.commit()
            except Exception as e:
                logger.warning(f"Could not save IoT API key: {e}")
    except Exception as e:
        logger.warning(f"Could not fetch IoT API key: {e}")
        # Continue with temporary key

    from flask import make_response
    response = make_response(render_template('iot.html', api_key=api_key))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/iot/builder/<int:device_id>')
@login_required
def iot_builder_page(device_id):
    """Render the IoT Dashboard Builder page."""
    from production.database import get_db_connection
    user_id = session.get('user_id')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Get device and verify access
        cur.execute("""
            SELECT d.id, d.device_id, d.name as device_name, d.user_id, d.project_id, d.device_type
            FROM iot_devices d
            WHERE d.id = %s
        """, (device_id,))
        device = cur.fetchone()

        if not device:
            return render_template('404.html'), 404

        # Check access: owner or project member
        has_access = device['user_id'] == user_id
        if not has_access and device['project_id']:
            cur.execute("""
                SELECT 1 FROM project_assignments
                WHERE user_id = %s AND project_id = %s
            """, (user_id, device['project_id']))
            has_access = cur.fetchone() is not None

        if not has_access:
            return render_template('404.html'), 404

        return render_template('iot-builder.html', device=device)

    finally:
        cur.close()
        conn.close()


# ============================================================================
# IOT API ENDPOINTS (Web UI)
# ============================================================================

@app.route('/api/iot/devices', methods=['GET'])
@login_required
def iot_list_devices():
    """List all IoT devices for the current user."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        devices = db.execute(
            '''SELECT id, device_id, name, device_type, project_id, endpoint, status, last_seen, config
               FROM iot_devices WHERE user_id = %s ORDER BY created_at DESC''',
            (user_id,)
        ).fetchall()

        return jsonify({
            'status': 'success',
            'devices': [dict(d) for d in devices] if devices else []
        })
    except Exception as e:
        logger.error(f"IoT list devices error: {e}")
        return jsonify({
            'status': 'success',
            'devices': [],
            'note': 'IoT table may not exist yet'
        })


@app.route('/api/iot/devices', methods=['POST'])
@login_required
def iot_add_device():
    """Add a new IoT device."""
    db = get_db()
    user_id = session.get('user_id')
    data = request.get_json()

    if not data:
        return jsonify({'error': True, 'message': 'No data provided'}), 400

    device_id = data.get('device_id', '').strip()
    name = data.get('name', '').strip()
    device_type = data.get('type', 'generic').strip()
    project_id = data.get('project_id')
    endpoint = data.get('endpoint', '').strip()

    if not device_id or not name:
        return jsonify({
            'error': True,
            'message': 'Device ID and name are required'
        }), 400

    try:
        # Check if table exists, create if not
        db.execute('''
            CREATE TABLE IF NOT EXISTS iot_devices (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                device_id VARCHAR(128) NOT NULL,
                name VARCHAR(255) NOT NULL,
                device_type VARCHAR(50) NOT NULL DEFAULT 'generic',
                project_id INTEGER,
                endpoint TEXT,
                api_key TEXT,
                poll_interval INTEGER DEFAULT 30,
                status VARCHAR(20) DEFAULT 'offline',
                last_seen TIMESTAMP,
                config JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(user_id, device_id)
            )
        ''')

        # Check for existing device
        existing = db.execute(
            'SELECT id FROM iot_devices WHERE user_id = %s AND device_id = %s',
            (user_id, device_id)
        ).fetchone()

        if existing:
            return jsonify({
                'error': True,
                'message': 'Device with this ID already exists'
            }), 400

        db.execute(
            '''INSERT INTO iot_devices (user_id, device_id, name, device_type, project_id, endpoint)
               VALUES (%s, %s, %s, %s, %s, %s)''',
            (user_id, device_id, name, device_type, project_id, endpoint)
        )
        db.commit()

        return jsonify({
            'status': 'success',
            'message': 'Device added successfully'
        })
    except Exception as e:
        logger.error(f"IoT add device error: {e}")
        return jsonify({
            'error': True,
            'message': 'Failed to add device'
        }), 500


@app.route('/api/iot/devices/<int:device_id>', methods=['DELETE'])
@login_required
def iot_delete_device(device_id):
    """Delete an IoT device."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        db.execute(
            'DELETE FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        )
        db.commit()

        return jsonify({
            'status': 'success',
            'message': 'Device deleted'
        })
    except Exception as e:
        logger.error(f"IoT delete device error: {e}")
        return jsonify({
            'error': True,
            'message': 'Failed to delete device'
        }), 500


@app.route('/api/iot/stats', methods=['GET'])
@login_required
def iot_get_stats():
    """Get IoT statistics for the current user."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        # Get device counts
        devices = db.execute(
            'SELECT COUNT(*) as total, COUNT(CASE WHEN status = %s THEN 1 END) as online FROM iot_devices WHERE user_id = %s',
            ('online', user_id)
        ).fetchone()

        # Get unique projects
        projects = db.execute(
            'SELECT COUNT(DISTINCT project_id) as count FROM iot_devices WHERE user_id = %s AND project_id IS NOT NULL',
            (user_id,)
        ).fetchone()

        # Get message count (from today)
        messages = db.execute(
            '''SELECT COUNT(*) as count FROM iot_messages
               WHERE user_id = %s AND created_at >= CURRENT_DATE''',
            (user_id,)
        ).fetchone()

        return jsonify({
            'status': 'success',
            'stats': {
                'devices': devices['total'] if devices else 0,
                'online': devices['online'] if devices else 0,
                'messages': messages['count'] if messages else 0,
                'projects': projects['count'] if projects else 0
            }
        })
    except Exception as e:
        logger.debug(f"IoT stats error (tables may not exist): {e}")
        return jsonify({
            'status': 'success',
            'stats': {
                'devices': 0,
                'online': 0,
                'messages': 0,
                'projects': 0
            }
        })


@app.route('/api/iot/messages', methods=['GET'])
@login_required
def iot_list_messages():
    """List IoT messages for the current user, optionally filtered by device.

    Now reads from unified iot_device_data table (v2).
    """
    db = get_db()
    user_id = session.get('user_id')

    device_id = request.args.get('device_id')
    search = request.args.get('search')
    limit = min(int(request.args.get('limit', 100)), 500)
    offset = int(request.args.get('offset', 0))

    try:
        # Build query to read from iot_device_data (unified table)
        query = '''
            SELECT d.id, d.data, d.data_type, d.source, d.received_at,
                   dev.device_id, dev.name as device_name, dev.project_id
            FROM iot_device_data d
            JOIN iot_devices dev ON d.device_id = dev.id
            WHERE dev.user_id = %s
        '''
        params = [user_id]

        if device_id:
            # Filter by device internal id or device_id string
            try:
                device_internal_id = int(device_id)
                query += ' AND dev.id = %s'
                params.append(device_internal_id)
            except ValueError:
                query += ' AND dev.device_id = %s'
                params.append(device_id)

        if search:
            query += ' AND d.data::text ILIKE %s'
            params.append(f'%{search}%')

        query += ' ORDER BY d.received_at DESC LIMIT %s OFFSET %s'
        params.extend([limit, offset])

        messages = db.execute(query, params).fetchall()

        result = []
        for msg in messages:
            msg_dict = {
                'id': msg['id'],
                'device_id': msg['device_id'],
                'device_name': msg['device_name'],
                'project_id': msg['project_id'],
                'data': msg['data'],
                'type': msg['data_type'],
                'source': msg['source'],
                'timestamp': msg['received_at'].timestamp() if msg['received_at'] else None,
                'created_at': msg['received_at'].isoformat() if msg['received_at'] else None,
                # Backward compatibility
                'metadata': {'type': msg['data_type'], 'source': msg['source']}
            }
            # Parse JSON if needed
            if isinstance(msg_dict.get('data'), str):
                try:
                    msg_dict['data'] = json.loads(msg_dict['data'])
                except Exception:
                    pass
            result.append(msg_dict)

        return jsonify({
            'status': 'success',
            'messages': result,
            'count': len(result)
        })
    except Exception as e:
        logger.debug(f"IoT messages error: {e}")
        return jsonify({
            'status': 'success',
            'messages': [],
            'count': 0
        })


@app.route('/api/iot/messages/export', methods=['GET'])
@login_required
def iot_export_messages():
    """Export IoT messages as CSV or JSON.

    Now reads from unified iot_device_data table (v2).
    """
    db = get_db()
    user_id = session.get('user_id')

    format_type = request.args.get('format', 'json').lower()
    device_id = request.args.get('device_id')
    limit = min(int(request.args.get('limit', 1000)), 10000)

    try:
        # Build query to read from iot_device_data (unified table)
        query = '''
            SELECT d.data, d.data_type, d.source, d.received_at,
                   dev.device_id, dev.name as device_name, dev.project_id,
                   EXTRACT(EPOCH FROM d.received_at) as timestamp
            FROM iot_device_data d
            JOIN iot_devices dev ON d.device_id = dev.id
            WHERE dev.user_id = %s
        '''
        params = [user_id]

        if device_id:
            try:
                device_internal_id = int(device_id)
                query += ' AND dev.id = %s'
                params.append(device_internal_id)
            except ValueError:
                query += ' AND dev.device_id = %s'
                params.append(device_id)

        query += ' ORDER BY d.received_at DESC LIMIT %s'
        params.append(limit)

        messages = db.execute(query, params).fetchall()

        result = []
        for msg in messages:
            msg_dict = {
                'device_id': msg['device_id'],
                'device_name': msg['device_name'],
                'project_id': msg['project_id'],
                'data': msg['data'],
                'type': msg['data_type'],
                'source': msg['source'],
                'timestamp': msg['timestamp'],
                'created_at': str(msg['received_at']) if msg['received_at'] else None,
                'metadata': {'type': msg['data_type'], 'source': msg['source']}
            }
            if isinstance(msg_dict.get('data'), str):
                try:
                    msg_dict['data'] = json.loads(msg_dict['data'])
                except Exception:
                    pass
            result.append(msg_dict)

        if format_type == 'csv':
            import io
            import csv
            from flask import Response

            # Flatten the data for CSV
            output = io.StringIO()
            if result:
                # Get all possible keys from data fields
                all_data_keys = set()
                for r in result:
                    if isinstance(r.get('data'), dict):
                        all_data_keys.update(r['data'].keys())

                fieldnames = ['timestamp', 'device_id', 'project_id', 'created_at'] + sorted(all_data_keys)
                writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()

                for r in result:
                    row = {
                        'timestamp': r.get('timestamp'),
                        'device_id': r.get('device_id'),
                        'project_id': r.get('project_id'),
                        'created_at': r.get('created_at')
                    }
                    if isinstance(r.get('data'), dict):
                        row.update(r['data'])
                    writer.writerow(row)

            return Response(
                output.getvalue(),
                mimetype='text/csv',
                headers={'Content-Disposition': 'attachment;filename=iot_data_export.csv'}
            )
        else:
            return jsonify({'status': 'success', 'messages': result, 'count': len(result)})

    except Exception as e:
        logger.error(f"IoT export error: {e}")
        return jsonify({'error': True, 'message': 'Failed to export data'}), 500


@app.route('/api/iot/devices/<int:device_id>/stats', methods=['GET'])
@login_required
def iot_device_stats(device_id):
    """Get statistics and recent data for a specific device."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        device = db.execute(
            'SELECT device_id, name, device_type, status, last_seen FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        # Get message count and recent values
        msg_count = db.execute(
            'SELECT COUNT(*) as count FROM iot_messages WHERE user_id = %s AND device_id = %s',
            (user_id, device['device_id'])
        ).fetchone()

        # Get last 24 hours of data for sparklines
        recent_data = db.execute(
            '''SELECT data, EXTRACT(EPOCH FROM created_at) as timestamp
               FROM iot_messages
               WHERE user_id = %s AND device_id = %s
                 AND created_at >= NOW() - INTERVAL '24 hours'
               ORDER BY created_at ASC
               LIMIT 100''',
            (user_id, device['device_id'])
        ).fetchall()

        # Parse and collect values for each metric
        metrics = {}
        for row in recent_data:
            data = row['data']
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    continue

            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, (int, float)):
                        if key not in metrics:
                            metrics[key] = []
                        metrics[key].append({'value': value, 'timestamp': row['timestamp']})

        return jsonify({
            'status': 'success',
            'device': dict(device),
            'total_messages': msg_count['count'] if msg_count else 0,
            'metrics': metrics
        })

    except Exception as e:
        logger.error(f"IoT device stats error: {e}")
        return jsonify({'error': True, 'message': 'Failed to get device stats'}), 500


@app.route('/api/iot/key/regenerate', methods=['POST'])
@login_required
def iot_regenerate_key():
    """Regenerate the IoT API key for the current user."""
    db = get_db()
    user_id = session.get('user_id')

    new_key = 'iot_' + secrets.token_hex(24)

    try:
        db.execute(
            'UPDATE users SET iot_api_key = %s WHERE id = %s',
            (new_key, user_id)
        )
        db.commit()

        return jsonify({
            'status': 'success',
            'api_key': new_key
        })
    except Exception as e:
        logger.error(f"IoT regenerate key error: {e}")
        return jsonify({
            'error': True,
            'message': 'Failed to regenerate key'
        }), 500


# ============================================================================
# IOT BRIDGE API v1 (External device communication with HMAC signing)
# ============================================================================

def verify_iot_signature(api_key: str, payload: dict, timestamp: int, signature: str) -> bool:
    """Verify HMAC-SHA256 signature for IoT requests."""
    # Check timestamp is within 5 minutes
    current_time = int(time.time())
    if abs(current_time - timestamp) > 300:  # 5 minutes
        return False

    # Compute expected signature
    message = f"{timestamp}.{json.dumps(payload, sort_keys=True)}"
    expected_signature = hmac.new(
        api_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # Constant-time comparison
    return hmac.compare_digest(expected_signature, signature)


def get_user_by_iot_key(api_key: str):
    """Get user by IoT API key."""
    if not api_key or not api_key.startswith('iot_'):
        return None

    db = get_db()
    try:
        user = db.execute(
            'SELECT id FROM users WHERE iot_api_key = %s',
            (api_key,)
        ).fetchone()
        return user
    except Exception as e:
        logger.error(f"Error getting user by IoT key: {e}")
        return None


@app.route('/api/iot/v1/data', methods=['POST'])
def iot_v1_receive_data():
    """
    Receive data from an IoT device (v1 API with HMAC signing).

    Headers:
        X-Phonos-Timestamp: Unix timestamp
        X-Phonos-Signature: HMAC-SHA256 signature

    Body:
        {
            "device_id": "sensor-001",
            "project_id": 1,
            "timestamp": 1704067200,
            "data": {...},
            "metadata": {...}  # optional
        }
    """
    # Get API key from query param or form data (for devices that can't set headers easily)
    api_key = request.args.get('api_key')

    # Get timestamp and signature from headers
    timestamp_str = request.headers.get('X-Phonos-Timestamp')
    signature = request.headers.get('X-Phonos-Signature')

    if not timestamp_str or not signature:
        return jsonify({
            'error': True,
            'message': 'Missing timestamp or signature headers'
        }), 401

    try:
        timestamp = int(timestamp_str)
    except (ValueError, TypeError):
        return jsonify({
            'error': True,
            'message': 'Invalid timestamp'
        }), 400

    # Get payload
    payload = request.get_json()
    if not payload:
        return jsonify({
            'error': True,
            'message': 'No data provided'
        }), 400

    # If no API key in query, we need to find user by trying all keys (not ideal)
    # Better: require API key in query or use a different auth method
    db = get_db()
    user = None

    if api_key:
        user = get_user_by_iot_key(api_key)
        if not user:
            return jsonify({
                'error': True,
                'message': 'Invalid API key'
            }), 401

        # Verify signature
        if not verify_iot_signature(api_key, payload, timestamp, signature):
            return jsonify({
                'error': True,
                'message': 'Invalid signature or expired request'
            }), 401
    else:
        # Try to find user by scanning keys (less secure, fallback)
        # For production, always require api_key parameter
        try:
            users = db.execute(
                'SELECT id, iot_api_key FROM users WHERE iot_api_key IS NOT NULL'
            ).fetchall()

            for u in users:
                if verify_iot_signature(u['iot_api_key'], payload, timestamp, signature):
                    user = u
                    api_key = u['iot_api_key']
                    break
        except Exception as e:
            logger.error(f"Error scanning for API keys: {e}")

        if not user:
            return jsonify({
                'error': True,
                'message': 'Could not verify request. Include api_key parameter.'
            }), 401

    # Extract data
    device_id = payload.get('device_id')
    project_id = payload.get('project_id')
    data = payload.get('data', {})
    metadata = payload.get('metadata', {})

    if not device_id:
        return jsonify({
            'error': True,
            'message': 'device_id is required'
        }), 400

    try:
        # Ensure tables exist
        db.execute('''
            CREATE TABLE IF NOT EXISTS iot_devices (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                device_id VARCHAR(128) NOT NULL,
                name VARCHAR(255),
                device_type VARCHAR(50) DEFAULT 'generic',
                project_id INTEGER,
                endpoint TEXT,
                api_key TEXT,
                poll_interval INTEGER DEFAULT 30,
                status VARCHAR(20) DEFAULT 'offline',
                last_seen TIMESTAMP,
                config JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(user_id, device_id)
            )
        ''')

        db.execute('''
            CREATE TABLE IF NOT EXISTS iot_messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                device_id VARCHAR(128) NOT NULL,
                project_id INTEGER,
                data JSONB NOT NULL,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')

        # Auto-register device if not exists
        existing_device = db.execute(
            'SELECT id FROM iot_devices WHERE user_id = %s AND device_id = %s',
            (user['id'], device_id)
        ).fetchone()

        device_db_id = None
        if not existing_device:
            result = db.execute(
                '''INSERT INTO iot_devices (user_id, device_id, name, device_type, project_id, status, last_seen)
                   VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                   RETURNING id''',
                (user['id'], device_id, device_id, metadata.get('device_type', 'generic'), project_id, 'online')
            )
            device_db_id = result.fetchone()['id']
        else:
            device_db_id = existing_device['id']
            # Update device status and current_state
            db.execute(
                '''UPDATE iot_devices
                   SET status = %s, last_seen = CURRENT_TIMESTAMP,
                       project_id = COALESCE(%s, project_id),
                       current_state = COALESCE(current_state, '{}'::jsonb) || %s,
                       last_data_at = CURRENT_TIMESTAMP
                   WHERE id = %s''',
                ('online', project_id, json.dumps(data), device_db_id)
            )

        # Store data in iot_device_data (v2 unified table)
        data_type = metadata.get('type', 'reading')
        source = metadata.get('source', 'v1_api')
        db.execute(
            '''INSERT INTO iot_device_data (device_id, data, data_type, source, received_at)
               VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)''',
            (device_db_id, json.dumps(data), data_type, source)
        )

        db.commit()

        logger.info(f"IoT data received from device {device_id}: {list(data.keys())}")

        return jsonify({
            'status': 'success',
            'message': 'Data received'
        })

    except Exception as e:
        logger.error(f"IoT v1 receive data error: {e}")
        return jsonify({
            'error': True,
            'message': 'Failed to process data'
        }), 500


@app.route('/api/iot/v1/register', methods=['POST'])
def iot_v1_register_device():
    """
    Register a new IoT device (v1 API with HMAC signing).

    Headers:
        X-Phonos-Timestamp: Unix timestamp
        X-Phonos-Signature: HMAC-SHA256 signature

    Body:
        {
            "device_id": "sensor-001",
            "project_id": 1,
            "name": "Temperature Sensor",
            "device_type": "temperature_sensor",
            "timestamp": 1704067200
        }
    """
    api_key = request.args.get('api_key')
    timestamp_str = request.headers.get('X-Phonos-Timestamp')
    signature = request.headers.get('X-Phonos-Signature')

    if not timestamp_str or not signature:
        return jsonify({
            'error': True,
            'message': 'Missing timestamp or signature headers'
        }), 401

    try:
        timestamp = int(timestamp_str)
    except (ValueError, TypeError):
        return jsonify({
            'error': True,
            'message': 'Invalid timestamp'
        }), 400

    payload = request.get_json()
    if not payload:
        return jsonify({
            'error': True,
            'message': 'No data provided'
        }), 400

    db = get_db()
    user = None

    if api_key:
        user = get_user_by_iot_key(api_key)
        if not user:
            return jsonify({
                'error': True,
                'message': 'Invalid API key'
            }), 401

        if not verify_iot_signature(api_key, payload, timestamp, signature):
            return jsonify({
                'error': True,
                'message': 'Invalid signature or expired request'
            }), 401
    else:
        # Fallback: scan keys
        try:
            users = db.execute(
                'SELECT id, iot_api_key FROM users WHERE iot_api_key IS NOT NULL'
            ).fetchall()

            for u in users:
                if verify_iot_signature(u['iot_api_key'], payload, timestamp, signature):
                    user = u
                    break
        except Exception as e:
            logger.error(f"Error scanning for API keys: {e}")

        if not user:
            return jsonify({
                'error': True,
                'message': 'Could not verify request'
            }), 401

    device_id = payload.get('device_id')
    project_id = payload.get('project_id')
    name = payload.get('name', device_id)
    device_type = payload.get('device_type', 'generic')

    if not device_id:
        return jsonify({
            'error': True,
            'message': 'device_id is required'
        }), 400

    try:
        # Ensure table exists
        db.execute('''
            CREATE TABLE IF NOT EXISTS iot_devices (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                device_id VARCHAR(128) NOT NULL,
                name VARCHAR(255),
                device_type VARCHAR(50) DEFAULT 'generic',
                project_id INTEGER,
                endpoint TEXT,
                api_key TEXT,
                poll_interval INTEGER DEFAULT 30,
                status VARCHAR(20) DEFAULT 'offline',
                last_seen TIMESTAMP,
                config JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(user_id, device_id)
            )
        ''')

        # Check if device exists
        existing = db.execute(
            'SELECT id FROM iot_devices WHERE user_id = %s AND device_id = %s',
            (user['id'], device_id)
        ).fetchone()

        if existing:
            # Update existing device
            db.execute(
                '''UPDATE iot_devices
                   SET name = %s, device_type = %s, project_id = %s, status = %s, last_seen = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND device_id = %s''',
                (name, device_type, project_id, 'online', user['id'], device_id)
            )
            message = 'Device updated'
        else:
            # Create new device
            db.execute(
                '''INSERT INTO iot_devices (user_id, device_id, name, device_type, project_id, status, last_seen)
                   VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)''',
                (user['id'], device_id, name, device_type, project_id, 'online')
            )
            message = 'Device registered'

        db.commit()

        return jsonify({
            'status': 'success',
            'message': message
        })

    except Exception as e:
        logger.error(f"IoT v1 register error: {e}")
        return jsonify({
            'error': True,
            'message': 'Failed to register device'
        }), 500


# ============================================================================
# LEGACY IOT BRIDGE API (Backward compatibility)
# ============================================================================

@app.route('/api/iot/bridge/data', methods=['POST'])
def iot_bridge_receive_data():
    """
    Legacy endpoint: Receive data from an IoT device.
    Devices authenticate using the IoT API key in the header.
    Now stores data in unified iot_device_data table.
    """
    api_key = request.headers.get('X-IoT-API-Key') or request.headers.get('Authorization', '').replace('Bearer ', '')

    if not api_key or not api_key.startswith('iot_'):
        return jsonify({'error': True, 'message': 'Invalid API key'}), 401

    db = get_db()

    try:
        # Find user by API key
        user = db.execute(
            'SELECT id FROM users WHERE iot_api_key = %s',
            (api_key,)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'Invalid API key'}), 401

        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        device_id = data.get('device_id')
        metrics = data.get('metrics', data.get('data', {}))
        data_type = data.get('type', 'reading')

        # Get or create device and store data
        if device_id:
            device = db.execute(
                'SELECT id FROM iot_devices WHERE device_id = %s AND user_id = %s',
                (device_id, user['id'])
            ).fetchone()

            if device:
                device_db_id = device['id']
                # Update device status and current_state
                db.execute(
                    '''UPDATE iot_devices
                       SET status = 'online', last_seen = CURRENT_TIMESTAMP,
                           current_state = COALESCE(current_state, '{}'::jsonb) || %s,
                           last_data_at = CURRENT_TIMESTAMP
                       WHERE id = %s''',
                    (json.dumps(metrics), device_db_id)
                )

                # Store data in iot_device_data
                if metrics:
                    db.execute(
                        '''INSERT INTO iot_device_data (device_id, data, data_type, source, received_at)
                           VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)''',
                        (device_db_id, json.dumps(metrics), data_type, 'bridge_api')
                    )

                db.commit()

        logger.info(f"IoT data received from device {device_id}: {list(metrics.keys())}")

        return jsonify({
            'status': 'success',
            'message': 'Data received'
        })
    except Exception as e:
        logger.error(f"IoT bridge receive error: {e}")
        return jsonify({
            'error': True,
            'message': 'Failed to process data'
        }), 500


@app.route('/api/iot/bridge/command', methods=['POST'])
def iot_bridge_send_command():
    """
    Legacy endpoint: Send a command to an IoT device via the bridge.
    """
    api_key = request.headers.get('X-IoT-API-Key') or request.headers.get('Authorization', '').replace('Bearer ', '')

    if not api_key or not api_key.startswith('iot_'):
        return jsonify({'error': True, 'message': 'Invalid API key'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': True, 'message': 'No data provided'}), 400

    device_id = data.get('device_id')
    command = data.get('command')

    logger.info(f"IoT command sent to device {device_id}: {command}")

    # In a real implementation, this would forward the command to the device
    return jsonify({
        'status': 'success',
        'message': 'Command queued',
        'note': 'Bridge forwarding not yet implemented'
    })


# ============================================================================
# IOT ENHANCED: DEVICE SCHEMA MANAGEMENT
# ============================================================================

@app.route('/api/iot/devices/<int:device_id>/schema', methods=['GET'])
@login_required
def iot_get_device_schema(device_id):
    """Get device I/O schema definition."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        # Verify device ownership
        device = db.execute(
            'SELECT * FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        # Get schema (create default if not exists)
        schema = db.execute(
            'SELECT * FROM iot_device_schemas WHERE device_id = %s',
            (device_id,)
        ).fetchone()

        if not schema:
            # Create default schema
            db.execute('''
                INSERT INTO iot_device_schemas (device_id, inputs, outputs, dashboard_config)
                VALUES (%s, %s, %s, %s)
            ''', (device_id, '[]', '[]', '{}'))
            db.commit()
            schema = db.execute(
                'SELECT * FROM iot_device_schemas WHERE device_id = %s',
                (device_id,)
            ).fetchone()

        return jsonify({
            'status': 'success',
            'schema': {
                'id': schema['id'],
                'device_id': device_id,
                'inputs': schema['inputs'] if isinstance(schema['inputs'], list) else json.loads(schema['inputs'] or '[]'),
                'outputs': schema['outputs'] if isinstance(schema['outputs'], list) else json.loads(schema['outputs'] or '[]'),
                'dashboard_config': schema['dashboard_config'] if isinstance(schema['dashboard_config'], dict) else json.loads(schema['dashboard_config'] or '{}'),
                'widget_styles': schema['widget_styles'] if isinstance(schema['widget_styles'], dict) else json.loads(schema['widget_styles'] or '{}'),
                'schema_version': schema['schema_version'],
                'updated_at': schema['updated_at'].isoformat() if schema['updated_at'] else None
            }
        })
    except Exception as e:
        logger.error(f"Error getting device schema: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/iot/devices/<int:device_id>/schema', methods=['PUT'])
@login_required
def iot_update_device_schema(device_id):
    """Update device I/O schema definition."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        # Verify device ownership
        device = db.execute(
            'SELECT * FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        inputs = json.dumps(data.get('inputs', []))
        outputs = json.dumps(data.get('outputs', []))
        dashboard_config = json.dumps(data.get('dashboard_config', {}))
        widget_styles = json.dumps(data.get('widget_styles', {}))

        # Upsert schema
        db.execute('''
            INSERT INTO iot_device_schemas (device_id, inputs, outputs, dashboard_config, widget_styles, updated_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (device_id)
            DO UPDATE SET
                inputs = EXCLUDED.inputs,
                outputs = EXCLUDED.outputs,
                dashboard_config = EXCLUDED.dashboard_config,
                widget_styles = EXCLUDED.widget_styles,
                schema_version = iot_device_schemas.schema_version + 1,
                updated_at = CURRENT_TIMESTAMP
        ''', (device_id, inputs, outputs, dashboard_config, widget_styles))
        db.commit()

        return jsonify({
            'status': 'success',
            'message': 'Schema updated'
        })
    except Exception as e:
        logger.error(f"Error updating device schema: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


# ============================================================================
# IOT ENHANCED: COMMAND MANAGEMENT (USER/WEB UI)
# ============================================================================

@app.route('/api/iot/devices/<int:device_id>/commands', methods=['POST'])
@login_required
def iot_send_command(device_id):
    """Send a command to a device (from web UI)."""
    import uuid
    db = get_db()
    user_id = session.get('user_id')

    try:
        # Verify device ownership
        device = db.execute(
            'SELECT * FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        command_id = str(uuid.uuid4())
        input_id = data.get('input_id')
        value = data.get('value')
        command_type = data.get('command_type', 'set_input')
        priority = data.get('priority', 0)

        # Insert command into queue
        db.execute('''
            INSERT INTO iot_command_queue
            (device_id, user_id, command_id, command_type, input_id, value, priority, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '5 minutes')
        ''', (device_id, user_id, command_id, command_type, input_id, json.dumps(value), priority))

        # Update device last_command_at
        db.execute(
            'UPDATE iot_devices SET last_command_at = CURRENT_TIMESTAMP WHERE id = %s',
            (device_id,)
        )
        db.commit()

        # If device is websocket connected, emit via Socket.IO
        if device['websocket_connected']:
            try:
                from flask_socketio import emit
                from ..app import socketio
                socketio.emit('iot_command', {
                    'command_id': command_id,
                    'input_id': input_id,
                    'value': value,
                    'command_type': command_type
                }, room=f'iot_device_{device_id}')
                # Mark as sent
                db.execute(
                    'UPDATE iot_command_queue SET status = %s, sent_at = CURRENT_TIMESTAMP WHERE command_id = %s',
                    ('sent', command_id)
                )
                db.commit()
            except Exception as socket_err:
                logger.warning(f"Could not emit via socket: {socket_err}")

        return jsonify({
            'status': 'success',
            'command_id': command_id,
            'message': 'Command queued'
        })
    except Exception as e:
        logger.error(f"Error sending command: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/iot/devices/<int:device_id>/commands', methods=['GET'])
@login_required
def iot_list_commands(device_id):
    """List commands for a device (pending and recent)."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        # Verify device ownership
        device = db.execute(
            'SELECT * FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        status_filter = request.args.get('status')
        limit = min(int(request.args.get('limit', 50)), 200)

        if status_filter:
            commands = db.execute('''
                SELECT * FROM iot_command_queue
                WHERE device_id = %s AND status = %s
                ORDER BY created_at DESC LIMIT %s
            ''', (device_id, status_filter, limit)).fetchall()
        else:
            commands = db.execute('''
                SELECT * FROM iot_command_queue
                WHERE device_id = %s
                ORDER BY created_at DESC LIMIT %s
            ''', (device_id, limit)).fetchall()

        return jsonify({
            'status': 'success',
            'commands': [{
                'id': cmd['id'],
                'command_id': cmd['command_id'],
                'command_type': cmd['command_type'],
                'input_id': cmd['input_id'],
                'value': cmd['value'] if isinstance(cmd['value'], (dict, list)) else json.loads(cmd['value'] or '{}'),
                'status': cmd['status'],
                'created_at': cmd['created_at'].isoformat() if cmd['created_at'] else None,
                'acknowledged_at': cmd['acknowledged_at'].isoformat() if cmd['acknowledged_at'] else None,
                'response': cmd['response'] if isinstance(cmd['response'], (dict, list, type(None))) else json.loads(cmd['response'] or 'null')
            } for cmd in commands]
        })
    except Exception as e:
        logger.error(f"Error listing commands: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/iot/devices/<int:device_id>/state', methods=['GET'])
@login_required
def iot_get_device_state(device_id):
    """Get current device state (latest input/output values)."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        device = db.execute(
            'SELECT * FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        current_state = device['current_state']
        if isinstance(current_state, str):
            current_state = json.loads(current_state or '{}')

        return jsonify({
            'status': 'success',
            'device_id': device_id,
            'state': current_state,
            'last_seen': device['last_seen'].isoformat() if device['last_seen'] else None,
            'online': device['status'] == 'online',
            'websocket_connected': device['websocket_connected']
        })
    except Exception as e:
        logger.error(f"Error getting device state: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


# ============================================================================
# IOT ENHANCED: DEVICE POLLING ENDPOINTS (FOR DEVICES)
# ============================================================================

@app.route('/api/iot/v1/commands', methods=['GET', 'POST'])
def iot_device_poll_commands():
    """Device endpoint: Poll for pending commands."""
    # Get API key
    api_key = request.args.get('api_key') or request.headers.get('X-IoT-API-Key')
    if not api_key or not api_key.startswith('iot_'):
        return jsonify({'error': True, 'message': 'Invalid API key'}), 401

    # Get user by API key
    user = get_user_by_iot_key(api_key)
    if not user:
        return jsonify({'error': True, 'message': 'Invalid API key'}), 401

    # Get device_id from request
    if request.method == 'POST':
        data = request.get_json() or {}
    else:
        data = {}
    device_id_str = data.get('device_id') or request.args.get('device_id')

    if not device_id_str:
        return jsonify({'error': True, 'message': 'device_id required'}), 400

    db = get_db()

    try:
        # Find device
        device = db.execute('''
            SELECT id FROM iot_devices
            WHERE user_id = %s AND (device_id = %s OR id::text = %s)
        ''', (user['id'], str(device_id_str), str(device_id_str))).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        device_db_id = device['id']

        # Get pending commands
        commands = db.execute('''
            SELECT command_id, command_type, input_id, value, priority, created_at
            FROM iot_command_queue
            WHERE device_id = %s AND status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 10
        ''', (device_db_id,)).fetchall()

        # Mark as sent
        if commands:
            command_ids = [cmd['command_id'] for cmd in commands]
            db.execute('''
                UPDATE iot_command_queue
                SET status = 'sent', sent_at = CURRENT_TIMESTAMP
                WHERE command_id = ANY(%s)
            ''', (command_ids,))

        # Update device last_seen
        db.execute('''
            UPDATE iot_devices
            SET last_seen = CURRENT_TIMESTAMP, status = 'online'
            WHERE id = %s
        ''', (device_db_id,))
        db.commit()

        return jsonify({
            'status': 'success',
            'commands': [{
                'command_id': cmd['command_id'],
                'command_type': cmd['command_type'],
                'input_id': cmd['input_id'],
                'value': cmd['value'] if isinstance(cmd['value'], (dict, list)) else json.loads(cmd['value'] or '{}')
            } for cmd in commands]
        })
    except Exception as e:
        logger.error(f"Error polling commands: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/iot/v1/commands/<command_id>/ack', methods=['POST'])
def iot_device_ack_command(command_id):
    """Device endpoint: Acknowledge command execution."""
    # Get API key
    api_key = request.args.get('api_key') or request.headers.get('X-IoT-API-Key')
    if not api_key or not api_key.startswith('iot_'):
        return jsonify({'error': True, 'message': 'Invalid API key'}), 401

    # Get user by API key
    user = get_user_by_iot_key(api_key)
    if not user:
        return jsonify({'error': True, 'message': 'Invalid API key'}), 401

    data = request.get_json() or {}
    success = data.get('success', True)
    response = data.get('response')
    error_message = data.get('error_message')

    db = get_db()

    try:
        # Verify command belongs to user's device
        cmd = db.execute('''
            SELECT cq.*, d.user_id
            FROM iot_command_queue cq
            JOIN iot_devices d ON d.id = cq.device_id
            WHERE cq.command_id = %s AND d.user_id = %s
        ''', (command_id, user['id'])).fetchone()

        if not cmd:
            return jsonify({'error': True, 'message': 'Command not found'}), 404

        # Update command status
        status = 'acknowledged' if success else 'failed'
        db.execute('''
            UPDATE iot_command_queue
            SET status = %s, acknowledged_at = CURRENT_TIMESTAMP,
                response = %s, error_message = %s
            WHERE command_id = %s
        ''', (status, json.dumps(response), error_message, command_id))

        # If this was a set_input command, update device state
        if cmd['command_type'] == 'set_input' and cmd['input_id'] and success:
            db.execute('''
                UPDATE iot_devices
                SET current_state = COALESCE(current_state, '{}'::jsonb) || %s
                WHERE id = %s
            ''', (json.dumps({cmd['input_id']: cmd['value']}), cmd['device_id']))

        db.commit()

        # Notify user via Socket.IO
        try:
            from flask_socketio import emit
            from ..app import socketio
            socketio.emit('iot_command_ack', {
                'command_id': command_id,
                'success': success,
                'response': response
            }, room=f'iot_user_{user["id"]}')
        except Exception:
            pass

        return jsonify({
            'status': 'success',
            'message': 'Command acknowledged'
        })
    except Exception as e:
        logger.error(f"Error acknowledging command: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


# ============================================================================
# IOT ENHANCED: SDK CODE GENERATION
# ============================================================================

@app.route('/api/iot/devices/<int:device_id>/sdk/<platform>', methods=['GET'])
@login_required
def iot_generate_sdk(device_id, platform):
    """Generate SDK code for a device."""
    db = get_db()
    user_id = session.get('user_id')

    try:
        # Get device
        device = db.execute(
            'SELECT * FROM iot_devices WHERE id = %s AND user_id = %s',
            (device_id, user_id)
        ).fetchone()

        if not device:
            return jsonify({'error': True, 'message': 'Device not found'}), 404

        # Get user's API key
        user = db.execute('SELECT iot_api_key FROM users WHERE id = %s', (user_id,)).fetchone()
        api_key = user['iot_api_key'] if user else 'YOUR_API_KEY'

        # Get schema
        schema = db.execute(
            'SELECT * FROM iot_device_schemas WHERE device_id = %s',
            (device_id,)
        ).fetchone()

        inputs = []
        outputs = []
        if schema:
            inputs = schema['inputs'] if isinstance(schema['inputs'], list) else json.loads(schema['inputs'] or '[]')
            outputs = schema['outputs'] if isinstance(schema['outputs'], list) else json.loads(schema['outputs'] or '[]')

        # Get base URL
        base_url = request.host_url.rstrip('/')

        # Generate code based on platform
        if platform == 'python':
            code = generate_python_sdk(device, api_key, base_url, inputs, outputs)
            filename = f"phonos_iot_{device['device_id']}.py"
            content_type = 'text/x-python'
        elif platform == 'javascript':
            code = generate_javascript_sdk(device, api_key, base_url, inputs, outputs)
            filename = f"phonos_iot_{device['device_id']}.js"
            content_type = 'application/javascript'
        elif platform == 'arduino':
            code = generate_arduino_sdk(device, api_key, base_url, inputs, outputs)
            filename = f"PhonosIoT_{device['device_id']}.h"
            content_type = 'text/x-c'
        else:
            return jsonify({'error': True, 'message': f'Unknown platform: {platform}'}), 400

        # Return as file download or JSON
        if request.args.get('download') == 'true':
            from flask import Response
            return Response(
                code,
                mimetype=content_type,
                headers={'Content-Disposition': f'attachment; filename={filename}'}
            )

        return jsonify({
            'status': 'success',
            'platform': platform,
            'filename': filename,
            'code': code
        })
    except Exception as e:
        logger.error(f"Error generating SDK: {e}")
        return jsonify({'error': True, 'message': str(e)}), 500


def generate_python_sdk(device, api_key, base_url, inputs, outputs):
    """Generate Python SDK code."""
    device_id = device['device_id']
    device_name = device['name'] or device_id

    # Generate input methods
    input_methods = []
    for inp in inputs:
        if inp['type'] == 'toggle':
            input_methods.append(f'''
    def set_{inp['id']}(self, value: bool) -> dict:
        """Set {inp.get('label', inp['id'])} (toggle)."""
        return self.send_command('{inp['id']}', value)''')
        elif inp['type'] == 'slider':
            min_val = inp.get('min', 0)
            max_val = inp.get('max', 100)
            input_methods.append(f'''
    def set_{inp['id']}(self, value: float) -> dict:
        """Set {inp.get('label', inp['id'])} (range: {min_val}-{max_val})."""
        if not {min_val} <= value <= {max_val}:
            raise ValueError(f"Value must be between {min_val} and {max_val}")
        return self.send_command('{inp['id']}', value)''')
        elif inp['type'] == 'button':
            input_methods.append(f'''
    def trigger_{inp['id']}(self) -> dict:
        """Trigger {inp.get('label', inp['id'])} action."""
        return self.send_command('{inp['id']}', True, command_type='action')''')

    # Generate output type hints
    output_hints = []
    for out in outputs:
        out_type = 'float' if out['type'] == 'numeric' else 'bool' if out['type'] == 'binary' else 'str'
        output_hints.append(f"        {out['id']}: {out_type}  # {out.get('label', out['id'])}")

    return f'''#!/usr/bin/env python3
"""
Phonos IoT SDK - Auto-generated for: {device_name}
Device ID: {device_id}
Base URL: {base_url}

Usage:
    client = PhonosIoTClient("{api_key[:8]}...")
    client.send_readings(temperature=23.5, humidity=45.0)
    commands = client.poll_commands()
"""
import hashlib
import hmac
import json
import time
import urllib.request
from typing import Any, Dict, List, Optional

class PhonosIoTClient:
    """Phonos IoT Device Client for {device_name}."""

    def __init__(self, api_key: str, base_url: str = "{base_url}"):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.device_id = "{device_id}"

    def _sign_request(self, payload: dict) -> tuple:
        """Generate HMAC-SHA256 signature."""
        timestamp = int(time.time())
        message = f"{{timestamp}}.{{json.dumps(payload, sort_keys=True)}}"
        signature = hmac.new(
            self.api_key.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return timestamp, signature

    def _request(self, endpoint: str, payload: dict, method: str = "POST") -> dict:
        """Make authenticated API request."""
        timestamp, signature = self._sign_request(payload)

        url = f"{{self.base_url}}{{endpoint}}?api_key={{self.api_key}}"
        data = json.dumps(payload, sort_keys=True).encode()

        req = urllib.request.Request(url, data=data if method == "POST" else None, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Phonos-Timestamp", str(timestamp))
        req.add_header("X-Phonos-Signature", signature)

        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())

    # =========================================================================
    # OUTPUT METHODS (Send readings to Phonos)
    # =========================================================================

    def send_readings(self, **kwargs) -> dict:
        """Send output readings to Phonos.

        Available outputs:
{chr(10).join(output_hints) if output_hints else '        (No outputs defined)'}
        """
        return self._request("/api/iot/v1/data", {{
            "device_id": self.device_id,
            "data": kwargs
        }})

    # =========================================================================
    # INPUT METHODS (Receive commands from Phonos)
    # =========================================================================

    def poll_commands(self) -> List[dict]:
        """Poll for pending commands from Phonos.

        Returns list of commands: [{{'command_id': str, 'input_id': str, 'value': any}}]
        """
        response = self._request("/api/iot/v1/commands", {{
            "device_id": self.device_id
        }})
        return response.get("commands", [])

    def acknowledge_command(self, command_id: str, success: bool = True, response: Any = None) -> dict:
        """Acknowledge command execution."""
        return self._request(f"/api/iot/v1/commands/{{command_id}}/ack", {{
            "device_id": self.device_id,
            "success": success,
            "response": response
        }})

    def send_command(self, input_id: str, value: Any, command_type: str = "set_input") -> dict:
        """Send a command (for testing from Python)."""
        return self._request("/api/iot/v1/data", {{
            "device_id": self.device_id,
            "data": {{input_id: value}},
            "metadata": {{"command_type": command_type}}
        }})
{"".join(input_methods)}


# =============================================================================
# EXAMPLE USAGE
# =============================================================================

if __name__ == "__main__":
    # Initialize client
    client = PhonosIoTClient("{api_key}")

    # Send readings example
    # client.send_readings(temperature=23.5, humidity=45.0)

    # Poll and handle commands
    commands = client.poll_commands()
    for cmd in commands:
        print(f"Command: {{cmd['input_id']}} = {{cmd['value']}}")
        # Execute command on your hardware here...
        client.acknowledge_command(cmd["command_id"], success=True)
'''


def generate_javascript_sdk(device, api_key, base_url, inputs, outputs):
    """Generate JavaScript/Node.js SDK code."""
    device_id = device['device_id']
    device_name = device['name'] or device_id

    return f'''/**
 * Phonos IoT SDK - JavaScript/Node.js
 * Auto-generated for: {device_name}
 * Device ID: {device_id}
 *
 * Usage (Node.js):
 *   const {{ PhonosIoTClient }} = require('./phonos_iot_{device_id}');
 *   const client = new PhonosIoTClient('{api_key[:8]}...');
 *   await client.sendReadings({{ temperature: 23.5 }});
 */

const crypto = require('crypto');
const https = require('https');
const http = require('http');

class PhonosIoTClient {{
    constructor(apiKey, options = {{}}) {{
        this.apiKey = apiKey;
        this.baseUrl = options.baseUrl || '{base_url}';
        this.deviceId = '{device_id}';
    }}

    _signRequest(payload) {{
        const timestamp = Math.floor(Date.now() / 1000);
        const sortedPayload = JSON.stringify(payload, Object.keys(payload).sort());
        const message = `${{timestamp}}.${{sortedPayload}}`;
        const signature = crypto
            .createHmac('sha256', this.apiKey)
            .update(message)
            .digest('hex');
        return {{ timestamp, signature, sortedPayload }};
    }}

    async _request(endpoint, payload, method = 'POST') {{
        const {{ timestamp, signature, sortedPayload }} = this._signRequest(payload);

        return new Promise((resolve, reject) => {{
            const url = new URL(`${{this.baseUrl}}${{endpoint}}`);
            url.searchParams.append('api_key', this.apiKey);

            const isHttps = url.protocol === 'https:';
            const lib = isHttps ? https : http;

            const options = {{
                hostname: url.hostname,
                port: url.port || (isHttps ? 443 : 80),
                path: `${{url.pathname}}${{url.search}}`,
                method,
                headers: {{
                    'Content-Type': 'application/json',
                    'X-Phonos-Timestamp': timestamp.toString(),
                    'X-Phonos-Signature': signature
                }}
            }};

            const req = lib.request(options, (res) => {{
                let data = '';
                res.on('data', chunk => data += chunk);
                res.on('end', () => {{
                    try {{
                        resolve(JSON.parse(data));
                    }} catch (e) {{
                        reject(new Error(`Invalid JSON response: ${{data}}`));
                    }}
                }});
            }});

            req.on('error', reject);
            req.write(sortedPayload);
            req.end();
        }});
    }}

    // Send readings to Phonos
    async sendReadings(readings) {{
        return this._request('/api/iot/v1/data', {{
            device_id: this.deviceId,
            data: readings
        }});
    }}

    // Poll for pending commands
    async pollCommands() {{
        const response = await this._request('/api/iot/v1/commands', {{
            device_id: this.deviceId
        }});
        return response.commands || [];
    }}

    // Acknowledge command execution
    async acknowledgeCommand(commandId, success = true, response = null) {{
        return this._request(`/api/iot/v1/commands/${{commandId}}/ack`, {{
            device_id: this.deviceId,
            success,
            response
        }});
    }}
}}

module.exports = {{ PhonosIoTClient }};

// Example usage
if (require.main === module) {{
    (async () => {{
        const client = new PhonosIoTClient('{api_key}');

        // Send readings
        // await client.sendReadings({{ temperature: 23.5, humidity: 45.0 }});

        // Poll commands
        const commands = await client.pollCommands();
        for (const cmd of commands) {{
            console.log(`Command: ${{cmd.input_id}} = ${{cmd.value}}`);
            await client.acknowledgeCommand(cmd.command_id, true);
        }}
    }})();
}}
'''


def generate_arduino_sdk(device, api_key, base_url, inputs, outputs):
    """Generate Arduino/ESP32 SDK code."""
    device_id = device['device_id']
    device_name = device['name'] or device_id
    class_name = ''.join(word.capitalize() for word in device_id.replace('-', '_').split('_'))

    # Parse URL for host
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    host = parsed.netloc
    is_https = parsed.scheme == 'https'

    return f'''/**
 * Phonos IoT SDK - Arduino/ESP32
 * Auto-generated for: {device_name}
 * Device ID: {device_id}
 *
 * Requirements:
 *   - ESP32 or ESP8266 board
 *   - ArduinoJson library (v6+)
 *   - WiFi library (built-in)
 *
 * Usage:
 *   #include "PhonosIoT_{device_id}.h"
 *   PhonosIoT iot("{api_key[:16]}...");
 *   iot.begin();
 *   iot.sendReading("temperature", 23.5);
 */

#ifndef PHONOS_IOT_{device_id.upper().replace('-', '_')}_H
#define PHONOS_IOT_{device_id.upper().replace('-', '_')}_H

#include <Arduino.h>
#include <ArduinoJson.h>

#if defined(ESP32)
    #include <WiFi.h>
    #include <HTTPClient.h>
    #include <mbedtls/md.h>
#elif defined(ESP8266)
    #include <ESP8266WiFi.h>
    #include <ESP8266HTTPClient.h>
    #include <Hash.h>
#endif

class PhonosIoT {{
public:
    PhonosIoT(const char* apiKey) : _apiKey(apiKey) {{
        _deviceId = "{device_id}";
        _baseUrl = "{base_url}";
    }}

    void begin() {{
        // Initialize - called after WiFi connected
    }}

    // Send a single reading
    bool sendReading(const char* key, float value) {{
        StaticJsonDocument<256> doc;
        doc["device_id"] = _deviceId;
        JsonObject data = doc.createNestedObject("data");
        data[key] = value;
        return _sendRequest("/api/iot/v1/data", doc);
    }}

    // Send multiple readings
    bool sendReadings(JsonDocument& readings) {{
        StaticJsonDocument<512> doc;
        doc["device_id"] = _deviceId;
        doc["data"] = readings;
        return _sendRequest("/api/iot/v1/data", doc);
    }}

    // Poll for commands (returns number of commands)
    int pollCommands(JsonDocument& commands) {{
        StaticJsonDocument<256> payload;
        payload["device_id"] = _deviceId;

        String response = _sendRequestGetResponse("/api/iot/v1/commands", payload);
        if (response.length() > 0) {{
            StaticJsonDocument<1024> responseDoc;
            DeserializationError error = deserializeJson(responseDoc, response);
            if (!error && responseDoc.containsKey("commands")) {{
                commands.set(responseDoc["commands"]);
                return responseDoc["commands"].size();
            }}
        }}
        return 0;
    }}

    // Acknowledge command
    bool acknowledgeCommand(const char* commandId, bool success = true) {{
        StaticJsonDocument<256> doc;
        doc["device_id"] = _deviceId;
        doc["success"] = success;

        String endpoint = "/api/iot/v1/commands/";
        endpoint += commandId;
        endpoint += "/ack";

        return _sendRequest(endpoint.c_str(), doc);
    }}

private:
    const char* _apiKey;
    const char* _deviceId;
    const char* _baseUrl;

    String _computeHMAC(const String& message) {{
        byte hmacResult[32];

#if defined(ESP32)
        mbedtls_md_context_t ctx;
        mbedtls_md_init(&ctx);
        mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
        mbedtls_md_hmac_starts(&ctx, (const unsigned char*)_apiKey, strlen(_apiKey));
        mbedtls_md_hmac_update(&ctx, (const unsigned char*)message.c_str(), message.length());
        mbedtls_md_hmac_finish(&ctx, hmacResult);
        mbedtls_md_free(&ctx);
#elif defined(ESP8266)
        // ESP8266 uses different crypto library
        // For simplicity, you may need to add a HMAC library
        // This is a placeholder
        memset(hmacResult, 0, 32);
#endif

        String signature = "";
        for (int i = 0; i < 32; i++) {{
            char hex[3];
            sprintf(hex, "%02x", hmacResult[i]);
            signature += hex;
        }}
        return signature;
    }}

    bool _sendRequest(const char* endpoint, JsonDocument& doc) {{
        String response = _sendRequestGetResponse(endpoint, doc);
        return response.indexOf("\\"success\\"") != -1;
    }}

    String _sendRequestGetResponse(const char* endpoint, JsonDocument& doc) {{
        HTTPClient http;

        String payload;
        serializeJson(doc, payload);

        unsigned long timestamp = time(nullptr);
        if (timestamp < 1000000) {{
            timestamp = millis() / 1000 + 1700000000; // Fallback if NTP not synced
        }}

        String message = String(timestamp) + "." + payload;
        String signature = _computeHMAC(message);

        String url = String(_baseUrl) + endpoint + "?api_key=" + _apiKey;

#if defined(ESP32)
        http.begin(url);
#elif defined(ESP8266)
        WiFiClient client;
        http.begin(client, url);
#endif

        http.addHeader("Content-Type", "application/json");
        http.addHeader("X-Phonos-Timestamp", String(timestamp));
        http.addHeader("X-Phonos-Signature", signature);

        int httpCode = http.POST(payload);
        String response = "";

        if (httpCode > 0) {{
            response = http.getString();
        }}

        http.end();
        return response;
    }}
}};

#endif // PHONOS_IOT_{device_id.upper().replace('-', '_')}_H
'''
