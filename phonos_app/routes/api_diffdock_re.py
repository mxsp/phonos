"""API routes for DiffDock-RE molecular docking prediction (NVIDIA NIM)."""
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


# DiffDock-RE API configuration
NVIDIA_DIFFDOCK_URL = os.getenv(
    "DIFFDOCK_API_URL",
    "https://health.api.nvidia.com/v1/biology/mit/diffdock"
)
NVIDIA_STATUS_URL = os.getenv(
    "DIFFDOCK_STATUS_URL",
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
DIFFDOCK_CREDIT_COST = 20

# NVCF polling settings
NVCF_POLL_SECONDS = 300
NVCF_POLL_INTERVAL = 2

# Valid ligand file types
VALID_LIGAND_FILE_TYPES = {'sdf', 'mol2', 'smiles'}


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

@app.route('/diffdock-re')
@login_required
def diffdock_re_page():
    """Render the DiffDock-RE molecular docking page."""
    db = get_db()
    user_id = session.get('user_id')

    # Get user credits
    user = db.execute(
        'SELECT credits FROM users WHERE id = %s', (user_id,)
    ).fetchone()
    user_credits = user['credits'] if user else 0

    return render_template('diffdock_re.html', user_credits=user_credits)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/diffdock-re/predict', methods=['POST'])
@login_required
@csrf_protect
def diffdock_re_predict():
    """
    Submit a molecular docking prediction using DiffDock-RE.

    Request body:
        protein: Protein structure in PDB format (string, required)
        ligand: Molecular structure in SDF, Mol2, or SMILES format (string, required)
        ligand_file_type: Format of ligand input - "sdf", "mol2", or "smiles" (required)
        num_poses: Number of docking poses to generate (1-20, default 10)
        time_divisions: Diffusion time steps (1-100, default 20)
        steps: Number of diffusion steps (1-100, default 18)
        save_trajectory: Save diffusion trajectory (boolean, default false)
        is_staged: Use staged diffusion (boolean, default false)

    Returns:
        JSON with docked ligand poses, visualization files, and confidence scores
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        # Validate protein input
        protein = data.get('protein', '').strip()
        if not protein:
            return jsonify({'error': True, 'message': 'No protein structure provided (PDB format required)'}), 400

        # Unwrap JSON-encoded PDB (e.g. ESMFold/AlphaFold2 output format {"pdbs":[...]} or {"pdb_content":"..."})
        if protein.startswith('{') or protein.startswith('['):
            try:
                wrapped = json.loads(protein)
                if isinstance(wrapped, dict):
                    if 'pdbs' in wrapped and isinstance(wrapped['pdbs'], list) and wrapped['pdbs']:
                        protein = wrapped['pdbs'][0]
                    elif 'pdb_content' in wrapped:
                        protein = wrapped['pdb_content']
                    elif 'structure' in wrapped:
                        protein = wrapped['structure']
            except (json.JSONDecodeError, KeyError):
                pass

        # Basic PDB format validation - should contain ATOM or MODEL records
        protein_upper = protein.upper()
        has_atom = 'ATOM' in protein_upper or 'HETATM' in protein_upper
        # Check for CIF/mmCIF format
        has_cif = protein_upper.startswith('DATA_') or '_atom_site.' in protein_upper

        if not has_atom and not has_cif:
            return jsonify({
                'error': True,
                'message': 'Invalid protein structure: must be in PDB or mmCIF format containing ATOM records'
            }), 400

        # Detect format from content if not specified
        protein_format = data.get('protein_format', 'pdb').lower()
        if has_cif and protein_format == 'pdb':
            protein_format = 'cif'

        # Limit protein size (reasonable upper bound)
        if len(protein) > 10_000_000:
            return jsonify({
                'error': True,
                'message': 'Protein structure too large (max 10 MB)'
            }), 400

        # Validate ligand input
        ligand = data.get('ligand', '').strip()
        if not ligand:
            return jsonify({'error': True, 'message': 'No ligand provided'}), 400

        ligand_file_type = data.get('ligand_file_type', '').strip().lower()
        if ligand_file_type not in VALID_LIGAND_FILE_TYPES:
            return jsonify({
                'error': True,
                'message': f'Invalid ligand_file_type. Must be one of: {", ".join(sorted(VALID_LIGAND_FILE_TYPES))}'
            }), 400

        # Validate SMILES ligand (basic sanity check)
        if ligand_file_type == 'smiles':
            if len(ligand) > 2000:
                return jsonify({
                    'error': True,
                    'message': 'SMILES string too long (max 2000 characters)'
                }), 400
            # NVIDIA cloud API no longer accepts SMILES directly — convert to SDF
            try:
                from phonos_app.helpers.databases import convert_smiles_to_sdf
                ligand = convert_smiles_to_sdf(ligand)
                ligand_file_type = 'sdf'
                logger.info(f"DiffDock-RE: Converted SMILES to SDF ({len(ligand)} chars)")
            except (ValueError, ImportError) as conv_err:
                logger.error(f"DiffDock-RE SMILES→SDF conversion failed: {conv_err}")
                return jsonify({
                    'error': True,
                    'message': f'Failed to process SMILES ligand: {str(conv_err)}'
                }), 400
        else:
            # SDF/Mol2 size limit
            if len(ligand) > 1_000_000:
                return jsonify({
                    'error': True,
                    'message': 'Ligand file too large (max 1 MB)'
                }), 400

        # Get user and check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < DIFFDOCK_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {DIFFDOCK_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Extract optional parameters with clamping
        num_poses = min(max(1, int(data.get('num_poses', 10))), 20)
        time_divisions = min(max(1, int(data.get('time_divisions', 20))), 100)
        steps = min(max(1, int(data.get('steps', 18))), 100)
        save_trajectory = bool(data.get('save_trajectory', False))
        is_staged = bool(data.get('is_staged', False))
        no_final_step_noise = bool(data.get('no_final_step_noise', False))

        # Prepare API request
        if not NVIDIA_API_KEY:
            logger.error("DiffDock-RE request rejected: NVIDIA_API_KEY is not configured")
            return jsonify({'error': True, 'message': 'Model backend is not configured.'}), 503

        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": "30",
        }

        payload = {
            "protein": protein,
            "ligand": ligand,
            "ligand_file_type": ligand_file_type,
            "num_poses": num_poses,
            "time_divisions": time_divisions,
            "steps": steps,
            "save_trajectory": save_trajectory,
            "is_staged": is_staged,
            "no_final_step_noise": no_final_step_noise
        }

        logger.info(
            f"DiffDock-RE prediction request: user={session['user_id']}, "
            f"ligand_type={ligand_file_type}, num_poses={num_poses}, "
            f"protein_len={len(protein)}, ligand_len={len(ligand)}"
        )

        # Call DiffDock-RE API (60s connect, 90s read)
        response = requests.post(
            url=NVIDIA_DIFFDOCK_URL,
            headers=headers,
            json=payload,
            timeout=(60, 90)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                logger.error(f"DiffDock-RE unexpected content type: {content_type}")
                return _cloudflare_error(520)

            # Immediate result - deduct credits and return
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (DIFFDOCK_CREDIT_COST, session['user_id'])
            )
            db.commit()

            num_poses_returned = len(result.get('position_confidence', result.get('pose_confidence', [])))
            logger.info(
                f"DiffDock-RE prediction completed (sync): user={session['user_id']}, "
                f"poses={num_poses_returned}"
            )
            return jsonify(result)

        elif response.status_code == 202:
            # Async task - return task_id immediately so frontend can poll
            nvcf_reqid = response.headers.get("NVCF-REQID")
            if not nvcf_reqid:
                logger.error("DiffDock-RE 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(f"DiffDock-RE async task started: {nvcf_reqid}, user={session['user_id']}")

            # Store task metadata in Redis (TTL: 10 minutes)
            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': DIFFDOCK_CREDIT_COST,
                    'started_at': time.time(),
                    'num_poses': num_poses,
                    'ligand_file_type': ligand_file_type
                })
                r.setex(f"diffdock-re:task:{nvcf_reqid}", 28800, task_meta)
            except Exception as redis_err:
                logger.warning(f"DiffDock-RE Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            logger.error(f"DiffDock-RE API error: {response.status_code} - {response.text[:500]}")
            if response.status_code == 429:
                return _cloudflare_error(503)
            elif response.status_code >= 500:
                return _cloudflare_error(502)
            elif response.status_code == 401 or response.status_code == 403:
                return _cloudflare_error(521)
            else:
                return _cloudflare_error(520)

    except requests.exceptions.Timeout:
        logger.error("DiffDock-RE API timeout")
        return _cloudflare_error(524)
    except requests.exceptions.ConnectionError:
        logger.error("DiffDock-RE connection error")
        return _cloudflare_error(522)
    except requests.exceptions.RequestException as e:
        logger.error(f"DiffDock-RE network error: {e}")
        return _cloudflare_error(502)
    except Exception as e:
        logger.error(f"DiffDock-RE prediction error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/diffdock-re/status/<task_id>', methods=['GET'])
@login_required
def diffdock_re_status(task_id):
    """Check status of an async DiffDock-RE prediction task."""
    try:
        # Validate task_id format (basic safety check)
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        # Look up task metadata in Redis
        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"diffdock-re:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"DiffDock-RE Redis lookup failed: {redis_err}")

        if not task_meta:
            return jsonify({'error': True, 'message': 'Task not found or expired'}), 404

        # Verify this task belongs to the current user
        if task_meta.get('user_id') != session['user_id']:
            return jsonify({'error': True, 'message': 'Access denied'}), 403

        # Poll NVIDIA status once
        if not NVIDIA_API_KEY:
            logger.error("DiffDock-RE status request rejected: NVIDIA_API_KEY is not configured")
            return jsonify({'error': True, 'message': 'Model backend is not configured.'}), 503

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
            logger.error(f"DiffDock-RE status check error: {e}")
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
                r.delete(f"diffdock-re:task:{task_id}")
            except Exception:
                pass

            num_poses_returned = len(result.get('position_confidence', result.get('pose_confidence', [])))
            logger.info(
                f"DiffDock-RE async task completed: {task_id}, "
                f"user={session['user_id']}, poses={num_poses_returned}"
            )
            return jsonify(result)

        elif status_response.status_code == 404:
            # Task expired or not found
            try:
                r = get_redis_connection()
                r.delete(f"diffdock-re:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"DiffDock-RE status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"DiffDock-RE status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/diffdock-re/fetch-pdb', methods=['POST'])
@login_required
@csrf_protect
def diffdock_re_fetch_pdb():
    """Fetch a protein structure from PDB by ID, supporting PDB and CIF formats."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        pdb_id = data.get('pdb_id', '').strip().upper()
        if not pdb_id:
            return jsonify({'error': True, 'message': 'No PDB ID provided'}), 400

        # Validate PDB ID format (1-4 alphanumeric characters)
        if not pdb_id or len(pdb_id) > 4 or not pdb_id.isalnum():
            return jsonify({'error': True, 'message': 'Invalid PDB ID format'}), 400

        format = data.get('format', 'pdb').lower()
        if format not in ('pdb', 'cif', 'mmcif'):
            format = 'pdb'

        try:
            from phonos_app.helpers.databases import import_from_pdb
            result = import_from_pdb(pdb_id, format=format)
            structure_format = result.get('format', format)
            # Return structure data with format info
            return jsonify({
                'pdb_id': pdb_id,
                'structure_data': result['structure_data'],
                'format': structure_format,
                'entry_data': result.get('entry_data', {}),
                'source': 'pdb'
            })
        except Exception as import_err:
            logger.error(f"PDB fetch failed for {pdb_id}: {import_err}")
            return jsonify({
                'error': True,
                'message': f'Failed to fetch PDB structure {pdb_id}: {str(import_err)}'
            }), 502

    except Exception as e:
        logger.error(f"DiffDock-RE fetch-pdb error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/diffdock-re/info', methods=['GET'])
@login_required
def diffdock_re_info():
    """Get DiffDock-RE model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'model': 'DiffDock-RE',
            'provider': 'MIT',
            'description': 'Diffusion-based molecular docking for predicting protein-ligand binding poses',
            'credit_cost': DIFFDOCK_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'parameters': {
                'num_poses': {'min': 1, 'max': 20, 'default': 10},
                'time_divisions': {'min': 1, 'max': 100, 'default': 20},
                'steps': {'min': 1, 'max': 100, 'default': 18},
                'save_trajectory': {'type': 'boolean', 'default': False},
                'is_staged': {'type': 'boolean', 'default': False}
            },
            'input_formats': {
                'protein': ['pdb', 'cif', 'mmcif'],
                'ligand': ['sdf', 'mol2', 'smiles']
            },
            'capabilities': [
                'Protein-ligand docking prediction',
                'Multi-pose generation',
                'Pose confidence scoring',
                'Diffusion-based trajectory sampling',
                'Staged diffusion support'
            ],
            'output_fields': [
                {'field': 'docked_ligand', 'description': 'Docked ligand structure in SDF format'},
                {'field': 'visualization_files', 'description': 'Protein visualization in PDB format'},
                {'field': 'pose_confidence', 'description': 'Confidence scores for each predicted pose'}
            ]
        })
    except Exception as e:
        logger.error(f"DiffDock-RE info error: {e}")
        return _cloudflare_error(520)
