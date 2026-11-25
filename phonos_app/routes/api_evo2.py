"""API routes for Evo2 DNA generation."""
from __future__ import annotations

import json
import os
import random
import requests
from flask import jsonify, request, session

from ..app import app, logger
from ..helpers import get_db, login_required
from ..helpers.security import csrf_protect


# Evo2 API configuration
NVIDIA_EVO2_URL = os.getenv(
    "EVO2_API_URL",
    "https://health.api.nvidia.com/v1/biology/arc/evo2-40b/generate"
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
NVIDIA_EVO2_STATUS_URL = os.getenv(
    "EVO2_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status"
)

# Cost per generation (credits)
EVO2_CREDIT_COST = 20


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


@app.route('/api/evo2/generate', methods=['POST'])
@login_required
@csrf_protect
def evo2_generate():
    """
    Generate DNA sequence using Evo2-40B model.

    Request body:
        sequence: Input DNA sequence (seed)
        num_tokens: Number of tokens to generate (1-1200)
        temperature: Sampling temperature (0-1.3)
        top_k: Top-k sampling parameter (0-6)
        top_p: Nucleus sampling parameter (0-1)
        enable_sampled_probs: Whether to return sampling probabilities
        random_seed: Optional integer seed for reproducible generation
        enable_logits: Include logits for each generated token
        enable_elapsed_ms_per_token: Include per-token latency metrics

    Returns:
        JSON with generated sequence and optional probabilities
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        # Resolve sequence from direct input or uploaded file
        sequence = data.get('sequence', '').strip()
        file_id = data.get('file_id')
        if file_id:
            try:
                file_id = int(file_id)
            except (TypeError, ValueError):
                return jsonify({'error': True, 'message': 'Invalid file_id'}), 400
            db = get_db()
            file_row = db.execute(
                'SELECT id, project_id, path, name FROM files WHERE id = %s',
                (file_id,),
            ).fetchone()
            if not file_row:
                return jsonify({'error': True, 'message': 'File not found'}), 404
            try:
                with open(file_row['path'], 'r', encoding='utf-8', errors='ignore') as f:
                    raw = f.read().strip()
            except (OSError, IOError) as e:
                return jsonify({'error': True, 'message': f'Could not read file: {e}'}), 500
            if not raw:
                return jsonify({'error': True, 'message': 'Uploaded file is empty'}), 400
            # Strip FASTA headers if present
            cleaned = []
            for line in raw.splitlines():
                s = line.strip()
                if s and not s.startswith('>'):
                    cleaned.append(s)
            sequence = ''.join(cleaned)
            if not sequence:
                return jsonify({'error': True, 'message': 'No sequence data found in uploaded file'}), 400
        if not sequence:
            return jsonify({'error': True, 'message': 'No input sequence provided. Provide a sequence or a file_id.'}), 400

        # Validate sequence contains only valid nucleotides
        sequence = sequence.upper()
        valid_nucleotides = set('ATCGN')
        if not all(c in valid_nucleotides for c in sequence):
            return jsonify({
                'error': True,
                'message': 'Invalid sequence: must contain only A, T, C, G, N'
            }), 400

        # Get user and check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < EVO2_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {EVO2_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Extract parameters
        num_tokens = min(max(1, int(data.get('num_tokens', 100))), 1200)
        temperature = max(0.01, min(float(data.get('temperature', 0.7)), 1.3))
        top_k = min(max(0, int(data.get('top_k', 3))), 6)
        top_p = min(max(0, float(data.get('top_p', 0.0))), 1.0)
        enable_logits = bool(data.get('enable_logits', False))
        enable_elapsed_ms_per_token = bool(data.get('enable_elapsed_ms_per_token', False))
        random_seed = data.get('random_seed', None)
        if random_seed in ('', 'null'):
            random_seed = None
        if random_seed is not None:
            try:
                random_seed = int(random_seed)
            except (TypeError, ValueError):
                return jsonify({'error': True, 'message': 'random_seed must be an integer'}), 400
            random_seed = min(max(0, random_seed), 4294967295)

        # Force enable_sampled_probs to True as it is required for visualization
        enable_probs = True

        # Prepare API request
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "NVCF-POLL-SECONDS": "15",
        }

        payload = {
            "sequence": sequence,
            "num_tokens": num_tokens,
            "temperature": temperature,
            "top_k": top_k,
            "top_p": top_p,
            "enable_sampled_probs": enable_probs,
            "enable_logits": enable_logits,
            "enable_elapsed_ms_per_token": enable_elapsed_ms_per_token
        }
        if random_seed is not None:
            payload["random_seed"] = random_seed

        logger.info(f"Evo2 generation request: user={session['user_id']}, tokens={num_tokens}, seq_len={len(sequence)}")

        # Call Evo2 API (30s connect, 300s read — model can be slow to start)
        response = requests.post(
            url=NVIDIA_EVO2_URL,
            headers=headers,
            json=payload,
            timeout=(30, 300)
        )

        if response.status_code == 202:
            # Async task - poll NVCF status until complete
            task_id = response.headers.get("NVCF-REQID")
            if not task_id:
                logger.error("Evo2 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(f"Evo2 async task: {task_id}, user={session['user_id']}")
            status_url = f"{NVIDIA_EVO2_STATUS_URL}/{task_id}"
            poll_headers = {"Authorization": f"Bearer {NVIDIA_API_KEY}", "Accept": "application/json"}
            import time as _time
            deadline = _time.time() + 600  # 10 minute inline poll max

            while _time.time() < deadline:
                try:
                    st = requests.get(status_url, headers=poll_headers, timeout=30)
                    if st.status_code == 200:
                        response = st
                        break
                    elif st.status_code == 202:
                        _time.sleep(3)
                        continue
                    elif st.status_code == 404:
                        logger.error(f"Evo2 async task {task_id} not found")
                        return _cloudflare_error(502)
                    else:
                        logger.error(f"Evo2 status poll error: {st.status_code}")
                        return _cloudflare_error(502)
                except requests.exceptions.Timeout:
                    _time.sleep(3)
                    continue
                except requests.exceptions.RequestException as poll_err:
                    logger.error(f"Evo2 status poll request error: {poll_err}")
                    return _cloudflare_error(522)
            else:
                logger.error(f"Evo2 async task {task_id} timed out after 900s")
                return _cloudflare_error(524)

        if response.status_code != 200:
            logger.error(f"Evo2 API error: {response.status_code} - {response.text[:500]}")
            # Map upstream errors to Cloudflare-style errors
            if response.status_code == 429:
                return _cloudflare_error(503)  # Rate limited -> Service Unavailable
            elif response.status_code >= 500:
                return _cloudflare_error(502)  # Server error -> Bad Gateway
            elif response.status_code == 401 or response.status_code == 403:
                return _cloudflare_error(521)  # Auth error -> Web Server Is Down
            else:
                return _cloudflare_error(520)  # Unknown error

        content_type = response.headers.get("Content-Type", "")

        if "application/json" not in content_type:
            logger.error(f"Evo2 unexpected content type: {content_type}")
            return _cloudflare_error(520)  # Unexpected response

        result = response.json()

        # Deduct credits
        db.execute(
            'UPDATE users SET credits = credits - %s WHERE id = %s',
            (EVO2_CREDIT_COST, session['user_id'])
        )
        db.commit()

        logger.info(f"Evo2 generation completed: user={session['user_id']}, generated_len={len(result.get('sequence', ''))}")

        return jsonify(result)

    except requests.exceptions.Timeout:
        logger.error("Evo2 API timeout")
        return _cloudflare_error(524)  # Timeout -> A Timeout Occurred
    except requests.exceptions.ConnectionError:
        logger.error("Evo2 connection error")
        return _cloudflare_error(522)  # Connection error -> Connection Timed Out
    except requests.exceptions.RequestException as e:
        logger.error(f"Evo2 network error: {e}")
        return _cloudflare_error(502)  # Network error -> Bad Gateway
    except Exception as e:
        logger.error(f"Evo2 generation error: {e}", exc_info=True)
        return _cloudflare_error(520)  # Unknown error


@app.route('/api/evo2/info', methods=['GET'])
@login_required
def evo2_info():
    """Get Evo2 model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'model': 'Evo2-40B',
            'max_tokens': 1200,
            'credit_cost': EVO2_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'parameters': {
                'num_tokens': {'min': 1, 'max': 1200, 'default': 100},
                'temperature': {'min': 0.01, 'max': 1.3, 'default': 0.7},
                'top_k': {'min': 0, 'max': 6, 'default': 3},
                'top_p': {'min': 0, 'max': 1, 'default': 0.0},
                'random_seed': {'min': 0, 'max': 4294967295, 'default': None},
                'enable_sampled_probs': {'type': 'boolean', 'default': False},
                'enable_logits': {'type': 'boolean', 'default': False},
                'enable_elapsed_ms_per_token': {'type': 'boolean', 'default': False}
            }
        })
    except Exception as e:
        logger.error(f"Evo2 info error: {e}")
        return _cloudflare_error(520)
