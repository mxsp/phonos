"""API routes for job-related operations."""
from __future__ import annotations

import gzip
import json
import math
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
from typing import Any, Dict, Iterable, Optional

import bcrypt
import redis
from flask import (
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
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
from .api_external_bridge import MODEL_TYPE_MAP
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
    get_job_branch_path,
    get_job_branch_relative_path,
    get_db,
    get_team_membership,
    get_team_node_rows,
    get_project_assignment,
    get_project_storage_roots,
    get_safe_project_path,
    get_supported_model_name,
    get_user_storage_state,
    is_valid_public_key,
    friendly_artifact_download_name,
    login_required,
    normalize_project_role,
    normalize_job_branch_name,
    normalize_worker_types,
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
    resolve_job_artifact_path,
    resolve_job_branch_path,
    tags as tags_helper,
)
from ..helpers.analytics import track_event
from ..helpers.batches import create_batch, add_job_to_batch
from ..helpers.evo2_view import build_evo2_view

# Import security decorators
from ..helpers.security import csrf_protect, rate_limit, sanitize_json_inputs, sanitize_html, sanitize_input, validate_sql_identifier
from ..helpers.file_scanner import scan_upload
from ..helpers.advanced_validation import AdvancedSanitizer

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}


def _extract_plddt_from_mmcif(cif_path: str, qa_only: bool = False) -> list:
    """Extract per-residue pLDDT (0-100) from a ModelCIF/mmCIF structure file.

    OpenFold3/AlphaFold-style mmCIF stores pLDDT in the ``_ma_qa_metric_local``
    QA table, falling back to the ``_atom_site`` B-factor column when *qa_only*
    is False.  Pass *qa_only* = True to skip the B-factor fallback — use this
    for OpenFold3 where the B-factor column may contain the AF3 ranking score
    (0.8·ipTM + 0.2·pTM) instead of pLDDT, which would produce a misleading
    ~20 % "confidence" for monomer predictions.
    Returns [] on failure.
    """
    try:
        with open(cif_path, 'r') as f:
            lines = f.read().split('\n')
    except OSError:
        return []

    # Strategy 1: ModelCIF per-residue QA table.
    qa_scores = []
    qa_headers = {}
    in_qa_loop = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('loop_'):
            in_qa_loop = False
            qa_headers = {}
            continue
        if line.startswith('_ma_qa_metric_local.'):
            in_qa_loop = True
            qa_headers[line.split('.', 1)[1].strip()] = len(qa_headers)
            continue
        if not in_qa_loop:
            continue
        if line.startswith('#'):
            in_qa_loop = False
            continue
        if line.startswith('_'):
            continue
        parts = line.split()
        idx = qa_headers.get('metric_value')
        if idx is None or idx >= len(parts):
            continue
        try:
            qa_scores.append(float(parts[idx]))
        except (TypeError, ValueError):
            continue

    if qa_scores:
        if max(qa_scores) <= 1.05:
            qa_scores = [s * 100 for s in qa_scores]
        return [max(0.0, min(100.0, s)) for s in qa_scores]

    # Strategy 2: CA/P/C4' B-factors from the _atom_site loop.
    # NOTE: OpenFold3 often stores the AF3 ranking score in this column
    # instead of pLDDT — callers should pass qa_only=True in that case.
    if not qa_only:
        atom_headers = {}
        in_atom_loop = False
        seen = set()
        scores = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith('loop_'):
                in_atom_loop = False
                atom_headers = {}
                continue
            if line.startswith('_atom_site.'):
                in_atom_loop = True
                atom_headers[line.split('.', 1)[1].strip()] = len(atom_headers)
                continue
            if not in_atom_loop:
                continue
            if line.startswith('#'):
                in_atom_loop = False
                continue
            if line.startswith('_'):
                continue
            parts = line.split()
            if len(parts) < len(atom_headers):
                continue

            def col(*names):
                for n in names:
                    i = atom_headers.get(n)
                    if i is not None and i < len(parts):
                        return parts[i].strip('\'"')
                return ''

            if (col('group_PDB') or 'ATOM') != 'ATOM':
                continue
            if col('label_atom_id', 'auth_atom_id') not in ('CA', 'P', "C4'"):
                continue
            res_key = col('label_asym_id', 'auth_asym_id') + ':' + col('label_seq_id', 'auth_seq_id')
            if not res_key.endswith(':') and res_key in seen:
                continue
            seen.add(res_key)
            b_raw = col('B_iso_or_equiv')
            try:
                scores.append(float(b_raw))
            except (TypeError, ValueError):
                continue

        if scores and max(scores) <= 1.05:
            scores = [s * 100 for s in scores]
        return [max(0.0, min(100.0, s)) for s in scores]
    return []


def strip_fasta_header(sequence: str) -> str:
    """
    Strip FASTA header(s) from a sequence string.

    FASTA format:
        >header line (description)
        SEQUENCEDATA
        MOREDATA

    This function removes all lines starting with '>' and joins the remaining
    sequence lines together, removing whitespace.

    Args:
        sequence: Raw sequence string that may contain FASTA headers

    Returns:
        Clean sequence string with headers removed
    """
    if not sequence:
        return sequence

    # Check if this looks like a FASTA format (has '>' character)
    if '>' not in sequence:
        # Not FASTA format, just remove whitespace and return
        return ''.join(sequence.split())

    # Process FASTA format - remove header lines
    sequence_lines = []
    for line in sequence.splitlines():
        stripped = line.strip()
        # Skip empty lines and header lines (starting with '>')
        if not stripped or stripped.startswith('>'):
            continue
        sequence_lines.append(stripped)

    # Join all sequence lines and remove any remaining whitespace
    return ''.join(''.join(sequence_lines).split())



# Import _delete_project_logic from api.py since it's used by handle_single_project
from .api import _delete_project_logic


@app.route('/api/projects/<int:project_id>', methods=['DELETE'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def handle_single_project(project_id):
    """
    Deletes a project and all its associated data. Only the 'Project Manager' can delete a project.
    """
    db = get_db()
    user_id = session['user_id']

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404
    team_id = project['team_id']

    # Get all members before deletion to notify them after.
    members = db.execute('SELECT user_id FROM project_assignments WHERE project_id = %s', (project_id,)).fetchall()

    db.execute('BEGIN')
    try:
        _delete_project_logic(db, project_id, team_id)
        db.commit()
        # After successful deletion, emit an event to update the UI for all members
        for member in members:
            socketio.emit('project_deleted', {'project_id': project_id}, room=f"user_{member['user_id']}")
            socketio.emit('user_info_update', room=f"user_{member['user_id']}")
        return jsonify({'status': 'success', 'message': 'Project and all associated jobs/files deleted.'})
    except Exception:
        db.rollback()
        logger.error(f"Error deleting project {project_id} for user {user_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while deleting the project.'}), 500

@app.route('/api/projects/<int:project_id>/archive', methods=['POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def archive_project(project_id):
    """Archive a project: zip its files and mark as archived."""
    import zipfile as _zipfile
    import logging
    logger = logging.getLogger(__name__)
    db = get_db()
    data = request.json or {}
    is_archived = data.get('is_archived', True)

    project = db.execute('SELECT id, name, team_id, is_archived FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found'}), 404

    if is_archived and not project['is_archived']:
        project_path = get_safe_project_path(project_id, project['team_id'])
        if project_path:
            try:
                archives_dir = os.path.join(project_path, 'archives')
                os.makedirs(archives_dir, exist_ok=True)
                zip_path = os.path.join(archives_dir, f'{project_id}.zip')
                with _zipfile.ZipFile(zip_path, 'w', _zipfile.ZIP_DEFLATED) as zf:
                    for subdir in ('uploads', 'jobs'):
                        src = os.path.join(project_path, subdir)
                        if os.path.isdir(src):
                            for root, _dirs, fnames in os.walk(src):
                                for fname in fnames:
                                    fpath = os.path.join(root, fname)
                                    arcname = os.path.relpath(fpath, project_path)
                                    zf.write(fpath, arcname)
            except Exception as e:
                logger.error(f"Error creating archive for project {project_id}: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': 'Failed to create archive. Please try again.'}), 500

    if is_archived:
        db.execute(
            'UPDATE projects SET is_archived = 1, archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
            (project_id,)
        )
    else:
        db.execute(
            'UPDATE projects SET is_archived = 0, archived_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
            (project_id,)
        )
    db.commit()

    status_text = 'archived' if is_archived else 'unarchived'
    return jsonify({'status': 'success', 'message': f'Project {status_text} successfully.'})

@app.route('/api/projects/<int:project_id>/download', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
def download_project(project_id):
    """Download a project as a .curry file containing all files, jobs, and metadata.

    .curry file format specification:
    - Container: ZIP archive with .curry extension
    - Structure:
      /project_metadata.json - Project info and jobs data
      /files/ - Project input files
      /jobs/ - Job output files organized by job_id
    """
    import zipfile
    import io
    from flask import send_file

    db = get_db()
    project = db.execute('SELECT name, description, is_private, download_count FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    # Get all project files
    files = db.execute('SELECT id, name, path, size, type, created_at FROM files WHERE project_id = %s', (project_id,)).fetchall()

    # Get all project jobs with full details
    jobs = db.execute('''
        SELECT j.job_id, j.model, j.status, j.created_at, j.updated_at, j.output_file,
               j.output_file_size, j.output_display_name, j.sequence, j.message,
               j.execution_time, j.input_file_id, j.policy, j.creator_id,
               u.username as creator_username
        FROM jobs j
        LEFT JOIN users u ON j.creator_id = u.id
        WHERE j.project_id = %s
        ORDER BY j.created_at DESC
    ''', (project_id,)).fetchall()

    # Create .curry file (ZIP) in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Add metadata including jobs data
        jobs_data = []
        for job in jobs:
            job_dict = dict(job)
            # Convert datetime to ISO format for JSON serialization
            if job_dict.get('created_at'):
                job_dict['created_at'] = job_dict['created_at']
            if job_dict.get('updated_at'):
                job_dict['updated_at'] = job_dict['updated_at']
            jobs_data.append(job_dict)

        files_data = []
        for file_row in files:
            file_dict = dict(file_row)
            if file_dict.get('created_at'):
                file_dict['created_at'] = file_dict['created_at']
            files_data.append(file_dict)

        metadata = {
            'format_version': '1.0',
            'format_name': 'curry',
            'name': project['name'],
            'description': project['description'],
            'exported_at': datetime.utcnow().isoformat(),
            'jobs': jobs_data,
            'files_metadata': files_data
        }
        # default=str so datetime/Decimal values (created_at/updated_at etc.,
        # returned as objects by RealDictCursor) serialize instead of raising.
        zip_file.writestr('project_metadata.json', json.dumps(metadata, indent=2, default=str))

        # Add project input files
        for file_row in files:
            file_path = file_row['path']
            if os.path.exists(file_path):
                zip_file.write(file_path, os.path.join('files', file_row['name']))

        # Add job output files
        for job in jobs:
            if job['output_file'] and os.path.exists(job['output_file']):
                # Store job outputs in jobs/{job_id}/ directory
                output_filename = job['output_display_name'] or os.path.basename(job['output_file'])
                zip_file.write(job['output_file'], os.path.join('jobs', job['job_id'], output_filename))

    # Increment download count for public projects
    if not project['is_private']:
        db.execute('UPDATE projects SET download_count = download_count + 1 WHERE id = %s', (project_id,))
        db.commit()

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/x-curry',
        as_attachment=True,
        download_name=f"{project['name']}.curry"
    )

@app.route('/api/projects/upload', methods=['POST'])
@login_required
@csrf_protect
def upload_project():
    """Upload and restore a project from a .curry file.

    Supports both .curry files (with jobs) and legacy .zip files (without jobs).
    """
    import zipfile
    import tempfile
    import uuid

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file provided.'}), 400

    file = request.files['file']
    # Accept both .curry and .zip files
    if not (file.filename.endswith('.curry') or file.filename.endswith('.zip')):
        return jsonify({'status': 'error', 'message': 'Only .curry or .zip files are supported.'}), 400

    user_id = session['user_id']
    db = get_db()

    # Resolve the destination team: use the requested team (if the user is a
    # member) or fall back to the user's personal workspace, mirroring project
    # creation. Without this, an omitted team_id orphaned the imported project.
    raw_team_id = request.form.get('team_id')
    if raw_team_id:
        try:
            team_id = int(raw_team_id)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'A valid Team ID is required.'}), 400
        if not get_team_membership(db, user_id, team_id):
            return jsonify({'status': 'error', 'message': 'You must be a member of the team to import projects.'}), 403
    else:
        team_id = ensure_personal_team(db, user_id)
        if not team_id:
            return jsonify({'status': 'error', 'message': 'Could not resolve a workspace for the imported project.'}), 500

    try:
        # Create temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save to temporary location first
            temp_path = os.path.join(temp_dir, 'project_archive.tmp')
            file.save(temp_path)

            # Scan the file
            is_safe, message = scan_upload(temp_path, file.filename, user_id, request.remote_addr)
            if not is_safe:
                # temp_path is automatically deleted by scan_upload
                return jsonify({'status': 'error', 'message': message}), 400

            # Move to final location only if safe
            archive_path = os.path.join(temp_dir, 'project_archive')
            os.rename(temp_path, archive_path)

            # Extract archive into a dedicated subdir (so the raw archive file we
            # saved alongside it isn't later mistaken for project content).
            extract_dir = os.path.join(temp_dir, 'extracted')
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(archive_path, 'r') as zip_file:
                # Validate against Zip Slip path traversal
                real_extract = os.path.realpath(extract_dir)
                for member in zip_file.infolist():
                    member_path = os.path.realpath(os.path.join(real_extract, member.filename))
                    if not member_path.startswith(real_extract + os.sep) and member_path != real_extract:
                        return jsonify({'status': 'error', 'message': 'Invalid archive: path traversal detected.'}), 400
                zip_file.extractall(extract_dir)

            # Locate the export manifest. A re-zipped export may nest it inside a
            # folder, so fall back to a recursive search before giving up.
            metadata_path = os.path.join(extract_dir, 'project_metadata.json')
            if not os.path.exists(metadata_path):
                metadata_path = None
                for _root, _dirs, _fnames in os.walk(extract_dir):
                    if 'project_metadata.json' in _fnames:
                        metadata_path = os.path.join(_root, 'project_metadata.json')
                        break

            if metadata_path:
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)
                base_dir = os.path.dirname(metadata_path)
                is_curry = metadata.get('format_name') == 'curry'
                base_name = metadata.get('name') or 'Imported Project'
                description = metadata.get('description') or ''
            else:
                # No manifest: treat as a plain or archived-project zip (the
                # "Download zip" of an archived project holds uploads/ and jobs/
                # but no manifest). Recover it as a fresh project named after the
                # uploaded file, importing every file it contains.
                metadata = {}
                base_dir = extract_dir
                is_curry = False
                raw_name = os.path.splitext(os.path.basename(file.filename or 'Imported Project'))[0]
                if raw_name.endswith('_archive'):
                    raw_name = raw_name[:-len('_archive')]
                base_name = raw_name.strip() or 'Imported Project'
                description = ''

            # Avoid colliding with an existing project of the same name in this
            # team (e.g. re-importing a project you still have) by suffixing.
            project_name = base_name
            suffix = 1
            while db.execute(
                'SELECT 1 FROM projects WHERE team_id = %s AND lower(name) = lower(%s)',
                (team_id, project_name)
            ).fetchone():
                suffix += 1
                project_name = f'{base_name} ({suffix})'

            cursor = db.execute(
                'INSERT INTO projects (team_id, name, description) VALUES (%s, %s, %s)',
                (team_id, project_name, description)
            )
            result = cursor.fetchone()

            project_id = result["id"] if result else None

            # Assign user as project manager
            db.execute(
                'INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, %s)',
                (project_id, user_id, PROJECT_ROLE_MANAGER)
            )

            # Create file ID mapping (old file ID -> new file ID)
            file_id_mapping = {}

            user_projects_dir = get_safe_project_path(project_id, team_id)
            os.makedirs(user_projects_dir, exist_ok=True)

            files_metadata = metadata.get('files_metadata', [])
            files_meta_dict = {fm.get('name'): fm for fm in files_metadata if isinstance(fm, dict)}

            def _import_file(src_path, original_name):
                # Copy one file into the new project, deduping name collisions.
                dest_name = original_name
                stem, ext = os.path.splitext(original_name)
                n = 1
                while os.path.exists(os.path.join(user_projects_dir, dest_name)):
                    n += 1
                    dest_name = f'{stem}_{n}{ext}'
                dest_path = os.path.join(user_projects_dir, dest_name)
                shutil.copy2(src_path, dest_path)
                file_size = os.path.getsize(dest_path)
                file_meta = files_meta_dict.get(original_name, {})
                old_file_id = file_meta.get('id')
                cursor = db.execute(
                    'INSERT INTO files (project_id, creator_id, name, path, size) VALUES (%s, %s, %s, %s, %s)',
                    (project_id, user_id, dest_name, dest_path, file_size)
                )
                result = cursor.fetchone()
                if old_file_id and result:
                    file_id_mapping[old_file_id] = result["id"]

            files_dir = os.path.join(base_dir, 'files')
            if metadata_path and os.path.isdir(files_dir):
                # Export/.curry layout: input files live under files/
                for filename in sorted(os.listdir(files_dir)):
                    src_path = os.path.join(files_dir, filename)
                    if os.path.isfile(src_path):
                        _import_file(src_path, filename)
            elif not metadata_path:
                # Generic/archive zip: import every file it contains (uploads/, jobs/, …).
                for root, _dirs, fnames in os.walk(base_dir):
                    for fname in sorted(fnames):
                        src_path = os.path.join(root, fname)
                        if os.path.isfile(src_path):
                            _import_file(src_path, fname)

            # Import jobs if this is a .curry file
            if is_curry and 'jobs' in metadata:
                jobs_data = metadata['jobs']
                # Use the project's job output directory
                jobs_output_dir = get_safe_project_path(project_id, team_id)
                os.makedirs(jobs_output_dir, exist_ok=True)

                for job_data in jobs_data:
                    old_job_id = job_data['job_id']
                    new_job_id = str(uuid.uuid4())

                    # Map old input file ID to new one
                    old_input_file_id = job_data.get('input_file_id')
                    new_input_file_id = file_id_mapping.get(old_input_file_id) if old_input_file_id else None

                    # Copy job output file if it exists
                    job_output_path = None
                    job_output_size = 0
                    job_outputs_src_dir = os.path.join(base_dir, 'jobs', old_job_id)

                    if os.path.exists(job_outputs_src_dir):
                        for output_filename in os.listdir(job_outputs_src_dir):
                            src_output = os.path.join(job_outputs_src_dir, output_filename)
                            if os.path.isfile(src_output):
                                # Create unique output path for new job
                                dest_output = os.path.join(jobs_output_dir, f"{new_job_id}_{output_filename}")
                                shutil.copy2(src_output, dest_output)
                                job_output_path = dest_output
                                job_output_size = os.path.getsize(dest_output)
                                break  # Take first file as main output

                    # Insert job with new IDs
                    db.execute('''
                        INSERT INTO jobs (
                            job_id, project_id, creator_id, model, status, sequence,
                            message, execution_time, input_file_id, policy,
                            output_file, output_file_size, output_display_name,
                            created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        new_job_id,
                        project_id,
                        user_id,  # Assign to importing user
                        job_data.get('model'),
                        job_data.get('status', 'completed'),
                        job_data.get('sequence'),
                        job_data.get('message'),
                        job_data.get('execution_time'),
                        new_input_file_id,
                        job_data.get('policy'),
                        job_output_path,
                        job_output_size,
                        job_data.get('output_display_name'),
                        job_data.get('created_at'),
                        job_data.get('updated_at')
                    ))

            db.commit()

            job_count = len(metadata.get('jobs', [])) if is_curry else 0
            message = f'Project imported successfully with {job_count} jobs.' if is_curry else 'Project imported successfully.'

            return jsonify({
                'status': 'success',
                'message': message,
                'project_id': project_id,
                'jobs_imported': job_count
            })

    except Exception as e:
        db.rollback()
        logger.error(f"Error uploading project: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to import project. Please try again.'}), 500

@app.route('/api/projects/<int:project_id>/jobs', methods=['GET'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER})
def list_project_jobs(project_id: int):
    """Return all jobs for the given project ordered by recency."""
    try:
        db = get_db()
        rows = db.execute(
            '''SELECT j.job_id, j.model, j.name, j.status, j.created_at, j.output_file, j.output_file_size, j.output_display_name, LENGTH(j.sequence) AS sequence_length, j.message, j.execution_time, j.input_file_id, j.project_id, j.policy, j.creator_id, u.username as creator_username,
            j.batch_id, COALESCE(b.name, 'Unnamed Batch') as batch_name 
            FROM jobs j 
            LEFT JOIN users u ON j.creator_id = u.id 
            LEFT JOIN batches b ON j.batch_id = b.id
            WHERE j.project_id = %s 
            ORDER BY j.created_at DESC''',
            (project_id,),
        ).fetchall()

        jobs = []
        for row in rows:
            job = dict(row)
            model_type = job.get('model')
            model_info = Config.get_model_definition(model_type)
            job['model_name'] = model_info.get('name', model_type) if model_info else model_type
            jobs.append(job)

        return jsonify(jobs)
    except Exception as e:
        logger.error(f"Error fetching jobs for project {project_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch project jobs.'}), 500


@app.route('/api/projects/<int:project_id>/jobs', methods=['POST'])
@login_required
@project_role_required(JOB_SUBMISSION_ALLOWED_ROLES)
@csrf_protect
def submit_project_job(project_id: int):
    """Submit a new job for the specified project."""

    payload = request.get_json(silent=True) or {}
    payload = {**payload, 'project_id': project_id}
    return _handle_job_submission(project_id, payload)


@app.route('/api/batch/jobs', methods=['POST'])
@login_required
@csrf_protect
def submit_batch_jobs():
    """Submit multiple jobs as a batch.
    
    Two modes:
    - batch_mode=false (default): Each sequence becomes a separate job
    - batch_mode=true: All sequences are processed as a single job with multiple sequences
    """
    
    payload = request.get_json(silent=True) or {}
    project_id = payload.get('project_id')
    model_type = payload.get('model')
    items = payload.get('items', [])
    config = payload.get('config', {})
    batch_mode = payload.get('batch_mode', False)  # New: true = one job with multiple sequences
    
    if not project_id:
        return jsonify({'status': 'error', 'message': 'Missing project ID.'}), 400
    if not model_type:
        return jsonify({'status': 'error', 'message': 'Missing model selection.'}), 400
    if not items or not isinstance(items, list):
        return jsonify({'status': 'error', 'message': 'Missing or invalid items array.'}), 400
    if len(items) > 50:  # Reasonable limit for batch size
        return jsonify({'status': 'error', 'message': 'Batch size too large. Maximum 50 items per batch.'}), 400
    
    # Validate each item
    valid_items = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return jsonify({'status': 'error', 'message': f'Item {i+1} is not a valid object.'}), 400
        
        sequence = item.get('sequence', '').strip()
        chains = item.get('chains')
        
        if not sequence and not (isinstance(chains, list) and len(chains) > 0):
            continue  # Skip empty items
        
        valid_item = {
            'name': item.get('name', f'Batch Item {i+1}').strip()
        }
        if sequence:
            valid_item['sequence'] = sequence
        if chains:
            valid_item['chains'] = chains
            
        valid_items.append(valid_item)
    
    if not valid_items:
        return jsonify({'status': 'error', 'message': 'No valid sequences or chains found in batch.'}), 400
    
    try:
        db = get_db()
        
        # Validate project and permissions
        project = db.execute(
            "SELECT p.id FROM projects p WHERE p.id = %s",
            (project_id,)
        ).fetchone()
        if not project:
            return jsonify({'status': 'error', 'message': 'Project not found.'}), 404
        
        role = get_project_assignment(db, session['user_id'], project_id)
        if not role or role not in JOB_SUBMISSION_ALLOWED_ROLES:
            return jsonify({'status': 'error', 'message': 'You do not have permission to submit jobs to this project.'}), 403
        
        # Validate model
        model_info = Config.get_model_definition(model_type)
        if not model_info:
            return jsonify({'status': 'error', 'message': 'Unknown model requested.'}), 400
        
        # Check if model supports sequence or multimer input
        supported_inputs = set(model_info.get('input_modes') or [])
        is_multimer_model = model_info.get('task') in ['protein_complex_folding', 'structure_prediction'] or model_type.endswith('-multimer')
        
        if 'sequence' not in supported_inputs and not is_multimer_model:
            return jsonify({'status': 'error', 'message': 'Selected model does not support sequence/multimer input.'}), 400
        
        # Create a batch record for these jobs
        batch_name = payload.get('batch_name', f"Batch {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        try:
            batch = create_batch(
                project_id=project_id,
                creator_id=session['user_id'],
                name=batch_name,
                description=f"{'Multi-sequence ' if batch_mode else ''}Batch job with {len(valid_items)} items using {model_info.get('name', model_type)}",
                metadata={'model': model_type, 'item_count': len(valid_items), 'batch_mode': batch_mode}
            )
            batch_id = batch['id']
            batch_id_str = batch['batch_id']
        except Exception as e:
            logger.error(f"Failed to create batch: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Failed to create batch container.'}), 500
        
        # Create batch jobs
        created_jobs = []
        errors = []
        
        if batch_mode:
            # MODE 2: Submit all items as a single job (only supports sequence mode currently)
            try:
                sequences_list = [item.get('sequence') for item in valid_items if 'sequence' in item]
                if not sequences_list:
                    return jsonify({'status': 'error', 'message': 'Batch mode (single job) only supports individual sequences.'}), 400

                top_level_keys = {'project_id', 'model', 'name', 'sequences'}
                model_params = {k: v for k, v in config.items() if k not in top_level_keys}
                job_payload = {
                    'project_id': project_id,
                    'model': model_type,
                    'name': batch_name,
                    'sequences': sequences_list,  # Send as array instead of single sequence
                    'custom_params': model_params,
                }
                
                result = _handle_job_submission(project_id, job_payload)
                
                if isinstance(result, tuple):
                    response_obj, status_code = result[0], result[1]
                else:
                    response_obj, status_code = result, 200

                if status_code >= 400:
                    err_data = response_obj.get_json(silent=True) or {}
                    err_msg = err_data.get('message', 'Unknown error')
                    errors.append(f"Failed to create batch job: {err_msg}")
                else:
                    resp_data = response_obj.get_json(silent=True) or {}
                    job_uuid = resp_data.get('job_id')
                    
                    if job_uuid:
                        job_row = db.execute(
                            'SELECT id FROM jobs WHERE job_id = %s',
                            (job_uuid,)
                        ).fetchone()
                        if job_row:
                            add_job_to_batch(batch_id, job_row['id'])

                    created_jobs.append({
                        'name': batch_name,
                        'job_id': job_uuid,
                        'sequences_count': len(sequences_list)
                    })
                    
            except Exception as e:
                errors.append(f"Failed to create batch job: {str(e)}")
                logger.error(f"Batch job creation error: {e}", exc_info=True)
        else:
            # MODE 1 (default): Each item becomes a separate job
            for item in valid_items:
                try:
                    # Model-specific config fields (ligand_smiles, ligand_ccd, ligand_id,
                    # output_format, diffusion_samples, msa_input, etc.) must go into
                    # custom_params, not the top-level payload, because
                    # _handle_job_submission reads them from custom_params (line 3002+).
                    top_level_keys = {'project_id', 'model', 'name', 'sequence'}
                    model_params = {k: v for k, v in config.items() if k not in top_level_keys}
                    # Clients send config wrapped in custom_params; unwrap it so the
                    # values are at the top level of the per-job custom_params dict.
                    if 'custom_params' in model_params and isinstance(model_params['custom_params'], dict):
                        model_params = model_params['custom_params']

                    job_payload = {
                        'project_id': project_id,
                        'model': model_type,
                        'name': item['name'],
                        'custom_params': model_params,
                    }
                    
                    if 'sequence' in item:
                        job_payload['sequence'] = item['sequence']
                    if 'chains' in item:
                        if 'custom_params' not in job_payload:
                            job_payload['custom_params'] = {}
                        job_payload['custom_params']['chains'] = item['chains']
                    
                    result = _handle_job_submission(project_id, job_payload)
                    
                    if isinstance(result, tuple):
                        response_obj, status_code = result[0], result[1]
                    else:
                        response_obj, status_code = result, 200

                    if status_code >= 400:
                        err_data = response_obj.get_json(silent=True) or {}
                        err_msg = err_data.get('message', 'Unknown error')
                        errors.append(f"Failed to create job '{item['name']}': {err_msg}")
                    else:
                        resp_data = response_obj.get_json(silent=True) or {}
                        job_uuid = resp_data.get('job_id')

                        if job_uuid:
                            job_row = db.execute(
                                'SELECT id FROM jobs WHERE job_id = %s',
                                (job_uuid,)
                            ).fetchone()
                            if job_row:
                                add_job_to_batch(batch_id, job_row['id'])

                        created_jobs.append({
                            'name': item['name'],
                            'job_id': job_uuid,
                            'info': 'multimer' if 'chains' in item else f"{len(item.get('sequence', ''))} AA"
                        })
                            
                except Exception as e:
                    errors.append(f"Failed to create job '{item['name']}': {str(e)}")
                    logger.error(f"Batch job creation error for item {item['name']}: {e}", exc_info=True)
        
        # Return response with summary
        response_data = {
            'status': 'success',
            'message': f'Batch processing completed. {len(created_jobs)} jobs created, {len(errors)} errors.',
            'jobs_count': len(created_jobs),
            'errors_count': len(errors),
            'created_jobs': created_jobs,
            'batch_id': batch_id_str,
            'batch_db_id': batch_id
        }
        
        if errors:
            response_data['errors'] = errors
            response_data['message'] += ' Check errors for details.'
        
        status_code = 207 if errors else 200  # 207 Multi-Status if some errors
        
        return jsonify(response_data), status_code
        
    except Exception as e:
        logger.error(f"Batch submission error: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': 'An unexpected error occurred during batch submission.'
        }), 500


@app.route('/api/jobs/<string:job_id>/logs', methods=['GET'])
@login_required
def get_job_logs(job_id):
    try:
        db = get_db()
        user_id = session['user_id']
        job_info = db.execute('SELECT project_id FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
        if not job_info:
            return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

        role = get_project_assignment(db, user_id, job_info['project_id'])
        if not role:
            return jsonify({'status': 'error', 'message': 'You do not have permission to view these logs.'}), 403
        logs = db.execute('SELECT status, message, timestamp FROM job_logs WHERE job_id = %s ORDER BY timestamp ASC', (job_id,)).fetchall()
        return jsonify([dict(log) for log in logs])
    except Exception as e:
        logger.error(f"Error fetching logs for job {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch job logs.'}), 500


@app.route('/api/jobs/<string:job_id>/retry', methods=['POST'])
@login_required
@csrf_protect
def retry_job(job_id):
    db = get_db()
    user_id = session['user_id']
    job = db.execute('SELECT * FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
    if not job:
        return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

    role = get_project_assignment(db, user_id, job['project_id'])
    if not role or role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to retry this job.'}), 403

    if job['status'] != 'failed':
        return jsonify({'status': 'error', 'message': 'Only failed jobs can be retried.'}), 400

    model_info = Config.get_model_definition(job['model'])
    if not model_info:
        return jsonify({'status': 'error', 'message': 'Unknown model for retry.'}), 400

    creator_id = job['creator_id']
    ensure_monthly_credit_reset(db, creator_id)
    user = db.execute('SELECT credits FROM users WHERE id = %s', (creator_id,)).fetchone()

    retry_cost = job['cost']
    for candidate in (retry_cost, model_info.get('credit_cost'), 0):
        try:
            retry_cost = max(0, int(float(candidate)))
            break
        except (TypeError, ValueError):
            continue
    else:
        retry_cost = 0

    if user['credits'] < retry_cost:
        return jsonify({'status': 'error', 'message': 'The job creator has insufficient credits to retry the job.'}), 402

    project = db.execute('SELECT * FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    team_id = project['team_id']
    # organization_id may not exist in all database setups
    try:
        organization_id = project['organization_id'] or project['team_id']
    except (KeyError, IndexError):
        organization_id = project['team_id']

    project_path = get_safe_project_path(job['project_id'], team_id)
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Project path could not be determined or is invalid.'}), 404

    is_active, is_visible = get_model_state(db, job['model'])
    if not (is_active and is_visible):
        reason = (Config.get_model_definition(job['model']) or {}).get('offline_reason')
        return jsonify({'status': 'error', 'message': reason or 'Selected model is currently offline for maintenance.'}), 400

    output_dir = get_job_branch_path(project_path, job_id, branch='main', create=True)
    if not output_dir:
        return jsonify({'status': 'error', 'message': 'Failed to resolve job output directory.'}), 500
    job_task = model_info.get('task') or 'protein_folding'
    output_extension = model_info.get('output_extension') or '.out'
    output_filename = f"{job_id}{output_extension}"
    output_path = os.path.join(output_dir, output_filename)

    sequence = (job['sequence'] or '').strip()
    if not sequence:
        return jsonify({'status': 'error', 'message': 'Original job sequence is unavailable; cannot retry.'}), 400

    job_metadata: dict[str, object] = {}
    if job['policy']:
        try:
            job_metadata = json.loads(job['policy'])
        except (TypeError, json.JSONDecodeError):
            job_metadata = {}

    custom_params = job_metadata.get('custom_params')
    if not isinstance(custom_params, dict):
        custom_params = {}

    job_metadata.update({
        'input_type': 'sequence',
        'node_id': model_info.get('node_id'),
        'task': job_task,
        'artifact_branches': {
            'main': {'relative_path': get_job_branch_relative_path('main')},
            'inputs': {'relative_path': get_job_branch_relative_path('inputs')},
        },
    })

    output_display_name = 'ESM2 Embedding Summary' if job_task == 'protein_embedding' else None
    job_params = {'sequence': sequence}
    if custom_params:
        job_params.update({k: v for k, v in custom_params.items() if v != '' and k not in job_params})
    job_artifacts: dict[str, str] = {}

    if job_task == 'protein_embedding':
        artifact_filename = f"{job_id}_embeddings.npz"
        embeddings_path = os.path.join(output_dir, artifact_filename)
        job_artifacts['embeddings_npz'] = embeddings_path
        job_metadata['artifacts'] = [{
            'name': 'embeddings_npz',
            'filename': artifact_filename,
            'display_name': 'ESM2 residue embeddings (.npz)',
        }]

    job_metadata_json = json.dumps(job_metadata)

    db.execute('BEGIN')
    try:
        # Reset job status and message, debit credit from the original creator
        db.execute(
            "UPDATE jobs SET status = 'submitted', message = NULL, created_at = CURRENT_TIMESTAMP, policy = %s WHERE job_id = %s",
            (job_metadata_json, job_id),
        )
        db.execute('INSERT INTO job_logs (job_id, status, message) VALUES (%s, %s, %s)', (job_id, 'submitted', 'Job retried by user.'))
        db.execute('UPDATE users SET credits = credits - %s WHERE id = %s', (retry_cost, creator_id))
        db.commit()
    except psycopg2.Error:
        db.rollback()
        logger.error(f"DB error while retrying job {job_id} for user {user_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Database error during job retry.'}), 500

    try:
        target_queue = determine_job_target_queue(db, team_id, job['model'])
        params_payload = dict(job_params)
        job_payload = {
            'job_id': job['job_id'],
            'model': job['model'],
            'task': job_task,
            'params': params_payload,
            'output_path': output_path,
            'output_display_name': output_display_name,
            'team_id': team_id,
            'organization_id': organization_id,
        }
        if job_artifacts and job_metadata.get('artifacts'):
            # Build artifacts list with paths for workers
            # Workers expect: [{'name': str, 'path': str, 'filename': str, ...}]
            artifacts_for_worker = []
            for meta in job_metadata['artifacts']:
                artifact_name = meta.get('name')
                if artifact_name and artifact_name in job_artifacts:
                    artifacts_for_worker.append({
                        'name': artifact_name,
                        'path': job_artifacts[artifact_name],
                        'filename': meta.get('filename'),
                        'display_name': meta.get('display_name'),
                    })
            if artifacts_for_worker:
                job_payload['artifacts'] = artifacts_for_worker
        if target_queue:
            job_payload['target_queue'] = target_queue
        r = get_redis_connection()
        # The retry reuses job_id; drop the previous run's terminal-status lock
        # (helpers/listeners.py) or this run's result is skipped as a duplicate.
        r.delete(f"job_terminal_lock:{job_id}")
        r.lpush("queue:new_jobs", json.dumps(job_payload))
    except redis.RedisError:
        # If queuing fails, roll back the DB transaction
        logger.error(f"Redis error while re-queuing job {job_id} for user {user_id}.", exc_info=True)
        with app.app_context():
            db = get_db()
            db.execute('BEGIN')
            db.execute("UPDATE jobs SET status = 'failed', message = 'Failed to re-queue' WHERE job_id = %s", (job_id,))
            db.execute('UPDATE users SET credits = credits + %s WHERE id = %s', (retry_cost, creator_id))
            db.commit()
        return jsonify({'status': 'error', 'message': 'Failed to re-queue job. Please try again.'}), 500

    retried_job = db.execute('SELECT * FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
    # Notify all project members of the job update
    assignments = db.execute('SELECT user_id FROM project_assignments WHERE project_id = %s', (job['project_id'],)).fetchall()
    retried_job_data = {k: serialize_datetime(v) for k, v in dict(retried_job).items()}
    for assignment in assignments:
        socketio.emit('job_update', {'job': retried_job_data}, room=f"user_{assignment['user_id']}")
        socketio.emit('user_info_update', room=f"user_{assignment['user_id']}")
    return jsonify({'status': 'success', 'message': 'Job has been re-submitted.'}), 200


@app.route('/api/jobs/<string:job_id>/duplicate', methods=['GET'])
@login_required
def duplicate_job_config(job_id):
    """Get job configuration for duplication/editing."""
    try:
        db = get_db()
        user_id = session['user_id']
        
        # Fetch job details
        job = db.execute('SELECT * FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
        if not job:
            return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

        # Check permissions
        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job['creator_id'] != user_id:
            return jsonify({'status': 'error', 'message': 'Permission denied.'}), 403

        # Parse policy for parameters
        params = {}
        if job['policy']:
            try:
                policy_data = json.loads(job['policy'])
                params = policy_data.get('custom_params', {})
            except (TypeError, json.JSONDecodeError):
                pass

        # Construct response
        response = {
            'status': 'success',
            'data': {
                'job_id': job['job_id'],
                'name': f"Copy of {job['name']}" if job['name'] else f"Copy of {job['job_id'][:8]}",
                'model': job['model'],
                'sequence': job['sequence'],
                'input_file_id': job['input_file_id'],
                'project_id': job['project_id'],
                'params': params
            }
        }
        
        return jsonify(response)
    except Exception as e:
        logger.error(f"Error getting duplicate config for job {job_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Internal error.'}), 500


@app.route('/api/jobs/<string:job_id>', methods=['DELETE'])
@login_required
@csrf_protect
@rate_limit(limit=30, window=3600, action='job_delete')
def delete_job(job_id):
    db = get_db()
    user_id = session['user_id']
    job = db.execute('SELECT project_id, output_file, output_file_size, creator_id FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
    if not job:
        return jsonify({'status': 'error', 'message': 'Job not found.'}), 404

    role = get_project_assignment(db, user_id, job['project_id'])
    if not role or role not in ['Project Manager', 'Project Contributor']:
        return jsonify({'status': 'error', 'message': 'You do not have permission to delete this job.'}), 403

    # Get all members before deletion to notify them after.
    members = db.execute('SELECT user_id FROM project_assignments WHERE project_id = %s', (job['project_id'],)).fetchall()

    db.execute('BEGIN')
    try:
        # Pass the job creator's ID to correctly update storage
        delete_job_file_and_update_storage(db, job['creator_id'], job_id, job['output_file'], job['output_file_size'])
        db.execute('DELETE FROM jobs WHERE job_id = %s', (job_id,))
        db.commit()

        # After successful deletion, emit events to update the UI for all members
        for member in members:
            socketio.emit('job_deleted', {'job_id': job_id, 'project_id': job['project_id']}, room=f"user_{member['user_id']}")
            socketio.emit('user_info_update', room=f"user_{member['user_id']}")

        return jsonify({'status': 'success', 'message': 'Job and associated file deleted.'})
    except Exception:
        db.rollback()
        logger.error(f"Failed to delete job {job_id} for user {user_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'An internal error occurred while deleting the job.'}), 500

@app.route('/view_output/<job_id>')
@login_required
def view_output(job_id):
    try:
        db = get_db()
        user_id = session['user_id']
        job_row = db.execute('SELECT * FROM jobs WHERE job_id = %s', (job_id,)).fetchone()
        if not job_row:
            return "Job not found.", 404

        job = dict(job_row)

        # Allow access if user has project assignment OR is the job creator
        role = get_project_assignment(db, user_id, job['project_id'])
        if not role and job.get('creator_id') != user_id:
            return "You do not have permission to view this output.", 403

        project = db.execute('SELECT name, team_id FROM projects WHERE id = %s', (job['project_id'],)).fetchone()
        
        if not project:
            logger.error(f"Project not found for job {job_id}, project_id {job['project_id']}")
            return "Project not found.", 404

        project_path = get_safe_project_path(job['project_id'], project['team_id'])

        metadata: dict[str, object] = {}
        if job.get('policy'):
            try:
                metadata = json.loads(job['policy'])
            except (TypeError, json.JSONDecodeError) as json_err:
                logger.error(f"Error decoding policy JSON for job {job_id}. Policy: {job['policy']}. Error: {json_err}")
                metadata = {}

        job_output_dir = resolve_job_branch_path(
            project_path=project_path,
            job_id=job_id,
            output_file=job.get('output_file'),
            policy=metadata,
            branch='main',
            create=False,
        ) if project_path else None

        # Fallback: if job_output_dir is None but output_file exists, derive directory from output_file
        if not job_output_dir and job.get('output_file'):
            output_file = job.get('output_file')
            if output_file and os.path.exists(output_file):
                job_output_dir = os.path.dirname(os.path.abspath(output_file))
                logger.info(f"[view_output] Using fallback job_output_dir from output_file: {job_output_dir}")

        # Convert artifacts to artifacts_info if not already present (for backward compatibility)
        if not metadata.get('artifacts_info') and metadata.get('artifacts'):
            # Get job output directory to construct full paths
            if job_output_dir:
                artifacts_info = []
                for artifact in metadata['artifacts']:
                    artifact_file = artifact.get('filename')
                    if artifact_file:
                        artifact_path = resolve_job_artifact_path(
                            project_path=project_path,
                            job_id=job_id,
                            artifact_name=artifact_file,
                            output_file=job.get('output_file'),
                            policy=metadata,
                            preferred_branch=artifact.get('branch') or 'main',
                        ) or os.path.join(job_output_dir, artifact_file)
                        artifact_size = os.path.getsize(artifact_path) if os.path.exists(artifact_path) else 0
                        artifacts_info.append({
                            'name': artifact.get('name'),
                            'filename': artifact_file,
                            'display_name': artifact.get('display_name'),
                            'size': artifact_size,
                            'path': artifact_path,
                            'branch': artifact.get('branch'),
                            'relative_path': artifact.get('relative_path'),
                            'confidence': artifact.get('confidence'),
                            'type': artifact.get('type'),
                            'pose_index': artifact.get('pose_index'),
                        })
                metadata['artifacts_info'] = artifacts_info

        # Ensure artifacts_info list exists for further processing
        if not metadata.get('artifacts_info'):
             metadata['artifacts_info'] = []

        artifacts_info = metadata['artifacts_info']
        model_id = str(job.get('model') or '').strip().lower()
        model_name = str(metadata.get('model_name') or '').strip().lower()
        # Task is persisted in the job policy/metadata at submission ('task': ...).
        # Must be defined here: it is referenced by the is_molmim_job check below, and
        # without it view_output raised NameError for every non-molmim/genmol job
        # (Boltz2/OpenFold3/DiffDock/AlphaFold/ESMFold), 500'ing the output view.
        job_task = str(metadata.get('task') or '').strip().lower()
        # Molecule-generation results view (molecules table + SMILES viewer). Shared by
        # MolMIM and GenMol; keyed on the molecule_generation task so future generative
        # chemistry models inherit it automatically.
        is_molmim_job = (
            'molmim' in model_id or 'molmim' in model_name
            or 'genmol' in model_id or 'genmol' in model_name
            or job_task == 'molecule_generation'
        )

        # Strip content_b64 from artifacts_info to avoid bloating the template
        for a in artifacts_info:
            a.pop('content_b64', None)

        # SPECIAL HANDLING FOR ALPHAFOLD/ESMFOLD: Scan for multiple ranked PDB files
        if job.get('model') in ['type-0', 'type-2']:
            # Get job output directory
            if job_output_dir and os.path.exists(job_output_dir):
                # Filter out non-existent files from artifacts_info
                valid_artifacts = []
                existing_filenames = set()

                for a in artifacts_info:
                    full_path = os.path.join(job_output_dir, a['filename'])
                    if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                        a['path'] = full_path
                        a['size'] = os.path.getsize(full_path)
                        valid_artifacts.append(a)
                        existing_filenames.add(a['filename'])
                    elif not a.get('filename'):
                        valid_artifacts.append(a)

                artifacts_info = valid_artifacts

                try:
                    # Find all ranked PDB files from AlphaFold/ColabFold output
                    # Pattern: {job_id}_relaxed_rank_00X_alphafold2_ptm_model_Y_seed_000.pdb
                    import re
                    ranked_files = []
                    for filename in os.listdir(job_output_dir):
                        if filename.endswith('.pdb') and filename not in existing_filenames:
                            # Match AlphaFold patterns: ranked_0.pdb, relaxed_rank_001, rank_001, model_1.pdb
                            if any(pattern in filename.lower() for pattern in ['ranked_', 'rank_', 'relaxed_', 'model_']):
                                ranked_files.append(filename)

                    # Sort by rank number (rank_001 = best, rank_005 = worst)
                    def get_rank_num(fname):
                        try:
                            fname_lower = fname.lower()
                            # ColabFold pattern: relaxed_rank_00X_alphafold...
                            rank_match = re.search(r'rank_0*(\d+)', fname_lower)
                            if rank_match:
                                return int(rank_match.group(1))
                            # Try other patterns
                            if 'ranked_' in fname_lower:
                                return int(fname_lower.split('ranked_')[1].split('.')[0].split('_')[0])
                            elif 'model_' in fname_lower:
                                parts = fname_lower.split('model_')[1]
                                return int(parts.split('.')[0].split('_')[0])
                            return 9999
                        except (ValueError, IndexError):
                            return 9999

                    ranked_files.sort(key=get_rank_num)

                    # Add found files to artifacts_info
                    for filename in ranked_files:
                        file_path = os.path.join(job_output_dir, filename)
                        file_size = os.path.getsize(file_path)

                        rank_num = get_rank_num(filename)
                        is_relaxed = 'relaxed' in filename.lower()

                        # Extract model number if present
                        model_match = re.search(r'model_(\d+)', filename.lower())
                        model_num = int(model_match.group(1)) if model_match else None

                        # Create improved display name
                        if rank_num < 9999:
                            # Start with rank
                            if rank_num == 1:
                                display_parts = ["Rank 1 - Best"]
                            else:
                                display_parts = [f"Rank {rank_num}"]

                            # Add model number if available
                            if model_num is not None:
                                display_parts.append(f"Model {model_num}")

                            # Add relaxation status
                            if is_relaxed:
                                display_parts.append("Relaxed")
                            else:
                                display_parts.append("Unrelaxed")

                            display_name = " • ".join(display_parts)
                        else:
                            display_name = filename

                        artifacts_info.append({
                            'name': f'rank_{rank_num}_pdb',
                            'filename': filename,
                            'display_name': display_name,
                            'size': file_size,
                            'path': file_path,
                            'is_relaxed': is_relaxed,
                            'model_rank': rank_num
                        })

                except Exception as e:
                    logger.error(f"Error scanning AlphaFold output files at {job_output_dir}: {e}")

                metadata['artifacts_info'] = artifacts_info

                # Extract pLDDT scores from the best ranked PDB file (B-factor column)
                try:
                    pdb_file = job.get('output_file')
                    if not pdb_file and artifacts_info:
                        # Find best ranked PDB from artifacts
                        best_artifact = min(
                            [a for a in artifacts_info if a.get('filename', '').endswith('.pdb')],
                            key=lambda x: x.get('model_rank', 9999),
                            default=None
                        )
                        if best_artifact:
                            pdb_file = best_artifact.get('path')

                    if pdb_file and os.path.exists(pdb_file):
                        plddt_scores = []
                        current_residue = -1
                        current_chain = ''

                        with open(pdb_file, 'r') as f:
                            for line in f:
                                if line.startswith('ATOM'):
                                    atom_name = line[12:16].strip()
                                    # Only use CA atoms for per-residue scores
                                    if atom_name == 'CA':
                                        try:
                                            res_num = int(line[22:26].strip())
                                            chain = line[21:22].strip()
                                            b_factor = float(line[60:66].strip())

                                            # Only add if new residue
                                            if res_num != current_residue or chain != current_chain:
                                                plddt_scores.append(b_factor)
                                                current_residue = res_num
                                                current_chain = chain
                                        except (ValueError, IndexError):
                                            continue

                        if plddt_scores:
                            metadata['plddt_scores'] = plddt_scores
                            metadata['plddt_avg'] = round(sum(plddt_scores) / len(plddt_scores), 1)
                            metadata['plddt_min'] = round(min(plddt_scores), 1)
                            metadata['plddt_max'] = round(max(plddt_scores), 1)
                            logger.info(f"[AlphaFold] Extracted {len(plddt_scores)} pLDDT scores, avg={metadata['plddt_avg']}")

                except Exception as e:
                    logger.error(f"Error extracting pLDDT scores: {e}")

        # SPECIAL HANDLING FOR DIFFDOCK/FOLD+DOCK: Scan for multiple rank*.sdf files
        if job.get('model') in ['type-3', 'type-5', 'diffdock-re']:
            # Get job output directory if not already set
            if job_output_dir and os.path.exists(job_output_dir):
                # Filter out non-existent files from artifacts_info (cleanup placeholders)
                valid_artifacts = []
                existing_filenames = set()
                
                for a in artifacts_info:
                    # Construct full path to verify existence
                    full_path = os.path.join(job_output_dir, a['filename'])
                    if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                        a['path'] = full_path
                        a['size'] = os.path.getsize(full_path)
                        valid_artifacts.append(a)
                        existing_filenames.add(a['filename'])
                    elif not a.get('filename'): # Keep meta-only artifacts if any
                        valid_artifacts.append(a)
                
                # Update the list to only include valid files
                artifacts_info = valid_artifacts
                
                try:
                    # Find all PDB files (receptor/protein structure)
                    pdb_files = []
                    for filename in os.listdir(job_output_dir):
                        if filename.endswith('.pdb') and filename not in existing_filenames:
                            pdb_files.append(filename)

                    # Add PDB files to artifacts (these are the receptor structures)
                    for filename in pdb_files:
                        file_path = os.path.join(job_output_dir, filename)
                        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                            file_size = os.path.getsize(file_path)
                            artifacts_info.insert(0, {  # Insert at beginning so it appears first
                                'name': 'receptor_pdb',
                                'filename': filename,
                                'display_name': 'Receptor Protein',
                                'size': file_size,
                                'path': file_path,
                                'type': 'protein'
                            })
                            existing_filenames.add(filename)
                            logger.info(f"[DiffDock] Added receptor PDB: {filename}")

                    # Find all rank*.sdf and *_pose_*.sdf files (DiffDock classic and DiffDock-RE)
                    import re as _re
                    rank_files = []
                    for filename in os.listdir(job_output_dir):
                        if filename.endswith('.sdf') and filename not in existing_filenames:
                            if filename.startswith('rank') or '_pose_' in filename or '_docked_ligand' in filename:
                                rank_files.append(filename)

                    # Sort by rank/pose number
                    def get_rank_num(fname):
                        try:
                            if fname.startswith('rank'):
                                parts = fname.replace('rank', '').split('_')[0].split('.')[0]
                                return int(parts)
                            # DiffDock-RE pattern: {job_id}_pose_0.sdf
                            pose_match = _re.search(r'_pose_(\d+)', fname)
                            if pose_match:
                                return int(pose_match.group(1)) + 1  # 0-indexed to 1-indexed
                            # Combined ligand file goes last
                            if '_docked_ligand' in fname:
                                return 0  # Show combined file first
                            return 9999
                        except (ValueError, IndexError):
                            return 9999

                    rank_files.sort(key=get_rank_num)

                    # Try to get confidence scores from job metadata (DiffDock-RE stores them there)
                    pose_confidences = metadata.get('pose_confidence', [])
                    if not pose_confidences:
                        pose_confidences = metadata.get('position_confidence', [])

                    # Add found files to artifacts_info
                    for filename in rank_files:
                        file_path = os.path.join(job_output_dir, filename)
                        file_size = os.path.getsize(file_path)

                        # Extract confidence score from filename if present (e.g., rank1_confidence-0.28.sdf)
                        confidence = None
                        if 'confidence' in filename:
                            try:
                                conf_str = filename.split('confidence-')[1].split('.sdf')[0]
                                confidence = float(conf_str)
                            except (IndexError, ValueError):
                                pass

                        rank_num = get_rank_num(filename)

                        # For DiffDock-RE pose files, get confidence from metadata
                        if confidence is None and '_pose_' in filename:
                            pose_match = _re.search(r'_pose_(\d+)', filename)
                            if pose_match:
                                pose_idx = int(pose_match.group(1))
                                if pose_idx < len(pose_confidences):
                                    confidence = pose_confidences[pose_idx]

                        # Also check artifact metadata for confidence (stored by worker)
                        if confidence is None:
                            for stored_artifact in metadata.get('artifacts', []):
                                if stored_artifact.get('filename') == filename:
                                    confidence = stored_artifact.get('confidence')
                                    break

                        if '_docked_ligand' in filename:
                            display_name = "Combined Docked Ligand"
                        elif rank_num < 9999:
                            display_name = f"Pose {rank_num}"
                            if confidence is not None:
                                display_name += f" (conf: {confidence:.3f})"
                        else:
                            display_name = filename

                        artifacts_info.append({
                            'name': f'rank{rank_num}_sdf',
                            'filename': filename,
                            'display_name': display_name,
                            'size': file_size,
                            'path': file_path,
                            'confidence': confidence,
                            'type': 'ligand'
                        })

                except Exception as e:
                    logger.error(f"Error scanning DiffDock output files at {job_output_dir}: {e}")

                metadata['artifacts_info'] = artifacts_info

                # Set input_pdb_filename for DiffDock - look for the input protein artifact first
                # Priority: 1) input_protein artifact, 2) any PDB with 'input' in name, 3) receptor PDB
                for artifact in artifacts_info:
                    filename = artifact.get('filename', '')
                    artifact_type = artifact.get('type', '')
                    if artifact_type == 'input' or 'input_protein' in filename.lower():
                        metadata['input_pdb_filename'] = filename
                        logger.info(f"[DiffDock] Set input_pdb_filename to input artifact: {filename}")
                        break

                # Fallback to receptor PDB if no input artifact found
                if not metadata.get('input_pdb_filename'):
                    for artifact in artifacts_info:
                        filename = artifact.get('filename', '')
                        if filename.endswith('.pdb') and not filename.startswith('rank'):
                            metadata['input_pdb_filename'] = filename
                            logger.info(f"[DiffDock] Set input_pdb_filename to receptor: {filename}")
                            break

                # If still not found, check the main output file
                if not metadata.get('input_pdb_filename'):
                    output_file = job.get('output_file', '')
                    if output_file and os.path.exists(output_file):
                        basename = os.path.basename(output_file)
                        if basename.endswith('.pdb'):
                            metadata['input_pdb_filename'] = basename
                            logger.info(f"[DiffDock] Set input_pdb_filename from output_file: {basename}")

        # SPECIAL HANDLING FOR RFDIFFUSION: Scan for multiple PDB design files
        if job.get('model') == 'type-12':
            if job_output_dir and os.path.exists(job_output_dir):
                # Filter out non-existent files from artifacts_info
                valid_artifacts = []
                existing_filenames = set()

                for a in artifacts_info:
                    full_path = os.path.join(job_output_dir, a['filename'])
                    if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                        a['path'] = full_path
                        a['size'] = os.path.getsize(full_path)
                        valid_artifacts.append(a)
                        existing_filenames.add(a['filename'])
                    elif not a.get('filename'):
                        valid_artifacts.append(a)

                artifacts_info = valid_artifacts

                try:
                    import re
                    # Find all PDB files in the job directory
                    pdb_files = []
                    for filename in os.listdir(job_output_dir):
                        if filename.endswith('.pdb') and filename not in existing_filenames:
                            pdb_files.append(filename)

                    # Sort by design number or input first
                    def get_sort_key(fname):
                        fname_lower = fname.lower()
                        # Input files first
                        if '_input' in fname_lower:
                            return (0, fname)
                        # Then design files sorted by number
                        design_match = re.search(r'_design_(\d+)', fname_lower)
                        if design_match:
                            return (1, int(design_match.group(1)))
                        return (2, fname)

                    pdb_files.sort(key=get_sort_key)

                    # Add found files to artifacts_info
                    for filename in pdb_files:
                        file_path = os.path.join(job_output_dir, filename)
                        file_size = os.path.getsize(file_path)

                        # Create display name
                        if '_input' in filename.lower():
                            display_name = "Input Structure"
                        else:
                            design_match = re.search(r'_design_(\d+)', filename.lower())
                            if design_match:
                                display_name = f"Design {int(design_match.group(1)) + 1}"
                            else:
                                display_name = filename

                        artifacts_info.append({
                            'name': filename,
                            'filename': filename,
                            'display_name': display_name,
                            'size': file_size,
                            'path': file_path
                        })

                except Exception as e:
                    logger.error(f"Error scanning RFDiffusion output files at {job_output_dir}: {e}")

                metadata['artifacts_info'] = artifacts_info

        # SPECIAL HANDLING FOR PROTEINMPNN: Scan for designed sequence FASTA and PDB files
        if job.get('model') == 'type-4':
            if job_output_dir and os.path.exists(job_output_dir):
                valid_artifacts = []
                existing_filenames = set()

                for a in artifacts_info:
                    full_path = os.path.join(job_output_dir, a['filename'])
                    if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                        a['path'] = full_path
                        a['size'] = os.path.getsize(full_path)
                        valid_artifacts.append(a)
                        existing_filenames.add(a['filename'])
                    elif not a.get('filename'):
                        valid_artifacts.append(a)

                artifacts_info = valid_artifacts

                try:
                    # Find all output files (FASTA, PDB, JSON)
                    for filename in os.listdir(job_output_dir):
                        if filename in existing_filenames:
                            continue
                        if filename.endswith(('.fasta', '.fa', '.pdb', '.json')):
                            file_path = os.path.join(job_output_dir, filename)
                            file_size = os.path.getsize(file_path)

                            # Create display name
                            if filename.endswith(('.fasta', '.fa')):
                                display_name = "Designed Sequences (FASTA)"
                            elif filename.endswith('.pdb'):
                                display_name = "Structure (PDB)"
                            else:
                                display_name = filename

                            artifacts_info.append({
                                'name': filename,
                                'filename': filename,
                                'display_name': display_name,
                                'size': file_size,
                                'path': file_path
                            })

                except Exception as e:
                    logger.error(f"Error scanning ProteinMPNN output files at {job_output_dir}: {e}")

                metadata['artifacts_info'] = artifacts_info

        # SPECIAL HANDLING FOR METAL3D: Scan for prediction output files
        if job.get('model') == 'type-6':
            if job_output_dir and os.path.exists(job_output_dir):
                valid_artifacts = []
                existing_filenames = set()

                for a in artifacts_info:
                    full_path = os.path.join(job_output_dir, a['filename'])
                    if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                        a['path'] = full_path
                        a['size'] = os.path.getsize(full_path)
                        valid_artifacts.append(a)
                        existing_filenames.add(a['filename'])
                    elif not a.get('filename'):
                        valid_artifacts.append(a)

                artifacts_info = valid_artifacts

                try:
                    # Find all output files (PDB, JSON, CSV)
                    for filename in os.listdir(job_output_dir):
                        if filename in existing_filenames:
                            continue
                        if filename.endswith(('.pdb', '.json', '.csv')):
                            file_path = os.path.join(job_output_dir, filename)
                            file_size = os.path.getsize(file_path)

                            # Create display name
                            if filename.endswith('.pdb'):
                                display_name = "Structure with Metal Sites (PDB)"
                            elif filename.endswith('.json'):
                                display_name = "Prediction Results (JSON)"
                            elif filename.endswith('.csv'):
                                display_name = "Binding Site Data (CSV)"
                            else:
                                display_name = filename

                            artifacts_info.append({
                                'name': filename,
                                'filename': filename,
                                'display_name': display_name,
                                'size': file_size,
                                'path': file_path
                            })

                except Exception as e:
                    logger.error(f"Error scanning Metal3D output files at {job_output_dir}: {e}")

                metadata['artifacts_info'] = artifacts_info

        # GENERAL FALLBACK: For any model type without specific handling, scan for common structure files
        # This ensures we always show available output files even for new or unknown model types
        models_with_specific_handling = [
            'type-0', 'type-2', 'type-3', 'type-4', 'type-5', 'type-6', 'type-9', 'type-12',
            'boltz2', 'type-boltz2', 'molmim', 'evo2', 'type-8',
            'openfold3', 'esmfold-re', 'alphafold2-re', 'alphafold2-re-multimer',
            'diffdock-re', 'proteinmpnn-re',
        ]
        if is_molmim_job and model_id and model_id not in models_with_specific_handling:
            models_with_specific_handling.append(model_id)
        if job.get('model') not in models_with_specific_handling and job_output_dir and os.path.exists(job_output_dir):
            # Only run if artifacts_info is empty or has no valid files
            if not metadata.get('artifacts_info'):
                metadata['artifacts_info'] = []
            artifacts_info = metadata['artifacts_info']

            try:
                existing_filenames = {a['filename'] for a in artifacts_info if a.get('filename')}
                common_extensions = ('.pdb', '.cif', '.sdf', '.mol', '.mol2', '.json', '.fasta', '.fa', '.csv', '.log', '.smi')

                for filename in os.listdir(job_output_dir):
                    if filename in existing_filenames:
                        continue
                    if filename.lower().endswith(common_extensions):
                        file_path = os.path.join(job_output_dir, filename)
                        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                            file_size = os.path.getsize(file_path)
                            artifacts_info.append({
                                'name': filename,
                                'filename': filename,
                                'display_name': filename,
                                'size': file_size,
                                'path': file_path
                            })

                metadata['artifacts_info'] = artifacts_info
                if artifacts_info:
                    logger.info(f"[Fallback] Found {len(artifacts_info)} files for model {job.get('model')}")

            except Exception as e:
                logger.error(f"Error in fallback artifact scanning at {job_output_dir}: {e}")

        embedding_summary = None
        summary_error = None
        output_file = job.get('output_file')
        # Note: type-5 is Fold+Dock Pipeline which outputs PDB, not JSON embeddings
        # Embedding summary loading removed as it's no longer applicable

        # Check if output is a PDB or CIF file (both are structure files)
        is_pdb_output = False
        if output_file and isinstance(output_file, str):
            is_pdb_output = output_file.endswith('.pdb') or output_file.endswith('.cif')
        elif metadata.get('artifacts_info'):
            # Check if first artifact is PDB or CIF
            artifacts = metadata['artifacts_info']
            if artifacts and len(artifacts) > 0:
                filename = artifacts[0].get('filename', '')
                is_pdb_output = filename.endswith('.pdb') or filename.endswith('.cif')

        # Special case: Boltz2 outputs CIF structures in JSON format (will be converted to PDB)
        if job.get('model') in ['type-9', 'boltz2', 'type-boltz2'] and metadata.get('boltz2_output'):
            is_pdb_output = True
            # Note: CIF will be converted to PDB on download via download_output endpoint

        # SPECIAL HANDLING FOR EVO2: the result JSON holds the generated bases and
        # per-base sampling probabilities; build_evo2_view shapes it for the page.
        evo2_view = None
        if job.get('model') in ('evo2', 'type-8'):
            output_file = job.get('output_file')
            if output_file and os.path.exists(output_file):
                try:
                    with open(output_file, 'r') as f:
                        evo2_view = build_evo2_view(json.load(f), job.get('sequence'))
                except Exception as e:
                    logger.error(f"Error reading Evo2 output file: {e}")

        # SPECIAL HANDLING FOR NIM STRUCTURE MODELS: scan artifacts and validate files
        NIM_STRUCTURE_MODELS = {'openfold3', 'esmfold-re', 'alphafold2-re', 'alphafold2-re-multimer', 'diffdock-re', 'proteinmpnn-re'}
        if job.get('model') in NIM_STRUCTURE_MODELS:
            if job_output_dir and os.path.exists(job_output_dir):
                try:
                    valid_artifacts = []
                    existing_filenames = set()
                    for a in artifacts_info:
                        fname = a.get('filename', '')
                        full_path = os.path.join(job_output_dir, fname)
                        if fname and os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                            a['path'] = full_path
                            a['size'] = os.path.getsize(full_path)
                            valid_artifacts.append(a)
                            existing_filenames.add(fname)
                        elif not fname:
                            valid_artifacts.append(a)
                    # Scan for any .pdb/.cif/.fasta/.sdf files not already listed
                    for fname in os.listdir(job_output_dir):
                        if fname in existing_filenames:
                            continue
                        ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
                        if ext in ('pdb', 'cif', 'fasta', 'fa', 'sdf', 'mol', 'mol2'):
                            full_path = os.path.join(job_output_dir, fname)
                            if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                                artifact_entry = {
                                    'filename': fname,
                                    'display_name': fname,
                                    'path': full_path,
                                    'size': os.path.getsize(full_path),
                                }
                                # Set type for ligand files so template/viewer handle them correctly
                                if ext in ('sdf', 'mol', 'mol2'):
                                    artifact_entry['type'] = 'ligand'
                                    if 'ligand' in fname.lower():
                                        artifact_entry['display_name'] = 'Ligand (SDF)'
                                valid_artifacts.append(artifact_entry)
                    if valid_artifacts:
                        artifacts_info = valid_artifacts
                        metadata['artifacts_info'] = artifacts_info
                        # Set is_pdb_output if any PDB/CIF found
                        structure_exts = {'.pdb', '.cif'}
                        if any(a.get('filename', '').lower().endswith(tuple(structure_exts)) for a in artifacts_info):
                            is_pdb_output = True
                    # Extract pLDDT scores from PDB for AlphaFold2-RE / ESMFold-RE / OpenFold3
                    if job.get('model') in ('alphafold2-re', 'alphafold2-re-multimer', 'esmfold-re', 'openfold3') and not metadata.get('plddt_scores'):
                        # First try loading from job metadata JSON (worker may have stored them)
                        output_file = job.get('output_file')
                        plddt_loaded = False
                        if output_file and output_file.endswith('.json') and os.path.exists(output_file):
                            try:
                                with open(output_file, 'r') as f:
                                    meta_json = json.load(f)
                                if meta_json.get('plddt_scores'):
                                    metadata['plddt_scores'] = meta_json['plddt_scores']
                                    metadata['plddt_avg'] = meta_json.get('plddt_avg', round(sum(meta_json['plddt_scores']) / len(meta_json['plddt_scores']), 1))
                                    metadata['plddt_min'] = meta_json.get('plddt_min', round(min(meta_json['plddt_scores']), 1))
                                    metadata['plddt_max'] = meta_json.get('plddt_max', round(max(meta_json['plddt_scores']), 1))
                                    plddt_loaded = True
                                    logger.info(f"[NIM] Loaded pLDDT from JSON metadata, avg={metadata['plddt_avg']}")
                            except Exception as e:
                                logger.debug(f"[NIM] Could not load pLDDT from JSON: {e}")

                        # Fallback: extract from the structure file. OpenFold3 defaults to
                        # mmCIF output, so we must handle .cif (ModelCIF QA table / B-factors)
                        # and not only .pdb — otherwise plddt_avg stays empty for OF3 jobs.
                        if not plddt_loaded:
                            struct_file = None
                            for a in artifacts_info:
                                fname = a.get('filename', '')
                                if fname.endswith('.pdb') and not fname.endswith('_input_protein.pdb'):
                                    struct_file = a.get('path') or os.path.join(job_output_dir, fname)
                                    break
                            if not struct_file:
                                for a in artifacts_info:
                                    fname = a.get('filename', '')
                                    if fname.endswith('.cif') and not fname.endswith('_input_protein.cif'):
                                        struct_file = a.get('path') or os.path.join(job_output_dir, fname)
                                        break
                            if struct_file and os.path.exists(struct_file):
                                try:
                                    plddt_scores = []
                                    seen = set()
                                    if struct_file.endswith('.cif'):
                                        qa_only = (job.get('model') == 'openfold3')
                                        plddt_scores = _extract_plddt_from_mmcif(struct_file, qa_only=qa_only)
                                    else:
                                        with open(struct_file, 'r') as f:
                                            for line in f:
                                                if line.startswith('ATOM') and len(line) > 60:
                                                    atom_name = line[12:16].strip()
                                                    if atom_name == 'CA':
                                                        res_num = line[22:26].strip()
                                                        chain = line[21:22].strip()
                                                        key = (chain, res_num)
                                                        if key not in seen:
                                                            try:
                                                                b = float(line[60:66].strip())
                                                                plddt_scores.append(b)
                                                                seen.add(key)
                                                            except (ValueError, IndexError):
                                                                pass
                                    if plddt_scores:
                                        metadata['plddt_scores'] = plddt_scores
                                        metadata['plddt_avg'] = round(sum(plddt_scores) / len(plddt_scores), 1)
                                        metadata['plddt_min'] = round(min(plddt_scores), 1)
                                        metadata['plddt_max'] = round(max(plddt_scores), 1)
                                        logger.info(f"[NIM] Extracted {len(plddt_scores)} pLDDT from {os.path.basename(struct_file)}, avg={metadata['plddt_avg']}")
                                except Exception as e:
                                    logger.error(f"[NIM] Error extracting pLDDT from structure: {e}")

                    # ProteinMPNN-RE: save input PDB and parse FASTA sequences
                    if job.get('model') == 'proteinmpnn-re':
                        # Save input PDB from job sequence field (PDB content stored at submission)
                        input_pdb_content = job.get('sequence', '')
                        if input_pdb_content and 'ATOM' in input_pdb_content:
                            input_pdb_path = os.path.join(job_output_dir, '_input_protein.pdb')
                            if not os.path.exists(input_pdb_path):
                                try:
                                    with open(input_pdb_path, 'w') as f:
                                        f.write(input_pdb_content)
                                    artifacts_info.append({
                                        'filename': '_input_protein.pdb',
                                        'display_name': 'Input Structure (PDB)',
                                        'path': input_pdb_path,
                                        'size': os.path.getsize(input_pdb_path),
                                        'type': 'input',
                                    })
                                    metadata['input_pdb_filename'] = '_input_protein.pdb'
                                    is_pdb_output = True
                                    logger.info(f"[ProteinMPNN-RE] Saved input PDB for job {job.get('job_id')}")
                                except Exception as e:
                                    logger.error(f"[ProteinMPNN-RE] Error saving input PDB: {e}")
                            else:
                                metadata['input_pdb_filename'] = '_input_protein.pdb'
                                is_pdb_output = True

                        # Parse FASTA output sequences for template display
                        fasta_sequences = []
                        for a in artifacts_info:
                            fname = a.get('filename', '')
                            if fname.lower().endswith(('.fasta', '.fa')):
                                fasta_path = a.get('path') or os.path.join(job_output_dir, fname)
                                if os.path.exists(fasta_path):
                                    try:
                                        with open(fasta_path, 'r') as f:
                                            fasta_text = f.read()
                                        current_seq = None
                                        for line in fasta_text.split('\n'):
                                            line = line.strip()
                                            if line.startswith('>'):
                                                if current_seq:
                                                    fasta_sequences.append(current_seq)
                                                current_seq = {'header': line[1:], 'sequence': ''}
                                            elif current_seq and line:
                                                current_seq['sequence'] += line
                                        if current_seq:
                                            fasta_sequences.append(current_seq)
                                        logger.info(f"[ProteinMPNN-RE] Parsed {len(fasta_sequences)} sequences from {fname}")
                                    except Exception as e:
                                        logger.error(f"[ProteinMPNN-RE] Error parsing FASTA {fname}: {e}")
                                break
                        if fasta_sequences:
                            metadata['fasta_sequences'] = fasta_sequences
                            metadata['is_fasta_output'] = True

                except Exception as e:
                    logger.error(f"Error scanning NIM model output files: {e}")

                # Build per-sample scores table for OpenFold3 (from stored score arrays)
                if job.get('model') == 'openfold3':
                    conf_scores = metadata.get('confidence_scores', [])
                    ptm_scores = metadata.get('ptm_scores', [])
                    iptm_scores = metadata.get('iptm_scores', [])
                    if conf_scores:
                        samples = []
                        for idx in range(len(conf_scores)):
                            sample = {
                                'confidence': conf_scores[idx] if idx < len(conf_scores) else None,
                                'ptm': ptm_scores[idx] if idx < len(ptm_scores) else None,
                                'i_tm': iptm_scores[idx] if idx < len(iptm_scores) else None,
                            }
                            samples.append(sample)
                        metadata['sample_scores'] = samples
                        logger.info(f"[OpenFold3] Built {len(samples)} sample scores for view output")

                    # Protein + ligand complex: surface a 3-way split in the viewer.
                    # OpenFold3 returns the bound ligand baked into a single combined
                    # structure (HETATM alongside the polymer), so the sidebar used to
                    # list one file and the ligand could be missed. Offer
                    # Complex (default) / Protein only / Ligand (SDF), generating the
                    # split files on the fly for jobs whose worker run never wrote them
                    # (e.g. PDB-output jobs). Idempotent: re-uses files once written.
                    try:
                        from ..helpers.ligand_split import (
                            structure_has_ligand, extract_ligand_sdf, extract_protein_only,
                            _molblock_has_bonds,
                        )

                        def _is_struct(a):
                            return (a.get('filename') or '').lower().endswith(('.pdb', '.cif'))

                        def _is_protein_only(a):
                            fn = (a.get('filename') or '').lower()
                            return a.get('type') == 'protein' or fn.endswith(('_protein.pdb', '_protein.cif'))

                        def _is_ligand_file(a):
                            fn = (a.get('filename') or '').lower()
                            return a.get('type') == 'ligand' or fn.endswith(('.sdf', '.mol', '.mol2'))

                        # The combined/main output = a structure file that is neither the
                        # protein-only split nor an input; prefer the declared main output.
                        main_basename = os.path.basename(job.get('output_file') or '')
                        combined = None
                        for a in artifacts_info:
                            if not _is_struct(a) or _is_protein_only(a) or a.get('type') == 'input':
                                continue
                            if a.get('filename') == main_basename:
                                combined = a
                                break
                            if combined is None:
                                combined = a

                        if combined is not None:
                            combined_path = combined.get('path') or os.path.join(job_output_dir, combined['filename'])
                            combined_fmt = 'cif' if combined['filename'].lower().endswith('.cif') else 'pdb'
                            try:
                                with open(combined_path, 'r', encoding='utf-8', errors='ignore') as _fh:
                                    combined_text = _fh.read()
                            except OSError:
                                combined_text = ''

                            if combined_text and structure_has_ligand(combined_text, combined_fmt):
                                metadata['has_embedded_ligand'] = True

                                # Display names: <job last-4> + role + format, e.g.
                                # "21f5 Complex (PDB)" / "21f5 Protein (PDB)" / "21f5 Ligand (SDF)".
                                last4 = (str(job_id or '') or combined['filename'])[-4:]

                                def _fmt_label(fn):
                                    f = (fn or '').lower()
                                    if f.endswith('.cif'):
                                        return 'CIF'
                                    if f.endswith(('.sdf', '.mol', '.mol2')):
                                        return 'SDF'
                                    return 'PDB'

                                combined['display_name'] = f'{last4} Complex ({_fmt_label(combined["filename"])})'
                                combined['type'] = combined.get('type') or 'output'
                                base = os.path.splitext(combined['filename'])[0]

                                # Ligand SDF: generate if absent, OR re-bond an existing
                                # one that has zero bonds. Ligand HETATM records carry no
                                # connectivity, so a naively-extracted SDF can render as a
                                # cloud of loose atoms — heal it from the combined structure
                                # regardless of which code wrote it.
                                existing_ligs = [a for a in artifacts_info if _is_ligand_file(a)]
                                if not existing_ligs:
                                    sdf = extract_ligand_sdf(combined_text, combined_fmt)
                                    if sdf:
                                        name = f'{base}_ligand.sdf'
                                        p = os.path.join(job_output_dir, name)
                                        if not os.path.exists(p):
                                            with open(p, 'w', encoding='utf-8') as _fh:
                                                _fh.write(sdf)
                                        artifacts_info.append({
                                            'filename': name, 'display_name': f'{last4} Ligand (SDF)',
                                            'path': p, 'size': os.path.getsize(p), 'type': 'ligand',
                                        })
                                else:
                                    for a in existing_ligs:
                                        ap = a.get('path') or os.path.join(job_output_dir, a.get('filename') or '')
                                        try:
                                            if not ap or not os.path.exists(ap):
                                                continue
                                            with open(ap, 'r', encoding='utf-8', errors='ignore') as _fh:
                                                cur = _fh.read()
                                            if _molblock_has_bonds(cur):
                                                continue
                                            fixed = extract_ligand_sdf(combined_text, combined_fmt)
                                            if fixed and _molblock_has_bonds(fixed):
                                                with open(ap, 'w', encoding='utf-8') as _fh:
                                                    _fh.write(fixed)
                                                a['size'] = os.path.getsize(ap)
                                                logger.info(f"[OpenFold3] re-bonded zero-bond ligand SDF {os.path.basename(ap)} for job {job_id}")
                                        except OSError:
                                            pass

                                # Protein only (only if not already present)
                                if not any(_is_protein_only(a) for a in artifacts_info):
                                    prot = extract_protein_only(combined_text, combined_fmt)
                                    if prot:
                                        name = f'{base}_protein.{combined_fmt}'
                                        p = os.path.join(job_output_dir, name)
                                        if not os.path.exists(p):
                                            with open(p, 'w', encoding='utf-8') as _fh:
                                                _fh.write(prot)
                                        artifacts_info.append({
                                            'filename': name, 'display_name': f'{last4} Protein ({_fmt_label(name)})',
                                            'path': p, 'size': os.path.getsize(p), 'type': 'protein',
                                        })

                                # Normalize split labels/types (idempotent across views — always
                                # overwrite so the <job last-4> naming is consistent).
                                for a in artifacts_info:
                                    if a is combined:
                                        continue
                                    if _is_protein_only(a):
                                        a['type'] = 'protein'
                                        a['display_name'] = f'{last4} Protein ({_fmt_label(a.get("filename"))})'
                                    elif _is_ligand_file(a):
                                        a['type'] = 'ligand'
                                        a['display_name'] = f'{last4} Ligand (SDF)'

                                # Order so the Complex loads first (default-checked),
                                # then protein-only; ligand SDFs are a separate sidebar
                                # section so their order here does not matter.
                                def _rank(a):
                                    if not _is_struct(a):
                                        return 2
                                    return 1 if _is_protein_only(a) else 0
                                artifacts_info.sort(key=_rank)
                                metadata['artifacts_info'] = artifacts_info
                                logger.info(f"[OpenFold3] protein/ligand/complex split ready for job {job_id}")
                    except Exception as _split_err:
                        logger.warning(f"[OpenFold3] Could not build protein/ligand split: {_split_err}")

        # SPECIAL HANDLING FOR MOLMIM: Scan output directory for files
        if is_molmim_job and job_output_dir and os.path.exists(job_output_dir):
            try:
                existing_filenames = {a.get('filename') for a in artifacts_info if a.get('filename')}
                molmim_extensions = ('.json', '.smi', '.csv', '.sdf', '.mol', '.mol2', '.txt', '.log')
                for filename in os.listdir(job_output_dir):
                    if filename in existing_filenames:
                        continue
                    if filename.lower().endswith(molmim_extensions):
                        file_path = os.path.join(job_output_dir, filename)
                        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                            file_size = os.path.getsize(file_path)
                            # Determine display type
                            fl = filename.lower()
                            if fl.endswith(('.sdf', '.mol', '.mol2')):
                                dtype = 'ligand'
                                display = filename
                            elif fl.endswith('.json'):
                                dtype = 'output'
                                display = 'MolMIM Output (JSON)'
                            elif fl.endswith('.smi'):
                                dtype = 'output'
                                display = 'SMILES Output'
                            elif fl.endswith('.csv'):
                                dtype = 'output'
                                display = 'Results (CSV)'
                            else:
                                dtype = 'output'
                                display = filename
                            artifacts_info.append({
                                'name': filename,
                                'filename': filename,
                                'display_name': display,
                                'size': file_size,
                                'path': file_path,
                                'type': dtype,
                            })
                            existing_filenames.add(filename)
                if artifacts_info:
                    logger.info(f"[MolMIM] Found {len(artifacts_info)} output files for job {job.get('job_id', 'unknown')}")
                metadata['artifacts_info'] = artifacts_info
            except Exception as e:
                logger.error(f"[MolMIM] Error scanning output files: {e}")

        # SPECIAL HANDLING FOR MOLMIM: Read generated molecules output
        molmim_result = None
        if is_molmim_job:
            output_file = job.get('output_file')
            if output_file and os.path.exists(output_file):
                try:
                    with open(output_file, 'r', encoding='utf-8') as f:
                        raw_output = f.read()

                    def _normalize_molecules(molecules_raw):
                        if isinstance(molecules_raw, str):
                            try:
                                molecules_raw = json.loads(molecules_raw)
                            except json.JSONDecodeError:
                                molecules_raw = []

                        if not isinstance(molecules_raw, list):
                            return []

                        normalized = []
                        for entry in molecules_raw:
                            if isinstance(entry, dict):
                                smiles_value = (
                                    entry.get('sample')
                                    or entry.get('smiles')
                                    or entry.get('molecule')
                                    or ''
                                )
                                if not smiles_value:
                                    continue

                                score_value = entry.get('score')
                                if score_value is None:
                                    score_value = entry.get('property_score')
                                try:
                                    score_value = float(score_value) if score_value is not None else None
                                    if score_value is not None and not math.isfinite(score_value):
                                        score_value = None
                                except (TypeError, ValueError):
                                    score_value = None

                                normalized.append({
                                    'smiles': str(smiles_value).strip(),
                                    'score': score_value
                                })
                                continue

                            smiles_value = str(entry).strip() if entry is not None else ''
                            if smiles_value:
                                normalized.append({'smiles': smiles_value, 'score': None})
                        return normalized

                    def _parse_smiles_text(raw_text: str):
                        molecules = []
                        if not raw_text:
                            return molecules

                        for line in raw_text.splitlines():
                            stripped = line.strip()
                            if not stripped:
                                continue
                            if stripped.startswith('#'):
                                continue
                            lower_stripped = stripped.lower()
                            if lower_stripped.startswith('smiles'):
                                continue
                            if 'smiles' in lower_stripped and (',' in stripped or '\t' in stripped or ';' in stripped):
                                continue

                            tokens = (
                                stripped
                                .replace(',', ' ')
                                .replace('\t', ' ')
                                .replace(';', ' ')
                                .split()
                            )
                            if not tokens:
                                continue

                            smiles_value = tokens[0].strip('"').strip("'")
                            if not smiles_value:
                                continue

                            score_value = None
                            for token in tokens[1:4]:
                                try:
                                    score_value = float(token)
                                    if not math.isfinite(score_value):
                                        score_value = None
                                    break
                                except (TypeError, ValueError):
                                    continue

                            molecules.append({'smiles': smiles_value, 'score': score_value})

                        return molecules

                    parsed_output = None
                    try:
                        parsed_output = json.loads(raw_output)
                    except json.JSONDecodeError:
                        parsed_output = None

                    if isinstance(parsed_output, dict):
                        parsed_output['molecules'] = _normalize_molecules(parsed_output.get('molecules'))
                        molmim_result = parsed_output
                    elif isinstance(parsed_output, list):
                        molmim_result = {
                            'molecules': _normalize_molecules(parsed_output),
                            'source_format': 'json_list'
                        }
                    else:
                        fallback_molecules = _parse_smiles_text(raw_output)
                        if fallback_molecules:
                            molmim_result = {
                                'molecules': fallback_molecules,
                                'source_format': 'smiles_text'
                            }
                        else:
                            molmim_result = {'molecules': [], 'raw_output': raw_output}
                except Exception as e:
                    logger.error(f"Error reading MolMIM output file for job {job_id}: {e}")
                    molmim_result = {'molecules': [], 'parse_error': str(e)}

        # SPECIAL HANDLING FOR BOLTZ2: Load structure and metrics from JSON output
        if job.get('model') in ['type-9', 'boltz2', 'type-boltz2']:
            logger.info(f"[Boltz2] Loading output for job {job_id}, model: {job.get('model')}")
            output_file = job.get('output_file')
            logger.info(f"[Boltz2] Output file: {output_file}")
            
            # Load main JSON output if exists
            if output_file and os.path.exists(output_file):
                try:
                    with open(output_file, 'r') as f:
                        boltz2_data = json.load(f)
                    metadata['boltz2_output'] = boltz2_data
                    logger.info(f"[Boltz2] Loaded output with keys: {list(boltz2_data.keys()) if boltz2_data else 'None'}")
                    logger.info(f"[Boltz2] Confidence: {boltz2_data.get('confidence_scores', 'N/A')}")

                    # Populate top-level metadata keys for template fallback
                    if boltz2_data:
                        metadata['confidence_scores'] = boltz2_data.get('confidence_scores', [])
                        metadata['ptm_scores'] = boltz2_data.get('ptm_scores', [])
                        metadata['iptm_scores'] = boltz2_data.get('iptm_scores', [])
                        metadata['complex_plddt_scores'] = boltz2_data.get('complex_plddt_scores', [])
                        metadata['plddt_scores'] = boltz2_data.get('plddt_scores', [])
                        metadata['affinities'] = boltz2_data.get('affinities', {})
                        metadata['plddt_avg'] = boltz2_data.get('plddt_avg')
                        metadata['plddt_min'] = boltz2_data.get('plddt_min')
                        metadata['plddt_max'] = boltz2_data.get('plddt_max')
                        metadata['num_models'] = boltz2_data.get('num_models')
                        metadata['has_ligand'] = boltz2_data.get('has_ligand')
                        metadata['diffusion_samples_requested'] = boltz2_data.get('diffusion_samples_requested')
                        metadata['runtime_metrics'] = boltz2_data.get('runtime_metrics')
                        metadata['predict_affinity'] = boltz2_data.get('predict_affinity')
                        metadata['recycling_steps'] = boltz2_data.get('recycling_steps')
                        metadata['sampling_steps'] = boltz2_data.get('sampling_steps')
                        metadata['step_scale'] = boltz2_data.get('step_scale')
                        metadata['ligand_input_type'] = boltz2_data.get('ligand_input_type')
                        metadata['ligand_smiles'] = boltz2_data.get('ligand_smiles')
                        metadata['ligand_ccd'] = boltz2_data.get('ligand_ccd')

                        # Flag PAE matrix availability. The matrix is stored as a
                        # separate gzip artifact ({job_id}_pae.json.gz) and fetched
                        # lazily by the frontend; trust the worker-provided flags
                        # (persisted in job metadata) rather than the inline JSON.
                        if boltz2_data.get('has_pae_matrix') or metadata.get('has_pae_matrix'):
                            metadata['has_pae_matrix'] = True
                            metadata['pae_matrix_size'] = (
                                boltz2_data.get('pae_matrix_size')
                                or metadata.get('pae_matrix_size')
                                or 0
                            )
                        logger.info(f"[Boltz2] Populated top-level metrics for template")
                except Exception as e:
                    logger.error(f"[Boltz2] Error loading output: {e}")
                    metadata['boltz2_output'] = {}
            else:
                logger.warning(f"[Boltz2] Output file not found or doesn't exist: {output_file}")
                metadata['boltz2_output'] = {}

            # NEW: Scan for multiple output artifacts (.cif, .pdb, .sdf files)
            if job_output_dir and os.path.exists(job_output_dir):
                try:
                    import glob
                    artifacts_info = metadata.get('artifacts_info', [])

                    # Normalize existing artifacts from stored metadata by attaching absolute paths.
                    # Older/worker-provided artifacts_info entries may only include filename/display_name.
                    valid_artifacts = []
                    existing_files = set()
                    for artifact in artifacts_info:
                        filename = artifact.get('filename')
                        if not filename:
                            valid_artifacts.append(artifact)
                            continue

                        full_path = artifact.get('path') or os.path.join(job_output_dir, filename)
                        if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                            artifact['path'] = full_path
                            artifact['size'] = os.path.getsize(full_path)
                            valid_artifacts.append(artifact)
                            existing_files.add(filename)

                    artifacts_info = valid_artifacts

                    # Find all CIF files
                    cif_files = glob.glob(os.path.join(job_output_dir, "*.cif"))
                    # Also look for PDB files just in case
                    pdb_files = glob.glob(os.path.join(job_output_dir, "*.pdb"))
                    # Also scan for ligand SDF files (protein + ligand predictions)
                    sdf_files = glob.glob(os.path.join(job_output_dir, "*.sdf"))

                    # Detect the compressed PAE matrix artifact (fallback if the worker
                    # metadata flag was not persisted, e.g. for deduplicated jobs).
                    if glob.glob(os.path.join(job_output_dir, "*_pae.json.gz")):
                        metadata['has_pae_matrix'] = True
                        metadata.setdefault('pae_matrix_size', metadata.get('pae_matrix_size', 0))

                    all_structure_files = cif_files + pdb_files + sdf_files
                    
                    for file_path in all_structure_files:
                        filename = os.path.basename(file_path)
                        if filename in existing_files:
                            continue
                            
                        file_size = os.path.getsize(file_path)
                        
                        # Determine display name
                        display_name = filename
                        artifact_type = 'structure'

                        # Handle SDF ligand files
                        if filename.endswith('.sdf'):
                            artifact_type = 'ligand'
                            if 'ligand' in filename.lower():
                                # Format: {job_id}_ligand.sdf or {job_id}_ligand_model_0.sdf
                                if 'model_' in filename:
                                    import re
                                    match = re.search(r'model_(\d+)', filename)
                                    if match:
                                        display_name = f"Ligand Model {int(match.group(1)) + 1}"
                                else:
                                    display_name = "Ligand (SDF)"
                            else:
                                display_name = "Ligand"
                        elif "ranked" in filename:
                            parts = filename.split('_')
                            try:
                                # extract rank number assuming format like ranked_0.cif
                                for part in parts:
                                    if part.isdigit():
                                        display_name = f"Rank {part}"
                                        break
                            except:
                                pass

                        artifacts_info.append({
                            'name': filename,
                            'filename': filename,
                            'display_name': display_name,
                            'size': file_size,
                            'path': file_path,
                            'type': artifact_type
                        })
                    
                    # Sort artifacts (structures first, then ligands)
                    def sort_key(x):
                        name = x['filename']
                        artifact_type = x.get('type', 'structure')

                        # Structures/proteins come first (0-499), ligands after (500+)
                        base = 500 if artifact_type == 'ligand' else 0

                        if "ranked" in name:
                            # Extract number for correct sorting (0, 1, 2, 10)
                            import re
                            match = re.search(r'ranked_(\d+)', name)
                            if match:
                                return base + int(match.group(1))
                        elif "model_" in name:
                            # Handle model_0, model_1, etc.
                            import re
                            match = re.search(r'model_(\d+)', name)
                            if match:
                                return base + int(match.group(1))

                        return base + 99  # Put non-ranked/non-model at end of their category
                        
                    artifacts_info.sort(key=sort_key)
                    
                    if artifacts_info:
                        metadata['artifacts_info'] = artifacts_info
                        logger.info(f"[Boltz2] Populated {len(artifacts_info)} artifacts for UI")
                        
                    # Extract Input SMILES if available
                    metadata_file = os.path.join(job_output_dir, "job_metadata.json")
                    if os.path.exists(metadata_file):
                        try:
                            with open(metadata_file, 'r') as f:
                                job_meta = json.load(f)
                                # Check common fields for ligand input
                                if job_meta.get('ligand_smiles'):
                                    metadata['input_smiles'] = job_meta['ligand_smiles']
                                elif job_meta.get('smiles'):
                                    metadata['input_smiles'] = job_meta['smiles']
                                elif job_meta.get('ligand') and isinstance(job_meta['ligand'], str) and len(job_meta['ligand']) < 200:
                                    # Heuristic: simple string might be SMILES or CCD
                                    metadata['input_smiles'] = job_meta['ligand']
                        except Exception as e:
                            logger.warning(f"[Boltz2] Failed to extract metadata inputs: {e}")

                    # Extract per-residue pLDDT and sequence from CIF file
                    try:
                        cif_file = None
                        # Find the main CIF structure file (prefer non-protein-only)
                        for artifact in artifacts_info:
                            fname = artifact.get('filename', '')
                            if fname.endswith('.cif') and not fname.endswith('_protein.cif'):
                                candidate_path = artifact.get('path') or os.path.join(job_output_dir, fname)
                                if candidate_path and os.path.exists(candidate_path):
                                    cif_file = candidate_path
                                    break
                        # Fallback to protein CIF if main not found
                        if not cif_file:
                            for artifact in artifacts_info:
                                fname = artifact.get('filename', '')
                                if fname.endswith('.cif'):
                                    candidate_path = artifact.get('path') or os.path.join(job_output_dir, fname)
                                    if candidate_path and os.path.exists(candidate_path):
                                        cif_file = candidate_path
                                        break

                        if cif_file and os.path.exists(cif_file):
                            # Three-letter to one-letter amino acid mapping
                            AA_MAP = {
                                'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C',
                                'GLN': 'Q', 'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I',
                                'LEU': 'L', 'LYS': 'K', 'MET': 'M', 'PHE': 'F', 'PRO': 'P',
                                'SER': 'S', 'THR': 'T', 'TRP': 'W', 'TYR': 'Y', 'VAL': 'V',
                                'MSE': 'M', 'SEC': 'U', 'PYL': 'O',
                                'DA': 'A', 'DC': 'C', 'DG': 'G', 'DT': 'T', 'DU': 'U',
                                'A': 'A', 'C': 'C', 'G': 'G', 'T': 'T', 'U': 'U'
                            }

                            plddt_scores = []
                            residue_info = []  # List of dict(chain, res_num, res_name, b_factor)
                            seen_residues = set()

                            with open(cif_file, 'r') as f:
                                content = f.read()

                            lines = content.split('\n')

                            # Strategy 1: Preferred source in ModelCIF files.
                            qa_scores = []
                            qa_headers = {}
                            in_qa_loop = False
                            for raw_line in lines:
                                line = raw_line.strip()
                                if not line:
                                    continue

                                if line.startswith('loop_'):
                                    in_qa_loop = False
                                    qa_headers = {}
                                    continue

                                if line.startswith('_ma_qa_metric_local.'):
                                    in_qa_loop = True
                                    header_name = line.split('.', 1)[1].strip()
                                    qa_headers[header_name] = len(qa_headers)
                                    continue

                                if not in_qa_loop:
                                    continue

                                if line.startswith('#'):
                                    in_qa_loop = False
                                    continue

                                if line.startswith('_'):
                                    continue

                                parts = line.split()
                                if len(parts) < len(qa_headers):
                                    continue

                                metric_value_idx = qa_headers.get('metric_value')
                                if metric_value_idx is None:
                                    continue

                                try:
                                    qa_scores.append(float(parts[metric_value_idx]))
                                except (TypeError, ValueError, IndexError):
                                    continue

                            if qa_scores:
                                if max(qa_scores) <= 1.05:
                                    qa_scores = [score * 100 for score in qa_scores]
                                plddt_scores = [max(0.0, min(100.0, float(score))) for score in qa_scores]
                                logger.info(f"[Boltz2] Extracted {len(plddt_scores)} pLDDT scores from QA metrics")

                            # Strategy 2: Fallback to _atom_site loop (supports proteins + nucleic acids).
                            if not plddt_scores:
                                atom_headers = {}
                                in_atom_loop = False

                                def get_atom_col(parts, names, default=''):
                                    for name in names:
                                        idx = atom_headers.get(name)
                                        if idx is not None and idx < len(parts):
                                            return parts[idx]
                                    return default

                                for raw_line in lines:
                                    line = raw_line.strip()
                                    if not line:
                                        continue

                                    if line.startswith('loop_'):
                                        in_atom_loop = False
                                        atom_headers = {}
                                        continue

                                    if line.startswith('_atom_site.'):
                                        in_atom_loop = True
                                        header_name = line.split('.', 1)[1].strip()
                                        atom_headers[header_name] = len(atom_headers)
                                        continue

                                    if not in_atom_loop:
                                        continue

                                    if line.startswith('#'):
                                        in_atom_loop = False
                                        continue

                                    if line.startswith('_'):
                                        continue

                                    parts = line.split()
                                    if len(parts) < len(atom_headers):
                                        continue

                                    group = get_atom_col(parts, ['group_PDB'], 'ATOM')
                                    if group != 'ATOM':
                                        continue

                                    atom_name = get_atom_col(parts, ['label_atom_id', 'auth_atom_id'])
                                    if atom_name not in {'CA', 'P', "C4'"}:
                                        continue

                                    res_name = get_atom_col(parts, ['label_comp_id', 'auth_comp_id'])
                                    chain = get_atom_col(parts, ['label_asym_id', 'auth_asym_id'])
                                    res_num = get_atom_col(parts, ['label_seq_id', 'auth_seq_id'])
                                    if not res_num:
                                        continue

                                    res_key = (chain, res_num)
                                    if res_key in seen_residues:
                                        continue

                                    b_factor_raw = get_atom_col(parts, ['B_iso_or_equiv'])
                                    try:
                                        b_factor = float(b_factor_raw)
                                    except (TypeError, ValueError):
                                        b_factor = None

                                    if b_factor is None:
                                        continue

                                    if 0 <= b_factor <= 1.05:
                                        b_factor *= 100
                                    b_factor = max(0.0, min(100.0, b_factor))

                                    residue_info.append({
                                        'chain': chain,
                                        'res_num': res_num,
                                        'res_name': res_name,
                                        'b_factor': b_factor
                                    })
                                    seen_residues.add(res_key)

                                if residue_info:
                                    plddt_scores = [residue['b_factor'] for residue in residue_info]
                                    logger.info(
                                        f"[Boltz2] Extracted {len(plddt_scores)} pLDDT scores from atom-site B-factors"
                                    )

                            # Strategy 3: Last-resort parser for non-standard ATOM line exports.
                            if not plddt_scores:
                                current_key = None
                                for raw_line in lines:
                                    line = raw_line.strip()
                                    if not line.startswith('ATOM'):
                                        continue

                                    parts = line.split()
                                    if len(parts) < 10:
                                        continue

                                    try:
                                        atom_name = parts[3] if len(parts) > 3 else ''
                                        if atom_name not in {'CA', 'P', "C4'"}:
                                            continue

                                        res_name = parts[5] if len(parts) > 5 else ''
                                        chain = parts[6] if len(parts) > 6 else ''
                                        res_num = parts[8] if len(parts) > 8 else ''
                                        if not res_num:
                                            continue

                                        b_factor = None
                                        for idx in range(len(parts) - 1, max(8, len(parts) - 6), -1):
                                            try:
                                                candidate = float(parts[idx])
                                            except ValueError:
                                                continue
                                            if 0 <= candidate <= 1.05:
                                                b_factor = candidate * 100
                                                break
                                            if 0 <= candidate <= 100:
                                                b_factor = candidate
                                                break

                                        if b_factor is None:
                                            continue

                                        key = (chain, res_num)
                                        if key == current_key or key in seen_residues:
                                            continue

                                        b_factor = max(0.0, min(100.0, b_factor))
                                        residue_info.append({
                                            'chain': chain,
                                            'res_num': res_num,
                                            'res_name': res_name,
                                            'b_factor': b_factor
                                        })
                                        seen_residues.add(key)
                                        current_key = key
                                    except (TypeError, ValueError, IndexError):
                                        continue

                                if residue_info and not plddt_scores:
                                    plddt_scores = [residue['b_factor'] for residue in residue_info]
                                    logger.info(
                                        f"[Boltz2] Extracted {len(plddt_scores)} pLDDT scores with ATOM fallback parser"
                                    )

                            if plddt_scores:
                                metadata['plddt_scores'] = plddt_scores
                                metadata['plddt_avg'] = round(sum(plddt_scores) / len(plddt_scores), 1)
                                metadata['plddt_min'] = round(min(plddt_scores), 1)
                                metadata['plddt_max'] = round(max(plddt_scores), 1)

                                # Extract sequence if not already in job
                                if not job.get('sequence') and residue_info:
                                    sequence = ''
                                    for residue in residue_info:
                                        aa = AA_MAP.get(residue['res_name'].upper(), 'X')
                                        sequence += aa
                                    if sequence and 'X' not in sequence[:10]:  # Sanity check
                                        metadata['extracted_sequence'] = sequence
                                        logger.info(f"[Boltz2] Extracted sequence of length {len(sequence)}")

                                logger.info(f"[Boltz2] Extracted {len(plddt_scores)} pLDDT scores from CIF, avg={metadata['plddt_avg']}")

                    except Exception as e:
                        logger.error(f"[Boltz2] Error extracting pLDDT/sequence from CIF: {e}")

                except Exception as e:
                    logger.error(f"[Boltz2] Error scanning for artifacts: {e}")

        # SPECIAL HANDLING FOR OPENFOLD3: Load confidence scores and metadata from JSON output
        if job.get('model') == 'openfold3':
            logger.info(f"[OpenFold3] Loading output for job {job_id}")
            output_file = job.get('output_file')
            if output_file and os.path.exists(output_file) and output_file.endswith('.json'):
                try:
                    with open(output_file, 'r') as f:
                        of3_data = json.load(f)
                    metadata['openfold3_output'] = of3_data
                    logger.info(f"[OpenFold3] Loaded output with keys: {list(of3_data.keys()) if of3_data else 'None'}")
                    if of3_data:
                        metadata['confidence_scores'] = of3_data.get('confidence_scores', [])
                        metadata['ptm_scores'] = of3_data.get('ptm_scores', [])
                        metadata['iptm_scores'] = of3_data.get('iptm_scores', [])
                        metadata['num_samples'] = of3_data.get('num_samples', 0)
                        metadata['input_id'] = of3_data.get('input_id')
                        metadata['confidence_score'] = of3_data.get('confidence_score')
                        metadata['ptm_score'] = of3_data.get('ptm_score')
                        metadata['iptm_score'] = of3_data.get('iptm_score')
                        logger.info(f"[OpenFold3] Populated top-level scores: confidence={metadata['confidence_score']}")
                except Exception as e:
                    logger.error(f"[OpenFold3] Error loading output: {e}")
                    metadata['openfold3_output'] = {}
            # Scan for multiple diffusion sample artifacts if not already present
            if job_output_dir and os.path.exists(job_output_dir) and not metadata.get('artifacts_info'):
                try:
                    import glob
                    cif_files = sorted(glob.glob(os.path.join(job_output_dir, "*_sample*.cif")))
                    pdb_files = sorted(glob.glob(os.path.join(job_output_dir, "*_sample*.pdb")))
                    sample_files = cif_files + pdb_files
                    if sample_files:
                        sample_artifacts = []
                        conf_scores = metadata.get('confidence_scores', [])
                        for idx, fpath in enumerate(sample_files):
                            fname = os.path.basename(fpath)
                            conf = conf_scores[idx] if idx < len(conf_scores) else None
                            display = fname
                            if conf is not None:
                                display = f'Sample {idx+1} (conf: {conf:.3f})'
                            sample_artifacts.append({
                                'filename': fname,
                                'display_name': display,
                                'path': fpath,
                                'size': os.path.getsize(fpath),
                                'confidence_score': conf,
                            })
                        metadata['artifacts_info'] = sample_artifacts
                except Exception as e:
                    logger.error(f"[OpenFold3] Error scanning sample files: {e}")

        # SPECIAL HANDLING FOR MSA SEARCH: Parse A3M summary and preview rows for UI rendering.
        if job.get('model') == 'msa-search':
            msa_output_file = job.get('output_file')

            # Resolve from artifacts if output_file is missing.
            if (not msa_output_file or not os.path.exists(msa_output_file)) and artifacts_info:
                for artifact in artifacts_info:
                    filename = str(artifact.get('filename') or '')
                    if filename.lower().endswith('.a3m'):
                        candidate_path = artifact.get('path')
                        if not candidate_path and job_output_dir:
                            candidate_path = os.path.join(job_output_dir, filename)
                        if candidate_path and os.path.exists(candidate_path):
                            msa_output_file = candidate_path
                            break

            # Resolve from project job directory when stored path is stale.
            if (not msa_output_file or not os.path.exists(msa_output_file)) and job_output_dir and os.path.isdir(job_output_dir):
                try:
                    dir_candidates = []
                    for filename in os.listdir(job_output_dir):
                        if filename.lower().endswith('.a3m'):
                            candidate_path = os.path.join(job_output_dir, filename)
                            if os.path.exists(candidate_path):
                                dir_candidates.append((os.path.getmtime(candidate_path), candidate_path))
                    if dir_candidates:
                        dir_candidates.sort(reverse=True)
                        msa_output_file = dir_candidates[0][1]
                except OSError:
                    pass

            # Resolve from temporary MSA directory by job_id prefix.
            if not msa_output_file or not os.path.exists(msa_output_file):
                msa_tmp_dir = '/tmp/msa_search_results'
                if os.path.isdir(msa_tmp_dir):
                    try:
                        tmp_candidates = []
                        for filename in os.listdir(msa_tmp_dir):
                            if filename.startswith(job_id) and filename.lower().endswith('.a3m'):
                                candidate_path = os.path.join(msa_tmp_dir, filename)
                                if os.path.exists(candidate_path):
                                    tmp_candidates.append((os.path.getmtime(candidate_path), candidate_path))
                        if tmp_candidates:
                            tmp_candidates.sort(reverse=True)
                            msa_output_file = tmp_candidates[0][1]
                    except OSError:
                        pass

            msa_view = {
                'file_exists': False,
                'output_file': msa_output_file,
                'output_filename': os.path.basename(msa_output_file) if msa_output_file else None,
                'file_size_bytes': 0,
                'total_sequences': 0,
                'query_header': None,
                'query_length': 0,
                'min_sequence_length': 0,
                'max_sequence_length': 0,
                'avg_sequence_length': 0,
                'preview_limit': 25,
                'preview_rows': [],
                'is_truncated': False,
                'error': None,
            }

            if msa_output_file and os.path.exists(msa_output_file):
                try:
                    msa_view['file_exists'] = True
                    msa_view['file_size_bytes'] = os.path.getsize(msa_output_file)

                    current_header = None
                    current_seq_lines = []
                    sequence_lengths = []
                    preview_rows = []

                    def finalize_record():
                        nonlocal current_header, current_seq_lines, sequence_lengths, preview_rows
                        if current_header is None:
                            return

                        sequence = ''.join(current_seq_lines).strip()
                        current_seq_lines = []
                        if not sequence:
                            return

                        seq_len = len(sequence)
                        sequence_lengths.append(seq_len)
                        seq_index = len(sequence_lengths)

                        if seq_index == 1:
                            msa_view['query_header'] = current_header
                            msa_view['query_length'] = seq_len

                        if len(preview_rows) < msa_view['preview_limit']:
                            lowercase_count = sum(1 for ch in sequence if ch.islower())
                            preview_seq = sequence[:180]
                            if len(sequence) > 180:
                                preview_seq += '...'
                            preview_rows.append({
                                'index': seq_index,
                                'header': current_header,
                                'sequence_preview': preview_seq,
                                'sequence_length': seq_len,
                                'insertions': lowercase_count,
                                'is_query': seq_index == 1,
                            })

                    with open(msa_output_file, 'r', encoding='utf-8', errors='replace') as msa_file:
                        raw_msa_content = msa_file.read()

                    alignment_text = raw_msa_content
                    stripped_content = raw_msa_content.lstrip()
                    if stripped_content.startswith('{'):
                        try:
                            parsed_output = json.loads(raw_msa_content)
                            extracted_alignment = None
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
                                alignment_text = extracted_alignment
                        except json.JSONDecodeError:
                            pass

                    for raw_line in alignment_text.splitlines():
                        line = raw_line.strip()
                        if not line:
                            continue

                        if line.startswith('>'):
                            finalize_record()
                            current_header = line[1:].strip() or f'sequence_{len(sequence_lengths) + 1}'
                        elif current_header is not None:
                            current_seq_lines.append(''.join(line.split()))

                    finalize_record()

                    total_sequences = len(sequence_lengths)
                    msa_view['total_sequences'] = total_sequences
                    msa_view['preview_rows'] = preview_rows
                    msa_view['is_truncated'] = total_sequences > len(preview_rows)

                    if sequence_lengths:
                        msa_view['min_sequence_length'] = min(sequence_lengths)
                        msa_view['max_sequence_length'] = max(sequence_lengths)
                        msa_view['avg_sequence_length'] = round(sum(sequence_lengths) / len(sequence_lengths), 1)
                    else:
                        msa_view['error'] = 'MSA file is present but does not contain sequence records.'
                except Exception as parse_err:
                    msa_view['error'] = f'Unable to parse MSA file: {parse_err}'
                    logger.error(f"[MSA] Failed to parse A3M for job {job_id}: {parse_err}")
            else:
                msa_view['error'] = 'MSA output file not found on server.'

            metadata['msa_view'] = msa_view

        # Attach an individual, job-name-prefixed download filename to every
        # artifact (e.g. "MyDockRun_pose_1.sdf") so the sidebar shows — and the
        # download serves — uniquely named files instead of generic rank1.sdf /
        # ranked_0.pdb that collide across multiple jobs. Mirrors the naming in
        # download_job_artifact (the single source of truth for the served name).
        for _artifact in metadata.get('artifacts_info') or []:
            if isinstance(_artifact, dict) and _artifact.get('filename'):
                _artifact['download_name'] = friendly_artifact_download_name(
                    job.get('name'), job_id, _artifact.get('filename'), _artifact
                )

        response = make_response(render_template(
            'view_output.html',
            job=dict(job),
            project_name=project['name'],
            model_display_name=get_supported_model_name(job.get('model'), short=True),
            metadata=metadata,
            embedding_summary=embedding_summary,
            evo2_view=evo2_view,
            molmim_result=molmim_result,
            is_molmim_job=is_molmim_job,
            summary_error=summary_error,
            is_pdb_output=is_pdb_output,
            format_bytes=format_storage_bytes,
            INPUT_PDB_FILENAME=metadata.get('input_pdb_filename', ''),
        ))
        # Force no caching to ensure latest template is always served
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
        return response
    except Exception as e:
        import traceback
        error_message = f"Error in view_output for job {job_id}: {e}"
        error_traceback = traceback.format_exc()
        logger.error(error_message)
        logger.error(error_traceback)
        
        # Temporary: return error details for debugging
        return jsonify({
            "status": "error",
            "message": "An internal server error occurred during output viewing.",
            "debug_error": error_message,
            "debug_traceback": error_traceback
        }), 500

def _handle_job_submission(project_id: int, payload: dict[str, Any], *,
                           user_id: int = None,
                           skip_credit: bool = False,
                           cost_override: int = None,
                           pipeline_id: str = None,
                           pipeline_stage: int = None,
                           skip_role_check: bool = False,
                           skip_rate_limit: bool = False):
    model_type = payload.get('model')

    # Resolve short-name aliases (e.g. "boltz2", "esmfold") to canonical model
    # ids ("type-9", "type-0"). The notebook SDK and external API both submit
    # using the friendly short_name; internal callers already pass canonical ids.
    # Lookup is case-insensitive and tries underscore<->hyphen variants so
    # callers don't have to memorise the canonical spelling.
    if model_type:
        raw = str(model_type).strip()
        candidates = [raw, raw.lower(), raw.lower().replace('-', '_'), raw.lower().replace('_', '-')]
        aliased = None
        for c in candidates:
            if c in MODEL_TYPE_MAP:
                aliased = MODEL_TYPE_MAP[c]
                break
        if aliased and aliased != raw:
            model_type = aliased
            payload['model'] = model_type

    # Internal pipeline jobs are submitted from the result-listener thread, which
    # has no request/session context; callers pass user_id explicitly there.
    if user_id is None:
        user_id = session['user_id']

    if not model_type:
        return jsonify({'status': 'error', 'message': 'Missing model selection.'}), 400

    model_info = Config.get_model_definition(model_type)
    if not model_info:
        return jsonify({'status': 'error', 'message': 'Unknown model requested.'}), 400

    # Generate job_id early so we can use it for default naming
    job_id = str(uuid.uuid4())

    # Handle job name - use provided name, fallback to model-based default
    job_name = payload.get('name', '').strip()
    if not job_name:
        model_short_name = model_info.get('short_name', model_info.get('name', 'Job'))
        job_name = f"{model_short_name}-{job_id[:8]}"

    db = get_db()

    # Use LEFT JOIN to handle projects without team_id (e.g., forked projects)
    project = db.execute(
        "SELECT p.id, p.collaboration_billing_model, t.creator_id FROM projects p LEFT JOIN teams t ON p.team_id = t.id WHERE p.id = %s",
        (project_id,)
    ).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    project_creator_id = project['creator_id']
    collaboration_billing_model = project['collaboration_billing_model'] or 'collaborator_credits'

    billing_user_id = None
    if collaboration_billing_model == 'admin_credits' and project_creator_id:
        billing_user_id = project_creator_id
    else:  # 'collaborator_credits' or any other default, or no team creator
        billing_user_id = user_id

    is_active, is_visible = get_model_state(db, model_type)
    if not (is_active and is_visible):
        reason = (Config.get_model_definition(model_type) or {}).get('offline_reason')
        return jsonify({'status': 'error', 'message': reason or 'Selected model is currently offline for maintenance.'}), 400
    if not model_info.get('available', True):
        return jsonify({'status': 'error', 'message': 'Selected model is not available yet.'}), 400

    # Model-specific rate limiting (skipped for internal pipeline jobs)
    rate_limit_config = model_info.get('rate_limit')
    if rate_limit_config and not skip_rate_limit:
        max_requests = rate_limit_config.get('max_requests', 10)
        period = rate_limit_config.get('period', 'day')
        scope = rate_limit_config.get('scope', 'user')

        # Calculate time window
        if period == 'day':
            time_window = datetime.utcnow() - timedelta(days=1)
        elif period == 'hour':
            time_window = datetime.utcnow() - timedelta(hours=1)
        else:
            time_window = datetime.utcnow() - timedelta(days=1)

        # Check user's recent job count for this model
        if scope == 'user':
            recent_jobs_count = db.execute(
                'SELECT COUNT(*) as count FROM jobs WHERE creator_id = %s AND model = %s AND created_at >= %s',
                (user_id, model_type, time_window.isoformat())
            ).fetchone()['count']

            if recent_jobs_count >= max_requests:
                model_name = model_info.get('short_name', model_info.get('name', 'This model'))
                return jsonify({
                    'status': 'error',
                    'message': f'{model_name} rate limit exceeded. Maximum {max_requests} requests per {period}. Please try again later.'
                }), 429

    job_task = model_info.get('task') or 'protein_folding'
    supported_inputs = set(model_info.get('input_modes') or [])
    has_sequence_input = 'sequence' in supported_inputs
    has_file_input = 'file' in supported_inputs or 'pdb' in supported_inputs
    if not has_sequence_input and not has_file_input:
        return jsonify({'status': 'error', 'message': 'Selected model is not ready for interactive submissions yet.'}), 400

    if not skip_role_check:
        role = getattr(g, 'current_project_role', None)
        if not role:
            role = get_project_assignment(db, user_id, project_id)
        if not role or role not in JOB_SUBMISSION_ALLOWED_ROLES:
            return jsonify({'status': 'error', 'message': 'You do not have permission to submit jobs to this project.'}), 403

    input_file_id = None
    input_file_row = None
    sequence_value = ''
    has_multimer = False
    using_file_input = False
    job_metadata: dict[str, object] = {
        'node_id': model_info.get('node_id'),
        'task': job_task,
    }
    job_artifacts: dict[str, str] = {}
    params_payload: dict[str, Any] = {}

    # Extract and validate custom model-specific parameters
    custom_params = payload.get('custom_params', {})

    if custom_params and isinstance(custom_params, dict):
        custom_fields_def = model_info.get('custom_fields', [])
        if custom_fields_def:
            # Validate custom params against model definition
            validated_params = {}
            for field_def in custom_fields_def:
                field_name = field_def.get('name')
                if field_name in custom_params:
                    validated_params[field_name] = custom_params[field_name]
                elif field_def.get('default') is not None:
                    validated_params[field_name] = field_def['default']
            # Preserve 'chains' for multimer mode (Boltz2, OpenFold3)
            if 'chains' in custom_params and isinstance(custom_params['chains'], list):
                validated_params['chains'] = custom_params['chains']
            custom_params = validated_params
            job_metadata['custom_params'] = custom_params

    output_extension = model_info.get('output_extension') or '.out'
    output_display_name = None
    data_file_rows: list[Any] = []
    command_args: list[str] = []
    environment: dict[str, str] = {}
    timeout_seconds: int = None

    def _extract_multimer_sequences(chains_value: Any) -> list[str]:
        if not isinstance(chains_value, list):
            return []
        sequences: list[str] = []
        for chain in chains_value:
            if not isinstance(chain, dict):
                continue
            raw = chain.get('sequence')
            if raw is None:
                continue
            seq = str(raw).strip().replace(' ', '').replace('\n', '').replace('\r', '')
            if seq:
                sequences.append(seq)
        return sequences

    def _looks_like_pdb(content: str) -> bool:
        if not isinstance(content, str):
            return False
        return any(
            keyword in content[:500].upper()
            for keyword in ['ATOM  ', 'HETATM', 'HEADER', 'REMARK', 'MODEL ']
        )

    def _has_atom_records(content: str) -> bool:
        if not isinstance(content, str):
            return False
        for line in content.splitlines():
            record_type = line[:6].strip().upper()
            if record_type in {'ATOM', 'HETATM'}:
                return True
        return False

    def _resolve_sequence_molecule_type() -> str:
        molecule_type = ''
        if isinstance(custom_params, dict):
            molecule_type = str(custom_params.get('molecule_type') or '').strip().lower()
        if molecule_type in {'protein', 'dna', 'rna'}:
            return molecule_type
        if model_type in {'evo2', 'type-8'} or job_task in {'dna_sequence_generation', 'dna_generation'}:
            return 'dna'
        if has_sequence_input:
            return 'protein'
        return None

    def _sequence_validation_profile() -> tuple[str, set[str], str]:
        molecule_type = _resolve_sequence_molecule_type() or 'protein'
        if molecule_type == 'dna':
            return molecule_type, set('ACGTN'), 'DNA'
        if molecule_type == 'rna':
            return molecule_type, set('ACGU'), 'RNA'
        return 'protein', set('ACDEFGHIKLMNPQRSTVWY'), 'protein'

    def _load_sequence_from_file(file_path: str) -> str:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as handle:
                raw_contents = handle.read()
        except OSError:
            raise ValueError('Unable to read the selected input file.')

        if not raw_contents or not raw_contents.strip():
            raise ValueError('The selected input file is empty.')

        raw_contents = raw_contents.strip()
        lower_path = file_path.lower()

        structure_suffixes = ('.pdb', '.cif', '.mmcif', '.sdf', '.mol', '.mol2')
        is_structure_file = _looks_like_pdb(raw_contents) or lower_path.endswith(structure_suffixes)

        if is_structure_file:
            return raw_contents

        if 'smiles' in supported_inputs and 'sequence' not in supported_inputs:
            smiles_candidates = []
            for line in raw_contents.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('>'):
                    continue
                if stripped.startswith('#'):
                    continue
                lower_stripped = stripped.lower()
                if lower_stripped.startswith('smiles'):
                    continue
                if 'smiles' in lower_stripped and (',' in stripped or '\t' in stripped or ';' in stripped):
                    continue
                if ',' in stripped or '\t' in stripped or ';' in stripped:
                    token = re.split(r'[,;\t]', stripped)[0].strip()
                    if token:
                        smiles_candidates.append(token)
                else:
                    smiles_candidates.append(stripped)
            if smiles_candidates:
                return smiles_candidates[0]
            return raw_contents

        if has_sequence_input or '>' in raw_contents:
            cleaned = strip_fasta_header(raw_contents)
            return ''.join(cleaned.split())
        return raw_contents

    if job_task == 'python_script_execution':
        if not has_file_input:
            return jsonify({'status': 'error', 'message': 'Selected model requires an uploaded script input.'}), 400

        script_identifier = payload.get('script_file_id') or payload.get('file_id')
        if not script_identifier:
            return jsonify({'status': 'error', 'message': 'A Python script from the project file manager is required.'}), 400
        try:
            input_file_id = int(script_identifier)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'Invalid script file identifier.'}), 400

        # Try to find the file in the regular files table
        script_file_row = db.execute(
            'SELECT id, project_id, path, name FROM files WHERE id = %s',
            (input_file_id,),
        ).fetchone()

        # If not found, check if it's a job output file
        if not script_file_row:
            job_output_row = db.execute(
                'SELECT job_id as id, project_id, output_file as path, output_display_name as name FROM jobs WHERE job_id = %s AND output_file IS NOT NULL',
                (input_file_id,),
            ).fetchone()
            if job_output_row:
                script_file_row = dict(job_output_row)
                if not script_file_row['name']:
                    script_file_row['name'] = os.path.basename(script_file_row['path']) if script_file_row['path'] else 'output'

        if not script_file_row or script_file_row['project_id'] != project_id:
            return jsonify({'status': 'error', 'message': 'Script file not found for this project.'}), 404
        script_path = script_file_row['path']
        if not script_path or not os.path.exists(script_path):
            return jsonify({'status': 'error', 'message': 'The selected script file is missing on the server.'}), 404
        if not script_file_row['name'].lower().endswith('.py'):
            return jsonify({'status': 'error', 'message': 'Script file must have a .py extension.'}), 400

        input_file_row = script_file_row
        using_file_input = True
        sequence_value = script_file_row['name']

        raw_command_args = payload.get('command_args')
        if raw_command_args is None:
            command_args = []
        elif isinstance(raw_command_args, str):
            try:
                command_args = shlex.split(raw_command_args)
            except ValueError:
                return jsonify({'status': 'error', 'message': 'command_args string contains unmatched quotes.'}), 400
        elif isinstance(raw_command_args, Iterable):
            command_args = []
            for arg in raw_command_args:
                if arg is None:
                    continue
                arg_str = str(arg).strip()
                if arg_str:
                    command_args.append(arg_str)
        else:
            return jsonify({'status': 'error', 'message': 'command_args must be a string or a list of arguments.'}), 400

        raw_environment = payload.get('environment') or {}
        if isinstance(raw_environment, dict):
            for key, value in raw_environment.items():
                key_str = str(key).strip()
                if not key_str:
                    continue
                environment[key_str] = str(value)
        else:
            return jsonify({'status': 'error', 'message': 'environment must be an object containing key/value pairs.'}), 400

        raw_data_ids = payload.get('data_file_ids') or []
        if isinstance(raw_data_ids, str):
            raw_data_ids = [chunk.strip() for chunk in raw_data_ids.split(',') if chunk.strip()]
        elif not isinstance(raw_data_ids, Iterable):
            raw_data_ids = []

        for raw_id in raw_data_ids:
            try:
                data_id = int(raw_id)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'data_file_ids must reference valid project files.'}), 400
            data_row = db.execute(
                'SELECT id, project_id, path, name FROM files WHERE id = %s',
                (data_id,),
            ).fetchone()
            if not data_row or data_row['project_id'] != project_id:
                return jsonify({'status': 'error', 'message': 'One of the referenced data files was not found for this project.'}), 404
            if not data_row['path'] or not os.path.exists(data_row['path']):
                return jsonify({'status': 'error', 'message': f"Data file '{data_row['name']}' is missing on the server."}), 404
            data_file_rows.append(data_row)

        raw_timeout = payload.get('timeout_seconds')
        if raw_timeout is None:
            timeout_seconds = 3600
        else:
            try:
                timeout_seconds = int(raw_timeout)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'timeout_seconds must be an integer value.'}), 400
            if timeout_seconds < 60:
                return jsonify({'status': 'error', 'message': 'timeout_seconds must be at least 60 seconds.'}), 400
            timeout_seconds = min(timeout_seconds, 4 * 3600)

        job_metadata['input_type'] = 'python_script'
        job_metadata['script_file'] = {
            'id': script_file_row['id'],
            'name': script_file_row['name'],
        }
        if data_file_rows:
            job_metadata['data_files'] = [
                {'id': row['id'], 'name': row['name']} for row in data_file_rows
            ]
        if command_args:
            job_metadata['command_args'] = command_args
        if environment:
            job_metadata['environment'] = environment
        job_metadata['timeout_seconds'] = timeout_seconds

        params_payload = {
            'script_path': script_path,
            'script_name': script_file_row['name'],
            'data_files': [{'path': row['path'], 'name': row['name']} for row in data_file_rows],
            'command_args': command_args,
            'environment': environment,
            'timeout_seconds': timeout_seconds,
        }
        output_display_name = 'Python execution log'
    else:
        raw_file_id = payload.get('file_id')
        input_type = payload.get('input_type', '')

        # Some RFdiffusion submission paths send the selected file as custom_params.input_pdb
        # instead of top-level file_id. Normalize to file_id handling.
        if (not raw_file_id) and job_task == 'protein_design' and model_type == 'type-12':
            custom_input_ref = custom_params.get('input_pdb')
            if custom_input_ref not in (None, ''):
                raw_file_id = str(custom_input_ref).strip()

        using_file_input = bool(raw_file_id)
        using_pdb_content = input_type == 'pdb_content'

        # Handle direct PDB content from database fetch
        if using_pdb_content:
            pdb_content = payload.get('pdb_content', '').strip()
            pdb_id = payload.get('pdb_id', '')

            if not pdb_content:
                return jsonify({'status': 'error', 'message': 'PDB content is required when using PDB database input.'}), 400

            # Validate that it looks like PDB content
            if not any(keyword in pdb_content[:500].upper() for keyword in ['HEADER', 'ATOM  ', 'HETATM', 'REMARK', 'MODEL ']):
                return jsonify({'status': 'error', 'message': 'Invalid PDB content - file does not appear to be in PDB format.'}), 400

            sequence_value = pdb_content
            job_metadata['input_type'] = 'pdb_database'
            job_metadata['pdb_id'] = pdb_id
            job_metadata['pdb_source'] = 'RCSB PDB'
            using_file_input = False  # Not using file input

        elif using_file_input:
            if not has_file_input:
                return jsonify({'status': 'error', 'message': 'Selected model does not support file-based inputs yet.'}), 400

            input_file_row = None
            is_integer_id = False
            is_job_output = False
            try:
                input_file_id = int(raw_file_id)
                is_integer_id = True
            except (TypeError, ValueError):
                input_file_id = None  # Will be None for job outputs (UUID strings)

            # If it's an integer ID, try the files table first
            if is_integer_id:
                input_file_row = db.execute(
                    'SELECT id, project_id, path, name FROM files WHERE id = %s',
                    (input_file_id,),
                ).fetchone()

            # If not found (or if it's a UUID string), check if it's a job output file
            if not input_file_row:
                job_output_row = db.execute(
                    'SELECT job_id as id, project_id, output_file as path, output_display_name as name FROM jobs WHERE job_id = %s AND output_file IS NOT NULL',
                    (raw_file_id,),
                ).fetchone()
                if job_output_row:
                    # Convert to compatible format
                    input_file_row = dict(job_output_row)
                    is_job_output = True
                    input_file_id = None  # Don't store UUID in integer column
                    # Use output_display_name or extract from path
                    if not input_file_row['name']:
                        input_file_row['name'] = os.path.basename(input_file_row['path']) if input_file_row['path'] else 'output'
                    # Store reference to source job in metadata
                    job_metadata['source_job_id'] = raw_file_id

            if not input_file_row or input_file_row['project_id'] != project_id:
                return jsonify({'status': 'error', 'message': 'Input file not found for this project.'}), 404
            file_path = input_file_row['path']
            if not file_path or not os.path.exists(file_path):
                return jsonify({'status': 'error', 'message': 'The selected input file is missing on the server.'}), 404
            try:
                sequence_value = _load_sequence_from_file(file_path)
            except ValueError as exc:
                return jsonify({'status': 'error', 'message': str(exc)}), 400
        else:
            # For molecular_docking and pipeline models, files/inputs are handled via custom_params, not main file_id
            pipeline_tasks = ['molecular_docking', 'fold_dock_pipeline', 'design_fold_dock_pipeline']
            if not has_sequence_input and job_task not in pipeline_tasks:
                logger.warning(f"Validation failed - has_sequence_input: {has_sequence_input}, job_task: {job_task}, model_type: {model_type}")
                return jsonify({'status': 'error', 'message': 'Selected model requires an uploaded file input.'}), 400
            # Strip FASTA headers if present in the sequence input.
            # Some multimer models (openfold3, boltz, boltz1, boltz2, boltz2-
            # enhanced) accept a ``sequences`` list as their primary input. We
            # build a synthetic single ``sequence`` for the validator by
            # joining the list with ':' so the existing multimer code path
            # (which already strips ':' later) accepts it. Each worker reads
            # ``sequences`` from the payload directly, so this only affects
            # validation. The synthetic string is only used to drive the
            # existing checks below; the original list is preserved in the
            # payload so workers get the structured form.
            raw_sequence = (payload.get('sequence') or '').strip()
            seq_list = payload.get('sequences')
            has_seq_list = isinstance(seq_list, (list, tuple)) and any(
                isinstance(s, str) and s.strip() for s in seq_list
            )
            if not raw_sequence and has_seq_list:
                parts = [str(s).strip() for s in seq_list if isinstance(s, str) and s.strip()]
                if parts:
                    raw_sequence = ':'.join(parts)
            sequence_value = strip_fasta_header(raw_sequence)
            multimer_sequences = _extract_multimer_sequences(custom_params.get('chains') if isinstance(custom_params, dict) else None)
            if not sequence_value and len(multimer_sequences) >= 2:
                sequence_value = ''.join(multimer_sequences)

            # Models that accept a list-valued primary input skip the
            # single-sequence character-validity check; their workers validate
            # each list entry themselves. We still require at least one
            # non-empty entry below.
            multimer_models = {
                'openfold3', 'boltz', 'boltz1', 'boltz2', 'boltz2-enhanced',
                'alphafold2-re-multimer',
            }
            primary_input_is_list = (
                (model_type in multimer_models or has_seq_list)
                and not (payload.get('sequence') or '').strip()
            )

        # Skip sequence validation for pipeline models - inputs are handled separately via custom_params
        pipeline_tasks = ['molecular_docking', 'fold_dock_pipeline', 'design_fold_dock_pipeline']
        multimer_sequences = _extract_multimer_sequences(custom_params.get('chains') if isinstance(custom_params, dict) else None)
        has_multimer = len(multimer_sequences) >= 2
        if has_multimer and sequence_value and ':' in sequence_value:
            sequence_value = sequence_value.replace(':', '')
        has_rfdiffusion_pdb_content = (
            job_task == 'protein_design'
            and model_type == 'type-12'
            and bool((custom_params.get('input_pdb_content') or '').strip())
        )
        if (
            not sequence_value
            and job_task not in pipeline_tasks
            and not has_rfdiffusion_pdb_content
            and not has_multimer
            and not primary_input_is_list
        ):
            _molecule_type, _valid_chars, sequence_label = _sequence_validation_profile()
            return jsonify({'status': 'error', 'message': f'{sequence_label} sequence is required for this model.'}), 400

        # Skip validation for pipeline models if sequence_value is empty (inputs will be loaded from custom_params)
        if sequence_value:
            # Detect and reject HTML content
            html_indicators = ['<!doctype html>', '<html', '<head>', '<body>', '<script', '<meta', '<div id=']
            sequence_lower = sequence_value[:1000].lower()
            if any(indicator in sequence_lower for indicator in html_indicators):
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid file format: HTML content detected. Please upload a valid molecular structure file (PDB, MOL2, SDF, or protein sequence).'
                }), 400

            # Check if content looks like PDB format (contains ATOM or HETATM records)
            is_pdb_content = _looks_like_pdb(sequence_value)

            # Determine max sequence length based on model type
            # Only validate length for sequence inputs, not for PDB structure files
            if not is_pdb_content and not using_file_input:
                # Set max length based on specific model type
                if model_type == 'type-0':  # ESMFold
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_ESMFOLD
                elif model_type == 'type-2':  # AlphaFold2
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_ALPHAFOLD2
                elif model_type == 'type-3':  # DiffDock
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_DIFFDOCK
                elif model_type == 'type-5':  # Fold+Dock Pipeline
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_FOLD_DOCK
                else:
                    max_seq_length = None  # No limit for other models

                # Only check length if there's a limit set
                if max_seq_length is not None and len(sequence_value) > max_seq_length:
                    return jsonify({'status': 'error', 'message': f'Sequence exceeds {max_seq_length} amino acid limit.'}), 400

            # Skip sequence-alphabet validation for structure files, model
            # families with non-polymer inputs, and multimer models whose
            # primary input is a list (each worker validates its own entries).
            if (
                model_type not in ['type-3', 'type-4', 'type-6']
                and not is_pdb_content
                and not has_multimer
                and not primary_input_is_list
                and 'smiles' not in supported_inputs
            ):
                _molecule_type, valid_chars, sequence_label = _sequence_validation_profile()
                invalid = set(sequence_value.upper()) - valid_chars
                if invalid:
                    invalid_label = 'amino acid' if sequence_label == 'protein' else sequence_label
                    return jsonify({'status': 'error', 'message': f"Invalid {invalid_label} characters: {', '.join(sorted(invalid))}."}), 400

        job_metadata['input_type'] = 'file' if using_file_input else 'sequence'
        params_payload: Dict[str, Any] = {}
        if sequence_value:
            params_payload['sequence'] = sequence_value
        # Multimer models whose primary input is a list (openfold3, boltz,
        # boltz1, boltz2, boltz2-enhanced, alphafold2-re-multimer) read
        # ``sequences`` directly from params. Forward the original list so the
        # worker gets the structured form, not the ':'-joined validation string.
        if primary_input_is_list and isinstance(seq_list, (list, tuple)):
            clean_list = [str(s).strip() for s in seq_list if isinstance(s, str) and s.strip()]
            if clean_list:
                params_payload['sequences'] = clean_list
        # The multimer code path also reads chains=; forward it if the SDK
        # passed per-chain specs. OpenFold3 specifically requires ``chains`` as
        # a list of dicts; convert a plain list of strings into dicts so the
        # caller's convenience form (``sequences=['A','B']``) Just Works.
        if (
            model_type == 'openfold3'
            and isinstance(params_payload.get('sequences'), list)
            and params_payload['sequences']
            and isinstance(params_payload['sequences'][0], str)
        ):
            chain_dicts = []
            for idx, seq in enumerate(params_payload['sequences']):
                chain_dicts.append({
                    'sequence': seq,
                    'id': chr(ord('A') + idx),
                    'molecule_type': 'protein',
                })
            params_payload['chains'] = chain_dicts
        if isinstance(custom_params, dict) and isinstance(custom_params.get('chains'), list):
            params_payload['chains'] = custom_params['chains']

    is_structure_like_task = job_task in {
        'structure_prediction',
        'protein_structure_prediction',
        'protein_complex_folding',
        'enhanced_structure_prediction',
    }
    is_explicit_multimer_model = str(model_type).endswith('-multimer')
    if is_structure_like_task or has_multimer or is_explicit_multimer_model:
        job_metadata['structure_mode'] = 'multimer' if (has_multimer or is_explicit_multimer_model) else 'monomer'

    if input_file_row and 'input_file' not in job_metadata:
        job_metadata['input_file'] = {
            'id': input_file_row['id'],
            'name': input_file_row['name'],
        }

    # Calculate adaptive credit cost based on sequence and parameters.
    # Internal pipeline jobs are free here: the pipeline already paid a flat fee.
    if skip_credit:
        job_cost = 0 if cost_override is None else cost_override
    else:
        job_cost = Config.calculate_adaptive_credit_cost(model_type, sequence_value, custom_params)

        # DiffDock daily limit removed - no restrictions

        # Check if billing user has enough credits
        billing_user_credits = db.execute(
            "SELECT credits FROM users WHERE id = %s",
            (billing_user_id,)
        ).fetchone()['credits']

        if billing_user_credits < job_cost:
            return jsonify({
                'status': 'error',
                'message': f'Insufficient credits. This job requires {job_cost} credits, but you have {billing_user_credits}.'
            }), 402

        ensure_monthly_credit_reset(db, billing_user_id)
        billing_user = db.execute('SELECT credits FROM users WHERE id = %s', (billing_user_id,)).fetchone()
        if billing_user['credits'] <= 0:
            return jsonify({'status': 'error', 'message': 'Insufficient credits to submit a job.'}), 402

    project = db.execute('SELECT * FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404
    team_id = project['team_id']
    # organization_id may not exist in all database setups
    try:
        organization_id = project['organization_id'] or project['team_id']
    except (KeyError, IndexError):
        organization_id = project['team_id']

    project_path = get_safe_project_path(project_id, team_id)
    if not project_path:
        return jsonify({'status': 'error', 'message': 'Project path could not be determined or is invalid.'}), 404

    storage_state = get_user_storage_state(db, user_id)
    if storage_state['available'] <= 0:
        return jsonify({
            'status': 'error',
            'message': 'No storage available for job outputs. Delete existing files or outputs before submitting new jobs.',
            'storage': storage_state
        }), 507

    # job_id already generated earlier for default naming
    output_dir = get_job_branch_path(project_path, job_id, branch='main', create=True)
    input_branch_dir = get_job_branch_path(project_path, job_id, branch='inputs', create=True)
    if not output_dir or not input_branch_dir:
        return jsonify({'status': 'error', 'message': 'Failed to initialize job output storage.'}), 500

    job_branches = job_metadata.get('artifact_branches')
    if not isinstance(job_branches, dict):
        job_branches = {}
    job_branches['main'] = {'relative_path': get_job_branch_relative_path('main')}
    job_branches['inputs'] = {'relative_path': get_job_branch_relative_path('inputs')}
    job_metadata['artifact_branches'] = job_branches

    output_filename = f"{job_id}{output_extension}"
    output_path = os.path.join(output_dir, output_filename)

    if job_task == 'molecular_docking':
        # DiffDock outputs both PDB and SDF files
        output_display_name = 'DiffDock Output PDB'
        sdf_filename = f"{job_id}.sdf"
        sdf_path = os.path.join(output_dir, sdf_filename)
        job_artifacts['ligand_sdf'] = sdf_path
        job_metadata['artifacts'] = [{
            'name': 'ligand_sdf',
            'filename': sdf_filename,
            'display_name': 'Docked Ligand SDF',
        }]

        # Handle protein input from databases or files
        from phonos_app.helpers.databases import fetch_protein_for_job, fetch_ligand_for_job, DatabaseIntegrationError, convert_ligand_to_smiles

        protein_input_type = custom_params.get('protein_input_type', 'file')

        # If pdb_content was already provided (frontend fetched from external DB), use it directly
        if using_pdb_content and sequence_value:
            logger.info(f"[DiffDock] Using pre-fetched structure from frontend (input_type: {protein_input_type})")
            job_metadata['protein_source'] = {
                'type': protein_input_type,
                'id': payload.get('pdb_id', ''),
                'source': 'client_prefetch'
            }
        # If using file input, load the protein file
        elif protein_input_type == 'file':
            protein_file_id = custom_params.get('protein_file')
            if protein_file_id:
                try:
                    protein_file_id = int(protein_file_id)
                    protein_file_row = db.execute(
                        'SELECT id, project_id, path, name FROM files WHERE id = %s AND project_id = %s',
                        (protein_file_id, project_id)
                    ).fetchone()

                    if not protein_file_row:
                        return jsonify({'status': 'error', 'message': 'Selected protein file not found.'}), 404

                    protein_file_path = protein_file_row['path']
                    if not os.path.exists(protein_file_path):
                        return jsonify({'status': 'error', 'message': 'Protein file missing on server.'}), 404

                    with open(protein_file_path, 'r', encoding='utf-8') as f:
                        sequence_value = f.read()

                    job_metadata['protein_file'] = {
                        'id': protein_file_row['id'],
                        'name': protein_file_row['name']
                    }
                except (ValueError, TypeError):
                    # Not an integer - could be a job output UUID or job artifact reference
                    protein_file_str = str(protein_file_id).strip()

                    # Check if it's a job artifact reference: "job_id:artifact_filename"
                    if ':' in protein_file_str:
                        parts = protein_file_str.split(':', 1)
                        source_job_id = parts[0]
                        artifact_filename = parts[1]

                        # Verify the source job exists and belongs to this project
                        source_job_row = db.execute(
                            "SELECT job_id, project_id, output_file, COALESCE(policy, metadata, '{}') AS policy_blob "
                            "FROM jobs WHERE job_id = %s AND project_id = %s",
                            (source_job_id, project_id)
                        ).fetchone()

                        if not source_job_row:
                            return jsonify({'status': 'error', 'message': 'Source job output not found.'}), 404

                        source_policy = {}
                        if source_job_row.get('policy_blob'):
                            try:
                                source_policy = json.loads(source_job_row['policy_blob'])
                            except (TypeError, json.JSONDecodeError):
                                source_policy = {}

                        protein_file_path = resolve_job_artifact_path(
                            project_path=project_path,
                            job_id=source_job_id,
                            artifact_name=artifact_filename,
                            output_file=source_job_row.get('output_file'),
                            policy=source_policy,
                            preferred_branch='main',
                        )
                        if not protein_file_path:
                            return jsonify({'status': 'error', 'message': 'Source job has no output directory.'}), 404

                        if not os.path.exists(protein_file_path):
                            return jsonify({'status': 'error', 'message': f'Protein artifact file not found: {artifact_filename}'}), 404

                        with open(protein_file_path, 'r', encoding='utf-8') as f:
                            sequence_value = f.read()

                        # Store reference to source job and artifact in metadata
                        job_metadata['protein_file'] = {
                            'source_job_id': source_job_id,
                            'artifact_filename': artifact_filename,
                            'name': artifact_filename
                        }
                    else:
                        # It's a job output UUID (main output file)
                        job_output_row = db.execute(
                            'SELECT job_id, project_id, output_file, output_display_name FROM jobs WHERE job_id = %s AND project_id = %s AND output_file IS NOT NULL',
                            (protein_file_str, project_id)
                        ).fetchone()

                        if not job_output_row:
                            return jsonify({'status': 'error', 'message': 'Job output not found for protein input.'}), 404

                        protein_file_path = job_output_row['output_file']
                        if not os.path.exists(protein_file_path):
                            return jsonify({'status': 'error', 'message': 'Job output file missing on server.'}), 404

                        with open(protein_file_path, 'r', encoding='utf-8') as f:
                            sequence_value = f.read()

                        job_metadata['protein_file'] = {
                            'source_job_id': protein_file_str,
                            'name': job_output_row['output_display_name'] or os.path.basename(protein_file_path)
                        }
                except Exception as e:
                    logger.error(f"Error loading protein file: {e}", exc_info=True)
                    return jsonify({'status': 'error', 'message': 'Error loading protein file. Please check the file and try again.'}), 500
            elif not sequence_value:
                # No protein file specified and no sequence_value from main file_id
                return jsonify({'status': 'error', 'message': 'Protein structure file is required.'}), 400
        else:
            # Fetch protein data from databases (PDB ID or UniProt ID)
            try:
                sequence_value, protein_metadata = fetch_protein_for_job(
                    input_type=protein_input_type,
                    pdb_id=custom_params.get('protein_pdb_id', '').strip(),
                    uniprot_id=custom_params.get('protein_uniprot_id', '').strip(),
                    file_content=sequence_value
                )
                job_metadata['protein_source'] = protein_metadata
            except DatabaseIntegrationError as e:
                return jsonify({'status': 'error', 'message': f'Database error: {str(e)}'}), 400
            except ValueError as e:
                return jsonify({'status': 'error', 'message': str(e)}), 400
            except Exception as e:
                return jsonify({'status': 'error', 'message': 'Error fetching protein data. Please try again.'}), 500

        # Handle ligand input from databases or files
        ligand_input_type = custom_params.get('ligand_input_type', 'smiles')
        ligand = ''
        ligand_file_content = None

        # Debug logging
        logger.info(f"[DiffDock] ligand_input_type: {ligand_input_type}")
        logger.info(f"[DiffDock] custom_params keys: {list(custom_params.keys())}")
        logger.info(f"[DiffDock] ligand_file value: {custom_params.get('ligand_file')}")

        # If using file input, load the ligand file content and convert to SMILES
        if ligand_input_type == 'file':
            ligand_file_id = custom_params.get('ligand_file')
            if not ligand_file_id:
                return jsonify({'status': 'error', 'message': 'Please select a ligand file (SDF/MOL/MOL2 format) from your project files.'}), 400

            try:
                ligand_file_id = int(ligand_file_id)
                ligand_file_row = db.execute(
                    'SELECT id, name, path FROM files WHERE id = %s AND project_id = %s',
                    (ligand_file_id, project_id)
                ).fetchone()

                if not ligand_file_row:
                    return jsonify({'status': 'error', 'message': 'Selected ligand file not found.'}), 404

                ligand_file_path = ligand_file_row['path']
                if not os.path.exists(ligand_file_path):
                    return jsonify({'status': 'error', 'message': 'Ligand file missing on server.'}), 404

                # Try UTF-8 first, fall back to latin-1 if needed
                try:
                    with open(ligand_file_path, 'r', encoding='utf-8') as f:
                        ligand_file_content = f.read()
                except UnicodeDecodeError:
                    with open(ligand_file_path, 'r', encoding='latin-1') as f:
                        ligand_file_content = f.read()

                # Determine file format from extension
                file_ext = ligand_file_row['name'].lower().split('.')[-1]
                if file_ext not in ['sdf', 'mol', 'mol2', 'pdb']:
                    file_ext = 'sdf'  # Default to SDF

                # Convert ligand file to SMILES
                logger.info(f"[DiffDock] Converting {file_ext.upper()} file to SMILES: {ligand_file_row['name']}")
                ligand_file_content = convert_ligand_to_smiles(ligand_file_content, file_ext)
                logger.info(f"[DiffDock] Converted to SMILES: {ligand_file_content}")

                job_metadata['ligand_file'] = {
                    'id': ligand_file_row['id'],
                    'name': ligand_file_row['name'],
                    'converted_to_smiles': True
                }
            except (ValueError, TypeError):
                return jsonify({'status': 'error', 'message': 'Invalid ligand file ID.'}), 400
            except Exception as e:
                return jsonify({'status': 'error', 'message': 'Error loading ligand file. Please check the file and try again.'}), 500

        try:
            ligand, ligand_metadata = fetch_ligand_for_job(
                input_type=ligand_input_type,
                smiles=custom_params.get('ligand', '').strip(),
                pubchem_cid=custom_params.get('ligand_pubchem_cid', '').strip(),
                file_content=ligand_file_content
            )
            job_metadata['ligand_source'] = ligand_metadata
        except DatabaseIntegrationError as e:
            return jsonify({'status': 'error', 'message': f'Database error: {str(e)}'}), 400
        except ValueError as e:
            return jsonify({'status': 'error', 'message': str(e)}), 400
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'Error fetching ligand data: {str(e)}'}), 500

        if not ligand:
            return jsonify({'status': 'error', 'message': 'Ligand input is required for molecular docking.'}), 400

        # Smartly assign parameters based on content type for DiffDock
        # Only set these fields if they have actual content

        # Protein: Check for PDB content (headers/atoms) vs Sequence
        if sequence_value:
            if any(keyword in sequence_value[:500].upper() for keyword in ['HEADER', 'ATOM  ', 'HETATM', 'REMARK']):
                params_payload['pdb_content'] = sequence_value
            else:
                params_payload['protein_sequence'] = sequence_value

        # Ligand: Files are converted to SMILES upstream; pass to worker with file type
        if ligand:
            params_payload['ligand'] = ligand
            params_payload['ligand_file_type'] = custom_params.get('ligand_file_type', 'smiles')
            logger.info(f"[DiffDock] Sending ligand (type={params_payload['ligand_file_type']}): {ligand[:100]}...")

        params_payload['output_pdb'] = output_path
        params_payload['output_sdf'] = sdf_path

        # Save the input protein PDB to the job output directory for later viewing
        if sequence_value and any(keyword in sequence_value[:500].upper() for keyword in ['HEADER', 'ATOM  ', 'HETATM', 'REMARK']):
            input_protein_filename = f"{job_id}_input_protein.pdb"
            input_protein_path = os.path.join(input_branch_dir, input_protein_filename)
            try:
                with open(input_protein_path, 'w', encoding='utf-8') as f:
                    f.write(sequence_value)
                # Add to job artifacts so it's tracked
                if 'artifacts' not in job_metadata:
                    job_metadata['artifacts'] = []
                job_metadata['artifacts'].insert(0, {
                    'name': 'input_protein',
                    'filename': input_protein_filename,
                    'display_name': 'Input Protein (Original)',
                    'type': 'input',
                    'branch': 'inputs',
                    'relative_path': os.path.join(get_job_branch_relative_path('inputs'), input_protein_filename),
                })
                logger.info(f"[DiffDock] Saved input protein to: {input_protein_path}")
            except Exception as e:
                logger.warning(f"[DiffDock] Failed to save input protein: {e}")

    elif job_task == 'fold_and_dock' or job_task == 'fold_dock_pipeline':
        # Fold+Dock Pipeline: Fold protein then dock ligand
        output_display_name = 'Fold+Dock Output PDB'
        sdf_filename = f"{job_id}.sdf"
        sdf_path = os.path.join(output_dir, sdf_filename)
        job_artifacts['ligand_sdf'] = sdf_path
        job_metadata['artifacts'] = [{
            'name': 'ligand_sdf',
            'filename': sdf_filename,
            'display_name': 'Docked Ligand SDF',
        }]

        # Handle ligand input from files or SMILES
        from phonos_app.helpers.databases import fetch_ligand_for_job, DatabaseIntegrationError, convert_ligand_to_smiles

        ligand_input_type = custom_params.get('ligand_input_type', 'smiles')
        ligand = ''
        ligand_file_content = None

        # If using file input, load the ligand file content and convert to SMILES
        if ligand_input_type == 'file':
            ligand_file_id = custom_params.get('ligand_file')
            if not ligand_file_id:
                return jsonify({'status': 'error', 'message': 'Please select a ligand file (SDF/MOL/MOL2 format) from your project files.'}), 400

            try:
                ligand_file_id = int(ligand_file_id)
                ligand_file_row = db.execute(
                    'SELECT id, name, path FROM files WHERE id = %s AND project_id = %s',
                    (ligand_file_id, project_id)
                ).fetchone()

                if not ligand_file_row:
                    return jsonify({'status': 'error', 'message': 'Selected ligand file not found.'}), 404

                ligand_file_path = ligand_file_row['path']
                if not os.path.exists(ligand_file_path):
                    return jsonify({'status': 'error', 'message': 'Ligand file missing on server.'}), 404

                # Try UTF-8 first, fall back to latin-1 if needed
                try:
                    with open(ligand_file_path, 'r', encoding='utf-8') as f:
                        ligand_file_content = f.read()
                except UnicodeDecodeError:
                    with open(ligand_file_path, 'r', encoding='latin-1') as f:
                        ligand_file_content = f.read()

                # Determine file format from extension
                file_ext = ligand_file_row['name'].lower().split('.')[-1]
                if file_ext not in ['sdf', 'mol', 'mol2', 'pdb']:
                    file_ext = 'sdf'  # Default to SDF

                # Convert ligand file to SMILES
                logger.info(f"[Fold+Dock] Converting {file_ext.upper()} file to SMILES: {ligand_file_row['name']}")
                ligand_file_content = convert_ligand_to_smiles(ligand_file_content, file_ext)
                logger.info(f"[Fold+Dock] Converted to SMILES: {ligand_file_content}")

                job_metadata['ligand_file'] = {
                    'id': ligand_file_row['id'],
                    'name': ligand_file_row['name'],
                    'converted_to_smiles': True
                }
            except (ValueError, TypeError):
                return jsonify({'status': 'error', 'message': 'Invalid ligand file ID.'}), 400
            except Exception as e:
                return jsonify({'status': 'error', 'message': 'Error loading ligand file. Please check the file and try again.'}), 500

        try:
            ligand, ligand_metadata = fetch_ligand_for_job(
                input_type=ligand_input_type,
                smiles=custom_params.get('ligand', '').strip(),
                pubchem_cid=custom_params.get('ligand_pubchem_cid', '').strip(),
                file_content=ligand_file_content
            )
            job_metadata['ligand_source'] = ligand_metadata
        except DatabaseIntegrationError as e:
            return jsonify({'status': 'error', 'message': f'Database error: {str(e)}'}), 400
        except ValueError as e:
            return jsonify({'status': 'error', 'message': str(e)}), 400
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'Error fetching ligand data: {str(e)}'}), 500

        if not ligand:
            return jsonify({'status': 'error', 'message': 'Ligand input is required for Fold+Dock pipeline.'}), 400

        # Ligand: Always use SMILES format (files are converted to SMILES)
        if ligand:
            params_payload['ligand'] = ligand
            # Also update custom_params so it doesn't get overwritten later
            custom_params['ligand'] = ligand
            logger.info(f"[Fold+Dock] Sending ligand as SMILES: {ligand[:100]}...")

        # Protein sequence for folding
        if sequence_value:
            params_payload['protein_sequence'] = sequence_value

        params_payload['output_pdb'] = output_path
        params_payload['output_sdf'] = sdf_path

    elif job_task == 'structure_prediction':
        # Boltz2: Protein-Ligand Structure Prediction
        output_display_name = 'Boltz2 Structure'
        # Artifacts will be populated when viewing output based on what files are generated

        # Handle ligand input (similar to DiffDock/Fold+Dock)
        from phonos_app.helpers.databases import fetch_ligand_for_job, DatabaseIntegrationError, convert_ligand_to_smiles

        ligand_input_type = custom_params.get('ligand_input_type', 'none')
        ligand = ''
        ligand_file_content = None

        if ligand_input_type == 'file':
            ligand_file_id = custom_params.get('ligand_file')
            if ligand_file_id:
                try:
                    ligand_file_id = int(ligand_file_id)
                    ligand_file_row = db.execute(
                        'SELECT id, name, path FROM files WHERE id = %s AND project_id = %s',
                        (ligand_file_id, project_id)
                    ).fetchone()

                    if not ligand_file_row:
                        return jsonify({'status': 'error', 'message': 'Selected ligand file not found.'}), 404

                    ligand_file_path = ligand_file_row['path']
                    if not os.path.exists(ligand_file_path):
                        return jsonify({'status': 'error', 'message': 'Ligand file missing on server.'}), 404

                    # Read content
                    try:
                        with open(ligand_file_path, 'r', encoding='utf-8') as f:
                            ligand_file_content = f.read()
                    except UnicodeDecodeError:
                        with open(ligand_file_path, 'r', encoding='latin-1') as f:
                            ligand_file_content = f.read()

                    # Convert to SMILES if it's a structure file
                    file_ext = ligand_file_row['name'].lower().split('.')[-1]
                    if file_ext in ['sdf', 'mol', 'mol2', 'pdb']:
                        logger.info(f"[Boltz2] Converting {file_ext.upper()} file to SMILES: {ligand_file_row['name']}")
                        ligand_file_content = convert_ligand_to_smiles(ligand_file_content, file_ext)

                    job_metadata['ligand_file'] = {
                        'id': ligand_file_row['id'],
                        'name': ligand_file_row['name'],
                        'converted_to_smiles': True
                    }
                except Exception as e:
                    return jsonify({'status': 'error', 'message': f'Error loading ligand file: {str(e)}'}), 500

        # Fetch/process ligand if type is not none and not ccd
        # CCD codes are passed directly to the API without conversion
        if ligand_input_type not in ('none', 'ccd'):
            try:
                ligand, ligand_metadata = fetch_ligand_for_job(
                    input_type=ligand_input_type,
                    smiles=custom_params.get('ligand_smiles', '').strip(),
                    pubchem_cid=None, # Boltz2 UI uses SMILES/CCD/File
                    file_content=ligand_file_content
                )
                if ligand_metadata:
                    job_metadata['ligand_source'] = ligand_metadata
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'Error processing ligand: {str(e)}'}), 500

            if not ligand:
                 return jsonify({'status': 'error', 'message': 'Ligand input provided but could not be processed.'}), 400

        # Pass parameters to worker
        # Sequence is already in params_payload['sequence']
        
        # Add ligand if available
        if ligand:
            params_payload['ligand_smiles'] = ligand
            params_payload['ligand_input_type'] = 'smiles'
        elif ligand_input_type == 'ccd':
            ccd_code = custom_params.get('ligand_ccd', '').strip()
            if ccd_code:
                params_payload['ligand_ccd'] = ccd_code
                params_payload['ligand_input_type'] = 'ccd'
        else:
            params_payload['ligand_input_type'] = 'none'

        # Explicitly pass molecule_type and other custom params that might not be auto-merged
        params_payload['molecule_type'] = custom_params.get('molecule_type', 'protein')
        params_payload['chain_id'] = custom_params.get('chain_id', 'A')
        params_payload['without_potentials'] = custom_params.get('without_potentials', False) # Ensure this is passed!

        # Multimer mode: convert chains for worker consumption
        chains = custom_params.get('chains')
        if isinstance(chains, list) and len(chains) >= 2:
            normalized_chains = [
                {
                    'sequence': c.get('sequence', ''),
                    'molecule_type': c.get('molecule_type', 'protein'),
                    'id': c.get('chain_id', chr(65 + i)),
                    'chain_id': c.get('chain_id', chr(65 + i)),
                }
                for i, c in enumerate(chains)
            ]
            params_payload['chains'] = normalized_chains
            params_payload['sequences'] = normalized_chains
            params_payload['molecule_type'] = chains[0].get('molecule_type', 'protein')

        params_payload['output_path'] = output_path

    elif job_task == 'protein_sequence_design':
        # ProteinMPNN requires PDB structure input
        # Check if the input is PDB content
        is_pdb_input = _looks_like_pdb(sequence_value)

        if not is_pdb_input:
            return jsonify({'status': 'error', 'message': 'ProteinMPNN requires a PDB structure file as input.'}), 400

        # Pass PDB content to the worker in multiple field names for compatibility
        # Clear the generic 'sequence' field and use specific PDB fields
        params_payload = {
            'pdb': sequence_value,           # Primary field - most workers check this
            'pdb_content': sequence_value,   # Alternative field name
            'protein_pdb': sequence_value,   # Another common convention
            'output_path': output_path
        }
    elif job_task == 'protein_design' and model_type == 'type-12':
        # RFDiffusion expects raw PDB content in params.input_pdb.
        input_pdb = ''
        pdb_source = 'unknown'

        input_pdb_content = custom_params.get('input_pdb_content')
        if isinstance(input_pdb_content, str) and input_pdb_content.strip():
            input_pdb = input_pdb_content.strip()
            pdb_source = 'custom_params.input_pdb_content'

        if not input_pdb and isinstance(sequence_value, str) and _looks_like_pdb(sequence_value):
            input_pdb = sequence_value.strip()
            pdb_source = 'sequence_value'

        custom_input_pdb = custom_params.get('input_pdb')
        if not input_pdb and isinstance(custom_input_pdb, str) and _looks_like_pdb(custom_input_pdb):
            input_pdb = custom_input_pdb.strip()
            pdb_source = 'custom_params.input_pdb'

        if not input_pdb:
            return jsonify({
                'status': 'error',
                'message': 'RFDiffusion requires a valid PDB file or fetched PDB structure.'
            }), 400

        if not _has_atom_records(input_pdb):
            return jsonify({
                'status': 'error',
                'message': 'RFDiffusion input PDB must contain ATOM or HETATM records.'
            }), 400

        logger.info(
            "RFDiffusion job %s normalized input_pdb (%d chars) from %s",
            job_id,
            len(input_pdb),
            pdb_source,
        )

        contigs = str(custom_params.get('contigs', '')).strip()
        if not contigs:
            return jsonify({'status': 'error', 'message': 'RFDiffusion requires a contig specification.'}), 400

        hotspot_res_raw = custom_params.get('hotspot_res', [])
        if isinstance(hotspot_res_raw, str):
            hotspot_res = [part.strip() for part in hotspot_res_raw.split(',') if part.strip()]
        elif isinstance(hotspot_res_raw, list):
            hotspot_res = [str(part).strip() for part in hotspot_res_raw if str(part).strip()]
        else:
            hotspot_res = []

        try:
            diffusion_steps = int(custom_params.get('diffusion_steps', 50) or 50)
            num_designs = int(custom_params.get('num_designs', 1) or 1)
            noise_scale = float(custom_params.get('noise_scale', 1.0) or 1.0)
        except (TypeError, ValueError):
            return jsonify({
                'status': 'error',
                'message': 'RFDiffusion parameters diffusion_steps/num_designs/noise_scale are invalid.'
            }), 400

        params_payload = {
            'input_pdb': input_pdb,
            'contigs': contigs,
            'hotspot_res': hotspot_res,
            'diffusion_steps': diffusion_steps,
            'num_designs': num_designs,
            'noise_scale': noise_scale,
        }

        partial_t = custom_params.get('partial_T')
        if partial_t not in (None, ''):
            try:
                params_payload['partial_T'] = int(partial_t)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'RFDiffusion parameter partial_T must be an integer.'}), 400

        output_display_name = 'RFDiffusion Designs'
    elif job_task == 'protein_embedding':
        output_display_name = 'ESM2 Embedding Summary'
        artifact_filename = f"{job_id}_embeddings.npz"
        embeddings_path = os.path.join(output_dir, artifact_filename)
        job_artifacts['embeddings_npz'] = embeddings_path
        job_metadata['artifacts'] = [{
            'name': 'embeddings_npz',
            'filename': artifact_filename,
            'display_name': 'ESM2 residue embeddings (.npz)',
        }]
    elif job_task == 'python_script_execution':
        workspace_archive_name = f"{job_id}_workspace.tar.gz"
        workspace_archive_path = os.path.join(output_dir, workspace_archive_name)
        job_artifacts['workspace_snapshot'] = workspace_archive_path
        job_metadata['artifacts'] = [{
            'name': 'workspace_snapshot',
            'filename': workspace_archive_name,
            'display_name': 'Execution workspace (.tar.gz)',
        }]
        params_payload['workspace_archive_path'] = workspace_archive_path

    elif job_task == 'metal_binding_prediction':
        # Metal3D: Metal binding site prediction
        from phonos_app.helpers.databases import fetch_protein_for_job, DatabaseIntegrationError

        protein_input_type = custom_params.get('protein_input_type', 'file')

        # Handle protein input from file, PDB ID, or UniProt ID
        if protein_input_type == 'file':
            protein_file_id = custom_params.get('protein_file')
            if protein_file_id:
                try:
                    protein_file_id = int(protein_file_id)
                    protein_file_row = db.execute(
                        'SELECT id, project_id, path, name FROM files WHERE id = %s AND project_id = %s',
                        (protein_file_id, project_id)
                    ).fetchone()

                    if not protein_file_row:
                        return jsonify({'status': 'error', 'message': 'Selected protein file not found.'}), 404

                    protein_file_path = protein_file_row['path']
                    if not os.path.exists(protein_file_path):
                        return jsonify({'status': 'error', 'message': 'Protein file missing on server.'}), 404

                    with open(protein_file_path, 'r', encoding='utf-8') as f:
                        sequence_value = f.read()

                    job_metadata['protein_file'] = {
                        'id': protein_file_row['id'],
                        'name': protein_file_row['name']
                    }
                except (ValueError, TypeError):
                    # Handle job output reference
                    protein_file_str = str(protein_file_id).strip()
                    if ':' in protein_file_str:
                        parts = protein_file_str.split(':', 1)
                        source_job_id = parts[0]
                        artifact_filename = parts[1]

                        source_job_row = db.execute(
                            "SELECT job_id, project_id, output_file, COALESCE(policy, metadata, '{}') AS policy_blob "
                            "FROM jobs WHERE job_id = %s AND project_id = %s",
                            (source_job_id, project_id)
                        ).fetchone()

                        if not source_job_row:
                            return jsonify({'status': 'error', 'message': 'Source job output not found.'}), 404

                        source_policy = {}
                        if source_job_row.get('policy_blob'):
                            try:
                                source_policy = json.loads(source_job_row['policy_blob'])
                            except (TypeError, json.JSONDecodeError):
                                source_policy = {}

                        protein_file_path = resolve_job_artifact_path(
                            project_path=project_path,
                            job_id=source_job_id,
                            artifact_name=artifact_filename,
                            output_file=source_job_row.get('output_file'),
                            policy=source_policy,
                            preferred_branch='main',
                        )
                        if not protein_file_path:
                            return jsonify({'status': 'error', 'message': 'Source job has no output directory.'}), 404

                        if not os.path.exists(protein_file_path):
                            return jsonify({'status': 'error', 'message': f'Protein artifact file not found: {artifact_filename}'}), 404

                        with open(protein_file_path, 'r', encoding='utf-8') as f:
                            sequence_value = f.read()

                        job_metadata['protein_file'] = {
                            'source_job_id': source_job_id,
                            'artifact_filename': artifact_filename,
                            'name': artifact_filename
                        }
                    else:
                        # Job output UUID
                        job_output_row = db.execute(
                            'SELECT job_id, project_id, output_file, output_display_name FROM jobs WHERE job_id = %s AND project_id = %s AND output_file IS NOT NULL',
                            (protein_file_str, project_id)
                        ).fetchone()

                        if not job_output_row:
                            return jsonify({'status': 'error', 'message': 'Job output not found for protein input.'}), 404

                        protein_file_path = job_output_row['output_file']
                        if not os.path.exists(protein_file_path):
                            return jsonify({'status': 'error', 'message': 'Job output file missing on server.'}), 404

                        with open(protein_file_path, 'r', encoding='utf-8') as f:
                            sequence_value = f.read()

                        job_metadata['protein_file'] = {
                            'source_job_id': protein_file_str,
                            'name': job_output_row['output_display_name'] or os.path.basename(protein_file_path)
                        }
                except Exception as e:
                    logger.error(f"Error loading protein file: {e}", exc_info=True)
                    return jsonify({'status': 'error', 'message': 'Error loading protein file. Please check the file and try again.'}), 500
            elif not sequence_value:
                return jsonify({'status': 'error', 'message': 'Protein structure file is required.'}), 400
        else:
            # Fetch protein data from databases (PDB ID or UniProt ID)
            try:
                sequence_value, protein_metadata = fetch_protein_for_job(
                    input_type=protein_input_type,
                    pdb_id=custom_params.get('protein_pdb_id', '').strip(),
                    uniprot_id=custom_params.get('protein_uniprot_id', '').strip(),
                    file_content=sequence_value
                )
                job_metadata['protein_source'] = protein_metadata
            except DatabaseIntegrationError as e:
                return jsonify({'status': 'error', 'message': f'Database error: {str(e)}'}), 400
            except ValueError as e:
                return jsonify({'status': 'error', 'message': str(e)}), 400
            except Exception as e:
                return jsonify({'status': 'error', 'message': 'Error fetching protein data. Please try again.'}), 500

        # Validate that we have PDB content (Metal3D requires structure, not just sequence)
        if not sequence_value or not any(keyword in sequence_value[:500].upper() for keyword in ['HEADER', 'ATOM  ', 'HETATM', 'REMARK']):
            return jsonify({'status': 'error', 'message': 'Metal3D requires a PDB structure file. Sequences alone are not supported.'}), 400

        # Extract Metal3D-specific parameters
        grid_resolution = custom_params.get('grid_resolution', '1.0')
        prediction_mode = custom_params.get('prediction_mode', 'metal_binding')
        custom_residues = custom_params.get('custom_residues', '').strip()
        confidence_threshold = custom_params.get('confidence_threshold', 0.5)

        # Parse custom residues if provided
        residue_list = None
        if prediction_mode == 'custom' and custom_residues:
            try:
                residue_list = [int(r.strip()) for r in custom_residues.split(',') if r.strip()]
            except ValueError:
                return jsonify({'status': 'error', 'message': 'Invalid custom residue IDs. Must be comma-separated integers.'}), 400

        # Build output artifacts
        output_display_name = 'Metal3D Predictions'
        cube_filename = f"metal_{job_id}.cube"
        probe_filename = f"probes_{job_id}.pdb"
        results_filename = f"{job_id}_results.json"

        cube_path = os.path.join(output_dir, cube_filename)
        probe_path = os.path.join(output_dir, probe_filename)

        job_artifacts['cube_file'] = cube_path
        job_artifacts['probe_file'] = probe_path

        job_metadata['artifacts'] = [
            {
                'name': 'results_json',
                'filename': results_filename,
                'display_name': 'Prediction Results (JSON)',
            },
            {
                'name': 'cube_file',
                'filename': cube_filename,
                'display_name': 'Probability Grid (.cube)',
            },
            {
                'name': 'probe_file',
                'filename': probe_filename,
                'display_name': 'Binding Sites (PDB)',
            }
        ]

        # Build parameters payload for worker
        params_payload.update({
            'pdb_content': sequence_value,
            'grid_resolution': float(grid_resolution),
            'prediction_mode': prediction_mode,
            'confidence_threshold': float(confidence_threshold),
            'output_path': output_path,
            'cube_path': cube_path,
            'probe_path': probe_path,
        })

        if residue_list:
            params_payload['custom_residues'] = residue_list

        # Store custom params in metadata
        job_metadata['custom_params'] = {
            'grid_resolution': grid_resolution,
            'prediction_mode': prediction_mode,
            'confidence_threshold': confidence_threshold,
        }
        if custom_residues:
            job_metadata['custom_params']['custom_residues'] = custom_residues

    raw_artifacts = job_metadata.get('artifacts')
    if isinstance(raw_artifacts, list):
        normalized_artifacts = []
        for artifact in raw_artifacts:
            if not isinstance(artifact, dict):
                continue
            normalized = dict(artifact)
            filename = normalized.get('filename')
            branch_name = normalize_job_branch_name(normalized.get('branch') or 'main')
            normalized['branch'] = branch_name
            if filename and not normalized.get('relative_path'):
                normalized['relative_path'] = os.path.join(
                    get_job_branch_relative_path(branch_name),
                    filename,
                )
            normalized_artifacts.append(normalized)
        job_metadata['artifacts'] = normalized_artifacts

    job_metadata_json = json.dumps(job_metadata)

    db.execute('BEGIN')
    try:
        db.execute(
            'INSERT INTO jobs (project_id, creator_id, job_id, name, model, sequence, status, input_file_id, policy, cost, pipeline_id, pipeline_stage) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (project_id, user_id, job_id, job_name, model_type, sequence_value, 'submitted', input_file_id, job_metadata_json, job_cost, pipeline_id, pipeline_stage)
        )
        db.execute('INSERT INTO job_logs (job_id, status) VALUES (%s, %s)', (job_id, 'submitted'))
        # Deduct the calculated adaptive credit cost from the billing user
        # (job_cost is 0 for internal pipeline jobs, so this is a no-op there).
        db.execute('UPDATE users SET credits = credits - %s WHERE id = %s', (job_cost, billing_user_id))

        # Track job submission event
        try:
            track_event(
                user_id=user_id,
                event_type='job_submitted',
                credits_used=job_cost,
                project_id=project_id,
                job_id=None,  # Will be set when job completes
                metadata={
                    'model': model_type,
                    'task': job_task,
                    'job_id': job_id,
                    'input_type': job_metadata.get('input_type')
                }
            )
        except Exception as e:
            logger.warning(f"Failed to track job submission event: {e}")

        db.commit()
    except Exception:
        db.rollback()
        logger.error(f"Database error while creating job for user {user_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'A database error occurred while creating the job.'}), 500

    # Merge custom parameters into params_payload for worker processing
    # Remove keys that have already been explicitly added to avoid duplication
    if custom_params:
        # Keys already explicitly handled for specific job types
        handled_keys = {'ligand', 'protein', 'output_pdb', 'output_sdf'}
        if job_task == 'protein_design' and model_type == 'type-12':
            handled_keys.update({
                'input_pdb',
                'input_pdb_content',
                'contigs',
                'hotspot_res',
                'diffusion_steps',
                'num_designs',
                'noise_scale',
                'partial_T',
            })
        # Never let raw custom params overwrite explicitly mapped worker params.
        filtered_params = {
            k: v for k, v in custom_params.items()
            if k not in handled_keys and k not in params_payload
        }
        params_payload.update(filtered_params)

    # Force enable_sampled_probs for Evo2 jobs to get per-nucleotide confidence scores
    if model_type in ('evo2', 'type-8'):
        params_payload['enable_sampled_probs'] = True
        # Also update metadata so UI shows True (reflecting actual execution)
        if 'custom_params' not in job_metadata:
            job_metadata['custom_params'] = {}
        job_metadata['custom_params']['enable_sampled_probs'] = True

    job_metadata_json = json.dumps(job_metadata)

    try:
        target_queue = determine_job_target_queue(db, team_id, model_type)

        # Debug logging for ProteinMPN jobs
        if job_task == 'protein_sequence_design':
            logger.info(f"ProteinMPN job {job_id}: params_payload keys = {list(params_payload.keys())}")
            logger.info(f"ProteinMPN job {job_id}: has pdb_content = {'pdb_content' in params_payload}")
            logger.info(f"ProteinMPN job {job_id}: has pdb = {'pdb' in params_payload}")
            logger.info(f"ProteinMPN job {job_id}: has protein_pdb = {'protein_pdb' in params_payload}")
            logger.info(f"ProteinMPN job {job_id}: has sequence = {'sequence' in params_payload}")
            if 'pdb_content' in params_payload:
                pdb_len = len(params_payload['pdb_content']) if params_payload['pdb_content'] else 0
                logger.info(f"ProteinMPN job {job_id}: pdb_content length = {pdb_len} bytes")
                logger.info(f"ProteinMPN job {job_id}: pdb_content preview = {params_payload['pdb_content'][:100] if pdb_len > 0 else 'EMPTY'}")
            if 'pdb' in params_payload:
                pdb_len = len(params_payload['pdb']) if params_payload['pdb'] else 0
                logger.info(f"ProteinMPN job {job_id}: pdb length = {pdb_len} bytes")

        job_payload = {
            'job_id': job_id,
            'model': model_type,
            'task': job_task,
            'params': params_payload,
            'output_path': output_path,
            'output_display_name': output_display_name,
            'team_id': team_id,
            'organization_id': organization_id,
        }
        if job_artifacts and job_metadata.get('artifacts'):
            # Build artifacts list with paths for workers
            # Workers expect: [{'name': str, 'path': str, 'filename': str, ...}]
            artifacts_for_worker = []
            for meta in job_metadata['artifacts']:
                artifact_name = meta.get('name')
                if artifact_name and artifact_name in job_artifacts:
                    artifacts_for_worker.append({
                        'name': artifact_name,
                        'path': job_artifacts[artifact_name],
                        'filename': meta.get('filename'),
                        'display_name': meta.get('display_name'),
                    })
            if artifacts_for_worker:
                job_payload['artifacts'] = artifacts_for_worker
        if target_queue:
            job_payload['target_queue'] = target_queue

        # Final verification for ProteinMPN before sending to Redis
        if job_task == 'protein_sequence_design':
            logger.info(f"ProteinMPN job {job_id}: FINAL CHECK before Redis")
            logger.info(f"ProteinMPN job {job_id}: job_payload['params'] keys = {list(job_payload['params'].keys())}")
            if 'pdb_content' in job_payload['params']:
                logger.info(f"ProteinMPN job {job_id}: CONFIRMED pdb_content present in final payload")
            logger.info(f"ProteinMPN job {job_id}: Sending to queue:new_jobs")

        # Debug logging for DiffDock jobs
        if job_task == 'molecular_docking':
            logger.info(f"[DiffDock Job {job_id}] FINAL payload check before Redis:")
            logger.info(f"[DiffDock Job {job_id}] params keys: {list(job_payload['params'].keys())}")
            for key in ['pdb_content', 'protein_sequence', 'sdf_content', 'ligand', 'sequence']:
                if key in job_payload['params']:
                    value = job_payload['params'][key]
                    value_len = len(value) if value else 0
                    logger.info(f"[DiffDock Job {job_id}] {key}: length={value_len}, has_content={bool(value)}")

        r = get_redis_connection()
        r.lpush("queue:new_jobs", json.dumps(job_payload))
    except Exception:
        logger.error(f"Failed to queue job {job_id} for user {user_id} after DB insert.", exc_info=True)
        db.execute('DELETE FROM jobs WHERE job_id = %s', (job_id,))
        db.execute('UPDATE users SET credits = credits + %s WHERE id = %s', (job_cost, billing_user_id))
        db.commit()
        return jsonify({'status': 'error', 'message': 'Failed to queue job. Please try again.'}), 500

    new_job = db.execute('SELECT j.*, u.username as creator_username FROM jobs j LEFT JOIN users u ON j.creator_id = u.id WHERE j.job_id = %s', (job_id,)).fetchone()
    # Internal pipeline jobs are hidden from the Jobs tab and reported via the
    # pipeline view instead, so don't push per-job updates or admin email for them.
    if not pipeline_id:
        assignments = db.execute('SELECT user_id FROM project_assignments WHERE project_id = %s', (project_id,)).fetchall()
        new_job_data = {k: serialize_datetime(v) for k, v in dict(new_job).items()}
        for assignment in assignments:
            socketio.emit('job_update', {'job': new_job_data}, room=f"user_{assignment['user_id']}")
            socketio.emit('user_info_update', room=f"user_{assignment['user_id']}")
        _notify_admin_new_job(job_id, job_name, model_type, new_job, user_id, project_id, job_cost)

    return jsonify(dict(new_job)), 202


def _notify_admin_new_job(job_id, job_name, model_type, new_job, user_id, project_id, job_cost):
    # Notify admin about new job submission
    try:
        admin_email = os.environ.get("ADMIN_JOB_NOTIFICATION_EMAIL", "").strip()
        if not admin_email:
            return
        subject = f"New Job Submitted: {job_name} ({model_type})"
        body = f"""
        <h2>New Job Submitted</h2>
        <p><strong>Job ID:</strong> {job_id}</p>
        <p><strong>Name:</strong> {job_name}</p>
        <p><strong>Model:</strong> {model_type}</p>
        <p><strong>User:</strong> {new_job['creator_username']} (ID: {user_id})</p>
        <p><strong>Project ID:</strong> {project_id}</p>
        <p><strong>Cost:</strong> {job_cost} credits</p>
        <p><strong>Status:</strong> Submitted</p>
        """
        send_email(admin_email, subject, body)
    except Exception as e:
        logger.error(f"Failed to send admin notification email for job {job_id}: {e}")


@app.route('/api/estimate_job_cost', methods=['POST'])
@login_required
@csrf_protect
def estimate_job_cost():
    """Estimate the credit cost for a job before submission."""
    payload = request.json or {}
    model_type = payload.get('model')

    if not model_type:
        return jsonify({'status': 'error', 'message': 'Missing model selection.'}), 400

    model_info = Config.get_model_definition(model_type)
    if not model_info:
        return jsonify({'status': 'error', 'message': 'Unknown model requested.'}), 400

    # Extract sequence and custom params
    # Strip FASTA headers if present for accurate cost calculation
    raw_sequence = (payload.get('sequence') or '').strip()
    sequence = strip_fasta_header(raw_sequence)
    custom_params = payload.get('custom_params', {})

    # Calculate adaptive cost
    estimated_cost = Config.calculate_adaptive_credit_cost(model_type, sequence, custom_params)

    return jsonify({
        'status': 'success',
        'estimated_cost': estimated_cost,
        'model': model_type,
        'model_name': model_info.get('name', model_type),
    })
