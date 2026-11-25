"""API routes for MSA-Search (NVIDIA NIM ColabFold) - utility endpoint."""
from __future__ import annotations

import os
import random
import re
import requests
from flask import jsonify, request, session

from ..app import app, logger
from ..helpers import get_db, login_required
from ..helpers.security import csrf_protect


# MSA-Search API configuration
NVIDIA_MSA_SEARCH_URL = os.getenv(
    "MSA_SEARCH_API_URL",
    "https://health.api.nvidia.com/v1/biology/colabfold/msa-search/predict"
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

# Cost per search (credits) - cheap utility
MSA_SEARCH_CREDIT_COST = 20

# Sequence validation pattern
PROTEIN_SEQUENCE_RE = re.compile(r'^[ARNDCQEGHILKMFPSTWYV]+$')

# Valid databases for MSA search
VALID_DATABASES = {
    'all', 'colabfold_envdb_202108', 'uniref30_2302',
    'pdb100_230517', 'uniref30_2202'
}

# Valid output formats
VALID_OUTPUT_FORMATS = {'a3m', 'fasta'}

# Valid search types
VALID_SEARCH_TYPES = {'colabfold', 'alphafold2'}


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


@app.route('/api/msa-search/predict', methods=['POST'])
@login_required
@csrf_protect
def msa_search_predict():
    """
    Run Multiple Sequence Alignment search using ColabFold MSA-Search (NVIDIA NIM).

    This is a utility endpoint used by other models (AlphaFold2, OpenFold3, Boltz2)
    to generate MSAs for structure prediction.

    Request body:
        sequence: Protein sequence (1-4096 chars, standard amino acids only)
        databases: Array of databases to query (default: ["all"])
        e_value: E-value threshold (default: 0.0001)
        iterations: Number of search iterations (1-6, default: 1)
        max_msa_sequences: Maximum MSA sequences to return (1-10001, default: 500)
        output_alignment_formats: Output formats, "a3m" and/or "fasta" (default: ["a3m"])
        search_type: Search algorithm - "colabfold" or "alphafold2" (default: "colabfold")

    Returns:
        JSON with alignments organized by database and format
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        # --- Validate sequence ---
        sequence = data.get('sequence', '').strip().upper()

        if not sequence:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        # Remove whitespace/newlines
        sequence = sequence.replace(' ', '').replace('\n', '').replace('\r', '')

        if len(sequence) < 1 or len(sequence) > 4096:
            return jsonify({
                'error': True,
                'message': f'Sequence length must be between 1 and 4096 residues (got {len(sequence)})'
            }), 400

        if not PROTEIN_SEQUENCE_RE.match(sequence):
            return jsonify({
                'error': True,
                'message': 'Invalid sequence: must contain only standard amino acids (ARNDCQEGHILKMFPSTWYV)'
            }), 400

        # --- Validate optional parameters ---

        # Databases
        databases = data.get('databases', ['all'])
        if not isinstance(databases, list) or len(databases) == 0:
            databases = ['all']
        for db_name in databases:
            if db_name not in VALID_DATABASES:
                return jsonify({
                    'error': True,
                    'message': f'Invalid database: {db_name}. Valid options: {", ".join(sorted(VALID_DATABASES))}'
                }), 400

        # E-value
        try:
            e_value = float(data.get('e_value', 0.0001))
        except (TypeError, ValueError):
            return jsonify({'error': True, 'message': 'e_value must be a number'}), 400
        if e_value <= 0:
            return jsonify({'error': True, 'message': 'e_value must be positive'}), 400

        # Iterations
        iterations = data.get('iterations', 1)
        try:
            iterations = int(iterations)
        except (TypeError, ValueError):
            return jsonify({'error': True, 'message': 'iterations must be an integer'}), 400
        iterations = min(max(1, iterations), 6)

        # Max MSA sequences
        max_msa_sequences = data.get('max_msa_sequences', 500)
        try:
            max_msa_sequences = int(max_msa_sequences)
        except (TypeError, ValueError):
            return jsonify({'error': True, 'message': 'max_msa_sequences must be an integer'}), 400
        max_msa_sequences = min(max(1, max_msa_sequences), 10001)

        # Output alignment formats
        output_alignment_formats = data.get('output_alignment_formats', ['a3m'])
        if not isinstance(output_alignment_formats, list) or len(output_alignment_formats) == 0:
            output_alignment_formats = ['a3m']
        for fmt in output_alignment_formats:
            if fmt not in VALID_OUTPUT_FORMATS:
                return jsonify({
                    'error': True,
                    'message': f'Invalid output format: {fmt}. Valid options: a3m, fasta'
                }), 400

        # Search type
        search_type = data.get('search_type', 'colabfold')
        if search_type not in VALID_SEARCH_TYPES:
            return jsonify({
                'error': True,
                'message': f'Invalid search_type: {search_type}. Valid options: colabfold, alphafold2'
            }), 400

        # --- Check user credits ---
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < MSA_SEARCH_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {MSA_SEARCH_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # --- Build API payload ---
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        payload = {
            "sequence": sequence,
            "databases": databases,
            "e_value": e_value,
            "iterations": iterations,
            "max_msa_sequences": max_msa_sequences,
            "output_alignment_formats": output_alignment_formats,
            "search_type": search_type
        }

        logger.info(
            f"MSA-Search request: user={session['user_id']}, "
            f"seq_len={len(sequence)}, dbs={databases}, "
            f"type={search_type}, max_seqs={max_msa_sequences}"
        )

        # --- Call NVIDIA MSA-Search API (synchronous) ---
        response = requests.post(
            url=NVIDIA_MSA_SEARCH_URL,
            headers=headers,
            json=payload,
            timeout=180  # MSA searches can take a while
        )

        if response.status_code != 200:
            logger.error(f"MSA-Search API error: {response.status_code} - {response.text[:500]}")
            if response.status_code == 429:
                return _cloudflare_error(503)
            elif response.status_code >= 500:
                return _cloudflare_error(502)
            elif response.status_code in (401, 403):
                return _cloudflare_error(521)
            else:
                return _cloudflare_error(520)

        content_type = response.headers.get("Content-Type", "")

        if "application/json" not in content_type:
            logger.error(f"MSA-Search unexpected content type: {content_type}")
            return _cloudflare_error(520)

        result = response.json()

        # Deduct credits
        db.execute(
            'UPDATE users SET credits = credits - %s WHERE id = %s',
            (MSA_SEARCH_CREDIT_COST, session['user_id'])
        )
        db.commit()

        logger.info(f"MSA-Search completed: user={session['user_id']}, seq_len={len(sequence)}")

        return jsonify(result)

    except requests.exceptions.Timeout:
        logger.error("MSA-Search API timeout")
        return _cloudflare_error(524)
    except requests.exceptions.ConnectionError:
        logger.error("MSA-Search connection error")
        return _cloudflare_error(522)
    except requests.exceptions.RequestException as e:
        logger.error(f"MSA-Search network error: {e}")
        return _cloudflare_error(502)
    except Exception as e:
        logger.error(f"MSA-Search error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/msa-search/info', methods=['GET'])
@login_required
def msa_search_info():
    """Get MSA-Search endpoint information and user's available credits."""
    try:
        db = get_db()
        user = db.execute(
            'SELECT credits FROM users WHERE id = %s',
            (session['user_id'],)
        ).fetchone()

        return jsonify({
            'service': 'MSA-Search (ColabFold)',
            'provider': 'Hosted',
            'description': 'Multiple Sequence Alignment search for protein structure prediction pipelines',
            'type': 'utility',
            'max_sequence_length': 4096,
            'credit_cost': MSA_SEARCH_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'parameters': {
                'sequence': {
                    'type': 'string',
                    'required': True,
                    'min_length': 1,
                    'max_length': 4096,
                    'pattern': '^[ARNDCQEGHILKMFPSTWYV]+$',
                    'description': 'Protein sequence (standard amino acids only)'
                },
                'databases': {
                    'type': 'array',
                    'default': ['all'],
                    'options': sorted(VALID_DATABASES),
                    'description': 'Databases to search against'
                },
                'e_value': {
                    'type': 'number',
                    'default': 0.0001,
                    'description': 'E-value threshold for significance'
                },
                'iterations': {
                    'type': 'integer',
                    'min': 1,
                    'max': 6,
                    'default': 1,
                    'description': 'Number of search iterations'
                },
                'max_msa_sequences': {
                    'type': 'integer',
                    'min': 1,
                    'max': 10001,
                    'default': 500,
                    'description': 'Maximum number of sequences in the alignment'
                },
                'output_alignment_formats': {
                    'type': 'array',
                    'default': ['a3m'],
                    'options': sorted(VALID_OUTPUT_FORMATS),
                    'description': 'Output alignment format(s)'
                },
                'search_type': {
                    'type': 'string',
                    'default': 'colabfold',
                    'options': sorted(VALID_SEARCH_TYPES),
                    'description': 'Search algorithm to use'
                }
            },
            'used_by': [
                'AlphaFold2 - structure prediction',
                'OpenFold3 - structure prediction',
                'Boltz2 - structure prediction (optional MSA input)'
            ]
        })
    except Exception as e:
        logger.error(f"MSA-Search info error: {e}")
        return _cloudflare_error(520)
