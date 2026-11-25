"""API key management endpoints - available for all users with credit-based usage."""

import logging
from flask import Flask, jsonify, request, session

logger = logging.getLogger(__name__)


def register_api_keys_routes(app: Flask):
    """Register API keys management routes."""

    # Import helpers here to avoid circular imports
    from phonos_app.helpers import login_required
    from phonos_app.helpers.security import csrf_protect
    from phonos_app.helpers.api_keys import (
        create_api_key,
        get_user_api_keys,
        delete_api_key,
        toggle_api_key,
        create_worker_api_key,
        get_user_worker_keys
    )

    @app.route('/api/keys', methods=['GET'])
    @login_required
    def get_api_keys():
        """Get all API keys for the current user."""
        user_id = session['user_id']

        try:
            keys = get_user_api_keys(user_id)
            return jsonify({
                'status': 'success',
                'keys': keys
            })
        except Exception as e:
            logger.error(f"Error getting API keys for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to retrieve API keys'
            }), 500


    @app.route('/api/keys', methods=['POST'])
    @login_required
    @csrf_protect
    def create_new_api_key():
        """Create a new API key."""
        user_id = session['user_id']

        try:
            data = request.get_json()
            name = data.get('name', 'My API Key')

            # Limit to 5 keys per user
            existing_keys = get_user_api_keys(user_id)
            if len(existing_keys) >= 5:
                return jsonify({
                    'status': 'error',
                    'message': 'Maximum of 5 API keys allowed per user'
                }), 400

            key_data = create_api_key(user_id, name)

            logger.info(f"Created API key '{name}' for user {user_id}")

            return jsonify({
                'status': 'success',
                'message': 'API key created successfully',
                'key': key_data
            })

        except Exception as e:
            logger.error(f"Error creating API key for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to create API key'
            }), 500


    @app.route('/api/keys/<int:key_id>', methods=['DELETE'])
    @login_required
    @csrf_protect
    def delete_api_key_endpoint(key_id):
        """Delete an API key."""
        user_id = session['user_id']

        try:
            success = delete_api_key(user_id, key_id)

            if success:
                logger.info(f"Deleted API key {key_id} for user {user_id}")
                return jsonify({
                    'status': 'success',
                    'message': 'API key deleted successfully'
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': 'API key not found or unauthorized'
                }), 404

        except Exception as e:
            logger.error(f"Error deleting API key {key_id} for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to delete API key'
            }), 500


    @app.route('/api/keys/<int:key_id>/toggle', methods=['POST'])
    @login_required
    @csrf_protect
    def toggle_api_key_endpoint(key_id):
        """Enable or disable an API key."""
        user_id = session['user_id']

        try:
            data = request.get_json()
            is_active = data.get('is_active', True)

            success = toggle_api_key(user_id, key_id, is_active)

            if success:
                action = 'enabled' if is_active else 'disabled'
                logger.info(f"{action.capitalize()} API key {key_id} for user {user_id}")
                return jsonify({
                    'status': 'success',
                    'message': f'API key {action} successfully'
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': 'API key not found or unauthorized'
                }), 404

        except Exception as e:
            logger.error(f"Error toggling API key {key_id} for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to toggle API key'
            }), 500


    @app.route('/api/keys/stats', methods=['GET'])
    @login_required
    def get_api_usage_stats():
        """Get API usage statistics for the current user."""
        user_id = session['user_id']

        try:
            from phonos_app.helpers.database import get_db
            db = get_db()

            # Get usage stats for last 30 days
            result = db.execute("""
                SELECT
                    COUNT(*) as total_requests,
                    SUM(credits_used) as total_credits,
                    AVG(response_time_ms) as avg_response_time
                FROM api_usage
                WHERE user_id = %s
                AND timestamp > CURRENT_TIMESTAMP - INTERVAL '30 days'
            """, (user_id,))

            stats = result.fetchone()

            # Get usage by endpoint
            result = db.execute("""
                SELECT endpoint, COUNT(*) as count
                FROM api_usage
                WHERE user_id = %s
                AND timestamp > CURRENT_TIMESTAMP - INTERVAL '30 days'
                GROUP BY endpoint
                ORDER BY count DESC
                LIMIT 10
            """, (user_id,))

            by_endpoint = [{'endpoint': row['endpoint'], 'count': row['count']} for row in result.fetchall()]

            return jsonify({
                'status': 'success',
                'stats': {
                    'total_requests': stats['total_requests'] or 0,
                    'total_credits': stats['total_credits'] or 0,
                    'avg_response_time': float(stats['avg_response_time']) if stats['avg_response_time'] else 0,
                    'by_endpoint': by_endpoint
                }
            })

        except Exception as e:
            logger.error(f"Error getting API stats for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to retrieve API statistics'
            }), 500


    # ============================================================================
    # Webhooks - MOVED TO api_webhooks_new.py (Enhanced webhook system)
    # ============================================================================
    # All webhook routes have been moved to the enhanced webhook system
    # See: phonos_app/routes/api_webhooks_new.py


    # ============================================================================
    # Worker Keys - Dashboard endpoints
    # ============================================================================

    @app.route('/api/worker-keys', methods=['GET'])
    @login_required
    def get_worker_keys():
        """Get all worker keys for the current user."""
        user_id = session['user_id']

        try:
            keys = get_user_worker_keys(user_id)
            return jsonify({
                'status': 'success',
                'keys': keys
            })
        except Exception as e:
            logger.error(f"Error getting worker keys for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'keys': []
            })


    @app.route('/api/worker-keys', methods=['POST'])
    @login_required
    @csrf_protect
    def create_dashboard_worker_key():
        """Create a new worker key."""
        user_id = session['user_id']

        try:
            data = request.get_json() or {}
            name = data.get('name', 'Worker Key')
            model_type = data.get('model_type')  # Optional: restrict to specific model

            # Limit to 3 worker keys per user
            existing_keys = get_user_worker_keys(user_id)
            if len(existing_keys) >= 3:
                return jsonify({
                    'status': 'error',
                    'message': 'Maximum of 3 worker keys allowed per user'
                }), 400

            key_data = create_worker_api_key(user_id, name, model_type)

            if not key_data:
                return jsonify({
                    'status': 'error',
                    'message': 'Failed to create worker key'
                }), 500

            logger.info(f"Created worker key '{name}' for user {user_id}")

            return jsonify({
                'status': 'success',
                'message': 'Worker key created successfully',
                'key': key_data
            })

        except Exception as e:
            logger.error(f"Error creating worker key for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to create worker key'
            }), 500


    @app.route('/api/worker-keys/<int:key_id>', methods=['DELETE'])
    @login_required
    @csrf_protect
    def delete_dashboard_worker_key(key_id):
        """Delete a worker key."""
        user_id = session['user_id']

        try:
            from phonos_app.helpers.database import get_db
            db = get_db()

            # Verify ownership and key type
            result = db.execute("""
                DELETE FROM api_keys
                WHERE id = %s AND user_id = %s AND key_type = 'worker'
                RETURNING id
            """, (key_id, user_id))

            if not result.fetchone():
                return jsonify({
                    'status': 'error',
                    'message': 'Worker key not found or unauthorized'
                }), 404

            db.commit()
            logger.info(f"Deleted worker key {key_id} for user {user_id}")

            return jsonify({
                'status': 'success',
                'message': 'Worker key deleted successfully'
            })

        except Exception as e:
            logger.error(f"Error deleting worker key {key_id} for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to delete worker key'
            }), 500


    @app.route('/api/worker-keys/<int:key_id>/toggle', methods=['POST'])
    @login_required
    @csrf_protect
    def toggle_dashboard_worker_key(key_id):
        """Enable or disable a worker key."""
        user_id = session['user_id']

        try:
            from phonos_app.helpers.database import get_db
            db = get_db()
            data = request.get_json() or {}
            is_active = data.get('is_active', True)

            result = db.execute("""
                UPDATE api_keys
                SET is_active = %s
                WHERE id = %s AND user_id = %s AND key_type = 'worker'
                RETURNING id
            """, (1 if is_active else 0, key_id, user_id))

            if not result.fetchone():
                return jsonify({
                    'status': 'error',
                    'message': 'Worker key not found or unauthorized'
                }), 404

            db.commit()
            action = 'enabled' if is_active else 'disabled'
            logger.info(f"{action.capitalize()} worker key {key_id} for user {user_id}")

            return jsonify({
                'status': 'success',
                'message': f'Worker key {action} successfully'
            })

        except Exception as e:
            logger.error(f"Error toggling worker key {key_id} for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to toggle worker key'
            }), 500


    # ============================================================================
    # External Job Queue - Dashboard endpoints
    # ============================================================================

    @app.route('/api/external-queue/status', methods=['GET'])
    @login_required
    def get_external_queue_status():
        """Get external job queue status for the current user."""
        user_id = session['user_id']

        try:
            from phonos_app.helpers.database import get_db
            db = get_db()

            # Get queue stats by status
            result = db.execute("""
                SELECT status, COUNT(*) as count
                FROM external_api_job_queue
                WHERE user_id = %s
                GROUP BY status
            """, (user_id,))

            queue_stats = {'pending': 0, 'assigned': 0, 'processing': 0, 'completed': 0, 'failed': 0, 'total': 0}
            for row in result.fetchall():
                queue_stats[row['status']] = row['count']
                queue_stats['total'] += row['count']

            # Get stats by model type
            result = db.execute("""
                SELECT model_type, status, COUNT(*) as count
                FROM external_api_job_queue
                WHERE user_id = %s
                GROUP BY model_type, status
            """, (user_id,))

            by_model = {}
            for row in result.fetchall():
                model = row['model_type']
                if model not in by_model:
                    by_model[model] = {'pending': 0, 'assigned': 0, 'processing': 0, 'completed': 0, 'failed': 0}
                by_model[model][row['status']] = row['count']

            # Get recent jobs
            result = db.execute("""
                SELECT eq.id, eq.job_uuid, eq.model_type, eq.status, eq.progress,
                       eq.progress_message, eq.created_at, eq.completed_at,
                       j.name as job_name
                FROM external_api_job_queue eq
                LEFT JOIN jobs j ON eq.job_id = j.id
                WHERE eq.user_id = %s
                ORDER BY eq.created_at DESC
                LIMIT 20
            """, (user_id,))

            recent_jobs = []
            for row in result.fetchall():
                recent_jobs.append({
                    'id': row['id'],
                    'job_uuid': row['job_uuid'],
                    'model_type': row['model_type'],
                    'job_name': row['job_name'],
                    'status': row['status'],
                    'progress': row['progress'] or 0,
                    'progress_message': row['progress_message'],
                    'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                    'completed_at': row['completed_at'].isoformat() if row['completed_at'] else None
                })

            return jsonify({
                'status': 'success',
                'queue': queue_stats,
                'by_model': by_model,
                'recent_jobs': recent_jobs
            })

        except Exception as e:
            logger.error(f"Error getting external queue status for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'queue': {'pending': 0, 'assigned': 0, 'processing': 0, 'completed': 0, 'failed': 0, 'total': 0},
                'by_model': {},
                'recent_jobs': []
            })

    # =========================================================================
    # PROJECT API KEYS (project-scoped, dual-use encryption) - /api/project/v1
    # =========================================================================
    from phonos_app.helpers.project_api_keys import (
        create_project_api_key,
        list_project_api_keys,
        revoke_project_api_key,
    )
    from phonos_app.helpers.database import get_db as _get_db

    def _user_in_project(user_id, project_id):
        """True if the user is assigned to the project."""
        row = _get_db().execute(
            "SELECT 1 FROM project_assignments WHERE project_id = %s AND user_id = %s",
            (project_id, user_id),
        ).fetchone()
        return row is not None

    @app.route('/api/project-keys', methods=['GET'])
    @login_required
    def list_project_keys():
        """List the current user's project API keys (optionally per project)."""
        user_id = session['user_id']
        project_id = request.args.get('project_id', type=int)
        try:
            keys = list_project_api_keys(user_id, project_id)
            return jsonify({'status': 'success', 'keys': keys})
        except Exception as e:
            logger.error(f"Error listing project keys for user {user_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to retrieve project keys'}), 500

    @app.route('/api/project-keys', methods=['POST'])
    @login_required
    @csrf_protect
    def create_project_key():
        """Create a project-scoped API key (returns the secret once)."""
        user_id = session['user_id']
        data = request.get_json() or {}
        project_id = data.get('project_id')
        name = data.get('name', 'Project API Key')

        if not project_id:
            return jsonify({'status': 'error', 'message': 'project_id is required'}), 400

        try:
            if not _user_in_project(user_id, project_id):
                return jsonify({'status': 'error', 'message': 'You do not have access to this project'}), 403

            # Cap keys per project to keep the surface small.
            existing = list_project_api_keys(user_id, project_id)
            if len([k for k in existing if k['is_active']]) >= 5:
                return jsonify({'status': 'error',
                                'message': 'Maximum of 5 active project keys per project'}), 400

            key_data = create_project_api_key(user_id, project_id, name)
            logger.info(f"Created project API key for user {user_id} project {project_id}")
            return jsonify({
                'status': 'success',
                'message': 'Project API key created. Copy it now - it will not be shown again.',
                'key': key_data,
            })
        except Exception as e:
            logger.error(f"Error creating project key for user {user_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to create project key'}), 500

    @app.route('/api/project-keys/<int:key_id>', methods=['DELETE'])
    @login_required
    @csrf_protect
    def delete_project_key(key_id):
        """Revoke (deactivate) a project API key the user owns."""
        user_id = session['user_id']
        try:
            ok = revoke_project_api_key(user_id, key_id)
            if not ok:
                return jsonify({'status': 'error', 'message': 'Key not found'}), 404
            return jsonify({'status': 'success', 'message': 'Project API key revoked'})
        except Exception as e:
            logger.error(f"Error revoking project key {key_id} for user {user_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to revoke project key'}), 500
