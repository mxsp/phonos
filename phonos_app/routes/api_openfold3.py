"""API routes for OpenFold3 biomolecular structure prediction."""
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


# OpenFold3 API configuration
NVIDIA_OPENFOLD3_URL = os.getenv(
    "OPENFOLD3_API_URL",
    "https://health.api.nvidia.com/v1/biology/openfold/openfold3/predict"
)
NVIDIA_STATUS_URL = os.getenv(
    "OPENFOLD3_STATUS_URL",
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
OPENFOLD3_CREDIT_COST = 20

# NVCF initial long-poll: short so we get a 202 quickly and poll async
NVCF_INITIAL_POLL = 30
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

@app.route('/openfold3')
@login_required
def openfold3_page():
    """Render the OpenFold3 structure prediction page."""
    db = get_db()
    user_id = session.get('user_id')

    user = db.execute(
        'SELECT credits FROM users WHERE id = %s', (user_id,)
    ).fetchone()
    user_credits = user['credits'] if user else 0

    return render_template('openfold3.html', user_credits=user_credits)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/openfold3/predict', methods=['POST'])
@login_required
@csrf_protect
def openfold3_predict():
    """
    Predict biomolecular structure using OpenFold3 model.

    Request body (monomer mode - single sequence):
        sequence: Amino acid, DNA, or RNA sequence
        molecule_type: Type of molecule (protein, dna, rna - default: protein)

    Request body (multimer mode - multiple chains):
        chains: Array of chain objects, each with:
            sequence: Chain sequence
            molecule_type: protein, dna, or rna
            chain_id: Chain identifier (A, B, C, ...)
            msa_input: Optional MSA data for this chain

    Common parameters:
        ligand_smiles: Optional ligand SMILES string
        ligand_ccd: Optional ligand CCD code
        output_format: Output format (pdb or mmcif, default: pdb)
        msa_input: Optional MSA data (monomer only)
        msa_format: MSA format - 'a3m' or 'csv' (default: a3m)

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
                valid_chars = set('ACGTN')
                if not all(c in valid_chars for c in seq_clean):
                    return None, f'{prefix}Invalid DNA sequence: must contain only A, C, G, T, N'
            elif mol_type == 'rna':
                valid_chars = set('ACGU')
                if not all(c in valid_chars for c in seq_clean):
                    return None, f'{prefix}Invalid RNA sequence: must contain only A, C, G, U'
            if len(seq_clean) < 1 or len(seq_clean) > 1000:
                return None, f'{prefix}Sequence length must be between 1 and 1000 residues (got {len(seq_clean)})'
            return seq_clean, None

        # Helper to build MSA for a protein molecule
        def _build_msa(chain_id, seq_clean, msa_input_str='', msa_fmt='a3m'):
            if msa_input_str:
                return {
                    "main": {
                        msa_fmt: {
                            "alignment": msa_input_str,
                            "format": msa_fmt
                        }
                    }
                }
            # Default self-MSA
            self_msa = f">{chain_id}\n{seq_clean}\n"
            return {
                "main": {
                    "a3m": {
                        "alignment": self_msa,
                        "format": "a3m"
                    }
                }
            }

        # Determine monomer vs multimer mode
        chains = data.get('chains')
        is_multimer = isinstance(chains, list) and len(chains) > 0

        if is_multimer:
            # --- Multimer mode: multiple chains ---
            molecules = []
            for i, chain in enumerate(chains):
                chain_seq = chain.get('sequence', '')
                chain_type = chain.get('molecule_type', 'protein').lower()
                chain_id = chain.get('chain_id', chr(65 + i))

                if chain_type not in ['protein', 'dna', 'rna']:
                    return jsonify({
                        'error': True,
                        'message': f'Chain {chain_id}: Invalid molecule_type. Must be protein, dna, or rna'
                    }), 400

                seq_clean, err = _validate_sequence(chain_seq, chain_type, chain_id)
                if err:
                    return jsonify({'error': True, 'message': err}), 400

                molecule = {
                    "type": chain_type,
                    "id": chain_id,
                    "sequence": seq_clean
                }

                # OpenFold3 requires MSA for every protein
                if chain_type == 'protein':
                    chain_msa = chain.get('msa_input', '').strip()
                    molecule["msa"] = _build_msa(chain_id, seq_clean, chain_msa)

                molecules.append(molecule)

            molecule_type = chains[0].get('molecule_type', 'protein').lower()
            total_residues = sum(len(m['sequence']) for m in molecules)
            logger.info(f"OpenFold3 multimer request: user={session['user_id']}, chains={len(molecules)}, total_residues={total_residues}")

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

            molecule = {
                "type": molecule_type,
                "id": data.get('chain_id', 'A'),
                "sequence": sequence_clean
            }

            # Add MSA (protein only)
            if molecule_type == 'protein':
                msa_input = data.get('msa_input', '').strip()
                msa_format = data.get('msa_format', 'a3m').lower()
                molecule["msa"] = _build_msa(
                    data.get('chain_id', 'A'), sequence_clean, msa_input, msa_format
                )

            molecules = [molecule]

        # Get user and check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < OPENFOLD3_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {OPENFOLD3_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Add ligand molecule if specified (NIM format: smiles or ccd_code)
        ligand_smiles = data.get('ligand_smiles', '').strip()
        ligand_ccd = data.get('ligand_ccd', '').strip().upper()

        if ligand_smiles:
            molecules.append({
                "type": "ligand",
                "id": data.get('ligand_id', 'L'),
                "smiles": ligand_smiles
            })
        elif ligand_ccd:
            molecules.append({
                "type": "ligand",
                "id": data.get('ligand_id', 'L'),
                "ccd_codes": [ligand_ccd]
            })

        # Build output format (frontend sends 'mmcif', API expects 'cif')
        output_format = data.get('output_format', 'pdb')
        if output_format == 'mmcif':
            output_format = 'cif'
        if output_format not in ['pdb', 'cif']:
            output_format = 'pdb'

        # Get diffusion samples (1-5, default: 1)
        diffusion_samples = data.get('diffusion_samples', 1)
        try:
            diffusion_samples = int(diffusion_samples)
            diffusion_samples = max(1, min(5, diffusion_samples))
        except (ValueError, TypeError):
            diffusion_samples = 1

        # Build the OpenFold3 NIM API request payload
        request_id = f"phonos-{session['user_id']}-{int(time.time())}"
        payload = {
            "request_id": request_id,
            "inputs": [
                {
                    "input_id": "input-1",
                    "molecules": molecules,
                    "diffusion_samples": diffusion_samples,
                    "output_format": output_format
                }
            ]
        }

        # Prepare API request headers
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": str(NVCF_INITIAL_POLL)
        }

        total_len = sum(len(m['sequence']) for m in molecules if 'sequence' in m)
        logger.info(f"OpenFold3 prediction request: user={session['user_id']}, type={molecule_type}, chains={len(molecules)}, total_residues={total_len}, ligand={'yes' if (ligand_smiles or ligand_ccd) else 'no'}")

        # Call OpenFold3 API (60s connect, 90s read — gives buffer beyond NVCF-POLL-SECONDS)
        response = requests.post(
            url=NVIDIA_OPENFOLD3_URL,
            headers=headers,
            json=payload,
            timeout=(60, 90)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                # Text-based structure output (PDB/mmCIF)
                result = {
                    "structure": response.text,
                    "output_format": output_format,
                    "request_id": request_id
                }

            # Deduct credits
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (OPENFOLD3_CREDIT_COST, session['user_id'])
            )
            db.commit()
            logger.info(f"OpenFold3 prediction completed (sync): user={session['user_id']}")
            return jsonify(result)

        elif response.status_code == 202:
            # Async task - return task_id immediately so frontend can poll
            nvcf_reqid = response.headers.get("NVCF-REQID")
            if not nvcf_reqid:
                logger.error("OpenFold3 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(f"OpenFold3 async task started: {nvcf_reqid}, user={session['user_id']}")

            # Store task metadata in Redis (TTL: 10 minutes)
            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': OPENFOLD3_CREDIT_COST,
                    'started_at': time.time(),
                    'output_format': output_format
                })
                r.setex(f"openfold3:task:{nvcf_reqid}", 28800, task_meta)
            except Exception as redis_err:
                logger.warning(f"OpenFold3 Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            logger.error(f"OpenFold3 API error: {response.status_code} - {response.text[:500]}")
            if response.status_code == 429:
                return _cloudflare_error(503)
            elif response.status_code >= 500:
                return _cloudflare_error(502)
            elif response.status_code in (401, 403):
                return _cloudflare_error(521)
            else:
                return _cloudflare_error(520)

    except requests.exceptions.Timeout:
        logger.error("OpenFold3 API timeout")
        return _cloudflare_error(524)
    except requests.exceptions.ConnectionError:
        logger.error("OpenFold3 connection error")
        return _cloudflare_error(522)
    except requests.exceptions.RequestException as e:
        logger.error(f"OpenFold3 network error: {e}")
        return _cloudflare_error(502)
    except Exception as e:
        logger.error(f"OpenFold3 prediction error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/openfold3/status/<task_id>', methods=['GET'])
@login_required
def openfold3_status(task_id):
    """Check status of an async OpenFold3 prediction task."""
    try:
        # Validate task_id format
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        # Look up task metadata in Redis
        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"openfold3:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"OpenFold3 Redis lookup failed: {redis_err}")

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
            logger.error(f"OpenFold3 status check error: {e}")
            return jsonify({'status': 'pending', 'message': 'Status check failed, still processing'})

        if status_response.status_code == 202:
            # Still processing
            elapsed = int(time.time() - task_meta.get('started_at', time.time()))
            return jsonify({'status': 'pending', 'elapsed': elapsed})

        elif status_response.status_code == 200:
            # Completed
            content_type = status_response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = status_response.json()
            else:
                result = {
                    "structure": status_response.text[:50000],
                    "output_format": task_meta.get('output_format', 'pdb')
                }

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
                r.delete(f"openfold3:task:{task_id}")
            except Exception:
                pass

            logger.info(f"OpenFold3 async task completed: {task_id}, user={session['user_id']}")
            return jsonify(result)

        elif status_response.status_code == 404:
            try:
                r = get_redis_connection()
                r.delete(f"openfold3:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"OpenFold3 status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"OpenFold3 status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/openfold3/info', methods=['GET'])
@login_required
def openfold3_info():
    """Get OpenFold3 model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'model': 'OpenFold3',
            'provider': 'OpenFold Consortium',
            'description': 'Next-generation biomolecular structure prediction supporting proteins, DNA, RNA, and ligands with high accuracy.',
            'max_sequence_length': None,
            'credit_cost': OPENFOLD3_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'parameters': {
                'output_format': {'options': ['pdb', 'mmcif'], 'default': 'pdb'}
            },
            'molecule_types': {
                'supported': ['protein', 'dna', 'rna'],
                'default': 'protein'
            },
            'capabilities': [
                'Protein structure prediction',
                'DNA/RNA structure prediction',
                'Protein-ligand complex prediction',
                'Multi-molecule complex prediction',
                'PDB and mmCIF output formats',
                'MSA-enhanced predictions'
            ],
            'supported_ligand_inputs': [
                {'type': 'smiles', 'description': 'SMILES string for any small molecule'},
                {'type': 'ccd', 'description': 'CCD code for known ligands (e.g., ATP, GTP, NAD)'}
            ]
        })
    except Exception as e:
        logger.error(f"OpenFold3 info error: {e}")
        return _cloudflare_error(520)
