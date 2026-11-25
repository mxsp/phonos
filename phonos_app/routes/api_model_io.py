"""API routes for chaining model outputs into model inputs.

A job's output file — a designed FASTA from ProteinMPNN, a backbone PDB from
RFDiffusion, a docked SDF from DiffDock, a generated SMILES from MolMIM — is
almost always a valid input for some *other* model. These endpoints answer
"what is in this file, and which models can take it?" so both the UI and
external integrators can build chains without re-implementing format sniffing.

Three endpoints:

* ``GET /api/model-io/matrix``                                — the full compatibility table
* ``GET /api/jobs/<job_id>/artifacts/<name>/as-input``        — a job output
* ``GET /api/files/<file_id>/as-input``                       — an uploaded/project file

All three return the same ``payload``/``targets`` shape, so a caller can treat
job outputs and project files identically.
"""
from __future__ import annotations

import gzip
import os
from typing import Any, Optional

from flask import jsonify, request, session

from config import Config
from model_overrides import apply_overrides_to_models, fetch_model_overrides

from ..app import app, logger
from ..helpers import get_db, login_required
from ..helpers import get_safe_project_path, is_path_within_any, resolve_job_artifact_path
from ..helpers.model_io import classify_payload, compatible_models, describe_matrix
from ..helpers.projects import get_project_assignment
from ..helpers.worker_runtime import apply_runtime_status_to_models
from .api_files import (
    _artifact_bytes_from_policy,
    _get_allowed_path_prefixes,
    _is_safe_path_name,
    _resolve_job_artifact_dir,
    _safe_policy_dict,
)

# Reading a whole structure into memory is fine for the sizes our models emit,
# but a stray multi-GB trajectory should not be able to take the web worker
# down. Beyond this we classify from the extension alone.
MAX_CLASSIFY_BYTES = 16 * 1024 * 1024


def _visible_models() -> list[dict[str, Any]]:
    """The model catalog as the user sees it, with runtime availability applied."""
    try:
        db = get_db()
        overrides = fetch_model_overrides(db)
        models = apply_overrides_to_models(Config.SUPPORTED_MODELS, overrides)
    except Exception:
        logger.warning('model-io: falling back to static model catalog', exc_info=True)
        models = list(Config.SUPPORTED_MODELS)

    hidden = set(getattr(Config, 'UI_HIDDEN_MODEL_IDS', set()))
    models = [m for m in models if m.get('is_visible', True) and m.get('id') not in hidden]
    try:
        models = apply_runtime_status_to_models(models)
    except Exception:
        logger.warning('model-io: runtime status unavailable', exc_info=True)
    return models


def _decode(raw: Optional[bytes], filename: str) -> tuple[Optional[str], str]:
    """Best-effort text decode, transparently gunzipping a .gz artifact."""
    if raw is None:
        return None, filename
    name = filename
    if name.lower().endswith('.gz'):
        try:
            raw = gzip.decompress(raw)
            name = name[:-3]
        except OSError:
            return None, name
    for encoding in ('utf-8', 'latin-1'):
        try:
            return raw.decode(encoding), name
        except UnicodeDecodeError:
            continue
    return None, name


def _build_response(filename: str, content: Optional[str], source: dict[str, Any]):
    """Shared body for both as-input endpoints."""
    payload = classify_payload(filename, content)
    models = _visible_models()
    targets = compatible_models(payload['kinds'], models)
    unavailable = [
        m for m in compatible_models(payload['kinds'], models, include_unavailable=True)
        if not m['available']
    ]
    return jsonify({
        'status': 'success',
        'source': source,
        'payload': payload,
        'targets': targets,
        'unavailable_targets': unavailable,
    })


@app.route('/api/model-io/matrix', methods=['GET'])
@login_required
def model_io_matrix():
    """The payload-kind/model compatibility matrix.

    Stable, machine-readable description of what can be chained into what —
    intended for scripts and integrations that build pipelines programmatically.
    """
    return jsonify({'status': 'success', **describe_matrix(_visible_models())})


@app.route('/api/jobs/<string:job_id>/artifacts/<string:artifact_name>/as-input', methods=['GET'])
@login_required
def job_artifact_as_input(job_id: str, artifact_name: str):
    """Classify a job output and list the models that can consume it."""
    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute(
            "SELECT project_id, COALESCE(policy, metadata, '{}') AS policy_blob, creator_id, model, name, output_file "
            "FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        if not job:
            return jsonify({'status': 'error', 'message': 'Job not found.'}), 404
        job = dict(job)

        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job['creator_id'] != user_id:
            return jsonify({'status': 'error', 'message': 'You do not have permission to access this artifact.'}), 403

        if not _is_safe_path_name(artifact_name):
            return jsonify({'status': 'error', 'message': 'Invalid artifact name.'}), 400

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if not project:
            return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

        project_path = get_safe_project_path(job['project_id'], project['team_id'])
        if not project_path:
            return jsonify({'status': 'error', 'message': 'Project storage unavailable.'}), 404

        policy = _safe_policy_dict(job.get('policy_blob'))
        job_dir = _resolve_job_artifact_dir(project_path, job_id, job.get('output_file'), policy)
        artifact_path = resolve_job_artifact_path(
            project_path=project_path,
            job_id=job_id,
            artifact_name=artifact_name,
            output_file=job.get('output_file'),
            policy=policy,
            preferred_branch='main',
        ) or os.path.abspath(os.path.join(job_dir, artifact_name))
        job_root = os.path.abspath(os.path.join(project_path, 'jobs', job_id))

        if not is_path_within_any(artifact_path, [job_dir, job_root]):
            return jsonify({'status': 'error', 'message': 'Access to artifact is forbidden.'}), 403

        raw: Optional[bytes] = None
        if os.path.exists(artifact_path):
            try:
                if os.path.getsize(artifact_path) <= MAX_CLASSIFY_BYTES:
                    with open(artifact_path, 'rb') as fh:
                        raw = fh.read()
            except OSError:
                raw = None
        else:
            raw = _artifact_bytes_from_policy(policy, artifact_name)
            if raw is not None and len(raw) > MAX_CLASSIFY_BYTES:
                raw = None

        content, resolved_name = _decode(raw, artifact_name)
        return _build_response(resolved_name, content, {
            'type': 'job_artifact',
            'job_id': job_id,
            'project_id': job['project_id'],
            'artifact_name': artifact_name,
            'produced_by': job.get('model'),
            'job_name': job.get('name'),
            'download_url': f'/download_job_artifact/{job_id}/{artifact_name}',
        })
    except Exception:
        logger.error('model-io: failed to classify artifact %s of job %s', artifact_name, job_id, exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while reading the artifact.'}), 500


@app.route('/api/files/<int:file_id>/as-input', methods=['GET'])
@login_required
def project_file_as_input(file_id: int):
    """Classify a project file (upload or registered job output) the same way."""
    try:
        db = get_db()
        user_id = session['user_id']
        file_info = db.execute(
            'SELECT id, filename, path, project_id, size FROM files WHERE id = %s',
            (file_id,),
        ).fetchone()
        if not file_info:
            return jsonify({'status': 'error', 'message': 'File not found.'}), 404
        file_info = dict(file_info)

        if not get_project_assignment(db, user_id, file_info['project_id']):
            return jsonify({'status': 'error', 'message': 'You do not have permission to access this file.'}), 403

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (file_info['project_id'],)).fetchone()
        if not project:
            return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

        project_path = get_safe_project_path(file_info['project_id'], project['team_id'])
        allowed = _get_allowed_path_prefixes(project_path)
        file_path = os.path.abspath(file_info['path'] or '')
        real_path = os.path.realpath(file_path)
        if not (is_path_within_any(file_path, allowed) and is_path_within_any(real_path, allowed)):
            return jsonify({'status': 'error', 'message': 'Access to file path is forbidden.'}), 403

        raw: Optional[bytes] = None
        try:
            if os.path.exists(file_path) and os.path.getsize(file_path) <= MAX_CLASSIFY_BYTES:
                with open(file_path, 'rb') as fh:
                    raw = fh.read()
        except OSError:
            raw = None

        filename = file_info.get('filename') or os.path.basename(file_path)
        content, resolved_name = _decode(raw, filename)
        return _build_response(resolved_name, content, {
            'type': 'project_file',
            'file_id': file_id,
            'project_id': file_info['project_id'],
            'filename': filename,
            'download_url': f'/download_file/{file_id}',
        })
    except Exception:
        logger.error('model-io: failed to classify file %s', file_id, exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while reading the file.'}), 500


@app.route('/api/model-io/classify', methods=['POST'])
@login_required
def model_io_classify():
    """Classify an arbitrary snippet without storing it first.

    Handy while developing an integration: paste a filename and some content,
    get back the same ``payload``/``targets`` shape the file endpoints return.
    """
    data = request.get_json(silent=True) or {}
    filename = str(data.get('filename') or 'input.txt')[:255]
    content = data.get('content')
    if content is not None and not isinstance(content, str):
        return jsonify({'status': 'error', 'message': 'content must be a string'}), 400
    if isinstance(content, str) and len(content) > MAX_CLASSIFY_BYTES:
        return jsonify({'status': 'error', 'message': 'content too large'}), 413
    return _build_response(filename, content, {'type': 'inline', 'filename': filename})
