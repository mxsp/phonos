"API routes for Boltz2 protein structure prediction."
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


# Boltz2 API configuration
NVIDIA_BOLTZ2_URL = os.getenv(
    "BOLTZ2_API_URL",
    "https://health.api.nvidia.com/v1/biology/mit/boltz2/predict"
)
NVIDIA_STATUS_URL = os.getenv(
    "BOLTZ2_STATUS_URL",
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
BOLTZ2_CREDIT_COST = 20

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


def poll_nvcf_status(task_id: str, headers: dict) -> dict:
    """
    Poll NVCF status endpoint until task completes.

    Args:
        task_id: NVCF request ID
        headers: Authorization headers

    Returns:
        Dictionary with results
    """
    status_url = NVIDIA_STATUS_URL.format(task_id=task_id)
    start_time = time.time()

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)

            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return response.json()
                else:
                    return {"raw_content": response.text[:5000]}

            elif response.status_code == 202:
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            elif response.status_code == 404:
                raise Exception(f"Task {task_id} not found")

            else:
                raise Exception(f"Status check error: {response.status_code}")

        except requests.exceptions.Timeout:
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Task {task_id} timed out after {NVCF_POLL_SECONDS} seconds")


# ============================================================================
# PAGE ROUTE
# ============================================================================

@app.route('/boltz2')
@login_required
def boltz2_page():
    """Render the Boltz2 structure prediction page."""
    db = get_db()
    user_id = session.get('user_id')

    # Get user credits
    user = db.execute(
        'SELECT credits FROM users WHERE id = %s', (user_id,)
    ).fetchone()
    user_credits = user['credits'] if user else 0

    return render_template('boltz2.html', user_credits=user_credits)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/boltz2/predict', methods=['POST'])
@login_required
@csrf_protect
def boltz2_predict():
    """
    Predict protein structure using Boltz2 model.

    Request body (monomer mode - single sequence):
        sequence: Amino acid, DNA, or RNA sequence
        molecule_type: Type of molecule (protein, dna, rna - default: protein)

    Request body (multimer mode - multiple chains):
        chains: Array of chain objects, each with:
            sequence: Chain sequence
            molecule_type: protein, dna, or rna
            chain_id: Chain identifier (A, B, C, ...)

    Common parameters:
        ligand_smiles: Optional ligand SMILES string
        ligand_ccd: Optional ligand CCD code (e.g., ATP, GTP)
        recycling_steps: Recycling iterations (1-6, default 3)
        sampling_steps: Diffusion sampling steps (10-1000, default 50)
        diffusion_samples: Number of structure samples (1-5, default 1)
        step_scale: Scaling factor for diffusion steps (0.5-5.0, default 1.2)
        without_potentials: Exclude potential calculations (boolean, default: false)
        write_full_pae: Include full PAE matrix in output files (boolean, default: false)
        output_format: Output format - currently only mmcif supported (default: mmcif)
        concatenate_msas: Merge multiple sequence alignments (boolean, default: false)
        msa: Multiple sequence alignment data (optional, monomer only)
        modifications: Comma-separated list of modifications (optional)
        constraints: Array of structural constraints - pockets or bonds (optional)
        predict_affinity: Enable affinity prediction for ligands (boolean, default: false)
        sampling_steps_affinity: Affinity sampling iterations (10-1000, default 200)
        diffusion_samples_affinity: Affinity prediction attempts (1-10, default 5)
        affinity_mw_correction: Apply molecular weight adjustment (boolean, default: false)

    Returns:
        JSON with predicted structure and confidence metrics
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        # Helper to validate a single sequence
        def _validate_sequence(seq, mol_type, chain_label=''):
            prefix = f'Chain {chain_label}: ' if chain_label else ''
            seq_clean = seq.strip().upper().replace(' ', '').replace('\n', '').replace('\r', '')
            if not seq_clean:
                return None, f'{prefix}Sequence must not be empty'
            if mol_type == 'protein':
                valid_chars = set('ACDEFGHIKLMNPQRSTVWY')
                if not all(c in valid_chars for c in seq_clean):
                    return None, f'{prefix}Invalid protein sequence: must contain only standard amino acids (ACDEFGHIKLMNPQRSTVWY)'
            elif mol_type == 'dna':
                valid_chars = set('ACGT')
                if not all(c in valid_chars for c in seq_clean):
                    return None, f'{prefix}Invalid DNA sequence: must contain only A, C, G, T'
            elif mol_type == 'rna':
                valid_chars = set('ACGU')
                if not all(c in valid_chars for c in seq_clean):
                    return None, f'{prefix}Invalid RNA sequence: must contain only A, C, G, U'
            if len(seq_clean) < 1 or len(seq_clean) > 4096:
                return None, f'{prefix}Sequence length must be between 1 and 4096 residues (got {len(seq_clean)})'
            return seq_clean, None

        # Determine monomer vs multimer mode
        chains = data.get('chains')
        is_multimer = isinstance(chains, list) and len(chains) > 0

        if is_multimer:
            # --- Multimer mode: multiple chains ---
            if len(chains) > 12:
                return jsonify({
                    'error': True,
                    'message': 'Maximum of 12 polymer chains allowed'
                }), 400

            polymers = []
            for i, chain in enumerate(chains):
                chain_seq = chain.get('sequence', '')
                chain_struct = chain.get('structure', '')
                chain_type = chain.get('molecule_type', 'protein').lower()
                chain_id = chain.get('chain_id', chr(65 + i))  # A, B, C, ...

                if chain_type not in ['protein', 'dna', 'rna']:
                    return jsonify({
                        'error': True,
                        'message': f'Chain {chain_id}: Invalid molecule_type. Must be protein, dna, or rna'
                    }), 400

                polymer = {"id": chain_id, "molecule_type": chain_type}

                if chain_struct:
                    # Structural input (PDB/CIF content)
                    polymer["structure"] = chain_struct
                else:
                    # Sequence input
                    seq_clean, err = _validate_sequence(chain_seq, chain_type, chain_id)
                    if err:
                        return jsonify({'error': True, 'message': err}), 400
                    polymer["sequence"] = seq_clean

                # Per-chain MSA if provided
                chain_msa = chain.get('msa', '').strip()
                if chain_msa:
                    polymer["msa"] = chain_msa

                polymers.append(polymer)

            # Use first chain's type for logging
            molecule_type = chains[0].get('molecule_type', 'protein').lower()
            total_size = sum(len(p.get('sequence', '')) for p in polymers)
            logger.info(f"Boltz2 multimer request: user={session['user_id']}, chains={len(polymers)}, total_size={total_size}")

        else:
            # --- Monomer mode: single sequence (existing logic) ---
            sequence = data.get('sequence', '').strip().upper()

            if not sequence:
                return jsonify({'error': True, 'message': 'No sequence provided'}), 400

            molecule_type = data.get('molecule_type', 'protein').lower()
            if molecule_type not in ['protein', 'dna', 'rna']:
                return jsonify({
                    'error': True,
                    'message': 'Invalid molecule_type. Must be protein, dna, or rna'
                }), 400

            sequence_clean, err = _validate_sequence(sequence, molecule_type)
            if err:
                return jsonify({'error': True, 'message': err}), 400

            polymer = {
                "id": data.get('chain_id', 'A'),
                "molecule_type": molecule_type,
                "sequence": sequence_clean
            }

            # Add cyclic flag if specified (only for proteins)
            if data.get('cyclic', False) and molecule_type == 'protein':
                polymer["cyclic"] = True

            # Add MSA if provided
            msa = data.get('msa', '').strip()
            if msa:
                polymer["msa"] = msa

            # Add modifications if provided (comma-separated list)
            modifications = data.get('modifications', '').strip()
            if modifications:
                polymer["modifications"] = [m.strip() for m in modifications.split(',') if m.strip()]

            polymers = [polymer]

        # Get user and check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < BOLTZ2_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {BOLTZ2_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Build ligands list
        ligands = []
        ligand_smiles = data.get('ligand_smiles', '').strip()
        ligand_ccd = data.get('ligand_ccd', '').strip().upper()
        ligand_count = min(max(1, int(data.get('ligand_count', 1))), 10)

        if ligand_smiles:
            # Add ligand(s) - support multiple copies
            for i in range(ligand_count):
                ligand_id = data.get('ligand_id', 'L')
                if i > 0:
                    ligand_id = f"{ligand_id}{i+1}"
                ligands.append({
                    "id": ligand_id,
                    "smiles": ligand_smiles
                })
        elif ligand_ccd:
            # Add ligand(s) - support multiple copies
            for i in range(ligand_count):
                ligand_id = data.get('ligand_id', 'L')
                if i > 0:
                    ligand_id = f"{ligand_id}{i+1}"
                ligands.append({
                    "id": ligand_id,
                    "ccd": ligand_ccd
                })

        # Extract parameters with correct ranges
        recycling_steps = min(max(1, int(data.get('recycling_steps', 1))), 6)
        sampling_steps = min(max(10, int(data.get('sampling_steps', 50))), 1000)
        diffusion_samples = min(max(1, int(data.get('diffusion_samples', 3))), 5)
        step_scale = min(max(0.5, float(data.get('step_scale', 1.2))), 5.0)
        without_potentials = data.get('without_potentials', False)
        write_full_pae = data.get('write_full_pae', False)
        output_format = data.get('output_format', 'mmcif')  # Currently only mmcif supported
        concatenate_msas = data.get('concatenate_msas', False)

        # Affinity prediction parameters
        predict_affinity = data.get('predict_affinity', False)
        sampling_steps_affinity = min(max(10, int(data.get('sampling_steps_affinity', 200))), 1000)
        diffusion_samples_affinity = min(max(1, int(data.get('diffusion_samples_affinity', 5))), 10)
        affinity_mw_correction = data.get('affinity_mw_correction', False)

        # Prepare API request
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": "30",
        }

        payload = {
            "polymers": polymers,
            "recycling_steps": recycling_steps,
            "sampling_steps": sampling_steps,
            "diffusion_samples": diffusion_samples,
            "step_scale": step_scale,
            "without_potentials": without_potentials,
            "write_full_pae": write_full_pae,
            "output_format": output_format,
            "concatenate_msas": concatenate_msas
        }

        # Add constraints if provided
        constraints = data.get('constraints', [])
        if constraints:
            payload["constraints"] = constraints

        # Add ligands only if present
        if ligands:
            payload["ligands"] = ligands

            # Add affinity prediction parameters if requested
            if predict_affinity and len(ligands) >= 1:
                # Set predict_affinity flag on the first ligand
                ligands[0]["predict_affinity"] = True
                payload["sampling_steps_affinity"] = sampling_steps_affinity
                payload["diffusion_samples_affinity"] = diffusion_samples_affinity
                payload["affinity_mw_correction"] = affinity_mw_correction

        total_len = sum(len(p['sequence']) for p in polymers)
        logger.info(f"Boltz2 prediction request: user={session['user_id']}, type={molecule_type}, chains={len(polymers)}, total_residues={total_len}, ligand={'yes' if ligands else 'no'}")

        # Call Boltz2 API (60s connect, 90s read)
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=(60, 90)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                logger.error(f"Boltz2 unexpected content type: {content_type}")
                return _cloudflare_error(520)

            # Immediate result - deduct credits and return
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (BOLTZ2_CREDIT_COST, session['user_id'])
            )
            db.commit()
            logger.info(f"Boltz2 prediction completed (sync): user={session['user_id']}, confidence={result.get('confidence_scores', [None])[0]}")
            return jsonify(result)

        elif response.status_code == 202:
            # Async task - return task_id immediately so frontend can poll
            nvcf_reqid = response.headers.get("NVCF-REQID")
            if not nvcf_reqid:
                logger.error("Boltz2 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(f"Boltz2 async task started: {nvcf_reqid}, user={session['user_id']}")

            # Store task metadata in Redis (TTL: 10 minutes)
            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': BOLTZ2_CREDIT_COST,
                    'started_at': time.time()
                })
                r.setex(f"boltz2:task:{nvcf_reqid}", 28800, task_meta)
            except Exception as redis_err:
                logger.warning(f"Boltz2 Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            logger.error(f"Boltz2 API error: {response.status_code} - {response.text[:500]}")
            if response.status_code == 429:
                return _cloudflare_error(503)
            elif response.status_code >= 500:
                return _cloudflare_error(502)
            elif response.status_code == 401 or response.status_code == 403:
                return _cloudflare_error(521)
            else:
                return _cloudflare_error(520)

    except requests.exceptions.Timeout:
        logger.error("Boltz2 API timeout")
        return _cloudflare_error(524)
    except requests.exceptions.ConnectionError:
        logger.error("Boltz2 connection error")
        return _cloudflare_error(522)
    except requests.exceptions.RequestException as e:
        logger.error(f"Boltz2 network error: {e}")
        return _cloudflare_error(502)
    except Exception as e:
        logger.error(f"Boltz2 prediction error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/boltz2/status/<task_id>', methods=['GET'])
@login_required
def boltz2_status(task_id):
    """Check status of an async Boltz2 prediction task."""
    try:
        # Validate task_id format (basic safety check)
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        # Look up task metadata in Redis
        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"boltz2:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"Boltz2 Redis lookup failed: {redis_err}")

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
            logger.error(f"Boltz2 status check error: {e}")
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
                r.delete(f"boltz2:task:{task_id}")
            except Exception:
                pass

            logger.info(f"Boltz2 async task completed: {task_id}, user={session['user_id']}, confidence={result.get('confidence_scores', [None])[0]}")
            return jsonify(result)

        elif status_response.status_code == 404:
            # Task expired or not found
            try:
                r = get_redis_connection()
                r.delete(f"boltz2:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"Boltz2 status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"Boltz2 status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/boltz2/info', methods=['GET'])
@login_required
def boltz2_info():
    """Get Boltz2 model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'model': 'Boltz2',
            'provider': 'MIT',
            'description': 'Biomolecular structure prediction with protein-ligand binding',
            'max_sequence_length': 4096,
            'credit_cost': BOLTZ2_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'parameters': {
                'recycling_steps': {'min': 1, 'max': 6, 'default': 1},
                'sampling_steps': {'min': 10, 'max': 1000, 'default': 50},
                'diffusion_samples': {'min': 1, 'max': 5, 'default': 3},
                'step_scale': {'min': 0.5, 'max': 5.0, 'default': 1.2},
                'write_full_pae': {'type': 'boolean', 'default': False}
            },
            'molecule_types': {
                'supported': ['protein', 'dna', 'rna'],
                'default': 'protein'
            },
            'capabilities': [
                'Protein structure prediction',
                'DNA/RNA structure prediction',
                'Protein-ligand complex prediction',
                'Multi-chain complex prediction',
                'Confidence scoring (pLDDT, PTM, iPTM)',
                'Binding affinity prediction'
            ],
            'supported_ligand_inputs': [
                {'type': 'smiles', 'description': 'SMILES string for any small molecule'},
                {'type': 'ccd', 'description': 'CCD code for known ligands (e.g., ATP, GTP, NAD)'}
            ]
        })
    except Exception as e:
        logger.error(f"Boltz2 info error: {e}")
        return _cloudflare_error(520)


@app.route('/api/boltz2/common-ligands', methods=['GET'])
@login_required
def boltz2_common_ligands():
    """Get list of common ligands with CCD codes."""
    return jsonify({
        'ligands': [
            {'ccd': 'ATP', 'name': 'Adenosine Triphosphate', 'category': 'Nucleotides'},
            {'ccd': 'GTP', 'name': 'Guanosine Triphosphate', 'category': 'Nucleotides'},
            {'ccd': 'ADP', 'name': 'Adenosine Diphosphate', 'category': 'Nucleotides'},
            {'ccd': 'GDP', 'name': 'Guanosine Diphosphate', 'category': 'Nucleotides'},
            {'ccd': 'NAD', 'name': 'Nicotinamide Adenine Dinucleotide', 'category': 'Cofactors'},
            {'ccd': 'FAD', 'name': 'Flavin Adenine Dinucleotide', 'category': 'Cofactors'},
            {'ccd': 'HEM', 'name': 'Heme', 'category': 'Cofactors'},
            {'ccd': 'ZN', 'name': 'Zinc Ion', 'category': 'Metal Ions'},
            {'ccd': 'MG', 'name': 'Magnesium Ion', 'category': 'Metal Ions'},
            {'ccd': 'CA', 'name': 'Calcium Ion', 'category': 'Metal Ions'},
            {'ccd': 'FE', 'name': 'Iron Ion', 'category': 'Metal Ions'},
            {'ccd': 'MN', 'name': 'Manganese Ion', 'category': 'Metal Ions'},
            {'ccd': 'SAM', 'name': 'S-Adenosylmethionine', 'category': 'Cofactors'},
            {'ccd': 'COA', 'name': 'Coenzyme A', 'category': 'Cofactors'},
            {'ccd': 'PLP', 'name': 'Pyridoxal Phosphate', 'category': 'Cofactors'}
        ]
    })
