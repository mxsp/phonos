"""Model registry API: cost estimate, app version.

``/api/available_models`` (api_files.py) serves the registry itself; this module adds
``GET|POST /api/models/<id>/estimate`` and ``GET /api/version``. Model metadata
(ui, hosted_by, retired, replacement, credit_cost) lives in ``production/config.py``.
"""
from __future__ import annotations

import json
import os

from flask import jsonify, request, session

from config import Config

from ..app import app, logger
from ..helpers import get_db, login_required


def get_app_version() -> str:
    """One version string: a VERSION file in the repo root wins, else phonos_app.__version__."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    try:
        with open(os.path.join(root, 'VERSION'), encoding='utf-8') as fh:
            value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    from .. import __version__
    return __version__


@app.context_processor
def inject_phonos_version():
    return {'phonos_version': get_app_version()}


@app.context_processor
def inject_model_helpers():
    """Jinja helpers so templates never hard-code a price: {{ model_cost('type-9') }}."""
    def model_cost(model_id):
        return Config.calculate_adaptive_credit_cost(model_id)

    def model_runs_at(model_id):
        model = Config.get_model_definition(model_id) or {}
        hosted = model.get('hosted_by') or Config.DEFAULT_HOSTED_BY
        return {'provider': hosted.get('provider', ''), 'region': hosted.get('region', '')}

    return {'model_cost': model_cost, 'model_runs_at': model_runs_at,
            'public_model_table': Config.public_model_table}


@app.route('/api/version', methods=['GET'])
def api_version():
    return jsonify({'version': get_app_version()})


def _estimate_inputs():
    """Sequence and params from query string or JSON body."""
    body = request.get_json(silent=True) or {}
    sequence = body.get('sequence', request.args.get('sequence', '')) or ''
    params = body.get('params')
    if params is None:
        raw = request.args.get('params')
        if raw:
            try:
                params = json.loads(raw)
            except ValueError:
                params = None
    return str(sequence), params if isinstance(params, dict) else {}


@app.route('/api/models/<model_id>/estimate', methods=['GET', 'POST'])
@login_required
def api_model_estimate(model_id):
    model = Config.get_model_definition(model_id)
    if not model:
        return jsonify({'status': 'error', 'message': 'Unknown model.'}), 404
    try:
        sequence, params = _estimate_inputs()
        cost = int(Config.calculate_adaptive_credit_cost(model_id, sequence, params))
        row = get_db().execute('SELECT credits FROM users WHERE id = %s', (session['user_id'],)).fetchone()
        balance = int(row['credits'] or 0) if row else 0
        view = Config.public_model_view(model)
        return jsonify({
            'status': 'success',
            'model_id': model_id,
            'credit_cost': cost,
            'balance': balance,
            'balance_after': max(0, balance - cost),
            'sufficient': balance >= cost,
            'ui_state': view['ui_state'],
            'hosted_by': view['hosted_by'],
        })
    except Exception:
        logger.error('Estimate failed for %s', model_id, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Could not compute the estimate.'}), 500
