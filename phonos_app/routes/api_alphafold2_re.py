"""API routes for AlphaFold2-RE protein structure prediction (monomer and multimer)."""
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


# AlphaFold2-RE API configuration (monomer)
NVIDIA_ALPHAFOLD2_RE_URL = os.getenv(
    "ALPHAFOLD2_RE_API_URL",
    "https://health.api.nvidia.com/v1/biology/deepmind/alphafold2"
)

# AlphaFold2-RE Multimer API configuration
NVIDIA_ALPHAFOLD2_RE_MULTIMER_URL = os.getenv(
    "ALPHAFOLD2_RE_MULTIMER_API_URL",
    "https://health.api.nvidia.com/v1/biology/deepmind/alphafold2-multimer"
)

# NVCF status polling endpoint
NVIDIA_STATUS_URL = os.getenv(
    "ALPHAFOLD2_RE_STATUS_URL",
    "https://health.api.nvidia.com/v1/status/{task_id}"
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
ALPHAFOLD2_RE_CREDIT_COST = 20

# NVCF polling settings
# Use a longer poll window for initial POST so NVIDIA has time to either
# return results synchronously or create a task. The frontend then polls
# /status/<task_id> until the result is ready.
# Large sequences (1000+ residues) need more time for MSA generation before
# a task ID is returned; 300s prevents premature 504 timeouts.
NVCF_POLL_SECONDS = 30       # header sent on initial POST (short poll, get 202 quickly)
NVCF_POLL_INTERVAL = 5

# How long (seconds) to keep the task metadata alive in Redis.
# AlphaFold2 can take 30–90+ minutes, so use a 3-hour window.
ALPHAFOLD2_TASK_TTL = 28800  # 8 hours

# Valid amino acids
VALID_AMINO_ACIDS = set('ACDEFGHIKLMNPQRSTVWY')

# Valid MSA databases
VALID_DATABASES = {'uniref90', 'small_bfd', 'mgnify'}

# Valid MSA algorithms
VALID_ALGORITHMS = {'jackhmmer', 'mmseqs2'}

# Valid template searchers
VALID_TEMPLATE_SEARCHERS = {'hhsearch', 'hmmsearch'}

LEGACY_NVCF_STATUS_URL = "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"


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


def _validate_protein_sequence(sequence: str) -> str:
    """
    Validate and clean a protein sequence.

    Returns the cleaned sequence on success, or None if invalid.
    Sets error details via the returned tuple pattern in callers.
    """
    cleaned = sequence.strip().upper().replace(' ', '').replace('\n', '').replace('\r', '')
    if not cleaned:
        return None
    if not all(c in VALID_AMINO_ACIDS for c in cleaned):
        return None
    return cleaned


def _validate_msa_params(data: dict) -> dict:
    """Extract and validate MSA parameters from request data."""
    algorithm = data.get('algorithm', 'jackhmmer').lower()
    if algorithm not in VALID_ALGORITHMS:
        algorithm = 'jackhmmer'

    e_value = data.get('e_value', 0.0001)
    try:
        e_value = max(0.0, min(float(e_value), 10.0))
    except (TypeError, ValueError):
        e_value = 0.0001

    iterations = data.get('iterations', 1)
    try:
        iterations = max(1, int(iterations))
    except (TypeError, ValueError):
        iterations = 1

    databases = data.get('databases', ['uniref90', 'small_bfd', 'mgnify'])
    if isinstance(databases, list):
        databases = [db for db in databases if db in VALID_DATABASES]
    if not databases:
        databases = ['uniref90', 'small_bfd', 'mgnify']

    relax_prediction = bool(data.get('relax_prediction', True))

    return {
        'algorithm': algorithm,
        'e_value': e_value,
        'iterations': iterations,
        'databases': databases,
        'relax_prediction': relax_prediction,
    }


def _extract_task_id(response: requests.Response) -> str:
    """Extract async request ID from provider response headers."""
    for header in ("NVCF-REQID", "Nvcf-Reqid", "nvcf-reqid"):
        task_id = response.headers.get(header)
        if task_id:
            return task_id.strip()
    return None


def _is_async_response(response: requests.Response) -> bool:
    """
    Determine if a response indicates an async task that can be polled.

    NVIDIA health endpoints can return 202, and sometimes 504 when poll window
    expires while task is still processing. A 504 with a task ID means the
    task was created but the poll window expired - we should poll for results.
    """
    task_id = _extract_task_id(response)
    if not task_id:
        return False
    if response.status_code == 202:
        return True
    if response.status_code == 504:
        status_header = (
            response.headers.get("NVCF-STATUS")
            or response.headers.get("Nvcf-Status")
            or response.headers.get("nvcf-status")
            or ""
        ).strip().lower()
        return status_header not in {"errored", "error", "failed"}
    return False


def _status_url_candidates(task_id: str) -> list[str]:
    """Return status endpoints in priority order (current first, legacy fallback)."""
    primary = NVIDIA_STATUS_URL.format(task_id=task_id)
    legacy = LEGACY_NVCF_STATUS_URL.format(task_id=task_id)
    if primary == legacy:
        return [primary]
    return [primary, legacy]


def _poll_status_once(task_id: str, headers: dict[str, str]) -> requests.Response:
    """Poll async status endpoint once, with fallback to legacy NVCF URL."""
    last_response = None
    last_error = None

    for status_url in _status_url_candidates(task_id):
        try:
            status_response = requests.get(status_url, headers=headers, timeout=30)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            continue

        last_response = status_response
        # Try next endpoint when task ID format is valid but not found on this backend.
        if status_response.status_code == 404:
            continue
        return status_response

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise requests.exceptions.RequestException("No status response returned")


def _handle_nvidia_error(response, model_name: str):
    """Map NVIDIA API HTTP errors to user-facing responses with diagnostics."""
    nvcf_status = (
        response.headers.get("NVCF-STATUS")
        or response.headers.get("Nvcf-Status")
        or ""
    ).strip().lower()
    body_preview = response.text[:300] if response.text else "(empty)"

    logger.error(
        "%s API error: HTTP %s | NVCF-STATUS=%s | body=%s",
        model_name, response.status_code, nvcf_status or "(none)", body_preview,
    )

    if response.status_code == 429:
        return jsonify({
            'error': True,
            'message': (
                f'{model_name}: provider rate limit reached (HTTP 429). '
                'Please wait a few minutes and try again.'
            )
        }), 503
    elif response.status_code == 504:
        if nvcf_status in ("errored", "error", "failed"):
            return jsonify({
                'error': True,
                'message': (
                    f'{model_name}: model execution failed '
                    f'(HTTP {response.status_code}, NVCF-STATUS: {nvcf_status}). '
                    'The model backend is experiencing issues. Please try again later.'
                )
            }), 503
        logger.warning(
            "%s: 504 gateway timeout with no task ID. "
            "The sequence may require a longer processing window.", model_name,
        )
        return jsonify({
            'error': True,
            'message': (
                f'{model_name}: prediction server timed out (HTTP 504). '
                'This can happen with large sequences. Please try again — if the '
                'issue persists, try a shorter sequence or the mmseqs2 algorithm.'
            )
        }), 504
    elif response.status_code >= 500:
        return jsonify({
            'error': True,
            'message': (
                f'{model_name}: provider server error (HTTP {response.status_code}'
                f'{", NVCF: " + nvcf_status if nvcf_status else ""}). '
                'The model backend is temporarily unavailable.'
            )
        }), 502
    elif response.status_code in (401, 403):
        return jsonify({
            'error': True,
            'message': (
                f'{model_name}: authentication error (HTTP {response.status_code}). '
                'API key may be expired or invalid.'
            )
        }), 503
    else:
        return jsonify({
            'error': True,
            'message': (
                f'{model_name}: unexpected provider response (HTTP {response.status_code}'
                f'{", NVCF: " + nvcf_status if nvcf_status else ""}). '
                'Please try again or contact support.'
            )
        }), 502


def _handle_request_exceptions(e, model_name: str):
    """Handle requests library exceptions uniformly."""
    if isinstance(e, requests.exceptions.Timeout):
        logger.error(f"{model_name} API timeout")
        return _cloudflare_error(524)
    elif isinstance(e, requests.exceptions.ConnectionError):
        logger.error(f"{model_name} connection error")
        return _cloudflare_error(522)
    elif isinstance(e, requests.exceptions.RequestException):
        logger.error(f"{model_name} network error: {e}")
        return _cloudflare_error(502)
    else:
        logger.error(f"{model_name} error: {e}", exc_info=True)
        return _cloudflare_error(520)


# ============================================================================
# PAGE ROUTE
# ============================================================================

@app.route('/alphafold2-re')
@login_required
def alphafold2_re_page():
    """Render the AlphaFold2-RE structure prediction page."""
    db = get_db()
    user_id = session.get('user_id')

    user = db.execute(
        'SELECT credits FROM users WHERE id = %s', (user_id,)
    ).fetchone()
    user_credits = user['credits'] if user else 0

    return render_template('alphafold2_re.html', user_credits=user_credits)


# ============================================================================
# MONOMER API ROUTES
# ============================================================================

@app.route('/api/alphafold2-re/predict', methods=['POST'])
@login_required
@csrf_protect
def alphafold2_re_predict():
    """
    Predict protein structure using AlphaFold2-RE (monomer).

    Request body:
        sequence: Protein amino acid sequence (max 4096 chars)
        relax_prediction: Run structural relaxation (boolean, default: true)
        algorithm: MSA algorithm - jackhmmer or mmseqs2 (default: jackhmmer)
        e_value: E-value threshold for MSA (0-10, default: 0.0001)
        databases: Array of MSA databases (default: [uniref90, small_bfd, mgnify])
        iterations: Number of MSA iterations (min 1, default: 1)

    Returns:
        JSON with predicted PDB structure or async task_id
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '')
        sequence_clean = _validate_protein_sequence(sequence)

        if not sequence_clean:
            return jsonify({
                'error': True,
                'message': 'Invalid protein sequence: must contain only standard amino acids (ACDEFGHIKLMNPQRSTVWY)'
            }), 400

        if len(sequence_clean) > 4096:
            return jsonify({
                'error': True,
                'message': f'Sequence too long: max 4096 residues (got {len(sequence_clean)})'
            }), 400

        # Check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < ALPHAFOLD2_RE_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {ALPHAFOLD2_RE_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Extract and validate MSA parameters
        msa_params = _validate_msa_params(data)

        # Build API payload
        if not NVIDIA_API_KEY:
            logger.error("AlphaFold2-RE request rejected: NVIDIA_API_KEY is not configured")
            return jsonify({'error': True, 'message': 'Model backend is not configured.'}), 503

        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": str(NVCF_POLL_SECONDS),
        }

        # structure_models_to_relax parameter
        structure_models_to_relax = data.get('structure_models_to_relax', 'all').lower()
        if structure_models_to_relax not in ('all', 'best', 'none'):
            structure_models_to_relax = 'all'

        payload = {
            "sequence": sequence_clean,
            "relax_prediction": msa_params['relax_prediction'],
            "structure_models_to_relax": structure_models_to_relax,
            "algorithm": msa_params['algorithm'],
            "e_value": msa_params['e_value'],
            "databases": msa_params['databases'],
            "iterations": msa_params['iterations'],
        }

        logger.info(
            f"AlphaFold2-RE prediction request: user={session['user_id']}, "
            f"seq_len={len(sequence_clean)}, algorithm={msa_params['algorithm']}"
        )

        # Call AlphaFold2-RE API
        # connect timeout = 60s; read timeout = NVCF_POLL_SECONDS + 120s buffer
        # For large sequences (>1000 residues), NVIDIA needs time for MSA generation
        # before returning a task ID or synchronous result.
        response = requests.post(
            url=NVIDIA_ALPHAFOLD2_RE_URL,
            headers=headers,
            json=payload,
            timeout=(60, NVCF_POLL_SECONDS + 120)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                # AlphaFold2 may return PDB content directly
                result = {
                    "pdb_content": response.text,
                    "format": "pdb",
                    "sequence_length": len(sequence_clean)
                }

            # Deduct credits on immediate success
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (ALPHAFOLD2_RE_CREDIT_COST, session['user_id'])
            )
            db.commit()

            logger.info(
                f"AlphaFold2-RE prediction completed (sync): user={session['user_id']}"
            )
            return jsonify(result)

        elif _is_async_response(response):
            # Async task - return task_id for polling
            nvcf_reqid = _extract_task_id(response)
            if not nvcf_reqid:
                logger.error("AlphaFold2-RE 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(
                f"AlphaFold2-RE async task started: {nvcf_reqid}, user={session['user_id']}"
            )

            # Store task metadata in Redis (TTL: 15 minutes for long predictions)
            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': ALPHAFOLD2_RE_CREDIT_COST,
                    'model': 'alphafold2-re',
                    'started_at': time.time()
                })
                r.setex(f"alphafold2re:task:{nvcf_reqid}", ALPHAFOLD2_TASK_TTL, task_meta)
            except Exception as redis_err:
                logger.warning(f"AlphaFold2-RE Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            return _handle_nvidia_error(response, "AlphaFold2-RE")

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
            requests.exceptions.RequestException) as e:
        return _handle_request_exceptions(e, "AlphaFold2-RE")
    except Exception as e:
        logger.error(f"AlphaFold2-RE prediction error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/alphafold2-re/status/<task_id>', methods=['GET'])
@login_required
def alphafold2_re_status(task_id):
    """Check status of an async AlphaFold2-RE monomer prediction task."""
    try:
        # Validate task_id format
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        # Look up task metadata in Redis
        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"alphafold2re:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"AlphaFold2-RE Redis lookup failed: {redis_err}")

        if not task_meta:
            return jsonify({'error': True, 'message': 'Task not found or expired'}), 404

        # Verify task ownership
        if task_meta.get('user_id') != session['user_id']:
            return jsonify({'error': True, 'message': 'Access denied'}), 403

        # Poll NVIDIA status once
        if not NVIDIA_API_KEY:
            logger.error("AlphaFold2-RE status request rejected: NVIDIA_API_KEY is not configured")
            return jsonify({'error': True, 'message': 'Model backend is not configured.'}), 503

        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json"
        }
        try:
            status_response = _poll_status_once(task_id, headers)
        except requests.exceptions.Timeout:
            return jsonify({'status': 'pending', 'message': 'Status check timed out, still processing'})
        except requests.exceptions.RequestException as e:
            logger.error(f"AlphaFold2-RE status check error: {e}")
            return jsonify({'status': 'pending', 'message': 'Status check failed, still processing'})

        if status_response.status_code == 202:
            elapsed = int(time.time() - task_meta.get('started_at', time.time()))
            return jsonify({'status': 'pending', 'elapsed': elapsed})

        elif status_response.status_code == 200:
            content_type = status_response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = status_response.json()
            else:
                result = {
                    "pdb_content": status_response.text,
                    "format": "pdb"
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
                r.delete(f"alphafold2re:task:{task_id}")
            except Exception:
                pass

            logger.info(
                f"AlphaFold2-RE async task completed: {task_id}, user={session['user_id']}"
            )
            return jsonify(result)

        elif status_response.status_code == 404:
            try:
                r = get_redis_connection()
                r.delete(f"alphafold2re:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"AlphaFold2-RE status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"AlphaFold2-RE status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


# ============================================================================
# MULTIMER API ROUTES
# ============================================================================

@app.route('/api/alphafold2-re-multimer/predict', methods=['POST'])
@login_required
@csrf_protect
def alphafold2_re_multimer_predict():
    """
    Predict protein complex structure using AlphaFold2-RE Multimer.

    Request body:
        sequences: Array of protein sequences (1-6, max 4096 chars each)
        relax_prediction: Run structural relaxation (boolean, default: true)
        algorithm: MSA algorithm - jackhmmer or mmseqs2 (default: jackhmmer)
        e_value: E-value threshold for MSA (0-10, default: 0.0001)
        databases: Array of MSA databases (default: [uniref90, small_bfd, mgnify])
        iterations: Number of MSA iterations (min 1, default: 1)
        num_predictions_per_model: Predictions per model (1-8, default: 1)
        max_msa_sequences: Maximum MSA sequences (optional)
        template_searcher: Template search method - hhsearch or hmmsearch (default: hhsearch)

    Returns:
        JSON with predicted PDB structure or async task_id
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequences_raw = data.get('sequences', [])
        if not isinstance(sequences_raw, list) or len(sequences_raw) == 0:
            return jsonify({
                'error': True,
                'message': 'Must provide an array of 1-6 protein sequences'
            }), 400

        if len(sequences_raw) > 6:
            return jsonify({
                'error': True,
                'message': f'AlphaFold2-Multimer supports 1-6 sequences (got {len(sequences_raw)})'
            }), 400

        # Validate each sequence
        sequences_clean = []
        for i, seq in enumerate(sequences_raw):
            if not isinstance(seq, str):
                return jsonify({
                    'error': True,
                    'message': f'Sequence {i + 1} must be a string'
                }), 400

            cleaned = _validate_protein_sequence(seq)
            if not cleaned:
                return jsonify({
                    'error': True,
                    'message': f'Sequence {i + 1}: invalid protein sequence. '
                               f'Must contain only standard amino acids (ACDEFGHIKLMNPQRSTVWY)'
                }), 400

            if len(cleaned) > 4096:
                return jsonify({
                    'error': True,
                    'message': f'Sequence {i + 1} too long: max 4096 residues (got {len(cleaned)})'
                }), 400

            sequences_clean.append(cleaned)

        # Check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < ALPHAFOLD2_RE_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {ALPHAFOLD2_RE_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Extract and validate MSA parameters
        msa_params = _validate_msa_params(data)

        # Multimer-specific parameters
        num_predictions_per_model = data.get('num_predictions_per_model', 1)
        try:
            num_predictions_per_model = max(1, min(int(num_predictions_per_model), 8))
        except (TypeError, ValueError):
            num_predictions_per_model = 1

        template_searcher = data.get('template_searcher', 'hhsearch').lower()
        if template_searcher not in VALID_TEMPLATE_SEARCHERS:
            template_searcher = 'hhsearch'

        max_msa_sequences = data.get('max_msa_sequences', None)
        if max_msa_sequences is not None:
            try:
                max_msa_sequences = max(1, min(int(max_msa_sequences), 100000))
            except (TypeError, ValueError):
                max_msa_sequences = None

        # Build API payload
        if not NVIDIA_API_KEY:
            logger.error("AlphaFold2-RE-Multimer request rejected: NVIDIA_API_KEY is not configured")
            return jsonify({'error': True, 'message': 'Model backend is not configured.'}), 503

        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": str(NVCF_POLL_SECONDS),
        }

        # structure_models_to_relax parameter
        structure_models_to_relax = data.get('structure_models_to_relax', 'all').lower()
        if structure_models_to_relax not in ('all', 'best', 'none'):
            structure_models_to_relax = 'all'

        payload = {
            "sequences": sequences_clean,
            "relax_prediction": msa_params['relax_prediction'],
            "structure_models_to_relax": structure_models_to_relax,
            "algorithm": msa_params['algorithm'],
            "e_value": msa_params['e_value'],
            "databases": msa_params['databases'],
            "iterations": msa_params['iterations'],
            "num_predictions_per_model": num_predictions_per_model,
            "template_searcher": template_searcher,
        }

        if max_msa_sequences is not None:
            payload["max_msa_sequences"] = max_msa_sequences

        total_length = sum(len(s) for s in sequences_clean)
        logger.info(
            f"AlphaFold2-RE-Multimer prediction request: user={session['user_id']}, "
            f"chains={len(sequences_clean)}, total_len={total_length}, "
            f"algorithm={msa_params['algorithm']}"
        )

        # Call AlphaFold2-Multimer API
        # connect timeout = 60s; read timeout = NVCF_POLL_SECONDS + 120s buffer
        response = requests.post(
            url=NVIDIA_ALPHAFOLD2_RE_MULTIMER_URL,
            headers=headers,
            json=payload,
            timeout=(60, NVCF_POLL_SECONDS + 120)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                result = {
                    "pdb_content": response.text,
                    "format": "pdb",
                    "num_sequences": len(sequences_clean),
                    "sequence_lengths": [len(s) for s in sequences_clean]
                }

            # Deduct credits
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (ALPHAFOLD2_RE_CREDIT_COST, session['user_id'])
            )
            db.commit()

            logger.info(
                f"AlphaFold2-RE-Multimer prediction completed (sync): user={session['user_id']}"
            )
            return jsonify(result)

        elif _is_async_response(response):
            nvcf_reqid = _extract_task_id(response)
            if not nvcf_reqid:
                logger.error("AlphaFold2-RE-Multimer 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(
                f"AlphaFold2-RE-Multimer async task started: {nvcf_reqid}, user={session['user_id']}"
            )

            # Store task metadata in Redis (TTL: 20 minutes for multimer)
            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': ALPHAFOLD2_RE_CREDIT_COST,
                    'model': 'alphafold2-re-multimer',
                    'num_sequences': len(sequences_clean),
                    'started_at': time.time()
                })
                r.setex(f"alphafold2rem:task:{nvcf_reqid}", ALPHAFOLD2_TASK_TTL, task_meta)
            except Exception as redis_err:
                logger.warning(f"AlphaFold2-RE-Multimer Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            return _handle_nvidia_error(response, "AlphaFold2-RE-Multimer")

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
            requests.exceptions.RequestException) as e:
        return _handle_request_exceptions(e, "AlphaFold2-RE-Multimer")
    except Exception as e:
        logger.error(f"AlphaFold2-RE-Multimer prediction error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/alphafold2-re-multimer/status/<task_id>', methods=['GET'])
@login_required
def alphafold2_re_multimer_status(task_id):
    """Check status of an async AlphaFold2-RE Multimer prediction task."""
    try:
        # Validate task_id format
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        # Look up task metadata in Redis
        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"alphafold2rem:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"AlphaFold2-RE-Multimer Redis lookup failed: {redis_err}")

        if not task_meta:
            return jsonify({'error': True, 'message': 'Task not found or expired'}), 404

        # Verify task ownership
        if task_meta.get('user_id') != session['user_id']:
            return jsonify({'error': True, 'message': 'Access denied'}), 403

        # Poll NVIDIA status once
        if not NVIDIA_API_KEY:
            logger.error("AlphaFold2-RE-Multimer status request rejected: NVIDIA_API_KEY is not configured")
            return jsonify({'error': True, 'message': 'Model backend is not configured.'}), 503

        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json"
        }
        try:
            status_response = _poll_status_once(task_id, headers)
        except requests.exceptions.Timeout:
            return jsonify({'status': 'pending', 'message': 'Status check timed out, still processing'})
        except requests.exceptions.RequestException as e:
            logger.error(f"AlphaFold2-RE-Multimer status check error: {e}")
            return jsonify({'status': 'pending', 'message': 'Status check failed, still processing'})

        if status_response.status_code == 202:
            elapsed = int(time.time() - task_meta.get('started_at', time.time()))
            return jsonify({'status': 'pending', 'elapsed': elapsed})

        elif status_response.status_code == 200:
            content_type = status_response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = status_response.json()
            else:
                result = {
                    "pdb_content": status_response.text,
                    "format": "pdb",
                    "num_sequences": task_meta.get('num_sequences')
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
                r.delete(f"alphafold2rem:task:{task_id}")
            except Exception:
                pass

            logger.info(
                f"AlphaFold2-RE-Multimer async task completed: {task_id}, user={session['user_id']}"
            )
            return jsonify(result)

        elif status_response.status_code == 404:
            try:
                r = get_redis_connection()
                r.delete(f"alphafold2rem:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"AlphaFold2-RE-Multimer status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"AlphaFold2-RE-Multimer status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


# ============================================================================
# INFO ROUTE
# ============================================================================

@app.route('/api/alphafold2-re/info', methods=['GET'])
@login_required
def alphafold2_re_info():
    """Get AlphaFold2-RE model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'models': {
                'alphafold2-re': {
                    'name': 'AlphaFold2-RE',
                    'provider': 'DeepMind',
                    'description': 'Single protein (monomer) structure prediction using AlphaFold2',
                    'max_sequence_length': 4096,
                    'credit_cost': ALPHAFOLD2_RE_CREDIT_COST,
                    'input': {
                        'sequence': {'type': 'string', 'required': True, 'max_length': 4096},
                        'relax_prediction': {'type': 'boolean', 'default': True},
                        'algorithm': {'type': 'string', 'options': ['jackhmmer', 'mmseqs2'], 'default': 'jackhmmer'},
                        'e_value': {'type': 'number', 'min': 0, 'max': 10, 'default': 0.0001},
                        'databases': {
                            'type': 'array',
                            'options': ['uniref90', 'small_bfd', 'mgnify'],
                            'default': ['uniref90', 'small_bfd', 'mgnify']
                        },
                        'iterations': {'type': 'integer', 'min': 1, 'default': 1},
                        'max_msa_sequences': {'type': 'integer', 'min': 1, 'max': 10000, 'default': 4000},
                        'skip_template_search': {'type': 'boolean', 'default': False},
                    },
                    'output': 'PDB structure',
                },
                'alphafold2-re-multimer': {
                    'name': 'AlphaFold2-RE Multimer',
                    'provider': 'DeepMind',
                    'description': 'Protein complex (multimer) structure prediction using AlphaFold2-Multimer',
                    'max_sequences': 6,
                    'max_sequence_length': 4096,
                    'credit_cost': ALPHAFOLD2_RE_CREDIT_COST,
                    'input': {
                        'sequences': {
                            'type': 'array',
                            'required': True,
                            'min_items': 1,
                            'max_items': 6,
                            'item_max_length': 4096
                        },
                        'relax_prediction': {'type': 'boolean', 'default': True},
                        'algorithm': {'type': 'string', 'options': ['jackhmmer'], 'default': 'jackhmmer'},
                        'e_value': {'type': 'number', 'min': 0, 'max': 10, 'default': 0.0001},
                        'databases': {
                            'type': 'array',
                            'options': ['uniref90', 'small_bfd', 'mgnify'],
                            'default': ['uniref90', 'small_bfd', 'mgnify']
                        },
                        'iterations': {'type': 'integer', 'min': 1, 'default': 1},
                        'num_predictions_per_model': {'type': 'integer', 'min': 1, 'max': 8, 'default': 1},
                        'max_msa_sequences': {'type': 'integer', 'min': 1, 'max': 10000, 'default': 4000, 'optional': True},
                        'template_searcher': {
                            'type': 'string',
                            'options': ['hhsearch', 'hmmsearch'],
                            'default': 'hhsearch'
                        },
                    },
                    'output': 'PDB structure',
                },
            },
            'credit_cost': ALPHAFOLD2_RE_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'capabilities': [
                'Single protein (monomer) structure prediction',
                'Multi-protein complex (multimer) structure prediction (up to 6 chains)',
                'Multiple Sequence Alignment (MSA) with jackhmmer or mmseqs2',
                'Structural relaxation (Amber)',
                'pLDDT confidence scores',
                'Template-based and template-free prediction',
            ],
        })
    except Exception as e:
        logger.error(f"AlphaFold2-RE info error: {e}")
        return _cloudflare_error(520)
