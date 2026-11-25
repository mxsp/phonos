"""Public API endpoints for external model access via API keys."""

import time
import json
from flask import jsonify, request
from functools import wraps
from datetime import datetime, timedelta
from ..app import app, logger
from ..helpers.api_keys import verify_api_key, log_api_usage, check_rate_limit
from ..helpers.database import get_db
from ..helpers.paths import get_safe_project_path, is_path_within


def require_api_key(f):
    """Decorator to require and validate API key."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get API key from Authorization header
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return jsonify({
                'error': 'Missing or invalid Authorization header',
                'message': 'Please provide API key as: Authorization: Bearer YOUR_API_KEY'
            }), 401

        api_key = auth_header.replace('Bearer ', '').strip()

        # Verify API key
        key_data = verify_api_key(api_key)

        if not key_data:
            return jsonify({
                'error': 'Invalid API key',
                'message': 'The provided API key is invalid or has been deactivated'
            }), 401

        # Check rate limit
        if not check_rate_limit(key_data['key_id'], key_data['rate_limit']):
            return jsonify({
                'error': 'Rate limit exceeded',
                'message': f'You have exceeded the rate limit of {key_data["rate_limit"]} requests per minute'
            }), 429

        # Add to request context
        request.api_key_id = key_data['key_id']
        request.api_user_id = key_data['user_id']

        return f(*args, **kwargs)

    return decorated_function


@app.route('/v1/models', methods=['GET'])
@require_api_key
def v1_list_models():
    """List available models."""
    models = [
        {
            'id': 'esmfold',
            'name': 'ESMFold',
            'description': 'Fast protein structure prediction from Meta AI',
            'type': 'structure_prediction',
            'cost_credits': 10
        },
        {
            'id': 'alphafold2',
            'name': 'AlphaFold2',
            'description': 'High-accuracy protein structure prediction from DeepMind',
            'type': 'structure_prediction',
            'cost_credits': 50
        },
        {
            'id': 'diffdock',
            'name': 'DiffDock',
            'description': 'Molecular docking for protein-ligand interactions',
            'type': 'docking',
            'cost_credits': 30
        },
        {
            'id': 'evo2-40b',
            'name': 'Evo2-40B',
            'description': 'DNA sequence generation model from BioNeMo',
            'type': 'dna_generation',
            'cost_credits': 1,
            'endpoints': {
                'info': '/v1/evo2/info',
                'generate': '/v1/evo2/generate'
            }
        }
    ]

    return jsonify({
        'models': models
    })


@app.route('/v1/predict', methods=['POST'])
@require_api_key
def predict():
    """Submit a prediction job via API."""
    start_time = time.time()

    try:
        data = request.get_json()

        # Validate required fields
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        model = data.get('model')
        sequence = data.get('sequence')

        if not model:
            return jsonify({'error': 'Missing required field: model'}), 400

        if not sequence:
            return jsonify({'error': 'Missing required field: sequence'}), 400

        # Map model names to internal types
        model_map = {
            'esmfold': 'type-0',
            'alphafold2': 'type-2',
            'diffdock': 'type-3'
        }

        internal_model = model_map.get(model.lower())
        if not internal_model:
            return jsonify({
                'error': 'Invalid model',
                'message': f'Model "{model}" not found. Available: esmfold, alphafold2, diffdock'
            }), 400

        # Get user's default project or create one
        from ..helpers.database import get_db
        db = get_db()

        result = db.execute("""
            SELECT id FROM projects
            WHERE creator_id = %s AND name = 'API Requests'
            LIMIT 1
        """, (request.api_user_id,))

        project = result.fetchone()

        if not project:
            # Create API project
            result = db.execute("""
                INSERT INTO projects (creator_id, name, description)
                VALUES (%s, 'API Requests', 'Jobs submitted via API')
                RETURNING id
            """, (request.api_user_id,))
            project = result.fetchone()
            db.commit()

        project_id = project['id']

        # Submit job using existing infrastructure
        from ..helpers.jobs import submit_job_helper

        job_name = data.get('name', f'API Job - {model}')
        custom_params = data.get('params', {})

        job_result = submit_job_helper(
            user_id=request.api_user_id,
            project_id=project_id,
            model_type=internal_model,
            sequence=sequence,
            name=job_name,
            custom_params=custom_params
        )

        if job_result['status'] == 'success':
            response_time = int((time.time() - start_time) * 1000)

            # Log API usage
            log_api_usage(
                api_key_id=request.api_key_id,
                user_id=request.api_user_id,
                endpoint='/v1/predict',
                model_type=internal_model,
                status_code=200,
                response_time_ms=response_time,
                credits_used=job_result.get('credits_used', 0),
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent')
            )

            return jsonify({
                'success': True,
                'job_id': job_result['job_id'],
                'model': model,
                'status': 'pending',
                'credits_used': job_result.get('credits_used', 0),
                'message': 'Job submitted successfully'
            })
        else:
            return jsonify({
                'error': 'Job submission failed',
                'message': job_result.get('message', 'Unknown error')
            }), 500

    except Exception as e:
        logger.error(f"Error in API predict endpoint: {e}", exc_info=True)
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


@app.route('/v1/predict/queue', methods=['POST'])
@require_api_key
def predict_queue():
    """
    Submit a prediction job for external worker processing.

    This endpoint queues the job for processing by external workers
    instead of using the internal processing infrastructure.

    Request Body:
        {
            "model": "esmfold",
            "sequence": "MKFLILLFNILCLFPVLAADNH...",
            "name": "My Protein",
            "params": {}
        }

    Response:
        {
            "success": true,
            "job_id": "uuid",
            "queue_id": 123,
            "model": "esmfold",
            "status": "queued",
            "message": "Job queued for external processing"
        }
    """
    start_time = time.time()

    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        model = data.get('model')
        sequence = data.get('sequence')

        if not model:
            return jsonify({'error': 'Missing required field: model'}), 400

        if not sequence:
            return jsonify({'error': 'Missing required field: sequence'}), 400

        model_map = {
            'esmfold': 'type-0',
            'alphafold2': 'type-2',
            'diffdock': 'type-3',
            'boltz2': 'type-boltz2'
        }

        internal_model = model_map.get(model.lower())
        if not internal_model:
            return jsonify({
                'error': 'Invalid model',
                'message': f'Model "{model}" not found. Available: {", ".join(model_map.keys())}'
            }), 400

        db = get_db()

        # Get or create API project
        result = db.execute("""
            SELECT id FROM projects
            WHERE creator_id = %s AND name = 'API Requests'
            LIMIT 1
        """, (request.api_user_id,))

        project = result.fetchone()

        if not project:
            result = db.execute("""
                INSERT INTO projects (creator_id, name, description)
                VALUES (%s, 'API Requests', 'Jobs submitted via API')
                RETURNING id
            """, (request.api_user_id,))
            project = result.fetchone()
            db.commit()

        project_id = project['id']

        # Create job record
        import uuid
        job_uuid = str(uuid.uuid4())
        job_name = data.get('name', f'API Job - {model}')
        custom_params = data.get('params', {})

        result = db.execute("""
            INSERT INTO jobs (job_id, creator_id, project_id, name, sequence, model, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'queued')
            RETURNING id
        """, (job_uuid, request.api_user_id, project_id, job_name, sequence, internal_model))

        job = result.fetchone()
        job_db_id = job['id']

        # Queue for external processing
        import json
        job_data = {
            'sequence': sequence,
            'params': custom_params,
            'name': job_name
        }

        db.execute("""
            INSERT INTO external_api_job_queue
            (job_id, job_uuid, user_id, model_type, job_data, timeout_seconds, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        """, (job_db_id, job_uuid, request.api_user_id, internal_model,
              json.dumps(job_data), data.get('timeout', 3600)))

        db.commit()

        response_time = int((time.time() - start_time) * 1000)

        log_api_usage(
            api_key_id=request.api_key_id,
            user_id=request.api_user_id,
            endpoint='/v1/predict/queue',
            model_type=internal_model,
            status_code=200,
            response_time_ms=response_time,
            credits_used=0,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )

        return jsonify({
            'success': True,
            'job_id': job_uuid,
            'job_db_id': job_db_id,
            'model': model,
            'status': 'queued',
            'message': 'Job queued for external worker processing'
        })

    except Exception as e:
        logger.error(f"Error in queue predict endpoint: {e}", exc_info=True)
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


# =============================================================================
# WORKER API KEY MANAGEMENT
# =============================================================================

@app.route('/v1/keys/worker', methods=['GET'])
@require_api_key
def list_worker_keys():
    """List all worker API keys for the authenticated user."""
    try:
        from ..helpers.api_keys import get_user_worker_keys
        keys = get_user_worker_keys(request.api_user_id)
        return jsonify({'keys': keys})
    except Exception as e:
        logger.error(f"Error listing worker keys: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/keys/worker', methods=['POST'])
@require_api_key
def create_worker_key():
    """
    Create a new worker API key.

    Request Body:
        {
            "name": "My ESMFold Worker",
            "model": "esmfold",           // Optional: restrict to specific model
            "permissions": ["poll", "submit"]  // Optional: specific permissions
        }

    Response:
        {
            "success": true,
            "key": {
                "key_id": 123,
                "api_key": "wk_...",      // Full key, shown only once!
                "key_prefix": "wk_abc123",
                "name": "My ESMFold Worker",
                "scope_model_type": "type-0",
                "permissions": ["poll", "submit"]
            }
        }
    """
    try:
        data = request.get_json() or {}
        name = data.get('name', 'Worker Key')

        # Map model name to internal type
        model_map = {
            'esmfold': 'type-0',
            'alphafold2': 'type-2',
            'diffdock': 'type-3',
            'boltz2': 'type-boltz2'
        }

        scope_model_type = None
        if data.get('model'):
            scope_model_type = model_map.get(data['model'].lower())
            if not scope_model_type:
                return jsonify({
                    'error': 'Invalid model',
                    'message': f'Model not found. Available: {", ".join(model_map.keys())}'
                }), 400

        permissions = data.get('permissions', ['poll', 'submit', 'status'])

        from ..helpers.api_keys import create_worker_api_key
        key_data = create_worker_api_key(
            user_id=request.api_user_id,
            name=name,
            scope_model_type=scope_model_type,
            permissions=permissions
        )

        return jsonify({
            'success': True,
            'key': key_data,
            'warning': 'Store this API key securely. It will not be shown again.'
        }), 201

    except Exception as e:
        logger.error(f"Error creating worker key: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/keys/worker/<int:key_id>', methods=['DELETE'])
@require_api_key
def delete_worker_key(key_id):
    """Delete a worker API key."""
    try:
        from ..helpers.api_keys import delete_api_key
        if delete_api_key(request.api_user_id, key_id):
            return jsonify({'success': True, 'message': 'Worker key deleted'})
        else:
            return jsonify({'error': 'Key not found'}), 404
    except Exception as e:
        logger.error(f"Error deleting worker key: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/keys/worker/<int:key_id>/toggle', methods=['POST'])
@require_api_key
def toggle_worker_key(key_id):
    """Enable or disable a worker API key."""
    try:
        data = request.get_json() or {}
        is_active = data.get('is_active', True)

        from ..helpers.api_keys import toggle_api_key
        if toggle_api_key(request.api_user_id, key_id, is_active):
            return jsonify({
                'success': True,
                'message': f'Worker key {"enabled" if is_active else "disabled"}'
            })
        else:
            return jsonify({'error': 'Key not found'}), 404
    except Exception as e:
        logger.error(f"Error toggling worker key: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


# =============================================================================
# EXTERNAL QUEUE STATUS
# =============================================================================

@app.route('/v1/queue/status', methods=['GET'])
@require_api_key
def get_queue_status():
    """
    Get status of jobs in the external queue.

    Query Parameters:
        status: Filter by status (pending, assigned, completed, failed)
        model: Filter by model name

    Response:
        {
            "queue": {
                "total": 25,
                "pending": 10,
                "assigned": 5,
                "completed": 8,
                "failed": 2
            },
            "by_model": {
                "esmfold": {"pending": 5, "assigned": 2},
                "alphafold2": {"pending": 3, "assigned": 2}
            }
        }
    """
    try:
        db = get_db()

        # Get overall stats
        stats = db.execute("""
            SELECT status, model_type, COUNT(*) as count
            FROM external_api_job_queue
            WHERE user_id = %s
            GROUP BY status, model_type
        """, (request.api_user_id,)).fetchall()

        totals = {'pending': 0, 'assigned': 0, 'completed': 0, 'failed': 0}
        by_model = {}

        model_map_reverse = {
            'type-0': 'esmfold',
            'type-2': 'alphafold2',
            'type-3': 'diffdock',
            'type-boltz2': 'boltz2'
        }

        for row in stats:
            status = row['status']
            model_type = row['model_type']
            count = row['count']

            if status in totals:
                totals[status] += count

            model_name = model_map_reverse.get(model_type, model_type)
            if model_name not in by_model:
                by_model[model_name] = {}
            by_model[model_name][status] = count

        totals['total'] = sum(totals.values())

        return jsonify({
            'queue': totals,
            'by_model': by_model
        })

    except Exception as e:
        logger.error(f"Error getting queue status: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/jobs/<job_id>', methods=['GET'])
@require_api_key
def get_job_status(job_id):
    """Get job status and results."""
    try:
        from ..helpers.database import get_db
        db = get_db()

        # Verify job belongs to user
        result = db.execute("""
            SELECT job_id, name, status, output_file, message, created_at, updated_at
            FROM jobs
            WHERE job_id = %s AND creator_id = %s
        """, (job_id, request.api_user_id))

        job = result.fetchone()

        if not job:
            return jsonify({
                'error': 'Job not found',
                'message': 'Job does not exist or you do not have permission to access it'
            }), 404

        response = {
            'job_id': job['job_id'],
            'name': job['name'],
            'status': job['status'],
            'created_at': job['created_at'].isoformat() if job['created_at'] else None,
            'updated_at': job['updated_at'].isoformat() if job['updated_at'] else None
        }

        if job['status'] == 'completed' and job['output_file']:
            response['output_file'] = job['output_file']
            response['download_url'] = f"/v1/jobs/{job_id}/download"

        if job['status'] == 'failed' and job['message']:
            response['error'] = job['message']

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error getting job status: {e}", exc_info=True)
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


@app.route('/v1/jobs/<job_id>/download', methods=['GET'])
@require_api_key
def download_job_output(job_id):
    """Download job output file."""
    try:
        from ..helpers.database import get_db
        from flask import send_file
        import os
        db = get_db()

        # Verify job belongs to user and is completed
        result = db.execute("""
            SELECT j.job_id, j.output_file, j.output_display_name, j.status, j.project_id, p.team_id
            FROM jobs j
            LEFT JOIN projects p ON p.id = j.project_id
            WHERE j.job_id = %s AND j.creator_id = %s
        """, (job_id, request.api_user_id))

        job = result.fetchone()

        if not job:
            return jsonify({
                'error': 'Job not found',
                'message': 'Job does not exist or you do not have permission to access it'
            }), 404

        if job['status'] != 'completed':
            return jsonify({
                'error': 'Job not completed',
                'message': f'Job status is "{job["status"]}". Only completed jobs can be downloaded.'
            }), 400

        if not job['output_file'] or not os.path.exists(job['output_file']):
            return jsonify({
                'error': 'Output file not found',
                'message': 'The output file does not exist on the server'
            }), 404

        project_path = get_safe_project_path(job['project_id'], job['team_id']) if job['team_id'] is not None else None
        if not project_path or not is_path_within(job['output_file'], project_path):
            logger.warning("Blocked public API job output outside project root: job=%s", job_id)
            return jsonify({
                'error': 'Forbidden path',
                'message': 'The output file path is not allowed'
            }), 403

        # Determine display name
        display_name = job['output_display_name'] if job['output_display_name'] else f"{job_id}.pdb"

        # Send the file
        return send_file(
            job['output_file'],
            as_attachment=True,
            download_name=display_name,
            mimetype='chemical/x-pdb'
        )

    except Exception as e:
        logger.error(f"Error downloading job output {job_id}: {e}", exc_info=True)
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


@app.route('/v1/health', methods=['GET'])
def api_health():
    """Health check endpoint (no auth required)."""
    return jsonify({
        'status': 'operational',
        'version': 'v1',
        'message': 'PHONOS API is running'
    })


@app.route('/v1/openapi.json', methods=['GET'])
def openapi_spec():
    """Serve OpenAPI specification (no auth required)."""
    import os
    from flask import send_file

    spec_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'static', 'openapi.json')
    return send_file(spec_path, mimetype='application/json')


@app.route('/v1/docs', methods=['GET'])
def v1_api_docs():
    """Serve API documentation page with Swagger UI (no auth required)."""
    from flask import Response

    html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PHONOS API Documentation</title>
    <link rel="icon" type="image/png" href="/static/favicon.png">
    <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-tertiary: #1a1a24;
            --bg-card: #16161f;
            --accent-green: #00ff88;
            --accent-blue: #0fbcf9;
            --accent-purple: #a855f7;
            --accent-orange: #f97316;
            --accent-red: #ef4444;
            --text-primary: #f0f0f0;
            --text-secondary: #a0a0a8;
            --text-muted: #6b6b75;
            --border-color: #2a2a35;
            --code-bg: #1e1e28;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 0;
            background: var(--bg-primary);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            color: var(--text-primary);
            line-height: 1.6;
        }

        /* Header */
        .api-header {
            background: linear-gradient(180deg, var(--bg-secondary) 0%, var(--bg-primary) 100%);
            border-bottom: 1px solid var(--border-color);
            padding: 40px 20px;
        }

        .header-content {
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 20px;
        }

        .header-title {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .logo-icon {
            width: 48px;
            height: 48px;
            background: linear-gradient(135deg, var(--accent-green), var(--accent-blue));
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
            font-weight: 700;
            color: var(--bg-primary);
        }

        .header-text h1 {
            margin: 0;
            font-size: 1.75rem;
            font-weight: 700;
            background: linear-gradient(90deg, var(--accent-green), var(--accent-blue));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .header-text p {
            margin: 4px 0 0 0;
            color: var(--text-secondary);
            font-size: 0.9rem;
        }

        .header-links {
            display: flex;
            gap: 12px;
        }

        .header-link {
            padding: 10px 20px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 500;
            font-size: 0.9rem;
            transition: all 0.2s;
        }

        .header-link.primary {
            background: var(--accent-green);
            color: var(--bg-primary);
        }

        .header-link.primary:hover {
            background: #00cc6e;
        }

        .header-link.secondary {
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
        }

        .header-link.secondary:hover {
            background: var(--bg-card);
            border-color: var(--accent-green);
        }

        /* Navigation */
        .api-nav {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            padding: 0 20px;
            position: sticky;
            top: 0;
            z-index: 100;
        }

        .nav-content {
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding: 12px 0;
        }

        .nav-tab {
            padding: 8px 16px;
            border-radius: 6px;
            color: var(--text-secondary);
            text-decoration: none;
            font-size: 0.875rem;
            font-weight: 500;
            white-space: nowrap;
            transition: all 0.2s;
        }

        .nav-tab:hover, .nav-tab.active {
            color: var(--text-primary);
            background: var(--bg-tertiary);
        }

        .nav-tab.active {
            color: var(--accent-green);
        }

        /* Quick Start Section */
        .quick-start-section {
            max-width: 1200px;
            margin: 40px auto;
            padding: 0 20px;
        }

        .section-title {
            font-size: 1.5rem;
            font-weight: 600;
            margin: 0 0 24px 0;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .section-title::before {
            content: '';
            width: 4px;
            height: 24px;
            background: var(--accent-green);
            border-radius: 2px;
        }

        .cards-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-bottom: 32px;
        }

        .info-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            transition: all 0.2s;
        }

        .info-card:hover {
            border-color: var(--accent-green);
            transform: translateY(-2px);
        }

        .info-card h3 {
            margin: 0 0 8px 0;
            font-size: 1rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .info-card h3 .icon {
            font-size: 1.25rem;
        }

        .info-card p {
            margin: 0;
            color: var(--text-secondary);
            font-size: 0.875rem;
        }

        .code-example {
            background: var(--code-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
            margin-bottom: 24px;
        }

        .code-header {
            background: var(--bg-tertiary);
            padding: 12px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border-color);
        }

        .code-title {
            font-size: 0.875rem;
            font-weight: 500;
            color: var(--text-secondary);
        }

        .code-lang {
            font-size: 0.75rem;
            padding: 4px 8px;
            background: var(--bg-card);
            border-radius: 4px;
            color: var(--accent-blue);
            font-family: 'JetBrains Mono', monospace;
        }

        .code-content {
            padding: 16px;
            overflow-x: auto;
        }

        .code-content pre {
            margin: 0;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            line-height: 1.7;
            color: var(--text-primary);
        }

        .code-content .comment { color: var(--text-muted); }
        .code-content .string { color: var(--accent-green); }
        .code-content .keyword { color: var(--accent-purple); }
        .code-content .url { color: var(--accent-blue); }

        /* Models Section */
        .models-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }

        .model-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            position: relative;
            overflow: hidden;
        }

        .model-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
        }

        .model-card.evo2::before { background: var(--accent-green); }
        .model-card.esmfold::before { background: var(--accent-blue); }
        .model-card.alphafold::before { background: var(--accent-purple); }
        .model-card.diffdock::before { background: var(--accent-orange); }

        .model-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 12px;
        }

        .model-card.evo2 .model-badge { background: rgba(0,255,136,0.15); color: var(--accent-green); }
        .model-card.esmfold .model-badge { background: rgba(15,188,249,0.15); color: var(--accent-blue); }
        .model-card.alphafold .model-badge { background: rgba(168,85,247,0.15); color: var(--accent-purple); }
        .model-card.diffdock .model-badge { background: rgba(249,115,22,0.15); color: var(--accent-orange); }

        .model-card h3 {
            margin: 0 0 8px 0;
            font-size: 1.125rem;
        }

        .model-card .description {
            color: var(--text-secondary);
            font-size: 0.875rem;
            margin: 0 0 16px 0;
        }

        .model-stats {
            display: flex;
            gap: 16px;
            padding-top: 16px;
            border-top: 1px solid var(--border-color);
        }

        .model-stat {
            font-size: 0.8rem;
        }

        .model-stat .label {
            color: var(--text-muted);
        }

        .model-stat .value {
            color: var(--text-primary);
            font-weight: 600;
        }

        /* Swagger UI Overrides */
        .swagger-container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 0 20px 60px 20px;
        }

        .swagger-ui {
            font-family: 'Inter', sans-serif !important;
        }

        .swagger-ui .topbar { display: none; }

        .swagger-ui .info { margin: 30px 0; }

        .swagger-ui .info .title {
            font-family: 'Inter', sans-serif !important;
            color: var(--accent-green) !important;
        }

        .swagger-ui .info .description {
            font-family: 'Inter', sans-serif !important;
        }

        .swagger-ui .scheme-container {
            background: var(--bg-secondary) !important;
            box-shadow: none !important;
            border: 1px solid var(--border-color) !important;
            border-radius: 8px !important;
            padding: 16px !important;
        }

        .swagger-ui .opblock-tag {
            font-family: 'Inter', sans-serif !important;
            color: var(--text-primary) !important;
            border-bottom: 1px solid var(--border-color) !important;
        }

        .swagger-ui .opblock {
            border-radius: 8px !important;
            border: 1px solid var(--border-color) !important;
            background: var(--bg-card) !important;
            box-shadow: none !important;
            margin-bottom: 12px !important;
        }

        .swagger-ui .opblock .opblock-summary {
            border: none !important;
        }

        .swagger-ui .opblock .opblock-summary-method {
            font-family: 'JetBrains Mono', monospace !important;
            font-weight: 600 !important;
            font-size: 0.75rem !important;
            border-radius: 4px !important;
            min-width: 70px !important;
        }

        .swagger-ui .opblock.opblock-get .opblock-summary-method {
            background: var(--accent-green) !important;
            color: var(--bg-primary) !important;
        }

        .swagger-ui .opblock.opblock-post .opblock-summary-method {
            background: var(--accent-blue) !important;
            color: var(--bg-primary) !important;
        }

        .swagger-ui .opblock.opblock-delete .opblock-summary-method {
            background: var(--accent-red) !important;
            color: white !important;
        }

        .swagger-ui .opblock.opblock-put .opblock-summary-method {
            background: var(--accent-orange) !important;
            color: var(--bg-primary) !important;
        }

        .swagger-ui .opblock .opblock-summary-path {
            font-family: 'JetBrains Mono', monospace !important;
            color: var(--text-primary) !important;
        }

        .swagger-ui .opblock .opblock-summary-description {
            font-family: 'Inter', sans-serif !important;
            color: var(--text-secondary) !important;
        }

        .swagger-ui .opblock-body {
            background: var(--bg-secondary) !important;
        }

        .swagger-ui .btn {
            border-radius: 6px !important;
            font-family: 'Inter', sans-serif !important;
        }

        .swagger-ui .btn.execute {
            background: var(--accent-green) !important;
            color: var(--bg-primary) !important;
            border: none !important;
        }

        .swagger-ui .btn.execute:hover {
            background: #00cc6e !important;
        }

        .swagger-ui table tbody tr td {
            border-bottom: 1px solid var(--border-color) !important;
        }

        .swagger-ui .model-box {
            background: var(--bg-tertiary) !important;
        }

        .swagger-ui section.models {
            border: 1px solid var(--border-color) !important;
            border-radius: 8px !important;
        }

        .swagger-ui .response-col_status {
            font-family: 'JetBrains Mono', monospace !important;
        }

        .swagger-ui .markdown p, .swagger-ui .markdown li {
            color: var(--text-secondary) !important;
        }

        .swagger-ui .markdown code {
            background: var(--code-bg) !important;
            color: var(--accent-blue) !important;
            padding: 2px 6px !important;
            border-radius: 4px !important;
        }

        .swagger-ui input[type=text], .swagger-ui textarea {
            background: var(--bg-tertiary) !important;
            border: 1px solid var(--border-color) !important;
            border-radius: 6px !important;
            color: var(--text-primary) !important;
        }

        .swagger-ui select {
            background: var(--bg-tertiary) !important;
            border: 1px solid var(--border-color) !important;
            border-radius: 6px !important;
            color: var(--text-primary) !important;
        }

        /* Footer */
        .api-footer {
            background: var(--bg-secondary);
            border-top: 1px solid var(--border-color);
            padding: 24px 20px;
            text-align: center;
            color: var(--text-muted);
            font-size: 0.875rem;
        }

        .api-footer a {
            color: var(--accent-green);
            text-decoration: none;
        }

        .api-footer a:hover {
            text-decoration: underline;
        }

        /* Responsive */
        @media (max-width: 768px) {
            .header-content {
                flex-direction: column;
                text-align: center;
            }

            .header-title {
                flex-direction: column;
            }

            .header-links {
                width: 100%;
                justify-content: center;
            }

            .cards-grid, .models-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <header class="api-header">
        <div class="header-content">
            <div class="header-title">
                <div class="logo-icon">P</div>
                <div class="header-text">
                    <h1>PHONOS API</h1>
                    <p>Bioinformatics Platform API v1.0</p>
                </div>
            </div>
            <div class="header-links">
                <a href="https://phonos.fyreflysystems.com" class="header-link secondary">Dashboard</a>
                <a href="https://phonos.fyreflysystems.com/settings" class="header-link primary">Get API Key</a>
            </div>
        </div>
    </header>

    <nav class="api-nav">
        <div class="nav-content">
            <a href="#quick-start" class="nav-tab active">Quick Start</a>
            <a href="#models" class="nav-tab">Models</a>
            <a href="#worker-bridge" class="nav-tab">Worker Bridge</a>
            <a href="#endpoints" class="nav-tab">Endpoints</a>
            <a href="#authentication" class="nav-tab">Authentication</a>
            <a href="#errors" class="nav-tab">Errors</a>
        </div>
    </nav>

    <section class="quick-start-section" id="quick-start">
        <h2 class="section-title">Quick Start</h2>

        <div class="cards-grid">
            <div class="info-card">
                <h3><span class="icon">1</span> Get Your API Key</h3>
                <p>Generate an API key from your PHONOS dashboard under Settings > API Keys. Keep it secure!</p>
            </div>
            <div class="info-card">
                <h3><span class="icon">2</span> Make Requests</h3>
                <p>Include your API key in the Authorization header as a Bearer token with every request.</p>
            </div>
            <div class="info-card">
                <h3><span class="icon">3</span> Check Credits</h3>
                <p>Monitor your credit balance via the /v1/account endpoint. Most operations consume credits.</p>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">Generate DNA with Evo2-40B</span>
                <span class="code-lang">bash</span>
            </div>
            <div class="code-content">
<pre><span class="comment"># Generate DNA sequence continuation from a seed</span>
curl -X POST <span class="url">https://phonos.fyreflysystems.com/v1/evo2/generate</span> \\
  -H <span class="string">"Authorization: Bearer YOUR_API_KEY"</span> \\
  -H <span class="string">"Content-Type: application/json"</span> \\
  -d '{
    <span class="string">"sequence"</span>: <span class="string">"ATCGATCGATCGATCGATCG"</span>,
    <span class="string">"num_tokens"</span>: 128,
    <span class="string">"temperature"</span>: 0.7
  }'</pre>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">Predict Protein Structure</span>
                <span class="code-lang">bash</span>
            </div>
            <div class="code-content">
<pre><span class="comment"># Submit a structure prediction job</span>
curl -X POST <span class="url">https://phonos.fyreflysystems.com/v1/predict</span> \\
  -H <span class="string">"Authorization: Bearer YOUR_API_KEY"</span> \\
  -H <span class="string">"Content-Type: application/json"</span> \\
  -d '{
    <span class="string">"model"</span>: <span class="string">"esmfold"</span>,
    <span class="string">"sequence"</span>: <span class="string">"MKFLILLFNILCLFPVLAADNHGVGPQGASGVDPITFDINSNQTGVQLTLQSTGG"</span>,
    <span class="string">"name"</span>: <span class="string">"My Protein"</span>
  }'</pre>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">Python Example</span>
                <span class="code-lang">python</span>
            </div>
            <div class="code-content">
<pre><span class="keyword">import</span> requests

API_KEY = <span class="string">"YOUR_API_KEY"</span>
BASE_URL = <span class="string">"https://phonos.fyreflysystems.com"</span>

<span class="comment"># Generate DNA sequence</span>
response = requests.post(
    f<span class="string">"{BASE_URL}/v1/evo2/generate"</span>,
    headers={<span class="string">"Authorization"</span>: f<span class="string">"Bearer {API_KEY}"</span>},
    json={
        <span class="string">"sequence"</span>: <span class="string">"ATCGATCGATCGATCGATCG"</span>,
        <span class="string">"num_tokens"</span>: 100,
        <span class="string">"temperature"</span>: 0.7
    }
)

result = response.json()
print(f<span class="string">"Generated: {result['output']['sequence']}"</span>)</pre>
            </div>
        </div>
    </section>

    <section class="quick-start-section" id="models">
        <h2 class="section-title">Available Models</h2>

        <div class="models-grid">
            <div class="model-card evo2">
                <span class="model-badge">DNA Generation</span>
                <h3>Evo2-40B</h3>
                <p class="description">A foundation model for DNA sequence generation. Generate novel sequences from seed inputs.</p>
                <div class="model-stats">
                    <div class="model-stat">
                        <span class="label">Cost: </span>
                        <span class="value">1 credit</span>
                    </div>
                    <div class="model-stat">
                        <span class="label">Max tokens: </span>
                        <span class="value">1,200</span>
                    </div>
                </div>
            </div>

            <div class="model-card esmfold">
                <span class="model-badge">Structure Prediction</span>
                <h3>ESMFold</h3>
                <p class="description">Meta AI's fast protein structure prediction. Get results in seconds with good accuracy.</p>
                <div class="model-stats">
                    <div class="model-stat">
                        <span class="label">Cost: </span>
                        <span class="value">10 credits</span>
                    </div>
                    <div class="model-stat">
                        <span class="label">Speed: </span>
                        <span class="value">Fast</span>
                    </div>
                </div>
            </div>

            <div class="model-card alphafold">
                <span class="model-badge">Structure Prediction</span>
                <h3>AlphaFold2</h3>
                <p class="description">DeepMind's high-accuracy structure prediction. Best for publication-quality results.</p>
                <div class="model-stats">
                    <div class="model-stat">
                        <span class="label">Cost: </span>
                        <span class="value">50 credits</span>
                    </div>
                    <div class="model-stat">
                        <span class="label">Accuracy: </span>
                        <span class="value">Highest</span>
                    </div>
                </div>
            </div>

            <div class="model-card diffdock">
                <span class="model-badge">Molecular Docking</span>
                <h3>DiffDock</h3>
                <p class="description">State-of-the-art molecular docking for protein-ligand interaction prediction.</p>
                <div class="model-stats">
                    <div class="model-stat">
                        <span class="label">Cost: </span>
                        <span class="value">30 credits</span>
                    </div>
                    <div class="model-stat">
                        <span class="label">Type: </span>
                        <span class="value">Docking</span>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section class="quick-start-section" id="worker-bridge">
        <h2 class="section-title">External Worker Bridge API</h2>

        <div class="cards-grid">
            <div class="info-card">
                <h3><span class="icon">🔌</span> Bridge Architecture</h3>
                <p>Run your own compute workers that poll for jobs and submit results. No direct database access required.</p>
            </div>
            <div class="info-card">
                <h3><span class="icon">🔑</span> Worker Keys</h3>
                <p>Create dedicated worker API keys (wk_...) with model-specific scopes and rate limits.</p>
            </div>
            <div class="info-card">
                <h3><span class="icon">📊</span> Queue Management</h3>
                <p>Monitor pending jobs, track progress, and manage your processing queue.</p>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">1. Create a Worker API Key</span>
                <span class="code-lang">bash</span>
            </div>
            <div class="code-content">
<pre><span class="comment"># Create a worker key scoped to ESMFold jobs</span>
curl -X POST <span class="url">https://phonos.fyreflysystems.com/v1/keys/worker</span> \\
  -H <span class="string">"Authorization: Bearer YOUR_API_KEY"</span> \\
  -H <span class="string">"Content-Type: application/json"</span> \\
  -d '{
    <span class="string">"name"</span>: <span class="string">"ESMFold Worker"</span>,
    <span class="string">"model"</span>: <span class="string">"esmfold"</span>
  }'

<span class="comment"># Response contains your worker key (shown only once!)</span>
<span class="comment"># { "key": { "api_key": "wk_abc123...", "key_prefix": "wk_abc123" } }</span></pre>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">2. Submit a Job for External Processing</span>
                <span class="code-lang">bash</span>
            </div>
            <div class="code-content">
<pre><span class="comment"># Queue a job for worker processing (not processed internally)</span>
curl -X POST <span class="url">https://phonos.fyreflysystems.com/v1/predict/queue</span> \\
  -H <span class="string">"Authorization: Bearer YOUR_API_KEY"</span> \\
  -H <span class="string">"Content-Type: application/json"</span> \\
  -d '{
    <span class="string">"model"</span>: <span class="string">"esmfold"</span>,
    <span class="string">"sequence"</span>: <span class="string">"MKFLILLFNILCLFPVLAADNHGVGPQGASGVDPITFDINSNQTGVQLTLQSTGG"</span>,
    <span class="string">"name"</span>: <span class="string">"My Protein Structure"</span>
  }'</pre>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">3. Worker: Poll for Jobs</span>
                <span class="code-lang">bash</span>
            </div>
            <div class="code-content">
<pre><span class="comment"># Worker polls for pending jobs using worker key</span>
curl -X POST <span class="url">https://phonos.fyreflysystems.com/api/external/v1/jobs/poll</span> \\
  -H <span class="string">"Authorization: Bearer wk_YOUR_WORKER_KEY"</span> \\
  -H <span class="string">"Content-Type: application/json"</span> \\
  -d '{
    <span class="string">"worker_id"</span>: <span class="string">"worker-1"</span>,
    <span class="string">"max_jobs"</span>: 5
  }'

<span class="comment"># Response: { "jobs": [{ "job_id": "uuid", "sequence": "...", ... }] }</span></pre>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">4. Worker: Submit Results</span>
                <span class="code-lang">bash</span>
            </div>
            <div class="code-content">
<pre><span class="comment"># After processing, submit results back</span>
curl -X POST <span class="url">https://phonos.fyreflysystems.com/api/external/v1/jobs/JOB_UUID/result</span> \\
  -H <span class="string">"Authorization: Bearer wk_YOUR_WORKER_KEY"</span> \\
  -H <span class="string">"Content-Type: application/json"</span> \\
  -d '{
    <span class="string">"status"</span>: <span class="string">"completed"</span>,
    <span class="string">"result"</span>: {
      <span class="string">"pdb_data"</span>: <span class="string">"ATOM 1 N MET A 1..."</span>,
      <span class="string">"confidence_score"</span>: 0.95
    },
    <span class="string">"metrics"</span>: {
      <span class="string">"runtime_seconds"</span>: 45.2
    }
  }'</pre>
            </div>
        </div>

        <div class="code-example">
            <div class="code-header">
                <span class="code-title">Python Worker Example</span>
                <span class="code-lang">python</span>
            </div>
            <div class="code-content">
<pre><span class="keyword">import</span> requests
<span class="keyword">import</span> time

WORKER_KEY = <span class="string">"wk_YOUR_WORKER_KEY"</span>
BASE_URL = <span class="string">"https://phonos.fyreflysystems.com"</span>

<span class="keyword">def</span> poll_and_process():
    <span class="keyword">while</span> <span class="keyword">True</span>:
        <span class="comment"># Poll for jobs</span>
        resp = requests.post(
            f<span class="string">"{BASE_URL}/api/external/v1/jobs/poll"</span>,
            headers={<span class="string">"Authorization"</span>: f<span class="string">"Bearer {WORKER_KEY}"</span>},
            json={<span class="string">"worker_id"</span>: <span class="string">"python-worker-1"</span>, <span class="string">"max_jobs"</span>: 1}
        )

        jobs = resp.json().get(<span class="string">"jobs"</span>, [])

        <span class="keyword">for</span> job <span class="keyword">in</span> jobs:
            job_id = job[<span class="string">"job_id"</span>]
            sequence = job[<span class="string">"sequence"</span>]

            <span class="comment"># Process the job (your compute logic here)</span>
            result = process_structure(sequence)

            <span class="comment"># Submit result</span>
            requests.post(
                f<span class="string">"{BASE_URL}/api/external/v1/jobs/{job_id}/result"</span>,
                headers={<span class="string">"Authorization"</span>: f<span class="string">"Bearer {WORKER_KEY}"</span>},
                json={
                    <span class="string">"status"</span>: <span class="string">"completed"</span>,
                    <span class="string">"result"</span>: result
                }
            )

        time.sleep(5)  <span class="comment"># Poll interval</span>

<span class="keyword">if</span> __name__ == <span class="string">"__main__"</span>:
    poll_and_process()</pre>
            </div>
        </div>

        <h3 style="margin-top: 32px; color: var(--text-primary);">Worker Bridge Endpoints</h3>
        <div style="background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; margin-top: 16px;">
            <table style="width: 100%; border-collapse: collapse; font-size: 0.875rem;">
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-green);">GET</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/health</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Health check (no auth)</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-green);">GET</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/models</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">List available models for worker</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-blue);">POST</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/jobs/poll</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Poll for pending jobs</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-blue);">POST</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/jobs/{id}/result</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Submit job result</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-green);">GET</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/jobs/{id}/status</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Get job status</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-blue);">POST</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/jobs/{id}/heartbeat</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Send worker heartbeat</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-green);">GET</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/worker/info</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Get worker info & stats</td>
                </tr>
                <tr>
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-green);">GET</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/api/external/v1/jobs/queue</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Get queue status by model</td>
                </tr>
            </table>
        </div>

        <h3 style="margin-top: 32px; color: var(--text-primary);">Worker Key Management</h3>
        <div style="background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; margin-top: 16px;">
            <table style="width: 100%; border-collapse: collapse; font-size: 0.875rem;">
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-green);">GET</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/v1/keys/worker</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">List your worker keys</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-blue);">POST</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/v1/keys/worker</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Create a new worker key</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-red);">DELETE</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/v1/keys/worker/{id}</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Delete a worker key</td>
                </tr>
                <tr style="border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-blue);">POST</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/v1/keys/worker/{id}/toggle</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Enable/disable a worker key</td>
                </tr>
                <tr>
                    <td style="padding: 12px 0; font-family: 'JetBrains Mono', monospace; color: var(--accent-green);">GET</td>
                    <td style="padding: 12px 8px; font-family: 'JetBrains Mono', monospace;">/v1/queue/status</td>
                    <td style="padding: 12px 0; color: var(--text-secondary);">Get external queue statistics</td>
                </tr>
            </table>
        </div>
    </section>

    <section class="quick-start-section" id="endpoints">
        <h2 class="section-title">API Reference</h2>
    </section>

    <div class="swagger-container">
        <div id="swagger-ui"></div>
    </div>

    <footer class="api-footer">
        <p>PHONOS API v1.0 | <a href="https://phonos.fyreflysystems.com">phonos.fyreflysystems.com</a> | Need help? <a href="mailto:support@fyreflysystems.com">Contact Support</a></p>
    </footer>

    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        window.onload = function() {
            SwaggerUIBundle({
                url: "/v1/openapi.json",
                dom_id: '#swagger-ui',
                deepLinking: true,
                presets: [
                    SwaggerUIBundle.presets.apis,
                    SwaggerUIBundle.SwaggerUIStandalonePreset
                ],
                plugins: [
                    SwaggerUIBundle.plugins.DownloadUrl
                ],
                layout: "BaseLayout",
                defaultModelsExpandDepth: 0,
                docExpansion: "list",
                tryItOutEnabled: true,
                filter: true,
                showExtensions: true,
                showCommonExtensions: true
            });
        };

        // Smooth scroll for nav
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', function(e) {
                const href = this.getAttribute('href');
                if (href.startsWith('#')) {
                    e.preventDefault();
                    const target = document.querySelector(href);
                    if (target) {
                        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    }
                    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
                    this.classList.add('active');
                }
            });
        });
    </script>
</body>
</html>'''

    return Response(html, mimetype='text/html')


# ========================================
# ENHANCED API ENDPOINTS
# ========================================

@app.route('/v1/account', methods=['GET'])
@require_api_key
def get_account_info():
    """Get user account information and credit balance."""
    try:
        db = get_db()

        # Get user info and credits
        result = db.execute("""
            SELECT u.id, u.username, u.email, u.created_at,
                   COALESCE(u.credits, 0) as credits
            FROM users u
            WHERE u.id = %s
        """, (request.api_user_id,))

        user = result.fetchone()

        if not user:
            return jsonify({'error': 'User not found'}), 404

        # Get job stats
        job_stats = db.execute("""
            SELECT
                COUNT(*) as total_jobs,
                COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed_jobs,
                COUNT(CASE WHEN status = 'pending' OR status = 'running' THEN 1 END) as active_jobs,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed_jobs
            FROM jobs
            WHERE creator_id = %s
        """, (request.api_user_id,))

        stats = job_stats.fetchone()

        return jsonify({
            'account': {
                'id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'created_at': user['created_at'].isoformat() if user['created_at'] else None
            },
            'credits': {
                'balance': user['credits'],
                'currency': 'credits'
            },
            'jobs': {
                'total': stats['total_jobs'] or 0,
                'completed': stats['completed_jobs'] or 0,
                'active': stats['active_jobs'] or 0,
                'failed': stats['failed_jobs'] or 0
            }
        })

    except Exception as e:
        logger.error(f"Error getting account info: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/jobs', methods=['GET'])
@require_api_key
def list_jobs():
    """List all jobs for the authenticated user."""
    try:
        db = get_db()

        # Parse query parameters
        status_filter = request.args.get('status')  # pending, running, completed, failed
        model_filter = request.args.get('model')  # esmfold, alphafold2, diffdock
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))

        # Build query
        query = """
            SELECT job_id, name, status, model_type, output_file,
                   created_at, updated_at, message
            FROM jobs
            WHERE creator_id = %s
        """
        params = [request.api_user_id]

        if status_filter:
            query += " AND status = %s"
            params.append(status_filter)

        if model_filter:
            model_map = {'esmfold': 'type-0', 'alphafold2': 'type-2', 'diffdock': 'type-3'}
            internal_model = model_map.get(model_filter.lower())
            if internal_model:
                query += " AND model_type = %s"
                params.append(internal_model)

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        result = db.execute(query, tuple(params))
        jobs = []

        # Reverse model map for display
        reverse_model_map = {'type-0': 'esmfold', 'type-2': 'alphafold2', 'type-3': 'diffdock'}

        for row in result.fetchall():
            job = {
                'job_id': row['job_id'],
                'name': row['name'],
                'status': row['status'],
                'model': reverse_model_map.get(row['model_type'], row['model_type']),
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None
            }

            if row['status'] == 'completed' and row['output_file']:
                job['download_url'] = f"/v1/jobs/{row['job_id']}/download"

            if row['status'] == 'failed' and row['message']:
                job['error'] = row['message']

            jobs.append(job)

        # Get total count
        count_query = "SELECT COUNT(*) as total FROM jobs WHERE creator_id = %s"
        count_params = [request.api_user_id]
        if status_filter:
            count_query += " AND status = %s"
            count_params.append(status_filter)

        count_result = db.execute(count_query, tuple(count_params))
        total = count_result.fetchone()['total']

        return jsonify({
            'jobs': jobs,
            'pagination': {
                'total': total,
                'limit': limit,
                'offset': offset,
                'has_more': offset + limit < total
            }
        })

    except Exception as e:
        logger.error(f"Error listing jobs: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/predict/batch', methods=['POST'])
@require_api_key
def predict_batch():
    """Submit multiple prediction jobs in a single request."""
    start_time = time.time()

    try:
        data = request.get_json()

        if not data or 'jobs' not in data:
            return jsonify({'error': 'Missing required field: jobs'}), 400

        jobs_data = data.get('jobs', [])

        if not isinstance(jobs_data, list) or len(jobs_data) == 0:
            return jsonify({'error': 'jobs must be a non-empty array'}), 400

        if len(jobs_data) > 10:
            return jsonify({'error': 'Maximum 10 jobs per batch request'}), 400

        # Model mapping
        model_map = {
            'esmfold': 'type-0',
            'alphafold2': 'type-2',
            'diffdock': 'type-3'
        }

        db = get_db()

        # Get or create API project
        result = db.execute("""
            SELECT id FROM projects
            WHERE creator_id = %s AND name = 'API Requests'
            LIMIT 1
        """, (request.api_user_id,))

        project = result.fetchone()

        if not project:
            result = db.execute("""
                INSERT INTO projects (creator_id, name, description)
                VALUES (%s, 'API Requests', 'Jobs submitted via API')
                RETURNING id
            """, (request.api_user_id,))
            project = result.fetchone()
            db.commit()

        project_id = project['id']

        # Submit each job
        from ..helpers.jobs import submit_job_helper

        results = []
        total_credits = 0

        for i, job_data in enumerate(jobs_data):
            model = job_data.get('model')
            sequence = job_data.get('sequence')

            if not model or not sequence:
                results.append({
                    'index': i,
                    'success': False,
                    'error': 'Missing model or sequence'
                })
                continue

            internal_model = model_map.get(model.lower())
            if not internal_model:
                results.append({
                    'index': i,
                    'success': False,
                    'error': f'Invalid model: {model}'
                })
                continue

            job_name = job_data.get('name', f'Batch Job {i+1} - {model}')
            custom_params = job_data.get('params', {})

            job_result = submit_job_helper(
                user_id=request.api_user_id,
                project_id=project_id,
                model_type=internal_model,
                sequence=sequence,
                name=job_name,
                custom_params=custom_params
            )

            if job_result['status'] == 'success':
                credits_used = job_result.get('credits_used', 0)
                total_credits += credits_used
                results.append({
                    'index': i,
                    'success': True,
                    'job_id': job_result['job_id'],
                    'model': model,
                    'credits_used': credits_used
                })
            else:
                results.append({
                    'index': i,
                    'success': False,
                    'error': job_result.get('message', 'Unknown error')
                })

        response_time = int((time.time() - start_time) * 1000)

        # Log API usage
        log_api_usage(
            api_key_id=request.api_key_id,
            user_id=request.api_user_id,
            endpoint='/v1/predict/batch',
            model_type='batch',
            status_code=200,
            response_time_ms=response_time,
            credits_used=total_credits,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )

        successful = sum(1 for r in results if r.get('success'))

        return jsonify({
            'success': True,
            'summary': {
                'total': len(jobs_data),
                'successful': successful,
                'failed': len(jobs_data) - successful,
                'total_credits_used': total_credits
            },
            'results': results
        })

    except Exception as e:
        logger.error(f"Error in batch predict endpoint: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/usage', methods=['GET'])
@require_api_key
def get_api_usage():
    """Get API usage statistics for the authenticated user."""
    try:
        db = get_db()

        # Parse date range
        days = min(int(request.args.get('days', 30)), 90)
        start_date = datetime.now() - timedelta(days=days)

        # Get usage by endpoint
        endpoint_stats = db.execute("""
            SELECT endpoint, COUNT(*) as requests,
                   SUM(credits_used) as credits,
                   AVG(response_time_ms) as avg_response_time
            FROM api_usage
            WHERE user_id = %s AND timestamp >= %s
            GROUP BY endpoint
            ORDER BY requests DESC
        """, (request.api_user_id, start_date))

        endpoints = []
        for row in endpoint_stats.fetchall():
            endpoints.append({
                'endpoint': row['endpoint'],
                'requests': row['requests'],
                'credits_used': row['credits'] or 0,
                'avg_response_time_ms': round(row['avg_response_time'] or 0, 2)
            })

        # Get usage by model
        model_stats = db.execute("""
            SELECT model_type, COUNT(*) as requests,
                   SUM(credits_used) as credits
            FROM api_usage
            WHERE user_id = %s AND timestamp >= %s AND model_type IS NOT NULL
            GROUP BY model_type
            ORDER BY requests DESC
        """, (request.api_user_id, start_date))

        reverse_model_map = {'type-0': 'esmfold', 'type-2': 'alphafold2', 'type-3': 'diffdock'}
        models = []
        for row in model_stats.fetchall():
            models.append({
                'model': reverse_model_map.get(row['model_type'], row['model_type']),
                'requests': row['requests'],
                'credits_used': row['credits'] or 0
            })

        # Get daily usage
        daily_stats = db.execute("""
            SELECT DATE(timestamp) as date, COUNT(*) as requests,
                   SUM(credits_used) as credits
            FROM api_usage
            WHERE user_id = %s AND timestamp >= %s
            GROUP BY DATE(timestamp)
            ORDER BY date DESC
            LIMIT 30
        """, (request.api_user_id, start_date))

        daily = []
        for row in daily_stats.fetchall():
            daily.append({
                'date': row['date'].isoformat() if row['date'] else None,
                'requests': row['requests'],
                'credits_used': row['credits'] or 0
            })

        # Get totals
        totals = db.execute("""
            SELECT COUNT(*) as total_requests,
                   SUM(credits_used) as total_credits,
                   AVG(response_time_ms) as avg_response_time
            FROM api_usage
            WHERE user_id = %s AND timestamp >= %s
        """, (request.api_user_id, start_date))

        total = totals.fetchone()

        return jsonify({
            'period': {
                'days': days,
                'start_date': start_date.isoformat(),
                'end_date': datetime.now().isoformat()
            },
            'totals': {
                'requests': total['total_requests'] or 0,
                'credits_used': total['total_credits'] or 0,
                'avg_response_time_ms': round(total['avg_response_time'] or 0, 2)
            },
            'by_endpoint': endpoints,
            'by_model': models,
            'daily': daily
        })

    except Exception as e:
        logger.error(f"Error getting API usage: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/webhooks', methods=['GET'])
@require_api_key
def list_webhooks():
    """List all webhooks for the authenticated user."""
    try:
        db = get_db()

        result = db.execute("""
            SELECT id, url, events, is_active, created_at, last_triggered_at,
                   success_count, failure_count
            FROM api_webhooks
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (request.api_user_id,))

        webhooks = []
        for row in result.fetchall():
            webhooks.append({
                'id': row['id'],
                'url': row['url'],
                'events': json.loads(row['events']) if row['events'] else [],
                'is_active': bool(row['is_active']),
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'last_triggered_at': row['last_triggered_at'].isoformat() if row['last_triggered_at'] else None,
                'stats': {
                    'success_count': row['success_count'] or 0,
                    'failure_count': row['failure_count'] or 0
                }
            })

        return jsonify({'webhooks': webhooks})

    except Exception as e:
        logger.error(f"Error listing webhooks: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/webhooks', methods=['POST'])
@require_api_key
def create_webhook():
    """Create a new webhook."""
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        url = data.get('url')
        events = data.get('events', ['job.completed', 'job.failed'])

        if not url:
            return jsonify({'error': 'Missing required field: url'}), 400

        # Validate URL
        if not url.startswith('https://'):
            return jsonify({'error': 'Webhook URL must use HTTPS'}), 400

        # Validate events
        valid_events = ['job.completed', 'job.failed', 'job.started']
        for event in events:
            if event not in valid_events:
                return jsonify({
                    'error': f'Invalid event: {event}',
                    'valid_events': valid_events
                }), 400

        db = get_db()

        # Check webhook limit (max 5 per user)
        count = db.execute("""
            SELECT COUNT(*) as count FROM api_webhooks WHERE user_id = %s
        """, (request.api_user_id,))

        if count.fetchone()['count'] >= 5:
            return jsonify({'error': 'Maximum 5 webhooks per user'}), 400

        # Create webhook
        result = db.execute("""
            INSERT INTO api_webhooks (user_id, url, events, is_active)
            VALUES (%s, %s, %s, 1)
            RETURNING id, created_at
        """, (request.api_user_id, url, json.dumps(events)))

        row = result.fetchone()
        db.commit()

        return jsonify({
            'success': True,
            'webhook': {
                'id': row['id'],
                'url': url,
                'events': events,
                'is_active': True,
                'created_at': row['created_at'].isoformat() if row['created_at'] else None
            }
        }), 201

    except Exception as e:
        logger.error(f"Error creating webhook: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/webhooks/<int:webhook_id>', methods=['DELETE'])
@require_api_key
def delete_webhook(webhook_id):
    """Delete a webhook."""
    try:
        db = get_db()

        result = db.execute("""
            DELETE FROM api_webhooks
            WHERE id = %s AND user_id = %s
            RETURNING id
        """, (webhook_id, request.api_user_id))

        if not result.fetchone():
            return jsonify({'error': 'Webhook not found'}), 404

        db.commit()

        return jsonify({'success': True, 'message': 'Webhook deleted'})

    except Exception as e:
        logger.error(f"Error deleting webhook: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/webhooks/<int:webhook_id>/test', methods=['POST'])
@require_api_key
def test_webhook(webhook_id):
    """Send a test event to a webhook."""
    try:
        import requests as http_requests

        db = get_db()

        result = db.execute("""
            SELECT url FROM api_webhooks
            WHERE id = %s AND user_id = %s AND is_active = 1
        """, (webhook_id, request.api_user_id))

        webhook = result.fetchone()

        if not webhook:
            return jsonify({'error': 'Webhook not found or inactive'}), 404

        # Send test payload
        test_payload = {
            'event': 'test',
            'timestamp': datetime.now().isoformat(),
            'data': {
                'message': 'This is a test webhook event from PHONOS API',
                'webhook_id': webhook_id
            }
        }

        try:
            response = http_requests.post(
                webhook['url'],
                json=test_payload,
                headers={'Content-Type': 'application/json', 'X-Phonos-Event': 'test'},
                timeout=10
            )

            return jsonify({
                'success': True,
                'test_result': {
                    'status_code': response.status_code,
                    'response_time_ms': int(response.elapsed.total_seconds() * 1000),
                    'success': response.status_code < 400
                }
            })

        except http_requests.RequestException as e:
            return jsonify({
                'success': False,
                'test_result': {
                    'error': str(e),
                    'success': False
                }
            })

    except Exception as e:
        logger.error(f"Error testing webhook: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/jobs/<job_id>/cancel', methods=['POST'])
@require_api_key
def cancel_job(job_id):
    """Cancel a pending or running job."""
    try:
        db = get_db()

        # Check job exists and belongs to user
        result = db.execute("""
            SELECT job_id, status FROM jobs
            WHERE job_id = %s AND creator_id = %s
        """, (job_id, request.api_user_id))

        job = result.fetchone()

        if not job:
            return jsonify({'error': 'Job not found'}), 404

        if job['status'] not in ['pending', 'running', 'queued']:
            return jsonify({
                'error': 'Cannot cancel job',
                'message': f'Job status is "{job["status"]}". Only pending/running jobs can be cancelled.'
            }), 400

        # Update job status
        db.execute("""
            UPDATE jobs
            SET status = 'cancelled', message = 'Cancelled via API', updated_at = CURRENT_TIMESTAMP
            WHERE job_id = %s
        """, (job_id,))
        db.commit()

        return jsonify({
            'success': True,
            'job_id': job_id,
            'status': 'cancelled',
            'message': 'Job cancelled successfully'
        })

    except Exception as e:
        logger.error(f"Error cancelling job: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


# ========================================
# EVO2 DNA GENERATION API
# ========================================

import os
import requests as http_requests

# Evo2 API configuration
EVO2_API_URL = os.getenv(
    "EVO2_API_URL",
    "https://health.api.nvidia.com/v1/biology/arc/evo2-40b/generate"
)
EVO2_API_KEY = os.getenv("NVIDIA_API_KEY", "")
EVO2_CREDIT_COST = 20


@app.route('/v1/evo2/info', methods=['GET'])
@require_api_key
def evo2_model_info():
    """
    Get Evo2 model information and user's available credits.

    Returns model parameters, limits, and pricing information.
    """
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (request.api_user_id,)
        ).fetchone()

        return jsonify({
            'model': {
                'id': 'evo2-40b',
                'name': 'Evo2-40B',
                'description': 'DNA sequence generation model from BioNeMo',
                'provider': 'BioNeMo',
                'type': 'dna_generation'
            },
            'parameters': {
                'sequence': {
                    'description': 'Input DNA seed sequence',
                    'type': 'string',
                    'required': True,
                    'valid_characters': 'A, T, C, G, N'
                },
                'num_tokens': {
                    'description': 'Number of nucleotides to generate',
                    'type': 'integer',
                    'min': 1,
                    'max': 1200,
                    'default': 128
                },
                'temperature': {
                    'description': 'Sampling temperature (higher = more random)',
                    'type': 'float',
                    'min': 0.0,
                    'max': 1.3,
                    'default': 0.7
                },
                'top_k': {
                    'description': 'Top-k sampling parameter',
                    'type': 'integer',
                    'min': 0,
                    'max': 6,
                    'default': 4
                },
                'top_p': {
                    'description': 'Nucleus sampling parameter',
                    'type': 'float',
                    'min': 0.0,
                    'max': 1.0,
                    'default': 0.9
                },
                'enable_sampled_probs': {
                    'description': 'Return probability scores for generated nucleotides',
                    'type': 'boolean',
                    'default': True
                }
            },
            'pricing': {
                'base_cost': EVO2_CREDIT_COST,
                'currency': 'credits'
            },
            'user': {
                'credits_available': user['credits'] if user else 0
            }
        })
    except Exception as e:
        logger.error(f"Evo2 info error: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.route('/v1/evo2/generate', methods=['POST'])
@require_api_key
def evo2_generate_sequence():
    """
    Generate DNA sequence using Evo2-40B model.

    Request body:
        sequence: Input DNA seed sequence (required, valid chars: ATCGN)
        num_tokens: Number of nucleotides to generate (1-1200, default: 128)
        temperature: Sampling temperature (0-1.3, default: 0.7)
        top_k: Top-k sampling (0-6, default: 4)
        top_p: Nucleus sampling (0-1, default: 0.9)
        enable_sampled_probs: Return probabilities (default: true)

    Returns:
        Generated DNA sequence and optional probability scores
    """
    start_time = time.time()

    try:
        data = request.get_json()

        if not data:
            return jsonify({
                'error': 'Bad Request',
                'message': 'No JSON data provided'
            }), 400

        # Validate sequence
        sequence = data.get('sequence', '').strip().upper()

        if not sequence:
            return jsonify({
                'error': 'Bad Request',
                'message': 'Missing required field: sequence'
            }), 400

        # Validate sequence characters
        valid_nucleotides = set('ATCGN')
        invalid_chars = set(sequence) - valid_nucleotides
        if invalid_chars:
            return jsonify({
                'error': 'Bad Request',
                'message': f'Invalid characters in sequence: {", ".join(sorted(invalid_chars))}. Valid characters: A, T, C, G, N'
            }), 400

        # Check user credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (request.api_user_id,)
        ).fetchone()

        if not user:
            return jsonify({'error': 'User not found'}), 404

        if user['credits'] < EVO2_CREDIT_COST:
            return jsonify({
                'error': 'Insufficient Credits',
                'message': f'This operation requires {EVO2_CREDIT_COST} credits. You have {user["credits"]} credits.',
                'credits_required': EVO2_CREDIT_COST,
                'credits_available': user['credits']
            }), 402

        # Extract and validate parameters
        num_tokens = min(max(1, int(data.get('num_tokens', 128))), 1200)
        temperature = min(max(0.0, float(data.get('temperature', 0.7))), 1.3)
        top_k = min(max(0, int(data.get('top_k', 4))), 6)
        top_p = min(max(0.0, float(data.get('top_p', 0.9))), 1.0)
        # Force enable_sampled_probs to True
        enable_probs = True

        # Prepare API request
        headers = {
            "Authorization": f"Bearer {EVO2_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "sequence": sequence,
            "num_tokens": num_tokens,
            "temperature": temperature,
            "top_k": top_k,
            "top_p": top_p,
            "enable_sampled_probs": enable_probs
        }

        logger.info(f"Evo2 API request: user={request.api_user_id}, tokens={num_tokens}, seq_len={len(sequence)}")

        # Call Evo2 API
        try:
            response = http_requests.post(
                url=EVO2_API_URL,
                headers=headers,
                json=payload,
                timeout=120
            )
        except http_requests.exceptions.Timeout:
            return jsonify({
                'error': 'Gateway Timeout',
                'message': 'The upstream service took too long to respond'
            }), 504
        except http_requests.exceptions.ConnectionError:
            return jsonify({
                'error': 'Service Unavailable',
                'message': 'Unable to connect to the DNA generation service'
            }), 503
        except http_requests.exceptions.RequestException as e:
            logger.error(f"Evo2 request error: {e}")
            return jsonify({
                'error': 'Bad Gateway',
                'message': 'Error communicating with upstream service'
            }), 502

        if response.status_code != 200:
            logger.error(f"Evo2 API error: {response.status_code} - {response.text[:500]}")

            if response.status_code == 429:
                return jsonify({
                    'error': 'Rate Limited',
                    'message': 'Too many requests to the DNA generation service. Please try again later.'
                }), 429
            elif response.status_code >= 500:
                return jsonify({
                    'error': 'Bad Gateway',
                    'message': 'The upstream service returned an error'
                }), 502
            elif response.status_code in [401, 403]:
                return jsonify({
                    'error': 'Service Unavailable',
                    'message': 'DNA generation service is temporarily unavailable'
                }), 503
            else:
                return jsonify({
                    'error': 'Bad Gateway',
                    'message': 'Unexpected response from upstream service'
                }), 502

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            return jsonify({
                'error': 'Bad Gateway',
                'message': 'Invalid response format from upstream service'
            }), 502

        result = response.json()

        # Deduct credits
        db.execute(
            'UPDATE users SET credits = credits - %s WHERE id = %s',
            (EVO2_CREDIT_COST, request.api_user_id)
        )
        db.commit()

        response_time = int((time.time() - start_time) * 1000)

        # Log API usage
        log_api_usage(
            api_key_id=request.api_key_id,
            user_id=request.api_user_id,
            endpoint='/v1/evo2/generate',
            model_type='evo2',
            status_code=200,
            response_time_ms=response_time,
            credits_used=EVO2_CREDIT_COST,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )

        generated_sequence = result.get('sequence', '')

        logger.info(f"Evo2 generation completed: user={request.api_user_id}, generated_len={len(generated_sequence)}")

        return jsonify({
            'success': True,
            'model': 'evo2-40b',
            'input': {
                'sequence': sequence,
                'sequence_length': len(sequence)
            },
            'output': {
                'sequence': generated_sequence,
                'sequence_length': len(generated_sequence),
                'sampled_probs': result.get('sampled_probs') if enable_probs else None
            },
            'parameters': {
                'num_tokens': num_tokens,
                'temperature': temperature,
                'top_k': top_k,
                'top_p': top_p
            },
            'usage': {
                'credits_used': EVO2_CREDIT_COST,
                'response_time_ms': response_time
            }
        })

    except ValueError as e:
        return jsonify({
            'error': 'Bad Request',
            'message': f'Invalid parameter value: {str(e)}'
        }), 400
    except Exception as e:
        logger.error(f"Evo2 generation error: {e}", exc_info=True)
        return jsonify({
            'error': 'Internal Server Error',
            'message': 'An unexpected error occurred'
        }), 500
