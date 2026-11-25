"""API routes for ProteinMPNN-RE inverse folding / sequence design (NVIDIA NIM)."""
from __future__ import annotations

import json
import os
import random
import time
import requests
from flask import jsonify, request, session, render_template

from ..app import app, get_redis_connection, logger
from ..helpers import get_db, login_required
from ..helpers.security import csrf_protect


# ProteinMPNN-RE API configuration
NVIDIA_PROTEINMPNN_URL = os.getenv(
    "PROTEINMPNN_API_URL",
    "https://health.api.nvidia.com/v1/biology/ipd/proteinmpnn/predict"
)
NVIDIA_STATUS_URL = os.getenv(
    "PROTEINMPNN_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
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

# Cost per prediction (credits)
PROTEINMPNN_CREDIT_COST = 20

# NVCF polling settings
NVCF_POLL_SECONDS = 300
NVCF_POLL_INTERVAL = 2


def _cloudflare_error(status_code: int = 502) -> tuple:
    """Generate a Cloudflare-style error response to mask upstream errors."""
    ray_id = ''.join(random.choices('0123456789abcdef', k=16))

    error_messages = {
        502: {
            'code': 502,
            'title': 'Bad Gateway',
            'message': 'The web server reported a bad gateway error.',
            'ray_id': ray_id
        },
        503: {
            'code': 503,
            'title': 'Service Temporarily Unavailable',
            'message': 'The server is temporarily unable to service your request. Please try again later.',
            'ray_id': ray_id
        },
        504: {
            'code': 504,
            'title': 'Gateway Timeout',
            'message': 'The connection timed out. The server took too long to respond.',
            'ray_id': ray_id
        },
        520: {
            'code': 520,
            'title': 'Web Server Returned an Unknown Error',
            'message': 'The origin web server returned an unexpected response.',
            'ray_id': ray_id
        },
        521: {
            'code': 521,
            'title': 'Web Server Is Down',
            'message': 'The origin web server is refusing connections.',
            'ray_id': ray_id
        },
        522: {
            'code': 522,
            'title': 'Connection Timed Out',
            'message': 'The initial connection between Cloudflare and the origin web server timed out.',
            'ray_id': ray_id
        },
        524: {
            'code': 524,
            'title': 'A Timeout Occurred',
            'message': 'The origin server took too long to respond.',
            'ray_id': ray_id
        }
    }

    error_info = error_messages.get(status_code, error_messages[502])

    return jsonify({
        'error': True,
        'cloudflare': True,
        'code': error_info['code'],
        'title': error_info['title'],
        'message': error_info['message'],
        'ray_id': error_info['ray_id']
    }), error_info['code']


# ============================================================================
# PAGE ROUTE
# ============================================================================

@app.route('/proteinmpnn-re')
@login_required
def proteinmpnn_re_page():
    """Render the ProteinMPNN-RE sequence design page."""
    db = get_db()
    user_id = session.get('user_id')

    # Get user credits
    user = db.execute(
        'SELECT credits FROM users WHERE id = %s', (user_id,)
    ).fetchone()
    user_credits = user['credits'] if user else 0

    return render_template('proteinmpnn_re.html', user_credits=user_credits)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/proteinmpnn-re/predict', methods=['POST'])
@login_required
@csrf_protect
def proteinmpnn_re_predict():
    """
    Submit a protein sequence design prediction using ProteinMPNN-RE (NVIDIA NIM).

    Request body:
        input_pdb: PDB structure content (string, required)
        input_pdb_chain_id: Chain ID to design (string, required, e.g. "A")
        ca_only: Use only C-alpha atoms (boolean, optional, default false)
        use_soluble_model: Use soluble model instead of membrane (boolean, optional, default true)
        num_seq_per_target: Number of sequences to generate (1-100, default 1)
        sampling_temp: Sampling temperatures, comma-separated (string, optional, default "0.1")
        seed: Random seed (integer, optional, default 37)
        batch_size: Batch size (1-100, default 1)

    Returns:
        JSON with designed protein sequences (FASTA format) or async task_id
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        # Validate PDB input
        input_pdb = data.get('input_pdb', '').strip()
        if not input_pdb:
            return jsonify({'error': True, 'message': 'No PDB structure provided'}), 400

        # Validate PDB contains ATOM records
        pdb_lines = input_pdb.split('\n')
        atom_lines = [l for l in pdb_lines if l.startswith('ATOM')]
        if not atom_lines:
            return jsonify({
                'error': True,
                'message': 'Invalid PDB structure: must contain ATOM records'
            }), 400

        # Limit PDB size (reasonable upper bound)
        if len(input_pdb) > 10_000_000:
            return jsonify({
                'error': True,
                'message': 'PDB structure too large (max 10 MB)'
            }), 400

        # Parse chains to design (optional - defaults to all chains)
        pdb_path_chains_raw = data.get('pdb_path_chains', '')
        pdb_path_chains = None
        if pdb_path_chains_raw:
            if isinstance(pdb_path_chains_raw, list):
                pdb_path_chains = [c.strip() for c in pdb_path_chains_raw if c.strip()]
            elif isinstance(pdb_path_chains_raw, str):
                pdb_path_chains = [c.strip() for c in pdb_path_chains_raw.split(',') if c.strip()]

        # Get user and check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < PROTEINMPNN_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {PROTEINMPNN_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Extract optional parameters with defaults and clamping
        ca_only = bool(data.get('ca_only', False))
        use_soluble_model = bool(data.get('use_soluble_model', False))
        num_seq_per_target = min(max(1, int(data.get('num_seq_per_target', 1))), 100)

        # sampling_temp: parse as array of floats (NVIDIA API expects array)
        sampling_temp_raw = data.get('sampling_temp', '0.1')
        if isinstance(sampling_temp_raw, list):
            sampling_temp = [max(0.001, min(float(t), 0.999)) for t in sampling_temp_raw]
        elif isinstance(sampling_temp_raw, str):
            sampling_temp = [max(0.001, min(float(t.strip()), 0.999)) for t in sampling_temp_raw.split(',') if t.strip()]
        else:
            sampling_temp = [max(0.001, min(float(sampling_temp_raw), 0.999))]
        if not sampling_temp:
            sampling_temp = [0.1]

        seed_raw = data.get('seed')
        pssm_multi = float(data.get('pssm_multi', 0))
        pssm_threshold = float(data.get('pssm_threshold', 0))
        pssm_bias_flag = bool(data.get('pssm_bias_flag', False))

        # Prepare API request
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": "30",
        }

        payload = {
            "input_pdb": input_pdb,
            "ca_only": ca_only,
            "use_soluble_model": use_soluble_model,
            "num_seq_per_target": num_seq_per_target,
            "sampling_temp": sampling_temp,
            "pssm_multi": pssm_multi,
            "pssm_threshold": pssm_threshold,
            "pssm_bias_flag": pssm_bias_flag,
        }

        if pdb_path_chains:
            payload["pdb_path_chains"] = pdb_path_chains
        if seed_raw is not None:
            payload["seed"] = int(seed_raw)

        chains_str = ','.join(pdb_path_chains) if pdb_path_chains else 'all'
        logger.info(
            f"ProteinMPNN-RE prediction request: user={session['user_id']}, "
            f"chains={chains_str}, num_seq={num_seq_per_target}, "
            f"pdb_len={len(input_pdb)}"
        )

        # Call ProteinMPNN-RE API (60s connect, 90s read)
        response = requests.post(
            url=NVIDIA_PROTEINMPNN_URL,
            headers=headers,
            json=payload,
            timeout=(60, 90)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                logger.error(f"ProteinMPNN-RE unexpected content type: {content_type}")
                return _cloudflare_error(520)

            # Immediate result - deduct credits and return
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (PROTEINMPNN_CREDIT_COST, session['user_id'])
            )
            db.commit()
            logger.info(f"ProteinMPNN-RE prediction completed (sync): user={session['user_id']}")
            return jsonify(result)

        elif response.status_code == 202:
            # Async task - return task_id immediately so frontend can poll
            nvcf_reqid = response.headers.get("NVCF-REQID")
            if not nvcf_reqid:
                logger.error("ProteinMPNN-RE 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(f"ProteinMPNN-RE async task started: {nvcf_reqid}, user={session['user_id']}")

            # Store task metadata in Redis (TTL: 10 minutes)
            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': PROTEINMPNN_CREDIT_COST,
                    'started_at': time.time(),
                    'pdb_path_chains': pdb_path_chains or [],
                    'num_seq_per_target': num_seq_per_target
                })
                r.setex(f"proteinmpnn-re:task:{nvcf_reqid}", 28800, task_meta)
            except Exception as redis_err:
                logger.warning(f"ProteinMPNN-RE Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            logger.error(f"ProteinMPNN-RE API error: {response.status_code} - {response.text[:500]}")
            if response.status_code == 429:
                return _cloudflare_error(503)
            elif response.status_code >= 500:
                return _cloudflare_error(502)
            elif response.status_code == 401 or response.status_code == 403:
                return _cloudflare_error(521)
            else:
                return _cloudflare_error(520)

    except requests.exceptions.Timeout:
        logger.error("ProteinMPNN-RE API timeout")
        return _cloudflare_error(524)
    except requests.exceptions.ConnectionError:
        logger.error("ProteinMPNN-RE connection error")
        return _cloudflare_error(522)
    except requests.exceptions.RequestException as e:
        logger.error(f"ProteinMPNN-RE network error: {e}")
        return _cloudflare_error(502)
    except Exception as e:
        logger.error(f"ProteinMPNN-RE prediction error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/proteinmpnn-re/status/<task_id>', methods=['GET'])
@login_required
def proteinmpnn_re_status(task_id):
    """Check status of an async ProteinMPNN-RE prediction task."""
    try:
        # Validate task_id format (basic safety check)
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        # Look up task metadata in Redis
        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"proteinmpnn-re:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"ProteinMPNN-RE Redis lookup failed: {redis_err}")

        if not task_meta:
            return jsonify({'error': True, 'message': 'Task not found or expired'}), 404

        # Verify this task belongs to the current user
        if task_meta.get('user_id') != session['user_id']:
            return jsonify({'error': True, 'message': 'Access denied'}), 403

        # Poll NVIDIA status once
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json"
        }
        status_url = NVIDIA_STATUS_URL.format(task_id=task_id)

        try:
            status_response = requests.get(status_url, headers=headers, timeout=30)
        except requests.exceptions.Timeout:
            return jsonify({'status': 'pending', 'message': 'Status check timed out, still processing'})
        except requests.exceptions.RequestException as e:
            logger.error(f"ProteinMPNN-RE status check error: {e}")
            return jsonify({'status': 'pending', 'message': 'Status check failed, still processing'})

        if status_response.status_code == 202:
            # Still processing
            elapsed = int(time.time() - task_meta.get('started_at', time.time()))
            return jsonify({'status': 'pending', 'elapsed': elapsed})

        elif status_response.status_code == 200:
            # Completed - process result
            content_type = status_response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = status_response.json()
            else:
                result = {"raw_content": status_response.text[:5000]}

            # Deduct credits
            db = get_db()
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (task_meta['credit_cost'], session['user_id'])
            )
            db.commit()

            # Remove task from Redis
            try:
                r = get_redis_connection()
                r.delete(f"proteinmpnn-re:task:{task_id}")
            except Exception:
                pass

            logger.info(f"ProteinMPNN-RE async task completed: {task_id}, user={session['user_id']}")
            return jsonify(result)

        elif status_response.status_code == 404:
            # Task expired or not found
            try:
                r = get_redis_connection()
                r.delete(f"proteinmpnn-re:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"ProteinMPNN-RE status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"ProteinMPNN-RE status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/proteinmpnn-re/info', methods=['GET'])
@login_required
def proteinmpnn_re_info():
    """Get ProteinMPNN-RE model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'model': 'ProteinMPNN-RE',
            'provider': 'IPD',
            'description': 'Inverse folding model for protein sequence design from PDB structure',
            'credit_cost': PROTEINMPNN_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'parameters': {
                'input_pdb': {'type': 'string', 'required': True, 'description': 'Input PDB structure content'},
                'pdb_path_chains': {'type': 'array', 'required': False, 'description': 'Chain IDs to design (defaults to all chains)'},
                'ca_only': {'type': 'boolean', 'default': False, 'description': 'CA-only model for alpha carbon focus'},
                'use_soluble_model': {'type': 'boolean', 'default': False, 'description': 'Use soluble protein model'},
                'num_seq_per_target': {'type': 'integer', 'min': 1, 'max': 100, 'default': 1, 'description': 'Number of sequences to generate'},
                'sampling_temp': {'type': 'array[number]', 'default': [0.1], 'description': 'Sampling temperatures (0-1 exclusive, recommended 0.1-0.3)'},
                'seed': {'type': 'integer', 'optional': True, 'description': 'Random seed for reproducibility'},
                'pssm_multi': {'type': 'number', 'default': 0, 'min': 0, 'max': 1, 'description': 'PSSM influence weight'},
                'pssm_threshold': {'type': 'number', 'default': 0, 'description': 'PSSM threshold for filtering'},
                'pssm_bias_flag': {'type': 'boolean', 'default': False, 'description': 'Apply PSSM bias'}
            },
            'input_formats': {
                'structure': ['pdb']
            },
            'output_format': 'FASTA',
            'capabilities': [
                'Inverse protein folding / sequence design',
                'Multi-sequence generation per target',
                'Temperature-controlled sampling',
                'Soluble and membrane protein support',
                'C-alpha only mode',
                'Fast hosted inference'
            ]
        })
    except Exception as e:
        logger.error(f"ProteinMPNN-RE info error: {e}")
        return _cloudflare_error(520)
