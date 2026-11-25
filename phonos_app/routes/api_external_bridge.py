"""
External API Bridge for Worker-Based Job Processing

This module provides bridge API endpoints for external workers to:
- Poll for pending jobs
- Submit job results
- Check job status
- Manage worker connections
- Encrypted communication via API keys

Similar to the user models bridge (api_bridge.py) but for standard models
like ESMFold, AlphaFold2, and DiffDock.

Security Model:
- Workers authenticate using worker API keys (wk_ prefix)
- Keys are scoped to specific model types or all models
- Rate limiting per API key
- All operations logged for analytics
- Optional encrypted payloads using AES-256-GCM
- Request signing with HMAC-SHA256 for integrity
"""

import json
import time
import logging
from datetime import datetime
from functools import wraps
from flask import jsonify, request

from ..app import app, get_redis_connection
from ..helpers.api_keys import (
    verify_unified_api_key,
    check_rate_limit,
    log_api_usage,
    get_pending_external_jobs,
    assign_external_job,
    complete_external_job
)
from ..helpers.database import get_db
from ..helpers.user_model_crypto import (
    encrypt_data,
    decrypt_data,
    verify_request,
    sign_request,
    ModelMessage
)

logger = logging.getLogger(__name__)

# Comprehensive alias -> canonical model id map.
# Auto-derived from SUPPORTED_MODELS in production/config.py. Every friendly
# spelling (canonical id, short_name, snake_case, hyphenated) maps to the
# single canonical id the catalog uses. Lookup is case-insensitive; the first
# match wins. Missing keys fall through unchanged so callers can still pass
# a raw id like 'type-9' if they want.
MODEL_TYPE_MAP = {
    # ESMFold (canonical id type-0)
    'esmfold': 'type-0',
    'type-0': 'type-0',
    # AlphaFold2 (canonical id type-2)
    'alphafold2': 'type-2',
    'type-2': 'type-2',
    # DiffDock (canonical id type-3)
    'diffdock': 'type-3',
    'type-3': 'type-3',
    # ProteinMPNN (canonical id type-4)
    'proteinmpnn': 'type-4',
    'type-4': 'type-4',
    # Fold+Dock (canonical id type-5)
    'fold+dock': 'type-5',
    'fold-dock': 'type-5',
    'fold_dock': 'type-5',
    'type-5': 'type-5',
    # Metal3D (canonical id type-6)
    'metal3d': 'type-6',
    'type-6': 'type-6',
    # Evo2 (canonical id evo2; type-8 is an alias)
    'evo2': 'evo2',
    'type-8': 'evo2',
    # Boltz2 (canonical id type-9)
    'boltz2': 'type-9',
    'type-9': 'type-9',
    # RFdiffusion (canonical id type-12)
    'rfdiffusion': 'type-12',
    'type-12': 'type-12',
    # Hyphenated / RE models
    'esmfold-re': 'esmfold-re',
    'esmfold_re': 'esmfold-re',
    'alphafold2-re': 'alphafold2-re',
    'alphafold2_re': 'alphafold2-re',
    'alphafold2-re-multimer': 'alphafold2-re-multimer',
    'alphafold2_re_multimer': 'alphafold2-re-multimer',
    'alphafold2-multimer': 'alphafold2-re-multimer',
    'alphafold2_multimer': 'alphafold2-re-multimer',
    'diffdock-re': 'diffdock-re',
    'diffdock_re': 'diffdock-re',
    'proteinmpnn-re': 'proteinmpnn-re',
    'proteinmpnn_re': 'proteinmpnn-re',
    # Misc catalog entries
    'openfold3': 'openfold3',
    'boltz2-enhanced': 'boltz2-enhanced',
    'boltz2_enhanced': 'boltz2-enhanced',
    'molmim': 'molmim',
    'genmol': 'genmol',
    'msa-search': 'msa-search',
    'msa_search': 'msa-search',
    'msa search': 'msa-search',
}

REVERSE_MODEL_MAP = {v: k for k, v in MODEL_TYPE_MAP.items()}


def require_worker_api_key(f):
    """
    Decorator to require and validate worker API key.
    Also handles encrypted request payloads.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get API key from Authorization header
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return jsonify({
                'status': 'error',
                'error': 'missing_auth',
                'message': 'Missing or invalid Authorization header. Use: Authorization: Bearer YOUR_API_KEY'
            }), 401

        api_key = auth_header.replace('Bearer ', '').strip()

        # Verify API key using unified verification
        auth_context = verify_unified_api_key(api_key)

        if not auth_context:
            return jsonify({
                'status': 'error',
                'error': 'invalid_key',
                'message': 'Invalid or deactivated API key'
            }), 401

        # Check key type - must be 'worker' or 'public' with worker permissions
        key_type = auth_context.get('key_type', 'public')
        permissions = auth_context.get('permissions', [])

        if key_type not in ['worker', 'public']:
            return jsonify({
                'status': 'error',
                'error': 'invalid_key_type',
                'message': 'This endpoint requires a worker API key'
            }), 403

        # For public keys, check if they have worker permissions
        if key_type == 'public' and 'poll' not in permissions and 'worker' not in permissions:
            return jsonify({
                'status': 'error',
                'error': 'insufficient_permissions',
                'message': 'API key does not have worker permissions'
            }), 403

        # Check rate limit
        if not check_rate_limit(auth_context['key_id'], auth_context['rate_limit']):
            return jsonify({
                'status': 'error',
                'error': 'rate_limit_exceeded',
                'message': f'Rate limit exceeded ({auth_context["rate_limit"]} requests/minute)'
            }), 429

        # Check for signature verification (optional but recommended)
        x_timestamp = request.headers.get('X-Timestamp')
        x_signature = request.headers.get('X-Signature')

        if x_timestamp and x_signature:
            try:
                timestamp = int(x_timestamp)
                body = request.get_data(as_text=True) or ''
                if not verify_request(api_key, timestamp, body, x_signature):
                    logger.warning(f"Invalid request signature for key {auth_context['key_id']}")
                    return jsonify({
                        'status': 'error',
                        'error': 'invalid_signature',
                        'message': 'Request signature verification failed'
                    }), 401
                request.signature_verified = True
            except (ValueError, TypeError):
                pass

        # Add auth context to request
        request.auth_context = auth_context
        request.api_key_id = auth_context['key_id']
        request.api_user_id = auth_context['user_id']
        request.api_key = api_key  # Store for encryption operations
        request.encryption_enabled = auth_context.get('encryption_enabled', False)

        return f(*args, **kwargs)

    return decorated_function


def decrypt_request_payload(api_key: str, data: dict) -> dict:
    """
    Decrypt request payload if encrypted.

    Args:
        api_key: The API key for decryption
        data: The request data (may contain encrypted payload)

    Returns:
        Decrypted data dict
    """
    if not data:
        return {}

    if data.get('encrypted') and data.get('data'):
        try:
            decrypted = decrypt_data(data['data'], api_key)
            if isinstance(decrypted, dict):
                return decrypted
            return {'data': decrypted}
        except Exception as e:
            logger.error(f"Failed to decrypt request payload: {e}")
            raise ValueError("Failed to decrypt request payload")

    return data


def encrypt_response_payload(api_key: str, data: dict, encrypt: bool = True) -> dict:
    """
    Encrypt response payload if encryption is enabled.

    Args:
        api_key: The API key for encryption
        data: The response data to encrypt
        encrypt: Whether to encrypt

    Returns:
        Response dict (encrypted or plain)
    """
    if not encrypt:
        return data

    try:
        encrypted = encrypt_data(data, api_key)
        return {
            'status': 'success',
            'encrypted': True,
            'data': encrypted
        }
    except Exception as e:
        logger.error(f"Failed to encrypt response payload: {e}")
        return data  # Fallback to unencrypted


# =============================================================================
# BRIDGE API ENDPOINTS
# =============================================================================

@app.route('/api/external/v1/health', methods=['GET'])
def external_bridge_health():
    """
    Health check endpoint for external workers.

    Response:
        {
            "status": "ok",
            "service": "external-bridge",
            "version": "1.0",
            "timestamp": "2025-01-01T12:00:00Z"
        }
    """
    return jsonify({
        'status': 'ok',
        'service': 'external-bridge',
        'version': '1.0',
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    })


@app.route('/api/external/v1/models', methods=['GET'])
@require_worker_api_key
def external_bridge_list_models():
    """
    List available models for the authenticated worker.

    Response:
        {
            "status": "success",
            "models": [
                {
                    "id": "esmfold",
                    "internal_type": "type-0",
                    "name": "ESMFold",
                    "description": "Fast protein structure prediction"
                },
                ...
            ]
        }
    """
    auth_context = request.auth_context
    scope_model_type = auth_context.get('scope_model_type')

    models = []

    model_info = {
        'type-0': {'id': 'esmfold', 'name': 'ESMFold', 'description': 'Fast protein structure prediction from Meta AI'},
        'type-2': {'id': 'alphafold2', 'name': 'AlphaFold2', 'description': 'High-accuracy protein structure prediction'},
        'type-3': {'id': 'diffdock', 'name': 'DiffDock', 'description': 'Molecular docking for protein-ligand interactions'},
        'type-boltz2': {'id': 'boltz2', 'name': 'Boltz2', 'description': 'Protein structure prediction with Boltz2'},
        'type-evo2': {'id': 'evo2', 'name': 'Evo2-40B', 'description': 'DNA sequence generation'}
    }

    for internal_type, info in model_info.items():
        # If key is scoped to specific model, only show that model
        if scope_model_type and scope_model_type != internal_type:
            continue

        models.append({
            'id': info['id'],
            'internal_type': internal_type,
            'name': info['name'],
            'description': info['description']
        })

    return jsonify({
        'status': 'success',
        'models': models,
        'scope': scope_model_type
    })


@app.route('/api/external/v1/jobs/poll', methods=['POST'])
@require_worker_api_key
def external_bridge_poll_jobs():
    """
    Poll for pending jobs to process.

    Supports encrypted communication when X-Encryption header is 'true'.

    Request Headers (optional):
        X-Encryption: true              // Request encrypted response
        X-Timestamp: 1234567890         // Unix timestamp for signature
        X-Signature: abc123...          // HMAC-SHA256 signature

    Request Body:
        {
            "model": "esmfold",          // Optional: filter by model
            "model_type": "type-0",       // Optional: filter by internal type
            "worker_id": "worker-1",      // Optional: worker identifier
            "max_jobs": 5,                // Optional: max jobs to fetch (default: 1, max: 10)
            "encrypted": true,            // Optional: if payload is encrypted
            "data": "base64..."           // Encrypted payload (if encrypted=true)
        }

    Response (plain):
        {
            "status": "success",
            "jobs": [
                {
                    "job_id": "uuid-string",
                    "job_db_id": 123,
                    "model": "esmfold",
                    "model_type": "type-0",
                    "name": "My Protein",
                    "sequence": "MKFLIL...",
                    "params": {...},
                    "timeout_seconds": 3600
                }
            ],
            "count": 1
        }

    Response (encrypted when X-Encryption: true):
        {
            "status": "success",
            "encrypted": true,
            "data": "base64-encoded-encrypted-payload"
        }
    """
    start_time = time.time()
    auth_context = request.auth_context
    user_id = auth_context['user_id']
    key_id = auth_context['key_id']
    api_key = getattr(request, 'api_key', None)

    # Check if encrypted response is requested
    want_encrypted = request.headers.get('X-Encryption', '').lower() == 'true'

    # Parse request (handle encrypted payload)
    raw_data = request.get_json() or {}
    try:
        if api_key and raw_data.get('encrypted'):
            data = decrypt_request_payload(api_key, raw_data)
        else:
            data = raw_data
    except ValueError as e:
        return jsonify({
            'status': 'error',
            'error': 'decryption_failed',
            'message': str(e)
        }), 400

    worker_id = data.get('worker_id', 'unknown')
    max_jobs = min(data.get('max_jobs', 1), 10)

    # Determine model type filter
    model_filter = None
    if data.get('model'):
        model_filter = MODEL_TYPE_MAP.get(data['model'].lower())
    elif data.get('model_type'):
        model_filter = data['model_type']

    # If key is scoped to a model type, enforce it
    scope_model_type = auth_context.get('scope_model_type')
    if scope_model_type:
        if model_filter and model_filter != scope_model_type:
            return jsonify({
                'status': 'error',
                'error': 'model_not_authorized',
                'message': f'API key only authorized for {scope_model_type}'
            }), 403
        model_filter = scope_model_type

    try:
        db = get_db()

        # Query pending jobs from external_api_job_queue
        query = """
            SELECT eq.id as queue_id, eq.job_id, eq.job_uuid, eq.model_type,
                   eq.job_data, eq.timeout_seconds, eq.created_at,
                   j.name as job_name, j.sequence, j.job_data as full_job_data
            FROM external_api_job_queue eq
            JOIN jobs j ON eq.job_id = j.id
            WHERE eq.user_id = %s AND eq.status = 'pending'
        """
        params = [user_id]

        if model_filter:
            query += " AND eq.model_type = %s"
            params.append(model_filter)

        query += " ORDER BY eq.created_at ASC LIMIT %s"
        params.append(max_jobs)

        result = db.execute(query, tuple(params))
        pending_jobs = result.fetchall()

        if not pending_jobs:
            response_data = {
                'status': 'success',
                'jobs': [],
                'count': 0
            }
            if want_encrypted and api_key:
                response_data = encrypt_response_payload(api_key, response_data)
            return jsonify(response_data)

        # Process and assign jobs
        jobs_to_return = []

        for job_row in pending_jobs:
            queue_id = job_row['queue_id']
            job_uuid = job_row['job_uuid']
            model_type = job_row['model_type']

            # Parse job data
            job_data = {}
            if job_row['job_data']:
                try:
                    if isinstance(job_row['job_data'], str):
                        job_data = json.loads(job_row['job_data'])
                    elif isinstance(job_row['job_data'], dict):
                        job_data = job_row['job_data']
                except Exception as e:
                    logger.warning(f"Failed to parse job_data for {job_uuid}: {e}")

            # Mark job as assigned
            db.execute("""
                UPDATE external_api_job_queue
                SET status = 'assigned',
                    assigned_at = CURRENT_TIMESTAMP,
                    worker_api_key_id = %s,
                    worker_identifier = %s
                WHERE id = %s AND status = 'pending'
            """, (key_id, worker_id, queue_id))

            # Update main jobs table status
            db.execute("""
                UPDATE jobs
                SET status = 'processing',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (job_row['job_id'],))

            jobs_to_return.append({
                'job_id': job_uuid,
                'job_db_id': job_row['job_id'],
                'model': REVERSE_MODEL_MAP.get(model_type, model_type),
                'model_type': model_type,
                'name': job_row['job_name'],
                'sequence': job_row['sequence'],
                'params': job_data.get('params', {}),
                'input_data': job_data,
                'timeout_seconds': job_row['timeout_seconds'] or 3600,
                'created_at': job_row['created_at'].isoformat() if job_row['created_at'] else None
            })

        db.commit()

        # Log API usage
        response_time = int((time.time() - start_time) * 1000)
        log_api_usage(
            api_key_id=key_id,
            user_id=user_id,
            endpoint='/api/external/v1/jobs/poll',
            model_type=model_filter or 'all',
            status_code=200,
            response_time_ms=response_time,
            credits_used=0,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )

        logger.info(f"Worker {worker_id} polled {len(jobs_to_return)} jobs for user {user_id}")

        response_data = {
            'status': 'success',
            'jobs': jobs_to_return,
            'count': len(jobs_to_return),
            'encryption_available': bool(api_key)
        }

        # Encrypt response if requested
        if want_encrypted and api_key:
            response_data = encrypt_response_payload(api_key, response_data)

        return jsonify(response_data)

    except Exception as e:
        logger.error(f"Error in external bridge poll: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'poll_failed',
            'message': 'Failed to poll for jobs'
        }), 500


@app.route('/api/external/v1/jobs/<job_id>/result', methods=['POST'])
@require_worker_api_key
def external_bridge_submit_result(job_id):
    """
    Submit job result from a worker.

    Supports encrypted payloads for secure result transmission.

    Path Parameters:
        job_id: UUID of the job

    Request Headers (optional):
        X-Timestamp: 1234567890         // Unix timestamp for signature
        X-Signature: abc123...          // HMAC-SHA256 signature

    Request Body (plain):
        {
            "status": "completed",        // "completed" or "failed"
            "result": {                   // Result data (for completed)
                "output_file": "...",     // Path or content
                "pdb_data": "...",        // Optional: PDB content
                "scores": {...}           // Optional: confidence scores
            },
            "error_message": "...",       // Error message (for failed)
            "metrics": {
                "runtime_seconds": 45.2,
                "memory_used_mb": 1024
            }
        }

    Request Body (encrypted):
        {
            "encrypted": true,
            "data": "base64-encoded-encrypted-payload"
        }

    Response:
        {
            "status": "success",
            "message": "Result submitted successfully"
        }
    """
    start_time = time.time()
    auth_context = request.auth_context
    user_id = auth_context['user_id']
    key_id = auth_context['key_id']
    api_key = getattr(request, 'api_key', None)

    # Parse request (handle encrypted payload)
    raw_data = request.get_json()
    if not raw_data:
        return jsonify({
            'status': 'error',
            'error': 'no_data',
            'message': 'No JSON data provided'
        }), 400

    # Decrypt if encrypted
    try:
        if api_key and raw_data.get('encrypted'):
            data = decrypt_request_payload(api_key, raw_data)
            logger.info(f"Decrypted result payload for job {job_id}")
        else:
            data = raw_data
    except ValueError as e:
        return jsonify({
            'status': 'error',
            'error': 'decryption_failed',
            'message': str(e)
        }), 400

    job_status = data.get('status', 'completed')
    result_data = data.get('result', {})
    error_message = data.get('error_message')
    metrics = data.get('metrics', {})

    if job_status not in ['completed', 'failed']:
        return jsonify({
            'status': 'error',
            'error': 'invalid_status',
            'message': 'Status must be "completed" or "failed"'
        }), 400

    try:
        db = get_db()

        # Verify job exists and belongs to this worker
        job_check = db.execute("""
            SELECT eq.id as queue_id, eq.job_id, eq.job_uuid, eq.worker_api_key_id,
                   eq.model_type, j.creator_id
            FROM external_api_job_queue eq
            JOIN jobs j ON eq.job_id = j.id
            WHERE eq.job_uuid = %s
        """, (job_id,)).fetchone()

        if not job_check:
            return jsonify({
                'status': 'error',
                'error': 'job_not_found',
                'message': 'Job not found in external queue'
            }), 404

        # Security check: verify worker is authorized for this job
        if job_check['worker_api_key_id'] != key_id:
            return jsonify({
                'status': 'error',
                'error': 'not_authorized',
                'message': 'Not authorized to submit results for this job'
            }), 403

        queue_id = job_check['queue_id']
        job_db_id = job_check['job_id']

        # Update external queue
        db.execute("""
            UPDATE external_api_job_queue
            SET status = %s,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (job_status, queue_id))

        # Prepare result message for storage
        result_message = None
        if job_status == 'completed' and result_data:
            result_message = json.dumps(result_data)
        elif job_status == 'failed' and error_message:
            result_message = json.dumps({'error': error_message})

        # Handle output file if provided
        output_file = None
        output_display_name = None
        if result_data.get('output_file'):
            output_file = result_data['output_file']
            if result_data.get('output_display_name'):
                output_display_name = result_data['output_display_name']
            elif result_data.get('pdb_data'):
                output_display_name = f"{job_id}.pdb"

        # Update main jobs table
        runtime = metrics.get('runtime_seconds')
        db.execute("""
            UPDATE jobs
            SET status = %s,
                message = %s,
                output_file = COALESCE(%s, output_file),
                output_display_name = COALESCE(%s, output_display_name),
                execution_time = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (job_status, result_message, output_file, output_display_name, runtime, job_db_id))

        db.commit()

        # Log API usage
        response_time = int((time.time() - start_time) * 1000)
        log_api_usage(
            api_key_id=key_id,
            user_id=user_id,
            endpoint='/api/external/v1/jobs/result',
            model_type=job_check['model_type'],
            status_code=200,
            response_time_ms=response_time,
            credits_used=0,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )

        logger.info(f"Job {job_id} result submitted: {job_status}")

        return jsonify({
            'status': 'success',
            'message': 'Result submitted successfully',
            'job_id': job_id,
            'job_status': job_status
        })

    except Exception as e:
        logger.error(f"Error submitting job result for {job_id}: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'submit_failed',
            'message': 'Failed to submit result'
        }), 500


@app.route('/api/external/v1/jobs/<job_id>/status', methods=['GET'])
@require_worker_api_key
def external_bridge_job_status(job_id):
    """
    Get status of a specific job.

    Path Parameters:
        job_id: UUID of the job

    Response:
        {
            "status": "success",
            "job": {
                "job_id": "uuid",
                "status": "processing",
                "model": "esmfold",
                "created_at": "...",
                "assigned_at": "...",
                "worker_id": "worker-1"
            }
        }
    """
    auth_context = request.auth_context
    user_id = auth_context['user_id']

    try:
        db = get_db()

        result = db.execute("""
            SELECT eq.id, eq.job_uuid, eq.model_type, eq.status as queue_status,
                   eq.created_at, eq.assigned_at, eq.completed_at,
                   eq.worker_identifier,
                   j.name, j.status as job_status
            FROM external_api_job_queue eq
            JOIN jobs j ON eq.job_id = j.id
            WHERE eq.job_uuid = %s AND eq.user_id = %s
        """, (job_id, user_id)).fetchone()

        if not result:
            return jsonify({
                'status': 'error',
                'error': 'not_found',
                'message': 'Job not found or not accessible'
            }), 404

        return jsonify({
            'status': 'success',
            'job': {
                'job_id': result['job_uuid'],
                'name': result['name'],
                'model': REVERSE_MODEL_MAP.get(result['model_type'], result['model_type']),
                'model_type': result['model_type'],
                'queue_status': result['queue_status'],
                'job_status': result['job_status'],
                'created_at': result['created_at'].isoformat() if result['created_at'] else None,
                'assigned_at': result['assigned_at'].isoformat() if result['assigned_at'] else None,
                'completed_at': result['completed_at'].isoformat() if result['completed_at'] else None,
                'worker_id': result['worker_identifier']
            }
        })

    except Exception as e:
        logger.error(f"Error getting job status for {job_id}: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'status_failed',
            'message': 'Failed to get job status'
        }), 500


@app.route('/api/external/v1/jobs/<job_id>/heartbeat', methods=['POST'])
@require_worker_api_key
def external_bridge_job_heartbeat(job_id):
    """
    Send a heartbeat for an in-progress job.

    This prevents the job from being considered stale and reassigned.

    Path Parameters:
        job_id: UUID of the job

    Request Body:
        {
            "progress": 50,               // Optional: progress percentage
            "message": "Processing..."    // Optional: status message
        }

    Response:
        {
            "status": "success",
            "message": "Heartbeat recorded"
        }
    """
    auth_context = request.auth_context
    key_id = auth_context['key_id']

    data = request.get_json() or {}
    progress = data.get('progress')
    message = data.get('message')

    try:
        db = get_db()

        # Verify job belongs to this worker
        result = db.execute("""
            SELECT id, worker_api_key_id
            FROM external_api_job_queue
            WHERE job_uuid = %s AND status = 'assigned'
        """, (job_id,)).fetchone()

        if not result:
            return jsonify({
                'status': 'error',
                'error': 'not_found',
                'message': 'Job not found or not assigned'
            }), 404

        if result['worker_api_key_id'] != key_id:
            return jsonify({
                'status': 'error',
                'error': 'not_authorized',
                'message': 'Not authorized for this job'
            }), 403

        # Update heartbeat timestamp
        db.execute("""
            UPDATE external_api_job_queue
            SET last_heartbeat = CURRENT_TIMESTAMP,
                progress = COALESCE(%s, progress),
                progress_message = COALESCE(%s, progress_message)
            WHERE id = %s
        """, (progress, message, result['id']))

        db.commit()

        return jsonify({
            'status': 'success',
            'message': 'Heartbeat recorded',
            'job_id': job_id
        })

    except Exception as e:
        logger.error(f"Error recording heartbeat for {job_id}: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'heartbeat_failed',
            'message': 'Failed to record heartbeat'
        }), 500


@app.route('/api/external/v1/worker/info', methods=['GET'])
@require_worker_api_key
def external_bridge_worker_info():
    """
    Get information about the authenticated worker.

    Response:
        {
            "status": "success",
            "user_id": 123,
            "key_type": "worker",
            "scope_model_type": "type-0",
            "permissions": ["poll", "submit", "status"],
            "rate_limit": 60,
            "stats": {
                "pending_jobs": 5,
                "assigned_jobs": 2,
                "completed_jobs": 150
            }
        }
    """
    auth_context = request.auth_context
    user_id = auth_context['user_id']
    scope_model_type = auth_context.get('scope_model_type')

    try:
        db = get_db()

        # Get job stats
        query = """
            SELECT
                COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
                COUNT(CASE WHEN status = 'assigned' THEN 1 END) as assigned,
                COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed
            FROM external_api_job_queue
            WHERE user_id = %s
        """
        params = [user_id]

        if scope_model_type:
            query = query.replace("WHERE user_id = %s", "WHERE user_id = %s AND model_type = %s")
            params.append(scope_model_type)

        stats = db.execute(query, tuple(params)).fetchone()

        return jsonify({
            'status': 'success',
            'user_id': user_id,
            'key_type': auth_context.get('key_type', 'worker'),
            'scope_model_type': scope_model_type,
            'permissions': auth_context.get('permissions', []),
            'rate_limit': auth_context.get('rate_limit', 60),
            'stats': {
                'pending_jobs': stats['pending'] or 0,
                'assigned_jobs': stats['assigned'] or 0,
                'completed_jobs': stats['completed'] or 0,
                'failed_jobs': stats['failed'] or 0
            }
        })

    except Exception as e:
        logger.error(f"Error getting worker info: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'info_failed',
            'message': 'Failed to get worker information'
        }), 500


@app.route('/api/external/v1/jobs/queue', methods=['GET'])
@require_worker_api_key
def external_bridge_queue_status():
    """
    Get queue status and pending job counts by model type.

    Response:
        {
            "status": "success",
            "queue": {
                "total_pending": 25,
                "by_model": {
                    "esmfold": 10,
                    "alphafold2": 8,
                    "diffdock": 7
                }
            }
        }
    """
    auth_context = request.auth_context
    user_id = auth_context['user_id']
    scope_model_type = auth_context.get('scope_model_type')

    try:
        db = get_db()

        # Get counts by model type
        query = """
            SELECT model_type, COUNT(*) as count
            FROM external_api_job_queue
            WHERE user_id = %s AND status = 'pending'
        """
        params = [user_id]

        if scope_model_type:
            query += " AND model_type = %s"
            params.append(scope_model_type)

        query += " GROUP BY model_type"

        result = db.execute(query, tuple(params))

        by_model = {}
        total = 0
        for row in result.fetchall():
            model_name = REVERSE_MODEL_MAP.get(row['model_type'], row['model_type'])
            by_model[model_name] = row['count']
            total += row['count']

        return jsonify({
            'status': 'success',
            'queue': {
                'total_pending': total,
                'by_model': by_model
            }
        })

    except Exception as e:
        logger.error(f"Error getting queue status: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'queue_failed',
            'message': 'Failed to get queue status'
        }), 500
