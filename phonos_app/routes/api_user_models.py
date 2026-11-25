"""API routes for user-defined models management."""

import os
from flask import abort, jsonify, request, session, Flask
from config import Config
import logging

# Absolute imports to avoid relative import issues with dynamic loading
from phonos_app.helpers import login_required
from phonos_app.helpers.security import csrf_protect, rate_limit
from phonos_app.helpers.projects import get_project_assignment
from phonos_app.helpers.user_models import (
    CUSTOM_MODEL_CREDIT_COST,
    create_user_model,
    get_user_models,
    get_user_model,
    get_user_model_by_model_id,
    update_user_model,
    delete_user_model,
    create_model_api_key,
    get_model_api_keys,
    verify_model_api_key,
    get_decrypted_api_key
)

logger = logging.getLogger(__name__)


def _coerce_bool(value, default: bool = False) -> bool:
    """Normalize potentially-string boolean payload values."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def register_user_model_routes(app: Flask):
    """Register all user model routes."""

    @app.route('/api/user-models', methods=['GET'])
    @login_required
    def list_user_models():
        """List all user-defined models for the current user."""
        user_id = session['user_id']
        project_id = request.args.get('project_id', type=int)
        include_public = request.args.get('include_public', 'false').lower() == 'true'

        try:
            models = get_user_models(user_id, project_id=project_id, include_public=include_public)
            return jsonify({
                'status': 'success',
                'models': models
            })
        except Exception as e:
            logger.error(f"Error fetching user models: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to fetch models'
            }), 500


    @app.route('/api/user-models/<int:db_id>', methods=['GET'])
    @login_required
    def get_user_model_detail(db_id):
        """Get details of a specific user model by database ID."""
        user_id = session['user_id']

        try:
            model = get_user_model(db_id, user_id=user_id)
            if not model:
                return jsonify({
                    'status': 'error',
                    'message': 'Model not found or access denied'
                }), 404

            return jsonify({
                'status': 'success',
                'model': model
            })
        except Exception as e:
            logger.error(f"Error fetching user model {db_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to fetch model'
            }), 500


    @app.route('/api/user-models/by-id/<model_id>', methods=['GET'])
    @login_required
    def get_user_model_by_unique_id(model_id):
        """Get details of a specific user model by unique 16-char model_id."""
        user_id = session['user_id']

        try:
            # Validate model_id format (16 alphanumeric characters)
            if not model_id or len(model_id) != 16 or not model_id.isalnum():
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid model ID format. Must be 16 alphanumeric characters.'
                }), 400

            model = get_user_model_by_model_id(model_id, user_id=user_id)
            if not model:
                return jsonify({
                    'status': 'error',
                    'message': 'Model not found or access denied'
                }), 404

            return jsonify({
                'status': 'success',
                'model': model
            })
        except Exception as e:
            logger.error(f"Error fetching user model by model_id {model_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to fetch model'
            }), 500


    @app.route('/api/user-models', methods=['POST'])
    @login_required
    @csrf_protect
    @rate_limit(limit=10, window=3600, action='create_user_model')
    def create_user_model_endpoint():
        """Create a new user-defined model."""
        user_id = session['user_id']
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'No data provided'
            }), 400

        # Validate required fields
        required_fields = ['model_name', 'model_description', 'input_schema']
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({
                'status': 'error',
                'message': f'Missing required fields: {", ".join(missing_fields)}'
            }), 400

        # Validate input_schema
        input_schema = data.get('input_schema', [])
        if not isinstance(input_schema, list):
            return jsonify({
                'status': 'error',
                'message': 'input_schema must be an array'
            }), 400

        try:
            model = create_user_model(
                user_id=user_id,
                model_name=data['model_name'],
                model_description=data['model_description'],
                input_schema=input_schema,
                project_id=data.get('project_id'),
                model_type=data.get('model_type', 'custom'),
                output_format=data.get('output_format', 'json'),
                outline_color=data.get('outline_color', 'gray'),
                credit_cost=CUSTOM_MODEL_CREDIT_COST,
                estimated_runtime_seconds=data.get('estimated_runtime_seconds', 60),
                worker_endpoint_url=data.get('worker_endpoint_url'),
                is_public=data.get('is_public', False),
                endpoint_mode=data.get('endpoint_mode', 'direct_http' if data.get('worker_endpoint_url') else 'webhook_bridge'),
                auth_type=data.get('auth_type', 'none'),
                auth_header_name=data.get('auth_header_name', 'Authorization'),
                upstream_api_key=data.get('upstream_api_key'),
                custom_headers=data.get('custom_headers'),
                request_template_json=data.get('request_template_json', ''),
                response_output_path=data.get('response_output_path', ''),
                upstream_model_name=data.get('upstream_model_name', ''),
            )

            return jsonify({
                'status': 'success',
                'message': 'Model created successfully',
                'model': model
            }), 201
        except ValueError as e:
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 400
        except Exception as e:
            logger.error(f"Error creating user model: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to create model'
            }), 500


    @app.route('/api/user-models/<int:model_id>', methods=['PUT'])
    @login_required
    @csrf_protect
    def update_user_model_endpoint(model_id):
        """Update a user-defined model."""
        user_id = session['user_id']
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'No data provided'
            }), 400

        try:
            success = update_user_model(model_id, user_id, **data)
            if not success:
                return jsonify({
                    'status': 'error',
                    'message': 'Model not found or no changes made'
                }), 404

            return jsonify({
                'status': 'success',
                'message': 'Model updated successfully'
            })
        except Exception as e:
            logger.error(f"Error updating user model {model_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to update model'
            }), 500


    @app.route('/api/user-models/<int:model_id>', methods=['DELETE'])
    @login_required
    @csrf_protect
    def delete_user_model_endpoint(model_id):
        """Delete a user-defined model."""
        user_id = session['user_id']

        try:
            success = delete_user_model(model_id, user_id)
            if not success:
                return jsonify({
                    'status': 'error',
                    'message': 'Model not found or access denied'
                }), 404

            return jsonify({
                'status': 'success',
                'message': 'Model deleted successfully'
            })
        except Exception as e:
            logger.error(f"Error deleting user model {model_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to delete model'
            }), 500


    # ============================================================================
    # API Keys for User Models
    # ============================================================================

    @app.route('/api/worker-api-keys', methods=['GET'])
    @login_required
    def list_all_worker_api_keys():
        """List all worker API keys for the current user (across all models)."""
        user_id = session['user_id']

        try:
            from phonos_app.helpers.database import get_db
            db = get_db()

            # Fetch all API keys for this user with model info.
            try:
                keys = db.execute("""
                    SELECT
                        k.id, k.name, k.key_prefix, k.description, k.is_active,
                        k.rate_limit_per_minute, k.total_requests, k.last_used_at, k.created_at,
                        k.user_model_id, k.project_id, k.encryption_enabled,
                        um.model_name as user_model_name,
                        p.name as project_name
                    FROM user_model_api_keys k
                    LEFT JOIN user_models um ON k.user_model_id = um.id
                    LEFT JOIN projects p ON k.project_id = p.id
                    WHERE k.user_id = %s
                    ORDER BY k.created_at DESC
                """, (user_id,)).fetchall()
            except Exception:
                # Backwards compatibility for older schemas without encryption_enabled.
                keys = db.execute("""
                    SELECT
                        k.id, k.name, k.key_prefix, k.description, k.is_active,
                        k.rate_limit_per_minute, k.total_requests, k.last_used_at, k.created_at,
                        k.user_model_id, k.project_id,
                        um.model_name as user_model_name,
                        p.name as project_name
                    FROM user_model_api_keys k
                    LEFT JOIN user_models um ON k.user_model_id = um.id
                    LEFT JOIN projects p ON k.project_id = p.id
                    WHERE k.user_id = %s
                    ORDER BY k.created_at DESC
                """, (user_id,)).fetchall()

            return jsonify({
                'status': 'success',
                'api_keys': [dict(k) for k in keys]
            })
        except Exception as e:
            logger.error(f"Error fetching worker API keys: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to fetch API keys'
            }), 500


    @app.route('/api/worker-api-keys/<int:key_id>', methods=['DELETE'])
    @login_required
    @csrf_protect
    def delete_worker_api_key(key_id):
        """Delete a worker API key."""
        user_id = session['user_id']

        try:
            from phonos_app.helpers.database import get_db
            db = get_db()

            # Verify the key belongs to this user
            key = db.execute("""
                SELECT id FROM user_model_api_keys WHERE id = %s AND user_id = %s
            """, (key_id, user_id)).fetchone()

            if not key:
                return jsonify({
                    'status': 'error',
                    'message': 'API key not found'
                }), 404

            db.execute("DELETE FROM user_model_api_keys WHERE id = %s", (key_id,))
            db.commit()

            return jsonify({
                'status': 'success',
                'message': 'API key deleted'
            })
        except Exception as e:
            logger.error(f"Error deleting worker API key: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to delete API key'
            }), 500


    @app.route('/api/worker-api-keys/<int:key_id>/toggle', methods=['POST'])
    @login_required
    @csrf_protect
    def toggle_worker_api_key(key_id):
        """Toggle a worker API key's active status."""
        user_id = session['user_id']

        try:
            from phonos_app.helpers.database import get_db
            db = get_db()

            # Verify the key belongs to this user
            key = db.execute("""
                SELECT id, is_active FROM user_model_api_keys WHERE id = %s AND user_id = %s
            """, (key_id, user_id)).fetchone()

            if not key:
                return jsonify({
                    'status': 'error',
                    'message': 'API key not found'
                }), 404

            new_status = not key['is_active']
            db.execute("""
                UPDATE user_model_api_keys SET is_active = %s WHERE id = %s
            """, (new_status, key_id))
            db.commit()

            return jsonify({
                'status': 'success',
                'message': f'API key {"activated" if new_status else "deactivated"}',
                'is_active': new_status
            })
        except Exception as e:
            logger.error(f"Error toggling worker API key: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to toggle API key'
            }), 500


    @app.route('/api/worker-api-keys/<int:key_id>/view', methods=['GET'])
    @login_required
    def view_worker_api_key(key_id):
        """Legacy endpoint for viewing full worker keys (disabled by default)."""
        user_id = session['user_id']
        allow_legacy_reveal = os.environ.get('ALLOW_LEGACY_WORKER_KEY_REVEAL', 'false').lower() == 'true'

        if not allow_legacy_reveal:
            return jsonify({
                'status': 'error',
                'message': 'Worker keys are only shown once at creation for security hardening.'
            }), 410

        try:
            full_key = get_decrypted_api_key(key_id, user_id)

            if not full_key:
                return jsonify({
                    'status': 'error',
                    'message': 'API key not found or cannot be decrypted. Keys created before this feature was added cannot be viewed.'
                }), 404

            return jsonify({
                'status': 'success',
                'api_key': full_key
            })
        except Exception as e:
            logger.error(f"Error viewing API key {key_id}: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to retrieve API key'
            }), 500


    @app.route('/api/user-models/<int:model_id>/api-keys', methods=['GET'])
    @login_required
    def list_model_api_keys(model_id):
        """List all API keys for a user model."""
        user_id = session['user_id']

        try:
            keys = get_model_api_keys(user_id, user_model_id=model_id)
            return jsonify({
                'status': 'success',
                'api_keys': keys
            })
        except Exception as e:
            logger.error(f"Error fetching model API keys: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to fetch API keys'
            }), 500


    @app.route('/api/user-models/<int:model_id>/api-keys', methods=['POST'])
    @login_required
    @csrf_protect
    @rate_limit(limit=5, window=3600, action='create_model_api_key')
    def create_model_api_key_endpoint(model_id):
        """Create a new API key for a user model."""
        user_id = session['user_id']
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'API key name is required'
            }), 400

        key_name = str(data.get('name') or '').strip()
        if not key_name:
            return jsonify({
                'status': 'error',
                'message': 'API key name is required'
            }), 400

        try:
            # Verify user owns the model
            model = get_user_model(model_id, user_id=user_id)
            if not model or model['user_id'] != user_id:
                return jsonify({
                    'status': 'error',
                    'message': 'Model not found or access denied'
                }), 404

            try:
                rate_limit = int(data.get('rate_limit_per_minute', 60))
            except (TypeError, ValueError):
                rate_limit = 60
            rate_limit = max(1, min(rate_limit, 1000))

            api_key_data = create_model_api_key(
                user_id=user_id,
                name=key_name,
                user_model_id=model_id,
                rate_limit_per_minute=rate_limit,
                encryption_enabled=_coerce_bool(data.get('encryption_enabled'), True),
                description=data.get('description')
            )

            return jsonify({
                'status': 'success',
                'message': 'API key created successfully. Save this key now: it is shown only once.',
                'api_key': api_key_data
            }), 201
        except ValueError as e:
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 400
        except Exception as e:
            logger.error(f"Error creating model API key: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to create API key'
            }), 500


    @app.route('/api/projects/<int:project_id>/api-keys', methods=['POST'])
    @login_required
    @csrf_protect
    @rate_limit(limit=5, window=3600, action='create_project_api_key')
    def create_project_api_key_endpoint(project_id):
        """Create a new API key for a project (for all models in that project)."""
        user_id = session['user_id']
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'API key name is required'
            }), 400

        key_name = str(data.get('name') or '').strip()
        if not key_name:
            return jsonify({
                'status': 'error',
                'message': 'API key name is required'
            }), 400

        try:
            # Verify user has access to the project before allowing project-scoped key creation
            from phonos_app.helpers.database import get_db
            role = get_project_assignment(get_db(), user_id, project_id)
            if not role:
                return jsonify({
                    'status': 'error',
                    'message': 'Access denied to project'
                }), 403

            try:
                rate_limit = int(data.get('rate_limit_per_minute', 60))
            except (TypeError, ValueError):
                rate_limit = 60
            rate_limit = max(1, min(rate_limit, 1000))

            api_key_data = create_model_api_key(
                user_id=user_id,
                name=key_name,
                project_id=project_id,
                rate_limit_per_minute=rate_limit,
                encryption_enabled=_coerce_bool(data.get('encryption_enabled'), True),
                description=data.get('description')
            )

            return jsonify({
                'status': 'success',
                'message': 'Project API key created successfully. Save this key now: it is shown only once.',
                'api_key': api_key_data
            }), 201
        except ValueError as e:
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 400
        except Exception as e:
            logger.error(f"Error creating project API key: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to create API key'
            }), 500


    @app.route('/api/jobs/submit', methods=['POST'])
    @app.route('/api/user-models/submit-job', methods=['POST'])
    @login_required
    @csrf_protect
    def submit_user_model_job():
        """Submit a job to a user-defined model."""
        user_id = session['user_id']
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'No data provided'
            }), 400

        model_id = data.get('model_id')
        job_name = data.get('job_name', '').strip()
        input_data = data.get('input', {})

        if not model_id:
            return jsonify({
                'status': 'error',
                'message': 'Model ID is required'
            }), 400

        if not job_name:
            return jsonify({
                'status': 'error',
                'message': 'Job name is required'
            }), 400

        try:
            import uuid
            import json
            from datetime import datetime
            from phonos_app.helpers.database import get_db

            # Import encryption for secure job payloads
            try:
                from encryption_util import encrypt
            except ImportError:
                encrypt = None

            db = get_db()
            from phonos_app.helpers.custom_endpoint_runner import (
                ensure_byom_schema,
                execute_custom_model_job_async,
            )
            ensure_byom_schema(db)

            # Get user model details (including BYOM endpoint configuration)
            model = db.execute("""
                SELECT DISTINCT um.id, um.model_id, um.user_id, um.model_name, um.credit_cost,
                       um.input_schema, um.output_format, um.estimated_runtime_seconds,
                       um.is_active, um.worker_endpoint_url, um.project_id,
                       um.endpoint_mode, um.auth_type, um.auth_header_name,
                       um.upstream_api_key_enc, um.custom_headers_json,
                       um.request_template_json, um.response_output_path, um.upstream_model_name
                FROM user_models um
                LEFT JOIN projects p ON um.project_id = p.id
                LEFT JOIN project_assignments pa ON um.project_id = pa.project_id AND pa.user_id = %s
                WHERE um.id = %s AND (
                    um.is_public = true
                    OR um.user_id = %s
                    OR (um.project_id IS NOT NULL AND pa.user_id IS NOT NULL)
                )
            """, (user_id, model_id, user_id)).fetchone()

            if not model:
                return jsonify({
                    'status': 'error',
                    'message': 'Model not found or access denied'
                }), 404

            model_dict = dict(model) if isinstance(model, dict) or hasattr(model, 'keys') else {}
            if not model_dict.get('is_active'):
                return jsonify({
                    'status': 'error',
                    'message': 'Model is currently inactive'
                }), 400

            # Check user credits (unlocked in LOCAL_LAB_MODE when credits == 0)
            user_credits = db.execute('SELECT credits FROM users WHERE id = %s', (user_id,)).fetchone()
            avail_credits = int((user_credits['credits'] if user_credits else 0) or 0)
            req_credits = int(model_dict.get('credit_cost') or 0)
            if not getattr(Config, 'LOCAL_LAB_MODE', True) and avail_credits < req_credits:
                return jsonify({
                    'status': 'error',
                    'message': f'Insufficient credits. Required: {req_credits}, Available: {avail_credits}'
                }), 402

            # Validate input data against schema
            raw_schema = model_dict.get('input_schema')
            input_schema = raw_schema if isinstance(raw_schema, list) else json.loads(raw_schema or '[]')
            for field in input_schema:
                if field.get('required') and field['name'] not in input_data:
                    return jsonify({
                        'status': 'error',
                        'message': f'Missing required field: {field.get("label") or field["name"]}'
                    }), 400

            # Generate job ID
            job_id = str(uuid.uuid4())

            # Get project_id from model or request (fallback to user's first project if omitted)
            project_id = model_dict.get('project_id') or data.get('project_id')
            if not project_id:
                first_proj = db.execute(
                    'SELECT project_id FROM project_assignments WHERE user_id = %s ORDER BY project_id ASC LIMIT 1',
                    (user_id,),
                ).fetchone()
                if first_proj:
                    project_id = first_proj['project_id'] if isinstance(first_proj, dict) or hasattr(first_proj, 'keys') else first_proj[0]
            if not project_id:
                return jsonify({
                    'status': 'error',
                    'message': 'Project ID is required. Please create or select a project first.'
                }), 400

            if avail_credits >= req_credits and req_credits > 0:
                db.execute('UPDATE users SET credits = credits - %s WHERE id = %s', (req_credits, user_id))

            # Create job record (compatible with both PostgreSQL RETURNING id and SQLite)
            cursor = db.execute("""
                INSERT INTO jobs (job_id, project_id, creator_id, name, model, status, job_data, created_at)
                VALUES (%s, %s, %s, %s, %s, 'queued', %s, CURRENT_TIMESTAMP)
                RETURNING id
            """, (job_id, project_id, user_id, job_name, f'user-model-{model_id}', None))

            fetched_job = cursor.fetchone()
            job_db_id = (fetched_job['id'] if isinstance(fetched_job, dict) or hasattr(fetched_job, 'keys') else fetched_job[0]) if fetched_job else cursor.lastrowid
            if not job_db_id:
                raise Exception("Failed to retrieve job ID after insertion")

            # Prepare job payload
            job_payload = {
                'job_id': job_id,
                'model_id': model_id,
                'model_name': model_dict['model_name'],
                'input_data': input_data,
                'created_at': datetime.utcnow().isoformat()
            }

            if encrypt:
                job_data_json = json.dumps(job_payload)
                encrypted_data = encrypt(job_data_json.encode('utf-8'))
            else:
                encrypted_data = json.dumps(job_payload).encode('utf-8')

            db.execute('UPDATE jobs SET job_data = %s WHERE job_id = %s', (encrypted_data, job_id))

            db.execute("""
                INSERT INTO external_job_queue (job_id, user_model_id, status, created_at)
                VALUES (%s, %s, 'pending', CURRENT_TIMESTAMP)
            """, (job_db_id, model_id))

            db.commit()

            logger.info(f"User model job {job_id} submitted for model {model_id} by user {user_id}")

            endpoint_url = (model_dict.get('worker_endpoint_url') or '').strip()
            endpoint_mode = (model_dict.get('endpoint_mode') or ('direct_http' if endpoint_url else 'webhook_bridge')).strip().lower()

            if endpoint_url and endpoint_mode in ('direct_http', 'nvidia_nim', 'openai_compat', 'huggingface'):
                try:
                    from phonos_app.app import socketio as _sio
                except Exception:
                    _sio = None
                execute_custom_model_job_async(
                    app=app,
                    socketio=_sio,
                    job_id=job_id,
                    job_db_id=int(job_db_id),
                    project_id=int(project_id),
                    user_id=int(user_id),
                    model_row=model_dict,
                    input_data=input_data,
                )
            elif endpoint_url:
                # webhook_bridge pull/callback notification
                try:
                    import requests
                    import threading

                    def send_webhook():
                        try:
                            webhook_payload = {
                                'event': 'job.created',
                                'job_id': job_id,
                                'model_id': model_id,
                                'model_name': model_dict['model_name'],
                                'input_data': input_data,
                                'created_at': datetime.utcnow().isoformat(),
                                'callback_url': f'/api/bridge/v1/jobs/{job_id}/result'
                            }
                            requests.post(
                                endpoint_url,
                                json=webhook_payload,
                                headers={'Content-Type': 'application/json'},
                                timeout=10
                            )
                        except Exception as webhook_error:
                            logger.warning(f"Failed to send webhook for job {job_id}: {webhook_error}")

                    webhook_thread = threading.Thread(target=send_webhook, daemon=True)
                    webhook_thread.start()
                except Exception as e:
                    logger.warning(f"Could not initiate webhook for job {job_id}: {e}")

            return jsonify({
                'status': 'success',
                'message': 'Job submitted successfully',
                'job_id': job_id,
                'endpoint_mode': endpoint_mode,
                'credits_used': req_credits if avail_credits >= req_credits else 0
            }), 201

        except Exception as e:
            logger.error(f"Error submitting user model job: {e}", exc_info=True)
            db.rollback()
            return jsonify({
                'status': 'error',
                'message': 'Failed to submit job'
            }), 500


    @app.route('/api/user-model-jobs/<job_id>/result', methods=['GET'])
    @login_required
    def get_user_model_job_result(job_id):
        """Get the result of a user model job."""
        user_id = session['user_id']

        try:
            import json
            from phonos_app.helpers.database import get_db

            # Import decryption for secure job payloads
            try:
                from encryption_util import decrypt
            except ImportError:
                decrypt = None

            db = get_db()

            # Get job details with user model info
            job = db.execute("""
                SELECT j.id, j.job_id, j.name, j.model, j.status, j.message, j.metadata,
                       j.output_file, j.execution_time, j.created_at, j.updated_at, j.job_data,
                       eq.user_model_id, eq.status as queue_status, eq.completed_at,
                       um.model_name, um.model_description, um.output_format, um.credit_cost
                FROM jobs j
                LEFT JOIN external_job_queue eq ON j.id = eq.job_id
                LEFT JOIN user_models um ON eq.user_model_id = um.id
                WHERE j.job_id = %s AND j.creator_id = %s
            """, (job_id, user_id)).fetchone()

            if not job:
                return jsonify({
                    'status': 'error',
                    'message': 'Job not found'
                }), 404

            # Parse result from message or metadata
            result_data = None
            input_data = None

            # Try to parse message as JSON result
            if job['message']:
                try:
                    result_data = json.loads(job['message']) if isinstance(job['message'], str) else job['message']
                except Exception:
                    result_data = {'raw_output': job['message']}

            # Try to parse metadata
            metadata = {}
            if job['metadata']:
                try:
                    metadata = json.loads(job['metadata']) if isinstance(job['metadata'], str) else job['metadata']
                except Exception:
                    pass

            # Try to decrypt job_data to get input
            if job['job_data'] and decrypt:
                try:
                    if isinstance(job['job_data'], str):
                        data_bytes = job['job_data'].encode('utf-8')
                    elif isinstance(job['job_data'], (bytes, memoryview)):
                        data_bytes = bytes(job['job_data'])
                    else:
                        data_bytes = job['job_data']

                    decrypted = decrypt(data_bytes)
                    if decrypted:
                        job_payload = json.loads(decrypted.decode('utf-8'))
                        input_data = job_payload.get('input_data', {})
                except Exception as e:
                    logger.debug(f"Could not decrypt job_data: {e}")
                    # Try unencrypted
                    try:
                        if isinstance(job['job_data'], (bytes, memoryview)):
                            job_payload = json.loads(bytes(job['job_data']).decode('utf-8'))
                        else:
                            job_payload = json.loads(job['job_data'])
                        input_data = job_payload.get('input_data', {})
                    except Exception:
                        pass

            def to_iso_timestamp(value):
                if value is None:
                    return None
                if hasattr(value, 'isoformat'):
                    return value.isoformat()
                return str(value)

            return jsonify({
                'status': 'success',
                'job': {
                    'job_id': job['job_id'],
                    'name': job['name'],
                    'model': job['model'],
                    'job_status': job['status'],
                    'queue_status': job['queue_status'],
                    'execution_time': job['execution_time'],
                    'created_at': to_iso_timestamp(job['created_at']),
                    'completed_at': to_iso_timestamp(job['completed_at']),
                    'output_file': job['output_file'],
                    'user_model': {
                        'id': job['user_model_id'],
                        'name': job['model_name'],
                        'description': job['model_description'],
                        'output_format': job['output_format'],
                        'credit_cost': job['credit_cost']
                    } if job['user_model_id'] else None,
                    'input_data': input_data,
                    'result': result_data,
                    'metadata': metadata
                }
            })

        except Exception as e:
            logger.error(f"Error getting user model job result: {e}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': 'Failed to get job result'
            }), 500


    @app.route('/view_user_model_output/<job_id>')
    @login_required
    def view_user_model_output(job_id):
        """View output page for user model jobs."""
        user_id = session['user_id']

        try:
            import json
            from flask import render_template
            from phonos_app.helpers.database import get_db

            # Import decryption for secure job payloads
            try:
                from encryption_util import decrypt
            except ImportError:
                decrypt = None

            db = get_db()

            # Get job details with user model info and project
            job = db.execute("""
                SELECT j.id, j.job_id, j.name, j.model, j.status, j.message, j.metadata,
                       j.output_file, j.execution_time, j.created_at, j.updated_at, j.job_data,
                       j.project_id,
                       eq.user_model_id, eq.status as queue_status, eq.completed_at,
                       um.model_name, um.model_description, um.output_format, um.credit_cost,
                       p.name as project_name
                FROM jobs j
                LEFT JOIN external_job_queue eq ON j.id = eq.job_id
                LEFT JOIN user_models um ON eq.user_model_id = um.id
                LEFT JOIN projects p ON j.project_id = p.id
                WHERE j.job_id = %s AND j.creator_id = %s
            """, (job_id, user_id)).fetchone()

            if not job:
                return "Job not found", 404

            # Parse result from message
            result_data = None
            if job['message']:
                try:
                    result_data = json.loads(job['message']) if isinstance(job['message'], str) else job['message']
                except Exception:
                    result_data = {'raw_output': job['message']}

            # Parse metadata
            metadata = {}
            if job['metadata']:
                try:
                    metadata = json.loads(job['metadata']) if isinstance(job['metadata'], str) else job['metadata']
                except Exception:
                    pass

            # Try to decrypt job_data to get input
            input_data = {}
            if job['job_data'] and decrypt:
                try:
                    if isinstance(job['job_data'], str):
                        data_bytes = job['job_data'].encode('utf-8')
                    elif isinstance(job['job_data'], (bytes, memoryview)):
                        data_bytes = bytes(job['job_data'])
                    else:
                        data_bytes = job['job_data']

                    decrypted = decrypt(data_bytes)
                    if decrypted:
                        job_payload = json.loads(decrypted.decode('utf-8'))
                        input_data = job_payload.get('input_data', {})
                except Exception as e:
                    logger.debug(f"Could not decrypt job_data: {e}")
                    try:
                        if isinstance(job['job_data'], (bytes, memoryview)):
                            job_payload = json.loads(bytes(job['job_data']).decode('utf-8'))
                        else:
                            job_payload = json.loads(job['job_data'])
                        input_data = job_payload.get('input_data', {})
                    except Exception:
                        pass

            # Detect content format from result
            detected_format = None
            detected_content = None

            if result_data:
                # Priority fields to check
                priority_fields = ['pdb_text', 'pdb_content', 'pdb', 'structure', 'fasta', 'fasta_text',
                                   'sequence', 'smiles', 'sdf', 'mol', 'output', 'result', 'data', 'content']

                for field in priority_fields:
                    if field in result_data and isinstance(result_data[field], str) and len(result_data[field]) > 10:
                        content = result_data[field].strip()
                        # Detect format
                        if content.startswith('HEADER') or content.startswith('ATOM') or 'ATOM  ' in content[:500]:
                            detected_format = 'pdb'
                            detected_content = content
                            break
                        elif content.startswith('data_') or '_atom_site.' in content[:500]:
                            detected_format = 'cif'
                            detected_content = content
                            break
                        elif content.startswith('>'):
                            detected_format = 'fasta'
                            detected_content = content
                            break
                        elif 'V2000' in content or 'V3000' in content or 'M  END' in content:
                            detected_format = 'sdf'
                            detected_content = content
                            break

                # If not found in priority fields, scan all string fields
                if not detected_content:
                    for key, value in result_data.items():
                        if isinstance(value, str) and len(value) > 100:
                            content = value.strip()
                            if 'ATOM  ' in content[:1000] or content.startswith('HEADER'):
                                detected_format = 'pdb'
                                detected_content = content
                                break

            return render_template(
                'view_user_model_output.html',
                job=dict(job),
                project_name=job['project_name'] or 'Unknown Project',
                user_model={
                    'id': job['user_model_id'],
                    'name': job['model_name'],
                    'description': job['model_description'],
                    'output_format': job['output_format'],
                    'credit_cost': job['credit_cost']
                } if job['user_model_id'] else None,
                result_data=result_data,
                input_data=input_data,
                metadata=metadata,
                detected_format=detected_format,
                detected_content=detected_content
            )

        except Exception as e:
            logger.error(f"Error viewing user model output for job {job_id}: {e}", exc_info=True)
            return f"Error loading job output: {str(e)}", 500

    @app.route('/api/user-models/<int:model_id>/test-endpoint', methods=['POST'])
    @login_required
    @csrf_protect
    def test_saved_user_model_endpoint(model_id: int):
        """Probe or invoke a saved custom model endpoint."""
        from phonos_app.helpers.database import get_db
        from phonos_app.helpers.custom_endpoint_runner import ensure_byom_schema, probe_custom_endpoint

        user_id = session['user_id']
        db = get_db()
        ensure_byom_schema(db)
        row = db.execute(
            """
            SELECT um.* FROM user_models um
            LEFT JOIN project_assignments pa ON um.project_id = pa.project_id AND pa.user_id = %s
            WHERE um.id = %s AND (um.user_id = %s OR um.is_public = true OR pa.user_id IS NOT NULL)
            """,
            (user_id, model_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'Model not found or access denied.'}), 404
        model_dict = dict(row) if isinstance(row, dict) or hasattr(row, 'keys') else {}
        payload = request.get_json(silent=True) or {}
        sample_input = payload.get('sample_input') if isinstance(payload.get('sample_input'), dict) else None
        res = probe_custom_endpoint(model_dict, sample_input=sample_input, user_id=user_id)
        return jsonify(res), (200 if res.get('ok') else 400)

    @app.route('/api/user-models/probe-endpoint', methods=['POST'])
    @login_required
    @csrf_protect
    def probe_unsaved_user_model_endpoint():
        """Probe an endpoint configuration before saving a custom model."""
        from phonos_app.helpers.custom_endpoint_runner import probe_custom_endpoint, seal_endpoint_key

        user_id = session['user_id']
        data = request.get_json(silent=True) or {}
        draft = {
            'id': 0,
            'model_id': 'draft-probe',
            'model_name': data.get('model_name') or 'Draft Model',
            'worker_endpoint_url': data.get('worker_endpoint_url') or '',
            'endpoint_mode': data.get('endpoint_mode') or 'direct_http',
            'auth_type': data.get('auth_type') or 'none',
            'auth_header_name': data.get('auth_header_name') or 'Authorization',
            'upstream_api_key_enc': seal_endpoint_key(data.get('upstream_api_key')),
            'custom_headers_json': json.dumps(data.get('custom_headers') or {}) if isinstance(data.get('custom_headers'), dict) else (data.get('custom_headers') or '{}'),
            'request_template_json': data.get('request_template_json') or '',
            'response_output_path': data.get('response_output_path') or '',
            'upstream_model_name': data.get('upstream_model_name') or '',
            'output_format': data.get('output_format') or 'json',
        }
        sample_input = data.get('sample_input') if isinstance(data.get('sample_input'), dict) else None
        res = probe_custom_endpoint(draft, sample_input=sample_input, user_id=user_id)
        return jsonify(res), (200 if res.get('ok') else 400)
