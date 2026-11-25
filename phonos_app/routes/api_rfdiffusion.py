"""
RFDiffusion API Routes
Protein structure generation using NVIDIA RFDiffusion API.
"""
import os
import json
import base64
import logging
import re
import time
from datetime import datetime
from functools import wraps

import redis
import requests
from flask import Blueprint, request, jsonify, render_template, session, current_app

from phonos_app.helpers.auth import login_required
from phonos_app.helpers.security import csrf_protect
from phonos_app.helpers.database import get_db
from phonos_app.helpers.credits import ensure_monthly_credit_reset

logger = logging.getLogger(__name__)

rfdiffusion_bp = Blueprint('rfdiffusion', __name__)

# Credit cost per prediction
CREDIT_COST = 20

# NVIDIA API configuration
class _DynamicNvidiaKey:
    """Resolves NVIDIA_API_KEY dynamically (User BYOK -> Env -> Encrypted Instance DB)."""
    def _val(self) -> str:
        try:
            from flask import has_request_context, session
            from ..helpers.model_providers import resolve_nvidia_api_key
            uid = session.get("user_id") if has_request_context() else None
            val = resolve_nvidia_api_key(user_id=uid)
            if val:
                return val
        except Exception:
            pass
        return os.environ.get("NVIDIA_API_KEY", "") or ""

    def __bool__(self) -> bool:
        return bool(self._val())

    def __str__(self) -> str:
        return self._val()

    def __len__(self) -> int:
        return len(self._val())

    def strip(self, *args, **kwargs) -> str:
        return self._val().strip(*args, **kwargs)

NVIDIA_API_KEY = _DynamicNvidiaKey()
RFDIFFUSION_ENDPOINT = "https://health.api.nvidia.com/v1/biology/ipd/rfdiffusion/generate"

# Redis configuration
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)

# Job queue
JOB_QUEUE = "queue:job:type-12"


def get_redis_client():
    """Get Redis client."""
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True
    )


def mask_api_errors(f):
    """Decorator to mask internal API errors with Cloudflare-style codes."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except requests.exceptions.Timeout:
            return jsonify({
                'error': 'Gateway Timeout',
                'message': 'The request timed out while waiting for the upstream server.',
                'code': 524
            }), 524
        except requests.exceptions.ConnectionError:
            return jsonify({
                'error': 'Connection Timeout',
                'message': 'Unable to connect to the upstream server.',
                'code': 522
            }), 522
        except Exception as e:
            logger.error(f"API error: {str(e)}")
            return jsonify({
                'error': 'Unknown Error',
                'message': 'An unexpected error occurred.',
                'code': 520
            }), 520
    return wrapper


@rfdiffusion_bp.route('/rfdiffusion')
@login_required
def rfdiffusion_page():
    """Render RFDiffusion interface."""
    return render_template('rfdiffusion.html')


@rfdiffusion_bp.route('/api/rfdiffusion/info', methods=['GET'])
@login_required
def get_rfdiffusion_info():
    """Get RFDiffusion model info and user credits."""
    user_id = session.get('user_id')
    db = get_db()

    # Ensure monthly credit reset
    ensure_monthly_credit_reset(db, user_id)

    user = db.execute(
        'SELECT credits, plan FROM users WHERE id = %s',
        (user_id,)
    ).fetchone()

    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify({
        'model': 'RFDiffusion',
        'provider': 'Institute for Protein Design',
        'description': 'Protein structure generation using diffusion-based de novo design',
        'credit_cost': CREDIT_COST,
        'user_credits': user['credits'],
        'user_plan': user['plan'],
        'parameters': {
            'diffusion_steps': {
                'min': 1,
                'max': 200,
                'default': 50,
                'description': 'Number of diffusion denoising steps'
            },
            'num_designs': {
                'min': 1,
                'max': 8,
                'default': 1,
                'description': 'Number of designs to generate'
            },
            'noise_scale': {
                'min': 0.1,
                'max': 2.0,
                'default': 1.0,
                'description': 'Scale of noise during diffusion'
            }
        },
        'features': [
            'De novo protein design',
            'Binder design with hotspot residues',
            'Scaffold generation',
            'Partial diffusion for motif scaffolding'
        ]
    })


@rfdiffusion_bp.route('/api/rfdiffusion/predict', methods=['POST'])
@login_required
@csrf_protect
@mask_api_errors
def predict():
    """
    Submit RFDiffusion prediction job.

    Request body:
    {
        "input_pdb": "ATOM...",          # PDB content or base64 encoded
        "contigs": "A20-60/0 50-100",    # Contig specification
        "hotspot_res": ["A50", "A51"],   # Optional hotspot residues
        "diffusion_steps": 50,            # 1-200
        "num_designs": 1,                 # 1-8
        "noise_scale": 1.0,               # 0.1-2.0
        "partial_T": null,                # Optional partial diffusion
        "project_id": 123                 # Optional project to save to
    }
    """
    user_id = session.get('user_id')
    db = get_db()

    # Ensure monthly credit reset
    ensure_monthly_credit_reset(db, user_id)

    # Check credits
    user = db.execute(
        'SELECT credits FROM users WHERE id = %s',
        (user_id,)
    ).fetchone()

    if not user or user['credits'] < CREDIT_COST:
        return jsonify({
            'error': 'Insufficient credits',
            'required': CREDIT_COST,
            'available': user['credits'] if user else 0
        }), 402

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Extract and validate input PDB
    input_pdb = data.get('input_pdb', '')

    # Handle base64 encoded input
    if input_pdb and not input_pdb.startswith('ATOM') and not input_pdb.startswith('HEADER'):
        try:
            input_pdb = base64.b64decode(input_pdb).decode('utf-8')
        except:
            pass  # Not base64, use as-is

    if not input_pdb or len(input_pdb.strip()) < 50:
        return jsonify({'error': 'Invalid or missing input_pdb'}), 400

    # Validate contigs
    contigs = data.get('contigs', '')
    if not contigs:
        return jsonify({'error': 'Contigs specification required'}), 400

    # Validate contigs format
    contig_parts = re.split(r'[/\s]+', contigs.strip())
    for part in contig_parts:
        if part and not re.match(r'^[A-Z]?\d+(-\d+)?$', part):
            return jsonify({'error': f'Invalid contig format: {part}'}), 400

    # Validate hotspot residues
    hotspot_res = data.get('hotspot_res', [])
    if hotspot_res:
        if isinstance(hotspot_res, str):
            hotspot_res = [h.strip() for h in hotspot_res.split(',') if h.strip()]
        for hs in hotspot_res:
            if not re.match(r'^[A-Z]\d+$', hs):
                return jsonify({'error': f'Invalid hotspot format: {hs} (expected: A50)'}), 400

    # Get parameters with validation
    diffusion_steps = max(1, min(200, int(data.get('diffusion_steps', 50))))
    num_designs = max(1, min(8, int(data.get('num_designs', 1))))
    noise_scale = max(0.1, min(2.0, float(data.get('noise_scale', 1.0))))
    partial_T = data.get('partial_T')
    if partial_T is not None:
        partial_T = max(1, min(diffusion_steps, int(partial_T)))

    project_id = data.get('project_id')

    # Sync mode - call API directly for quick jobs
    if diffusion_steps <= 15 and num_designs == 1:
        return _sync_predict(
            db, user_id, input_pdb, contigs, hotspot_res,
            diffusion_steps, noise_scale, partial_T, project_id
        )

    # Async mode - queue job for worker
    return _async_predict(
        db, user_id, input_pdb, contigs, hotspot_res,
        diffusion_steps, num_designs, noise_scale, partial_T, project_id
    )


def _sync_predict(db, user_id, input_pdb, contigs, hotspot_res,
                  diffusion_steps, noise_scale, partial_T, project_id):
    """Synchronous prediction for small jobs."""
    if not NVIDIA_API_KEY:
        return jsonify({'error': 'Model provider API key not configured'}), 503

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "NVCF-POLL-SECONDS": "30",
    }

    payload = {
        "input_pdb": input_pdb,
        "contigs": contigs,
        "diffusion_steps": diffusion_steps,
    }

    if hotspot_res:
        payload["hotspot_res"] = hotspot_res
    # noise_scale is no longer accepted by the NVIDIA RFDiffusion API
    if partial_T is not None:
        payload["partial_T"] = partial_T

    start_time = time.time()

    try:
        response = requests.post(
            RFDIFFUSION_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=300
        )

        if response.status_code != 200:
            return jsonify({
                'error': 'API error',
                'message': response.text[:200]
            }), response.status_code

        result = response.json()
        execution_time = time.time() - start_time

        # Deduct credits
        db.execute(
            'UPDATE users SET credits = credits - %s WHERE id = %s',
            (CREDIT_COST, user_id)
        )
        db.commit()

        # Parse output structures
        structures = _parse_structures(result)

        return jsonify({
            'status': 'success',
            'structures': structures,
            'execution_time': execution_time,
            'credits_used': CREDIT_COST,
            'parameters': {
                'contigs': contigs,
                'diffusion_steps': diffusion_steps,
                'hotspot_res': hotspot_res
            }
        })

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timeout'}), 504
    except Exception as e:
        logger.error(f"Sync prediction error: {e}")
        return jsonify({'error': str(e)}), 500


def _async_predict(db, user_id, input_pdb, contigs, hotspot_res,
                   diffusion_steps, num_designs, noise_scale, partial_T, project_id):
    """Queue job for async processing."""
    import uuid

    job_id = str(uuid.uuid4())

    job_data = {
        'job_id': job_id,
        'user_id': user_id,
        'model': 'type-12',
        'task': 'protein_design',
        'project_id': project_id,
        'params': {
            'input_pdb': input_pdb,
            'contigs': contigs,
            'hotspot_res': hotspot_res,
            'diffusion_steps': diffusion_steps,
            'num_designs': num_designs,
            'noise_scale': noise_scale,
            'partial_T': partial_T
        },
        'created_at': datetime.utcnow().isoformat(),
        'credit_cost': CREDIT_COST
    }

    try:
        r = get_redis_client()
        r.lpush(JOB_QUEUE, json.dumps(job_data))

        # Create job record in database
        db.execute('''
            INSERT INTO jobs (
                job_id, user_id, project_id, model, status,
                task, parameters, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ''', (
            job_id, user_id, project_id, 'type-12', 'queued',
            'protein_design', json.dumps(job_data['params'])
        ))
        db.commit()

        return jsonify({
            'status': 'queued',
            'job_id': job_id,
            'message': 'Job queued for processing',
            'estimated_time': f'{diffusion_steps * num_designs * 2}s'
        }), 202

    except Exception as e:
        logger.error(f"Failed to queue job: {e}")
        return jsonify({'error': 'Failed to queue job'}), 500


def _parse_structures(result):
    """Parse structures from API response."""
    structures = []

    output_pdb = result.get('output_pdb', '')

    if 'MODEL' in output_pdb:
        # Multiple models
        models = re.split(r'\nENDMDL\s*\n?', output_pdb)
        for i, model in enumerate(models):
            if not model.strip():
                continue
            clean = '\n'.join(
                line for line in model.split('\n')
                if not line.startswith('MODEL')
            ).strip()
            if clean:
                structures.append({
                    'index': i,
                    'name': f'Design {i + 1}',
                    'pdb': clean + '\nEND\n',
                    'pdb_b64': base64.b64encode((clean + '\nEND\n').encode()).decode()
                })
    else:
        # Single structure
        structures.append({
            'index': 0,
            'name': 'Design 1',
            'pdb': output_pdb,
            'pdb_b64': base64.b64encode(output_pdb.encode()).decode()
        })

    return structures


@rfdiffusion_bp.route('/api/rfdiffusion/examples', methods=['GET'])
@login_required
def get_examples():
    """Get example PDB structures and contig specifications."""
    examples = [
        {
            'id': 'binder_design',
            'name': 'Binder Design',
            'description': 'Design a binder to a target protein with specified hotspot residues',
            'contigs': 'A1-100/0 50-80',
            'hotspot_res': ['A50', 'A51', 'A52', 'A53', 'A54'],
            'diffusion_steps': 50,
            'pdb_url': 'https://files.rcsb.org/download/1R42.pdb'
        },
        {
            'id': 'scaffold',
            'name': 'Scaffold Generation',
            'description': 'Generate a protein scaffold of specified length',
            'contigs': '100-150',
            'hotspot_res': [],
            'diffusion_steps': 50,
            'pdb_url': None
        },
        {
            'id': 'motif_scaffold',
            'name': 'Motif Scaffolding',
            'description': 'Build a scaffold around a fixed motif',
            'contigs': 'A10-25/0 B5-20/0 30-50',
            'hotspot_res': [],
            'diffusion_steps': 100,
            'pdb_url': 'https://files.rcsb.org/download/1QYS.pdb'
        }
    ]

    return jsonify({'examples': examples})


@rfdiffusion_bp.route('/api/rfdiffusion/fetch-pdb/<pdb_id>', methods=['GET'])
@login_required
def fetch_pdb(pdb_id):
    """Fetch PDB from RCSB."""
    pdb_id = pdb_id.upper()[:4]

    if not re.match(r'^[A-Z0-9]{4}$', pdb_id):
        return jsonify({'error': 'Invalid PDB ID'}), 400

    try:
        url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            return jsonify({'error': f'PDB {pdb_id} not found'}), 404

        pdb_content = response.text

        # Filter to ATOM records only for smaller payload
        atom_lines = [
            line for line in pdb_content.split('\n')
            if line.startswith('ATOM') or line.startswith('HETATM') or line.startswith('TER')
        ]

        # Limit to first 5000 atoms
        if len(atom_lines) > 5000:
            atom_lines = atom_lines[:5000]
            atom_lines.append('END')

        filtered_pdb = '\n'.join(atom_lines)

        return jsonify({
            'pdb_id': pdb_id,
            'pdb_content': filtered_pdb,
            'atom_count': len([l for l in atom_lines if l.startswith('ATOM')])
        })

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout fetching PDB'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def register_rfdiffusion_routes(app):
    """Register RFDiffusion routes with Flask app."""
    app.register_blueprint(rfdiffusion_bp)
    logger.info("RFDiffusion routes registered")
