"""API routes for flow-related operations."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from flask import abort, g, jsonify, request, session

from config import Config
from ..app import app, get_redis_connection, logger, socketio
from ..helpers import (
    create_user_notification,
    get_db,
    login_required,
    project_role_required,
    serialize_datetime,
    get_project_assignment,
)
from ..helpers.analytics import track_event
from ..helpers.security import csrf_protect, rate_limit, sanitize_input

PROJECT_ROLE_CONTRIBUTOR = Config.PROJECT_ROLE_CONTRIBUTOR
PROJECT_ROLE_MANAGER = Config.PROJECT_ROLE_MANAGER
PROJECT_ROLE_VIEWER = Config.PROJECT_ROLE_VIEWER


def register_flow_routes(app):
    """Register all flow-related routes."""

    @app.route('/api/projects/<int:project_id>/flows', methods=['GET'])
    @login_required
    @project_role_required([PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER])
    def get_project_flows(project_id):
        """Get all flows for a project."""
        try:
            db = get_db()

            result = db.execute("""
                SELECT
                    f.id,
                    f.flow_id,
                    f.project_id,
                    f.creator_id,
                    f.name,
                    f.description,
                    f.flow_data,
                    f.status,
                    f.created_at,
                    f.updated_at,
                    u.username as creator_username
                FROM flows f
                LEFT JOIN users u ON u.id = f.creator_id
                WHERE f.project_id = %s
                ORDER BY f.created_at DESC
            """, (project_id,))

            flows = []
            for row in result.fetchall():
                flow_data = json.loads(row['flow_data']) if row['flow_data'] else {'nodes': [], 'connections': []}
                flows.append({
                    'id': row['id'],
                    'flow_id': row['flow_id'],
                    'project_id': row['project_id'],
                    'creator_id': row['creator_id'],
                    'name': row['name'],
                    'description': row['description'],
                    'flow_data': flow_data,
                    'status': row['status'],
                    'created_at': serialize_datetime(row['created_at']),
                    'updated_at': serialize_datetime(row['updated_at']),
                    'creator_username': row['creator_username'],
                })

            track_event(session['user_id'], 'flows_list_viewed', metadata={'project_id': project_id, 'flow_count': len(flows)})

            return jsonify({'flows': flows})

        except Exception as e:
            logger.exception(f"Error fetching flows for project {project_id}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/projects/<int:project_id>/flows', methods=['POST'])
    @login_required
    @csrf_protect
    @rate_limit(limit=20, window=60)
    @project_role_required([PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR])
    def create_flow(project_id):
        """Create a new flow."""
        try:
            data = request.get_json()

            # Validate required fields
            if not data.get('name'):
                return jsonify({'error': 'Flow name is required'}), 400

            if not data.get('flow_data'):
                return jsonify({'error': 'Flow data is required'}), 400

            name = sanitize_input(data['name'])
            description = sanitize_input(data.get('description', ''))
            flow_data = data['flow_data']

            # Validate flow_data structure
            if not isinstance(flow_data, dict):
                return jsonify({'error': 'Invalid flow data format'}), 400

            if 'nodes' not in flow_data or 'connections' not in flow_data:
                return jsonify({'error': 'Flow data must contain nodes and connections'}), 400

            # Generate unique flow ID
            flow_id = str(uuid.uuid4())

            db = get_db()
            # cursor = db.cursor() # Not needed with PostgreSQL wrapper

            # Insert flow
            db.execute("""
                INSERT INTO flows (
                    flow_id, project_id, creator_id, name, description, flow_data, status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (
                flow_id,
                project_id,
                session['user_id'],
                name,
                description,
                json.dumps(flow_data)
            ))

            db.commit()

            # Get the created flow
            result = db.execute("""
                SELECT
                    f.id, f.flow_id, f.project_id, f.creator_id, f.name, f.description,
                    f.flow_data, f.status, f.created_at, f.updated_at,
                    u.username as creator_username
                FROM flows f
                LEFT JOIN users u ON u.id = f.creator_id
                WHERE f.flow_id = %s
            """, (flow_id,))

            row = result.fetchone()
            created_flow = {
                'id': row['id'],
                'flow_id': row['flow_id'],
                'project_id': row['project_id'],
                'creator_id': row['creator_id'],
                'name': row['name'],
                'description': row['description'],
                'flow_data': json.loads(row['flow_data']) if row['flow_data'] else {},
                'status': row['status'],
                'created_at': serialize_datetime(row['created_at']),
                'updated_at': serialize_datetime(row['updated_at']),
                'creator_username': row['creator_username'],
            }

            track_event(session['user_id'], 'flow_created', metadata={
                'project_id': project_id,
                'flow_id': flow_id,
                'node_count': len(flow_data.get('nodes', []))
            })

            # Emit socket event
            socketio.emit('flow_created', {
                'flow': created_flow,
                'project_id': project_id
            }, room=f'project_{project_id}')

            return jsonify({'flow': created_flow}), 201

        except Exception as e:
            logger.exception(f"Error creating flow for project {project_id}")
            if db:
                db.rollback()
            return jsonify({'error': str(e)}), 500


    @app.route('/api/flows/<flow_id>', methods=['GET'])
    @login_required
    def get_flow(flow_id):
        """Get a specific flow by ID."""
        try:
            db = get_db()
            # cursor = db.cursor() # Not needed with PostgreSQL wrapper

            result = db.execute("""
                SELECT
                    f.id, f.flow_id, f.project_id, f.creator_id, f.name, f.description,
                    f.flow_data, f.status, f.created_at, f.updated_at,
                    u.username as creator_username
                FROM flows f
                LEFT JOIN users u ON u.id = f.creator_id
                WHERE f.flow_id = %s
            """, (flow_id,))

            row = result.fetchone()
            if not row:
                return jsonify({'error': 'Flow not found'}), 404

            project_id = row['project_id']

            # Check if user has access to the project
            result = db.execute("""
                SELECT pa.role
                FROM project_assignments pa
                WHERE pa.project_id = %s AND pa.user_id = %s
            """, (project_id, session['user_id']))

            if not result.fetchone() and not session.get('is_admin'):
                return jsonify({'error': 'Access denied'}), 403

            flow = {
                'id': row['id'],
                'flow_id': row['flow_id'],
                'project_id': row['project_id'],
                'creator_id': row['creator_id'],
                'name': row['name'],
                'description': row['description'],
                'flow_data': json.loads(row['flow_data']) if row['flow_data'] else {},
                'status': row['status'],
                'created_at': serialize_datetime(row['created_at']),
                'updated_at': serialize_datetime(row['updated_at']),
                'creator_username': row['creator_username'],
            }

            return jsonify({'flow': flow})

        except Exception as e:
            logger.exception(f"Error fetching flow {flow_id}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/flows/<flow_id>', methods=['PUT'])
    @login_required
    @csrf_protect
    @project_role_required([PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR])
    def update_flow(flow_id):
        """Update a flow."""
        try:
            data = request.get_json()
            db = get_db()
            # cursor = db.cursor() # Not needed with PostgreSQL wrapper

            # Check if flow exists and user has access
            result = db.execute("""
                SELECT f.project_id, f.creator_id
                FROM flows f
                WHERE f.flow_id = %s
            """, (flow_id,))

            row = result.fetchone()
            if not row:
                return jsonify({'error': 'Flow not found'}), 404

            project_id = row['project_id']

            # Build update query
            updates = []
            params = []

            if 'name' in data:
                updates.append('name = %s')
                params.append(sanitize_input(data['name']))

            if 'description' in data:
                updates.append('description = %s')
                params.append(sanitize_input(data['description']))

            if 'flow_data' in data:
                updates.append('flow_data = %s')
                params.append(json.dumps(data['flow_data']))

            if 'status' in data:
                updates.append('status = %s')
                params.append(data['status'])

            if not updates:
                return jsonify({'error': 'No fields to update'}), 400

            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.append(flow_id)

            result = db.execute(f"""
                UPDATE flows
                SET {', '.join(updates)}
                WHERE flow_id = %s
            """, params)

            db.commit()

            # Get updated flow
            result = db.execute("""
                SELECT
                    f.id, f.flow_id, f.project_id, f.creator_id, f.name, f.description,
                    f.flow_data, f.status, f.created_at, f.updated_at,
                    u.username as creator_username
                FROM flows f
                LEFT JOIN users u ON u.id = f.creator_id
                WHERE f.flow_id = %s
            """, (flow_id,))

            row = result.fetchone()
            updated_flow = {
                'id': row['id'],
                'flow_id': row['flow_id'],
                'project_id': row['project_id'],
                'creator_id': row['creator_id'],
                'name': row['name'],
                'description': row['description'],
                'flow_data': json.loads(row['flow_data']) if row['flow_data'] else {},
                'status': row['status'],
                'created_at': serialize_datetime(row['created_at']),
                'updated_at': serialize_datetime(row['updated_at']),
                'creator_username': row['creator_username'],
            }

            track_event(session['user_id'], 'flow_updated', metadata={'flow_id': flow_id, 'project_id': project_id})

            # Emit socket event
            socketio.emit('flow_updated', {
                'flow': updated_flow,
                'project_id': project_id
            }, room=f'project_{project_id}')

            return jsonify({'flow': updated_flow})

        except Exception as e:
            logger.exception(f"Error updating flow {flow_id}")
            if db:
                db.rollback()
            return jsonify({'error': str(e)}), 500


    @app.route('/api/flows/<flow_id>', methods=['DELETE'])
    @login_required
    @csrf_protect
    @project_role_required([PROJECT_ROLE_MANAGER])
    def delete_flow(flow_id):
        """Delete a flow."""
        try:
            db = get_db()
            # cursor = db.cursor() # Not needed with PostgreSQL wrapper

            # Check if flow exists
            result = db.execute("""
                SELECT f.project_id FROM flows f WHERE f.flow_id = %s
            """, (flow_id,))

            row = result.fetchone()
            if not row:
                return jsonify({'error': 'Flow not found'}), 404

            project_id = row['project_id']

            # Delete flow
            result = db.execute("DELETE FROM flows WHERE flow_id = %s", (flow_id,))
            db.commit()

            track_event(session['user_id'], 'flow_deleted', metadata={'flow_id': flow_id, 'project_id': project_id})

            # Emit socket event
            socketio.emit('flow_deleted', {
                'flow_id': flow_id,
                'project_id': project_id
            }, room=f'project_{project_id}')

            return jsonify({'message': 'Flow deleted successfully'})

        except Exception as e:
            logger.exception(f"Error deleting flow {flow_id}")
            if db:
                db.rollback()
            return jsonify({'error': str(e)}), 500


    @app.route('/api/flows/<flow_id>/execute', methods=['POST'])
    @login_required
    @csrf_protect
    @rate_limit(limit=10, window=60)
    def execute_flow(flow_id):
        """Execute a flow."""
        try:
            db = get_db()
            # cursor = db.cursor() # Not needed with PostgreSQL wrapper

            # Get flow
            result = db.execute("""
                SELECT f.project_id, f.flow_data, f.name
                FROM flows f
                WHERE f.flow_id = %s
            """, (flow_id,))

            row = result.fetchone()
            if not row:
                return jsonify({'error': 'Flow not found'}), 404

            project_id = row['project_id']
            flow_data = json.loads(row['flow_data']) if row['flow_data'] else {}
            flow_name = row['name']

            # Manual permission check
            role = get_project_assignment(db, session['user_id'], project_id)
            if not role or role not in [PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR]:
                # Check if admin
                if not session.get('is_admin'):
                    return jsonify({'error': 'Access denied'}), 403

            nodes = flow_data.get('nodes', [])
            if not nodes:
                return jsonify({'error': 'Flow has no nodes to execute'}), 400

            # Generate execution ID
            execution_id = str(uuid.uuid4())

            # Create flow execution record
            db.execute("""
                INSERT INTO flow_executions (
                    execution_id, flow_id, project_id, creator_id,
                    status, current_step, total_steps, started_at
                ) VALUES (%s, %s, %s, %s, 'running', 0, %s, CURRENT_TIMESTAMP)
            """, (
                execution_id,
                flow_id,
                project_id,
                session['user_id'],
                len(nodes)
            ))

            # Create step records
            for i, node in enumerate(nodes):
                db.execute("""
                    INSERT INTO flow_execution_steps (
                        execution_id, step_index, step_type, step_config, status
                    ) VALUES (%s, %s, %s, %s, 'pending')
                """, (
                    execution_id,
                    i,
                    node.get('type', 'unknown'),
                    json.dumps(node)
                ))

            db.commit()

            # Queue the flow execution for processing
            # This would typically be sent to a background worker
            redis_conn = get_redis_connection()
            if redis_conn:
                redis_conn.publish('flow_execution_queue', json.dumps({
                    'execution_id': execution_id,
                    'flow_id': flow_id,
                    'project_id': project_id,
                    'user_id': session['user_id']
                }))

            track_event(session['user_id'], 'flow_executed', metadata={
                'flow_id': flow_id,
                'execution_id': execution_id,
                'project_id': project_id
            })

            create_user_notification(
                db,
                session['user_id'],
                'Flow Execution Started',
                f'Your flow "{flow_name}" has started executing.',
                category='flow',
                metadata={'flow_id': flow_id, 'execution_id': execution_id}
            )

            # Emit socket event
            socketio.emit('flow_execution_started', {
                'execution_id': execution_id,
                'flow_id': flow_id,
                'project_id': project_id
            }, room=f'project_{project_id}')

            return jsonify({
                'execution_id': execution_id,
                'status': 'running',
                'message': 'Flow execution started'
            }), 202

        except Exception as e:
            logger.exception(f"Error executing flow {flow_id}")
            if db:
                db.rollback()
            return jsonify({'error': str(e)}), 500


    @app.route('/api/flows/<flow_id>/executions', methods=['GET'])
    @login_required
    def get_flow_executions(flow_id):
        """Get all executions for a flow."""
        try:
            db = get_db()
            # cursor = db.cursor() # Not needed with PostgreSQL wrapper

            # Get flow to check permissions
            result = db.execute("SELECT project_id FROM flows WHERE flow_id = %s", (flow_id,))
            row = result.fetchone()
            if not row:
                return jsonify({'error': 'Flow not found'}), 404
            project_id = row['project_id']

            # Manual permission check
            role = get_project_assignment(db, session['user_id'], project_id)
            if not role and not session.get('is_admin'):
                 return jsonify({'error': 'Access denied'}), 403

            cursor = db.execute("""
                SELECT
                    fe.id, fe.execution_id, fe.flow_id, fe.project_id, fe.creator_id,
                    fe.status, fe.current_step, fe.total_steps, fe.error_message,
                    fe.started_at, fe.completed_at,
                    u.username as creator_username
                FROM flow_executions fe
                LEFT JOIN users u ON u.id = fe.creator_id
                WHERE fe.flow_id = %s
                ORDER BY fe.started_at DESC
            """, (flow_id,))

            executions = []
            for row in cursor.fetchall():
                executions.append({
                    'id': row['id'],
                    'execution_id': row['execution_id'],
                    'flow_id': row['flow_id'],
                    'project_id': row['project_id'],
                    'creator_id': row['creator_id'],
                    'status': row['status'],
                    'current_step': row['current_step'],
                    'total_steps': row['total_steps'],
                    'error_message': row['error_message'],
                    'started_at': serialize_datetime(row['started_at']),
                    'completed_at': serialize_datetime(row['completed_at']),
                    'creator_username': row['creator_username'],
                })

            return jsonify({'executions': executions})

        except Exception as e:
            logger.exception(f"Error fetching executions for flow {flow_id}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/flow-executions/<execution_id>', methods=['GET'])
    @login_required
    def get_flow_execution_details(execution_id):
        """Get details of a specific flow execution."""
        try:
            db = get_db()
            # cursor = db.cursor() # Not needed with PostgreSQL wrapper

            result = db.execute("""
                SELECT
                    fe.id, fe.execution_id, fe.flow_id, fe.project_id, fe.creator_id,
                    fe.status, fe.current_step, fe.total_steps, fe.error_message,
                    fe.started_at, fe.completed_at,
                    u.username as creator_username
                FROM flow_executions fe
                LEFT JOIN users u ON u.id = fe.creator_id
                WHERE fe.execution_id = %s
            """, (execution_id,))

            row = result.fetchone()
            if not row:
                return jsonify({'error': 'Execution not found'}), 404

            project_id = row['project_id']

            # Manual permission check
            role = get_project_assignment(db, session['user_id'], project_id)
            if not role and not session.get('is_admin'):
                 return jsonify({'error': 'Access denied'}), 403

            execution = {
                'id': row['id'],
                'execution_id': row['execution_id'],
                'flow_id': row['flow_id'],
                'project_id': row['project_id'],
                'creator_id': row['creator_id'],
                'status': row['status'],
                'current_step': row['current_step'],
                'total_steps': row['total_steps'],
                'error_message': row['error_message'],
                'started_at': serialize_datetime(row['started_at']),
                'completed_at': serialize_datetime(row['completed_at']),
                'creator_username': row['creator_username'],
            }

            # Get execution steps
            cursor = db.execute("""
                SELECT
                    id, execution_id, step_index, step_type, step_config,
                    job_id, status, started_at, completed_at, error_message
                FROM flow_execution_steps
                WHERE execution_id = %s
                ORDER BY step_index
            """, (execution_id,))

            steps = []
            for step_row in cursor.fetchall():
                steps.append({
                    'id': step_row['id'],
                    'execution_id': step_row['execution_id'],
                    'step_index': step_row['step_index'],
                    'step_type': step_row['step_type'],
                    'step_config': json.loads(step_row['step_config']) if step_row['step_config'] else {},
                    'job_id': step_row['job_id'],
                    'status': step_row['status'],
                    'started_at': serialize_datetime(step_row['started_at']),
                    'completed_at': serialize_datetime(step_row['completed_at']),
                    'error_message': step_row['error_message'],
                })

            execution['steps'] = steps

            return jsonify({'execution': execution})

        except Exception as e:
            logger.exception(f"Error fetching execution details {execution_id}")
            return jsonify({'error': str(e)}), 500
