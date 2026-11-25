"""
Bridge API for User-Hosted Workers

This API acts as a secure middleman between user-hosted workers and the Redis job queue.
Workers poll for jobs and submit results without needing direct Redis access or encryption keys.

Security Model:
- Workers authenticate using project/model-specific API keys
- Bridge API handles all encryption/decryption using the master ENCRYPTION_KEY
- Workers only see plaintext job data for their authorized models
- Results are re-encrypted before being pushed to Redis
"""

import json
import time
import redis
import logging
from datetime import datetime
from flask import abort, jsonify, request
from config import Config

from ..app import app, get_redis_connection
from ..helpers.user_models import verify_model_api_key, get_user_model, increment_model_usage
from ..helpers.database import get_db
from ..helpers.user_model_security import (
    sanitize_output_data,
    sanitize_job_input_data,
    encrypt_with_api_key,
    decrypt_with_api_key
)
from ..helpers.user_model_crypto import (
    encrypt_data as encrypt_bridge_payload,
    decrypt_data as decrypt_bridge_payload,
    verify_request as verify_bridge_signature
)

logger = logging.getLogger(__name__)

try:
    from encryption_util import encrypt, decrypt
except ImportError:
    encrypt = None
    decrypt = None

# Ensure module is executed when imported with `from .api_bridge import *`
__all__ = ['authenticate_bridge_request']


def authenticate_bridge_request():
    """
    Authenticate a Bridge API request using API key.

    Returns:
        dict: Authentication context with user_id, model_id, project_id, key_id
        None: If authentication fails
    """
    # Get API key from Authorization header
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return None

    api_key = auth_header.replace('Bearer ', '', 1)

    # Verify the API key
    auth_context = verify_model_api_key(api_key)
    if not auth_context:
        return None

    # Store the original API key for encryption/decryption
    # (verify_model_api_key doesn't return it for security)
    auth_context['api_key'] = api_key

    # TODO: Check rate limiting
    # This would use the rate_limit field from the auth_context

    return auth_context


def enforce_bridge_signature(auth_context):
    """Require HMAC signature for encrypted bridge traffic."""
    if not auth_context.get('encryption_enabled'):
        return None

    timestamp_header = request.headers.get('X-Timestamp')
    signature = request.headers.get('X-Signature')

    if not timestamp_header or not signature:
        return jsonify({
            'status': 'error',
            'message': 'Missing X-Timestamp or X-Signature for encrypted worker key'
        }), 401

    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return jsonify({
            'status': 'error',
            'message': 'Invalid X-Timestamp header'
        }), 400

    raw_body = request.get_data(as_text=True) or ''
    if not verify_bridge_signature(auth_context['api_key'], timestamp, raw_body):
        return jsonify({
            'status': 'error',
            'message': 'Invalid request signature or timestamp window exceeded'
        }), 401

    return None


@app.route('/api/bridge/v1/jobs/poll', methods=['POST'])
def bridge_poll_jobs():
    """
    Poll for new jobs assigned to user-hosted workers.

    Request Body:
        {
            "model_id": 123,  // Optional: specific user model ID
            "project_id": 456,  // Optional: project ID
            "worker_id": "worker-1",  // Optional: worker identifier
            "max_jobs": 1  // Number of jobs to fetch (default: 1)
        }

    Response:
        {
            "status": "success",
            "jobs": [
                {
                    "job_id": "uuid",
                    "model_id": 123,
                    "input_data": { ... },  // Plaintext job data
                    "custom_params": { ... },
                    "timeout_seconds": 3600
                }
            ]
        }

    Authentication:
        Requires Bearer token (project or model API key)
    """
    # Authenticate request
    try:
        auth_context = authenticate_bridge_request()
        if not auth_context:
            return jsonify({
                'status': 'error',
                'message': 'Invalid or missing API key'
            }), 401

        signature_error = enforce_bridge_signature(auth_context)
        if signature_error:
            return signature_error
    except Exception as e:
        logger.error(f"Error during bridge authentication: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': 'Authentication failed due to internal error'
        }), 500

    user_id = auth_context['user_id']
    model_id = auth_context.get('user_model_id')
    project_id = auth_context.get('project_id')

    # Get request data
    data = request.get_json() or {}
    requested_model_id = data.get('model_id', model_id)
    requested_project_id = data.get('project_id', project_id)
    worker_id = data.get('worker_id', 'unknown')
    max_jobs = min(data.get('max_jobs', 1), 10)  # Limit to 10 jobs at once

    # Security check: Ensure worker can only access authorized models/projects
    if model_id and requested_model_id != model_id:
        return jsonify({
            'status': 'error',
            'message': 'API key not authorized for this model'
        }), 403

    if project_id and requested_project_id != project_id:
        return jsonify({
            'status': 'error',
            'message': 'API key not authorized for this project'
        }), 403

    try:
        db = get_db()
        r = get_redis_connection()

        # Find pending external jobs
        # Query jobs that are:
        # 1. Assigned to this user's models
        # 2. In 'pending' or 'queued' status
        # 3. Not yet assigned to a worker

        query = """
            SELECT
                j.id as job_db_id,
                j.job_id,
                j.job_data,
                j.message,
                j.model,
                eq.user_model_id,
                eq.timeout_seconds,
                um.model_type,
                um.output_format
            FROM jobs j
            JOIN external_job_queue eq ON j.id = eq.job_id
            JOIN user_models um ON eq.user_model_id = um.id
            WHERE eq.status = 'pending'
            AND um.user_id = %s
            AND um.is_active = true
        """

        params = [user_id]

        if requested_model_id:
            query += " AND eq.user_model_id = %s"
            params.append(requested_model_id)

        if requested_project_id:
            query += " AND um.project_id = %s"
            params.append(requested_project_id)

        query += " ORDER BY eq.created_at ASC LIMIT %s"
        params.append(max_jobs)

        result = db.execute(query, tuple(params))
        pending_jobs = result.fetchall()

        if not pending_jobs:
            return jsonify({
                'status': 'success',
                'jobs': []
            })

        # Process jobs and decrypt data
        jobs_to_return = []

        for job_row in pending_jobs:
            job_db_id = job_row['job_db_id']  # Integer ID for DB operations
            job_id = job_row['job_id']  # UUID string for worker API
            user_model_id = job_row['user_model_id']
            encrypted_job_data = job_row['job_data']

            # Fallback to message field if job_data is NULL (for testing/legacy)
            if not encrypted_job_data and job_row.get('message'):
                encrypted_job_data = job_row['message']

            # Decrypt job data using API key-based decryption (with fallback to master key)
            job_data_dict = None
            api_key = auth_context.get('api_key')

            if encrypted_job_data:
                # Convert string to bytes if needed
                try:
                    if isinstance(encrypted_job_data, str):
                        data_bytes = encrypted_job_data.encode('utf-8')
                    elif isinstance(encrypted_job_data, (bytes, memoryview)):
                        data_bytes = bytes(encrypted_job_data)
                    else:
                        data_bytes = encrypted_job_data
                except Exception as e:
                    logger.error(f"Failed to convert job data to bytes for {job_id}: {e}")
                    continue

                # Try API-key-based decryption first (if available)
                if api_key:
                    try:
                        decrypted_bytes = decrypt_with_api_key(data_bytes, api_key)
                        if decrypted_bytes:
                            job_data_dict = json.loads(decrypted_bytes.decode('utf-8'))
                            logger.debug(f"Successfully decrypted job {job_id} with API key")
                    except Exception as e:
                        logger.debug(f"API-key decryption failed for job {job_id}, trying master key: {e}")

                # Fallback to master key decryption
                if job_data_dict is None and decrypt:
                    try:
                        decrypted_bytes = decrypt(data_bytes)
                        if decrypted_bytes:
                            job_data_dict = json.loads(decrypted_bytes.decode('utf-8'))
                            logger.debug(f"Successfully decrypted job {job_id} with master key")
                    except Exception as e:
                        logger.warning(f"Master key decryption failed for job {job_id}, trying unencrypted fallback: {e}")
                        # Fall through to unencrypted handling

            # Fallback: try to parse as unencrypted JSON
            if job_data_dict is None and encrypted_job_data:
                try:
                    if isinstance(encrypted_job_data, (bytes, memoryview)):
                        job_data_dict = json.loads(bytes(encrypted_job_data).decode('utf-8'))
                    elif isinstance(encrypted_job_data, str):
                        job_data_dict = json.loads(encrypted_job_data)
                    else:
                        logger.error(f"Unexpected data type for job {job_id}: {type(encrypted_job_data)}")
                        continue
                    logger.info(f"Successfully parsed unencrypted job data for {job_id}")
                except Exception as e:
                    logger.error(f"Could not parse job data for {job_id}: {e}")
                    continue

            if job_data_dict is None:
                logger.error(f"No valid job data found for {job_id}")
                continue

            # Sanitize input data to remove sensitive fields before sending to worker
            raw_input_data = job_data_dict.get('input_data', {})
            raw_custom_params = job_data_dict.get('custom_params', {})

            sanitized_input = sanitize_job_input_data(raw_input_data)
            sanitized_params = sanitize_job_input_data(raw_custom_params)

            payload = {
                'job_id': job_id,
                'model_id': user_model_id,
                'input_data': sanitized_input,
                'custom_params': sanitized_params,
                'timeout_seconds': job_row['timeout_seconds'],
                'output_format': job_row['output_format']
            }

            if auth_context.get('encryption_enabled'):
                try:
                    encrypted_payload = encrypt_bridge_payload(payload, auth_context['api_key'])
                    jobs_to_return.append({
                        'encrypted': True,
                        'data': encrypted_payload
                    })
                except Exception as encryption_error:
                    logger.error(
                        f"Failed to encrypt bridge payload for job {job_id}: {encryption_error}",
                        exc_info=True
                    )
                    continue
            else:
                jobs_to_return.append(payload)

            # Mark job as assigned only after payload is prepared successfully
            db.execute("""
                UPDATE external_job_queue
                SET status = 'assigned',
                    assigned_at = CURRENT_TIMESTAMP,
                    worker_api_key_id = %s,
                    worker_identifier = %s
                WHERE job_id = %s
            """, (auth_context['key_id'], worker_id, job_db_id))

            # Update job status in main jobs table
            db.execute("""
                UPDATE jobs
                SET status = 'processing'
                WHERE id = %s
            """, (job_db_id,))

        db.commit()

        return jsonify({
            'status': 'success',
            'jobs': jobs_to_return,
            'count': len(jobs_to_return),
            'encryption_enabled': bool(auth_context.get('encryption_enabled')),
            'signature_required': bool(auth_context.get('encryption_enabled'))
        })

    except Exception as e:
        logger.error(f"Error in bridge poll: {e}", exc_info=True)
        db.rollback()
        return jsonify({
            'status': 'error',
            'message': 'Failed to poll for jobs'
        }), 500


@app.route('/api/bridge/v1/jobs/<job_id>/result', methods=['POST'])
def bridge_submit_result(job_id):
    """
    Submit job result from a user-hosted worker.

    Request Body:
        {
            "status": "completed",  // or "failed"
            "result": { ... },  // Result data (will be encrypted)
            "output_file": "base64_encoded_data",  // Optional
            "error_message": "...",  // If failed
            "metrics": {
                "runtime_seconds": 123.45,
                "memory_used_mb": 256
            }
        }

    Response:
        {
            "status": "success",
            "message": "Result submitted successfully"
        }

    Authentication:
        Requires Bearer token (project or model API key)
    """
    # Authenticate request
    try:
        auth_context = authenticate_bridge_request()
        if not auth_context:
            return jsonify({
                'status': 'error',
                'message': 'Invalid or missing API key'
            }), 401

        signature_error = enforce_bridge_signature(auth_context)
        if signature_error:
            return signature_error
    except Exception as e:
        logger.error(f"Error during bridge authentication: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': 'Authentication failed due to internal error'
        }), 500

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({
            'status': 'error',
            'message': 'No data provided'
        }), 400

    if auth_context.get('encryption_enabled'):
        if not data.get('encrypted') or not data.get('data'):
            return jsonify({
                'status': 'error',
                'message': 'Encrypted worker keys must submit encrypted result payloads'
            }), 400

        try:
            decrypted_payload = decrypt_bridge_payload(data['data'], auth_context['api_key'])
            if isinstance(decrypted_payload, bytes):
                decrypted_payload = json.loads(decrypted_payload.decode('utf-8'))
            if not isinstance(decrypted_payload, dict):
                raise ValueError("Decrypted payload is not an object")
            data = decrypted_payload
        except Exception as decrypt_error:
            logger.warning(f"Failed to decrypt encrypted worker result for job {job_id}: {decrypt_error}")
            return jsonify({
                'status': 'error',
                'message': 'Failed to decrypt encrypted result payload'
            }), 400

    job_status = data.get('status', 'completed')
    raw_result_data = data.get('result', {})
    error_message = data.get('error_message')
    metrics = data.get('metrics', {})

    try:
        db = get_db()
        r = get_redis_connection()

        # Look up job by UUID to get integer ID, output format, and verify it belongs to authenticated API key
        job_check = db.execute("""
            SELECT j.id as job_db_id, eq.user_model_id, eq.worker_api_key_id, um.user_id, um.output_format
            FROM jobs j
            JOIN external_job_queue eq ON j.id = eq.job_id
            JOIN user_models um ON eq.user_model_id = um.id
            WHERE j.job_id = %s
        """, (job_id,)).fetchone()

        if not job_check:
            return jsonify({
                'status': 'error',
                'message': 'Job not found in external queue'
            }), 404

        job_db_id = job_check['job_db_id']
        output_format = job_check['output_format'] or 'json'

        # Security check: ensure the API key matches the assigned worker
        if job_check['worker_api_key_id'] != auth_context['key_id']:
            return jsonify({
                'status': 'error',
                'message': 'Not authorized to submit results for this job'
            }), 403

        # Sanitize result data to remove sensitive fields and filter by output format
        result_data = sanitize_output_data(raw_result_data, output_format)
        logger.info(f"Sanitized result data for job {job_id} (format: {output_format})")

        # Prepare result package (use UUID for result tracking)
        result_package = {
            'job_id': job_id,
            'status': job_status,
            'result': result_data,
            'error_message': error_message,
            'metrics': metrics,
            'completed_at': datetime.utcnow().isoformat()
        }

        # Encrypt result using API key-based encryption for enhanced security
        # This ensures only the worker with the correct API key can decrypt the result
        result_json = json.dumps(result_package)
        api_key = auth_context.get('api_key')

        if api_key:
            # Use API-key-based encryption (primary method)
            encrypted_result = encrypt_with_api_key(result_json.encode('utf-8'), api_key)
            if not encrypted_result:
                logger.warning(f"API-key encryption failed for job {job_id}, falling back to master key")
                # Fallback to master encryption key
                if encrypt:
                    encrypted_result = encrypt(result_json.encode('utf-8'))
                else:
                    encrypted_result = result_json.encode('utf-8')
        elif encrypt:
            # Fallback to master encryption key if API key not available
            encrypted_result = encrypt(result_json.encode('utf-8'))
            if not encrypted_result:
                raise Exception("Encryption failed")
        else:
            # No encryption available
            encrypted_result = result_json.encode('utf-8')

        # Push encrypted result to Redis job results queue
        r.lpush(Config.REDIS_JOB_RESULTS_QUEUE, encrypted_result)

        # Update external_job_queue status (use integer ID)
        db.execute("""
            UPDATE external_job_queue
            SET status = %s,
                completed_at = CURRENT_TIMESTAMP
            WHERE job_id = %s
        """, ('completed' if job_status == 'completed' else 'failed', job_db_id))

        # Prepare result message for storage
        # Store the result data as JSON in the message column for retrieval
        result_message = None
        if job_status == 'completed' and result_data:
            result_message = json.dumps(result_data)
        elif job_status == 'failed' and error_message:
            result_message = json.dumps({'error': error_message})

        # Update main jobs table with status AND result message (use integer ID)
        db.execute("""
            UPDATE jobs
            SET status = %s,
                message = %s,
                execution_time = %s
            WHERE id = %s
        """, (job_status, result_message, metrics.get('runtime_seconds'), job_db_id))

        # Increment model usage counter
        increment_model_usage(job_check['user_model_id'])

        db.commit()

        logger.info(f"Job {job_id} result submitted successfully by worker via Bridge API")

        return jsonify({
            'status': 'success',
            'message': 'Result submitted successfully'
        })

    except Exception as e:
        logger.error(f"Error submitting job result for {job_id}: {e}", exc_info=True)
        db.rollback()
        return jsonify({
            'status': 'error',
            'message': 'Failed to submit result'
        }), 500


@app.route('/api/bridge/v1/health', methods=['GET'])
def bridge_health():
    """
    Health check endpoint for workers.

    Response:
        {
            "status": "ok",
            "timestamp": "2025-01-01T12:00:00Z",
            "version": "1.0"
        }
    """
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'version': '1.0'
    })


@app.route('/api/bridge/v1/worker/info', methods=['GET'])
def bridge_worker_info():
    """
    Get information about the authenticated worker's authorized models and projects.

    Authentication:
        Requires Bearer token (project or model API key)

    Response:
        {
            "status": "success",
            "user_id": 123,
            "authorized_models": [...],
            "authorized_projects": [...]
        }
    """
    # Authenticate request
    try:
        auth_context = authenticate_bridge_request()
        if not auth_context:
            return jsonify({
                'status': 'error',
                'message': 'Invalid or missing API key'
            }), 401
    except Exception as e:
        logger.error(f"Error during bridge authentication: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': 'Authentication failed due to internal error'
        }), 500

    try:
        db = get_db()

        # Get authorized models
        models = []
        if auth_context.get('user_model_id'):
            model_data = get_user_model(auth_context['user_model_id'])
            if model_data:
                models.append({
                    'id': model_data['id'],
                    'name': model_data['model_name'],
                    'description': model_data['model_description']
                })

        # Get authorized projects
        projects = []
        if auth_context.get('project_id'):
            project_data = db.execute("""
                SELECT id, name, description
                FROM projects
                WHERE id = %s
            """, (auth_context['project_id'],)).fetchone()
            if project_data:
                projects.append({
                    'id': project_data['id'],
                    'name': project_data['name'],
                    'description': project_data['description']
                })

        return jsonify({
            'status': 'success',
            'user_id': auth_context['user_id'],
            'authorized_models': models,
            'authorized_projects': projects,
            'rate_limit': auth_context.get('rate_limit')
        })

    except Exception as e:
        logger.error(f"Error getting worker info: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': 'Failed to get worker information'
        }), 500
