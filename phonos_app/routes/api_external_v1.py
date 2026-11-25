"""
External API v1 - Complete API for External Integration

This module provides a comprehensive REST API for external access to Phonos.
All endpoints require API key authentication via Bearer token.

Authentication:
    Authorization: Bearer YOUR_API_KEY

Available Endpoints:
    # User & Account
    GET  /api/external/v1/me                    - Get current user info
    GET  /api/external/v1/usage                 - Get API usage statistics
    GET  /api/external/v1/credits               - Get credit balance

    # Models
    GET  /api/external/v1/models                - List available models

    # Jobs
    GET  /api/external/v1/jobs                  - List user's jobs
    POST /api/external/v1/jobs                  - Submit a new job
    GET  /api/external/v1/jobs/<job_id>         - Get job details
    GET  /api/external/v1/jobs/<job_id>/status  - Get job status
    GET  /api/external/v1/jobs/<job_id>/result  - Get job result/output
    DELETE /api/external/v1/jobs/<job_id>       - Delete a job
    POST /api/external/v1/jobs/<job_id>/cancel  - Cancel a pending job

    # Projects
    GET  /api/external/v1/projects              - List user's projects
    POST /api/external/v1/projects              - Create a new project
    GET  /api/external/v1/projects/<id>         - Get project details
    PUT  /api/external/v1/projects/<id>         - Update project
    DELETE /api/external/v1/projects/<id>       - Delete project
    GET  /api/external/v1/projects/<id>/jobs    - List jobs in project
    GET  /api/external/v1/projects/<id>/files   - List files in project
    GET  /api/external/v1/projects/<id>/members - List project members

    # Files
    GET  /api/external/v1/files                 - List user's files
    POST /api/external/v1/files/upload          - Upload a file
    GET  /api/external/v1/files/<id>            - Get file info
    GET  /api/external/v1/files/<id>/download   - Download file
    DELETE /api/external/v1/files/<id>          - Delete file

    # User Models (Custom Models)
    GET  /api/external/v1/user-models           - List user's custom models
    GET  /api/external/v1/user-models/<id>      - Get custom model details
    POST /api/external/v1/user-models/<id>/jobs - Submit job to custom model

    # Search
    GET  /api/external/v1/search                - Search across all resources

    # API Keys Management
    GET  /api/external/v1/keys                  - List your API keys
    POST /api/external/v1/keys                  - Create new API key
    DELETE /api/external/v1/keys/<key_id>       - Revoke API key
"""

import json
import os
import time
import uuid
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import jsonify, request, send_file, g
from werkzeug.utils import secure_filename

from ..app import app, logger, get_redis_connection
from ..helpers.api_keys import (
    verify_unified_api_key,
    check_rate_limit,
    log_api_usage,
    create_api_key,
    delete_api_key,
    get_user_api_keys
)
from ..helpers.database import get_db
from ..helpers.paths import get_safe_project_path, is_path_within

# =============================================================================
# CONSTANTS
# =============================================================================

API_VERSION = '1.0.0'
API_PREFIX = '/api/external/v1'

# Model type mapping
MODEL_TYPE_MAP = {
    'esmfold': 'type-0',
    'alphafold2': 'type-2',
    'diffdock': 'type-3',
    'boltz2': 'type-boltz2',
    'evo2': 'type-evo2',
    'evo2-40b': 'type-evo2'
}

REVERSE_MODEL_MAP = {v: k for k, v in MODEL_TYPE_MAP.items()}

# Model info for documentation
MODEL_INFO = {
    'type-0': {
        'id': 'esmfold',
        'name': 'ESMFold',
        'description': 'Fast protein structure prediction from Meta AI',
        'type': 'structure_prediction',
        'input': 'protein_sequence',
        'output': 'pdb_structure',
        'max_length': 1000
    },
    'type-2': {
        'id': 'alphafold2',
        'name': 'AlphaFold2',
        'description': 'High-accuracy protein structure prediction from DeepMind',
        'type': 'structure_prediction',
        'input': 'protein_sequence',
        'output': 'pdb_structure',
        'max_length': 2500
    },
    'type-3': {
        'id': 'diffdock',
        'name': 'DiffDock',
        'description': 'Molecular docking for protein-ligand interactions',
        'type': 'docking',
        'input': 'protein_structure,ligand',
        'output': 'docked_structure'
    },
    'type-boltz2': {
        'id': 'boltz2',
        'name': 'Boltz2',
        'description': 'Protein structure prediction with Boltz2',
        'type': 'structure_prediction',
        'input': 'protein_sequence',
        'output': 'pdb_structure'
    },
    'type-evo2': {
        'id': 'evo2',
        'name': 'Evo2-40B',
        'description': 'DNA sequence generation model',
        'type': 'sequence_generation',
        'input': 'dna_sequence',
        'output': 'generated_sequences'
    }
}

# =============================================================================
# AUTHENTICATION DECORATOR
# =============================================================================

def require_external_api_key(f):
    """
    Decorator to require and validate API key for external API endpoints.

    Supports:
    - Public API keys (pk_*)
    - Worker API keys (wk_*)
    - Model API keys (mk_*)
    - Project API keys (pj_*)

    Sets request context:
    - request.api_user_id: User ID
    - request.api_key_id: Key ID in database
    - request.auth_context: Full authentication context
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()

        # Get API key from Authorization header
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return jsonify({
                'status': 'error',
                'error': 'missing_authorization',
                'message': 'Missing or invalid Authorization header. Use: Authorization: Bearer YOUR_API_KEY'
            }), 401

        api_key = auth_header.replace('Bearer ', '').strip()

        if not api_key:
            return jsonify({
                'status': 'error',
                'error': 'empty_api_key',
                'message': 'API key cannot be empty'
            }), 401

        # Verify API key
        auth_context = verify_unified_api_key(api_key)

        if not auth_context:
            return jsonify({
                'status': 'error',
                'error': 'invalid_api_key',
                'message': 'Invalid or deactivated API key'
            }), 401

        # Check rate limit
        if not check_rate_limit(auth_context['key_id'], auth_context['rate_limit']):
            return jsonify({
                'status': 'error',
                'error': 'rate_limit_exceeded',
                'message': f'Rate limit exceeded ({auth_context["rate_limit"]} requests/minute). Please wait and try again.'
            }), 429

        # Set request context
        request.api_user_id = auth_context['user_id']
        request.api_key_id = auth_context['key_id']
        request.auth_context = auth_context
        request.api_start_time = start_time

        return f(*args, **kwargs)

    return decorated_function


def log_api_request(endpoint, status_code, model_type=None, credits_used=0):
    """Helper to log API request after completion."""
    if hasattr(request, 'api_start_time'):
        response_time = int((time.time() - request.api_start_time) * 1000)
    else:
        response_time = 0

    log_api_usage(
        api_key_id=request.api_key_id,
        user_id=request.api_user_id,
        endpoint=endpoint,
        model_type=model_type or 'general',
        status_code=status_code,
        response_time_ms=response_time,
        credits_used=credits_used,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent')
    )


# =============================================================================
# API INFO & HEALTH
# =============================================================================

@app.route(f'{API_PREFIX}/info', methods=['GET'])
def external_api_info():
    """
    Get API information and available endpoints.

    No authentication required.

    Response:
        {
            "status": "success",
            "api": {
                "name": "Phonos External API",
                "version": "1.0.0",
                "documentation": "/api/docs"
            }
        }
    """
    return jsonify({
        'status': 'success',
        'api': {
            'name': 'Phonos External API',
            'version': API_VERSION,
            'documentation': '/api/docs',
            'base_url': API_PREFIX,
            'authentication': 'Bearer token (API key)',
            'endpoints': {
                'user': ['/me', '/usage', '/credits'],
                'models': ['/models'],
                'jobs': ['/jobs', '/jobs/<job_id>', '/jobs/<job_id>/status', '/jobs/<job_id>/result'],
                'projects': ['/projects', '/projects/<id>', '/projects/<id>/jobs', '/projects/<id>/files'],
                'files': ['/files', '/files/<id>', '/files/<id>/download'],
                'user_models': ['/user-models', '/user-models/<id>'],
                'search': ['/search'],
                'keys': ['/keys']
            }
        }
    })


# =============================================================================
# USER & ACCOUNT ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/me', methods=['GET'])
@require_external_api_key
def external_api_me():
    """
    Get current user information.

    Response:
        {
            "status": "success",
            "user": {
                "id": 123,
                "username": "johndoe",
                "email": "john@example.com",
                "plan": "professional",
                "created_at": "2024-01-01T00:00:00Z",
                "credits_remaining": 1000
            }
        }
    """
    try:
        db = get_db()

        user = db.execute("""
            SELECT id, username, email, plan, credits, created_at,
                   bio, institution, profile_picture
            FROM users
            WHERE id = %s
        """, (request.api_user_id,)).fetchone()

        if not user:
            return jsonify({
                'status': 'error',
                'error': 'user_not_found',
                'message': 'User not found'
            }), 404

        log_api_request(f'{API_PREFIX}/me', 200)

        return jsonify({
            'status': 'success',
            'user': {
                'id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'plan': user['plan'] or 'free',
                'credits_remaining': user['credits'] or 0,
                'bio': user['bio'],
                'institution': user['institution'],
                'profile_picture': user['profile_picture'],
                'created_at': user['created_at'].isoformat() if user['created_at'] else None
            }
        })

    except Exception as e:
        logger.error(f"Error in /me endpoint: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error',
            'message': 'Failed to retrieve user information'
        }), 500


@app.route(f'{API_PREFIX}/usage', methods=['GET'])
@require_external_api_key
def external_api_usage():
    """
    Get API usage statistics.

    Query Parameters:
        - period: 'day', 'week', 'month', 'year' (default: 'month')
        - key_id: Specific API key ID (optional)

    Response:
        {
            "status": "success",
            "usage": {
                "total_requests": 1500,
                "total_credits_used": 500,
                "period": "month",
                "by_endpoint": {...},
                "by_model": {...}
            }
        }
    """
    try:
        db = get_db()
        period = request.args.get('period', 'month')
        key_id = request.args.get('key_id')

        # Determine time filter
        time_filters = {
            'day': "timestamp > CURRENT_TIMESTAMP - INTERVAL '1 day'",
            'week': "timestamp > CURRENT_TIMESTAMP - INTERVAL '7 days'",
            'month': "timestamp > CURRENT_TIMESTAMP - INTERVAL '30 days'",
            'year': "timestamp > CURRENT_TIMESTAMP - INTERVAL '365 days'"
        }
        time_filter = time_filters.get(period, time_filters['month'])

        # Build query
        base_where = f"user_id = %s AND {time_filter}"
        params = [request.api_user_id]

        if key_id:
            base_where += " AND api_key_id = %s"
            params.append(key_id)

        # Get totals
        totals = db.execute(f"""
            SELECT
                COUNT(*) as total_requests,
                COALESCE(SUM(credits_used), 0) as total_credits_used,
                COALESCE(AVG(response_time_ms), 0) as avg_response_time
            FROM api_usage
            WHERE {base_where}
        """, tuple(params)).fetchone()

        # Get by endpoint
        by_endpoint = {}
        endpoints = db.execute(f"""
            SELECT endpoint, COUNT(*) as count
            FROM api_usage
            WHERE {base_where}
            GROUP BY endpoint
            ORDER BY count DESC
            LIMIT 10
        """, tuple(params)).fetchall()

        for row in endpoints:
            by_endpoint[row['endpoint']] = row['count']

        # Get by model
        by_model = {}
        models = db.execute(f"""
            SELECT model_type, COUNT(*) as count, COALESCE(SUM(credits_used), 0) as credits
            FROM api_usage
            WHERE {base_where} AND model_type != 'general'
            GROUP BY model_type
            ORDER BY count DESC
        """, tuple(params)).fetchall()

        for row in models:
            model_name = REVERSE_MODEL_MAP.get(row['model_type'], row['model_type'])
            by_model[model_name] = {
                'requests': row['count'],
                'credits_used': row['credits']
            }

        log_api_request(f'{API_PREFIX}/usage', 200)

        return jsonify({
            'status': 'success',
            'usage': {
                'total_requests': totals['total_requests'] or 0,
                'total_credits_used': int(totals['total_credits_used'] or 0),
                'avg_response_time_ms': int(totals['avg_response_time'] or 0),
                'period': period,
                'by_endpoint': by_endpoint,
                'by_model': by_model
            }
        })

    except Exception as e:
        logger.error(f"Error in /usage endpoint: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error',
            'message': 'Failed to retrieve usage statistics'
        }), 500


@app.route(f'{API_PREFIX}/credits', methods=['GET'])
@require_external_api_key
def external_api_credits():
    """
    Get credit balance and usage summary.

    Response:
        {
            "status": "success",
            "credits": {
                "balance": 1000,
                "used_this_month": 250,
                "plan_limit": 5000,
                "reset_date": "2024-02-01"
            }
        }
    """
    try:
        db = get_db()

        user = db.execute("""
            SELECT credits, plan, monthly_credits_used, credits_reset_at
            FROM users
            WHERE id = %s
        """, (request.api_user_id,)).fetchone()

        if not user:
            return jsonify({
                'status': 'error',
                'error': 'user_not_found'
            }), 404

        # Get plan limits
        plan_limits = {
            'free': 100,
            'starter': 1000,
            'professional': 5000,
            'enterprise': 50000
        }

        plan = user['plan'] or 'free'
        plan_limit = plan_limits.get(plan, 100)

        log_api_request(f'{API_PREFIX}/credits', 200)

        return jsonify({
            'status': 'success',
            'credits': {
                'balance': user['credits'] or 0,
                'used_this_month': user['monthly_credits_used'] or 0,
                'plan': plan,
                'plan_limit': plan_limit,
                'reset_date': user['credits_reset_at'].isoformat() if user['credits_reset_at'] else None
            }
        })

    except Exception as e:
        logger.error(f"Error in /credits endpoint: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


# =============================================================================
# MODELS ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/models', methods=['GET'])
@require_external_api_key
def external_api_list_models():
    """
    List available prediction models.

    Response:
        {
            "status": "success",
            "models": [
                {
                    "id": "esmfold",
                    "name": "ESMFold",
                    "description": "Fast protein structure prediction",
                    "type": "structure_prediction",
                    "input": "protein_sequence",
                    "output": "pdb_structure"
                },
                ...
            ]
        }
    """
    models = []
    for internal_type, info in MODEL_INFO.items():
        models.append(info.copy())

    log_api_request(f'{API_PREFIX}/models', 200)

    return jsonify({
        'status': 'success',
        'models': models
    })


@app.route(f'{API_PREFIX}/models/<model_id>', methods=['GET'])
@require_external_api_key
def external_api_get_model(model_id):
    """
    Get detailed information about a specific model.

    Path Parameters:
        model_id: Model identifier (e.g., 'esmfold', 'alphafold2')

    Response:
        {
            "status": "success",
            "model": {
                "id": "esmfold",
                "name": "ESMFold",
                "description": "...",
                "parameters": {...}
            }
        }
    """
    internal_type = MODEL_TYPE_MAP.get(model_id.lower())

    if not internal_type or internal_type not in MODEL_INFO:
        return jsonify({
            'status': 'error',
            'error': 'model_not_found',
            'message': f'Model "{model_id}" not found. Available: {", ".join(MODEL_TYPE_MAP.keys())}'
        }), 404

    model_info = MODEL_INFO[internal_type].copy()

    log_api_request(f'{API_PREFIX}/models/{model_id}', 200)

    return jsonify({
        'status': 'success',
        'model': model_info
    })


# =============================================================================
# JOBS ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/jobs', methods=['GET'])
@require_external_api_key
def external_api_list_jobs():
    """
    List user's jobs with filtering and pagination.

    Query Parameters:
        - project_id: Filter by project (optional)
        - model: Filter by model type (optional)
        - status: Filter by status (optional)
        - limit: Max results (default: 50, max: 200)
        - offset: Pagination offset (default: 0)
        - sort: Sort field (default: 'created_at')
        - order: 'asc' or 'desc' (default: 'desc')

    Response:
        {
            "status": "success",
            "jobs": [...],
            "pagination": {
                "total": 150,
                "limit": 50,
                "offset": 0,
                "has_more": true
            }
        }
    """
    try:
        db = get_db()

        # Parse query parameters
        project_id = request.args.get('project_id', type=int)
        model = request.args.get('model')
        status = request.args.get('status')
        limit = min(request.args.get('limit', 50, type=int), 200)
        offset = request.args.get('offset', 0, type=int)
        sort = request.args.get('sort', 'created_at')
        order = request.args.get('order', 'desc').upper()

        # Build query
        where_clauses = ["j.creator_id = %s"]
        params = [request.api_user_id]

        if project_id:
            where_clauses.append("j.project_id = %s")
            params.append(project_id)

        if model:
            internal_model = MODEL_TYPE_MAP.get(model.lower())
            if internal_model:
                where_clauses.append("j.model = %s")
                params.append(internal_model)

        if status:
            where_clauses.append("j.status = %s")
            params.append(status)

        where_sql = " AND ".join(where_clauses)

        # Validate sort field
        valid_sorts = ['created_at', 'updated_at', 'name', 'status']
        if sort not in valid_sorts:
            sort = 'created_at'

        if order not in ['ASC', 'DESC']:
            order = 'DESC'

        # Get total count
        count_result = db.execute(f"""
            SELECT COUNT(*) as total
            FROM jobs j
            WHERE {where_sql}
        """, tuple(params)).fetchone()
        total = count_result['total']

        # Get jobs
        params.extend([limit, offset])
        jobs_result = db.execute(f"""
            SELECT j.id, j.job_id, j.name, j.model, j.status, j.sequence,
                   j.created_at, j.updated_at, j.execution_time,
                   j.output_file, j.output_display_name, j.output_file_size,
                   j.project_id, p.name as project_name
            FROM jobs j
            LEFT JOIN projects p ON j.project_id = p.id
            WHERE {where_sql}
            ORDER BY j.{sort} {order}
            LIMIT %s OFFSET %s
        """, tuple(params))

        jobs = []
        for row in jobs_result.fetchall():
            jobs.append({
                'id': row['id'],
                'job_id': row['job_id'],
                'name': row['name'],
                'model': REVERSE_MODEL_MAP.get(row['model'], row['model']),
                'model_type': row['model'],
                'status': row['status'],
                'sequence_length': len(row['sequence']) if row['sequence'] else 0,
                'project_id': row['project_id'],
                'project_name': row['project_name'],
                'has_output': bool(row['output_file']),
                'output_size': row['output_file_size'],
                'execution_time': row['execution_time'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None
            })

        log_api_request(f'{API_PREFIX}/jobs', 200)

        return jsonify({
            'status': 'success',
            'jobs': jobs,
            'pagination': {
                'total': total,
                'limit': limit,
                'offset': offset,
                'has_more': (offset + limit) < total
            }
        })

    except Exception as e:
        logger.error(f"Error listing jobs: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error',
            'message': 'Failed to list jobs'
        }), 500


@app.route(f'{API_PREFIX}/jobs', methods=['POST'])
@require_external_api_key
def external_api_submit_job():
    """
    Submit a new prediction job.

    Request Body:
        {
            "model": "esmfold",              // Required: model to use
            "sequence": "MKFLIL...",          // Required: input sequence
            "name": "My Protein",             // Optional: job name
            "project_id": 123,                // Optional: project to add job to
            "params": {}                      // Optional: model-specific parameters
        }

    Response:
        {
            "status": "success",
            "job": {
                "job_id": "uuid-string",
                "model": "esmfold",
                "status": "pending",
                "created_at": "..."
            }
        }
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'error': 'no_data',
                'message': 'No JSON data provided'
            }), 400

        model = data.get('model')
        sequence = data.get('sequence')

        if not model:
            return jsonify({
                'status': 'error',
                'error': 'missing_model',
                'message': 'Missing required field: model'
            }), 400

        if not sequence:
            return jsonify({
                'status': 'error',
                'error': 'missing_sequence',
                'message': 'Missing required field: sequence'
            }), 400

        # Map model name to internal type
        internal_model = MODEL_TYPE_MAP.get(model.lower())
        if not internal_model:
            return jsonify({
                'status': 'error',
                'error': 'invalid_model',
                'message': f'Invalid model "{model}". Available: {", ".join(MODEL_TYPE_MAP.keys())}'
            }), 400

        db = get_db()

        # Get or create project
        project_id = data.get('project_id')

        if not project_id:
            # Use or create "API Requests" project
            project = db.execute("""
                SELECT id FROM projects
                WHERE creator_id = %s AND name = 'API Requests'
                LIMIT 1
            """, (request.api_user_id,)).fetchone()

            if not project:
                result = db.execute("""
                    INSERT INTO projects (creator_id, name, description)
                    VALUES (%s, 'API Requests', 'Jobs submitted via External API')
                    RETURNING id
                """, (request.api_user_id,))
                project = result.fetchone()
                db.commit()

            project_id = project['id']
        else:
            # Verify user has access to project
            access = db.execute("""
                SELECT 1 FROM project_assignments
                WHERE project_id = %s AND user_id = %s
            """, (project_id, request.api_user_id)).fetchone()

            if not access:
                return jsonify({
                    'status': 'error',
                    'error': 'project_not_accessible',
                    'message': 'You do not have access to this project'
                }), 403

        # Create job
        job_uuid = str(uuid.uuid4())
        job_name = data.get('name', f'API Job - {model}')
        params = data.get('params', {})

        # Clean sequence (remove FASTA headers if present)
        clean_sequence = sequence
        if '>' in sequence:
            lines = []
            for line in sequence.splitlines():
                if not line.strip().startswith('>'):
                    lines.append(line.strip())
            clean_sequence = ''.join(lines)
        clean_sequence = ''.join(clean_sequence.split())

        # Insert job with source='api' to identify API-submitted jobs
        result = db.execute("""
            INSERT INTO jobs (job_id, creator_id, project_id, name, sequence, model, status, job_data, source)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, 'api')
            RETURNING id, created_at
        """, (job_uuid, request.api_user_id, project_id, job_name, clean_sequence,
              internal_model, json.dumps(params)))

        job_row = result.fetchone()
        job_db_id = job_row['id']
        created_at = job_row['created_at']

        # Queue job for processing
        try:
            redis_client = get_redis_connection()
            if redis_client:
                job_payload = {
                    'job_id': job_uuid,
                    'job_db_id': job_db_id,
                    'user_id': request.api_user_id,
                    'model': internal_model,
                    'sequence': clean_sequence,
                    'params': params,
                    'name': job_name
                }
                redis_client.lpush(f'phonos:jobs:{internal_model}', json.dumps(job_payload))
        except Exception as redis_error:
            logger.warning(f"Failed to queue job to Redis: {redis_error}")

        db.commit()

        log_api_request(f'{API_PREFIX}/jobs', 201, model_type=internal_model)

        return jsonify({
            'status': 'success',
            'job': {
                'job_id': job_uuid,
                'job_db_id': job_db_id,
                'model': model,
                'model_type': internal_model,
                'name': job_name,
                'sequence_length': len(clean_sequence),
                'status': 'pending',
                'project_id': project_id,
                'created_at': created_at.isoformat() if created_at else None
            }
        }), 201

    except Exception as e:
        logger.error(f"Error submitting job: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error',
            'message': 'Failed to submit job'
        }), 500


@app.route(f'{API_PREFIX}/jobs/<job_id>', methods=['GET'])
@require_external_api_key
def external_api_get_job(job_id):
    """
    Get detailed information about a specific job.

    Path Parameters:
        job_id: Job UUID

    Response:
        {
            "status": "success",
            "job": {
                "job_id": "uuid",
                "name": "My Protein",
                "model": "esmfold",
                "status": "completed",
                "sequence": "MKFLIL...",
                "result": {...},
                "created_at": "...",
                "completed_at": "..."
            }
        }
    """
    try:
        db = get_db()

        job = db.execute("""
            SELECT j.*, p.name as project_name
            FROM jobs j
            LEFT JOIN projects p ON j.project_id = p.id
            WHERE j.job_id = %s AND j.creator_id = %s
        """, (job_id, request.api_user_id)).fetchone()

        if not job:
            return jsonify({
                'status': 'error',
                'error': 'job_not_found',
                'message': f'Job "{job_id}" not found or not accessible'
            }), 404

        # Parse message if it contains JSON result
        result_data = None
        if job['message']:
            try:
                result_data = json.loads(job['message'])
            except:
                result_data = {'message': job['message']}

        log_api_request(f'{API_PREFIX}/jobs/{job_id}', 200)

        return jsonify({
            'status': 'success',
            'job': {
                'id': job['id'],
                'job_id': job['job_id'],
                'name': job['name'],
                'model': REVERSE_MODEL_MAP.get(job['model'], job['model']),
                'model_type': job['model'],
                'status': job['status'],
                'sequence': job['sequence'],
                'sequence_length': len(job['sequence']) if job['sequence'] else 0,
                'project_id': job['project_id'],
                'project_name': job['project_name'],
                'has_output': bool(job['output_file']),
                'output_file_name': job['output_display_name'],
                'output_file_size': job['output_file_size'],
                'execution_time': job['execution_time'],
                'result': result_data,
                'created_at': job['created_at'].isoformat() if job['created_at'] else None,
                'updated_at': job['updated_at'].isoformat() if job['updated_at'] else None
            }
        })

    except Exception as e:
        logger.error(f"Error getting job {job_id}: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error',
            'message': 'Failed to get job details'
        }), 500


@app.route(f'{API_PREFIX}/jobs/<job_id>/status', methods=['GET'])
@require_external_api_key
def external_api_job_status(job_id):
    """
    Get job status (lightweight endpoint for polling).

    Path Parameters:
        job_id: Job UUID

    Response:
        {
            "status": "success",
            "job_id": "uuid",
            "job_status": "completed",
            "progress": 100,
            "message": "Job completed successfully"
        }
    """
    try:
        db = get_db()

        job = db.execute("""
            SELECT job_id, status, execution_time, message
            FROM jobs
            WHERE job_id = %s AND creator_id = %s
        """, (job_id, request.api_user_id)).fetchone()

        if not job:
            return jsonify({
                'status': 'error',
                'error': 'job_not_found'
            }), 404

        # Determine progress
        progress_map = {
            'pending': 0,
            'queued': 10,
            'processing': 50,
            'completed': 100,
            'failed': 100,
            'cancelled': 100
        }

        log_api_request(f'{API_PREFIX}/jobs/{job_id}/status', 200)

        return jsonify({
            'status': 'success',
            'job_id': job['job_id'],
            'job_status': job['status'],
            'progress': progress_map.get(job['status'], 0),
            'execution_time': job['execution_time'],
            'is_complete': job['status'] in ['completed', 'failed', 'cancelled']
        })

    except Exception as e:
        logger.error(f"Error getting job status: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/jobs/<job_id>/result', methods=['GET'])
@require_external_api_key
def external_api_job_result(job_id):
    """
    Get job result/output.

    Path Parameters:
        job_id: Job UUID

    Query Parameters:
        - format: 'json' (default) or 'file'

    Response (format=json):
        {
            "status": "success",
            "job_id": "uuid",
            "result": {
                "pdb_data": "...",
                "scores": {...}
            }
        }

    Response (format=file):
        Returns the output file directly
    """
    try:
        db = get_db()
        format_type = request.args.get('format', 'json')

        job = db.execute("""
            SELECT j.job_id, j.status, j.output_file, j.output_display_name, j.message,
                   j.project_id, p.team_id
            FROM jobs j
            LEFT JOIN projects p ON p.id = j.project_id
            WHERE j.job_id = %s AND j.creator_id = %s
        """, (job_id, request.api_user_id)).fetchone()

        if not job:
            return jsonify({
                'status': 'error',
                'error': 'job_not_found'
            }), 404

        if job['status'] != 'completed':
            return jsonify({
                'status': 'error',
                'error': 'job_not_complete',
                'message': f'Job status is "{job["status"]}". Results are only available for completed jobs.',
                'job_status': job['status']
            }), 400

        if not job['output_file'] or not os.path.exists(job['output_file']):
            return jsonify({
                'status': 'error',
                'error': 'no_output',
                'message': 'No output file available for this job'
            }), 404

        project_path = get_safe_project_path(job['project_id'], job['team_id']) if job['team_id'] is not None else None
        if not project_path or not is_path_within(job['output_file'], project_path):
            logger.warning("Blocked external API job output outside project root: job=%s", job_id)
            return jsonify({
                'status': 'error',
                'error': 'forbidden_path',
                'message': 'Output file path is not allowed'
            }), 403

        log_api_request(f'{API_PREFIX}/jobs/{job_id}/result', 200)

        if format_type == 'file':
            # Return file directly
            return send_file(
                job['output_file'],
                as_attachment=True,
                download_name=job['output_display_name'] or f'{job_id}.pdb'
            )

        # Return JSON with result data
        result_data = None
        if job['message']:
            try:
                result_data = json.loads(job['message'])
            except:
                result_data = {'message': job['message']}

        # Read PDB content if it's a structure file
        pdb_content = None
        if job['output_file'].endswith('.pdb'):
            try:
                with open(job['output_file'], 'r') as f:
                    pdb_content = f.read()
            except:
                pass

        return jsonify({
            'status': 'success',
            'job_id': job['job_id'],
            'output_file_name': job['output_display_name'],
            'result': result_data,
            'pdb_data': pdb_content
        })

    except Exception as e:
        logger.error(f"Error getting job result: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/jobs/<job_id>', methods=['DELETE'])
@require_external_api_key
def external_api_delete_job(job_id):
    """
    Delete a job and its output files.

    Path Parameters:
        job_id: Job UUID

    Response:
        {
            "status": "success",
            "message": "Job deleted successfully"
        }
    """
    try:
        db = get_db()

        job = db.execute("""
            SELECT id, job_id, output_file, creator_id
            FROM jobs
            WHERE job_id = %s AND creator_id = %s
        """, (job_id, request.api_user_id)).fetchone()

        if not job:
            return jsonify({
                'status': 'error',
                'error': 'job_not_found'
            }), 404

        # Delete output file if exists
        if job['output_file'] and os.path.exists(job['output_file']):
            try:
                os.remove(job['output_file'])
            except Exception as e:
                logger.warning(f"Failed to delete job output file: {e}")

        # Delete job record
        db.execute("DELETE FROM jobs WHERE id = %s", (job['id'],))
        db.commit()

        log_api_request(f'{API_PREFIX}/jobs/{job_id}', 200)

        return jsonify({
            'status': 'success',
            'message': 'Job deleted successfully',
            'job_id': job_id
        })

    except Exception as e:
        logger.error(f"Error deleting job: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/jobs/<job_id>/cancel', methods=['POST'])
@require_external_api_key
def external_api_cancel_job(job_id):
    """
    Cancel a pending or processing job.

    Path Parameters:
        job_id: Job UUID

    Response:
        {
            "status": "success",
            "message": "Job cancelled successfully"
        }
    """
    try:
        db = get_db()

        job = db.execute("""
            SELECT id, job_id, status
            FROM jobs
            WHERE job_id = %s AND creator_id = %s
        """, (job_id, request.api_user_id)).fetchone()

        if not job:
            return jsonify({
                'status': 'error',
                'error': 'job_not_found'
            }), 404

        if job['status'] in ['completed', 'failed', 'cancelled']:
            return jsonify({
                'status': 'error',
                'error': 'cannot_cancel',
                'message': f'Cannot cancel job with status "{job["status"]}"'
            }), 400

        # Update job status
        db.execute("""
            UPDATE jobs
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (job['id'],))
        db.commit()

        log_api_request(f'{API_PREFIX}/jobs/{job_id}/cancel', 200)

        return jsonify({
            'status': 'success',
            'message': 'Job cancelled successfully',
            'job_id': job_id
        })

    except Exception as e:
        logger.error(f"Error cancelling job: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


# =============================================================================
# PROJECTS ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/projects', methods=['GET'])
@require_external_api_key
def external_api_list_projects():
    """
    List user's projects.

    Query Parameters:
        - limit: Max results (default: 50, max: 200)
        - offset: Pagination offset
        - include_archived: Include archived projects (default: false)

    Response:
        {
            "status": "success",
            "projects": [...]
        }
    """
    try:
        db = get_db()

        limit = min(request.args.get('limit', 50, type=int), 200)
        offset = request.args.get('offset', 0, type=int)
        include_archived = request.args.get('include_archived', 'false').lower() == 'true'

        where_clause = """
            pa.user_id = %s
        """
        params = [request.api_user_id]

        if not include_archived:
            where_clause += " AND (p.is_archived = false OR p.is_archived IS NULL)"

        # Get total count
        count = db.execute(f"""
            SELECT COUNT(*) as total
            FROM projects p
            JOIN project_assignments pa ON p.id = pa.project_id
            WHERE {where_clause}
        """, tuple(params)).fetchone()['total']

        # Get projects
        params.extend([limit, offset])
        projects_result = db.execute(f"""
            SELECT p.id, p.name, p.description, p.is_private, p.is_archived,
                   p.created_at, p.updated_at, pa.role,
                   (SELECT COUNT(*) FROM jobs WHERE project_id = p.id) as job_count,
                   (SELECT COUNT(*) FROM files WHERE project_id = p.id) as file_count
            FROM projects p
            JOIN project_assignments pa ON p.id = pa.project_id
            WHERE {where_clause}
            ORDER BY p.updated_at DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, tuple(params))

        projects = []
        for row in projects_result.fetchall():
            projects.append({
                'id': row['id'],
                'name': row['name'],
                'description': row['description'],
                'is_private': bool(row['is_private']),
                'is_archived': bool(row['is_archived']),
                'role': row['role'],
                'job_count': row['job_count'],
                'file_count': row['file_count'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None
            })

        log_api_request(f'{API_PREFIX}/projects', 200)

        return jsonify({
            'status': 'success',
            'projects': projects,
            'pagination': {
                'total': count,
                'limit': limit,
                'offset': offset,
                'has_more': (offset + limit) < count
            }
        })

    except Exception as e:
        logger.error(f"Error listing projects: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/projects', methods=['POST'])
@require_external_api_key
def external_api_create_project():
    """
    Create a new project.

    Request Body:
        {
            "name": "My Project",
            "description": "Project description",
            "is_private": true
        }

    Response:
        {
            "status": "success",
            "project": {...}
        }
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'error': 'no_data'
            }), 400

        name = data.get('name')
        if not name:
            return jsonify({
                'status': 'error',
                'error': 'missing_name',
                'message': 'Project name is required'
            }), 400

        description = data.get('description', '')
        is_private = data.get('is_private', True)

        db = get_db()

        # Create project
        result = db.execute("""
            INSERT INTO projects (creator_id, name, description, is_private)
            VALUES (%s, %s, %s, %s)
            RETURNING id, created_at
        """, (request.api_user_id, name, description, is_private))

        project = result.fetchone()
        project_id = project['id']

        # Add creator as manager
        db.execute("""
            INSERT INTO project_assignments (project_id, user_id, role)
            VALUES (%s, %s, 'Project Manager')
        """, (project_id, request.api_user_id))

        db.commit()

        log_api_request(f'{API_PREFIX}/projects', 201)

        return jsonify({
            'status': 'success',
            'project': {
                'id': project_id,
                'name': name,
                'description': description,
                'is_private': is_private,
                'role': 'Project Manager',
                'created_at': project['created_at'].isoformat() if project['created_at'] else None
            }
        }), 201

    except Exception as e:
        logger.error(f"Error creating project: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/projects/<int:project_id>', methods=['GET'])
@require_external_api_key
def external_api_get_project(project_id):
    """
    Get project details.

    Path Parameters:
        project_id: Project ID

    Response:
        {
            "status": "success",
            "project": {...}
        }
    """
    try:
        db = get_db()

        project = db.execute("""
            SELECT p.*, pa.role,
                   (SELECT COUNT(*) FROM jobs WHERE project_id = p.id) as job_count,
                   (SELECT COUNT(*) FROM files WHERE project_id = p.id) as file_count,
                   (SELECT COUNT(*) FROM project_assignments WHERE project_id = p.id) as member_count
            FROM projects p
            JOIN project_assignments pa ON p.id = pa.project_id
            WHERE p.id = %s AND pa.user_id = %s
        """, (project_id, request.api_user_id)).fetchone()

        if not project:
            return jsonify({
                'status': 'error',
                'error': 'project_not_found'
            }), 404

        log_api_request(f'{API_PREFIX}/projects/{project_id}', 200)

        return jsonify({
            'status': 'success',
            'project': {
                'id': project['id'],
                'name': project['name'],
                'description': project['description'],
                'is_private': bool(project['is_private']),
                'is_archived': bool(project['is_archived']),
                'role': project['role'],
                'job_count': project['job_count'],
                'file_count': project['file_count'],
                'member_count': project['member_count'],
                'created_at': project['created_at'].isoformat() if project['created_at'] else None,
                'updated_at': project['updated_at'].isoformat() if project['updated_at'] else None
            }
        })

    except Exception as e:
        logger.error(f"Error getting project: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/projects/<int:project_id>', methods=['PUT'])
@require_external_api_key
def external_api_update_project(project_id):
    """
    Update project details.

    Path Parameters:
        project_id: Project ID

    Request Body:
        {
            "name": "New Name",
            "description": "New description",
            "is_private": false
        }

    Response:
        {
            "status": "success",
            "project": {...}
        }
    """
    try:
        data = request.get_json() or {}
        db = get_db()

        # Check access (need manager role)
        access = db.execute("""
            SELECT role FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        """, (project_id, request.api_user_id)).fetchone()

        if not access:
            return jsonify({
                'status': 'error',
                'error': 'project_not_found'
            }), 404

        if access['role'] not in ['Project Manager']:
            return jsonify({
                'status': 'error',
                'error': 'insufficient_permissions',
                'message': 'Only Project Managers can update projects'
            }), 403

        # Build update query
        updates = []
        params = []

        if 'name' in data:
            updates.append("name = %s")
            params.append(data['name'])

        if 'description' in data:
            updates.append("description = %s")
            params.append(data['description'])

        if 'is_private' in data:
            updates.append("is_private = %s")
            params.append(data['is_private'])

        if not updates:
            return jsonify({
                'status': 'error',
                'error': 'no_updates',
                'message': 'No fields to update'
            }), 400

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(project_id)

        db.execute(f"""
            UPDATE projects
            SET {', '.join(updates)}
            WHERE id = %s
        """, tuple(params))
        db.commit()

        log_api_request(f'{API_PREFIX}/projects/{project_id}', 200)

        return jsonify({
            'status': 'success',
            'message': 'Project updated successfully'
        })

    except Exception as e:
        logger.error(f"Error updating project: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/projects/<int:project_id>', methods=['DELETE'])
@require_external_api_key
def external_api_delete_project(project_id):
    """
    Delete a project and all its jobs/files.

    Path Parameters:
        project_id: Project ID

    Response:
        {
            "status": "success",
            "message": "Project deleted successfully"
        }
    """
    try:
        db = get_db()

        # Check access (need manager role)
        access = db.execute("""
            SELECT role FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        """, (project_id, request.api_user_id)).fetchone()

        if not access:
            return jsonify({
                'status': 'error',
                'error': 'project_not_found'
            }), 404

        if access['role'] not in ['Project Manager']:
            return jsonify({
                'status': 'error',
                'error': 'insufficient_permissions',
                'message': 'Only Project Managers can delete projects'
            }), 403

        # Delete associated data
        db.execute("DELETE FROM jobs WHERE project_id = %s", (project_id,))
        db.execute("DELETE FROM files WHERE project_id = %s", (project_id,))
        db.execute("DELETE FROM project_assignments WHERE project_id = %s", (project_id,))
        db.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        db.commit()

        log_api_request(f'{API_PREFIX}/projects/{project_id}', 200)

        return jsonify({
            'status': 'success',
            'message': 'Project deleted successfully'
        })

    except Exception as e:
        logger.error(f"Error deleting project: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/projects/<int:project_id>/jobs', methods=['GET'])
@require_external_api_key
def external_api_project_jobs(project_id):
    """
    List jobs in a project.

    Path Parameters:
        project_id: Project ID

    Query Parameters:
        - limit: Max results (default: 50)
        - offset: Pagination offset
        - status: Filter by status

    Response:
        {
            "status": "success",
            "jobs": [...]
        }
    """
    try:
        db = get_db()

        # Check access
        access = db.execute("""
            SELECT 1 FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        """, (project_id, request.api_user_id)).fetchone()

        if not access:
            return jsonify({
                'status': 'error',
                'error': 'project_not_found'
            }), 404

        limit = min(request.args.get('limit', 50, type=int), 200)
        offset = request.args.get('offset', 0, type=int)
        status = request.args.get('status')

        where_clause = "j.project_id = %s"
        params = [project_id]

        if status:
            where_clause += " AND j.status = %s"
            params.append(status)

        # Get jobs
        params.extend([limit, offset])
        jobs_result = db.execute(f"""
            SELECT j.id, j.job_id, j.name, j.model, j.status,
                   j.created_at, j.updated_at, j.execution_time,
                   j.output_file, j.output_display_name
            FROM jobs j
            WHERE {where_clause}
            ORDER BY j.created_at DESC
            LIMIT %s OFFSET %s
        """, tuple(params))

        jobs = []
        for row in jobs_result.fetchall():
            jobs.append({
                'id': row['id'],
                'job_id': row['job_id'],
                'name': row['name'],
                'model': REVERSE_MODEL_MAP.get(row['model'], row['model']),
                'status': row['status'],
                'has_output': bool(row['output_file']),
                'output_file_name': row['output_display_name'],
                'execution_time': row['execution_time'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None
            })

        log_api_request(f'{API_PREFIX}/projects/{project_id}/jobs', 200)

        return jsonify({
            'status': 'success',
            'project_id': project_id,
            'jobs': jobs
        })

    except Exception as e:
        logger.error(f"Error listing project jobs: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/projects/<int:project_id>/files', methods=['GET'])
@require_external_api_key
def external_api_project_files(project_id):
    """
    List files in a project.

    Path Parameters:
        project_id: Project ID

    Response:
        {
            "status": "success",
            "files": [...]
        }
    """
    try:
        db = get_db()

        # Check access
        access = db.execute("""
            SELECT 1 FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        """, (project_id, request.api_user_id)).fetchone()

        if not access:
            return jsonify({
                'status': 'error',
                'error': 'project_not_found'
            }), 404

        files_result = db.execute("""
            SELECT id, name, type, size, created_at
            FROM files
            WHERE project_id = %s
            ORDER BY created_at DESC
        """, (project_id,))

        files = []
        for row in files_result.fetchall():
            files.append({
                'id': row['id'],
                'name': row['name'],
                'type': row['type'],
                'size': row['size'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None
            })

        log_api_request(f'{API_PREFIX}/projects/{project_id}/files', 200)

        return jsonify({
            'status': 'success',
            'project_id': project_id,
            'files': files
        })

    except Exception as e:
        logger.error(f"Error listing project files: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/projects/<int:project_id>/members', methods=['GET'])
@require_external_api_key
def external_api_project_members(project_id):
    """
    List project members.

    Path Parameters:
        project_id: Project ID

    Response:
        {
            "status": "success",
            "members": [...]
        }
    """
    try:
        db = get_db()

        # Check access
        access = db.execute("""
            SELECT 1 FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        """, (project_id, request.api_user_id)).fetchone()

        if not access:
            return jsonify({
                'status': 'error',
                'error': 'project_not_found'
            }), 404

        members_result = db.execute("""
            SELECT u.id, u.username, u.email, pa.role, pa.created_at as joined_at
            FROM project_assignments pa
            JOIN users u ON pa.user_id = u.id
            WHERE pa.project_id = %s
            ORDER BY pa.created_at
        """, (project_id,))

        members = []
        for row in members_result.fetchall():
            members.append({
                'user_id': row['id'],
                'username': row['username'],
                'email': row['email'],
                'role': row['role'],
                'joined_at': row['joined_at'].isoformat() if row['joined_at'] else None
            })

        log_api_request(f'{API_PREFIX}/projects/{project_id}/members', 200)

        return jsonify({
            'status': 'success',
            'project_id': project_id,
            'members': members
        })

    except Exception as e:
        logger.error(f"Error listing project members: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


# =============================================================================
# FILES ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/files', methods=['GET'])
@require_external_api_key
def external_api_list_files():
    """
    List user's files across all projects.

    Query Parameters:
        - project_id: Filter by project
        - type: Filter by file type
        - limit: Max results (default: 50)
        - offset: Pagination offset

    Response:
        {
            "status": "success",
            "files": [...]
        }
    """
    try:
        db = get_db()

        limit = min(request.args.get('limit', 50, type=int), 200)
        offset = request.args.get('offset', 0, type=int)
        project_id = request.args.get('project_id', type=int)
        file_type = request.args.get('type')

        # Build query - get files from user's projects
        where_clause = """
            f.project_id IN (
                SELECT project_id FROM project_assignments WHERE user_id = %s
            )
        """
        params = [request.api_user_id]

        if project_id:
            where_clause += " AND f.project_id = %s"
            params.append(project_id)

        if file_type:
            where_clause += " AND f.type = %s"
            params.append(file_type)

        # Get total
        count = db.execute(f"""
            SELECT COUNT(*) as total FROM files f WHERE {where_clause}
        """, tuple(params)).fetchone()['total']

        # Get files
        params.extend([limit, offset])
        files_result = db.execute(f"""
            SELECT f.id, f.name, f.type, f.size, f.created_at,
                   f.project_id, p.name as project_name
            FROM files f
            LEFT JOIN projects p ON f.project_id = p.id
            WHERE {where_clause}
            ORDER BY f.created_at DESC
            LIMIT %s OFFSET %s
        """, tuple(params))

        files = []
        for row in files_result.fetchall():
            files.append({
                'id': row['id'],
                'name': row['name'],
                'type': row['type'],
                'size': row['size'],
                'project_id': row['project_id'],
                'project_name': row['project_name'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None
            })

        log_api_request(f'{API_PREFIX}/files', 200)

        return jsonify({
            'status': 'success',
            'files': files,
            'pagination': {
                'total': count,
                'limit': limit,
                'offset': offset,
                'has_more': (offset + limit) < count
            }
        })

    except Exception as e:
        logger.error(f"Error listing files: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/files/upload', methods=['POST'])
@require_external_api_key
def external_api_upload_file():
    """
    Upload a file.

    Form Data:
        - file: File to upload (required)
        - project_id: Project ID to upload to (required)
        - name: Custom file name (optional)

    Response:
        {
            "status": "success",
            "file": {
                "id": 123,
                "name": "protein.pdb",
                "size": 12345
            }
        }
    """
    try:
        if 'file' not in request.files:
            return jsonify({
                'status': 'error',
                'error': 'no_file',
                'message': 'No file provided'
            }), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({
                'status': 'error',
                'error': 'no_filename'
            }), 400

        project_id = request.form.get('project_id', type=int)
        if not project_id:
            return jsonify({
                'status': 'error',
                'error': 'missing_project_id',
                'message': 'project_id is required'
            }), 400

        db = get_db()

        # Check project access
        access = db.execute("""
            SELECT role FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        """, (project_id, request.api_user_id)).fetchone()

        if not access:
            return jsonify({
                'status': 'error',
                'error': 'project_not_found'
            }), 404

        if access['role'] not in ['Project Manager', 'Contributor']:
            return jsonify({
                'status': 'error',
                'error': 'insufficient_permissions'
            }), 403

        # Save file
        from config import Config
        filename = secure_filename(file.filename)
        custom_name = request.form.get('name', filename)

        # Create project directory
        upload_dir = os.path.join(Config.UPLOAD_FOLDER, str(request.api_user_id), str(project_id))
        os.makedirs(upload_dir, exist_ok=True)

        # Generate unique filename
        unique_filename = f"{uuid.uuid4()}_{filename}"
        file_path = os.path.join(upload_dir, unique_filename)

        file.save(file_path)
        file_size = os.path.getsize(file_path)

        # Determine file type
        ext = os.path.splitext(filename)[1].lower()
        file_type = {
            '.pdb': 'structure',
            '.fasta': 'sequence',
            '.fa': 'sequence',
            '.sdf': 'ligand',
            '.mol2': 'ligand',
            '.csv': 'data',
            '.json': 'data'
        }.get(ext, 'other')

        # Insert record
        result = db.execute("""
            INSERT INTO files (project_id, user_id, name, path, size, type)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
        """, (project_id, request.api_user_id, custom_name, file_path, file_size, file_type))

        file_row = result.fetchone()
        db.commit()

        log_api_request(f'{API_PREFIX}/files/upload', 201)

        return jsonify({
            'status': 'success',
            'file': {
                'id': file_row['id'],
                'name': custom_name,
                'original_name': filename,
                'type': file_type,
                'size': file_size,
                'project_id': project_id,
                'created_at': file_row['created_at'].isoformat() if file_row['created_at'] else None
            }
        }), 201

    except Exception as e:
        logger.error(f"Error uploading file: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/files/<int:file_id>', methods=['GET'])
@require_external_api_key
def external_api_get_file(file_id):
    """
    Get file information.

    Path Parameters:
        file_id: File ID

    Response:
        {
            "status": "success",
            "file": {...}
        }
    """
    try:
        db = get_db()

        file_row = db.execute("""
            SELECT f.*, p.name as project_name
            FROM files f
            LEFT JOIN projects p ON f.project_id = p.id
            WHERE f.id = %s AND f.project_id IN (
                SELECT project_id FROM project_assignments WHERE user_id = %s
            )
        """, (file_id, request.api_user_id)).fetchone()

        if not file_row:
            return jsonify({
                'status': 'error',
                'error': 'file_not_found'
            }), 404

        log_api_request(f'{API_PREFIX}/files/{file_id}', 200)

        return jsonify({
            'status': 'success',
            'file': {
                'id': file_row['id'],
                'name': file_row['name'],
                'type': file_row['type'],
                'size': file_row['size'],
                'project_id': file_row['project_id'],
                'project_name': file_row['project_name'],
                'created_at': file_row['created_at'].isoformat() if file_row['created_at'] else None
            }
        })

    except Exception as e:
        logger.error(f"Error getting file: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/files/<int:file_id>/download', methods=['GET'])
@require_external_api_key
def external_api_download_file(file_id):
    """
    Download a file.

    Path Parameters:
        file_id: File ID

    Response:
        File content
    """
    try:
        db = get_db()

        file_row = db.execute("""
            SELECT f.name, f.path, f.project_id, p.team_id
            FROM files f
            JOIN projects p ON p.id = f.project_id
            WHERE f.id = %s AND f.project_id IN (
                SELECT project_id FROM project_assignments WHERE user_id = %s
            )
        """, (file_id, request.api_user_id)).fetchone()

        if not file_row:
            return jsonify({
                'status': 'error',
                'error': 'file_not_found'
            }), 404

        if not os.path.exists(file_row['path']):
            return jsonify({
                'status': 'error',
                'error': 'file_missing',
                'message': 'File no longer exists on disk'
            }), 404

        project_path = get_safe_project_path(file_row['project_id'], file_row['team_id'])
        if not project_path or not is_path_within(file_row['path'], project_path):
            logger.warning("Blocked external API file outside project root: file=%s", file_id)
            return jsonify({
                'status': 'error',
                'error': 'forbidden_path',
                'message': 'File path is not allowed'
            }), 403

        log_api_request(f'{API_PREFIX}/files/{file_id}/download', 200)

        return send_file(
            file_row['path'],
            as_attachment=True,
            download_name=file_row['name']
        )

    except Exception as e:
        logger.error(f"Error downloading file: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/files/<int:file_id>', methods=['DELETE'])
@require_external_api_key
def external_api_delete_file(file_id):
    """
    Delete a file.

    Path Parameters:
        file_id: File ID

    Response:
        {
            "status": "success",
            "message": "File deleted"
        }
    """
    try:
        db = get_db()

        # Check access (need contributor or manager role)
        file_row = db.execute("""
            SELECT f.id, f.path, f.project_id, pa.role
            FROM files f
            JOIN project_assignments pa ON f.project_id = pa.project_id
            WHERE f.id = %s AND pa.user_id = %s
        """, (file_id, request.api_user_id)).fetchone()

        if not file_row:
            return jsonify({
                'status': 'error',
                'error': 'file_not_found'
            }), 404

        if file_row['role'] not in ['Project Manager', 'Contributor']:
            return jsonify({
                'status': 'error',
                'error': 'insufficient_permissions'
            }), 403

        # Delete physical file
        if file_row['path'] and os.path.exists(file_row['path']):
            try:
                os.remove(file_row['path'])
            except:
                pass

        # Delete record
        db.execute("DELETE FROM files WHERE id = %s", (file_id,))
        db.commit()

        log_api_request(f'{API_PREFIX}/files/{file_id}', 200)

        return jsonify({
            'status': 'success',
            'message': 'File deleted successfully'
        })

    except Exception as e:
        logger.error(f"Error deleting file: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


# =============================================================================
# USER MODELS ENDPOINTS
# =============================================================================

@app.route(f'{API_PREFIX}/user-models', methods=['GET'])
@require_external_api_key
def external_api_list_user_models():
    """
    List user's custom models.

    Query Parameters:
        - include_public: Include public models (default: true)

    Response:
        {
            "status": "success",
            "models": [...]
        }
    """
    try:
        db = get_db()

        include_public = request.args.get('include_public', 'true').lower() == 'true'

        where_clause = "um.user_id = %s"
        params = [request.api_user_id]

        if include_public:
            where_clause = f"({where_clause} OR um.is_public = true)"

        models_result = db.execute(f"""
            SELECT um.id, um.name, um.description, um.model_type, um.endpoint_url,
                   um.is_public, um.is_active, um.created_at,
                   u.username as owner_username
            FROM user_models um
            LEFT JOIN users u ON um.user_id = u.id
            WHERE {where_clause} AND um.is_active = true
            ORDER BY um.created_at DESC
        """, tuple(params))

        models = []
        for row in models_result.fetchall():
            models.append({
                'id': row['id'],
                'name': row['name'],
                'description': row['description'],
                'model_type': row['model_type'],
                'is_public': bool(row['is_public']),
                'is_own': row['owner_username'] == request.api_user_id,
                'owner': row['owner_username'],
                'created_at': row['created_at'].isoformat() if row['created_at'] else None
            })

        log_api_request(f'{API_PREFIX}/user-models', 200)

        return jsonify({
            'status': 'success',
            'models': models
        })

    except Exception as e:
        logger.error(f"Error listing user models: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/user-models/<int:model_id>', methods=['GET'])
@require_external_api_key
def external_api_get_user_model(model_id):
    """
    Get custom model details.

    Path Parameters:
        model_id: User model ID

    Response:
        {
            "status": "success",
            "model": {...}
        }
    """
    try:
        db = get_db()

        model = db.execute("""
            SELECT um.*, u.username as owner_username
            FROM user_models um
            LEFT JOIN users u ON um.user_id = u.id
            WHERE um.id = %s AND (um.user_id = %s OR um.is_public = true)
        """, (model_id, request.api_user_id)).fetchone()

        if not model:
            return jsonify({
                'status': 'error',
                'error': 'model_not_found'
            }), 404

        log_api_request(f'{API_PREFIX}/user-models/{model_id}', 200)

        return jsonify({
            'status': 'success',
            'model': {
                'id': model['id'],
                'name': model['name'],
                'description': model['description'],
                'model_type': model['model_type'],
                'is_public': bool(model['is_public']),
                'owner': model['owner_username'],
                'created_at': model['created_at'].isoformat() if model['created_at'] else None
            }
        })

    except Exception as e:
        logger.error(f"Error getting user model: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/user-models/<int:model_id>/jobs', methods=['POST'])
@require_external_api_key
def external_api_submit_user_model_job(model_id):
    """
    Submit a job to a custom model.

    Path Parameters:
        model_id: User model ID

    Request Body:
        {
            "sequence": "MKFLIL...",
            "name": "My Job",
            "params": {}
        }

    Response:
        {
            "status": "success",
            "job": {...}
        }
    """
    try:
        data = request.get_json()

        if not data or not data.get('sequence'):
            return jsonify({
                'status': 'error',
                'error': 'missing_sequence'
            }), 400

        db = get_db()

        # Check model access
        model = db.execute("""
            SELECT id, name, model_type, user_id
            FROM user_models
            WHERE id = %s AND (user_id = %s OR is_public = true) AND is_active = true
        """, (model_id, request.api_user_id)).fetchone()

        if not model:
            return jsonify({
                'status': 'error',
                'error': 'model_not_found'
            }), 404

        # Get or create API project
        project = db.execute("""
            SELECT id FROM projects
            WHERE creator_id = %s AND name = 'API Requests'
        """, (request.api_user_id,)).fetchone()

        if not project:
            result = db.execute("""
                INSERT INTO projects (creator_id, name, description)
                VALUES (%s, 'API Requests', 'Jobs submitted via External API')
                RETURNING id
            """, (request.api_user_id,))
            project = result.fetchone()
            db.commit()

        project_id = project['id']

        # Create job
        job_uuid = str(uuid.uuid4())
        job_name = data.get('name', f'Custom Model Job - {model["name"]}')
        sequence = data.get('sequence')
        params = data.get('params', {})

        # Store user_model_id in job_data
        job_data = {
            'params': params,
            'user_model_id': model_id
        }

        result = db.execute("""
            INSERT INTO jobs (job_id, creator_id, project_id, name, sequence, model, status, job_data, source)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, 'api')
            RETURNING id, created_at
        """, (job_uuid, request.api_user_id, project_id, job_name, sequence,
              f'user_model_{model_id}', json.dumps(job_data)))

        job_row = result.fetchone()
        db.commit()

        log_api_request(f'{API_PREFIX}/user-models/{model_id}/jobs', 201)

        return jsonify({
            'status': 'success',
            'job': {
                'job_id': job_uuid,
                'job_db_id': job_row['id'],
                'model_id': model_id,
                'model_name': model['name'],
                'name': job_name,
                'status': 'pending',
                'created_at': job_row['created_at'].isoformat() if job_row['created_at'] else None
            }
        }), 201

    except Exception as e:
        logger.error(f"Error submitting user model job: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


# =============================================================================
# SEARCH ENDPOINT
# =============================================================================

@app.route(f'{API_PREFIX}/search', methods=['GET'])
@require_external_api_key
def external_api_search():
    """
    Search across jobs, projects, and files.

    Query Parameters:
        - q: Search query (required)
        - type: 'all', 'jobs', 'projects', 'files' (default: 'all')
        - limit: Max results per type (default: 10)

    Response:
        {
            "status": "success",
            "query": "protein",
            "results": {
                "jobs": [...],
                "projects": [...],
                "files": [...]
            }
        }
    """
    try:
        query = request.args.get('q', '').strip()
        search_type = request.args.get('type', 'all')
        limit = min(request.args.get('limit', 10, type=int), 50)

        if not query:
            return jsonify({
                'status': 'error',
                'error': 'missing_query',
                'message': 'Search query (q) is required'
            }), 400

        db = get_db()
        results = {}
        search_pattern = f'%{query}%'

        # Search jobs
        if search_type in ['all', 'jobs']:
            jobs = db.execute("""
                SELECT j.id, j.job_id, j.name, j.model, j.status, j.created_at
                FROM jobs j
                WHERE j.creator_id = %s AND (
                    j.name ILIKE %s OR j.job_id ILIKE %s OR j.sequence ILIKE %s
                )
                ORDER BY j.created_at DESC
                LIMIT %s
            """, (request.api_user_id, search_pattern, search_pattern, search_pattern, limit)).fetchall()

            results['jobs'] = [{
                'id': j['id'],
                'job_id': j['job_id'],
                'name': j['name'],
                'model': REVERSE_MODEL_MAP.get(j['model'], j['model']),
                'status': j['status'],
                'created_at': j['created_at'].isoformat() if j['created_at'] else None
            } for j in jobs]

        # Search projects
        if search_type in ['all', 'projects']:
            projects = db.execute("""
                SELECT DISTINCT p.id, p.name, p.description, p.created_at
                FROM projects p
                JOIN project_assignments pa ON p.id = pa.project_id
                WHERE pa.user_id = %s AND (
                    p.name ILIKE %s OR p.description ILIKE %s
                )
                ORDER BY p.created_at DESC
                LIMIT %s
            """, (request.api_user_id, search_pattern, search_pattern, limit)).fetchall()

            results['projects'] = [{
                'id': p['id'],
                'name': p['name'],
                'description': p['description'],
                'created_at': p['created_at'].isoformat() if p['created_at'] else None
            } for p in projects]

        # Search files
        if search_type in ['all', 'files']:
            files = db.execute("""
                SELECT f.id, f.name, f.type, f.size, f.created_at
                FROM files f
                WHERE f.project_id IN (
                    SELECT project_id FROM project_assignments WHERE user_id = %s
                ) AND f.name ILIKE %s
                ORDER BY f.created_at DESC
                LIMIT %s
            """, (request.api_user_id, search_pattern, limit)).fetchall()

            results['files'] = [{
                'id': f['id'],
                'name': f['name'],
                'type': f['type'],
                'size': f['size'],
                'created_at': f['created_at'].isoformat() if f['created_at'] else None
            } for f in files]

        log_api_request(f'{API_PREFIX}/search', 200)

        return jsonify({
            'status': 'success',
            'query': query,
            'results': results
        })

    except Exception as e:
        logger.error(f"Error in search: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


# =============================================================================
# API KEYS MANAGEMENT
# =============================================================================

@app.route(f'{API_PREFIX}/keys', methods=['GET'])
@require_external_api_key
def external_api_list_keys():
    """
    List your API keys (metadata only, not the actual keys).

    Response:
        {
            "status": "success",
            "keys": [...]
        }
    """
    try:
        keys = get_user_api_keys(request.api_user_id)

        log_api_request(f'{API_PREFIX}/keys', 200)

        return jsonify({
            'status': 'success',
            'keys': keys
        })

    except Exception as e:
        logger.error(f"Error listing keys: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/keys', methods=['POST'])
@require_external_api_key
def external_api_create_key():
    """
    Create a new API key.

    Request Body:
        {
            "name": "My New Key"
        }

    Response:
        {
            "status": "success",
            "key": {
                "key_id": 123,
                "api_key": "pk_...",    // Full key, shown only once!
                "name": "My New Key"
            }
        }
    """
    try:
        data = request.get_json() or {}
        name = data.get('name', 'API Key')

        # Check key limit (max 5 per user)
        existing = get_user_api_keys(request.api_user_id)
        if len(existing) >= 5:
            return jsonify({
                'status': 'error',
                'error': 'key_limit_reached',
                'message': 'Maximum 5 API keys allowed per user'
            }), 400

        key_data = create_api_key(request.api_user_id, name)

        log_api_request(f'{API_PREFIX}/keys', 201)

        return jsonify({
            'status': 'success',
            'key': key_data,
            'warning': 'Save this API key now. You will not be able to see it again!'
        }), 201

    except Exception as e:
        logger.error(f"Error creating key: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


@app.route(f'{API_PREFIX}/keys/<int:key_id>', methods=['DELETE'])
@require_external_api_key
def external_api_delete_key(key_id):
    """
    Revoke/delete an API key.

    Path Parameters:
        key_id: API key ID to delete

    Response:
        {
            "status": "success",
            "message": "API key deleted"
        }
    """
    try:
        # Prevent deleting the key currently being used
        if key_id == request.api_key_id:
            return jsonify({
                'status': 'error',
                'error': 'cannot_delete_current',
                'message': 'Cannot delete the API key currently being used'
            }), 400

        deleted = delete_api_key(request.api_user_id, key_id)

        if not deleted:
            return jsonify({
                'status': 'error',
                'error': 'key_not_found'
            }), 404

        log_api_request(f'{API_PREFIX}/keys/{key_id}', 200)

        return jsonify({
            'status': 'success',
            'message': 'API key deleted successfully'
        })

    except Exception as e:
        logger.error(f"Error deleting key: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


# =============================================================================
# BATCH OPERATIONS
# =============================================================================

@app.route(f'{API_PREFIX}/jobs/batch', methods=['POST'])
@require_external_api_key
def external_api_batch_submit_jobs():
    """
    Submit multiple jobs in a single request.

    Request Body:
        {
            "jobs": [
                {"model": "esmfold", "sequence": "...", "name": "Job 1"},
                {"model": "esmfold", "sequence": "...", "name": "Job 2"}
            ],
            "project_id": 123  // Optional
        }

    Response:
        {
            "status": "success",
            "submitted": 2,
            "jobs": [
                {"job_id": "...", "name": "Job 1", "status": "pending"},
                {"job_id": "...", "name": "Job 2", "status": "pending"}
            ],
            "errors": []
        }
    """
    try:
        data = request.get_json()

        if not data or not data.get('jobs'):
            return jsonify({
                'status': 'error',
                'error': 'missing_jobs',
                'message': 'jobs array is required'
            }), 400

        jobs_data = data['jobs']

        if len(jobs_data) > 100:
            return jsonify({
                'status': 'error',
                'error': 'too_many_jobs',
                'message': 'Maximum 100 jobs per batch'
            }), 400

        db = get_db()

        # Get or create project
        project_id = data.get('project_id')
        if not project_id:
            project = db.execute("""
                SELECT id FROM projects
                WHERE creator_id = %s AND name = 'API Requests'
            """, (request.api_user_id,)).fetchone()

            if not project:
                result = db.execute("""
                    INSERT INTO projects (creator_id, name, description)
                    VALUES (%s, 'API Requests', 'Jobs submitted via External API')
                    RETURNING id
                """, (request.api_user_id,))
                project = result.fetchone()
                db.commit()

            project_id = project['id']

        submitted_jobs = []
        errors = []

        for idx, job_data in enumerate(jobs_data):
            try:
                model = job_data.get('model')
                sequence = job_data.get('sequence')

                if not model or not sequence:
                    errors.append({
                        'index': idx,
                        'error': 'Missing model or sequence'
                    })
                    continue

                internal_model = MODEL_TYPE_MAP.get(model.lower())
                if not internal_model:
                    errors.append({
                        'index': idx,
                        'error': f'Invalid model: {model}'
                    })
                    continue

                job_uuid = str(uuid.uuid4())
                job_name = job_data.get('name', f'Batch Job {idx + 1}')

                result = db.execute("""
                    INSERT INTO jobs (job_id, creator_id, project_id, name, sequence, model, status, source)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', 'api')
                    RETURNING id, created_at
                """, (job_uuid, request.api_user_id, project_id, job_name, sequence, internal_model))

                job_row = result.fetchone()

                submitted_jobs.append({
                    'index': idx,
                    'job_id': job_uuid,
                    'job_db_id': job_row['id'],
                    'name': job_name,
                    'model': model,
                    'status': 'pending'
                })

            except Exception as job_error:
                errors.append({
                    'index': idx,
                    'error': str(job_error)
                })

        db.commit()

        log_api_request(f'{API_PREFIX}/jobs/batch', 201)

        return jsonify({
            'status': 'success',
            'submitted': len(submitted_jobs),
            'jobs': submitted_jobs,
            'errors': errors
        }), 201

    except Exception as e:
        logger.error(f"Error in batch job submission: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'error': 'internal_error'
        }), 500


logger.info(f"External API v1 loaded - {API_PREFIX}")
