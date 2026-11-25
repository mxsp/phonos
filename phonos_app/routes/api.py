"""API routes for projects, accounts, jobs, files, settings, and help desk."""
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
from typing import Any, Iterable, Optional

import bcrypt
import redis
from flask import (
    abort,
    g,
    jsonify,
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

# Import encryption for secure job payloads
try:
    from encryption_util import encrypt
except ImportError:
    encrypt = None

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
    get_job_branch_path,
    get_job_branch_relative_path,
    get_db,
    get_team_membership,
    get_team_node_rows,
    get_project_assignment,
    get_project_storage_roots,
    get_safe_project_path,
    get_user_storage_state,
    is_valid_public_key,
    login_required,
    normalize_job_branch_name,
    normalize_project_role,
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
    tags as tags_helper,
)

# Import security decorators
from ..helpers.security import csrf_protect, rate_limit, sanitize_json_inputs, sanitize_html, sanitize_input, validate_sql_identifier
from ..helpers.file_scanner import scan_upload

IMAGE_ALLOWED_EXTENSIONS = Config.IMAGE_ALLOWED_EXTENSIONS
PROJECT_ALLOWED_EXTENSIONS = Config.PROJECT_ALLOWED_EXTENSIONS


JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}

__all__ = [
    'JOB_SUBMISSION_ALLOWED_ROLES',
    '_delete_project_logic',
    '_handle_job_submission',
]


def _delete_project_logic(db, project_id, team_id):
    """Internal logic to delete a project and its associated data."""
    project_path = get_safe_project_path(project_id, team_id)
    if not project_path:
        logger.warning(f"Attempt to delete non-existent or unauthorized project {project_id} in team {team_id}")
        return

    # Delete associated jobs and their files first, reclaiming storage from each creator
    jobs_in_project = db.execute('SELECT job_id, output_file, output_file_size, creator_id FROM jobs WHERE project_id = %s', (project_id,)).fetchall()
    for job in jobs_in_project:
        delete_job_file_and_update_storage(db, job['creator_id'], job['job_id'], job['output_file'], job['output_file_size'])

    # Delete uploaded files, reclaiming storage from each creator
    files_in_project = db.execute('SELECT id, path, size, creator_id FROM files WHERE project_id = %s', (project_id,)).fetchall()
    for file_item in files_in_project:
        if os.path.exists(file_item['path']):
            os.remove(file_item['path'])
        db.execute('UPDATE users SET storage_used = GREATEST(0, storage_used - %s) WHERE id = %s', (file_item['size'], file_item['creator_id']))


    # Delete the physical project folder
    if os.path.exists(project_path):
        try:
            shutil.rmtree(project_path)
        except OSError:
            logger.error(f"Error deleting project directory {project_path}.", exc_info=True)

    # Finally, delete the project record from the database, which cascades to jobs, files, assignments
    db.execute('DELETE FROM projects WHERE id = %s', (project_id,))


def _handle_job_submission(project_id: int, payload: dict[str, Any]):
    model_type = payload.get('model')
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

    project = db.execute(
        "SELECT t.creator_id, p.collaboration_billing_model FROM projects p JOIN teams t ON p.team_id = t.id WHERE p.id = %s",
        (project_id,)
    ).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

    project_creator_id = project['creator_id']
    collaboration_billing_model = project['collaboration_billing_model']

    billing_user_id = None
    if collaboration_billing_model == 'admin_credits':
        billing_user_id = project_creator_id
    else:  # 'collaborator_credits' or any other default
        billing_user_id = user_id

    is_active, is_visible = get_model_state(db, model_type)
    if not (is_active and is_visible):
        reason = (Config.get_model_definition(model_type) or {}).get('offline_reason')
        return jsonify({'status': 'error', 'message': reason or 'Selected model is currently offline for maintenance.'}), 400
    if not model_info.get('available', True):
        return jsonify({'status': 'error', 'message': 'Selected model is not available yet.'}), 400

    # Model-specific rate limiting
    # DiffDock rate limit removed - was 10 requests per day
    # if model_type == 'type-3':  # DiffDock
    #     from datetime import datetime, timedelta
    #     twenty_four_hours_ago = datetime.utcnow() - timedelta(days=1)
    #     recent_jobs_count = db.execute(
    #         'SELECT COUNT(*) as count FROM jobs WHERE creator_id = ? AND model = ? AND created_at >= ?',
    #         (user_id, 'type-3', twenty_four_hours_ago.isoformat())
    #     ).fetchone()['count']
    #     if recent_jobs_count >= 10:
    #         return jsonify({
    #             'status': 'error',
    #             'message': 'DiffDock rate limit exceeded. Maximum 10 requests per day. Please try again tomorrow.'
    #         }), 429

    job_task = model_info.get('task') or 'protein_folding'
    supported_inputs = set(model_info.get('input_modes') or [])
    has_sequence_input = 'sequence' in supported_inputs or 'smiles' in supported_inputs
    # SMILES models support project-file input as a quality-of-life enhancement.
    has_file_input = 'file' in supported_inputs or 'pdb' in supported_inputs or 'smiles' in supported_inputs
    if not has_sequence_input and not has_file_input:
        return jsonify({'status': 'error', 'message': 'Selected model is not ready for interactive submissions yet.'}), 400

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
        # Preserve multijob metadata before validation strips unknown fields
        multijob_fields = {}
        for mj_key in ('multijob_id', 'multijob_total', 'multijob_index'):
            if mj_key in custom_params:
                multijob_fields[mj_key] = custom_params[mj_key]

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
            # Preserve multimer chains even if not declared in custom_fields.
            if 'chains' in custom_params and isinstance(custom_params['chains'], list):
                validated_params['chains'] = custom_params['chains']
            custom_params = validated_params
            custom_params.update(multijob_fields)
            job_metadata['custom_params'] = custom_params
        elif multijob_fields:
            job_metadata['custom_params'] = multijob_fields

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

    def _strip_fasta_header(sequence: str) -> str:
        if not sequence:
            return sequence
        if '>' not in sequence:
            return ''.join(sequence.split())

        sequence_lines = []
        for line in sequence.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('>'):
                continue
            sequence_lines.append(stripped)
        return ''.join(''.join(sequence_lines).split())

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
        if job_task == 'molecule_generation':
            smiles_candidates = []
            for line in raw_contents.splitlines():
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

                token = tokens[0].strip().strip('"').strip("'")
                if not token:
                    continue
                if len(token) > 2000:
                    continue
                if ' ' in token:
                    continue

                smiles_candidates.append(token)

            if not smiles_candidates:
                raise ValueError(
                    'No valid SMILES entries were found in the selected file. '
                    'Use a .smi/.smiles file (one SMILES per line) or paste SMILES directly.'
                )

            if len(smiles_candidates) > 1:
                job_metadata['smiles_file_info'] = {
                    'detected_entries': len(smiles_candidates),
                    'selected_entry': 1,
                }
            return smiles_candidates[0]
        if _looks_like_pdb(raw_contents) or lower_path.endswith(('.pdb', '.cif', '.mmcif', '.sdf', '.mol', '.mol2')):
            return raw_contents
        if has_sequence_input or '>' in raw_contents:
            cleaned = _strip_fasta_header(raw_contents)
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
        using_file_input = bool(raw_file_id)

        if using_file_input:
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
            if not has_sequence_input:
                return jsonify({'status': 'error', 'message': 'Selected model requires an uploaded file input.'}), 400

            multimer_sequences = _extract_multimer_sequences(custom_params.get('chains') if isinstance(custom_params, dict) else None)
            # Support both single sequence and multiple sequences
            sequences_value = payload.get('sequences', [])
            if isinstance(sequences_value, list) and sequences_value:
                # Multiple sequences - pass as array
                sequence_value = ''
                params_payload = {'sequences': sequences_value}
            else:
                # Single sequence (original behavior)
                sequences_value = []
                sequence_value = (payload.get('sequence') or '').strip()
                # Multimer fallback: allow chain-based submissions even when top-level sequence is empty.
                if not sequence_value and len(multimer_sequences) >= 2:
                    sequence_value = ''.join(multimer_sequences)
                params_payload = {'sequence': sequence_value} if sequence_value else {}

        # Validate we have at least one sequence
        multimer_sequences = _extract_multimer_sequences(custom_params.get('chains') if isinstance(custom_params, dict) else None)
        has_multimer = len(multimer_sequences) >= 2
        if has_multimer and sequence_value and ':' in sequence_value:
            sequence_value = sequence_value.replace(':', '')
            if 'sequence' in params_payload:
                params_payload['sequence'] = sequence_value

        has_single = bool(sequence_value)
        has_multiple = bool(sequences_value) if 'sequences_value' in dir() else False
        if not has_single and not has_multiple and not has_multimer:
            if 'smiles' in supported_inputs:
                return jsonify({'status': 'error', 'message': 'SMILES input is required for this model.'}), 400
            _molecule_type, _valid_chars, sequence_label = _sequence_validation_profile()
            return jsonify({'status': 'error', 'message': f'{sequence_label} sequence is required for this model.'}), 400

        # Detect and reject HTML content for single sequence
        if has_single:
            html_indicators = ['<!doctype html>', '<html', '<head>', '<body>', '<script', '<meta', '<div id=']
            sequence_lower = sequence_value[:1000].lower()
            if any(indicator in sequence_lower for indicator in html_indicators):
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid file format: HTML content detected. Please upload a valid molecular structure file (PDB, MOL2, SDF, or protein sequence).'
                }), 400

            # Check if content looks like PDB format (contains ATOM or HETATM records)
            is_pdb_content = any(keyword in sequence_value[:500].upper()
                        for keyword in ['ATOM  ', 'HETATM', 'HEADER', 'REMARK'])

            # Determine max sequence length based on model type
            # Only validate length for sequence inputs, not for PDB structure files
            if not is_pdb_content and not using_file_input:
                if model_type == 'type-0':  # ESMFold
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_ESMFOLD
                elif model_type == 'type-2':  # AlphaFold2
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_ALPHAFOLD2
                elif model_type == 'type-3':  # DiffDock
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_DIFFDOCK
                elif model_type == 'type-5':  # Fold+Dock
                    max_seq_length = Config.MAX_SEQUENCE_LENGTH_FOLD_DOCK
                else:  # Other models - no limit
                    max_seq_length = None

                # Only check length if there's a limit set
                if max_seq_length is not None and len(sequence_value) > max_seq_length:
                    return jsonify({'status': 'error', 'message': f'Sequence exceeds {max_seq_length} characters.'}), 400

            # Skip amino acid validation for DiffDock, ProteinMPNN, SMILES models, and for PDB files (which contain structural data, not sequences)
            if model_type not in ['type-3', 'type-4'] and not is_pdb_content and 'smiles' not in supported_inputs and not has_multimer:
                _molecule_type, valid_chars, sequence_label = _sequence_validation_profile()
                invalid = set(sequence_value.upper()) - valid_chars
                if invalid:
                    invalid_label = 'amino acid' if sequence_label == 'protein' else sequence_label
                    return jsonify({'status': 'error', 'message': f"Invalid {invalid_label} characters: {', '.join(sorted(invalid))}."}), 400

        # Set input_type metadata
        if using_file_input:
            job_metadata['input_type'] = 'smiles_file' if 'smiles' in supported_inputs else 'file'
        elif sequences_value:
            job_metadata['input_type'] = 'sequences'
        else:
            job_metadata['input_type'] = 'sequence'

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

    # Calculate adaptive credit cost based on sequence and parameters
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

    ensure_monthly_credit_reset(db, user_id)
    user = db.execute('SELECT credits FROM users WHERE id = %s', (user_id,)).fetchone()
    if user['credits'] <= 0:
        return jsonify({'status': 'error', 'message': 'Insufficient credits to submit a job.'}), 402

    project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
    if not project:
        return jsonify({'status': 'error', 'message': 'Project not found.'}), 404
    team_id = project['team_id']

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

    job_id = str(uuid.uuid4())
    output_dir = get_job_branch_path(project_path, job_id, branch='main', create=True)
    input_dir_created = get_job_branch_path(project_path, job_id, branch='inputs', create=True)
    if not output_dir or not input_dir_created:
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

        # Handle protein input from files
        protein_input_type = custom_params.get('protein_input_type', 'file')

        if protein_input_type == 'file':
            protein_file_id = custom_params.get('protein_file') or custom_params.get('protein')
            if protein_file_id:
                try:
                    # Try as integer first (regular file)
                    try:
                        protein_file_id_int = int(protein_file_id)
                        protein_file_row = db.execute(
                            'SELECT id, project_id, path, name FROM files WHERE id = %s AND project_id = %s',
                            (protein_file_id_int, project_id)
                        ).fetchone()
                    except (ValueError, TypeError):
                        # If not an integer, try as UUID (job output)
                        protein_file_row = db.execute(
                            'SELECT job_id as id, project_id, output_file as path, output_display_name as name FROM jobs WHERE job_id = %s AND project_id = %s',
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
                        'id': protein_file_row.get('id'),
                        'name': protein_file_row.get('name')
                    }
                    logger.info(f"[DiffDock] Loaded protein file: {protein_file_row.get('name')}")
                except Exception as e:
                    return jsonify({'status': 'error', 'message': f'Error loading protein file: {str(e)}'}), 500

        # For NIM-style docking (diffdock-re), pass ligand/ligand_file_type directly without conversion
        if model_type == 'diffdock-re':
            ligand = custom_params.get('ligand', '').strip()
            if not ligand:
                return jsonify({'status': 'error', 'message': 'Ligand input is required for DiffDock-RE.'}), 400
            params_payload['ligand'] = ligand
            params_payload['ligand_file_type'] = custom_params.get('ligand_file_type', 'sdf')
        else:
            # Handle ligand input from databases or files (old DiffDock type-3)
            ligand_input_type = custom_params.get('ligand_input_type', 'smiles')
            ligand = ''
            ligand_file_content = None

        # Debug logging
        logger.info(f"[DiffDock] model_type: {model_type}")
        logger.info(f"[DiffDock] custom_params keys: {list(custom_params.keys())}")
        if model_type != 'diffdock-re':
            logger.info(f"[DiffDock] ligand_input_type: {ligand_input_type}")
            logger.info(f"[DiffDock] ligand_file value: {custom_params.get('ligand_file')}")

        if model_type != 'diffdock-re':
            # If using file input, load the ligand file content (old DiffDock type-3)
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

                    job_metadata['ligand_file'] = {
                        'id': ligand_file_row['id'],
                        'name': ligand_file_row['name']
                    }
                except (ValueError, TypeError):
                    return jsonify({'status': 'error', 'message': 'Invalid ligand file ID.'}), 400
                except Exception as e:
                    return jsonify({'status': 'error', 'message': f'Error loading ligand file: {str(e)}'}), 500

            try:
                from phonos_app.helpers.databases import fetch_ligand_for_job, convert_ligand_to_smiles

                # If file input, convert SDF/MOL to SMILES
                if ligand_input_type == 'file' and ligand_file_content:
                    try:
                        # Determine file format from extension
                        file_ext = ligand_file_row['name'].lower().split('.')[-1]
                        if file_ext not in ['sdf', 'mol', 'mol2', 'pdb']:
                            file_ext = 'sdf'  # Default to SDF

                        # Convert ligand file to SMILES using OpenBabel
                        logger.info(f"[DiffDock] Converting {file_ext.upper()} file to SMILES: {ligand_file_row['name']}")
                        ligand = convert_ligand_to_smiles(ligand_file_content, file_ext)
                        ligand_metadata = {
                            'type': 'file_converted_to_smiles',
                            'original_format': file_ext,
                            'file_id': job_metadata.get('ligand_file', {}).get('id'),
                            'file_name': job_metadata.get('ligand_file', {}).get('name')
                        }
                        logger.info(f"[DiffDock] Converted ligand file to SMILES: {ligand}")

                    except Exception as e:
                        logger.error(f"Error converting ligand file to SMILES: {str(e)}", exc_info=True)
                        return jsonify({'status': 'error', 'message': f'Error parsing ligand file: {str(e)}'}), 400
                else:
                    # Use the original fetch_ligand_for_job for SMILES/PubChem inputs
                    ligand, ligand_metadata = fetch_ligand_for_job(
                        input_type=ligand_input_type,
                        smiles=custom_params.get('ligand', '').strip(),
                        pubchem_cid=custom_params.get('ligand_pubchem_cid', '').strip(),
                        file_content=ligand_file_content
                    )

                job_metadata['ligand_source'] = ligand_metadata
            except ValueError as e:
                return jsonify({'status': 'error', 'message': str(e)}), 400
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'Error fetching ligand data: {str(e)}'}), 500

            if not ligand:
                return jsonify({'status': 'error', 'message': 'Ligand input is required for molecular docking.'}), 400

            # Ligand: Always use SMILES format (files are converted to SMILES)
            if ligand:
                params_payload['ligand'] = ligand
                # Also update custom_params so it doesn't get overwritten later
                custom_params['ligand'] = ligand

        # Protein: Unwrap JSON-encoded PDB if needed (e.g. AlphaFold2-RE / ESMFold-RE output files)
        if sequence_value and (sequence_value.lstrip().startswith('{') or sequence_value.lstrip().startswith('[')):
            try:
                wrapped = json.loads(sequence_value)
                if isinstance(wrapped, dict):
                    # AlphaFold2-RE saves {"api_response": {"pdb_content": "..."}, ...}
                    # ESMFold-RE saves {"pdbs": ["..."], ...}
                    extracted = None
                    api_resp = wrapped.get('api_response', {})
                    if isinstance(api_resp, dict):
                        extracted = api_resp.get('pdb_content') or api_resp.get('structure')
                    if not extracted:
                        pdbs = wrapped.get('pdbs', [])
                        if isinstance(pdbs, list) and pdbs:
                            extracted = pdbs[0]
                    if not extracted:
                        extracted = wrapped.get('pdb_content') or wrapped.get('structure')
                    if extracted and isinstance(extracted, str) and extracted.strip():
                        sequence_value = extracted
                        logger.info(f"[DiffDock] Unwrapped JSON-encoded PDB ({len(sequence_value)} chars)")
            except (json.JSONDecodeError, KeyError):
                pass

        # Protein: Check for PDB content (headers/atoms) vs Sequence
        if sequence_value:
            if any(keyword in sequence_value[:500].upper() for keyword in ['HEADER', 'ATOM  ', 'HETATM', 'REMARK']):
                params_payload['pdb_content'] = sequence_value
                params_payload['protein'] = sequence_value  # for DiffDock-RE worker
            else:
                params_payload['protein_sequence'] = sequence_value

        params_payload['output_pdb'] = output_path
        params_payload['output_sdf'] = sdf_path

        # Debug logging - what's being sent to worker
        logger.info("[DiffDock] Final params being sent to worker:")
        logger.info(f"[DiffDock]   - protein length: {len(sequence_value) if sequence_value else 0}")
        logger.info(f"[DiffDock]   - ligand length: {len(ligand) if ligand else 0}")
        logger.info(f"[DiffDock]   - protein type: {type(sequence_value)}")
        logger.info(f"[DiffDock]   - ligand type: {type(ligand)}")
        logger.info(f"[DiffDock]   - protein first 80 chars: {sequence_value[:80] if sequence_value else 'EMPTY'}")
        logger.info(f"[DiffDock]   - ligand first 80 chars: {ligand[:80] if ligand else 'EMPTY'}")

    elif job_task == 'fold_and_dock' or job_task == 'fold_dock_pipeline':
        # Fold+Dock Pipeline: Similar to DiffDock but with protein folding first
        sdf_filename = f"{job_id}.sdf"
        sdf_path = os.path.join(output_dir, sdf_filename)
        job_artifacts['ligand_sdf'] = sdf_path

        # Handle ligand input - same as DiffDock
        ligand_input_type = custom_params.get('ligand_input_type', 'smiles')
        ligand = ''
        ligand_file_content = None

        # If using file input, load the ligand file content
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

                job_metadata['ligand_file'] = {
                    'id': ligand_file_row['id'],
                    'name': ligand_file_row['name']
                }
            except (ValueError, TypeError):
                return jsonify({'status': 'error', 'message': 'Invalid ligand file ID.'}), 400
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'Error loading ligand file: {str(e)}'}), 500

        try:
            from phonos_app.helpers.databases import fetch_ligand_for_job, convert_ligand_to_smiles

            # If file input, convert SDF/MOL to SMILES
            if ligand_input_type == 'file' and ligand_file_content:
                try:
                    # Determine file format from extension
                    file_ext = ligand_file_row['name'].lower().split('.')[-1]
                    if file_ext not in ['sdf', 'mol', 'mol2', 'pdb']:
                        file_ext = 'sdf'  # Default to SDF

                    # Convert ligand file to SMILES using OpenBabel
                    logger.info(f"[Fold+Dock] Converting {file_ext.upper()} file to SMILES: {ligand_file_row['name']}")
                    ligand = convert_ligand_to_smiles(ligand_file_content, file_ext)
                    ligand_metadata = {
                        'type': 'file_converted_to_smiles',
                        'original_format': file_ext,
                        'file_id': job_metadata.get('ligand_file', {}).get('id'),
                        'file_name': job_metadata.get('ligand_file', {}).get('name')
                    }
                    logger.info(f"[Fold+Dock] Converted ligand file to SMILES: {ligand}")

                except Exception as e:
                    logger.error(f"Error converting ligand file to SMILES: {str(e)}", exc_info=True)
                    return jsonify({'status': 'error', 'message': f'Error parsing ligand file: {str(e)}'}), 400
            else:
                # Use the original fetch_ligand_for_job for SMILES/PubChem inputs
                ligand, ligand_metadata = fetch_ligand_for_job(
                    input_type=ligand_input_type,
                    smiles=custom_params.get('ligand', '').strip(),
                    pubchem_cid=custom_params.get('ligand_pubchem_cid', '').strip(),
                    file_content=ligand_file_content
                )

            job_metadata['ligand_source'] = ligand_metadata
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

        # Protein: Always a sequence for Fold+Dock (needs to be folded first)
        if sequence_value:
            params_payload['protein_sequence'] = sequence_value

        params_payload['output_pdb'] = output_path
        params_payload['output_sdf'] = sdf_path

        # Debug logging
        logger.info(f"[Fold+Dock] Final params being sent to worker:")
        logger.info(f"[Fold+Dock]   - protein_sequence length: {len(sequence_value) if sequence_value else 0}")
        logger.info(f"[Fold+Dock]   - ligand SMILES: {ligand[:80] if ligand else 'EMPTY'}")

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
    elif job_task == 'protein_design':
        # ProteinMPNN-RE and similar: pass PDB content as input_pdb for worker
        if sequence_value:
            params_payload['input_pdb'] = sequence_value
    elif job_task == 'molecule_generation':
        # MolMIM and similar: pass SMILES as smi for worker
        smi = sequence_value or custom_params.get('smi', '')
        if smi:
            params_payload['smi'] = smi

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
            'INSERT INTO jobs (project_id, creator_id, job_id, name, model, sequence, status, input_file_id, policy, cost) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (project_id, user_id, job_id, job_name, model_type, sequence_value, 'submitted', input_file_id, job_metadata_json, job_cost)
        )
        db.execute('INSERT INTO job_logs (job_id, status) VALUES (%s, %s)', (job_id, 'submitted'))
        # Deduct the calculated adaptive credit cost from the billing user
        db.execute('UPDATE users SET credits = credits - %s WHERE id = %s', (job_cost, billing_user_id))
        db.commit()
    except Exception:
        db.rollback()
        logger.error(f"Database error while creating job for user {user_id}.", exc_info=True)
        return jsonify({'status': 'error', 'message': 'A database error occurred while creating the job.'}), 500

    # Merge custom parameters into params_payload for worker processing
    # Skip empty-string values so custom_params don't overwrite already-set keys (e.g. sequence)
    if custom_params:
        params_payload.update({k: v for k, v in custom_params.items() if v != ''})

    try:
        target_queue = determine_job_target_queue(db, team_id, model_type)
        job_payload = {
            'job_id': job_id,
            'model': model_type,
            'task': job_task,
            'params': params_payload,
            'output_path': output_path,
            'output_display_name': output_display_name,
            'team_id': team_id,
        }
        if job_artifacts:
            job_payload['artifacts'] = job_artifacts
        if target_queue:
            job_payload['target_queue'] = target_queue
        r = get_redis_connection()
        r.lpush("queue:new_jobs", json.dumps(job_payload))
    except Exception:
        logger.error(f"Failed to queue job {job_id} for user {user_id} after DB insert.", exc_info=True)
        db.execute('DELETE FROM jobs WHERE job_id = %s', (job_id,))
        db.execute('UPDATE users SET credits = credits + 1 WHERE id = %s', (user_id,))
        db.commit()
        return jsonify({'status': 'error', 'message': 'Failed to queue job. Please try again.'}), 500

    new_job = db.execute('SELECT j.*, u.username as creator_username FROM jobs j LEFT JOIN users u ON j.creator_id = u.id WHERE j.job_id = %s', (job_id,)).fetchone()
    assignments = db.execute('SELECT user_id FROM project_assignments WHERE project_id = %s', (project_id,)).fetchall()

    # Serialize datetime fields for socket.io
    job_data = dict(new_job)
    for key in ['created_at', 'updated_at']:
        if key in job_data:
            job_data[key] = serialize_datetime(job_data[key])

    for assignment in assignments:
        socketio.emit('job_update', {'job': job_data}, room=f"user_{assignment['user_id']}")
        socketio.emit('user_info_update', room=f"user_{assignment['user_id']}")
    return jsonify(dict(new_job)), 202


@app.route('/api/system/warnings', methods=['GET'])
@login_required
def get_active_system_warnings():
    """Get all active system warnings."""
    db = get_db()
    warnings = db.execute(
        'SELECT * FROM system_warnings WHERE is_active = 1 ORDER BY created_at DESC'
    ).fetchall()
    return jsonify([dict(w) for w in warnings])
