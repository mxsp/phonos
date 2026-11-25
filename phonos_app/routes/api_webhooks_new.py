"""
Enhanced webhook API routes with improved validation, security, and delivery tracking.
"""

import logging
from flask import Flask, jsonify, request, session
from phonos_app.helpers import login_required
from phonos_app.helpers.security import csrf_protect
from phonos_app.helpers.database import get_db
from phonos_app.helpers.webhooks import (
    create_webhook,
    get_user_webhooks,
    delete_webhook,
    update_webhook,
    deliver_webhook,
    get_webhook_deliveries,
    VALID_EVENTS
)

logger = logging.getLogger(__name__)


def register_webhook_routes(app: Flask):
    """Register enhanced webhook routes."""

    @app.route('/api/webhooks', methods=['GET'])
    @login_required
    def list_webhooks():
        """Get all webhooks for the current user."""
        user_id = session['user_id']

        try:
            db = get_db()
            webhooks = get_user_webhooks(db, user_id)

            return jsonify({
                'status': 'success',
                'webhooks': webhooks,
                'limits': {
                    'max_webhooks': 10,
                    'current_count': len(webhooks)
                },
                'available_events': VALID_EVENTS
            })

        except Exception as e:
            logger.error(f"Error listing webhooks for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to retrieve webhooks',
                'webhooks': []
            }), 500


    @app.route('/api/webhooks', methods=['POST'])
    @login_required
    @csrf_protect
    def create_new_webhook():
        """Create a new webhook."""
        user_id = session['user_id']

        try:
            db = get_db()
            data = request.get_json()

            if not data:
                return jsonify({
                    'status': 'error',
                    'message': 'Request body is required',
                    'field': 'body'
                }), 400

            # Extract fields
            url = data.get('url', '').strip()
            events = data.get('events', [])
            description = data.get('description', '').strip() or None
            retry_enabled = data.get('retry_enabled', True)
            max_retries = data.get('max_retries', 3)
            timeout = data.get('timeout_seconds', 10)

            # Create webhook
            success, webhook_data, error = create_webhook(
                db, user_id, url, events, description,
                retry_enabled, max_retries, timeout
            )

            if not success:
                return jsonify({
                    'status': 'error',
                    'message': error,
                    'field': 'url' if 'URL' in error else 'events' if 'event' in error.lower() else None
                }), 400

            logger.info(f"Created webhook {webhook_data['id']} for user {user_id}")

            return jsonify({
                'status': 'success',
                'message': 'Webhook created successfully',
                'webhook': webhook_data
            }), 201

        except Exception as e:
            logger.error(f"Error creating webhook for user {user_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to create webhook'
            }), 500


    @app.route('/api/webhooks/<int:webhook_id>', methods=['GET'])
    @login_required
    def get_webhook_details(webhook_id):
        """Get detailed information about a specific webhook."""
        user_id = session['user_id']

        try:
            db = get_db()

            # Get webhook
            result = db.execute("""
                SELECT id, url, events, description, is_active, created_at,
                       last_triggered_at, success_count, failure_count,
                       retry_enabled, max_retries, timeout_seconds
                FROM api_webhooks
                WHERE id = %s AND user_id = %s
            """, (webhook_id, user_id))

            webhook = result.fetchone()
            if not webhook:
                return jsonify({
                    'status': 'error',
                    'message': 'Webhook not found'
                }), 404

            # Get recent deliveries
            deliveries = get_webhook_deliveries(db, webhook_id, limit=20)

            webhook_data = {
                'id': webhook['id'],
                'url': webhook['url'],
                'events': webhook['events'] if isinstance(webhook['events'], list) else [],
                'description': webhook['description'],
                'is_active': bool(webhook['is_active']),
                'retry_enabled': bool(webhook['retry_enabled']),
                'max_retries': webhook['max_retries'],
                'timeout_seconds': webhook['timeout_seconds'],
                'created_at': webhook['created_at'].isoformat() if webhook['created_at'] else None,
                'last_triggered_at': webhook['last_triggered_at'].isoformat() if webhook['last_triggered_at'] else None,
                'stats': {
                    'success_count': webhook['success_count'] or 0,
                    'failure_count': webhook['failure_count'] or 0,
                    'total_deliveries': (webhook['success_count'] or 0) + (webhook['failure_count'] or 0)
                },
                'recent_deliveries': deliveries
            }

            return jsonify({
                'status': 'success',
                'webhook': webhook_data
            })

        except Exception as e:
            logger.error(f"Error getting webhook {webhook_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to retrieve webhook details'
            }), 500


    @app.route('/api/webhooks/<int:webhook_id>', methods=['PATCH'])
    @login_required
    @csrf_protect
    def update_webhook_settings(webhook_id):
        """Update webhook settings."""
        user_id = session['user_id']

        try:
            db = get_db()
            data = request.get_json()

            if not data:
                return jsonify({
                    'status': 'error',
                    'message': 'Request body is required'
                }), 400

            # Update webhook
            success, error = update_webhook(db, user_id, webhook_id, **data)

            if not success:
                return jsonify({
                    'status': 'error',
                    'message': error
                }), 400 if 'not found' not in error.lower() else 404

            logger.info(f"Updated webhook {webhook_id} for user {user_id}")

            return jsonify({
                'status': 'success',
                'message': 'Webhook updated successfully'
            })

        except Exception as e:
            logger.error(f"Error updating webhook {webhook_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to update webhook'
            }), 500


    @app.route('/api/webhooks/<int:webhook_id>', methods=['DELETE'])
    @login_required
    @csrf_protect
    def delete_webhook_endpoint(webhook_id):
        """Delete a webhook."""
        user_id = session['user_id']

        try:
            db = get_db()

            success, error = delete_webhook(db, user_id, webhook_id)

            if not success:
                status_code = 404 if 'not found' in error.lower() else 500
                return jsonify({
                    'status': 'error',
                    'message': error
                }), status_code

            return jsonify({
                'status': 'success',
                'message': 'Webhook deleted successfully'
            })

        except Exception as e:
            logger.error(f"Error deleting webhook {webhook_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to delete webhook'
            }), 500


    @app.route('/api/webhooks/<int:webhook_id>/test', methods=['POST'])
    @login_required
    @csrf_protect
    def test_webhook_endpoint(webhook_id):
        """Send a test event to a webhook."""
        user_id = session['user_id']

        try:
            db = get_db()

            # Verify ownership
            result = db.execute("""
                SELECT id, url FROM api_webhooks
                WHERE id = %s AND user_id = %s
            """, (webhook_id, user_id))

            webhook = result.fetchone()
            if not webhook:
                return jsonify({
                    'status': 'error',
                    'message': 'Webhook not found'
                }), 404

            # Test payload
            test_payload = {
                'message': 'This is a test webhook delivery from PHONOS',
                'webhook_id': webhook_id,
                'test': True
            }

            # Deliver test webhook
            success, error = deliver_webhook(db, webhook_id, 'test', test_payload)

            if success:
                # Get the latest delivery log
                deliveries = get_webhook_deliveries(db, webhook_id, limit=1)
                latest = deliveries[0] if deliveries else None

                return jsonify({
                    'status': 'success',
                    'message': 'Test webhook delivered successfully',
                    'delivery': latest
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': f'Webhook test failed: {error}',
                    'error': error
                }), 500

        except Exception as e:
            logger.error(f"Error testing webhook {webhook_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to test webhook'
            }), 500


    @app.route('/api/webhooks/<int:webhook_id>/toggle', methods=['POST'])
    @login_required
    @csrf_protect
    def toggle_webhook_endpoint(webhook_id):
        """Enable or disable a webhook."""
        user_id = session['user_id']

        try:
            db = get_db()
            data = request.get_json() or {}
            is_active = data.get('is_active', True)

            success, error = update_webhook(db, user_id, webhook_id, is_active=is_active)

            if not success:
                status_code = 404 if 'not found' in error.lower() else 500
                return jsonify({
                    'status': 'error',
                    'message': error
                }), status_code

            action = 'enabled' if is_active else 'disabled'
            logger.info(f"{action.capitalize()} webhook {webhook_id} for user {user_id}")

            return jsonify({
                'status': 'success',
                'message': f'Webhook {action} successfully'
            })

        except Exception as e:
            logger.error(f"Error toggling webhook {webhook_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to toggle webhook'
            }), 500


    @app.route('/api/webhooks/<int:webhook_id>/deliveries', methods=['GET'])
    @login_required
    def get_webhook_delivery_history(webhook_id):
        """Get delivery history for a webhook."""
        user_id = session['user_id']

        try:
            db = get_db()

            # Verify ownership
            result = db.execute("""
                SELECT id FROM api_webhooks
                WHERE id = %s AND user_id = %s
            """, (webhook_id, user_id))

            if not result.fetchone():
                return jsonify({
                    'status': 'error',
                    'message': 'Webhook not found'
                }), 404

            # Get delivery history
            limit = request.args.get('limit', 50, type=int)
            limit = min(limit, 100)  # Max 100

            deliveries = get_webhook_deliveries(db, webhook_id, limit=limit)

            return jsonify({
                'status': 'success',
                'deliveries': deliveries,
                'count': len(deliveries)
            })

        except Exception as e:
            logger.error(f"Error getting deliveries for webhook {webhook_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to retrieve delivery history'
            }), 500


    @app.route('/api/webhooks/events', methods=['GET'])
    @login_required
    def get_available_events():
        """Get list of available webhook events."""
        return jsonify({
            'status': 'success',
            'events': [
                {
                    'type': 'job.completed',
                    'description': 'Triggered when a job completes successfully'
                },
                {
                    'type': 'job.failed',
                    'description': 'Triggered when a job fails'
                },
                {
                    'type': 'job.started',
                    'description': 'Triggered when a job starts processing'
                },
                {
                    'type': 'job.progress',
                    'description': 'Triggered when job progress is updated (may be frequent)'
                }
            ]
        })
