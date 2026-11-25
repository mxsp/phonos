"""API routes for file operations - uploads, downloads, and file management."""
from __future__ import annotations

import base64
import gzip
import io
import json
import math
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import psycopg2
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import bcrypt
import redis
from flask import (
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import join_room
from werkzeug.utils import secure_filename

from config import Config

TEAM_MUTABLE_ROLES = Config.TEAM_MUTABLE_ROLES
TEAM_ROLE_ADMIN = Config.TEAM_ROLE_ADMIN
TEAM_ROLE_BILLING = Config.TEAM_ROLE_BILLING
TEAM_ROLE_MEMBER = Config.TEAM_ROLE_MEMBER
TEAM_ROLE_OWNER = Config.TEAM_ROLE_OWNER
PROJECT_MUTABLE_ROLES = Config.PROJECT_MUTABLE_ROLES
PROJECT_ROLE_CONTRIBUTOR = Config.PROJECT_ROLE_CONTRIBUTOR
PROJECT_ROLE_MANAGER = Config.PROJECT_ROLE_MANAGER
PROJECT_ROLE_VIEWER = Config.PROJECT_ROLE_VIEWER
from plan_service import DEFAULT_PLAN, get_plan_definition, is_valid_plan, normalize_plan
from model_overrides import apply_overrides_to_models, fetch_model_overrides, get_model_state

from ..app import NODE_ID_PATTERN, SUPPORTED_MODEL_IDS, app, get_redis_connection, logger, socketio
from ..helpers import (
    admin_required,
    allowed_file,
    apply_plan_to_user,
    build_plan_catalog,
    create_user_notification,
    delete_job_file_and_update_storage,
    determine_job_target_queue,
    ensure_monthly_credit_reset,
    ensure_notifications_table,
    ensure_personal_team,
    ensure_user_layout_columns,
    externalize_project_role,
    format_storage_bytes,
    generate_unique_team_slug,
    get_all_job_output_roots,
    get_db,
    get_team_membership,
    get_team_node_rows,
    get_project_assignment,
    get_project_storage_roots,
    get_safe_project_path,
    get_user_storage_state,
    is_valid_public_key,
    is_path_within,
    is_path_within_any,
    friendly_artifact_download_name,
    login_required,
    normalize_project_role,
    normalize_worker_types,
    list_job_artifact_dirs,
    resolve_file_path,
    resolve_job_artifact_path,
    resolve_job_branch_path,
    team_has_team_plan,
    team_is_enterprise,
    team_role_required,
    parse_sqlite_timestamp,
    parse_worker_types,
    project_role_required,
    record_job_notification,
    send_email,
    serialize_datetime,
    serialize_team,
    serialize_team_member,
    serialize_team_node,
    user_has_team_plan,
    tags as tags_helper,
)
from ..helpers.analytics import track_event
from ..helpers.projects import validate_file_creator_access
from ..helpers.worker_runtime import apply_runtime_status_to_models

# Import security decorators
from ..helpers.security import csrf_protect, rate_limit, sanitize_json_inputs, sanitize_html, sanitize_input, validate_sql_identifier
from ..helpers.file_scanner import scan_upload

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}


# Run CPU-bound (C-extension) parsing in a real OS thread so the single eventlet
# worker's hub keeps serving other greenlets instead of blocking. Degrades
# gracefully to a direct call when eventlet/tpool is unavailable (mirrors the
# psycogreen optional-import pattern in wsgi.py). The wrapped function MUST be
# pure parsing: it cannot touch Flask request context, the DB connection, or any
# non-threadsafe global — only plain args (strings/bytes/paths) and plain returns.
try:
    from eventlet import tpool as _tpool

    def _run_blocking(fn, *a, **kw):
        return _tpool.execute(fn, *a, **kw)
except Exception:  # pragma: no cover - depends on deploy env
    def _run_blocking(fn, *a, **kw):
        return fn(*a, **kw)


def _get_allowed_path_prefixes(project_path: Optional[str] = None) -> list:
    """Get all allowed path prefixes for file access.

    Dynamically includes all configured job output roots from the disk manager.
    """
    allowed_prefixes = []

    # Add project path if provided
    if project_path:
        allowed_prefixes.append(os.path.abspath(project_path))
        allowed_prefixes.append(os.path.realpath(project_path))

    # Add local job_outputs directory
    local_job_outputs = os.path.abspath(os.path.join(os.getcwd(), 'job_outputs'))
    allowed_prefixes.append(local_job_outputs)

    # Add configured JOB_OUTPUT_DIR
    config_job_outputs = os.path.abspath(app.config.get('JOB_OUTPUT_DIR', ''))
    if config_job_outputs:
        allowed_prefixes.append(config_job_outputs)
        allowed_prefixes.append(os.path.realpath(config_job_outputs))

    # Add fallback JOB_OUTPUT_DIR if configured
    fallback_dir = app.config.get('JOB_OUTPUT_DIR_FALLBACK', '')
    if fallback_dir:
        allowed_prefixes.append(os.path.abspath(fallback_dir))

    # Add all configured job output roots from disk manager
    try:
        for root in get_all_job_output_roots():
            if root and root not in allowed_prefixes:
                allowed_prefixes.append(os.path.abspath(root))
                allowed_prefixes.append(os.path.realpath(root))
    except Exception as e:
        logger.warning(f"Could not get job output roots from disk manager: {e}")

    # Add deployment-specific legacy prefixes (JOB_OUTPUT_LEGACY_PREFIXES) for
    # backwards compatibility with paths stored by other deployments.
    for path in app.config.get('JOB_OUTPUT_LEGACY_PREFIXES', []):
        if path not in allowed_prefixes:
            allowed_prefixes.append(path)

    # Remove empty strings and duplicates
    return list(set(p for p in allowed_prefixes if p))


def _is_safe_path_name(name: str) -> bool:
    """Return True for a single safe filename/path component."""
    if not name:
        return False
    return os.path.basename(name) == name and '/' not in name and '\\' not in name and '..' not in name


FILE_MANAGER_ARTIFACT_EXTENSIONS: set[str] = {
    '.pdb', '.ent', '.pdbqt', '.cif', '.mmcif',
    '.sdf', '.mol', '.mol2', '.xyz', '.smi', '.smiles',
    '.json', '.jsonl', '.csv', '.tsv', '.txt', '.log', '.md',
    '.yaml', '.yml', '.html',
    '.fasta', '.fa', '.faa', '.fna', '.seq', '.a3m', '.aln', '.msa', '.sto',
    '.npz', '.npy', '.pt', '.pth', '.ckpt', '.pkl', '.pickle', '.h5', '.hdf5', '.parquet',
    '.cube', '.cub', '.dx', '.map', '.mrc', '.mtz',
    '.zip', '.gz', '.bz2', '.xz', '.tar', '.tgz',
    '.pdf', '.png', '.jpg', '.jpeg', '.svg',
}


def _safe_policy_dict(policy_value: Any) -> dict[str, Any]:
    if isinstance(policy_value, dict):
        return policy_value
    if not policy_value:
        return {}
    try:
        parsed = json.loads(policy_value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _artifact_bytes_from_policy(policy: dict[str, Any], artifact_name: str) -> Optional[bytes]:
    """Recover artifact bytes from metadata for jobs where the artifact file is no longer on disk."""
    if not artifact_name:
        return None

    artifact_entries: list[dict[str, Any]] = []
    artifacts_info = policy.get('artifacts_info')
    if isinstance(artifacts_info, list):
        artifact_entries.extend(item for item in artifacts_info if isinstance(item, dict))

    compact_artifacts = policy.get('artifacts')
    if isinstance(compact_artifacts, list):
        artifact_entries.extend(item for item in compact_artifacts if isinstance(item, dict))

    for entry in artifact_entries:
        filename = entry.get('filename') or entry.get('name')
        if filename != artifact_name:
            continue

        content_b64 = entry.get('content_b64')
        if isinstance(content_b64, str) and content_b64:
            try:
                return base64.b64decode(content_b64)
            except (TypeError, ValueError):
                logger.warning("Failed to decode metadata content_b64 for artifact '%s'.", artifact_name)

        content = entry.get('content')
        if isinstance(content, str):
            return content.encode('utf-8')

    return None


def _build_model_artifact_display_name(
    model_id: Optional[str],
    filename: str,
    preferred_name: Optional[str] = None,
    confidence: Optional[float] = None,
) -> str:
    if preferred_name:
        return preferred_name

    model = (model_id or '').lower()
    lower = (filename or '').lower()

    # DiffDock / FoldDock / DiffDock-RE
    if model in {'type-3', 'type-5', 'diffdock-re', 'diffdock'}:
        pose_match = re.search(r'_pose_(\d+)', lower)
        rank_match = re.search(r'rank(\d+)', lower)

        if lower.endswith(('.pdb', '.cif', '.mmcif')) and (
            'receptor' in lower or 'protein' in lower or 'input' in lower
        ):
            return 'Receptor Protein'
        if '_docked_ligand' in lower:
            return 'Combined Docked Ligand'
        if pose_match:
            pose_idx = int(pose_match.group(1)) + 1
            if confidence is not None:
                return f'Pose {pose_idx} (conf: {confidence:.3f})'
            return f'Pose {pose_idx}'
        if rank_match:
            pose_idx = int(rank_match.group(1))
            if confidence is not None:
                return f'Pose {pose_idx} (conf: {confidence:.3f})'
            return f'Pose {pose_idx}'
        if 'confidence' in lower and lower.endswith('.json'):
            return 'Pose Confidence'

    # AlphaFold / ESMFold / OpenFold / ProteinMPNN
    if model in {
        'type-0', 'type-2', 'esmfold-re', 'alphafold2-re', 'alphafold2-re-multimer',
        'openfold3', 'proteinmpnn-re'
    }:
        ranked_match = re.search(r'ranked_(\d+)', lower)
        relaxed_match = re.search(r'relaxed_rank_(\d+)', lower)
        if ranked_match:
            return f'Ranked Model {int(ranked_match.group(1)) + 1}'
        if relaxed_match:
            return f'Relaxed Rank {int(relaxed_match.group(1))}'
        if 'pae' in lower:
            return 'Predicted Aligned Error'
        if 'plddt' in lower or 'confidence' in lower:
            return 'Confidence Scores'
        if lower.endswith(('.pdb', '.cif', '.mmcif')):
            return 'Predicted Structure'

    # Boltz2 family
    if model in {'type-9', 'boltz2', 'type-boltz2'}:
        if '_protein' in lower:
            return 'Protein Structure'
        if '_ligand' in lower:
            return 'Ligand Structure'
        if 'confidence' in lower or 'score' in lower:
            return 'Confidence Metrics'

    return filename


def _collect_job_artifacts_for_file_manager(
    model_id: Optional[str],
    policy: dict[str, Any],
    job_output_dir: str,
    main_output_filename: str = '',
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen_filenames: set[str] = set()
    main_output_basename = os.path.basename(main_output_filename or '')
    abs_job_output_dir = os.path.abspath(job_output_dir)
    job_root_dir = abs_job_output_dir
    if os.path.basename(os.path.dirname(abs_job_output_dir)) == 'branches':
        job_root_dir = os.path.dirname(os.path.dirname(abs_job_output_dir))

    def _add_artifact(
        filename: str,
        display_name: Optional[str] = None,
        confidence: Optional[float] = None,
        artifact_type: Optional[str] = None,
        size: Optional[int] = None,
        branch: Optional[str] = None,
        relative_path: Optional[str] = None,
        stored_path: Optional[str] = None,
    ) -> None:
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return
        if filename in seen_filenames:
            return
        if main_output_basename and filename == main_output_basename:
            return

        branch_name = str(branch).strip() if branch is not None else ''
        file_path = os.path.join(job_output_dir, filename)
        candidate_paths: list[str] = []
        if stored_path:
            candidate_paths.append(os.path.abspath(stored_path))
        if relative_path:
            candidate_paths.append(os.path.abspath(os.path.join(job_root_dir, relative_path)))
        if branch_name:
            candidate_paths.append(
                os.path.abspath(
                    os.path.join(job_root_dir, 'branches', branch_name, filename)
                )
            )
        candidate_paths.append(os.path.abspath(file_path))
        for candidate in candidate_paths:
            if os.path.exists(candidate):
                file_path = candidate
                break
        resolved_size = size if size is not None else 0
        if (not resolved_size) and os.path.exists(file_path):
            try:
                resolved_size = os.path.getsize(file_path)
            except OSError:
                resolved_size = 0
        if resolved_size <= 0 and not os.path.exists(file_path):
            # Preserve metadata-only entries only when they have explicit naming.
            if not display_name:
                return

        artifacts.append({
            'filename': filename,
            'display_name': _build_model_artifact_display_name(model_id, filename, display_name, confidence),
            'confidence': confidence,
            'type': artifact_type,
            'size': resolved_size,
            'branch': branch_name or None,
        })
        seen_filenames.add(filename)

    # 1) Worker-provided artifacts_info
    artifacts_info = policy.get('artifacts_info')
    if isinstance(artifacts_info, list):
        for item in artifacts_info:
            if not isinstance(item, dict):
                continue
            _add_artifact(
                filename=item.get('filename') or '',
                display_name=item.get('display_name'),
                confidence=item.get('confidence'),
                artifact_type=item.get('type'),
                size=item.get('size'),
                branch=item.get('branch'),
                relative_path=item.get('relative_path'),
                stored_path=item.get('path'),
            )

    # 2) Fallback to compact metadata.artifacts
    compact_artifacts = policy.get('artifacts')
    if isinstance(compact_artifacts, list):
        for item in compact_artifacts:
            if not isinstance(item, dict):
                continue
            _add_artifact(
                filename=item.get('filename') or item.get('name') or '',
                display_name=item.get('display_name'),
                confidence=item.get('confidence'),
                artifact_type=item.get('type'),
                size=item.get('size'),
                branch=item.get('branch'),
                relative_path=item.get('relative_path'),
                stored_path=item.get('path'),
            )

    # 3) Directory scan fallback for missing/legacy metadata
    allowed_exts = FILE_MANAGER_ARTIFACT_EXTENSIONS
    if os.path.isdir(job_output_dir):
        try:
            for filename in sorted(os.listdir(job_output_dir)):
                if not filename or filename.startswith('.'):
                    continue
                if filename in seen_filenames:
                    continue
                ext = os.path.splitext(filename)[1].lower()
                if ext not in allowed_exts:
                    continue
                _add_artifact(filename=filename)
        except OSError:
            pass

    def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        fname = (item.get('filename') or '').lower()
        model = (model_id or '').lower()

        # Docking-first ordering
        if model in {'type-3', 'type-5', 'diffdock-re', 'diffdock'}:
            if fname.endswith(('.pdb', '.cif', '.mmcif')) and (
                'receptor' in fname or 'protein' in fname or 'input' in fname
            ):
                return (0, 0, fname)
            if '_docked_ligand' in fname:
                return (1, 0, fname)
            pose_match = re.search(r'_pose_(\d+)', fname)
            if pose_match:
                return (2, int(pose_match.group(1)) + 1, fname)
            rank_match = re.search(r'rank(\d+)', fname)
            if rank_match:
                return (2, int(rank_match.group(1)), fname)
            if 'confidence' in fname or 'score' in fname:
                return (3, 0, fname)
            return (4, 0, fname)

        # Structure + metrics
        if fname.endswith(('.pdb', '.cif', '.mmcif')):
            return (0, 0, fname)
        if 'confidence' in fname or 'plddt' in fname or 'pae' in fname or 'score' in fname:
            return (1, 0, fname)
        if fname.endswith('.json'):
            return (2, 0, fname)
        return (3, 0, fname)

    artifacts.sort(key=_sort_key)
    return artifacts


def _resolve_job_artifact_dir(
    project_path: str,
    job_id: str,
    output_file: Optional[str],
    policy: Optional[dict[str, Any]] = None,
) -> str:
    """Resolve the most likely artifact directory for a job output."""
    fallback_dir = os.path.join(project_path, 'jobs', job_id)
    candidate_dirs: list[str] = []

    branch_dir = resolve_job_branch_path(
        project_path=project_path,
        job_id=job_id,
        output_file=output_file,
        policy=policy,
        branch='main',
        create=False,
    )
    if branch_dir:
        candidate_dirs.append(branch_dir)

    # Fallback: derive from output_file if branch_dir not found
    if not branch_dir and output_file:
        resolved_output = resolve_file_path(output_file) or output_file
        if resolved_output and os.path.exists(resolved_output):
            candidate_dirs.append(os.path.dirname(os.path.abspath(resolved_output)))

    if output_file:
        resolved_output = resolve_file_path(output_file) or output_file
        candidate_dirs.append(os.path.dirname(os.path.abspath(resolved_output)))

    candidate_dirs.append(fallback_dir)
    allowed_prefixes = _get_allowed_path_prefixes(project_path)

    for candidate in candidate_dirs:
        if not candidate:
            continue
        abs_candidate = os.path.abspath(candidate)
        real_candidate = os.path.realpath(candidate)
        if is_path_within_any(abs_candidate, allowed_prefixes) and is_path_within_any(real_candidate, allowed_prefixes):
            return abs_candidate

    return fallback_dir


@app.route('/api/files/upload', methods=['POST'])
@login_required
@csrf_protect
def upload_file_manager():
    db = get_db()
    user_id = session['user_id']
    try:
        project_id = int(request.form.get('project_id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'A valid Project ID is required.'}), 400

    # Validate user has permission to upload to this project
    is_valid, error_msg = validate_file_creator_access(db, user_id, project_id)
    if not is_valid:
        return jsonify({'status': 'error', 'message': error_msg}), 403

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    team_id = project['team_id']

    project_path = get_safe_project_path(project_id, team_id)
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Project path could not be determined or is invalid.'}), 404

    target_dir = os.path.join(project_path, 'uploads')
    os.makedirs(target_dir, exist_ok=True)

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part in the request.'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file.'}), 400

    if file and allowed_file(file.filename, PROJECT_ALLOWED_EXTENSIONS):
        filename = secure_filename(file.filename)
        file_path = os.path.join(target_dir, filename)

        if not is_path_within(file_path, target_dir):
            return jsonify({'status': 'error', 'message': 'Forbidden file path.'}), 403

        if os.path.exists(file_path):
            return jsonify({'status': 'error', 'message': 'A file with this name already exists in this location.'}), 409

        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        storage_state = get_user_storage_state(db, user_id)
        if storage_state['available'] <= 0:
            return jsonify({
                'status': 'error',
                'message': 'You have no storage remaining. Delete files or outputs before uploading new ones.',
                'storage': storage_state
            }), 507
        if file_size > storage_state['available']:
            return jsonify({
                'status': 'error',
                'message': (
                    f"Uploading this file requires {format_storage_bytes(file_size)}, "
                    f"but only {format_storage_bytes(storage_state['available'])} is available."
                ),
                'storage': storage_state
            }), 507

        # Save to temporary location first
        temp_path = file_path + '.tmp'
        file.save(temp_path)

        # Scan the file
        is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
        if not is_safe:
            # temp_path is automatically deleted by scan_upload
            return jsonify({'status': 'error', 'message': message}), 400

        # Move to final location only if safe
        os.rename(temp_path, file_path)

        notes = request.form.get('notes', None)
        # Sanitize notes for XSS protection
        sanitized_notes = sanitize_html(notes) if notes else None
        db.execute('BEGIN')
        try:
            cursor = db.execute('INSERT INTO files (project_id, creator_id, name, path, size, notes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
                                (project_id, user_id, filename, file_path, file_size, sanitized_notes))
            result = cursor.fetchone()

            file_id = result["id"] if result else None
            db.execute('UPDATE users SET storage_used = storage_used + %s WHERE id = %s', (file_size, user_id))

            # Track file upload event
            try:
                track_event(
                    user_id=user_id,
                    event_type='file_uploaded',
                    storage_delta=file_size,
                    project_id=project_id,
                    metadata={
                        'filename': filename,
                        'file_size': file_size
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to track file upload event: {e}")

            db.commit()
            new_file = db.execute("SELECT id, name, size, created_at, notes, project_id, 'upload' as type, NULL as job_id FROM files WHERE id = %s",
                                  (file_id,)).fetchone()
            if not new_file:
                # File was inserted but couldn't be retrieved - this shouldn't happen
                # but we handle it gracefully instead of crashing
                logger.error(f"File inserted but not found for lastrowid={cursor.lastrowid}, user {user_id}")
                return jsonify({'status': 'success', 'message': 'File uploaded successfully'}), 201
            file_dict = dict(new_file)
            # Convert datetime to ISO format string for JSON serialization
            if file_dict.get('created_at') and hasattr(file_dict['created_at'], 'isoformat'):
                file_dict['created_at'] = file_dict['created_at'].isoformat()
            return jsonify({'status': 'success', 'file': file_dict}), 201
        except Exception:
            db.rollback()
            if os.path.exists(file_path):
                os.remove(file_path)
            logger.error(f"Failed to upload file for user {user_id}.", exc_info=True)
            return jsonify({'status': 'error', 'message': 'An internal error occurred during file upload.'}), 500
    else:
        return jsonify({'status': 'error', 'message': 'File type not allowed.'}), 400

@app.route('/api/projects/<int:project_id>/files', methods=['GET', 'POST'])
@login_required
def handle_project_files(project_id):
    db = get_db()
    user_id = session['user_id']

    if request.method == 'GET':
        try:
            # Check read permission
            role = get_project_assignment(db, user_id, project_id)
            if not role:
                return jsonify({'status': 'error', 'message': 'You do not have access to this project.'}), 403

            project_row = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
            if not project_row:
                return jsonify({'status': 'error', 'message': 'Project not found.'}), 404
            project_path = get_safe_project_path(project_id, project_row['team_id'])
            if not project_path:
                return jsonify({'status': 'error', 'message': 'Project path could not be determined.'}), 404

            # OPTIMIZED: Single UNION query for files and jobs
            # This reduces DB round-trips and takes advantage of indexes
            query = """
                SELECT
                    id::text as id,
                    name as filename,
                    NULL as output_path,
                    size,
                    created_at,
                    notes,
                    'upload' as type,
                    NULL as job_id,
                    NULL as model,
                    NULL as status,
                    NULL as metadata_blob,
                    NULL as output_display_name
                FROM files
                WHERE project_id = %s AND path IS NOT NULL AND path != ''

                UNION ALL

                SELECT
                    job_id::text as id,
                    output_file as filename,
                    output_file as output_path,
                    output_file_size as size,
                    created_at,
                    'Output from model ' || model as notes,
                    'output' as type,
                    job_id,
                    model,
                    status,
                    COALESCE(policy, metadata, '{}') as metadata_blob,
                    output_display_name
                FROM jobs
                WHERE project_id = %s AND output_file IS NOT NULL

                ORDER BY created_at DESC
            """

            all_rows = db.execute(query, (project_id, project_id)).fetchall()
            all_files = []

            # Process results
            for row in all_rows:
                row_dict = dict(row)

                # Format output file display names
                if row_dict['type'] == 'output' and row_dict.get('filename'):
                    row_dict['filename'] = row_dict['output_display_name'] or os.path.basename(row_dict['filename'])
                    row_dict['output_variant'] = 'main'

                all_files.append(row_dict)

                # Add job artifacts from metadata (only for output type)
                if row_dict['type'] == 'output':
                    try:
                        output_path = row_dict.get('output_path')
                        metadata = _safe_policy_dict(row_dict.get('metadata_blob'))
                        output_dir = _resolve_job_artifact_dir(
                            project_path=project_path,
                            job_id=row_dict['job_id'],
                            output_file=output_path,
                            policy=metadata,
                        )
                        artifacts = _collect_job_artifacts_for_file_manager(
                            model_id=row_dict.get('model'),
                            policy=metadata,
                            job_output_dir=output_dir,
                            main_output_filename=output_path or '',
                        )

                        for artifact in artifacts:
                            artifact_filename = artifact.get('filename')
                            if not artifact_filename:
                                continue
                            all_files.append({
                                'id': f"{row_dict['job_id']}:{artifact_filename}",
                                'filename': artifact.get('display_name') or artifact_filename,
                                'size': artifact.get('size', 0),
                                'created_at': row_dict['created_at'],
                                'notes': f"Artifact from {row_dict.get('model', 'job')} job",
                                'type': 'output',
                                'job_id': row_dict['job_id'],
                                'model': row_dict['model'],
                                'status': row_dict['status'],
                                'artifact_filename': artifact_filename,
                                'artifact_type': artifact.get('type'),
                                'confidence': artifact.get('confidence'),
                                'output_variant': 'artifact',
                            })
                    except Exception as e:
                        logger.error(f"Error processing artifacts for job {row_dict.get('job_id')}: {e}")
                        # Continue processing other files even if one fails

            # Include directories that exist on disk but are not recorded as files.
            try:
                existing_names = {row.get('filename') for row in all_files if row.get('filename')}
                if project_path and os.path.isdir(project_path):
                    for entry in os.scandir(project_path):
                        if not entry.is_dir() or entry.name.startswith('.'):
                            continue
                        if entry.name in existing_names:
                            continue
                        all_files.append({
                            'id': 'dir:' + entry.name,
                            'filename': entry.name,
                            'size': 0,
                            'created_at': datetime.fromtimestamp(entry.stat().st_ctime),
                            'notes': '',
                            'type': 'directory',
                            'job_id': None,
                            'model': None,
                            'status': None,
                            'metadata_blob': None,
                            'output_display_name': None,
                        })
            except Exception as e:
                logger.warning(f"Could not scan project directories for project {project_id}: {e}")

            # Sort once at the end (artifacts share parent job's created_at, so order is preserved).
            # Normalize to ISO strings so datetime/str/None values all sort consistently.
            def _sort_key(row):
                value = row.get('created_at')
                if value is None:
                    return ''
                if hasattr(value, 'isoformat'):
                    return value.isoformat()
                return str(value)
            all_files.sort(key=_sort_key, reverse=True)
            return jsonify(all_files)
        except Exception as e:
            logger.error(f"Error fetching files for project {project_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to fetch project files.'}), 500

    # POST - Upload file
    # Validate user has permission to upload to this project
    is_valid, error_msg = validate_file_creator_access(db, user_id, project_id)
    if not is_valid:
        return jsonify({'status': 'error', 'message': error_msg}), 403

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    team_id = project['team_id']

    project_path = get_safe_project_path(project_id, team_id)
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Project path could not be determined or is invalid.'}), 404

    target_dir = os.path.join(project_path, 'uploads')
    os.makedirs(target_dir, exist_ok=True)

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part in the request.'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file.'}), 400

    if file and allowed_file(file.filename, PROJECT_ALLOWED_EXTENSIONS):
        filename = secure_filename(file.filename)
        file_path = os.path.join(target_dir, filename)

        if not is_path_within(file_path, target_dir):
            return jsonify({'status': 'error', 'message': 'Forbidden file path.'}), 403

        if os.path.exists(file_path):
            return jsonify({'status': 'error', 'message': 'A file with this name already exists in this location.'}), 409

        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        storage_state = get_user_storage_state(db, user_id)
        if storage_state['available'] <= 0:
            return jsonify({
                'status': 'error',
                'message': 'You have no storage remaining. Delete files or outputs before uploading new ones.',
                'storage': storage_state
            }), 507
        if file_size > storage_state['available']:
            return jsonify({
                'status': 'error',
                'message': (
                    f"Uploading this file requires {format_storage_bytes(file_size)}, "
                    f"but only {format_storage_bytes(storage_state['available'])} is available."
                ),
                'storage': storage_state
            }), 507

        # Save to temporary location first
        temp_path = file_path + '.tmp'
        file.save(temp_path)

        # Scan the file
        is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
        if not is_safe:
            # temp_path is automatically deleted by scan_upload
            return jsonify({'status': 'error', 'message': message}), 400

        # Move to final location only if safe
        os.rename(temp_path, file_path)

        notes = request.form.get('notes', None)
        # Sanitize notes for XSS protection
        sanitized_notes = sanitize_html(notes) if notes else None
        db.execute('BEGIN')
        try:
            cursor = db.execute('INSERT INTO files (project_id, creator_id, name, path, size, notes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
                                (project_id, user_id, filename, file_path, file_size, sanitized_notes))
            inserted_row = cursor.fetchone()
            new_file_id = inserted_row['id'] if inserted_row else None
            db.execute('UPDATE users SET storage_used = storage_used + %s WHERE id = %s', (file_size, user_id))
            db.commit()
            new_file = db.execute("SELECT id, name as filename, size, created_at, notes, project_id, 'upload' as type, NULL as job_id FROM files WHERE id = %s",
                                  (new_file_id,)).fetchone()
            file_dict = dict(new_file)
            # Convert datetime to ISO format string for JSON serialization
            if file_dict.get('created_at') and hasattr(file_dict['created_at'], 'isoformat'):
                file_dict['created_at'] = file_dict['created_at'].isoformat()

            # Record project history snapshot
            try:
                from phonos_app.helpers.project_history import log_project_event as log_ph
                log_ph(
                    project_id=project_id,
                    user_id=user_id,
                    action='file_upload',
                    resource_type='file',
                    resource_id=new_file_id,
                    resource_name=filename,
                    description=f"Uploaded {filename}",
                    metadata={'size': file_size, 'path': file_path},
                )
            except Exception:
                pass

            return jsonify({'status': 'success', 'file': file_dict}), 201
        except Exception:
            db.rollback()
            if os.path.exists(file_path):
                os.remove(file_path)
            logger.error(f"Failed to upload file for user {user_id}.", exc_info=True)
            return jsonify({'status': 'error', 'message': 'An internal error occurred during file upload.'}), 500
    else:
        return jsonify({'status': 'error', 'message': 'File type not allowed.'}), 400


@app.route('/api/projects/<int:project_id>/files/bulk', methods=['POST'])
@login_required
@csrf_protect
def bulk_upload_project_files(project_id):
    """
    Bulk file upload endpoint - handles multiple files in a single request.
    Uses batch database operations for better performance.
    Supports WebSocket progress updates.
    """
    db = get_db()
    user_id = session['user_id']

    # Check permissions
    is_valid, error_msg = validate_file_creator_access(db, user_id, project_id)
    if not is_valid:
        return jsonify({'status': 'error', 'message': error_msg}), 403

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    team_id = project['team_id']
    project_path = get_safe_project_path(project_id, team_id)
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Project path could not be determined or is invalid.'}), 404

    target_dir = os.path.join(project_path, 'uploads')
    os.makedirs(target_dir, exist_ok=True)

    # Get all uploaded files from the request
    if 'files' not in request.files:
        return jsonify({'status': 'error', 'message': 'No files in the request.'}), 400

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'status': 'error', 'message': 'No files selected.'}), 400

    # Filter valid files
    valid_files = [f for f in files if f and f.filename and allowed_file(f.filename, PROJECT_ALLOWED_EXTENSIONS)]
    if not valid_files:
        return jsonify({'status': 'error', 'message': 'No valid files to upload.'}), 400

    # Calculate total size needed
    total_size = 0
    file_info_list = []

    for file in valid_files:
        filename = secure_filename(file.filename)
        file_path = os.path.join(target_dir, filename)

        # Check path safety
        if not is_path_within(file_path, target_dir):
            continue

        # Check for duplicates
        if os.path.exists(file_path):
            continue

        # Get file size efficiently (stream-based)
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        total_size += file_size
        file_info_list.append({
            'file': file,
            'filename': filename,
            'path': file_path,
            'size': file_size
        })

    if not file_info_list:
        return jsonify({'status': 'error', 'message': 'All files were invalid, duplicates, or forbidden.'}), 400

    # Check storage quota for all files at once
    storage_state = get_user_storage_state(db, user_id)
    if storage_state['available'] <= 0:
        return jsonify({
            'status': 'error',
            'message': 'You have no storage remaining. Delete files or outputs before uploading new ones.',
            'storage': storage_state
        }), 507

    if total_size > storage_state['available']:
        return jsonify({
            'status': 'error',
            'message': (
                f"Uploading these files requires {format_storage_bytes(total_size)}, "
                f"but only {format_storage_bytes(storage_state['available'])} is available."
            ),
            'storage': storage_state
        }), 507

    # Process files with progress tracking
    uploaded_files = []
    failed_files = []
    total_uploaded_size = 0
    upload_session_id = str(uuid.uuid4())

    # Emit initial progress via WebSocket (if available)
    try:
        from phonos_app.app import socketio
        socketio.emit('bulk_upload_progress', {
            'session_id': upload_session_id,
            'total_files': len(file_info_list),
            'processed': 0,
            'uploaded': 0,
            'failed': 0,
            'status': 'starting'
        }, room=f"user-{user_id}")
    except Exception:
        pass  # WebSocket is optional

    for idx, file_info in enumerate(file_info_list):
        try:
            file = file_info['file']
            filename = file_info['filename']
            file_path = file_info['path']
            file_size = file_info['size']

            # Save to temporary location first (chunked write for large files)
            temp_path = file_path + '.tmp'

            # Use chunked writing for better memory efficiency
            CHUNK_SIZE = 64 * 1024  # 64KB chunks
            with open(temp_path, 'wb') as f:
                while True:
                    chunk = file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)

            # Scan the file
            is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
            if not is_safe:
                failed_files.append({'filename': filename, 'reason': message})
                # Emit progress update
                try:
                    socketio.emit('bulk_upload_progress', {
                        'session_id': upload_session_id,
                        'total_files': len(file_info_list),
                        'processed': idx + 1,
                        'uploaded': len(uploaded_files),
                        'failed': len(failed_files),
                        'current_file': filename,
                        'status': 'failed',
                        'reason': message
                    }, room=f"user-{user_id}")
                except Exception:
                    pass
                continue

            # Move to final location
            os.rename(temp_path, file_path)

            uploaded_files.append({
                'filename': filename,
                'path': file_path,
                'size': file_size
            })
            total_uploaded_size += file_size

            # Emit progress update
            try:
                socketio.emit('bulk_upload_progress', {
                    'session_id': upload_session_id,
                    'total_files': len(file_info_list),
                    'processed': idx + 1,
                    'uploaded': len(uploaded_files),
                    'failed': len(failed_files),
                    'current_file': filename,
                    'status': 'success'
                }, room=f"user-{user_id}")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Error uploading file {file_info['filename']}: {e}", exc_info=True)
            failed_files.append({'filename': file_info['filename'], 'reason': str(e)})
            # Clean up temp file if it exists
            temp_path = file_info['path'] + '.tmp'
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    # Batch insert into database using executemany for better performance
    if uploaded_files:
        db.execute('BEGIN')
        try:
            # Get common notes for all files (optional)
            notes = request.form.get('notes', None)
            sanitized_notes = sanitize_html(notes) if notes else None

            # Prepare batch insert data
            insert_data = [
                (project_id, user_id, f['filename'], f['path'], f['size'], sanitized_notes)
                for f in uploaded_files
            ]

            # Use executemany for batch insert
            db.executemany(
                'INSERT INTO files (project_id, creator_id, name, path, size, notes) VALUES (%s, %s, %s, %s, %s, %s)',
                insert_data
            )

            # Update user storage in single query
            db.execute('UPDATE users SET storage_used = storage_used + %s WHERE id = %s',
                      (total_uploaded_size, user_id))

            db.commit()

            # Fetch inserted files for response
            file_paths = [f['path'] for f in uploaded_files]
            placeholders = ','.join(['%s'] * len(file_paths))
            inserted_files = db.execute(
                f"SELECT id, name as filename, size, created_at, notes, project_id, 'upload' as type, NULL as job_id FROM files WHERE path IN ({placeholders})",
                file_paths
            ).fetchall()

            inserted_files_list = []
            for f in inserted_files:
                file_dict = dict(f)
                if file_dict.get('created_at') and hasattr(file_dict['created_at'], 'isoformat'):
                    file_dict['created_at'] = file_dict['created_at'].isoformat()
                inserted_files_list.append(file_dict)

            # Emit completion
            try:
                socketio.emit('bulk_upload_progress', {
                    'session_id': upload_session_id,
                    'total_files': len(file_info_list),
                    'processed': len(file_info_list),
                    'uploaded': len(uploaded_files),
                    'failed': len(failed_files),
                    'status': 'completed'
                }, room=f"user-{user_id}")
            except Exception:
                pass

            return jsonify({
                'status': 'success',
                'uploaded': len(uploaded_files),
                'failed': len(failed_files),
                'files': inserted_files_list,
                'failed_files': failed_files,
                'total_size': total_uploaded_size,
                'session_id': upload_session_id
            }), 201

        except Exception as e:
            db.rollback()
            # Clean up uploaded files on database error
            for f in uploaded_files:
                if os.path.exists(f['path']):
                    try:
                        os.remove(f['path'])
                    except Exception:
                        pass
            logger.error(f"Failed to save bulk upload to database for user {user_id}: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'An internal error occurred during bulk upload.'}), 500
    else:
        return jsonify({
            'status': 'partial_failure',
            'uploaded': 0,
            'failed': len(failed_files),
            'failed_files': failed_files,
            'message': 'All files failed to upload.'
        }), 400


@app.route('/api/projects/<int:project_id>/files/bulk-delete', methods=['POST'])
@login_required
@csrf_protect
def bulk_delete_project_files(project_id):
    """
    Bulk file deletion endpoint - deletes multiple files in a single request.
    Uses database function for atomic operation and efficient storage updates.
    """
    db = get_db()
    user_id = session['user_id']

    # Check permissions
    role = get_project_assignment(db, user_id, project_id)
    if not role or role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to delete files from this project.'}), 403

    # Get file IDs from request
    data = request.get_json()
    if not data or 'file_ids' not in data:
        return jsonify({'status': 'error', 'message': 'No file IDs provided.'}), 400

    file_ids = data['file_ids']
    if not isinstance(file_ids, list) or not file_ids:
        return jsonify({'status': 'error', 'message': 'Invalid file IDs format.'}), 400

    # Validate all files belong to this project and user has permission
    placeholders = ','.join(['%s'] * len(file_ids))
    files_to_delete = db.execute(
        f"SELECT id, path, size, project_id, creator_id FROM files WHERE id IN ({placeholders}) AND project_id = %s",
        file_ids + [project_id]
    ).fetchall()

    if not files_to_delete:
        return jsonify({'status': 'error', 'message': 'No valid files found to delete.'}), 404

    # Verify project access
    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    project_path = get_safe_project_path(project_id, project['team_id'])
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Invalid project path.'}), 404

    # Process deletions
    deleted_files = []
    failed_deletions = []
    total_freed_space = 0

    db.execute('BEGIN')
    try:
        for file_row in files_to_delete:
            file_dict = dict(file_row)
            file_id = file_dict['id']
            file_path = file_dict['path']
            file_size = file_dict['size']
            creator_id = file_dict['creator_id']

            # Path safety check — use abspath + os.sep to prevent prefix collisions
            safe_prefix = os.path.abspath(project_path) + os.sep
            if not os.path.abspath(file_path).startswith(safe_prefix):
                failed_deletions.append({
                    'file_id': file_id,
                    'reason': 'Invalid file path'
                })
                continue

            try:
                # Delete physical file if it exists
                if os.path.exists(file_path):
                    os.remove(file_path)

                # Delete from database
                db.execute('DELETE FROM files WHERE id = %s', (file_id,))

                # Update creator's storage quota
                db.execute('UPDATE users SET storage_used = GREATEST(0, storage_used - %s) WHERE id = %s',
                          (file_size, creator_id))

                deleted_files.append(file_id)
                total_freed_space += file_size

            except Exception as e:
                logger.error(f"Error deleting file {file_id}: {e}", exc_info=True)
                failed_deletions.append({
                    'file_id': file_id,
                    'reason': str(e)
                })

        db.commit()

        # Emit notification via WebSocket
        try:
            from phonos_app.app import socketio
            socketio.emit('bulk_delete_complete', {
                'project_id': project_id,
                'deleted_count': len(deleted_files),
                'freed_space': total_freed_space
            }, room=f"user-{user_id}")
        except Exception:
            pass

        return jsonify({
            'status': 'success',
            'deleted': len(deleted_files),
            'failed': len(failed_deletions),
            'deleted_file_ids': deleted_files,
            'failed_deletions': failed_deletions,
            'freed_space': total_freed_space
        }), 200

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to bulk delete files for user {user_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred during bulk deletion.'}), 500


@app.route('/api/all_files_view', methods=['GET'])
@login_required
def get_all_files_view():
    try:
        db = get_db()
        user_id = session['user_id']

        # Fetch projects the user is assigned to (exclude archived)
        assigned_projects_q = db.execute(
            '''SELECT pa.project_id FROM project_assignments pa
               JOIN projects p ON pa.project_id = p.id
               WHERE pa.user_id = %s AND (p.is_archived = 0 OR p.is_archived IS NULL)''',
            (user_id,)
        ).fetchall()
        project_ids = sorted({row['project_id'] for row in assigned_projects_q if row['project_id'] is not None})

        upload_conditions = []
        upload_params: list[Any] = []
        if project_ids:
            placeholders = ','.join('%s' for _ in project_ids)
            upload_conditions.append(f"f.project_id IN ({placeholders})")
            upload_params.extend(project_ids)
        upload_conditions.append('f.creator_id = %s')
        upload_params.append(user_id)

        output_conditions = []
        output_params: list[Any] = []
        if project_ids:
            placeholders = ','.join('%s' for _ in project_ids)
            output_conditions.append(f"j.project_id IN ({placeholders})")
            output_params.extend(project_ids)
        output_conditions.append('j.creator_id = %s')
        output_params.append(user_id)

        # Fetch all files from those projects or created by the user
        uploaded_files_query = (
            "SELECT f.id, f.name as filename, f.size, f.created_at, f.updated_at, f.notes, p.name as project_name, p.id as project_id, "
            "'upload' as type, NULL as job_id, NULL as model, NULL as status "
            "FROM files f "
            "LEFT JOIN projects p ON f.project_id = p.id "
            f"WHERE (p.is_archived = 0 OR p.is_archived IS NULL) AND ({' OR '.join(upload_conditions)})"
        )
        uploaded_files = db.execute(uploaded_files_query, tuple(upload_params)).fetchall()

        output_files_query = (
            "SELECT j.job_id as id, j.output_file as filename, j.output_display_name, j.output_file_size as size, "
            "j.created_at, j.updated_at, 'Output from model ' || j.model as notes, p.name as project_name, "
            "p.id as project_id, 'output' as type, j.job_id, j.model, j.status, j.name as job_name, j.policy "
            "FROM jobs j "
            "LEFT JOIN projects p ON j.project_id = p.id "
            f"WHERE j.output_file IS NOT NULL AND (p.is_archived = 0 OR p.is_archived IS NULL) AND ({' OR '.join(output_conditions)})"
        )
        output_files_q = db.execute(output_files_query, tuple(output_params)).fetchall()

        all_files_list = [dict(f) for f in uploaded_files]
        for row_dict in all_files_list:
            row_dict['name'] = row_dict.get('filename')
        for row in output_files_q:
            row_dict = dict(row)
            output_path = row_dict.get('filename')
            if row_dict.get('filename'):
                row_dict['filename'] = row_dict['output_display_name'] or os.path.basename(row_dict['filename'])
            row_dict['output_path'] = output_path
            row_dict['output_variant'] = 'main'
            row_dict['name'] = row_dict.get('filename')
            all_files_list.append(row_dict)

        # Include job artifacts (multiple output files per job) from policy metadata
        artifact_job_ids = []
        artifact_params: list[Any] = []
        if project_ids:
            placeholders = ','.join('%s' for _ in project_ids)
            artifact_conditions = f"j.project_id IN ({placeholders})"
            artifact_params.extend(project_ids)
        else:
            artifact_conditions = "1=0"
        artifact_conditions += ' OR j.creator_id = %s'
        artifact_params.append(user_id)

        artifact_jobs_query = (
            "SELECT j.job_id, j.output_file, COALESCE(j.policy, j.metadata, '{}') as policy_blob, "
            "j.created_at, j.updated_at, j.model, j.status, "
            "j.name as job_name, p.name as project_name, p.id as project_id, p.team_id "
            "FROM jobs j "
            "LEFT JOIN projects p ON j.project_id = p.id "
            f"WHERE COALESCE(j.policy, j.metadata) IS NOT NULL AND j.status = 'completed' AND (p.is_archived = 0 OR p.is_archived IS NULL) AND ({artifact_conditions})"
        )
        artifact_jobs = db.execute(artifact_jobs_query, tuple(artifact_params)).fetchall()

        for job_row in artifact_jobs:
            try:
                policy = _safe_policy_dict(job_row.get('policy_blob'))

                project_path = get_safe_project_path(job_row['project_id'], job_row['team_id'])
                if not project_path:
                    continue
                main_output_filename = job_row.get('output_file') or ''
                job_output_dir = _resolve_job_artifact_dir(
                    project_path=project_path,
                    job_id=job_row['job_id'],
                    output_file=main_output_filename,
                    policy=policy,
                )
                artifacts_info = _collect_job_artifacts_for_file_manager(
                    model_id=job_row.get('model'),
                    policy=policy,
                    job_output_dir=job_output_dir,
                    main_output_filename=main_output_filename,
                )
                if not artifacts_info:
                    continue

                for artifact in artifacts_info:
                    art_filename = artifact.get('filename', '')
                    if not art_filename:
                        continue
                    art_path = os.path.join(job_output_dir, art_filename)
                    # Skip if it's the same as the main output file (already listed)
                    art_size = artifact.get('size', 0)
                    if not art_size and os.path.exists(art_path):
                        art_size = os.path.getsize(art_path)
                    all_files_list.append({
                        'id': job_row['job_id'] + ':' + art_filename,
                        'filename': artifact.get('display_name') or art_filename,
                        'name': artifact.get('display_name') or art_filename,
                        'size': art_size,
                        'created_at': job_row['created_at'],
                        'updated_at': job_row['updated_at'],
                        'notes': f"Output from model {job_row['model']}",
                        'project_name': job_row['project_name'],
                        'project_id': job_row['project_id'],
                        'type': 'output',
                        'job_id': job_row['job_id'],
                        'job_name': job_row['job_name'],
                        'model': job_row['model'],
                        'status': job_row['status'],
                        'artifact_filename': art_filename,
                        'artifact_type': artifact.get('type'),
                        'confidence': artifact.get('confidence'),
                        'output_variant': 'artifact',
                    })
            except Exception as artifact_error:
                logger.warning(
                    "Skipping artifact parse for job %s in all-files view: %s",
                    job_row.get('job_id'),
                    artifact_error,
                )
                continue

        all_files_list.sort(key=lambda x: x['created_at'] or '', reverse=True)
        return jsonify(all_files_list)
    except Exception as e:
        logger.error(f"Error fetching files view: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch files.'}), 500

@app.route('/api/files/<int:file_id>/rename', methods=['PATCH'])
@login_required
@csrf_protect
def rename_uploaded_file(file_id):
    db = get_db()
    user_id = session['user_id']
    data = request.json or {}
    new_name = data.get('name')
    if not new_name: return jsonify({'status': 'error', 'message': 'New name not provided.'}), 400
    new_filename_safe = secure_filename(new_name)
    if new_filename_safe != new_name:
        return jsonify({'status': 'error', 'message': 'Invalid characters in new name.'}), 400

    file_info = db.execute('SELECT path, project_id, creator_id FROM files WHERE id = %s', (file_id,)).fetchone()
    if not file_info: return jsonify({'status': 'error', 'message': 'File not found.'}), 404

    role = get_project_assignment(db, user_id, file_info['project_id'])
    if not role or role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to rename this file.'}), 403

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (file_info['project_id'],)).fetchone()
    if not project: return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    project_path = get_safe_project_path(file_info['project_id'], project['team_id'])
    if not project_path: return jsonify({'status': 'error', 'message': 'Project path could not be determined.'}), 500

    current_dir = os.path.dirname(file_info['path'])
    new_filepath = os.path.join(current_dir, new_filename_safe)

    if not is_path_within(new_filepath, project_path):
        return jsonify({'status': 'error', 'message': 'Forbidden file path after rename.'}), 403
    if os.path.exists(new_filepath): return jsonify({'status': 'error', 'message': 'A file with this name already exists.'}), 409
    try:
        os.rename(file_info['path'], new_filepath)
        db.execute('UPDATE files SET name = %s, path = %s WHERE id = %s', (new_filename_safe, new_filepath, file_id))
        db.commit()
        return jsonify({'status': 'success', 'message': 'File renamed.'})
    except Exception:
        logger.error(f"Error renaming file {file_id} for user {user_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while renaming the file.'}), 500

@app.route('/api/jobs/<string:job_id>/rename', methods=['PATCH'])
@login_required
@csrf_protect
def rename_job_output(job_id):
    db = get_db()
    user_id = session['user_id']
    data = request.json or {}
    new_name = data.get('name')
    if not new_name: return jsonify({'status': 'error', 'message': 'New name not provided.'}), 400
    job_info = db.execute('SELECT project_id FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
    if not job_info:
        return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

    role = get_project_assignment(db, user_id, job_info['project_id'])
    if not role or role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to rename this job output.'}), 403
    # Update both name and output_display_name for consistency
    db.execute('UPDATE jobs SET name = %s, output_display_name = %s WHERE job_id = %s', (new_name, new_name, job_id))
    db.commit()
    return jsonify({'status': 'success', 'message': 'Job renamed successfully.'})

@app.route('/api/files/<int:file_id>/content', methods=['GET', 'PUT'])
@login_required
@csrf_protect
def handle_file_content(file_id):
    db = get_db()
    user_id = session['user_id']
    file_info = db.execute('SELECT path, project_id, size, creator_id FROM files WHERE id = %s', (file_id,)).fetchone()
    if not file_info: return jsonify({'status': 'error', 'message': 'File not found or access denied.'}), 404

    role = get_project_assignment(db, user_id, file_info['project_id'])
    if not role:
        return jsonify({'status': 'error', 'message': 'You do not have permission to access this file.'}), 403
    if request.method == 'PUT' and role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to edit this file.'}), 403

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (file_info['project_id'],)).fetchone()
    if not project: return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    project_path = get_safe_project_path(file_info['project_id'], project['team_id'])
    
    # Use robust path validation similar to download_file
    allowed_prefixes = _get_allowed_path_prefixes(project_path)
    file_path = os.path.abspath(file_info['path'])
    real_path = os.path.realpath(file_path)
    
    is_safe = is_path_within_any(file_path, allowed_prefixes) and is_path_within_any(real_path, allowed_prefixes)
            
    if not is_safe:
        return jsonify({'status': 'error', 'message': 'Access to file path is forbidden.'}), 403

    if request.method == 'GET':
        try:
            max_lines = request.args.get('max_lines', type=int)
            # Guard against reading very large files into memory
            MAX_CONTENT_READ_BYTES = 50 * 1024 * 1024  # 50 MB
            try:
                file_stat_size = os.path.getsize(file_path)
            except OSError:
                file_stat_size = 0
            if file_stat_size > MAX_CONTENT_READ_BYTES:
                return jsonify({'status': 'error', 'message': f'File too large to preview ({file_stat_size // (1024*1024)} MB). Please download instead.'}), 413

            with open(file_path, 'rb') as f:
                if max_lines:
                    raw_bytes = b''
                    hit_eof = True
                    for _ in range(max_lines):
                        line = f.readline()
                        if not line:
                            hit_eof = True
                            break
                        raw_bytes += line
                        hit_eof = False
                    else:
                        # loop completed without break -> max_lines read,
                        # and there may be more content remaining.
                        hit_eof = False
                    # Only report truncation when content was actually cut
                    # short (more lines remain beyond max_lines). Previously
                    # this was always True whenever max_lines was requested,
                    # showing a false "truncated" banner for tiny files.
                    truncated = not hit_eof
                else:
                    raw_bytes = f.read()
                    truncated = False

            _, extension = os.path.splitext(file_path)
            normalized_extension = extension.lower().lstrip('.')

            if normalized_extension in {'gz', 'gzip'}:
                try:
                    raw_bytes = gzip.decompress(raw_bytes)
                except OSError:
                    logger.error(
                        "Failed to decompress uploaded file %s for user %s.",
                        file_id,
                        user_id,
                        exc_info=True,
                    )
                    return jsonify({'status': 'error', 'message': 'Unable to decompress the uploaded file.'}), 500

                base_name = os.path.splitext(os.path.basename(file_path))[0]
                _, secondary_ext = os.path.splitext(base_name)
                normalized_extension = secondary_ext.lower().lstrip('.') or 'pdb'

            try:
                decoded_content = raw_bytes.decode('utf-8')
                encoding_used = 'utf-8'
            except UnicodeDecodeError:
                decoded_content = raw_bytes.decode('latin-1')
                encoding_used = 'latin-1'

            return jsonify({
                'status': 'success',
                'content': decoded_content,
                'format': normalized_extension or None,
                'encoding': encoding_used,
                'size': file_stat_size,
                'truncated': truncated,
            })
        except FileNotFoundError:
            return jsonify({'status': 'error', 'message': 'File is missing from the server.'}), 404
        except Exception:
            logger.error(f"Error reading file {file_id} for user {user_id}.", exc_info=True)
            return jsonify({'status': 'error', 'message': 'An internal error occurred while reading the file.'}), 500

    if request.method == 'PUT':
        try:
            new_content = request.json.get('content', '')
            new_content_bytes = new_content.encode('utf-8')
            new_size = len(new_content_bytes)

            # Get old file size from database
            old_size = file_info['size'] or 0
            size_difference = new_size - old_size

            # If file is getting larger, check storage quota
            if size_difference > 0:
                storage_state = get_user_storage_state(db, file_info['creator_id'])
                if size_difference > storage_state['available']:
                    return jsonify({
                        'status': 'error',
                        'message': f'Insufficient storage. Need {format_storage_bytes(size_difference)} more space.'
                    }), 507

            # Write the new content
            with open(file_path, 'wb') as f:
                f.write(new_content_bytes)

            # Update file size in database and user storage
            db.execute('BEGIN')
            try:
                db.execute('UPDATE files SET size = %s WHERE id = %s', (new_size, file_id))
                db.execute('UPDATE users SET storage_used = GREATEST(0, storage_used + %s) WHERE id = %s',
                          (size_difference, file_info['creator_id']))
                db.commit()

                # Track storage change event
                if size_difference != 0:
                    try:
                        track_event(
                            user_id=user_id,
                            event_type='file_edited',
                            storage_delta=size_difference,
                            project_id=file_info['project_id'],
                            metadata={
                                'file_id': file_id,
                                'old_size': old_size,
                                'new_size': new_size
                            }
                        )
                    except Exception as e:
                        logger.warning(f"Failed to track file edit event: {e}")

                # Notify user of storage change
                socketio.emit('user_info_update', room=f"user_{file_info['creator_id']}")

            except Exception:
                db.rollback()
                raise

            return jsonify({
                'status': 'success',
                'message': 'File saved successfully.',
                'new_size': new_size,
                'size_change': size_difference
            })
        except Exception:
            logger.error(f"Error writing to file {file_id} for user {user_id}.", exc_info=True)
            return jsonify({'status': 'error', 'message': 'An internal error occurred while saving the file.'}), 500

@app.route('/api/files/bulk-content', methods=['POST'])
@login_required
def bulk_file_content():
    """Return text content for multiple files in a single request."""
    try:
        db = get_db()
        user_id = session['user_id']
        data = request.get_json(silent=True) or {}
        file_ids = data.get('file_ids', [])

        if not isinstance(file_ids, list) or len(file_ids) == 0:
            return jsonify({'status': 'error', 'message': 'file_ids must be a non-empty list'}), 400
        if len(file_ids) > 500:
            return jsonify({'status': 'error', 'message': 'Maximum 500 files per request'}), 400

        placeholders = ','.join(['%s'] * len(file_ids))
        rows = db.execute(
            f'SELECT id, path, project_id FROM files WHERE id IN ({placeholders})',
            tuple(file_ids)
        ).fetchall()

        # Check user has access to each project (one lookup per distinct project)
        project_ids = list({r['project_id'] for r in rows})
        allowed_projects = set()
        for pid in project_ids:
            if get_project_assignment(db, user_id, pid):
                allowed_projects.add(pid)

        MAX_BYTES = 50 * 1024 * 1024
        results = []
        for row in rows:
            fid = row['id']
            if row['project_id'] not in allowed_projects:
                results.append({'id': fid, 'error': 'forbidden'})
                continue

            file_path = os.path.abspath(row['path'])
            try:
                if os.path.getsize(file_path) > MAX_BYTES:
                    results.append({'id': fid, 'error': 'too_large'})
                    continue
                with open(file_path, 'rb') as f:
                    raw = f.read()
                _, ext = os.path.splitext(file_path)
                norm_ext = ext.lower().lstrip('.')
                if norm_ext in ('gz', 'gzip'):
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        results.append({'id': fid, 'error': 'decompress_failed'})
                        continue
                    base = os.path.splitext(os.path.basename(file_path))[0]
                    _, sec_ext = os.path.splitext(base)
                    norm_ext = sec_ext.lower().lstrip('.') or 'pdb'
                try:
                    content = raw.decode('utf-8')
                except UnicodeDecodeError:
                    content = raw.decode('latin-1')
                results.append({'id': fid, 'content': content, 'format': norm_ext})
            except FileNotFoundError:
                results.append({'id': fid, 'error': 'not_found'})
            except Exception:
                results.append({'id': fid, 'error': 'read_failed'})

        return jsonify({'status': 'success', 'files': results})
    except Exception as e:
        logger.error('bulk_file_content error: %s', e, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Internal error reading files'}), 500


@app.route('/api/files/<int:file_id>', methods=['DELETE'])
@login_required
@csrf_protect
@rate_limit(limit=20, window=3600, action='file_delete')
def delete_file(file_id):
    db = get_db()
    user_id = session['user_id']
    file_info = db.execute('SELECT path, size, project_id, creator_id FROM files WHERE id = %s', (file_id,)).fetchone()
    if not file_info: return jsonify({'status': 'error', 'message': 'File not found or access denied.'}), 404

    role = get_project_assignment(db, user_id, file_info['project_id'])
    if not role or role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to delete this file.'}), 403

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (file_info['project_id'],)).fetchone()
    if not project: return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    project_path = get_safe_project_path(file_info['project_id'], project['team_id'])
    if not project_path or not is_path_within(file_info['path'], project_path):
        return jsonify({'status': 'error', 'message': 'Forbidden file deletion.'}), 403

    # Get all members before deletion to notify them after.
    members = db.execute('SELECT user_id FROM project_assignments WHERE project_id = %s', (file_info['project_id'],)).fetchall()

    file_path_on_disk = file_info['path']
    db.execute('BEGIN')
    try:
        # Deduct storage from the creator's quota and delete DB record first
        db.execute('UPDATE users SET storage_used = GREATEST(0, storage_used - %s) WHERE id = %s', (file_info['size'], file_info['creator_id']))
        db.execute('DELETE FROM files WHERE id = %s', (file_id,))

        # Track file deletion event
        try:
            track_event(
                user_id=user_id,
                event_type='file_deleted',
                storage_delta=-file_info['size'],  # Negative for deletion
                project_id=file_info['project_id'],
                metadata={
                    'file_id': file_id,
                    'file_size': file_info['size']
                }
            )
        except Exception as e:
            logger.warning(f"Failed to track file deletion event: {e}")

        db.commit()

        # Delete physical file AFTER DB commit succeeds (worst case: orphaned file on disk)
        try:
            if os.path.exists(file_path_on_disk):
                os.remove(file_path_on_disk)
        except OSError as e:
            logger.warning(f"File {file_id} deleted from DB but physical removal failed: {e}")

        # After successful deletion, emit events to update the UI for all members
        for member in members:
            socketio.emit('file_deleted', {'file_id': file_id, 'project_id': file_info['project_id']}, room=f"user_{member['user_id']}")
            socketio.emit('user_info_update', room=f"user_{member['user_id']}")

        return jsonify({'status': 'success', 'message': 'File deleted successfully.'})
    except Exception:
        db.rollback()
        logger.error(f"Failed to delete file {file_id} for user {user_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while deleting the file.'}), 500

@app.route('/api/available_models', methods=['GET'])
@login_required
def get_available_models():
    try:
        db = get_db()
        overrides = fetch_model_overrides(db)
        models = apply_overrides_to_models(Config.SUPPORTED_MODELS, overrides)
        hidden_model_ids = set(getattr(Config, 'UI_HIDDEN_MODEL_IDS', set()))
        greyed_model_ids = set(getattr(Config, 'UI_GREYED_MODEL_IDS', set()))
        models = [
            model for model in models
            if model.get('is_visible', True) and model.get('id') not in hidden_model_ids
        ]
        # Mark greyed-out models so the UI can render them as disabled
        for model in models:
            if model.get('id') in greyed_model_ids:
                model['available'] = False
                model['is_active'] = False
                model['greyed_out'] = True

        models = apply_runtime_status_to_models(models)
        models = [Config.public_model_view(model) for model in models]

        def _model_sort_key(model: dict) -> tuple[int, str]:
            node_id = model.get('node_id', '') or ''
            try:
                numeric = int(node_id.split('-')[-1])
            except (ValueError, TypeError):
                numeric = 99
            return numeric, model.get('name', '')

        models.sort(key=_model_sort_key)
        return jsonify(models)
    except Exception as e:
        logger.error(f"Error fetching available models: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch available models.'}), 500

@app.route('/full_html')
def full_html():
    """Redirect to dashboard (used by Stripe after payment)."""
    return redirect(url_for('dashboard'))

@app.route('/download_file/<int:file_id>')
@login_required
def download_file(file_id):
    try:
        db = get_db()
        user_id = session['user_id']
        file_info = db.execute('SELECT path, name, project_id, creator_id FROM files WHERE id = %s', (file_id,)).fetchone()
        if not file_info:
            logger.warning(f"File record not found in DB for file_id={file_id}, user={user_id}")
            return "File not found.", 404
        if not file_info['path']:
            logger.warning(f"File record has no path for file_id={file_id}, name={file_info.get('name')}, user={user_id}")
            return "File path is missing.", 404

        role = get_project_assignment(db, user_id, file_info['project_id'])
        logger.info(f"File download permission check - User: {user_id}, Role: {role}, Creator: {file_info.get('creator_id')}, File: {file_id}")
        if not role and file_info.get('creator_id') != user_id:
            logger.warning(f"Permission denied for user {user_id} to download file {file_id}")
            return "You do not have permission to download this file.", 403

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (file_info['project_id'],)).fetchone()
        if not project:
            logger.warning(f"Project not found for project_id={file_info['project_id']}, file_id={file_id}")
            return "Project not found.", 404

        project_path = get_safe_project_path(file_info['project_id'], project['team_id'])
        
        # Resolve file path
        file_path = os.path.abspath(file_info['path'])
        
        # Path validation using dynamic allowlist from disk manager
        allowed_prefixes = _get_allowed_path_prefixes(project_path)

        target_path = os.path.abspath(file_path)
        target_real_path = os.path.realpath(file_path)
        is_safe = is_path_within_any(target_path, allowed_prefixes) and is_path_within_any(target_real_path, allowed_prefixes)
        
        if not is_safe:
            logger.warning(f"Access forbidden - file path outside allowed prefixes. File: {file_path}, Allowed: {allowed_prefixes}")
            return "Access to file path is forbidden.", 403

        if not os.path.exists(file_path):
            logger.warning(f"File not found on disk: {file_path}, file_id={file_id}")
            return "File not found on disk.", 404

        # Check if this is a view request (for 3D viewer) or download request
        view_mode = request.args.get('view', 'false').lower() == 'true'

        if view_mode:
             return send_from_directory(
                directory=os.path.dirname(file_path),
                path=os.path.basename(file_path),
                as_attachment=False
            )
        else:
            return send_from_directory(directory=os.path.dirname(file_path), path=os.path.basename(file_path), as_attachment=True, download_name=file_info['name'])
    except Exception as e:
        logger.error(f"Error downloading file {file_id}: {e}", exc_info=True)
        return "An error occurred while downloading the file.", 500


def _ligand_text_to_smiles_blocking(text: str, ext: str = ''):
    """Pure RDKit parsing for SDF/MOL/MOL2 -> canonical SMILES.

    Runs entirely on plain string args with no Flask/DB/global access so it is
    safe to dispatch through ``_run_blocking`` (a real OS thread under eventlet).
    Returns (smiles, error_message). RDKit is imported lazily so a missing/broken
    install can't break module import (mirrors the openbabel lazy-import lesson).
    """
    if not text or not text.strip():
        return None, 'Ligand file is empty'
    try:
        from rdkit import Chem
        from rdkit import RDLogger
        RDLogger.DisableLog('rdApp.*')  # silence parse warnings on the server log
    except Exception as e:  # pragma: no cover - depends on host install
        logger.error(f"RDKit unavailable for SDF->SMILES conversion: {e}")
        return None, 'SMILES conversion is unavailable on the server (RDKit missing)'

    ext = (ext or '').lower().lstrip('.')
    mol = None
    try:
        if ext == 'mol2' or ('@<TRIPOS>' in text[:2000]):
            mol = Chem.MolFromMol2Block(text, sanitize=True, removeHs=False)
        else:
            # SDF (possibly multi-record) or single MOL block. Take the first
            # molecule that parses. Fall back to a forgiving (no-sanitize) parse.
            block = text.split('$$$$')[0]
            mol = Chem.MolFromMolBlock(block, sanitize=True, removeHs=False)
            if mol is None:
                mol = Chem.MolFromMolBlock(block, sanitize=False, removeHs=False)
    except Exception as e:
        logger.warning(f"RDKit parse error during SDF->SMILES: {e}")
        mol = None

    if mol is None:
        return None, 'Could not parse a molecule from the ligand file'
    try:
        smiles = Chem.MolToSmiles(mol)
    except Exception as e:
        logger.warning(f"RDKit MolToSmiles failed: {e}")
        return None, 'Could not derive SMILES from the molecule'
    if not smiles or not smiles.strip():
        return None, 'Conversion produced an empty SMILES string'
    return smiles.strip(), None


def _ligand_text_to_smiles(text: str, ext: str = ''):
    """Convert an SDF / MOL / MOL2 ligand block to a canonical SMILES string.

    The CPU-bound RDKit parsing runs in a real OS thread (via ``_run_blocking``)
    so it does not block the single eventlet worker's hub. Returns
    (smiles, error_message).
    """
    return _run_blocking(_ligand_text_to_smiles_blocking, text, ext)


def _cif_to_pdb_blocking(cif_data: str):
    """Pure Biopython CIF-string -> PDB-string conversion.

    Operates only on the in-memory CIF string with no Flask/DB/global access so
    it is safe to dispatch through ``_run_blocking`` (a real OS thread under
    eventlet). Returns (pdb_content, error_message); pdb_content is None on
    failure so the caller can fall back to the raw CIF.
    """
    try:
        from io import StringIO
        from Bio.PDB import MMCIFParser, PDBIO

        cif_file = StringIO(cif_data)
        parser = MMCIFParser(QUIET=True)
        structure = parser.get_structure('boltz2', cif_file)

        pdb_io = PDBIO()
        pdb_io.set_structure(structure)

        pdb_output = StringIO()
        pdb_io.save(pdb_output)
        return pdb_output.getvalue(), None
    except Exception as conv_error:
        return None, str(conv_error)


@app.route('/api/ligand/sdf-to-smiles', methods=['POST'])
@login_required
@csrf_protect
def ligand_sdf_to_smiles():
    """Convert a project SDF/MOL/MOL2 ligand file (by file_id) — or raw posted
    ligand text — into a SMILES string usable by OpenFold3 / Boltz2 ligand inputs.

    Body: {"file_id": <int>}  OR  {"sdf": "<text>", "name": "lig.sdf"}
    Returns: {"success": true, "smiles": "...", "name": "..."}
    """
    try:
        data = request.get_json(silent=True) or {}
        file_id = data.get('file_id')
        raw_text = data.get('sdf') or data.get('text')
        name = data.get('name') or ''

        text = None
        if file_id is not None:
            db = get_db()
            user_id = session['user_id']
            try:
                file_id_int = int(file_id)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Invalid file id'}), 400

            file_info = db.execute(
                'SELECT path, name, project_id, creator_id FROM files WHERE id = %s',
                (file_id_int,)
            ).fetchone()
            if not file_info or not file_info['path']:
                return jsonify({'success': False, 'error': 'File not found'}), 404

            role = get_project_assignment(db, user_id, file_info['project_id'])
            if not role and file_info.get('creator_id') != user_id:
                return jsonify({'success': False, 'error': 'Permission denied'}), 403

            project = db.execute('SELECT team_id FROM projects WHERE id = %s', (file_info['project_id'],)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404

            project_path = get_safe_project_path(file_info['project_id'], project['team_id'])
            file_path = os.path.abspath(file_info['path'])
            allowed_prefixes = _get_allowed_path_prefixes(project_path)
            if not (is_path_within_any(file_path, allowed_prefixes)
                    and is_path_within_any(os.path.realpath(file_path), allowed_prefixes)):
                return jsonify({'success': False, 'error': 'Access to file path is forbidden'}), 403
            if not os.path.exists(file_path):
                return jsonify({'success': False, 'error': 'File not found on disk'}), 404

            with open(file_path, 'r', errors='replace') as fh:
                text = fh.read()
            name = name or file_info['name']
        elif raw_text:
            text = raw_text
        else:
            return jsonify({'success': False, 'error': 'Provide a file_id or sdf text'}), 400

        ext = os.path.splitext(name)[1] if name else ''
        smiles, err = _ligand_text_to_smiles(text, ext)
        if err:
            return jsonify({'success': False, 'error': err}), 422
        return jsonify({'success': True, 'smiles': smiles, 'name': name})
    except Exception as e:
        logger.error(f"Error converting ligand to SMILES: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Conversion failed'}), 500

@app.route('/download_output/<job_id>')
@login_required
def download_output(job_id):
    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute(
            "SELECT output_file, output_display_name, project_id, creator_id, model, "
            "COALESCE(policy, metadata, '{}') AS policy_blob "
            "FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()

        if not job:
            return "Job not found.", 404

        # Allow access if user has project assignment OR is the job creator
        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job.get('creator_id') != user_id:
            logger.warning(f"Permission denied for user {user_id} to download job {job_id}")
            return "You do not have permission to download this file.", 403

        # Special handling for Boltz2: Extract CIF from JSON and convert to PDB
        if job.get('model') in ['type-9', 'boltz2', 'type-boltz2']:
            stored_path = job['output_file']
            if stored_path and os.path.exists(stored_path) and stored_path.endswith('.cif.json'):
                try:
                    with open(stored_path, 'r') as f:
                        boltz2_data = json.load(f)

                    if boltz2_data.get('structures') and len(boltz2_data['structures']) > 0:
                        cif_data = boltz2_data['structures'][0].get('structure', '')
                        if cif_data:
                            # Convert CIF to PDB format using BioPython. The
                            # CPU-bound parse runs in a real OS thread so it does
                            # not block the single eventlet worker's hub.
                            pdb_content, conv_error = _run_blocking(_cif_to_pdb_blocking, cif_data)
                            if conv_error is None and pdb_content:
                                # Return PDB data as downloadable file
                                download_name = f"{job_id}.pdb"
                                return Response(
                                    pdb_content,
                                    mimetype='chemical/x-pdb',
                                    headers={'Content-Disposition': f'attachment; filename={download_name}'}
                                )
                            else:
                                logger.error(f"Error converting CIF to PDB: {conv_error}")
                                # Fallback: return raw CIF data
                                download_name = f"{job_id}.cif"
                                return Response(
                                    cif_data,
                                    mimetype='chemical/x-cif',
                                    headers={'Content-Disposition': f'attachment; filename={download_name}'}
                                )
                except Exception as e:
                    logger.error(f"Error extracting Boltz2 structure data: {e}")
                    # Fall through to normal download if extraction fails

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if not project:
            return "Project not found.", 404

        project_path = get_safe_project_path(job['project_id'], project['team_id'])
        job_policy = _safe_policy_dict(job.get('policy_blob'))
        
        # Resolve the file path
        file_path = None
        stored_path = job['output_file']
        
        # 1. Try stored path if it exists
        if stored_path and os.path.exists(stored_path):
            file_path = os.path.abspath(stored_path)
        
        # 2. Try constructing path from project_path if stored path fails
        if not file_path and project_path and stored_path:
            # Try to recover filename from stored path
            filename = os.path.basename(stored_path)
            branch_dir = resolve_job_branch_path(
                project_path=project_path,
                job_id=job_id,
                output_file=stored_path,
                policy=job_policy,
                branch='main',
                create=False,
            )
            candidate = os.path.join(branch_dir, filename) if branch_dir else None
            if candidate and os.path.exists(candidate):
                file_path = os.path.abspath(candidate)
            # FALLBACK: Check if file is directly in project folder (legacy/flat structure)
            if not file_path:
                candidate_flat = os.path.join(project_path, filename)
                if os.path.exists(candidate_flat):
                    file_path = os.path.abspath(candidate_flat)

        # 3. Special case: Allow /tmp/msa_search_results for MSA jobs (non-project paths)
        if not file_path or not os.path.exists(file_path):
            if stored_path and stored_path.startswith('/tmp/') and os.path.exists(stored_path):
                file_path = os.path.abspath(stored_path)

        # 4. MSA fallback resolution: recover moved/stale A3M paths by scanning known locations.
        if (not file_path or not os.path.exists(file_path)) and job.get('model') == 'msa-search':
            msa_candidates: list[str] = []
            filename_hint = os.path.basename(stored_path) if stored_path else ''

            if project_path:
                for job_dir_candidate in list_job_artifact_dirs(
                    project_path=project_path,
                    job_id=job_id,
                    output_file=stored_path,
                    policy=job_policy,
                ):
                    if filename_hint:
                        msa_candidates.append(os.path.join(job_dir_candidate, filename_hint))
                    try:
                        for name in os.listdir(job_dir_candidate):
                            if name.lower().endswith('.a3m'):
                                msa_candidates.append(os.path.join(job_dir_candidate, name))
                    except OSError:
                        pass

            if filename_hint:
                repo_job_outputs = os.path.abspath(os.path.join(os.getcwd(), 'job_outputs'))
                msa_candidates.append(os.path.join(repo_job_outputs, job_id, filename_hint))

            msa_tmp_dir = '/tmp/msa_search_results'
            if os.path.isdir(msa_tmp_dir):
                if filename_hint:
                    msa_candidates.append(os.path.join(msa_tmp_dir, filename_hint))
                try:
                    for name in os.listdir(msa_tmp_dir):
                        if name.startswith(job_id) and name.lower().endswith('.a3m'):
                            msa_candidates.append(os.path.join(msa_tmp_dir, name))
                except OSError:
                    pass

            existing_candidates = []
            for candidate in msa_candidates:
                if candidate and os.path.exists(candidate):
                    try:
                        existing_candidates.append((os.path.getmtime(candidate), candidate))
                    except OSError:
                        continue

            if existing_candidates:
                existing_candidates.sort(reverse=True)
                file_path = os.path.abspath(existing_candidates[0][1])
                logger.info(f"[MSA] Recovered download path for {job_id}: {file_path}")

        if not file_path or not os.path.exists(file_path):
             return "File not found.", 404

        # Path validation using dynamic allowlist from disk manager
        allowed_prefixes = _get_allowed_path_prefixes(project_path)
        if job.get('model') == 'msa-search':
            msa_tmp_dir = '/tmp/msa_search_results'
            allowed_prefixes.append(os.path.abspath(msa_tmp_dir))
            allowed_prefixes.append(os.path.realpath(msa_tmp_dir))

        target_path = os.path.abspath(file_path)
        target_real_path = os.path.realpath(file_path)
        is_safe = is_path_within_any(target_path, allowed_prefixes) and is_path_within_any(target_real_path, allowed_prefixes)
        
        logger.info(f"Path validation - File: {file_path} (real: {target_real_path}), Safe: {is_safe}")
        
        if not is_safe:
            logger.warning(f"Path validation failed - File path: {file_path} (real: {target_real_path}) not in {allowed_prefixes}")
            return "Access denied to file path.", 403

        if job.get('model') == 'msa-search':
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as msa_file:
                    raw_msa_content = msa_file.read()

                extracted_alignment = None
                if raw_msa_content.lstrip().startswith('{'):
                    parsed_output = json.loads(raw_msa_content)
                    alignments = parsed_output.get('alignments')
                    if isinstance(alignments, dict):
                        for db_entry in alignments.values():
                            if not isinstance(db_entry, dict):
                                continue
                            a3m_entry = db_entry.get('a3m')
                            if isinstance(a3m_entry, dict):
                                candidate_alignment = a3m_entry.get('alignment')
                                if isinstance(candidate_alignment, str) and candidate_alignment.strip():
                                    extracted_alignment = candidate_alignment
                                    break

                if extracted_alignment:
                    download_name = job['output_display_name'] or f"{job_id}_msa_result.a3m"
                    return Response(
                        extracted_alignment,
                        mimetype='text/plain',
                        headers={'Content-Disposition': f'attachment; filename={download_name}'}
                    )
            except Exception as msa_download_err:
                logger.warning(f"[MSA] Could not extract alignment text for {job_id}: {msa_download_err}")

        download_name = job['output_display_name'] or os.path.basename(file_path)
        return send_from_directory(directory=os.path.dirname(file_path), path=os.path.basename(file_path), as_attachment=True, download_name=download_name)
    except Exception as e:
        logger.error(f"Error downloading job output {job_id}: {e}", exc_info=True)
        return "An error occurred while downloading the job output.", 500


@app.route('/download_output_raw/<job_id>')
@login_required
def download_output_raw(job_id):
    """Download raw job output (for Boltz2 JSON, etc.)"""
    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute('SELECT output_file, project_id, creator_id FROM jobs WHERE job_id = %s', (job_id,)).fetchone()

        if not job:
            return "Job not found.", 404

        # Allow access if user has project assignment OR is the job creator
        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job.get('creator_id') != user_id:
            return "You do not have permission to access this file.", 403

        stored_path = job['output_file']
        if not stored_path or not os.path.exists(stored_path):
            return "File not found.", 404

        # Path traversal guard — validate stored_path is under an allowed prefix
        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if project:
            project_path = get_safe_project_path(job['project_id'], project['team_id'])
            if project_path:
                allowed_prefixes = _get_allowed_path_prefixes(project_path)
                abs_path = os.path.abspath(stored_path)
                real_path = os.path.realpath(stored_path)
                if not (is_path_within_any(abs_path, allowed_prefixes) and is_path_within_any(real_path, allowed_prefixes)):
                    return "Access to file path is forbidden.", 403

        # Return the raw file content
        with open(stored_path, 'r') as f:
            content = f.read()

        return Response(content, mimetype='application/json')
    except Exception as e:
        logger.error(f"Error downloading raw job output {job_id}: {e}", exc_info=True)
        return "An error occurred.", 500


@app.route('/job/<job_id>/pae_matrix')
@login_required
def get_job_pae_matrix(job_id):
    """Serve the decompressed PAE matrix for a Boltz2 job.

    The worker stores the PAE matrix as a gzip-compressed artifact
    ({job_id}_pae.json.gz) so it never bloats the job result payload; this
    endpoint decompresses it server-side and returns {"pae_matrix": [...]}.
    """
    import glob
    import gzip as _gzip
    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute('SELECT output_file, project_id, creator_id FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
        if not job:
            return jsonify({'error': 'Job not found'}), 404

        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job.get('creator_id') != user_id:
            return jsonify({'error': 'Forbidden'}), 403

        # Locate the job output directory from the stored output file path.
        stored_path = job['output_file']
        job_output_dir = os.path.dirname(stored_path) if stored_path else None
        if not job_output_dir or not os.path.isdir(job_output_dir):
            return jsonify({'error': 'PAE matrix not found'}), 404

        # Path-traversal guard against the project's allowed prefixes.
        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if project:
            project_path = get_safe_project_path(job['project_id'], project['team_id'])
            if project_path:
                allowed_prefixes = _get_allowed_path_prefixes(project_path)
                real_dir = os.path.realpath(job_output_dir)
                if not is_path_within_any(real_dir, allowed_prefixes):
                    return jsonify({'error': 'Forbidden'}), 403

        matches = glob.glob(os.path.join(job_output_dir, '*_pae.json.gz'))
        if not matches:
            return jsonify({'error': 'PAE matrix not available for this job'}), 404

        with open(matches[0], 'rb') as f:
            raw = _gzip.decompress(f.read())
        return Response(raw, mimetype='application/json')
    except Exception as e:
        logger.error(f"Error serving PAE matrix for {job_id}: {e}", exc_info=True)
        return jsonify({'error': 'An error occurred'}), 500


@app.route('/download_all_job_artifacts/<string:job_id>')
@login_required
def download_all_job_artifacts(job_id):
    """Download all artifacts for a job as a ZIP file."""
    import zipfile
    from io import BytesIO

    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute(
            'SELECT project_id, COALESCE(policy, metadata, \'{}\') AS policy_blob, creator_id, name, output_file FROM jobs WHERE job_id = %s',
            (job_id,),
        ).fetchone()
        if not job:
            return "Job not found.", 404
        job = dict(job)

        # Allow access if user has project assignment OR is the job creator
        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job['creator_id'] != user_id:
            return "You do not have permission to download these artifacts.", 403

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if not project:
            return "Project not found.", 404

        project_path = get_safe_project_path(job['project_id'], project['team_id'])
        if not project_path:
            return "Project storage path unavailable.", 404

        metadata: dict[str, object] = {}
        if job.get('policy_blob'):
            try:
                metadata = json.loads(job['policy_blob'])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
        job_dir = _resolve_job_artifact_dir(project_path, job_id, job.get('output_file'), metadata)

        artifact_records = metadata.get('artifacts') or metadata.get('artifacts_info') or []

        # Create a ZIP file in memory
        memory_file = BytesIO()
        seen_filenames = set()
        used_arcnames: set[str] = set()

        def _zip_arcname(fname, record=None):
            """Individual, job-name-prefixed name for a file inside the archive
            so extracting multiple jobs' zips doesn't clobber rank1.sdf etc."""
            arcname = friendly_artifact_download_name(job.get('name'), job_id, fname, record)
            if arcname in used_arcnames:
                stem, ext = os.path.splitext(arcname)
                idx = 2
                while f"{stem}_{idx}{ext}" in used_arcnames:
                    idx += 1
                arcname = f"{stem}_{idx}{ext}"
            used_arcnames.add(arcname)
            return arcname

        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Add artifacts from metadata
            for record in artifact_records:
                filename = record.get('filename') or record.get('name')
                if not _is_safe_path_name(filename):
                    continue

                artifact_path = resolve_job_artifact_path(
                    project_path=project_path,
                    job_id=job_id,
                    artifact_name=filename,
                    output_file=job.get('output_file'),
                    policy=metadata,
                    preferred_branch=record.get('branch') if isinstance(record, dict) else 'main',
                ) or os.path.abspath(os.path.join(job_dir, filename))
                if not is_path_within_any(artifact_path, [job_dir, os.path.join(project_path, 'jobs', job_id)]):
                    logger.warning(f"Skipping artifact with invalid path: {filename}")
                    continue
                if not os.path.exists(artifact_path):
                    logger.warning(f"Artifact file missing: {filename}")
                    continue

                zf.write(artifact_path, _zip_arcname(filename, record if isinstance(record, dict) else None))
                seen_filenames.add(filename)

            # Fallback: scan job directory for files not already in metadata
            for scan_dir in list_job_artifact_dirs(
                project_path=project_path,
                job_id=job_id,
                output_file=job.get('output_file'),
                policy=metadata,
            ):
                allowed_exts = FILE_MANAGER_ARTIFACT_EXTENSIONS
                for fname in sorted(os.listdir(scan_dir)):
                    if fname in seen_filenames or fname.startswith('.'):
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in allowed_exts:
                        continue
                    fpath = os.path.abspath(os.path.join(scan_dir, fname))
                    if not os.path.isfile(fpath):
                        continue
                    if not is_path_within_any(fpath, [scan_dir, job_dir]):
                        continue
                    zf.write(fpath, _zip_arcname(fname))
                    seen_filenames.add(fname)

        if not seen_filenames:
            return "No artifacts found for this job.", 404

        memory_file.seek(0)

        # Generate a friendly zip filename
        job_name = job.get('name') or job_id
        zip_filename = f"{job_name}_artifacts.zip"

        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=zip_filename
        )
    except Exception as e:
        logger.error(f"Error downloading all artifacts for job {job_id}: {e}", exc_info=True)
        return "An error occurred while creating the ZIP file.", 500


@app.route('/download_job_artifact/<string:job_id>/<string:artifact_name>')
@login_required
def download_job_artifact(job_id, artifact_name):
    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute(
            'SELECT project_id, name, COALESCE(policy, metadata, \'{}\') AS policy_blob, creator_id, output_file FROM jobs WHERE job_id = %s',
            (job_id,),
        ).fetchone()
        if not job:
            logger.error(f"[download_job_artifact] Job {job_id} not found")
            return "Job not found.", 404
        job = dict(job)

        # Allow access if user has project assignment OR is the job creator
        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job['creator_id'] != user_id:
            return "You do not have permission to download this artifact.", 403

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if not project:
            logger.error(f"[download_job_artifact] Project {job['project_id']} not found")
            return "Project not found.", 404

        project_path = get_safe_project_path(job['project_id'], project['team_id'])
        logger.info(f"[download_job_artifact] project_path={project_path}, team_id={project['team_id']}")
        if not project_path:
            logger.error(f"[download_job_artifact] No project path for project {job['project_id']}")
            return "Project storage path unavailable.", 404

        metadata: dict[str, object] = {}
        if job.get('policy_blob'):
            try:
                metadata = json.loads(job['policy_blob'])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
        job_dir = _resolve_job_artifact_dir(project_path, job_id, job.get('output_file'), metadata)
        logger.info(f"[download_job_artifact] job_dir={job_dir}, artifact={artifact_name}")

        artifact_records = metadata.get('artifacts') or metadata.get('artifacts_info') or []
        selected = None
        for record in artifact_records:
            if record.get('name') == artifact_name or record.get('filename') == artifact_name:
                selected = record
                break

        filename = artifact_name
        
        # If not found in metadata, check if it's a safe dynamic artifact (e.g. rank*.sdf)
        if not selected:
            # Allow common safe extensions for dynamic artifacts
            lowered_name = artifact_name.lower()
            allow_suffixes = tuple(FILE_MANAGER_ARTIFACT_EXTENSIONS) + ('.tar.gz', '.tar.bz2', '.tar.xz')
            if not lowered_name.endswith(allow_suffixes):
                return "Artifact not found or type not allowed.", 404
            
            # Basic path validation for the requested filename
            if not _is_safe_path_name(artifact_name):
                 return "Invalid filename.", 400
        else:
            filename = selected.get('filename') or selected.get('name') or artifact_name

        if not filename:
            return "Artifact filename missing.", 404

        # Individual, descriptive download name (job-name prefix + pose/model number)
        # so files from different jobs don't collide as generic rank1.sdf / ranked_0.pdb.
        download_name = friendly_artifact_download_name(
            job.get('name'), job_id, filename, selected if isinstance(selected, dict) else None
        )

        artifact_path = resolve_job_artifact_path(
            project_path=project_path,
            job_id=job_id,
            artifact_name=filename,
            output_file=job.get('output_file'),
            policy=metadata,
            preferred_branch=(selected or {}).get('branch') if isinstance(selected, dict) else 'main',
        )
        if not artifact_path:
            artifact_path = os.path.abspath(os.path.join(job_dir, filename))
        logger.info(f"[download_job_artifact] artifact_path={artifact_path}, exists={os.path.exists(artifact_path)}")
        if not is_path_within_any(artifact_path, [job_dir, os.path.join(project_path, 'jobs', job_id)]):
            return "Access to artifact is forbidden.", 403
        if not os.path.exists(artifact_path):
            fallback_bytes = _artifact_bytes_from_policy(metadata, filename)
            if fallback_bytes is not None:
                logger.warning(
                    "[download_job_artifact] Serving %s for job %s from metadata fallback because the file is missing on disk.",
                    filename,
                    job_id,
                )
                guessed_mimetype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                return send_file(
                    io.BytesIO(fallback_bytes),
                    mimetype=guessed_mimetype,
                    as_attachment=True,
                    download_name=download_name,
                )

            logger.error(f"[download_job_artifact] File not found: {artifact_path}")
            # List what files ARE in the directory
            if os.path.isdir(job_dir):
                files_in_dir = os.listdir(job_dir)
                logger.error(f"[download_job_artifact] Files in {job_dir}: {files_in_dir}")
            else:
                logger.error(f"[download_job_artifact] job_dir does not exist: {job_dir}")
            return "Artifact file is missing.", 404

        # Use a job-name-prefixed download name so files from different jobs
        # are individually named (e.g. "MyDockRun_pose_1.sdf") instead of all
        # arriving as generic rank1.sdf / ranked_0.pdb.
        return send_from_directory(
            directory=os.path.dirname(artifact_path),
            path=os.path.basename(artifact_path),
            as_attachment=True,
            download_name=download_name,
        )
    except Exception as e:
        logger.error(f"Error downloading artifact {artifact_name} for job {job_id}: {e}", exc_info=True)
        return "An error occurred while downloading the artifact.", 500

@app.route('/api/jobs/<string:job_id>/artifacts/<string:artifact_name>/content')
@login_required
@csrf_protect
def get_job_artifact_content(job_id, artifact_name):
    """Return the text content of a job artifact file (analogous to /api/files/<id>/content)."""
    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute(
            'SELECT project_id, COALESCE(policy, metadata, \'{}\') AS policy_blob, creator_id, output_file FROM jobs WHERE job_id = %s',
            (job_id,),
        ).fetchone()
        if not job:
            return jsonify({'status': 'error', 'message': 'Job not found.'}), 404
        job = dict(job)

        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job['creator_id'] != user_id:
            return jsonify({'status': 'error', 'message': 'You do not have permission to access this artifact.'}), 403

        # Validate artifact name
        if not _is_safe_path_name(artifact_name):
            return jsonify({'status': 'error', 'message': 'Invalid artifact name.'}), 400

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if not project:
            return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

        project_path = get_safe_project_path(job['project_id'], project['team_id'])
        if not project_path:
            return jsonify({'status': 'error', 'message': 'Project storage path unavailable.'}), 404

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

        # Path traversal guard
        if not is_path_within_any(artifact_path, [job_dir, job_root]):
            return jsonify({'status': 'error', 'message': 'Access to artifact is forbidden.'}), 403

        # Guard against reading very large files into memory
        MAX_CONTENT_READ_BYTES = 50 * 1024 * 1024  # 50 MB
        raw_bytes: Optional[bytes] = None
        source_name = artifact_path
        artifact_exists = os.path.exists(artifact_path)

        if artifact_exists:
            try:
                artifact_stat_size = os.path.getsize(artifact_path)
            except OSError:
                artifact_stat_size = 0
            if artifact_stat_size > MAX_CONTENT_READ_BYTES:
                return jsonify({'status': 'error', 'message': f'Artifact too large to preview ({artifact_stat_size // (1024*1024)} MB). Please download instead.'}), 413

            with open(artifact_path, 'rb') as f:
                raw_bytes = f.read()
        else:
            raw_bytes = _artifact_bytes_from_policy(policy, artifact_name)
            source_name = artifact_name
            if raw_bytes is None:
                return jsonify({'status': 'error', 'message': 'Artifact file is missing.'}), 404
            if len(raw_bytes) > MAX_CONTENT_READ_BYTES:
                return jsonify({'status': 'error', 'message': f'Artifact too large to preview ({len(raw_bytes) // (1024*1024)} MB). Please download instead.'}), 413
            logger.warning(
                "Serving artifact '%s' for job %s from metadata fallback because file is missing on disk.",
                artifact_name,
                job_id,
            )

        _, extension = os.path.splitext(source_name)
        normalized_extension = extension.lower().lstrip('.')

        if normalized_extension in {'gz', 'gzip'}:
            try:
                raw_bytes = gzip.decompress(raw_bytes)
            except OSError:
                return jsonify({'status': 'error', 'message': 'Unable to decompress artifact.'}), 500
            base_name = os.path.splitext(os.path.basename(source_name))[0]
            _, secondary_ext = os.path.splitext(base_name)
            normalized_extension = secondary_ext.lower().lstrip('.') or 'txt'

        try:
            decoded_content = raw_bytes.decode('utf-8')
            encoding_used = 'utf-8'
        except UnicodeDecodeError:
            decoded_content = raw_bytes.decode('latin-1')
            encoding_used = 'latin-1'

        return jsonify({
            'status': 'success',
            'content': decoded_content,
            'format': normalized_extension or None,
            'encoding': encoding_used,
            'size': len(raw_bytes),
        })
    except Exception:
        logger.error(f"Error reading artifact {artifact_name} for job {job_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while reading the artifact.'}), 500


@app.route('/api/jobs/<string:job_id>/artifacts')
@login_required
def list_job_artifacts(job_id):
    """
    List all artifacts (output files) for a job.
    Returns both registered artifacts from metadata and any SDF files in the job directory.
    Useful for DiffDock jobs that produce multiple SDF files.
    """
    try:
        db = get_db()
        user_id = session['user_id']
        job = db.execute(
            'SELECT project_id, COALESCE(policy, metadata, \'{}\') AS policy_blob, creator_id, model, name, output_file FROM jobs WHERE job_id = %s',
            (job_id,),
        ).fetchone()
        if not job:
            return jsonify({'status': 'error', 'message': 'Job not found'}), 404
        job = dict(job)

        # Check permissions
        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job['creator_id'] != user_id:
            return jsonify({'status': 'error', 'message': 'Permission denied'}), 403

        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        if not project:
            return jsonify({'status': 'error', 'message': 'Project not found'}), 404

        project_path = get_safe_project_path(job['project_id'], project['team_id'])
        if not project_path:
            return jsonify({'status': 'error', 'message': 'Project storage unavailable'}), 404

        # Parse job metadata
        metadata: dict[str, object] = {}
        if job.get('policy_blob'):
            try:
                metadata = json.loads(job['policy_blob'])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
        job_dir = _resolve_job_artifact_dir(project_path, job_id, job.get('output_file'), metadata)

        artifacts = []
        seen_files = set()

        # 1. Add registered artifacts from metadata
        artifact_records = metadata.get('artifacts') or []
        for record in artifact_records:
            filename = record.get('filename') or record.get('name')
            if not filename:
                continue

            artifact_path = resolve_job_artifact_path(
                project_path=project_path,
                job_id=job_id,
                artifact_name=filename,
                output_file=job.get('output_file'),
                policy=metadata,
                preferred_branch=record.get('branch') if isinstance(record, dict) else 'main',
            ) or os.path.join(job_dir, filename)
            if os.path.exists(artifact_path):
                ext = os.path.splitext(filename)[1].lower()
                file_size = os.path.getsize(artifact_path)
                artifacts.append({
                    'filename': filename,
                    'type': record.get('type', 'output'),
                    'size': file_size,
                    'format': ext.lstrip('.'),
                    'display_name': record.get('display_name', filename)
                })
                seen_files.add(filename)

        # 2. Scan for additional molecular files (especially SDF files from DiffDock)
        molecular_extensions = {'.sdf', '.pdb', '.mol', '.mol2', '.cif', '.mmcif', '.xyz'}
        for scan_dir in list_job_artifact_dirs(
            project_path=project_path,
            job_id=job_id,
            output_file=job.get('output_file'),
            policy=metadata,
        ):
            try:
                for file in os.listdir(scan_dir):
                    if file in seen_files:
                        continue

                    ext = os.path.splitext(file)[1].lower()
                    if ext in molecular_extensions:
                        file_path = os.path.join(scan_dir, file)
                        if os.path.isfile(file_path):
                            file_size = os.path.getsize(file_path)
                            artifacts.append({
                                'filename': file,
                                'type': 'output',
                                'size': file_size,
                                'format': ext.lstrip('.'),
                                'display_name': file
                            })
                            seen_files.add(file)
            except OSError as e:
                logger.warning(f"Error scanning job directory {scan_dir}: {e}")

        # Sort artifacts (SDF files by rank if present, then alphabetically)
        def artifact_sort_key(artifact):
            filename = artifact['filename']
            # Extract rank number for DiffDock output files (e.g., rank_1.sdf -> 1)
            if 'rank_' in filename:
                try:
                    rank_str = filename.split('rank_')[1].split('.')[0]
                    return (0, int(rank_str))  # (priority, rank_number)
                except (IndexError, ValueError):
                    pass
            return (1, filename.lower())  # (priority, filename)

        artifacts.sort(key=artifact_sort_key)

        return jsonify({
            'status': 'success',
            'job_id': job_id,
            'job_name': job.get('name'),
            'model_id': job.get('model'),
            'artifacts': artifacts,
            'count': len(artifacts)
        })

    except Exception as e:
        logger.error(f"Error listing artifacts for job {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to list artifacts'}), 500

@app.route('/download_attachment/<int:attachment_id>')
@login_required
def download_attachment(attachment_id):
    try:
        db = get_db()
        user_id = session['user_id']
        attachment = db.execute('SELECT * FROM help_ticket_attachments WHERE id = %s', (attachment_id,)).fetchone()
        if not attachment:
            return "Attachment not found.", 404

        ticket = db.execute('SELECT user_id FROM help_tickets WHERE id = %s', (attachment['ticket_id'],)).fetchone()
        if not ticket:
            return "Ticket not found.", 404

        user = db.execute('SELECT is_admin FROM users WHERE id = %s', (user_id,)).fetchone()
        if not user['is_admin'] and ticket['user_id'] != user_id:
            return "You do not have permission to download this file.", 403

        return send_from_directory(
            directory=os.path.dirname(attachment['file_path']),
            path=os.path.basename(attachment['file_path']),
            as_attachment=True,
            download_name=attachment['friendly_name']
        )
    except Exception as e:
        logger.error(f"Error downloading attachment {attachment_id}: {e}", exc_info=True)
        return "An error occurred while downloading the attachment.", 500

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve uploaded files. Profile pictures are public, other files require login."""
    # Prevent path traversal attacks
    upload_folder = os.path.realpath(app.config['UPLOAD_FOLDER'])
    requested_path = os.path.realpath(os.path.join(upload_folder, filename))
    if not is_path_within(requested_path, upload_folder) and requested_path != upload_folder:
        abort(403)

    # Allow profile pictures to be served without login
    if filename.startswith('profile_pics/'):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/upload_profile_picture', methods=['POST'])
@login_required
@csrf_protect
def upload_profile_picture():
    """Upload a profile picture for the current user."""
    if 'profile_picture' not in request.files:
        return jsonify({'status': 'error', 'message': 'No profile picture uploaded.'}), 400

    file = request.files['profile_picture']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No profile picture uploaded.'}), 400

    if not file or not allowed_file(file.filename, IMAGE_ALLOWED_EXTENSIONS):
        return jsonify({'status': 'error', 'message': 'Unsupported file format. Allowed: png, jpg, jpeg, gif, bmp, webp'}), 400

    filename = secure_filename(file.filename)
    user_id = session['user_id']
    unique_filename = f"{user_id}_{int(time.time())}_{filename}"

    upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'profile_pics')
    os.makedirs(upload_dir, exist_ok=True)

    filepath = os.path.join(upload_dir, unique_filename)

    # Save to temporary location first
    temp_path = filepath + '.tmp'
    file.save(temp_path)

    # Scan the file
    is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
    if not is_safe:
        return jsonify({'status': 'error', 'message': message}), 400

    # Move to final location only if safe
    os.rename(temp_path, filepath)

    db = get_db()
    profile_picture_path = os.path.join('profile_pics', unique_filename)
    db.execute('UPDATE users SET profile_picture_url = %s WHERE id = %s', (profile_picture_path, user_id))
    db.commit()

    return jsonify({
        'status': 'success',
        'message': 'Profile picture updated.',
        'url': f"/uploads/{profile_picture_path}"
    })


# ============================================================================
# ENHANCED FILE OPERATIONS (Copy, Bulk Rename, Preview)
# ============================================================================

@app.route('/api/projects/<int:project_id>/files/copy', methods=['POST'])
@login_required
@csrf_protect
def copy_project_file(project_id):
    """Copy/duplicate a file within a project."""
    db = get_db()
    user_id = session['user_id']

    is_valid, error_msg = validate_file_creator_access(db, user_id, project_id)
    if not is_valid:
        return jsonify({'status': 'error', 'message': error_msg}), 403

    data = request.get_json() or {}
    file_id = data.get('file_id')
    if not file_id:
        return jsonify({'status': 'error', 'message': 'file_id is required'}), 400

    new_name = data.get('new_name')
    target_path = data.get('target_path')

    try:
        # Check file exists and belongs to project
        file_row = db.execute(
            'SELECT id, name, path FROM files WHERE id = %s AND project_id = %s',
            (file_id, project_id)
        ).fetchone()
        if not file_row:
            return jsonify({'status': 'error', 'message': 'File not found'}), 404

        from phonos_app.helpers.file_operations import copy_file
        result = copy_file(project_id, file_id, new_name, target_path, user_id=user_id)

        return jsonify({'status': 'success', 'file': result}), 201
    except (ValueError, FileNotFoundError) as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to copy file {file_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/projects/<int:project_id>/folders', methods=['POST'])
@login_required
@csrf_protect
def create_project_folder(project_id):
    """Create a new folder inside a project."""
    db = get_db()
    user_id = session['user_id']

    role = get_project_assignment(db, user_id, project_id)
    if not role or role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to create folders in this project.'}), 403

    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': 'Folder name is required.'}), 400

    safe_name = secure_filename(name)
    if not safe_name or safe_name != name:
        return jsonify({'status': 'error', 'message': 'Invalid folder name.'}), 400

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    project_path = get_safe_project_path(project_id, project['team_id'])
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Invalid project path.'}), 500

    folder_path = os.path.abspath(os.path.join(project_path, safe_name))
    if not is_path_within(folder_path, project_path):
        return jsonify({'status': 'error', 'message': 'Forbidden folder path.'}), 403

    if os.path.exists(folder_path):
        return jsonify({'status': 'error', 'message': 'A folder or file with this name already exists.'}), 409

    try:
        os.makedirs(folder_path, exist_ok=False)
        return jsonify({'status': 'success', 'message': 'Folder created.', 'name': safe_name})
    except Exception as e:
        logger.error(f"Failed to create folder {folder_path} in project {project_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to create folder.'}), 500


@app.route('/api/projects/<int:project_id>/files/bulk-rename', methods=['POST'])
@login_required
@csrf_protect
def bulk_rename_project_files(project_id):
    """Bulk rename files in a project using pattern matching."""
    db = get_db()
    user_id = session['user_id']

    is_valid, error_msg = validate_file_creator_access(db, user_id, project_id)
    if not is_valid:
        return jsonify({'status': 'error', 'message': error_msg}), 403

    data = request.get_json() or {}
    file_ids = data.get('file_ids', [])
    pattern = data.get('pattern', '')
    replacement = data.get('replacement', '')
    use_regex = data.get('use_regex', False)
    dry_run = data.get('dry_run', False)

    if not file_ids:
        return jsonify({'status': 'error', 'message': 'file_ids list is required'}), 400
    if not pattern:
        return jsonify({'status': 'error', 'message': 'pattern is required'}), 400

    try:
        from phonos_app.helpers.file_operations import bulk_rename_files
        result = bulk_rename_files(
            project_id=project_id,
            file_ids=file_ids,
            pattern=pattern,
            replacement=replacement,
            use_regex=use_regex,
            dry_run=dry_run,
        )

        if not dry_run and result.get('results'):
            for r in result['results']:
                if r.get('success'):
                    try:
                        from phonos_app.helpers.project_history import log_project_event
                        log_project_event(
                            project_id=project_id,
                            user_id=user_id,
                            action='file_rename',
                            resource_type='file',
                            resource_id=r['file_id'],
                            resource_name=r['new_name'],
                            description=f"Renamed {r['old_name']} → {r['new_name']}",
                            metadata={'old_name': r['old_name'], 'new_name': r['new_name']},
                        )
                    except Exception:
                        pass

        return jsonify({'status': 'success', 'result': result})
    except Exception as e:
        logger.error(f"Failed to bulk rename files: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/projects/<int:project_id>/files/<int:file_id>/preview', methods=['GET'])
@login_required
def preview_project_file(project_id, file_id):
    """Get a preview of a file."""
    db = get_db()
    user_id = session['user_id']

    role = get_project_assignment(db, user_id, project_id)
    if not role:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    try:
        from phonos_app.helpers.file_operations import get_file_preview
        preview = get_file_preview(project_id, file_id)
        return jsonify({'status': 'success', 'preview': preview})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 404
    except FileNotFoundError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 404
    except Exception as e:
        logger.error(f"Failed to preview file {file_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ============================================================================

@app.route('/remove_profile_picture', methods=['POST'])
@login_required
@csrf_protect
def remove_profile_picture():
    """Remove the current user's profile picture."""
    db = get_db()
    user_id = session['user_id']

    # Get current profile picture path
    user = db.execute('SELECT profile_picture_url FROM users WHERE id = %s', (user_id,)).fetchone()
    if user and user['profile_picture_url']:
        pic_path = user['profile_picture_url']
        # Delete the file
        full_path = os.path.join(app.config['UPLOAD_FOLDER'], pic_path)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
            except OSError as e:
                logger.error(f"Error deleting profile picture {full_path}: {e}")

    # Clear the URL in the database
    db.execute('UPDATE users SET profile_picture_url = NULL WHERE id = %s', (user_id,))
    db.commit()

    return jsonify({'status': 'success', 'message': 'Profile picture removed.'})
