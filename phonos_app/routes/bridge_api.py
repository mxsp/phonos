"""
User Model Bridge API with Encryption Support

Provides secure endpoints for custom model workers with:
- API key authentication with HMAC-SHA256 signing
- Optional AES-256-GCM encryption for job data
- Secure job polling and result submission
- Worker authentication and authorization
"""

import json
import hashlib
import hmac
import time
import logging
from functools import wraps
from flask import jsonify, request, g
from production.database import get_db_connection

# Import encryption utilities
try:
    from phonos_app.helpers.user_model_crypto import (
        encrypt_data,
        decrypt_data,
        hash_api_key,
        verify_request,
        sign_request,
        generate_model_api_key,
        create_job_encryption_header,
        verify_job_encryption_headers
    )
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logging.warning("User model crypto module not available")

logger = logging.getLogger(__name__)

# API version prefix
API_PREFIX = '/api/bridge/v1'


def worker_auth_required(f):
    """Decorator to authenticate worker requests using API key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid Authorization header'}), 401
        
        api_key = auth_header.replace('Bearer ', '').strip()
        
        if not api_key:
            return jsonify({'error': 'API key required'}), 401
        
        # Hash the API key for database lookup
        api_key_hash = hash_api_key(api_key) if CRYPTO_AVAILABLE else hashlib.sha256(api_key.encode()).hexdigest()
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Look up the API key
            cur.execute("""
                SELECT k.id, k.user_model_id, k.user_id, k.is_active, k.name,
                       um.model_name, um.model_id as model_id_str, um.encryption_enabled
                FROM user_model_api_keys k
                JOIN user_models um ON k.user_model_id = um.id
                WHERE k.key_hash = %s
            """, (api_key_hash,))
            
            key_data = cur.fetchone()
            
            if not key_data:
                return jsonify({'error': 'Invalid API key'}), 401
            
            if not key_data['is_active']:
                return jsonify({'error': 'API key is disabled'}), 403
            
            # Store key info in request context
            g.api_key = api_key
            g.api_key_id = key_data['id']
            g.user_id = key_data['user_id']
            g.model_id = key_data['user_model_id']
            g.model_id_str = key_data['model_id_str']
            g.model_name = key_data['model_name']
            g.encryption_enabled = key_data['encryption_enabled']
            
            # Update last used timestamp
            cur.execute("""
                UPDATE user_model_api_keys 
                SET last_used_at = NOW(), use_count = use_count + 1
                WHERE id = %s
            """, (key_data['id'],))
            conn.commit()
            
            return f(*args, **kwargs)
            
        finally:
            cur.close()
            conn.close()
    
    return decorated


def verify_request_signature(f):
    """Decorator to verify HMAC-SHA256 request signatures."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not CRYPTO_AVAILABLE:
            return f(*args, **kwargs)
        
        # Get signature and timestamp from headers
        signature = request.headers.get('X-Signature')
        timestamp_str = request.headers.get('X-Timestamp')
        
        if signature and timestamp_str:
            try:
                timestamp = int(timestamp_str)
                body = request.get_data(as_text=True) or ''
                
                if not verify_request(g.api_key, timestamp, body, signature):
                    return jsonify({'error': 'Invalid request signature'}), 401
                    
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid timestamp format'}), 400
        
        return f(*args, **kwargs)
    
    return decorated


def register_bridge_routes(app):
    """Register all bridge API routes."""
    
    @app.route(f'{API_PREFIX}/health', methods=['GET'])
    def bridge_health():
        """Health check endpoint."""
        return jsonify({
            'status': 'healthy',
            'version': '1.0.0',
            'encryption_available': CRYPTO_AVAILABLE
        })
    
    @app.route(f'{API_PREFIX}/worker/info', methods=['GET'])
    @worker_auth_required
    def worker_info():
        """Get information about the authenticated worker."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Get user info
            cur.execute("""
                SELECT id, username, email FROM users WHERE id = %s
            """, (g.user_id,))
            user = cur.fetchone()
            
            # Get authorized models
            cur.execute("""
                SELECT um.id, um.model_id, um.model_name, um.output_format
                FROM user_model_api_keys k
                JOIN user_models um ON k.user_model_id = um.id
                WHERE k.user_id = %s AND k.is_active = true
            """, (g.user_id,))
            models = cur.fetchall()
            
            return jsonify({
                'status': 'success',
                'user_id': g.user_id,
                'username': user['username'] if user else None,
                'worker_key_id': g.api_key_id,
                'encryption_enabled': g.encryption_enabled,
                'authorized_models': [{
                    'id': m['id'],
                    'model_id': m['model_id'],
                    'name': m['model_name'],
                    'output_format': m['output_format']
                } for m in models]
            })
        finally:
            cur.close()
            conn.close()
    
    @app.route(f'{API_PREFIX}/jobs/poll', methods=['POST'])
    @worker_auth_required
    @verify_request_signature
    def poll_jobs():
        """Poll for pending jobs."""
        data = request.get_json() or {}
        max_jobs = min(data.get('max_jobs', 1), 10)
        specific_model_id = data.get('model_id')
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Build query to get pending jobs
            query = """
                SELECT j.id, j.job_id, j.user_model_id, j.input_data, 
                       j.custom_params, j.output_format, j.created_at,
                       um.model_name, um.encryption_enabled
                FROM jobs j
                JOIN user_models um ON j.user_model_id = um.id
                WHERE j.status = 'pending'
                AND j.user_model_id = %s
            """
            params = [g.model_id]
            
            if specific_model_id:
                query += " AND j.user_model_id = %s"
                params.append(specific_model_id)
            
            query += " ORDER BY j.created_at ASC LIMIT %s"
            params.append(max_jobs)
            
            cur.execute(query, params)
            jobs = cur.fetchall()
            
            result_jobs = []
            for job in jobs:
                job_data = {
                    'job_id': job['job_id'],
                    'model_id': job['user_model_id'],
                    'model_name': job['model_name'],
                    'input_data': job['input_data'],
                    'custom_params': job['custom_params'] or {},
                    'output_format': job['output_format'],
                    'created_at': job['created_at'].isoformat() if job['created_at'] else None
                }
                
                # Encrypt job data if encryption is enabled
                if CRYPTO_AVAILABLE and job['encryption_enabled']:
                    encrypted_payload = encrypt_data(job_data, g.api_key)
                    result_jobs.append({
                        'encrypted': True,
                        'data': encrypted_payload
                    })
                else:
                    result_jobs.append(job_data)
                
                # Mark job as processing
                cur.execute("""
                    UPDATE jobs 
                    SET status = 'processing', started_at = NOW(), worker_id = %s
                    WHERE id = %s
                """, (request.headers.get('X-Worker-ID', 'unknown'), job['id']))
            
            conn.commit()
            
            return jsonify({
                'status': 'success',
                'jobs': result_jobs,
                'count': len(result_jobs),
                'encryption_enabled': CRYPTO_AVAILABLE and g.encryption_enabled
            })
            
        finally:
            cur.close()
            conn.close()
    
    @app.route(f'{API_PREFIX}/jobs/<job_id>/result', methods=['POST'])
    @worker_auth_required
    @verify_request_signature
    def submit_job_result(job_id):
        """Submit job result."""
        data = request.get_json() or {}
        
        # Handle encrypted results
        if data.get('encrypted') and CRYPTO_AVAILABLE:
            try:
                decrypted = decrypt_data(data['data'], g.api_key)
                data = decrypted
            except Exception as e:
                logger.error(f"Failed to decrypt job result: {e}")
                return jsonify({'error': 'Failed to decrypt result'}), 400
        
        status = data.get('status')
        result = data.get('result', {})
        error_message = data.get('error_message')
        output_file = data.get('output_file')
        metrics = data.get('metrics', {})
        
        if not status or status not in ['completed', 'failed']:
            return jsonify({'error': 'Invalid status. Must be "completed" or "failed"'}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Verify job exists and belongs to this model
            cur.execute("""
                SELECT id, user_model_id FROM jobs 
                WHERE job_id = %s AND user_model_id = %s
            """, (job_id, g.model_id))
            
            job = cur.fetchone()
            if not job:
                return jsonify({'error': 'Job not found or not authorized'}), 404
            
            # Update job status
            cur.execute("""
                UPDATE jobs 
                SET status = %s,
                    completed_at = NOW(),
                    result_data = %s,
                    error_message = %s,
                    output_file = %s,
                    worker_metrics = %s
                WHERE id = %s
            """, (
                status,
                json.dumps(result) if isinstance(result, dict) else result,
                error_message,
                output_file,
                json.dumps(metrics) if isinstance(metrics, dict) else metrics,
                job['id']
            ))
            
            conn.commit()
            
            return jsonify({
                'status': 'success',
                'message': f'Job {job_id} marked as {status}'
            })
            
        finally:
            cur.close()
            conn.close()
    
    @app.route(f'{API_PREFIX}/jobs/<job_id>/heartbeat', methods=['POST'])
    @worker_auth_required
    def job_heartbeat(job_id):
        """Update job progress."""
        data = request.get_json() or {}
        progress = data.get('progress', 0)
        message = data.get('message', '')
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE jobs 
                SET progress = %s, 
                    last_heartbeat = NOW(),
                    status_message = %s
                WHERE job_id = %s AND user_model_id = %s
            """, (progress, message, job_id, g.model_id))
            
            conn.commit()
            
            return jsonify({'status': 'success'})
        finally:
            cur.close()
            conn.close()


def init_bridge_api(app):
    """Initialize the bridge API with the Flask app."""
    register_bridge_routes(app)
    logger.info("User Model Bridge API initialized with encryption support")
