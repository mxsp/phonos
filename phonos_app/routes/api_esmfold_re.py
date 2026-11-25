"""API routes for ESMFold-RE protein structure prediction (NVIDIA NIM)."""
from __future__ import annotations

import json
import os
import random
import re
import time
import requests
from flask import jsonify, request, session, render_template

from ..app import app, get_redis_connection, logger
from ..helpers import get_db, login_required
from ..helpers.security import csrf_protect


# ESMFold-RE API configuration
NVIDIA_ESMFOLD_RE_URL = os.getenv(
    "ESMFOLD_RE_API_URL",
    "https://health.api.nvidia.com/v1/biology/meta/esmfold"
)
NVIDIA_STATUS_URL = os.getenv(
    "ESMFOLD_RE_STATUS_URL",
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
ESMFOLD_RE_CREDIT_COST = 20

# NVCF polling settings
NVCF_POLL_SECONDS = 300
NVCF_POLL_INTERVAL = 2

# Sequence validation
ESMFOLD_RE_MAX_SEQ_LENGTH = 1024
ESMFOLD_RE_VALID_PATTERN = re.compile(r'^[ARNDCQEGHILKMFPSTWYVXBOU]+$')


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

@app.route('/esmfold-re')
@login_required
def esmfold_re_page():
    """Render the ESMFold-RE structure prediction page."""
    db = get_db()
    user_id = session.get('user_id')

    # Get user credits
    user = db.execute(
        'SELECT credits FROM users WHERE id = %s', (user_id,)
    ).fetchone()
    user_credits = user['credits'] if user else 0

    return render_template('esmfold_re.html', user_credits=user_credits)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/esmfold-re/predict', methods=['POST'])
@login_required
@csrf_protect
def esmfold_re_predict():
    """
    Predict protein structure using ESMFold-RE (NVIDIA NIM).

    Request body:
        sequence: Protein amino acid sequence (max 1024 chars)
            Valid characters: ARNDCQEGHILKMFPSTWYVXBOU

    Returns:
        JSON with predicted PDB structure or async task_id
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '').strip().upper()

        if not sequence:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        # Clean sequence - remove whitespace
        sequence_clean = sequence.replace(' ', '').replace('\n', '').replace('\r', '')

        # Validate sequence characters
        if not ESMFOLD_RE_VALID_PATTERN.match(sequence_clean):
            return jsonify({
                'error': True,
                'message': 'Invalid protein sequence: must contain only valid amino acid characters (ARNDCQEGHILKMFPSTWYVXBOU)'
            }), 400

        # Check sequence length
        if len(sequence_clean) < 1 or len(sequence_clean) > ESMFOLD_RE_MAX_SEQ_LENGTH:
            return jsonify({
                'error': True,
                'message': f'Sequence length must be between 1 and {ESMFOLD_RE_MAX_SEQ_LENGTH} residues (got {len(sequence_clean)})'
            }), 400

        # Get user and check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < ESMFOLD_RE_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {ESMFOLD_RE_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Prepare API request
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": "30",
        }

        payload = {
            "sequence": sequence_clean
        }

        logger.info(f"ESMFold-RE prediction request: user={session['user_id']}, seq_len={len(sequence_clean)}")

        # Call ESMFold-RE API (60s connect, 90s read)
        response = requests.post(
            url=NVIDIA_ESMFOLD_RE_URL,
            headers=headers,
            json=payload,
            timeout=(60, 90)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                logger.error(f"ESMFold-RE unexpected content type: {content_type}")
                return _cloudflare_error(520)

            # Immediate result - deduct credits and return
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (ESMFOLD_RE_CREDIT_COST, session['user_id'])
            )
            db.commit()
            logger.info(f"ESMFold-RE prediction completed (sync): user={session['user_id']}")
            return jsonify(result)

        elif response.status_code == 202:
            # Async task - return task_id immediately so frontend can poll
            nvcf_reqid = response.headers.get("NVCF-REQID")
            if not nvcf_reqid:
                logger.error("ESMFold-RE 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(f"ESMFold-RE async task started: {nvcf_reqid}, user={session['user_id']}")

            # Store task metadata in Redis (TTL: 10 minutes)
            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': ESMFOLD_RE_CREDIT_COST,
                    'started_at': time.time()
                })
                r.setex(f"esmfold-re:task:{nvcf_reqid}", 28800, task_meta)
            except Exception as redis_err:
                logger.warning(f"ESMFold-RE Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            logger.error(f"ESMFold-RE API error: {response.status_code} - {response.text[:500]}")
            if response.status_code == 429:
                return _cloudflare_error(503)
            elif response.status_code >= 500:
                return _cloudflare_error(502)
            elif response.status_code == 401 or response.status_code == 403:
                return _cloudflare_error(521)
            else:
                return _cloudflare_error(520)

    except requests.exceptions.Timeout:
        logger.error("ESMFold-RE API timeout")
        return _cloudflare_error(524)
    except requests.exceptions.ConnectionError:
        logger.error("ESMFold-RE connection error")
        return _cloudflare_error(522)
    except requests.exceptions.RequestException as e:
        logger.error(f"ESMFold-RE network error: {e}")
        return _cloudflare_error(502)
    except Exception as e:
        logger.error(f"ESMFold-RE prediction error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/esmfold-re/status/<task_id>', methods=['GET'])
@login_required
def esmfold_re_status(task_id):
    """Check status of an async ESMFold-RE prediction task."""
    try:
        # Validate task_id format (basic safety check)
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        # Look up task metadata in Redis
        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"esmfold-re:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"ESMFold-RE Redis lookup failed: {redis_err}")

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
            logger.error(f"ESMFold-RE status check error: {e}")
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
                r.delete(f"esmfold-re:task:{task_id}")
            except Exception:
                pass

            logger.info(f"ESMFold-RE async task completed: {task_id}, user={session['user_id']}")
            return jsonify(result)

        elif status_response.status_code == 404:
            # Task expired or not found
            try:
                r = get_redis_connection()
                r.delete(f"esmfold-re:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"ESMFold-RE status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"ESMFold-RE status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/esmfold-re/info', methods=['GET'])
@login_required
def esmfold_re_info():
    """Get ESMFold-RE model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'model': 'ESMFold-RE',
            'provider': 'Meta',
            'description': 'Protein structure prediction from amino acid sequence using ESMFold',
            'max_sequence_length': ESMFOLD_RE_MAX_SEQ_LENGTH,
            'credit_cost': ESMFOLD_RE_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'valid_characters': 'ARNDCQEGHILKMFPSTWYVXBOU',
            'output_format': 'PDB',
            'capabilities': [
                'Single-sequence protein structure prediction',
                'PDB format output',
                'Fast hosted inference',
                'No MSA required'
            ]
        })
    except Exception as e:
        logger.error(f"ESMFold-RE info error: {e}")
        return _cloudflare_error(520)
