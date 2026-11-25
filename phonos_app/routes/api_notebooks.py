"""
Phonos Notebook API Routes
Handles notebook CRUD operations and kernel management.
"""
import os
import re
import json
import asyncio
import shutil
import zipfile
import logging
import mimetypes
import unicodedata
from io import BytesIO
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, g, current_app, session, send_file
from werkzeug.utils import secure_filename

from phonos_app.helpers.auth import login_required
from phonos_app.helpers.database import get_db
from phonos_app.helpers.kernel_manager import kernel_manager
from phonos_app.helpers.security import csrf_protect

logger = logging.getLogger(__name__)

notebook_bp = Blueprint('notebook', __name__, url_prefix='/api/notebooks')

MAX_NOTEBOOK_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 2000
MAX_NOTEBOOK_CELLS = 500
MAX_CELL_SOURCE_LENGTH = 200000
# Must stay in sync with NB_EXEC_TYPES in static/js/notebook.js, plus the two
# non-executable types. An unknown type is coerced to 'code' in _normalize_cells,
# so anything missing here is silently rewritten into a Python cell on save --
# which is how Wolfram cells started failing with a Python SyntaxError on '(* *)'.
ALLOWED_CELL_TYPES = {
    'code', 'markdown', 'raw',
    'wolfram', 'quantum',
    'c', 'cpp', 'rust', 'r',
}


def _serialize_timestamp(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _is_within_directory(base_dir, target_path):
    """Return True when target_path resolves within base_dir."""
    base_real = os.path.realpath(base_dir)
    target_real = os.path.realpath(target_path)
    return target_real == base_real or target_real.startswith(base_real + os.sep)


def _normalize_relative_path(raw_path):
    """Normalize and validate a user-provided relative path."""
    if raw_path is None:
        return ''

    if not isinstance(raw_path, str):
        raw_path = str(raw_path)

    candidate = raw_path.strip().replace('\\', '/').lstrip('/')
    if not candidate or candidate == '.':
        return ''

    if '\x00' in candidate or '\n' in candidate or '\r' in candidate:
        raise ValueError('Invalid path')

    normalized = os.path.normpath(candidate).replace('\\', '/')
    if normalized in ('', '.'):
        return ''
    if normalized == '..' or normalized.startswith('../'):
        raise ValueError('Invalid path')

    return normalized


def _slugify_notebook_name(name):
    """
    Convert a notebook name into a filesystem-safe slug.

    "My Analysis #2" -> "my_analysis_2". Falls back to "notebook" when the
    name contains no slug-worthy characters.
    """
    if not isinstance(name, str):
        name = ''
    slug = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    slug = slug.lower()
    slug = re.sub(r'[^a-z0-9]+', '_', slug)
    slug = slug.strip('_')
    return slug or 'notebook'


def _resolve_notebook_workspace(notebook, create=False):
    """
    Return the absolute path of a notebook's workspace folder.

    Naming scheme (per product decision 2026-07-07):
        <NOTEBOOK_FILES_DIR>/<slugified_name>_workspace/

    Backward compatibility: notebooks that already have a legacy
    ``<NOTEBOOK_FILES_DIR>/<notebook_id>/`` folder on disk keep using it so
    existing files are not disturbed. Only *new* notebooks (no legacy folder)
    adopt the ``<name>_workspace`` scheme.

    Ownership / collision handling: when a new notebook's ``<slug>_workspace``
    folder is first created, a hidden ``.nbowner`` marker file is written
    inside it recording the owning notebook id. On subsequent resolutions the
    marker is read: if the folder exists but is owned by a *different*
    notebook (a same-name collision), this notebook falls back to the unique
    ``<slug>_<id>_workspace`` folder. This avoids re-running collision
    detection blindly (which would mistake a notebook's own existing folder
    for a collision on every read) and needs no DB state.

    Args:
        notebook: notebook dict (must contain id + name).
        create: when True the folder is created on disk if missing.
    """
    base_path = current_app.config.get(
        'NOTEBOOK_FILES_DIR', '/data/notebook_files'
    )
    notebook_id = notebook['id']

    # Backward compat: a pre-existing legacy <id>/ folder keeps being used.
    legacy_path = os.path.join(base_path, str(notebook_id))
    if os.path.isdir(legacy_path):
        return legacy_path

    slug = _slugify_notebook_name(notebook.get('name'))
    candidate = os.path.join(base_path, f'{slug}_workspace')

    # If the candidate folder already exists, check who owns it. A folder
    # owned by another notebook (same-name collision) forces the unique
    # id-qualified path. A folder with no marker is adopted as this
    # notebook's own (covers folders created by notebook code).
    if os.path.isdir(candidate):
        owner = _read_workspace_owner(candidate)
        if owner is not None and str(owner) != str(notebook_id):
            candidate = os.path.join(base_path, f'{slug}_{notebook_id}_workspace')

    if create:
        try:
            os.makedirs(candidate, exist_ok=True)
        except OSError:
            # Fall back to the unique ID-qualified path if the chosen folder
            # could not be created (e.g. a stale same-named folder).
            candidate = os.path.join(base_path, f'{slug}_{notebook_id}_workspace')
            os.makedirs(candidate, exist_ok=True)
        _write_workspace_owner(candidate, notebook_id)

    return candidate


_NB_OWNER_MARKER = '.nbowner'


def _read_workspace_owner(workspace_dir):
    """Return the owning notebook id recorded in the workspace, or None."""
    marker = os.path.join(workspace_dir, _NB_OWNER_MARKER)
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except (OSError, ValueError):
        return None


def _write_workspace_owner(workspace_dir, notebook_id):
    """Record the owning notebook id, but never overwrite an existing owner."""
    marker = os.path.join(workspace_dir, _NB_OWNER_MARKER)
    if os.path.exists(marker):
        return
    try:
        with open(marker, 'w', encoding='utf-8') as f:
            f.write(str(notebook_id))
    except OSError:
        # Marker is best-effort; its absence just disables collision
        # disambiguation, which is safe (falls back to adopting the folder).
        pass


def _notebook_file_full_path(notebook, relative_path, create_root=False):
    """Resolve a relative workspace path to an absolute, validated path."""
    workspace = _resolve_notebook_workspace(notebook, create=create_root)
    relative_path = _normalize_relative_path(relative_path)
    full_path = os.path.join(workspace, relative_path) if relative_path else workspace
    if not _is_within_directory(workspace, full_path):
        raise ValueError('Invalid path')
    return full_path


def _resolve_project_role(db, user_id, project_id):
    """Resolve a user's role for a project, including team fallback membership."""
    assignment = db.execute('''
        SELECT role FROM project_assignments
        WHERE project_id = %s AND user_id = %s
    ''', (project_id, user_id)).fetchone()
    if assignment:
        return assignment['role']

    project = db.execute(
        'SELECT id, team_id, organization_id FROM projects WHERE id = %s',
        (project_id,)
    ).fetchone()
    if not project:
        return None

    team_id = project['team_id'] or project['organization_id']
    if not team_id:
        return None

    team_member = db.execute(
        'SELECT role FROM team_members WHERE team_id = %s AND user_id = %s',
        (team_id, user_id)
    ).fetchone()
    if not team_member:
        return None

    role = (team_member['role'] or '').lower()
    if role in ('owner', 'admin'):
        return 'Project Manager'
    if role == 'member':
        return 'Project Contributor'
    return 'Project Viewer'


def _build_project_container_path(raw_path, fallback_name='', job_id=None):
    """Convert a host filesystem path to the notebook container path."""
    if raw_path and isinstance(raw_path, str):
        normalized = raw_path.replace('\\', '/')

        if '/uploads/' in normalized:
            relative = normalized.split('/uploads/', 1)[1].lstrip('/')
            return f"/project/uploads/{relative}" if relative else '/project/uploads'

        if '/jobs/' in normalized:
            relative = normalized.split('/jobs/', 1)[1].lstrip('/')
            return f"/project/jobs/{relative}" if relative else '/project/jobs'

        if '/projects/' in normalized:
            parts = normalized.split('/projects/', 1)[1].split('/', 1)
            if len(parts) == 2 and parts[1]:
                return f"/project/{parts[1].lstrip('/')}"

        basename = os.path.basename(normalized)
        if basename:
            return f"/project/{basename}"

    safe_name = (fallback_name or '').strip('/ ')
    if safe_name:
        return f"/project/{safe_name}"

    if job_id:
        return f"/project/jobs/{job_id}"

    return '/project/'


def get_current_user():
    """Get the current user from session and database."""
    user_id = session.get('user_id')
    if not user_id:
        return None

    db = get_db()
    user = db.execute(
        'SELECT id, username FROM users WHERE id = %s',
        (user_id,)
    ).fetchone()

    if user:
        return dict(user)

    # Graceful fallback for transient states where session is valid but
    # user lookup is temporarily unavailable (e.g. test bootstrap mismatch).
    return {
        'id': user_id,
        'username': session.get('username', 'unknown')
    }


def async_route(f):
    """Decorator to run async route handlers"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        finally:
            loop.close()
    return wrapper


def check_disk_space(path, min_free_mb=100):
    """Check if there's enough disk space available"""
    try:
        stat = os.statvfs(path)
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        return free_mb >= min_free_mb
    except OSError:
        return True  # Assume ok if we can't check


def safe_file_operation(operation, error_message="File operation failed"):
    """Decorator for safe file operations with proper error handling"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except PermissionError as e:
                current_app.logger.error(f"Permission denied: {e}")
                return jsonify({'error': f'{error_message}: Permission denied'}), 403
            except OSError as e:
                if e.errno == 28:  # ENOSPC - No space left on device
                    current_app.logger.error(f"Disk space error: {e}")
                    return jsonify({'error': f'{error_message}: Not enough disk space'}), 507
                elif e.errno == 36:  # ENAMETOOLONG - File name too long
                    current_app.logger.error(f"File name too long: {e}")
                    return jsonify({'error': f'{error_message}: Filename too long'}), 400
                else:
                    current_app.logger.error(f"OS error: {e}")
                    return jsonify({'error': f'{error_message}: {str(e)}'}), 500
            except FileNotFoundError as e:
                current_app.logger.error(f"File not found: {e}")
                return jsonify({'error': f'{error_message}: File not found'}), 404
            except Exception as e:
                current_app.logger.error(f"Unexpected error: {e}")
                return jsonify({'error': f'{error_message}: {str(e)}'}), 500
        return wrapper
    return decorator


def validate_project_scope(required_role=None):
    """
    Decorator to validate that the user has access to the project specified in the request.

    Args:
        required_role: Optional minimum role required (e.g., 'Project Manager', 'Project Contributor')
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({'error': 'Authentication required'}), 401

            db = get_db()

            # Try to get project_id from various sources
            project_id = None

            # From URL parameters
            if 'project_id' in kwargs:
                project_id = kwargs['project_id']

            # From request body
            if not project_id:
                data = request.get_json(silent=True) or {}
                project_id = data.get('project_id')

            if not project_id:
                return jsonify({'error': 'project_id is required'}), 400

            # Check project access
            assignment = db.execute('''
                SELECT role FROM project_assignments
                WHERE user_id = %s AND project_id = %s
            ''', (user['id'], project_id)).fetchone()

            if not assignment:
                # Check team membership
                project = db.execute(
                    'SELECT team_id, organization_id FROM projects WHERE id = %s',
                    (project_id,)
                ).fetchone()

                if not project:
                    return jsonify({'error': 'Project not found'}), 404

                team_id = project['team_id'] or project['organization_id']
                if team_id:
                    team_member = db.execute(
                        'SELECT role FROM team_members WHERE team_id = %s AND user_id = %s',
                        (team_id, user['id'])
                    ).fetchone()

                    if team_member:
                        role = team_member['role']
                        if role in ('owner', 'admin'):
                            user_role = 'Project Manager'
                        elif role == 'member':
                            user_role = 'Project Contributor'
                        else:
                            user_role = 'Project Viewer'
                    else:
                        return jsonify({'error': 'No access to this project'}), 403
                else:
                    return jsonify({'error': 'No access to this project'}), 403
            else:
                user_role = assignment['role']

            # Validate required role
            if required_role:
                role_hierarchy = {
                    'Project Viewer': 1,
                    'Project Contributor': 2,
                    'Project Manager': 3
                }
                user_level = role_hierarchy.get(user_role, 0)
                required_level = role_hierarchy.get(required_role, 0)

                if user_level < required_level:
                    return jsonify({'error': f'Insufficient permissions. Required: {required_role}'}), 403

            g.current_project_id = project_id
            g.current_project_role = user_role

            return f(*args, **kwargs)
        return wrapper
    return decorator


def check_notebook_access(notebook_id, user_id, require_write=False):
    """Check if user has access to a notebook with user-project isolation"""
    db = get_db()

    notebook = db.execute('''
        SELECT n.*, pa.role as project_role, p.team_id
        FROM notebooks n
        LEFT JOIN projects p ON n.project_id = p.id
        LEFT JOIN project_assignments pa ON n.project_id = pa.project_id AND pa.user_id = %s
        WHERE n.id = %s
    ''', (user_id, notebook_id)).fetchone()

    if not notebook:
        return None, 'Notebook not found'

    # Owner has full access
    if notebook['creator_id'] == user_id:
        result = dict(notebook)
        result['project_access_level'] = 'owner'
        return result, None

    # Check project access - notebooks must belong to a project
    if notebook['project_id']:
        if notebook['project_role']:
            # Check for write access if required
            if require_write and notebook['project_role'] not in ('Project Manager', 'Project Contributor'):
                return None, 'No write access to notebook'
            result = dict(notebook)
            result['project_access_level'] = notebook['project_role']
            return result, None
        return None, 'No access to this notebook'

    return None, 'No access to this notebook'


def _normalized_notebook_name(name):
    """Normalize notebook names for consistency and UI safety."""
    if not isinstance(name, str):
        return 'Untitled Notebook'
    cleaned = name.strip()
    if not cleaned:
        cleaned = 'Untitled Notebook'
    return cleaned[:MAX_NOTEBOOK_NAME_LENGTH]


def _normalized_description(description):
    """Normalize notebook description with sensible size limits."""
    if not isinstance(description, str):
        return ''
    return description.strip()[:MAX_DESCRIPTION_LENGTH]


def _normalize_cells(cells):
    """Validate and normalize notebook cells for persistence."""
    if not isinstance(cells, list):
        return []

    normalized = []
    for raw_cell in cells[:MAX_NOTEBOOK_CELLS]:
        if not isinstance(raw_cell, dict):
            continue

        cell_type = raw_cell.get('type', 'code')
        if cell_type not in ALLOWED_CELL_TYPES:
            cell_type = 'code'

        source = raw_cell.get('source', '')
        if not isinstance(source, str):
            source = str(source)

        outputs = raw_cell.get('outputs', [])
        if not isinstance(outputs, list):
            outputs = []

        metadata = raw_cell.get('metadata', {})
        if not isinstance(metadata, dict):
            metadata = {}

        normalized.append({
            'id': raw_cell.get('id'),
            'type': cell_type,
            'source': source[:MAX_CELL_SOURCE_LENGTH],
            'outputs': outputs,
            'execution_count': raw_cell.get('execution_count'),
            'metadata': metadata,
        })

    return normalized


def _parse_notebook_metadata(raw_metadata):
    """Best-effort parse of notebook metadata payloads stored as JSON or dicts."""
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _notebooks_has_settings_column(db) -> bool:
    """Check whether the notebooks table exposes a dedicated settings column."""
    try:
        row = db.execute(
            '''
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'notebooks' AND column_name = 'settings'
            LIMIT 1
            '''
        ).fetchone()
        return bool(row)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _normalize_notebook_metadata(raw_metadata):
    """Normalize notebook metadata payloads to a dict."""
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


# ============================================================================
# NOTEBOOK CRUD
# ============================================================================

@notebook_bp.route('', methods=['GET'])
@login_required
def list_notebooks():
    """List all notebooks accessible to the user with user-project isolation"""
    user = get_current_user()
    db = get_db()

    # Get notebooks from user's projects and personal notebooks
    # This query ensures we only return notebooks the user has access to
    project_id = request.args.get('project_id', type=int)
    query_limit = request.args.get('limit', default=100, type=int)
    query_limit = min(max(query_limit or 100, 1), 300)

    notebooks = db.execute('''
        SELECT n.id, n.name, n.project_id,
               n.created_at, n.updated_at, n.metadata, n.cells,
               p.name as project_name,
               u.username as creator_name,
               (SELECT COUNT(*) FROM notebook_sessions ns
                WHERE ns.notebook_id = n.id AND ns.kernel_status IN ('idle', 'busy')) as active_sessions
        FROM notebooks n
        LEFT JOIN projects p ON n.project_id = p.id
        LEFT JOIN users u ON n.creator_id = u.id
        WHERE (n.creator_id = %s
           OR n.project_id IN (
               SELECT project_id FROM project_assignments WHERE user_id = %s
           ))
        AND (%s IS NULL OR n.project_id = %s)
        ORDER BY n.updated_at DESC
        LIMIT %s
    ''', (user['id'], user['id'], project_id, project_id, query_limit)).fetchall()

    notebook_list = []
    for nb in notebooks:
        # Handle SQLite JSON stored as string
        metadata = nb['metadata']
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}
        elif not isinstance(metadata, dict):
            metadata = {}

        # Parse cells to count outputs
        cells_data = nb['cells']
        if isinstance(cells_data, str):
            try:
                cells_data = json.loads(cells_data)
            except (ValueError, TypeError):
                cells_data = []
        elif not isinstance(cells_data, list):
            cells_data = []

        total_outputs = 0
        for cell in cells_data:
            outputs = cell.get('outputs', [])
            if isinstance(outputs, list):
                total_outputs += len(outputs)

        notebook_list.append({
            'id': nb['id'],
            'name': nb['name'],
            'description': metadata.get('description', ''),
            'project_id': nb['project_id'],
            'project_name': nb['project_name'],
            'creator_name': nb['creator_name'],
            'kernel_type': metadata.get('kernel', 'python3'),
            'active_sessions': nb['active_sessions'],
            'cell_count': len(cells_data),
            'output_count': total_outputs,
            'created_at': _serialize_timestamp(nb['created_at']),
            'updated_at': _serialize_timestamp(nb['updated_at']),
        })

    return jsonify({'notebooks': notebook_list, 'count': len(notebook_list)})


@notebook_bp.route('', methods=['POST'])
@login_required
@validate_project_scope(required_role='Project Contributor')
def create_notebook():
    """Create a new notebook with project isolation"""
    user = get_current_user()
    data = request.get_json() or {}
    db = get_db()

    name = _normalized_notebook_name(data.get('name', 'Untitled Notebook'))
    description = _normalized_description(data.get('description', ''))
    project_id = data.get('project_id')
    kernel_type = data.get('kernel_type', 'python3')
    cells = _normalize_cells(data.get('cells', []))
    metadata = data.get('metadata', {})
    if not isinstance(metadata, dict):
        metadata = {}

    current_app.logger.info(f"create_notebook: user_id={user['id']}, project_id={project_id}, name={name}")

    # Store description and kernel_type in metadata
    metadata['description'] = description
    metadata['kernel'] = kernel_type

    # Notebooks must belong to a project (enforces user-project isolation)
    if not project_id:
        current_app.logger.warning(f"create_notebook: missing project_id for user {user['id']}")
        return jsonify({'error': 'project_id is required - notebooks must belong to a project'}), 400

    # Verify project access using the validated scope from decorator
    validated_project_id = g.get('current_project_id')
    if str(project_id) != str(validated_project_id):
        current_app.logger.warning(f"create_notebook: project scope violation for user {user['id']}, expected {validated_project_id}, got {project_id}")
        return jsonify({'error': 'Project scope violation'}), 403

    project_access = db.execute('''
        SELECT role FROM project_assignments
        WHERE project_id = %s AND user_id = %s
    ''', (project_id, user['id'])).fetchone()

    if not project_access or project_access['role'] not in ('Project Manager', 'Project Contributor'):
        current_app.logger.warning(f"create_notebook: no write access for user {user['id']} in project {project_id}")
        return jsonify({'error': 'No write access to project'}), 403

    try:
        cursor = db.execute('''
            INSERT INTO notebooks (project_id, user_id, creator_id, name, cells, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
        ''', (project_id, user['id'], user['id'], name, json.dumps(cells), json.dumps(metadata)))

        result = cursor.fetchone()
        db.commit()
        current_app.logger.info(f"create_notebook: successfully created notebook {result['id']} for user {user['id']}")

        return jsonify({
            'id': result['id'],
            'created_at': result['created_at'].isoformat() if hasattr(result['created_at'], 'isoformat') else str(result['created_at'])
        }), 201
    except Exception as e:
        current_app.logger.error(f"create_notebook: failed to create notebook for user {user['id']}: {e}")
        raise


@notebook_bp.route('/<int:notebook_id>', methods=['GET'])
@login_required
def get_notebook(notebook_id):
    """Get a specific notebook with all cells"""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    db = get_db()
    # Get full notebook with project info
    nb = db.execute('''
        SELECT n.*, p.name as project_name, u.username as creator_name
        FROM notebooks n
        LEFT JOIN projects p ON n.project_id = p.id
        LEFT JOIN users u ON n.creator_id = u.id
        WHERE n.id = %s
    ''', (notebook_id,)).fetchone()

    metadata = nb['metadata'] if isinstance(nb['metadata'], dict) else (json.loads(nb['metadata']) if nb['metadata'] else {})

    # Check for active session
    active_session_info = None
    existing_session = db.execute('''
        SELECT session_token, kernel_status
        FROM notebook_sessions
        WHERE notebook_id = %s AND user_id = %s
        AND kernel_status IN ('idle', 'busy', 'starting')
        AND ended_at IS NULL
    ''', (notebook_id, user['id'])).fetchone()

    if existing_session:
        # Verify it's alive
        ks = kernel_manager.get_session(existing_session['session_token'])
        if ks:
            active_session_info = {
                'session_id': existing_session['session_token'],
                'status': ks.status
            }
        # We don't clean up here (GET request shouldn't modify state ideally), 
        # but we just won't report it as active if it's dead.

    return jsonify({
        'notebook': {
            'id': nb['id'],
            'name': nb['name'],
            'description': metadata.get('description', ''),
            'project_id': nb['project_id'],
            'project_name': nb['project_name'],
            'creator_id': nb['creator_id'],
            'creator_name': nb['creator_name'],
            'kernel_type': metadata.get('kernel', 'python3'),
            'cells': nb['cells'] if isinstance(nb['cells'], list) else (json.loads(nb['cells']) if nb['cells'] else []),
            'metadata': metadata,
            'created_at': nb['created_at'].isoformat() if hasattr(nb['created_at'], 'isoformat') else (str(nb['created_at']) if nb['created_at'] else None),
            'updated_at': nb['updated_at'].isoformat() if hasattr(nb['updated_at'], 'isoformat') else (str(nb['updated_at']) if nb['updated_at'] else None),
        },
        'active_session': active_session_info
    })


@notebook_bp.route('/<int:notebook_id>', methods=['PUT'])
@login_required
def update_notebook(notebook_id):
    """Update a notebook"""
    user = get_current_user()
    data = request.get_json() or {}

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    db = get_db()

    # Build update query dynamically
    updates = []
    params = []

    if 'name' in data:
        updates.append('name = %s')
        params.append(_normalized_notebook_name(data['name']))

    if 'description' in data:
        updates.append('description = %s')
        params.append(_normalized_description(data['description']))

    if 'cells' in data:
        updates.append('cells = %s')
        params.append(json.dumps(_normalize_cells(data['cells'])))

    metadata_payload = None
    if 'metadata' in data:
        metadata_payload = data['metadata']
        if not isinstance(metadata_payload, dict):
            return jsonify({'error': 'metadata must be an object'}), 400

    settings_payload = None
    if 'settings' in data:
        settings_payload = data['settings']
        if not isinstance(settings_payload, dict):
            return jsonify({'error': 'settings must be an object'}), 400

    if settings_payload is not None:
        if _notebooks_has_settings_column(db):
            updates.append('settings = %s')
            params.append(json.dumps(settings_payload))
        else:
            # Backward compatibility: persist settings inside metadata when the
            # dedicated settings column is unavailable.
            if metadata_payload is None:
                metadata_payload = _parse_notebook_metadata(notebook.get('metadata'))
            metadata_payload['settings'] = settings_payload

    if metadata_payload is not None:
        updates.append('metadata = %s')
        params.append(json.dumps(metadata_payload))

    if not updates:
        return jsonify({'error': 'No fields to update'}), 400

    updates.append('updated_at = CURRENT_TIMESTAMP')
    params.append(notebook_id)

    try:
        db.execute(f'''
            UPDATE notebooks
            SET {', '.join(updates)}
            WHERE id = %s
        ''', params)
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.error(f"Failed to update notebook {notebook_id}: {e}")
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'success': True,
        'updated_at': datetime.utcnow().isoformat()
    })


@notebook_bp.route('/<int:notebook_id>', methods=['DELETE'])
@login_required
def delete_notebook(notebook_id):
    """Delete a notebook"""
    user = get_current_user()
    db = get_db()

    notebook = db.execute(
        'SELECT creator_id FROM notebooks WHERE id = %s',
        (notebook_id,)
    ).fetchone()

    if not notebook:
        return jsonify({'error': 'Notebook not found'}), 404

    if notebook['creator_id'] != user['id']:
        return jsonify({'error': 'Only the owner can delete this notebook'}), 403

    # Stop any active sessions first
    active_sessions = db.execute('''
        SELECT session_token FROM notebook_sessions
        WHERE notebook_id = %s AND kernel_status IN ('idle', 'busy', 'starting')
    ''', (notebook_id,)).fetchall()

    for sess in active_sessions:
        try:
            asyncio.run(kernel_manager.stop_kernel(sess['session_token']))
        except:
            pass

    # Delete notebook (cascade will delete sessions and files)
    db.execute('DELETE FROM notebooks WHERE id = %s', (notebook_id,))
    db.commit()

    return jsonify({'success': True})


# ============================================================================
# KERNEL MANAGEMENT
# ============================================================================

@notebook_bp.route('/<int:notebook_id>/kernel/start', methods=['POST'])
@login_required
@async_route
async def start_kernel(notebook_id):
    """Start a kernel for a notebook"""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    db = get_db()
    req_data = request.get_json(silent=True) or {}
    runtime_level = req_data.get('runtime_level', 'basic')
    force_restart = bool(req_data.get('force_restart'))

    # Check for existing active session
    existing = db.execute('''
        SELECT session_token, kernel_status FROM notebook_sessions
        WHERE notebook_id = %s AND user_id = %s
        AND kernel_status IN ('idle', 'busy', 'starting')
        AND ended_at IS NULL
    ''', (notebook_id, user['id'])).fetchone()

    if existing:
        if force_restart:
            current_app.logger.info(f"Force-restarting session {existing['session_token']} for notebook {notebook_id}")
            try:
                await kernel_manager.stop_kernel(existing['session_token'])
            except Exception as stop_error:
                current_app.logger.warning(f"Failed to stop existing session {existing['session_token']}: {stop_error}")
            db.execute('''
                UPDATE notebook_sessions
                SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP
                WHERE session_token = %s
            ''', (existing['session_token'],))
            db.commit()
        else:
        # Verify session is actually alive in memory/container
            # If server restarted, DB might be stale
            active_session = kernel_manager.get_session(existing['session_token'])

            if active_session:
                return jsonify({
                    'session_id': existing['session_token'],
                    'status': active_session.status, # Use actual status
                    'message': 'Using existing session'
                })
            else:
                current_app.logger.warning(f"Found stale session {existing['session_token']} in DB, marking as disconnected")
                # Mark as dead and continue to create new one
                db.execute('''
                    UPDATE notebook_sessions
                    SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP, error_message = 'Session lost (server restart)'
                    WHERE session_token = %s
                ''', (existing['session_token'],))
                db.commit()

    # Check user's session limit - and prune dead sessions first
    max_sessions = current_app.config.get('NOTEBOOK_MAX_SESSIONS_PER_USER', 2)
    
    # Get potentially active sessions from DB
    candidate_sessions = db.execute('''
        SELECT session_token FROM notebook_sessions
        WHERE user_id = %s AND kernel_status IN ('idle', 'busy', 'starting')
    ''', (user['id'],)).fetchall()
    
    current_app.logger.info(f"StartKernel: Checking {len(candidate_sessions)} candidates for user {user['id']}")

    active_count = 0
    for cand in candidate_sessions:
        # Check if actually running
        ks = kernel_manager.get_session(cand['session_token'])
        if ks:
            current_app.logger.info(f"StartKernel: Session {cand['session_token'][:8]} is active")
            active_count += 1
        else:
            current_app.logger.info(f"StartKernel: Session {cand['session_token'][:8]} is dead, pruning")
            # Stale record, clean it up
            db.execute('''
                UPDATE notebook_sessions
                SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP, error_message = 'Session pruned during start_kernel'
                WHERE session_token = %s
            ''', (cand['session_token'],))
            db.commit() # Commit immediately to free up slot

    current_app.logger.info(f"StartKernel: Final active count: {active_count}")

    if active_count >= max_sessions:
        return jsonify({
            'error': f'Maximum {max_sessions} active sessions allowed. Please stop an existing session first.'
        }), 429

    try:
        # Initialize kernel manager config
        kernel_manager.config = {
            'NOTEBOOK_WORKSPACE_BASE': current_app.config.get('NOTEBOOK_WORKSPACE_BASE', '/data/notebooks'),
            'UPLOAD_FOLDER': current_app.config.get('UPLOAD_FOLDER', '/data/uploads'),
            'NOTEBOOK_MEM_LIMIT': current_app.config.get('NOTEBOOK_MEM_LIMIT', '2g'),
            'NOTEBOOK_CPU_QUOTA': current_app.config.get('NOTEBOOK_CPU_QUOTA', 1.0),
            'NOTEBOOK_KERNEL_IMAGE': current_app.config.get('NOTEBOOK_KERNEL_IMAGE'),
        }

        # Get project info and user's role for file access
        team_id = None
        has_write_access = False

        if notebook['project_id']:
            project_info = db.execute('''
                SELECT p.team_id, pa.role
                FROM projects p
                LEFT JOIN project_assignments pa ON p.id = pa.project_id AND pa.user_id = %s
                WHERE p.id = %s
            ''', (user['id'], notebook['project_id'])).fetchone()

            if project_info:
                team_id = project_info.get('team_id')
                role = project_info.get('role')
                # Grant write access for owners, editors, and admins
                if role in ('owner', 'editor', 'admin', 'Project Manager', 'Project Contributor'):
                    has_write_access = True

        session = await kernel_manager.start_kernel(
            user_id=user['id'],
            notebook_id=notebook_id,
            project_id=notebook['project_id'],
            team_id=team_id,
            has_write_access=has_write_access,
            runtime_level=runtime_level
        )

        # Store session in database
        try:
            db.execute('''
                INSERT INTO notebook_sessions
                (notebook_id, user_id, container_id, session_token, kernel_status, workspace_path, runtime_level)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (notebook_id, user['id'], session.container_id,
                  session.session_id, session.status, session.workspace_path, runtime_level))
            db.commit()
        except Exception as db_error:
            # Fallback: try without runtime_level if column doesn't exist
            if 'runtime_level' in str(db_error):
                current_app.logger.warning(f"runtime_level column missing, falling back to insert without it: {db_error}")
                db.rollback()
                db.execute('''
                    INSERT INTO notebook_sessions
                    (notebook_id, user_id, container_id, session_token, kernel_status, workspace_path)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (notebook_id, user['id'], session.container_id,
                      session.session_id, session.status, session.workspace_path))
                db.commit()
            else:
                raise

        return jsonify({
            'session_id': session.session_id,
            'status': session.status,
            'workspace': session.workspace_path,
            'project_files_count': session.project_files_count,
            'has_project_files': session.project_files_count > 0,
            'runtime_level': runtime_level
        })

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.error(f"Failed to start kernel: {e}")
        return jsonify({'error': str(e)}), 500


@notebook_bp.route('/kernel/<session_id>/stop', methods=['POST'])
@login_required
@async_route
async def stop_kernel(session_id):
    """Stop a running kernel"""
    user = get_current_user()
    db = get_db()

    # Verify session belongs to user
    session_record = db.execute('''
        SELECT * FROM notebook_sessions
        WHERE session_token = %s AND user_id = %s
    ''', (session_id, user['id'])).fetchone()

    if not session_record:
        return jsonify({'error': 'Session not found'}), 404

    try:
        await kernel_manager.stop_kernel(session_id)

        db.execute('''
            UPDATE notebook_sessions
            SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP
            WHERE session_token = %s
        ''', (session_id,))
        db.commit()

        return jsonify({'success': True})

    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.error(f"Failed to stop kernel: {e}")
        return jsonify({'error': str(e)}), 500


@notebook_bp.route('/kernel/<session_id>/status', methods=['GET'])
@login_required
def get_kernel_status(session_id):
    """Get kernel status"""
    user = get_current_user()
    db = get_db()

    # Check database first
    session_record = db.execute('''
        SELECT kernel_status, last_activity, started_at
        FROM notebook_sessions
        WHERE session_token = %s AND user_id = %s
    ''', (session_id, user['id'])).fetchone()

    if not session_record:
        return jsonify({'status': 'not_found'})

    # Get live status from kernel manager
    session = kernel_manager.get_session(session_id)
    if session:
        return jsonify({
            'status': session.status,
            'execution_count': session.execution_count,
            'last_activity': session.last_activity.isoformat()
        })

    return jsonify({
        'status': session_record['kernel_status'],
        'last_activity': session_record['last_activity'].isoformat() if session_record['last_activity'] else None
    })


@notebook_bp.route('/kernel/<session_id>/interrupt', methods=['POST'])
@login_required
@async_route
async def interrupt_kernel(session_id):
    """Interrupt kernel execution"""
    user = get_current_user()
    db = get_db()

    # Verify session belongs to user
    session_record = db.execute('''
        SELECT id FROM notebook_sessions
        WHERE session_token = %s AND user_id = %s
    ''', (session_id, user['id'])).fetchone()

    if not session_record:
        return jsonify({'error': 'Session not found'}), 404

    try:
        await kernel_manager.interrupt_kernel(session_id)
        return jsonify({'success': True})
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


@notebook_bp.route('/sessions', methods=['GET'])
@login_required
def list_user_sessions():
    """List all active sessions for the user"""
    user = get_current_user()
    db = get_db()

    sessions = db.execute('''
        SELECT ns.*, n.name as notebook_name, n.project_id
        FROM notebook_sessions ns
        JOIN notebooks n ON ns.notebook_id = n.id
        WHERE ns.user_id = %s
        AND ns.kernel_status IN ('idle', 'busy', 'starting')
        AND ns.ended_at IS NULL
        ORDER BY ns.started_at DESC
    ''', (user['id'],)).fetchall()

    # Verify sessions have live containers
    verified_sessions = []
    for s in sessions:
        session = kernel_manager.get_session(s['session_token'])
        if session:
            verified_sessions.append({
                'session_id': s['session_token'],
                'notebook_id': s['notebook_id'],
                'project_id': s['project_id'],
                'notebook_name': s['notebook_name'],
                'status': session.status,
                'started_at': s['started_at'].isoformat() if s['started_at'] else None,
                'last_activity': session.last_activity.isoformat() if session else None,
            })
        else:
            # Clean up orphaned session
            db.execute('''
                UPDATE notebook_sessions
                SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP
                WHERE session_token = %s
            ''', (s['session_token'],))
            db.commit()

    return jsonify({'sessions': verified_sessions})


@notebook_bp.route('/sessions/stop-all', methods=['POST'])
@login_required
@async_route
async def stop_all_sessions():
    """Stop all active sessions for the user"""
    user = get_current_user()
    db = get_db()

    sessions = db.execute('''
        SELECT session_token FROM notebook_sessions
        WHERE user_id = %s
        AND kernel_status IN ('idle', 'busy', 'starting')
        AND ended_at IS NULL
    ''', (user['id'],)).fetchall()

    stopped = 0
    errors = []

    for s in sessions:
        try:
            await kernel_manager.stop_kernel(s['session_token'])
            stopped += 1
        except Exception as e:
            errors.append(str(e))

    # Mark all as disconnected
    db.execute('''
        UPDATE notebook_sessions
        SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP
        WHERE user_id = %s
        AND kernel_status IN ('idle', 'busy', 'starting')
        AND ended_at IS NULL
    ''', (user['id'],))
    db.commit()

    return jsonify({
        'stopped': stopped,
        'total': len(sessions),
        'errors': errors if errors else None
    })


# ============================================================================
# IMPORT/EXPORT
# ============================================================================

@notebook_bp.route('/import', methods=['POST'])
@login_required
def import_notebook():
    """Import a .ipynb file"""
    user = get_current_user()

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename or not file.filename.endswith('.ipynb'):
        return jsonify({'error': 'Invalid file type. Must be .ipynb'}), 400

    project_id = request.form.get('project_id', type=int)
    if not project_id:
        return jsonify({'error': 'project_id is required'}), 400

    try:
        db = get_db()
        role = _resolve_project_role(db, user['id'], project_id)
        if not role:
            return jsonify({'error': 'No access to project'}), 403
        if role not in ('Project Manager', 'Project Contributor'):
            return jsonify({'error': 'No write access to project'}), 403

        content = json.load(file)

        # Extract cells from ipynb format
        cells = []
        for cell in content.get('cells', []):
            source = cell.get('source', [])
            if isinstance(source, list):
                source = ''.join(source)

            cells.append({
                'id': f"cell-{len(cells)}-{datetime.utcnow().timestamp()}",
                'type': 'markdown' if cell.get('cell_type') == 'markdown' else 'code',
                'source': source,
                'outputs': cell.get('outputs', []),
                'execution_count': cell.get('execution_count'),
                'metadata': cell.get('metadata', {})
            })

        # Get name from metadata or filename
        metadata = _normalize_notebook_metadata(content.get('metadata', {}))
        phonos_meta = metadata.get('phonos', {})
        name = _normalized_notebook_name(phonos_meta.get('name') or secure_filename(file.filename).rsplit('.', 1)[0])

        # Extract kernel info
        kernel_info = metadata.get('kernelspec', {})
        kernel_type = kernel_info.get('name', 'python3')
        metadata['kernel'] = kernel_type
        metadata['description'] = _normalized_description(
            phonos_meta.get('description', metadata.get('description', ''))
        )

        # Create notebook (prefer user_id-compatible schema, then fallback)
        cells_payload = json.dumps(_normalize_cells(cells))
        metadata_payload = json.dumps(metadata)
        try:
            cursor = db.execute('''
                INSERT INTO notebooks (project_id, user_id, creator_id, name, cells, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (project_id, user['id'], user['id'], name, cells_payload, metadata_payload))
        except Exception as insert_error:
            if 'user_id' not in str(insert_error).lower():
                raise
            cursor = db.execute('''
                INSERT INTO notebooks (creator_id, project_id, name, kernel_type, cells, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (user['id'], project_id, name, kernel_type, cells_payload, metadata_payload))

        notebook_id = cursor.fetchone()['id']
        db.commit()

        return jsonify({
            'id': notebook_id,
            'name': name,
            'cell_count': len(cells)
        })

    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid notebook format'}), 400
    except Exception as e:
        current_app.logger.error(f"Import error: {e}")
        return jsonify({'error': f'Import failed: {str(e)}'}), 500


## Old export_notebook route removed - replaced by export_notebook_v2 with provenance support


# ============================================================================
# NOTEBOOK FILES
# ============================================================================

@notebook_bp.route('/<int:notebook_id>/files', methods=['GET'])
@login_required
def list_notebook_files(notebook_id):
    """List files/directories in a notebook workspace folder (filesystem source)."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    try:
        path = _normalize_relative_path(request.args.get('path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    try:
        workspace = _resolve_notebook_workspace(notebook)
    except OSError as e:
        current_app.logger.error(f"Error resolving notebook workspace: {e}")
        return jsonify({'error': 'Cannot access notebook workspace'}), 500

    full_path = os.path.join(workspace, path) if path else workspace
    if not _is_within_directory(workspace, full_path):
        return jsonify({'error': 'Invalid path'}), 400

    # The workspace root is created lazily on first write; an absent root means
    # the notebook simply has no files yet.
    if not os.path.exists(full_path):
        return jsonify({'files': [], 'path': path, 'workspace_name': os.path.basename(workspace)})

    if not os.path.isdir(full_path):
        return jsonify({'error': 'Not a directory'}), 400

    files = []
    try:
        for entry in os.scandir(full_path):
            if entry.name.startswith('.'):
                continue

            stat = entry.stat()
            files.append({
                'id': entry.name,
                'name': entry.name,
                'path': os.path.join(path, entry.name) if path else entry.name,
                'type': 'directory' if entry.is_dir() else 'file',
                'is_dir': entry.is_dir(),
                'size': stat.st_size if entry.is_file() else 0,
                'mime_type': mimetypes.guess_type(entry.name)[0] if entry.is_file() else None,
                'created_at': datetime.fromtimestamp(stat.st_ctime).isoformat(),
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
            })
    except Exception as e:
        current_app.logger.error(f"Error listing notebook files: {e}")
        return jsonify({'error': 'Failed to list files'}), 500

    files.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))

    return jsonify({
        'files': files,
        'path': path,
        'workspace_name': os.path.basename(workspace)
    })


@notebook_bp.route('/<int:notebook_id>/files', methods=['POST'])
@login_required
@csrf_protect
def upload_notebook_file(notebook_id):
    """Upload a file to the notebook workspace."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No filename'}), 400

    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400

    try:
        relative_dir = _normalize_relative_path(request.form.get('path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    file_path = f"{relative_dir}/{filename}" if relative_dir else filename

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)

    if size > 50 * 1024 * 1024:
        return jsonify({'error': 'File too large. Maximum 50MB'}), 400

    mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

    try:
        workspace = _resolve_notebook_workspace(notebook, create=True)
    except PermissionError:
        current_app.logger.error(f"Permission denied creating notebook workspace for {notebook_id}")
        return jsonify({'error': 'Cannot create notebook directory: Permission denied'}), 403
    except OSError as e:
        current_app.logger.error(f"OS error creating notebook directory: {e}")
        return jsonify({'error': 'Cannot create notebook directory'}), 500

    full_path = os.path.join(workspace, file_path)
    if not _is_within_directory(workspace, full_path):
        return jsonify({'error': 'Invalid path'}), 400

    if not check_disk_space(workspace, min_free_mb=50):
        return jsonify({'error': 'Not enough disk space to save file'}), 507

    target_dir = os.path.dirname(full_path)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except PermissionError:
        return jsonify({'error': 'Cannot create directory: Permission denied'}), 403

    try:
        file.save(full_path)
    except Exception as e:
        current_app.logger.error(f"Error saving file: {e}")
        return jsonify({'error': f'Failed to save file: {str(e)}'}), 500

    _upsert_notebook_file_row(notebook_id, notebook.get('project_id'), filename, file_path, size, mime_type)

    return jsonify({
        'name': filename,
        'path': file_path,
        'size': size
    })


@notebook_bp.route('/<int:notebook_id>/files/content', methods=['GET'])
@login_required
def get_notebook_file_content(notebook_id):
    """Return the content of a workspace file (text JSON, or raw download)."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    try:
        path = _normalize_relative_path(request.args.get('path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not path:
        return jsonify({'error': 'Path required'}), 400

    try:
        full_path = _notebook_file_full_path(notebook, path)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(full_path) or os.path.isdir(full_path):
        return jsonify({'error': 'File not found'}), 404

    # Raw download path — stream the file directly with a sane attachment name.
    if request.args.get('download') in ('1', 'true', 'True'):
        return send_file(
            full_path,
            as_attachment=True,
            download_name=os.path.basename(full_path)
        )

    size = os.path.getsize(full_path)
    mime_type = mimetypes.guess_type(path)[0] or 'application/octet-stream'

    # Binary / large files are reported as binary so the client falls back to
    # the download endpoint instead of embedding raw bytes in JSON.
    is_binary = _is_likely_binary(mime_type, path)
    max_preview = 256 * 1024  # 256 KB text preview
    if is_binary or size > max_preview:
        return jsonify({
            'binary': True,
            'size': size,
            'mime_type': mime_type,
            'truncated': size > 0
        })

    try:
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(max_preview + 1)
    except OSError as e:
        current_app.logger.error(f"Error reading notebook file {path}: {e}")
        return jsonify({'error': 'Failed to read file'}), 500

    truncated = len(content) > max_preview
    if truncated:
        content = content[:max_preview]

    return jsonify({
        'content': content,
        'size': size,
        'mime_type': mime_type,
        'truncated': truncated
    })


@notebook_bp.route('/<int:notebook_id>/files/directory', methods=['POST'])
@login_required
@csrf_protect
def create_notebook_directory(notebook_id):
    """Create a directory inside the notebook workspace."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    data = request.get_json(silent=True) or {}
    try:
        path = _normalize_relative_path(data.get('path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not path:
        return jsonify({'error': 'Directory path required'}), 400

    try:
        workspace = _resolve_notebook_workspace(notebook, create=True)
        full_path = os.path.join(workspace, path)
        if not _is_within_directory(workspace, full_path):
            return jsonify({'error': 'Invalid path'}), 400
    except (OSError, ValueError) as e:
        current_app.logger.error(f"Error creating notebook directory: {e}")
        return jsonify({'error': 'Cannot create directory'}), 500

    if os.path.exists(full_path):
        if os.path.isdir(full_path):
            return jsonify({'path': path, 'exists': True})
        return jsonify({'error': 'A file with that name already exists'}), 409

    try:
        os.makedirs(full_path, exist_ok=True)
    except PermissionError:
        return jsonify({'error': 'Cannot create directory: Permission denied'}), 403

    return jsonify({'path': path, 'created': True})


@notebook_bp.route('/<int:notebook_id>/files/rename', methods=['POST'])
@login_required
@csrf_protect
def rename_notebook_file(notebook_id):
    """Rename a file or directory within its current parent folder."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    data = request.get_json(silent=True) or {}
    try:
        old_path = _normalize_relative_path(data.get('path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    new_name = secure_filename(data.get('new_name', ''))
    if not old_path or not new_name:
        return jsonify({'error': 'Path and new name required'}), 400

    try:
        workspace = _resolve_notebook_workspace(notebook)
        old_full = os.path.join(workspace, old_path)
        if not _is_within_directory(workspace, old_full):
            return jsonify({'error': 'Invalid path'}), 400
    except (OSError, ValueError) as e:
        current_app.logger.error(f"Error resolving workspace for rename: {e}")
        return jsonify({'error': 'Cannot access workspace'}), 500

    if not os.path.exists(old_full):
        return jsonify({'error': 'File not found'}), 404

    new_full = os.path.join(os.path.dirname(old_full), new_name)
    if os.path.exists(new_full):
        return jsonify({'error': 'A file with that name already exists'}), 409

    try:
        os.rename(old_full, new_full)
    except OSError as e:
        current_app.logger.error(f"Error renaming {old_path}: {e}")
        return jsonify({'error': 'Failed to rename'}), 500

    new_path = os.path.join(os.path.dirname(old_path), new_name)
    _rename_notebook_file_row(notebook_id, old_path, new_path, new_name)

    return jsonify({'old_path': old_path, 'new_path': new_path, 'new_name': new_name})


@notebook_bp.route('/<int:notebook_id>/files', methods=['DELETE'])
@login_required
@csrf_protect
def delete_notebook_file(notebook_id):
    """Delete a single file or directory from the notebook workspace."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    try:
        path = _normalize_relative_path(request.args.get('path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not path:
        return jsonify({'error': 'Path required'}), 400

    try:
        full_path = _notebook_file_full_path(notebook, path)
    except (ValueError, OSError):
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(full_path):
        return jsonify({'error': 'File not found'}), 404

    try:
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)
    except OSError as e:
        current_app.logger.error(f"Error deleting {path}: {e}")
        return jsonify({'error': 'Failed to delete'}), 500

    _delete_notebook_file_rows(notebook_id, [path])

    return jsonify({'deleted': True, 'path': path})


@notebook_bp.route('/<int:notebook_id>/files/bulk-delete', methods=['POST'])
@login_required
@csrf_protect
def bulk_delete_notebook_files(notebook_id):
    """Delete multiple files/directories from the notebook workspace."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    data = request.get_json(silent=True) or {}
    paths = data.get('paths') or []
    if not isinstance(paths, list) or not paths:
        return jsonify({'error': 'No paths provided'}), 400

    try:
        workspace = _resolve_notebook_workspace(notebook)
    except OSError:
        return jsonify({'error': 'Cannot access workspace'}), 500

    deleted, failed = _bulk_apply(workspace, paths, lambda full, rel: (
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
    ))

    if deleted:
        _delete_notebook_file_rows(notebook_id, deleted)

    return jsonify({
        'deleted': deleted,
        'failed': failed,
        'deleted_count': len(deleted),
        'failed_count': len(failed)
    })


@notebook_bp.route('/<int:notebook_id>/files/bulk-move', methods=['POST'])
@login_required
@csrf_protect
def bulk_move_notebook_files(notebook_id):
    """Move multiple files/directories into a target subfolder of the workspace."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    data = request.get_json(silent=True) or {}
    paths = data.get('paths') or []
    dest_path = data.get('dest_path', '')
    if not isinstance(paths, list) or not paths:
        return jsonify({'error': 'No paths provided'}), 400

    try:
        workspace = _resolve_notebook_workspace(notebook, create=True)
        dest_norm = _normalize_relative_path(dest_path)
        dest_full = os.path.join(workspace, dest_norm) if dest_norm else workspace
        if not _is_within_directory(workspace, dest_full):
            return jsonify({'error': 'Invalid destination'}), 400
        os.makedirs(dest_full, exist_ok=True)
    except (OSError, ValueError) as e:
        current_app.logger.error(f"Error preparing move destination: {e}")
        return jsonify({'error': 'Cannot prepare destination'}), 500

    def _move(full, rel):
        # A path cannot be moved into itself or one of its subdirectories.
        target = os.path.join(dest_full, os.path.basename(rel))
        if os.path.abspath(target) == os.path.abspath(full):
            raise ValueError('Cannot move a folder into itself')
        if os.path.abspath(full) == os.path.abspath(dest_full):
            raise ValueError('Cannot move a folder into itself')
        if os.path.abspath(dest_full).startswith(os.path.abspath(full) + os.sep):
            raise ValueError('Cannot move a folder into its own subfolder')
        if os.path.exists(target):
            raise ValueError('Destination already exists')
        shutil.move(full, target)

    moved, failed = _bulk_apply(workspace, paths, _move)
    return jsonify({
        'moved': moved,
        'failed': failed,
        'dest_path': dest_norm,
        'moved_count': len(moved),
        'failed_count': len(failed)
    })


@notebook_bp.route('/<int:notebook_id>/files/bulk-copy', methods=['POST'])
@login_required
@csrf_protect
def bulk_copy_notebook_files(notebook_id):
    """Copy/duplicate multiple files/directories into a target subfolder."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    data = request.get_json(silent=True) or {}
    paths = data.get('paths') or []
    dest_path = data.get('dest_path', '')
    if not isinstance(paths, list) or not paths:
        return jsonify({'error': 'No paths provided'}), 400

    try:
        workspace = _resolve_notebook_workspace(notebook, create=True)
        dest_norm = _normalize_relative_path(dest_path)
        dest_full = os.path.join(workspace, dest_norm) if dest_norm else workspace
        if not _is_within_directory(workspace, dest_full):
            return jsonify({'error': 'Invalid destination'}), 400
        os.makedirs(dest_full, exist_ok=True)
    except (OSError, ValueError) as e:
        current_app.logger.error(f"Error preparing copy destination: {e}")
        return jsonify({'error': 'Cannot prepare destination'}), 500

    def _copy(full, rel):
        target = os.path.join(dest_full, os.path.basename(rel))
        if os.path.exists(target):
            raise ValueError('Destination already exists')
        if os.path.isdir(full):
            shutil.copytree(full, target)
        else:
            shutil.copy2(full, target)

    copied, failed = _bulk_apply(workspace, paths, _copy)
    return jsonify({
        'copied': copied,
        'failed': failed,
        'dest_path': dest_norm,
        'copied_count': len(copied),
        'failed_count': len(failed)
    })


@notebook_bp.route('/<int:notebook_id>/files/bulk-download', methods=['POST'])
@login_required
@csrf_protect
def bulk_download_notebook_files(notebook_id):
    """Stream the selected workspace files/directories as a single zip archive."""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    data = request.get_json(silent=True) or {}
    paths = data.get('paths') or []
    if not isinstance(paths, list) or not paths:
        return jsonify({'error': 'No paths provided'}), 400

    try:
        workspace = _resolve_notebook_workspace(notebook)
    except OSError:
        return jsonify({'error': 'Cannot access workspace'}), 500

    # Validate every path before zipping so we never serve files outside the
    # workspace, and cap the total size to avoid streaming gigabytes into RAM.
    total = 0
    entries = []
    for raw in paths:
        try:
            rel = _normalize_relative_path(raw)
        except ValueError:
            continue
        if not rel:
            continue
        full = os.path.join(workspace, rel)
        if not _is_within_directory(workspace, full) or not os.path.exists(full):
            continue
        if os.path.isdir(full):
            for dirpath, _dirs, fnames in os.walk(full):
                # Skip hidden files (e.g. the .nbowner ownership marker).
                for fname in fnames:
                    if fname.startswith('.'):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    total += os.path.getsize(fpath)
                    entries.append((fpath, os.path.relpath(fpath, workspace)))
        else:
            total += os.path.getsize(full)
            entries.append((full, rel))
        if total > 1024 * 1024 * 1024:  # 1 GB safety cap
            return jsonify({'error': 'Total size too large to zip (1GB max)'}), 413

    if not entries:
        return jsonify({'error': 'No valid files found to download'}), 404

    buffer = BytesIO()
    try:
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fpath, arcname in entries:
                zf.write(fpath, arcname)
    except OSError as e:
        current_app.logger.error(f"Error zipping notebook files: {e}")
        return jsonify({'error': 'Failed to build zip archive'}), 500

    buffer.seek(0)
    zip_name = f"{_slugify_notebook_name(notebook.get('name'))}_workspace.zip"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=zip_name,
        mimetype='application/zip'
    )


# ---------------------------------------------------------------------------
# Notebook workspace file helpers (DB sync + bulk apply)
# ---------------------------------------------------------------------------

def _bulk_apply(workspace, paths, op):
    """Apply ``op(full_path, rel_path)`` to each path, collecting results.

    Returns (succeeded_rel_paths, failed_list) where failed_list items are
    ``{'path': rel, 'reason': str}``. All paths are validated to stay within
    the workspace; failures never abort the whole batch.
    """
    succeeded = []
    failed = []
    seen = set()
    for raw in paths:
        try:
            rel = _normalize_relative_path(raw)
        except ValueError:
            failed.append({'path': str(raw), 'reason': 'Invalid path'})
            continue
        if not rel or rel in seen:
            continue
        seen.add(rel)

        full = os.path.join(workspace, rel)
        if not _is_within_directory(workspace, full):
            failed.append({'path': rel, 'reason': 'Outside workspace'})
            continue
        if not os.path.exists(full):
            failed.append({'path': rel, 'reason': 'Not found'})
            continue
        try:
            op(full, rel)
            succeeded.append(rel)
        except Exception as e:  # noqa: BLE001 - batch op must continue
            failed.append({'path': rel, 'reason': str(e)})

    return succeeded, failed


def _is_likely_binary(mime_type, path):
    """Heuristic: should this file be served as raw bytes instead of text JSON?"""
    if mime_type and not mime_type.startswith('text/'):
        text_exts = ('.py', '.js', '.ts', '.json', '.csv', '.tsv', '.md', '.yaml',
                     '.yml', '.xml', '.html', '.css', '.sh', '.r', '.jl', '.wl',
                     '.nb', '.txt', '.log', '.fasta', '.fa', '.pdb', '.cif', '.sdf',
                     '.mol', '.mol2', '.xyz')
        if not path.lower().endswith(text_exts):
            return True
    return False


def _upsert_notebook_file_row(notebook_id, project_id, filename, file_path, size, mime_type):
    """Best-effort sync of a workspace file into the notebook_files table."""
    try:
        db = get_db()
        db.execute('''
            INSERT INTO notebook_files (notebook_id, project_id, filename, file_path, size_bytes, mime_type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (notebook_id, file_path) DO UPDATE
            SET size_bytes = EXCLUDED.size_bytes, mime_type = EXCLUDED.mime_type,
                updated_at = CURRENT_TIMESTAMP
        ''', (notebook_id, project_id, filename, file_path, size, mime_type))
        db.commit()
    except Exception as e:  # noqa: BLE001 - DB sync is best-effort
        current_app.logger.warning(f"Could not sync notebook_files row for {file_path}: {e}")


def _delete_notebook_file_rows(notebook_id, rel_paths):
    """Best-effort removal of notebook_files rows for deleted paths (incl. children)."""
    try:
        db = get_db()
        for rel in rel_paths:
            # Match the path itself and anything beneath it (for deleted dirs).
            prefix = rel.rstrip('/') + '/%'
            db.execute(
                'DELETE FROM notebook_files WHERE notebook_id = %s '
                'AND (file_path = %s OR file_path LIKE %s)',
                (notebook_id, rel, prefix)
            )
        db.commit()
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning(f"Could not sync notebook_files deletes: {e}")


def _rename_notebook_file_row(notebook_id, old_path, new_path, new_name):
    """Best-effort rename of notebook_files rows (path + child paths)."""
    try:
        db = get_db()
        old_prefix = old_path.rstrip('/') + '/'
        new_prefix = new_path.rstrip('/') + '/'
        rows = db.execute(
            'SELECT id, file_path FROM notebook_files WHERE notebook_id = %s '
            'AND (file_path = %s OR file_path LIKE %s)',
            (notebook_id, old_path, old_prefix + '%')
        ).fetchall()
        for row in rows:
            fp = row['file_path']
            if fp == old_path:
                updated = new_path
            else:
                updated = new_prefix + fp[len(old_prefix):]
            db.execute(
                'UPDATE notebook_files SET file_path = %s, filename = %s, '
                'updated_at = CURRENT_TIMESTAMP WHERE id = %s',
                (updated, os.path.basename(updated), row['id'])
            )
        db.commit()
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning(f"Could not sync notebook_files rename: {e}")


# ============================================================================
# PROJECT-NOTEBOOK INTEGRATION
# ============================================================================

@notebook_bp.route('/project/<int:project_id>', methods=['GET'])
@login_required
def get_project_notebooks(project_id):
    """Get all notebooks for a specific project"""
    user = get_current_user()
    db = get_db()

    # Verify project access
    project_access = db.execute('''
        SELECT role FROM project_assignments
        WHERE project_id = %s AND user_id = %s
    ''', (project_id, user['id'])).fetchone()

    if not project_access:
        return jsonify({'error': 'No access to project'}), 403

    query_limit = request.args.get('limit', default=100, type=int)
    query_limit = min(max(query_limit or 100, 1), 300)

    notebooks = db.execute('''
        SELECT n.id, n.name, n.metadata,
               n.created_at, n.updated_at,
               u.username as creator_name,
               (SELECT COUNT(*) FROM notebook_sessions ns
                WHERE ns.notebook_id = n.id AND ns.kernel_status IN ('idle', 'busy')) as active_sessions
        FROM notebooks n
        LEFT JOIN users u ON n.user_id = u.id
        WHERE n.project_id = %s
        ORDER BY n.updated_at DESC
        LIMIT %s
    ''', (project_id, query_limit)).fetchall()

    notebook_list = []
    for nb in notebooks:
        # Handle SQLite JSON stored as string
        metadata = nb['metadata']
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}
        elif not isinstance(metadata, dict):
            metadata = {}

        notebook_list.append({
            'id': nb['id'],
            'name': nb['name'],
            'description': metadata.get('description', ''),
            'creator_name': nb['creator_name'],
            'kernel_type': metadata.get('kernel', 'python3'),
            'active_sessions': nb['active_sessions'],
            'created_at': nb['created_at'].isoformat() if nb['created_at'] else None,
            'updated_at': nb['updated_at'].isoformat() if nb['updated_at'] else None,
        })

    return jsonify({'notebooks': notebook_list, 'count': len(notebook_list)})


@notebook_bp.route('/project/<int:project_id>/resources', methods=['GET'])
@login_required
def get_project_resources(project_id):
    """Return notebook IDE resources for a project (notebooks + files/outputs)."""
    user = get_current_user()
    db = get_db()

    role = _resolve_project_role(db, user['id'], project_id)
    if not role:
        return jsonify({'error': 'No access to project'}), 403

    notebook_limit = request.args.get('notebook_limit', default=120, type=int)
    file_limit = request.args.get('file_limit', default=300, type=int)
    notebook_limit = min(max(notebook_limit or 120, 1), 300)
    file_limit = min(max(file_limit or 300, 1), 600)

    notebooks = db.execute('''
        SELECT n.id, n.name, n.metadata, n.updated_at,
               (SELECT COUNT(*) FROM notebook_sessions ns
                WHERE ns.notebook_id = n.id AND ns.kernel_status IN ('idle', 'busy')) AS active_sessions
        FROM notebooks n
        WHERE n.project_id = %s
          AND COALESCE(n.is_deleted, 0) = 0
        ORDER BY n.updated_at DESC
        LIMIT %s
    ''', (project_id, notebook_limit)).fetchall()

    notebook_items = []
    for nb in notebooks:
        metadata = _normalize_notebook_metadata(nb['metadata'])
        notebook_items.append({
            'id': nb['id'],
            'name': nb['name'],
            'description': _normalized_description(metadata.get('description', '')),
            'active_sessions': nb['active_sessions'],
            'updated_at': _serialize_timestamp(nb['updated_at']),
        })

    rows = db.execute('''
        SELECT
            CAST(id AS TEXT) AS id,
            name AS filename,
            path AS source_path,
            size,
            created_at,
            notes,
            'upload' AS type,
            NULL AS job_id,
            NULL AS model,
            NULL AS status,
            NULL AS metadata,
            NULL AS output_display_name
        FROM files
        WHERE project_id = %s

        UNION ALL

        SELECT
            CAST(job_id AS TEXT) AS id,
            output_file AS filename,
            output_file AS source_path,
            output_file_size AS size,
            created_at,
            'Output from model ' || model AS notes,
            'output' AS type,
            job_id,
            model,
            status,
            metadata,
            output_display_name
        FROM jobs
        WHERE project_id = %s
          AND output_file IS NOT NULL

        ORDER BY created_at DESC
        LIMIT %s
    ''', (project_id, project_id, file_limit)).fetchall()

    file_items = []
    for row in rows:
        row_dict = dict(row)
        source_name = row_dict.get('filename') or ''
        display_name = os.path.basename(source_name) if source_name else 'file'
        if row_dict['type'] == 'output' and row_dict.get('output_display_name'):
            display_name = row_dict['output_display_name']

        container_path = _build_project_container_path(
            row_dict.get('source_path'),
            fallback_name=source_name,
            job_id=row_dict.get('job_id')
        )

        file_items.append({
            'id': row_dict.get('id'),
            'name': display_name,
            'type': row_dict.get('type') or 'upload',
            'size': row_dict.get('size') or 0,
            'notes': row_dict.get('notes'),
            'job_id': row_dict.get('job_id'),
            'model': row_dict.get('model'),
            'status': row_dict.get('status'),
            'created_at': _serialize_timestamp(row_dict.get('created_at')),
            'container_path': container_path,
        })

        if row_dict['type'] != 'output':
            continue

        metadata = _normalize_notebook_metadata(row_dict.get('metadata'))
        artifacts_info = metadata.get('artifacts_info', [])
        if not isinstance(artifacts_info, list):
            continue

        output_dir = os.path.dirname(row_dict.get('source_path') or '')
        for artifact in artifacts_info:
            if not isinstance(artifact, dict):
                continue
            artifact_filename = artifact.get('filename')
            if not artifact_filename:
                continue

            artifact_relative_path = artifact.get('relative_path')
            artifact_source_path = None
            if isinstance(artifact_relative_path, str) and artifact_relative_path:
                job_root = None
                if output_dir and os.path.basename(os.path.dirname(output_dir)) == 'branches':
                    job_root = os.path.dirname(os.path.dirname(output_dir))
                elif output_dir:
                    job_root = output_dir
                if job_root:
                    artifact_source_path = os.path.join(job_root, artifact_relative_path)
            if not artifact_source_path:
                artifact_source_path = os.path.join(output_dir, artifact_filename) if output_dir else artifact_filename
            artifact_display = artifact.get('display_name') or artifact_filename
            artifact_id = f"{row_dict.get('job_id')}:{artifact_filename}"

            file_items.append({
                'id': artifact_id,
                'name': artifact_display,
                'type': 'artifact',
                'size': artifact.get('size') or 0,
                'notes': f"Artifact from {row_dict.get('model') or 'job'}",
                'job_id': row_dict.get('job_id'),
                'model': row_dict.get('model'),
                'status': row_dict.get('status'),
                'created_at': _serialize_timestamp(row_dict.get('created_at')),
                'container_path': _build_project_container_path(
                    artifact_source_path,
                    fallback_name=artifact_filename,
                    job_id=row_dict.get('job_id')
                ),
            })

    file_items.sort(key=lambda item: item.get('created_at') or '', reverse=True)

    return jsonify({
        'project_id': project_id,
        'role': role,
        'notebooks': notebook_items,
        'files': file_items,
        'counts': {
            'notebooks': len(notebook_items),
            'files': len(file_items),
        }
    })


@notebook_bp.route('/<int:notebook_id>/project', methods=['PATCH'])
@login_required
def update_notebook_project(notebook_id):
    """Link or move a notebook to a different project"""
    user = get_current_user()
    data = request.get_json() or {}
    db = get_db()

    # Check notebook ownership
    notebook = db.execute(
        'SELECT creator_id, project_id FROM notebooks WHERE id = %s AND COALESCE(is_deleted, 0) = 0',
        (notebook_id,)
    ).fetchone()

    if not notebook:
        return jsonify({'error': 'Notebook not found'}), 404

    if notebook['creator_id'] != user['id']:
        return jsonify({'error': 'Only the creator can move this notebook'}), 403

    new_project_id = data.get('project_id')

    # Validate new project access
    if new_project_id:
        project_access = db.execute('''
            SELECT role FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        ''', (new_project_id, user['id'])).fetchone()

        if not project_access or project_access['role'] not in ('Project Manager', 'Project Contributor'):
            return jsonify({'error': 'No write access to target project'}), 403

    db.execute(
        'UPDATE notebooks SET project_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
        (new_project_id, notebook_id)
    )
    db.commit()

    return jsonify({
        'success': True,
        'project_id': new_project_id
    })


@notebook_bp.route('/<int:notebook_id>/project-files', methods=['GET'])
@login_required
def get_notebook_project_files(notebook_id):
    """Get files from the notebook's linked project"""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    if not notebook['project_id']:
        return jsonify({'error': 'Notebook is not linked to a project'}), 400

    try:
        path = _normalize_relative_path(request.args.get('path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    # Get project files from file system
    project_files_base = current_app.config.get('PROJECT_FILES_DIR', '/data/projects')
    project_path = os.path.join(project_files_base, str(notebook['project_id']))

    if path:
        full_path = os.path.join(project_path, path)
    else:
        full_path = project_path

    # Security check - ensure path is within project directory
    if not _is_within_directory(project_path, full_path):
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.exists(full_path):
        return jsonify({'files': []})

    files = []
    try:
        import mimetypes
        for entry in os.scandir(full_path):
            if entry.name.startswith('.'):
                continue

            stat = entry.stat()
            files.append({
                'name': entry.name,
                'path': os.path.join(path, entry.name) if path else entry.name,
                'is_dir': entry.is_dir(),
                'size': stat.st_size if entry.is_file() else 0,
                'mime_type': mimetypes.guess_type(entry.name)[0] if entry.is_file() else None,
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
            })
    except Exception as e:
        current_app.logger.error(f"Error listing project files: {e}")
        return jsonify({'error': 'Failed to list files'}), 500

    # Sort: directories first, then alphabetically
    files.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))

    return jsonify({'files': files})


@notebook_bp.route('/<int:notebook_id>/project-files/copy', methods=['POST'])
@login_required
def copy_project_file_to_notebook(notebook_id):
    """Copy a file from the linked project to the notebook workspace"""
    user = get_current_user()
    data = request.get_json() or {}

    notebook, error = check_notebook_access(notebook_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    if not notebook['project_id']:
        return jsonify({'error': 'Notebook is not linked to a project'}), 400

    try:
        source_path = _normalize_relative_path(data.get('source_path', ''))
        dest_path = _normalize_relative_path(data.get('dest_path', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400

    if not source_path:
        return jsonify({'error': 'Source path required'}), 400

    # Construct full paths
    project_files_base = current_app.config.get('PROJECT_FILES_DIR', '/data/projects')
    project_path = os.path.join(project_files_base, str(notebook['project_id']))
    source_full = os.path.join(project_path, source_path)

    # Security check
    if not _is_within_directory(project_path, source_full):
        return jsonify({'error': 'Invalid source path'}), 400

    if not os.path.exists(source_full):
        return jsonify({'error': 'Source file not found'}), 404

    # Destination in notebook workspace
    notebook_files_base = current_app.config.get('NOTEBOOK_FILES_DIR', '/data/notebook_files')
    notebook_path = os.path.join(notebook_files_base, str(notebook_id))
    os.makedirs(notebook_path, exist_ok=True)

    filename = os.path.basename(source_path)
    if dest_path:
        dest_full = os.path.join(notebook_path, dest_path, filename)
    else:
        dest_full = os.path.join(notebook_path, filename)

    if not _is_within_directory(notebook_path, dest_full):
        return jsonify({'error': 'Invalid destination path'}), 400

    os.makedirs(os.path.dirname(dest_full), exist_ok=True)

    import shutil
    import mimetypes

    try:
        if os.path.isdir(source_full):
            shutil.copytree(source_full, dest_full, dirs_exist_ok=True)
            size = sum(os.path.getsize(os.path.join(dp, f))
                      for dp, dn, filenames in os.walk(dest_full)
                      for f in filenames)
        else:
            shutil.copy2(source_full, dest_full)
            size = os.path.getsize(dest_full)

        # Record in database
        db = get_db()
        final_path = (os.path.join(dest_path, filename) if dest_path else filename).replace('\\', '/')
        mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        db.execute('''
            INSERT INTO notebook_files (notebook_id, project_id, filename, file_path, size_bytes, mime_type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (notebook_id, file_path) DO UPDATE
            SET size_bytes = EXCLUDED.size_bytes, updated_at = CURRENT_TIMESTAMP
        ''', (notebook_id, notebook['project_id'], filename, final_path, size, mime_type))
        db.commit()

        return jsonify({
            'success': True,
            'name': filename,
            'path': final_path,
            'size': size
        })

    except Exception as e:
        current_app.logger.error(f"Error copying file: {e}")
        return jsonify({'error': f'Copy failed: {str(e)}'}), 500


@notebook_bp.route('/<int:notebook_id>/duplicate', methods=['POST'])
@login_required
def duplicate_notebook(notebook_id):
    """Create a copy of a notebook"""
    user = get_current_user()
    data = request.get_json() or {}

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    db = get_db()

    # Get full notebook
    nb = db.execute('SELECT * FROM notebooks WHERE id = %s', (notebook_id,)).fetchone()

    new_name = data.get('name', f"{nb['name']} (Copy)")
    new_project_id = data.get('project_id', nb['project_id'])

    # If new project specified, verify access
    if new_project_id and new_project_id != nb['project_id']:
        project_access = db.execute('''
            SELECT role FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        ''', (new_project_id, user['id'])).fetchone()

        if not project_access or project_access['role'] not in ('Project Manager', 'Project Contributor'):
            return jsonify({'error': 'No write access to target project'}), 403

    cursor = db.execute('''
        INSERT INTO notebooks (project_id, creator_id, name, description, kernel_type, cells, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
    ''', (new_project_id, user['id'], new_name, nb['description'],
          nb['kernel_type'], nb['cells'], nb['metadata']))

    result = cursor.fetchone()
    db.commit()

    return jsonify({
        'id': result['id'],
        'name': new_name,
        'created_at': result['created_at'].isoformat()
    }), 201


_WOLFRAM_QUANTUM_CELLS = [
    {'type': 'markdown', 'source': '# Wolfram + Quantum — a closed-loop pipeline\n\nThis notebook runs one continuous computation across **three engines**, where each stage consumes the previous stage\'s output:\n\n1. **Wolfram (theory)** — derive an exact quantity with symbolic/numeric math\n2. **Python (build)** — turn that quantity into a real quantum circuit\n3. **Quantum (run)** — execute on the Aer simulator, then real IBM hardware\n4. **Wolfram (analysis)** — the quantum results flow *back* into the same Wolfram session for statistics and plotting\n\nThe two engines share one **live Wolfram session**: `phonos.wolfram.get(name)` reads a value defined in a Wolfram cell, and `phonos.wolfram.set(name, value)` pushes Python data back so later Wolfram cells can use it.'},

    {'type': 'markdown', 'source': '## Stage 1 — Wolfram (theory)\n\nDerive a target probability from calculus, then the **exact** single-qubit rotation angle that encodes it. For a `Ry(θ)` rotation, the probability of measuring `|1⟩` is `sin²(θ/2)`, so to encode a probability `p` we need `θ = 2·arcsin(√p)`. Both `p` and `theta` stay in the shared Wolfram session.'},
    {'type': 'wolfram', 'source': '(* Target probability from a definite integral: Integrate[3 x^2, {0, 0.7}] = 0.7^3 *)\np = N[Integrate[3 x^2, {x, 0, 0.7}]];\n\n(* Exact angle that amplitude-encodes p into a single qubit *)\ntheta = 2 ArcSin[Sqrt[p]];\n\nColumn[{\n  Row[{"target  P(|1>) = ", p}],\n  Row[{"encoding theta   = ", theta, " rad"}]\n}]'},

    {'type': 'markdown', 'source': '## Stage 1b — Quantum *in* Wolfram (exact simulation)\n\nBefore anything runs on real hardware, simulate the very same `Ry(θ)` circuit **inside Wolfram** with gate matrices: the statevector and outcome probabilities are computed exactly — no sampling noise. This is the noise-free reference every later stage is measured against.'},
    {'type': 'wolfram', 'source': '(* Quantum circuit simulation in Wolfram: Ry(theta) applied to |0> *)\nry[t_] := {{Cos[t/2], -Sin[t/2]}, {Sin[t/2], Cos[t/2]}};\npsi = ry[theta] . {1, 0};      (* statevector |psi> = Ry(theta)|0> *)\nprobs = Abs[psi]^2;\nColumn[{\n  Row[{"statevector = ", NumberForm[N[psi], 4]}],\n  Row[{"P(|0>) = ", N[probs[[1]]]}],\n  Row[{"P(|1>) = ", N[probs[[2]]], "   (exactly the target p = ", p, ")"}]\n}]'},

    {'type': 'markdown', 'source': '## Stage 2 — Python (build)\n\nPull the angle Wolfram just computed and use it to parameterize a Qiskit circuit. Nothing is hardcoded — the circuit is defined entirely by the Wolfram result.'},
    {'type': 'code', 'source': '# Read the exact values straight out of the shared Wolfram session\ntheta    = phonos.wolfram.get("theta")   # float\np_target = phonos.wolfram.get("p")\nprint(f"Received from Wolfram -> theta = {theta:.6f} rad, target P(|1>) = {p_target:.4f}")\n\nfrom qiskit import QuantumCircuit\nqc = QuantumCircuit(1, 1)\nqc.ry(theta, 0)          # amplitude-encode the Wolfram probability\nqc.measure(0, 0)\nprint(qc.draw("text"))'},

    {'type': 'markdown', 'source': '## Stage 3 — Quantum (simulate)\n\nRun the Wolfram-parameterized circuit on the local Aer simulator, estimate the empirical probability, and hand the raw counts back to the Wolfram session.'},
    {'type': 'code', 'source': 'counts_sim = phonos.quantum.run(qc, shots=4096, mode="simulator")\nshots = sum(counts_sim.values())\np_sim = counts_sim.get("1", 0) / shots\nprint(f"counts    = {counts_sim}")\nprint(f"simulated P(|1>) = {p_sim:.4f}   (Wolfram target {p_target:.4f})")\n\n# Push the quantum results into the shared Wolfram session for analysis\nphonos.wolfram.set("countsSim", counts_sim)\nphonos.wolfram.set("shots", shots)'},

    {'type': 'markdown', 'source': '## Stage 4 — Wolfram (analysis)\n\nThe quantum counts produced in Python are now live in this same Wolfram session. Let Wolfram do the statistics: compare theory vs. experiment and check whether the result sits within one standard error.'},
    {'type': 'wolfram', 'source': 'pSim   = N[Lookup[countsSim, "1", 0]/shots];\nabsErr = Abs[pSim - p];\nse     = Sqrt[pSim (1 - pSim)/shots];   (* 1-sigma binomial standard error *)\nColumn[{\n  Row[{"target    P(|1>) = ", p}],\n  Row[{"simulated P(|1>) = ", pSim}],\n  Row[{"abs error        = ", absErr}],\n  Row[{"within 1 sigma?  = ", absErr <= se}]\n}]'},
    {'type': 'wolfram', 'source': 'BarChart[{p, pSim},\n  ChartLabels -> {"Wolfram theory", "Quantum sim"},\n  PlotLabel -> "Amplitude-encoded probability", PlotRange -> {0, 1},\n  ImageSize -> Medium]'},

    {'type': 'markdown', 'source': '## Stage 5 — Real IBM hardware, then a three-way comparison\n\nThe IBM credentials from your **Quantum Settings** are passed to this kernel automatically. This submits the *same* Wolfram-built circuit to a real QPU (expect a queue wait — the cell stays running while the job is queued), then charts theory vs. simulator vs. hardware — all in Wolfram.'},
    {'type': 'code', 'source': 'counts_hw = phonos.quantum.run(qc, shots=4096, mode="hardware")\np_hw = counts_hw.get("1", 0) / sum(counts_hw.values())\nprint(f"hardware P(|1>) = {p_hw:.4f}   (Wolfram target {p_target:.4f})")\n\nphonos.wolfram.set("countsHw", counts_hw)'},
    {'type': 'wolfram', 'source': 'pHw = N[Lookup[countsHw, "1", 0]/Total[Values[countsHw]]];\nBarChart[{p, pSim, pHw},\n  ChartLabels -> {"Theory", "Simulator", "Hardware"},\n  PlotLabel -> "Wolfram -> Python -> Quantum, verified back in Wolfram",\n  PlotRange -> {0, 1}, ImageSize -> Medium]'},
]


# Quantum chemistry starter (quantum runtime). Shared by GET listing and POST
# creation. Uses phonos.quantum.vqe (PySCF Hamiltonian -> Jordan-Wigner ->
# HF+UCCSD ansatz -> classical optimizer against the exact statevector
# estimator), then a dissociation curve and an optional hardware verification
# of the optimized ansatz via EstimatorV2 with error mitigation.
_VQE_CHEMISTRY_CELLS = [
    {'type': 'markdown', 'source': '# Molecular Ground State with VQE\n\nCompute a molecule\'s **electronic ground-state energy** with the Variational Quantum Eigensolver:\n\n1. **PySCF** builds the molecular Hamiltonian from the geometry\n2. **Jordan–Wigner** maps it onto qubits\n3. A **Hartree–Fock + UCCSD** ansatz is optimized classically against the exact statevector estimator\n4. For small molecules the exact (FCI) energy is computed by diagonalization, so you can see precisely how good VQE is\n\nEverything is wrapped in one call: `phonos.quantum.vqe(geometry)`.'},

    {'type': 'code', 'source': '# Hydrogen molecule at its equilibrium bond length (0.735 Angstrom, STO-3G)\nres = phonos.quantum.vqe("H 0 0 0; H 0 0 0.735")\n\nprint(f"\\nVQE  : {res[\'total_energy\']:.6f} Ha")\nprint(f"Exact: {res[\'exact_energy\']:.6f} Ha")\nprint(f"Error: {abs(res[\'total_energy\'] - res[\'exact_energy\']):.2e} Ha")'},

    {'type': 'markdown', 'source': '## Dissociation curve\n\nRepeat the calculation across bond lengths to trace the H₂ potential-energy surface — the textbook demonstration that VQE (dots) tracks the exact solution (dashed) across the whole curve, including the strongly-correlated stretched regime where Hartree–Fock fails.'},
    {'type': 'code', 'source': 'import matplotlib.pyplot as plt\n\ndistances = [0.4, 0.55, 0.735, 1.0, 1.4, 1.9, 2.5]\nvqe_E, fci_E = [], []\nfor d in distances:\n    r = phonos.quantum.vqe(f"H 0 0 0; H 0 0 {d}", maxiter=150, verbose=False)\n    vqe_E.append(r["total_energy"]); fci_E.append(r["exact_energy"])\n    print(f"d = {d:>5.3f} A   VQE = {r[\'total_energy\']:.6f} Ha   FCI = {r[\'exact_energy\']:.6f} Ha")\n\nplt.figure(figsize=(7, 4))\nplt.plot(distances, fci_E, "k--", label="Exact (FCI)")\nplt.plot(distances, vqe_E, "o-", label="VQE (UCCSD)")\nplt.xlabel("H-H bond length (\\u00c5)"); plt.ylabel("Energy (Hartree)")\nplt.title("H$_2$ dissociation curve")\nplt.legend(); plt.grid(alpha=0.3)\nplt.show()'},

    {'type': 'markdown', 'source': '## Verify on real IBM hardware\n\nEvaluate the *optimized* ansatz energy on a real QPU with `EstimatorV2` and error mitigation (readout twirling, dynamical decoupling, resilience level 1). Expect a queue wait — the cell stays running while the job is queued.\n\n> Tip: change `mode="hardware"` to `mode="noisy"` for a free rehearsal on a simulator carrying the real device\'s calibrated noise model.'},
    {'type': 'code', 'source': '# Re-derive the optimal ansatz (fast for H2), then measure its energy on hardware\nres = phonos.quantum.vqe("H 0 0 0; H 0 0 0.735", verbose=False)\nansatz_opt = res["ansatz"].assign_parameters(res["optimal_params"])\n\nE_elec_hw = phonos.quantum.expval(\n    ansatz_opt, res["hamiltonian"],\n    mode="hardware", shots=4096,\n    resilience_level=1, dynamical_decoupling=True, twirling=True,\n)\ntotal_hw = E_elec_hw + res["nuclear_repulsion"]\n\nprint(f"Hardware energy : {total_hw:.6f} Ha")\nprint(f"Statevector VQE : {res[\'total_energy\']:.6f} Ha")\nprint(f"Exact (FCI)     : {res[\'exact_energy\']:.6f} Ha")'},

    {'type': 'markdown', 'source': '## Going further\n\n- **Other molecules**: `phonos.quantum.vqe("Li 0 0 0; H 0 0 1.6")` (LiH, 12 qubits) — anything PySCF accepts.\n- **Async hardware jobs**: `job = phonos.quantum.submit(qc)` returns immediately; fetch counts later with `job.result()` or re-attach any time with `phonos.quantum.job(job_id)`. `phonos.quantum.jobs()` lists jobs submitted from this notebook.\n- **Pick a backend**: `phonos.quantum.backends()` shows live queue depths; set a preferred backend in **Quantum Settings**.'},
]


# Hybrid v2 "Chimera Lab" starter (hybrid runtime). Shared by GET listing and
# POST creation. One experiment across six engines: Wolfram derives the theory,
# Qiskit runs the quantum circuit, C++ brute-forces a Monte Carlo baseline,
# Rust bootstraps confidence intervals, R does the formal statistics and
# Python draws the final picture. Data crosses engines via the shared Wolfram
# session (phonos.wolfram), the embedded R session (phonos.r) and plain files
# in /workspace (visible to every language in the kernel container).
_HYBRID_V2_CELLS = [
    {'type': 'markdown', 'source': '# Chimera Lab — six engines, one experiment\n\nThis notebook runs a single experiment across **six runtimes**, each doing what it does best:\n\n| Stage | Engine | Role |\n|---|---|---|\n| 1 | **Wolfram** | exact theory (symbolic math) |\n| 2 | **Python + Qiskit** | the quantum experiment |\n| 3 | **C++** (g++) | brute-force Monte Carlo baseline |\n| 4 | **Rust** (rustc) | bootstrap resampling of the quantum data |\n| 5 | **R** (rpy2) | formal statistics |\n| 6 | **Python + matplotlib** | final visualization, closed back into Wolfram |\n\n**How data moves:** Wolfram values cross via `phonos.wolfram.get/set`; R globals cross via `phonos.r` / `phonos.lang.r_get`; compiled cells share plain files in `/workspace` — every language in this notebook sees the same working directory.\n\n> C/C++/Rust/R cells compile and run *inside* this kernel\'s container. Snippets without `main` are wrapped automatically, and pragmas on the first lines (`//%flags:`, `//%args:`, `//%stdin:`, `//%timeout:`) tune a cell.'},

    {'type': 'markdown', 'source': '## Stage 1 — Wolfram (theory)\n\nDerive a target probability from a definite integral, then the **exact** single-qubit rotation angle that encodes it: for `Ry(θ)`, the probability of measuring `|1⟩` is `sin²(θ/2)`, so `θ = 2·arcsin(√p)`. Both values stay live in the shared Wolfram session.'},
    {'type': 'wolfram', 'source': '(* Target probability from a definite integral: Integrate[3 x^2, {0, 0.7}] = 0.7^3 *)\np = N[Integrate[3 x^2, {x, 0, 0.7}]];\n\n(* Exact angle that amplitude-encodes p into a single qubit *)\ntheta = 2 ArcSin[Sqrt[p]];\n\nColumn[{\n  Row[{"target  P(|1>) = ", p}],\n  Row[{"encoding theta   = ", theta, " rad"}]\n}]'},

    {'type': 'markdown', 'source': '## Stage 2 — Python + Qiskit (the quantum experiment)\n\nPull the angle out of the Wolfram session, build the circuit, run it on the Aer simulator, and drop the raw results as files in `/workspace` where the compiled languages can pick them up.\n\n> Swap `mode="simulator"` for `mode="hardware"` to run on a real IBM QPU (expect a queue wait).'},
    {'type': 'code', 'source': '# Read the exact values straight out of the shared Wolfram session\ntheta    = phonos.wolfram.get("theta")\np_target = phonos.wolfram.get("p")\nprint(f"From Wolfram: theta = {theta:.6f} rad, target P(|1>) = {p_target:.4f}")\n\nfrom qiskit import QuantumCircuit\nqc = QuantumCircuit(1, 1)\nqc.ry(theta, 0)\nqc.measure(0, 0)\nprint(qc.draw("text"))\n\ncounts = phonos.quantum.run(qc, shots=8192, mode="simulator")\nshots = sum(counts.values())\np_quantum = counts.get("1", 0) / shots\nprint(f"quantum counts = {counts}   ->   P(|1>) = {p_quantum:.4f}")\n\n# Drop artifacts in /workspace — every language in this notebook sees them\nwith open("/workspace/chimera_theta.txt", "w") as f:\n    f.write(f"{theta}\\n")\nwith open("/workspace/chimera_counts.txt", "w") as f:\n    for k, v in sorted(counts.items()):\n        f.write(f"{k} {v}\\n")\nprint("wrote /workspace/chimera_theta.txt and chimera_counts.txt")'},

    {'type': 'markdown', 'source': '## Stage 3 — C++ (Monte Carlo baseline)\n\nA classical brute-force check: read `theta` from the workspace, simulate the same measurement **10 million times**, and time it. Note the cell is just a snippet — the `#include` lines are hoisted and the body is wrapped in `main()` automatically.'},
    {'type': 'cpp', 'source': '// Classical Monte Carlo baseline for the quantum experiment\n#include <fstream>\n#include <random>\n#include <chrono>\n\nstd::ifstream fin("/workspace/chimera_theta.txt");\ndouble theta = 0; fin >> theta;\nconst double p = std::sin(theta / 2) * std::sin(theta / 2);\n\nconst long long N = 10000000;\nstd::mt19937_64 rng(42);\nstd::uniform_real_distribution<double> u(0.0, 1.0);\n\nauto t0 = std::chrono::steady_clock::now();\nlong long ones = 0;\nfor (long long i = 0; i < N; ++i) if (u(rng) < p) ++ones;\nauto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count();\n\ndouble p_mc = (double)ones / N;\nstd::cout << "C++ Monte Carlo: p = " << p_mc << "   (" << N << " samples in " << ms << " ms)" << std::endl;\nstd::ofstream fout("/workspace/chimera_mc.txt");\nfout << p_mc << " " << N << std::endl;\nstd::cout << "wrote /workspace/chimera_mc.txt" << std::endl;'},

    {'type': 'markdown', 'source': '## Stage 4 — Rust (bootstrap resampling)\n\nRust reads the quantum counts and quantifies the uncertainty: Shannon entropy of the outcome distribution plus a **10,000-resample bootstrap** 95% confidence interval — 82 million RNG draws with a hand-rolled xorshift, no crates needed.'},
    {'type': 'rust', 'source': '// Bootstrap resampling of the quantum counts (hand-rolled RNG, no crates)\nuse std::fs;\n\nfn xorshift(s: &mut u64) -> u64 {\n    *s ^= *s << 13;\n    *s ^= *s >> 7;\n    *s ^= *s << 17;\n    *s\n}\n\nlet raw = fs::read_to_string("/workspace/chimera_counts.txt").expect("run the quantum cell first");\nlet (mut shots, mut ones) = (0u64, 0u64);\nfor line in raw.lines() {\n    let mut it = line.split_whitespace();\n    let bit = it.next().unwrap();\n    let n: u64 = it.next().unwrap().parse().unwrap();\n    shots += n;\n    if bit == "1" { ones += n; }\n}\nlet p_hat = ones as f64 / shots as f64;\n\n// Shannon entropy of the outcome distribution\nlet h: f64 = -[p_hat, 1.0 - p_hat].iter().filter(|q| **q > 0.0).map(|q| q * q.log2()).sum::<f64>();\n\n// Bootstrap: redraw `shots` samples 10,000 times\nlet reps = 10000usize;\nlet mut state = 0x9e3779b97f4a7c15u64;\nlet mut boots: Vec<f64> = Vec::with_capacity(reps);\nfor _ in 0..reps {\n    let mut k = 0u64;\n    for _ in 0..shots {\n        let r = (xorshift(&mut state) >> 11) as f64 / (1u64 << 53) as f64;\n        if r < p_hat { k += 1; }\n    }\n    boots.push(k as f64 / shots as f64);\n}\nboots.sort_by(|a, b| a.partial_cmp(b).unwrap());\nlet (lo, hi) = (boots[249], boots[9749]);\n\nprintln!("Rust bootstrap: p = {:.4}   95% CI [{:.4}, {:.4}]   entropy = {:.4} bits", p_hat, lo, hi, h);\nfs::write("/workspace/chimera_boot.txt", format!("{} {} {} {}\\n", p_hat, lo, hi, h)).unwrap();\nprintln!("wrote /workspace/chimera_boot.txt");'},

    {'type': 'markdown', 'source': '## Stage 5 — R (formal statistics)\n\nR runs in a **persistent embedded session** — variables survive across R cells, warnings land on stderr, plots render inline. Here R recomputes the theory from `theta` on its own, then runs an exact binomial test of the quantum data against it.'},
    {'type': 'r', 'source': '# Formal statistics on the quantum data (persistent R session via rpy2)\ntheta <- scan("/workspace/chimera_theta.txt", quiet = TRUE)\np_theory <- sin(theta / 2)^2\n\ncounts <- read.table("/workspace/chimera_counts.txt", col.names = c("bit", "n"))\nshots <- sum(counts$n)\nones  <- counts$n[counts$bit == 1]\n\ntest <- binom.test(ones, shots, p = p_theory)\npval <- test$p.value\nci   <- as.numeric(test$conf.int)\n\ncat(sprintf("theory P(1) = %.4f   observed = %.4f   exact binomial p-value = %.3f\\n",\n            p_theory, ones / shots, pval))\ncat(sprintf("95%% CI (Clopper-Pearson): [%.4f, %.4f]\\n", ci[1], ci[2]))\n\nbarplot(counts$n / shots, names.arg = counts$bit,\n        main = "Quantum outcome distribution (R)", ylab = "frequency",\n        col = c("#30404D", "#22d3ee"))\nabline(h = p_theory, lty = 2, col = "red")'},

    {'type': 'markdown', 'source': '## Stage 6 — Python (visualization + the chimera bridge)\n\nPython pulls **R\'s test results directly out of the R session** (`phonos.lang.r_get`), reads every engine\'s artifact from `/workspace`, draws the comparison — and pushes the final numbers back into the **Wolfram** session, closing the loop.'},
    {'type': 'code', 'source': '# One picture from all engines\nimport math\nimport matplotlib.pyplot as plt\n\ntheta = float(open("/workspace/chimera_theta.txt").read())\np_theory = math.sin(theta / 2) ** 2\n\ncounts = dict(line.split() for line in open("/workspace/chimera_counts.txt"))\nshots = sum(int(v) for v in counts.values())\np_quantum = int(counts.get("1", 0)) / shots\n\np_mc = float(open("/workspace/chimera_mc.txt").read().split()[0])\np_boot, lo, hi, entropy = map(float, open("/workspace/chimera_boot.txt").read().split())\n\n# Chimera bridge: pull the R test results straight into Python\npval = float(phonos.lang.r_get("pval")[0])\nci = [float(x) for x in phonos.lang.r_get("ci")]\nprint(f"R binomial test: p-value = {pval:.3f}, 95% CI = [{ci[0]:.4f}, {ci[1]:.4f}]")\n\nlabels = ["Wolfram\\ntheory", "Quantum\\n(Qiskit)", "C++\\nMonte Carlo", "Rust\\nbootstrap"]\nvalues = [p_theory, p_quantum, p_mc, p_boot]\ncolors = ["#e11d48", "#22d3ee", "#659ad2", "#ff6b35"]\n\nfig, ax = plt.subplots(figsize=(7.5, 4))\nbars = ax.bar(labels, values, color=colors, alpha=0.85)\nax.errorbar(3, p_boot, yerr=[[p_boot - lo], [hi - p_boot]], fmt="none", ecolor="black", capsize=6)\nax.axhline(p_theory, ls="--", color="black", lw=1, label="exact theory")\nax.set_ylabel("P(1)")\nax.set_title(f"One experiment, six engines   (R p-value {pval:.2f}, entropy {entropy:.3f} bits)")\nax.legend()\nfor b, v in zip(bars, values):\n    ax.text(b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.4f}", ha="center", fontsize=9)\nplt.tight_layout()\nplt.show()\n\n# Close the loop: hand every engine\'s estimate back to the Wolfram session\nphonos.wolfram.set("results", {"theory": p_theory, "quantum": p_quantum, "cpp": p_mc, "rust": p_boot})\nprint("pushed results to the Wolfram session")'},
    {'type': 'wolfram', 'source': '(* The Python results are live in this Wolfram session — final cross-check *)\nBarChart[Values[results],\n  ChartLabels -> Keys[results],\n  PlotLabel -> "Chimera cross-check (Wolfram)",\n  PlotRange -> {0, All}, ImageSize -> Medium]'},

    {'type': 'markdown', 'source': '## Going further\n\n- **Real hardware**: set `mode="hardware"` in Stage 2 — the IBM credentials from Quantum Settings flow into this kernel automatically.\n- **Cell pragmas**: `//%flags: -O3 -lpthread`, `//%args: ...`, `//%stdin: ...` (pre-fed input), `//%timeout: 120` on C/C++/Rust cells; `#%timeout:` for R.\n- **Interactive stdin**: a compiled program that blocks on `scanf`/`read_line` pops the notebook input dialog automatically; R `readline()` does too.\n- **More chimera bridges**: `phonos.r.df = pandas_df` pushes a DataFrame into R; `phonos.wolfram.set/get` crosses the Wolfram boundary; compiled cells rerun instantly thanks to a per-session binary cache.'},
]


@notebook_bp.route('/templates', methods=['GET'])
@login_required
def list_notebook_templates():
    """List available notebook templates"""
    templates = [
        {
            'id': 'blank',
            'name': 'Blank Notebook',
            'description': 'Start with an empty notebook',
            'cells': [
                {'type': 'code', 'source': '# Start coding here\n'}
            ]
        },
        {
            'id': 'code',
            'name': 'Code Notebook',
            'description': 'Start with an empty code cell',
            'cells': [
                {'type': 'code', 'source': '# Your code here\nimport phonos\n\n# List available models\nmodels = phonos.list_models()\nprint("Available models:", [m.name for m in models])'}
            ]
        },
        {
            'id': 'text',
            'name': 'Text Notebook',
            'description': 'Start with a markdown introduction',
            'cells': [
                {'type': 'markdown', 'source': '# New Section\n\nWrite your documentation here.'}
            ]
        },
        {
            'id': 'analysis',
            'name': 'Data Analysis',
            'description': 'Template for data analysis with pandas and visualization',
            'cells': [
                {'type': 'markdown', 'source': '# Data Analysis Notebook\n\nThis notebook provides a starting point for data analysis.'},
                {'type': 'code', 'source': '# Import common libraries\nimport pandas as pd\nimport numpy as np\nimport matplotlib.pyplot as plt\n\n# For interactive plots\n%matplotlib inline'},
                {'type': 'markdown', 'source': '## Load Data\n\nLoad your data file below:'},
                {'type': 'code', 'source': '# Load data\n# df = pd.read_csv("data.csv")\n# df.head()'},
                {'type': 'markdown', 'source': '## Data Exploration'},
                {'type': 'code', 'source': '# Explore data\n# df.describe()\n# df.info()'}
            ]
        },
        {
            'id': 'protein',
            'name': 'Protein Analysis',
            'description': 'Template for protein structure analysis',
            'cells': [
                {'type': 'markdown', 'source': '# Protein Analysis Notebook\n\nAnalyze protein structures and sequences.'},
                {'type': 'code', 'source': '# Import BioPython for sequence analysis\nfrom Bio import SeqIO\nfrom Bio.Seq import Seq\nfrom Bio.SeqRecord import SeqRecord\nimport numpy as np'},
                {'type': 'markdown', 'source': '## Load Structure\n\nLoad a PDB file:'},
                {'type': 'code', 'source': '# Load PDB structure\n# with open("structure.pdb", "r") as f:\n#     content = f.read()\n# print(content[:500])'},
                {'type': 'markdown', 'source': '## Sequence Analysis'},
                {'type': 'code', 'source': '# Example sequence\nseq = Seq("MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQQIA")\nprint(f"Length: {len(seq)}")\nprint(f"GC content: {(seq.count(\'G\') + seq.count(\'C\')) / len(seq) * 100:.1f}%")'}
            ]
        },
        {
            'id': 'data_analysis',
            'name': 'Data Analysis (Legacy)',
            'description': 'Template for data analysis with pandas and visualization',
            'cells': [
                {'type': 'markdown', 'source': '# Data Analysis Notebook\n\nThis notebook provides a starting point for data analysis.'},
                {'type': 'code', 'source': '# Import common libraries\nimport pandas as pd\nimport numpy as np\nimport matplotlib.pyplot as plt\n\n# For interactive plots\n%matplotlib inline'},
                {'type': 'markdown', 'source': '## Load Data\n\nLoad your data file below:'},
                {'type': 'code', 'source': '# Load data\n# df = pd.read_csv("data.csv")\n# df.head()'},
                {'type': 'markdown', 'source': '## Data Exploration'},
                {'type': 'code', 'source': '# Explore data\n# df.describe()\n# df.info()'}
            ]
        },
        {
            'id': 'protein_analysis',
            'name': 'Protein Analysis (Legacy)',
            'description': 'Template for protein structure analysis',
            'cells': [
                {'type': 'markdown', 'source': '# Protein Analysis Notebook\n\nAnalyze protein structures and sequences.'},
                {'type': 'code', 'source': '# Import BioPython for sequence analysis\nfrom Bio import SeqIO\nfrom Bio.Seq import Seq\nfrom Bio.SeqRecord import SeqRecord\nimport numpy as np'},
                {'type': 'markdown', 'source': '## Load Structure\n\nLoad a PDB file:'},
                {'type': 'code', 'source': '# Load PDB structure\n# with open("structure.pdb", "r") as f:\n#     content = f.read()\n# print(content[:500])'},
                {'type': 'markdown', 'source': '## Sequence Analysis'},
                {'type': 'code', 'source': '# Example sequence\nseq = Seq("MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQQIA")\nprint(f"Length: {len(seq)}")\nprint(f"GC content: {(seq.count(\'G\') + seq.count(\'C\')) / len(seq) * 100:.1f}%")'}
            ]
        },
        {
            'id': 'machine_learning',
            'name': 'Machine Learning',
            'description': 'Template for ML model training and evaluation',
            'cells': [
                {'type': 'markdown', 'source': '# Machine Learning Notebook\n\nBuild and train ML models.'},
                {'type': 'code', 'source': '# Import libraries\nimport numpy as np\nimport pandas as pd\nfrom sklearn.model_selection import train_test_split\nfrom sklearn.preprocessing import StandardScaler\nfrom sklearn.metrics import accuracy_score, classification_report'},
                {'type': 'markdown', 'source': '## Load Dataset'},
                {'type': 'code', 'source': '# Load and prepare data\n# X = ...\n# y = ...\n# X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)'},
                {'type': 'markdown', 'source': '## Train Model'},
                {'type': 'code', 'source': '# from sklearn.ensemble import RandomForestClassifier\n# model = RandomForestClassifier(n_estimators=100, random_state=42)\n# model.fit(X_train, y_train)'},
                {'type': 'markdown', 'source': '## Evaluate'},
                {'type': 'code', 'source': '# from sklearn.metrics import accuracy_score\n# y_pred = model.predict(X_test)\n# print(f"Accuracy: {accuracy_score(y_test, y_pred):.2f}")'}
            ]
        },
        {
            'id': 'wolfram_quantum',
            'name': 'Wolfram + Quantum',
            'description': 'Experimental hybrid runtime: Wolfram calcs, quantum simulation, then real IBM hardware — with a Python↔Wolfram data bridge',
            'kernel_type': 'hybrid',
            'cells': _WOLFRAM_QUANTUM_CELLS
        },
        {
            'id': 'hybrid_v2',
            'name': 'Hybrid v2 — Chimera Lab',
            'description': 'One experiment across six engines: Wolfram theory, a Qiskit circuit, C++ Monte Carlo, Rust bootstrap, R statistics and Python visualization sharing one workspace',
            'kernel_type': 'hybrid',
            'cells': _HYBRID_V2_CELLS
        },
        {
            'id': 'vqe_chemistry',
            'name': 'Molecular Ground State (VQE)',
            'description': 'Quantum chemistry: H2 ground-state energy and dissociation curve via VQE (PySCF + UCCSD), verified against exact FCI and real IBM hardware',
            'kernel_type': 'quantum',
            'cells': _VQE_CHEMISTRY_CELLS
        },
        {
            'id': 'quantum',
            'name': 'Quantum Hello World',
            'description': 'Bell state circuit with Qiskit (simulator + hardware)',
            'cells': [
                {'type': 'markdown', 'source': '# Quantum Hello World\n\nBuild a Bell state circuit, run it on the local AerSimulator, and learn how to switch to real IBM hardware via Quantum Settings.'},
                {'type': 'code', 'source': 'from qiskit import QuantumCircuit\nfrom qiskit_aer import AerSimulator\n\n# Create a Bell state circuit\nqc = QuantumCircuit(2, 2)\nqc.h(0)\nqc.cx(0, 1)\nqc.measure([0, 1], [0, 1])\n\n# Draw the circuit\nqc.draw("text")'},
                {'type': 'code', 'source': '# Run on the local AerSimulator\nresult = AerSimulator().run(qc, shots=1024).result()\ncounts = result.get_counts()\nprint("Counts:", counts)\n\n# Display a simple histogram\ntotal = sum(counts.values())\nfor bitstring, count in sorted(counts.items()):\n    bar = "\u2588" * int(count / total * 50)\n    print(f"{bitstring}: {bar} {count}")'},
                {'type': 'markdown', 'source': '## Run on real IBM hardware\n\nThe IBM credentials configured via **Quantum Settings** are passed to this kernel automatically.\nSimply use `QiskitRuntimeService()` — no token needed:'},
                {'type': 'code', 'source': 'from qiskit_ibm_runtime import QiskitRuntimeService\n\nservice = QiskitRuntimeService()\nprint("Backends:", [b.name for b in service.backends()])'},
                {'type': 'code', 'source': '# Select a backend and run the circuit on hardware.\n# (phonos.quantum.backend picks the least busy device, or the one from\n# your Quantum Settings — service.least_busy is unreliable on some\n# qiskit-ibm-runtime versions.)\nbackend = phonos.quantum.backend(run_mode="hardware")\nprint(f"Selected: {backend.name}")\n\n# Transpile for the backend\nfrom qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager\npm = generate_preset_pass_manager(backend=backend, optimization_level=1, translation_method="translator")\nqc_hw = pm.run(qc)\n\n# Run on hardware\nfrom qiskit_ibm_runtime import SamplerV2\nsampler = SamplerV2(mode=backend)\njob = sampler.run([qc_hw], shots=1024)\nprint(f"Job ID: {job.job_id()}")\n\n# Wait for result\nresult = job.result()\ncounts = result[0].data.meas.get_counts()\nprint("Hardware counts:", counts)'},
            ]
        },
    ]
    return jsonify({'templates': templates})


@notebook_bp.route('/templates/<template_id>', methods=['POST'])
@login_required
def create_from_template(template_id):
    """Create a new notebook from a template"""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.get_json() or {}

    logger.info(f"create_from_template: user_id={user['id']}, template_id={template_id}")

    # Template definitions
    templates = {
        'blank': {
            'name': 'Untitled Notebook',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'code', 'source': '# Import libraries\nimport numpy as np\nimport matplotlib.pyplot as plt\n\n%matplotlib inline\n\n# Your code here'}
            ]
        },
        'code': {
            'name': 'Code Notebook',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'code', 'source': '# Your code here\nimport phonos\n\n# List available models\nmodels = phonos.list_models()\nprint("Available models:", [m.name for m in models])'}
            ]
        },
        'text': {
            'name': 'Text Notebook',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'markdown', 'source': '# Text Notebook\n\nStart writing your documentation here.'},
                {'type': 'code', 'source': '# You can also add code cells\nprint("Hello, Phonos!")'}
            ]
        },
        'analysis': {
            'name': 'Data Analysis',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'markdown', 'source': '# Data Analysis Notebook\n\nThis notebook provides a starting point for data analysis.'},
                {'type': 'code', 'source': 'import pandas as pd\nimport numpy as np\nimport matplotlib.pyplot as plt\n\n%matplotlib inline'},
                {'type': 'markdown', 'source': '## Load Data'},
                {'type': 'code', 'source': '# df = pd.read_csv("data.csv")\n# df.head()'}
            ]
        },
        'protein': {
            'name': 'Protein Analysis',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'markdown', 'source': '# Protein Structure Analysis\n\nUse this notebook to predict protein structures using ESMFold and other AI models.'},
                {'type': 'code', 'source': 'import phonos\n\n# Predict protein structure with ESMFold\nsequence = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSH"\n\nprint(f"Predicting structure for sequence: {sequence[:30]}...")\njob = phonos.esmfold(sequence=sequence)\nprint(f"Job submitted: {job.job_id}")'}
            ]
        },
        'data_analysis': {
            'name': 'Data Analysis',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'markdown', 'source': '# Data Analysis Notebook\n\nThis notebook provides a starting point for data analysis.'},
                {'type': 'code', 'source': 'import pandas as pd\nimport numpy as np\nimport matplotlib.pyplot as plt\n\n%matplotlib inline'},
                {'type': 'markdown', 'source': '## Load Data'},
                {'type': 'code', 'source': '# df = pd.read_csv("data.csv")\n# df.head()'}
            ]
        },
        'protein_analysis': {
            'name': 'Protein Analysis',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'markdown', 'source': '# Protein Analysis Notebook'},
                {'type': 'code', 'source': 'from Bio import SeqIO\nfrom Bio.Seq import Seq\nimport numpy as np'},
                {'type': 'markdown', 'source': '## Load Structure'},
                {'type': 'code', 'source': '# Load your PDB file here'}
            ]
        },
        'machine_learning': {
            'name': 'Machine Learning',
            'kernel_type': 'python3',
            'cells': [
                {'type': 'markdown', 'source': '# Machine Learning Notebook'},
                {'type': 'code', 'source': 'import numpy as np\nimport pandas as pd\nfrom sklearn.model_selection import train_test_split'},
                {'type': 'markdown', 'source': '## Load Dataset'},
                {'type': 'code', 'source': '# Load your data here'}
            ]
        },
        'wolfram': {
            'name': 'Wolfram Notebook',
            'kernel_type': 'wolfram',
            'cells': [
                {'type': 'markdown', 'source': '# Wolfram Language Notebook\n\nRun symbolic and numeric computations, generate plots, and explore the Wolfram Language.'},
                {'type': 'wolfram', 'source': '(* A simple calculation *)\n2 + 2'},
                {'type': 'wolfram', 'source': '(* Plot a function *)\nPlot[Sin[x] Cos[2 x], {x, 0, 2 Pi},\n  AxesLabel -> {"x", "f(x)"},\n  ImageSize -> Medium\n]'},
                {'type': 'wolfram', 'source': '(* Solve an equation *)\nSolve[x^2 + 3 x - 4 == 0, x]'},
            ]
        },
        'wolfram_blank': {
            'name': 'Wolfram (blank)',
            'kernel_type': 'wolfram',
            'cells': [
                {'type': 'wolfram', 'source': '(* Your Wolfram Language code here *)\nPrint["Hello from Wolfram!"]'},
            ]
        },
        'wolfram_scientific': {
            'name': 'Wolfram Scientific',
            'kernel_type': 'wolfram',
            'cells': [
                {'type': 'markdown', 'source': '# Scientific Computing with Wolfram\n\nSymbolic math, calculus, linear algebra, and visualization.'},
                {'type': 'wolfram', 'source': '(* Symbolic differentiation *)\nD[Sin[x^2], x]'},
                {'type': 'wolfram', 'source': '(* Numerical integration *)\nNIntegrate[Exp[-x^2], {x, -Infinity, Infinity}]'},
                {'type': 'wolfram', 'source': '(* Linear algebra *)\nm = RandomReal[1, {3, 3}];\nInverse[m]'},
            ]
        },
        'wolfram_visualization': {
            'name': 'Wolfram Visualization',
            'kernel_type': 'wolfram',
            'cells': [
                {'type': 'markdown', 'source': '# Wolfram Visualization\n\nCreate publication-quality plots.'},
                {'type': 'wolfram', 'source': 'data = Table[{x, Sin[x] + RandomReal[{-0.1, 0.1}]}, {x, 0, 2 Pi, 0.1}];\nListPlot[data, Joined -> True, Mesh -> All]'},
                {'type': 'wolfram', 'source': 'Plot3D[Sin[x y], {x, 0, 3}, {y, 0, 3}, Mesh -> All]'},
            ]
        },
        'wolfram_quantum': {
            'name': 'Wolfram + Quantum',
            'kernel_type': 'hybrid',
            'cells': _WOLFRAM_QUANTUM_CELLS
        },
        'hybrid_v2': {
            'name': 'Hybrid v2 — Chimera Lab',
            'kernel_type': 'hybrid',
            'cells': _HYBRID_V2_CELLS
        },
        'vqe_chemistry': {
            'name': 'Molecular Ground State (VQE)',
            'kernel_type': 'quantum',
            'cells': _VQE_CHEMISTRY_CELLS
        },
        'quantum': {
            'name': 'Quantum Hello World',
            'kernel_type': 'quantum',
            'cells': [
                {'type': 'markdown', 'source': '# Quantum Hello World\n\nBuild a Bell state circuit, run it on the local AerSimulator, and learn how to switch to real IBM hardware via Quantum Settings.'},
                {'type': 'code', 'source': 'from qiskit import QuantumCircuit\nfrom qiskit_aer import AerSimulator\n\n# Create a Bell state circuit\nqc = QuantumCircuit(2, 2)\nqc.h(0)\nqc.cx(0, 1)\nqc.measure([0, 1], [0, 1])\n\n# Draw the circuit\nqc.draw("text")'},
                {'type': 'code', 'source': '# Run on the local AerSimulator\nresult = AerSimulator().run(qc, shots=1024).result()\ncounts = result.get_counts()\nprint("Counts:", counts)\n\n# Display a simple histogram\ntotal = sum(counts.values())\nfor bitstring, count in sorted(counts.items()):\n    bar = "\u2588" * int(count / total * 50)\n    print(f"{bitstring}: {bar} {count}")'},
                {'type': 'markdown', 'source': '## Run on real IBM hardware\n\nThe IBM credentials configured via **Quantum Settings** are passed to this kernel automatically.\nSimply use `QiskitRuntimeService()` — no token needed:'},
                {'type': 'code', 'source': 'from qiskit_ibm_runtime import QiskitRuntimeService\n\nservice = QiskitRuntimeService()\nprint("Backends:", [b.name for b in service.backends()])'},
                {'type': 'code', 'source': '# Select a backend and run the circuit on hardware.\n# (phonos.quantum.backend picks the least busy device, or the one from\n# your Quantum Settings — service.least_busy is unreliable on some\n# qiskit-ibm-runtime versions.)\nbackend = phonos.quantum.backend(run_mode="hardware")\nprint(f"Selected: {backend.name}")\n\n# Transpile for the backend\nfrom qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager\npm = generate_preset_pass_manager(backend=backend, optimization_level=1, translation_method="translator")\nqc_hw = pm.run(qc)\n\n# Run on hardware\nfrom qiskit_ibm_runtime import SamplerV2\nsampler = SamplerV2(mode=backend)\njob = sampler.run([qc_hw], shots=1024)\nprint(f"Job ID: {job.job_id()}")\n\n# Wait for result\nresult = job.result()\ncounts = result[0].data.meas.get_counts()\nprint("Hardware counts:", counts)'},
            ]
        },
    }

    if template_id not in templates:
        logger.warning(f"create_from_template: template not found: {template_id}")
        return jsonify({'error': 'Template not found'}), 404

    template = templates[template_id]
    name = data.get('name', template['name'])
    project_id = data.get('project_id')
    # Allow client to override the kernel_type (e.g. create a blank notebook with
    # a Wolfram runtime instead of the default Python).
    kernel_type = data.get('kernel_type') or template.get('kernel_type', 'python3')

    logger.info(f"create_from_template: name={name}, project_id={project_id}")

    if not project_id:
        logger.warning(f"create_from_template: missing project_id for user {user['id']}")
        return jsonify({'error': 'project_id is required'}), 400

    try:
        db = get_db()

        # Verify project access
        project_access = db.execute('''
            SELECT role FROM project_assignments
            WHERE project_id = %s AND user_id = %s
        ''', (project_id, user['id'])).fetchone()

        if not project_access or project_access['role'] not in ('Project Manager', 'Project Contributor'):
            logger.warning(f"create_from_template: no write access for user {user['id']} in project {project_id}")
            return jsonify({'error': 'No write access to project'}), 403

        # Generate cell IDs
        import time
        cells = []
        for i, cell in enumerate(template['cells']):
            cells.append({
                'id': f"cell-{i}-{int(time.time() * 1000)}",
                'type': cell['type'],
                'source': cell['source'],
                'outputs': [],
                'execution_count': None
            })

        cursor = db.execute('''
            INSERT INTO notebooks (project_id, user_id, creator_id, name, kernel_type, cells, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
        ''', (project_id, user['id'], user['id'], name, kernel_type, json.dumps(cells), json.dumps({'template': template_id, 'kernel_type': kernel_type})))

        result = cursor.fetchone()
        db.commit()
        logger.info(f"create_from_template: successfully created notebook {result['id']} from template {template_id} (kernel={kernel_type})")

        return jsonify({
            'id': result['id'],
            'name': name,
            'kernel_type': kernel_type,
            'created_at': result['created_at'].isoformat()
        }), 201
    except Exception as e:
        logger.error(f"create_from_template: failed to create notebook from template {template_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ============================================================================
# NOTEBOOK MODEL CATALOG
# ============================================================================

@notebook_bp.route('/models', methods=['GET'])
@login_required
def get_notebook_models():
    """Return notebook-facing platform/custom model catalog."""
    user = get_current_user()
    db = get_db()
    project_id = request.args.get('project_id', type=int)

    if project_id:
        role = _resolve_project_role(db, user['id'], project_id)
        if not role:
            return jsonify({'error': 'No access to project'}), 403

    try:
        from config import Config
        from production.model_overrides import apply_overrides_to_models, fetch_model_overrides
        from phonos_app.helpers.user_models import get_user_models
        from phonos_app.helpers.worker_runtime import apply_runtime_status_to_models

        overrides = fetch_model_overrides(db)
        models = apply_overrides_to_models(Config.SUPPORTED_MODELS, overrides)
        hidden_model_ids = set(getattr(Config, 'UI_HIDDEN_MODEL_IDS', set()))
        greyed_model_ids = set(getattr(Config, 'UI_GREYED_MODEL_IDS', set()))

        platform_models = []
        for model in models:
            if not model.get('is_visible', True):
                continue
            if model.get('id') in hidden_model_ids:
                continue

            normalized = dict(model)
            if normalized.get('id') in greyed_model_ids:
                normalized['available'] = False
                normalized['is_active'] = False
                normalized['greyed_out'] = True
            platform_models.append(normalized)

        platform_models = apply_runtime_status_to_models(platform_models)

        def _platform_sort_key(model_item):
            node_id = model_item.get('node_id', '') or ''
            try:
                numeric = int(str(node_id).split('-')[-1])
            except (TypeError, ValueError):
                numeric = 99
            return numeric, model_item.get('name', '') or ''

        platform_models.sort(key=_platform_sort_key)

        try:
            custom_models_raw = get_user_models(
                user['id'],
                project_id=project_id,
                include_public=False
            )
        except Exception as custom_model_error:
            err_text = str(custom_model_error).lower()
            if 'no such table' in err_text and 'user_models' in err_text:
                logger.warning("user_models table missing; returning empty custom model list")
                custom_models_raw = []
            else:
                raise

        custom_models = []
        for model in custom_models_raw:
            if model.get('is_active') is False:
                continue
            custom_models.append({
                'id': model.get('id'),
                'model_id': model.get('model_id'),
                'model_name': model.get('model_name'),
                'model_type': model.get('model_type'),
                'project_id': model.get('project_id'),
                'credit_cost': model.get('credit_cost', 10),
                'encryption_status': model.get('encryption_status'),
                'output_format': model.get('output_format'),
            })

        return jsonify({
            'platform_models': platform_models,
            'custom_models': custom_models,
            'counts': {
                'platform': len(platform_models),
                'custom': len(custom_models)
            }
        })
    except Exception as e:
        logger.error(f"Error fetching notebook models: {e}", exc_info=True)
        return jsonify({'error': 'Failed to fetch notebook models'}), 500


# ============================================================================
# RUNTIME LEVELS API
# ============================================================================

@notebook_bp.route('/runtimes', methods=['GET'])
@login_required
def list_runtime_levels():
    """List available runtime levels (public info only, no encrypted config)"""
    db = get_db()
    try:
        from phonos_app.helpers.runtime_config import get_runtime_config
        rc = get_runtime_config(db)
        levels = rc.get_levels()

        return jsonify({
            'runtimes': [{
                'name': l['name'],
                'display_name': l['display_name'],
                'backend_type': l['backend_type'],
                'mem_limit': l['mem_limit'],
                'cpu_quota': l['cpu_quota'],
                'gpu_enabled': l['gpu_enabled'],
                'max_sessions': l['max_sessions'],
                'sort_order': l['sort_order'],
            } for l in levels]
        })
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.error(f"Error listing runtimes: {e}")
        return jsonify({'runtimes': [
            {'name': 'basic', 'display_name': 'Basic', 'backend_type': 'local_docker',
             'mem_limit': '2g', 'cpu_quota': 1.0, 'gpu_enabled': False,
             'max_sessions': 5, 'sort_order': 0}
        ]})


@notebook_bp.route('/runtimes', methods=['POST'])
@login_required
def create_runtime_level():
    """Create a new runtime level (admin only)"""
    user = get_current_user()
    db = get_db()

    # Check admin role
    user_row = db.execute('SELECT role FROM users WHERE id = %s', (user['id'],)).fetchone()
    if not user_row or user_row.get('role') not in ('admin', 'superadmin'):
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json() or {}
    required = ['name', 'display_name', 'backend_type']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400

    try:
        from phonos_app.helpers.runtime_config import get_runtime_config
        rc = get_runtime_config()
        level = rc.create_level(
            name=data['name'],
            display_name=data['display_name'],
            backend_type=data['backend_type'],
            config=data.get('config'),
            mem_limit=data.get('mem_limit', '2g'),
            cpu_quota=data.get('cpu_quota', 1.0),
            gpu_enabled=data.get('gpu_enabled', False),
            max_sessions=data.get('max_sessions', 5),
            sort_order=data.get('sort_order', 0),
        )
        return jsonify(level), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error creating runtime: {e}")
        return jsonify({'error': str(e)}), 500


@notebook_bp.route('/runtimes/<name>', methods=['PUT'])
@login_required
def update_runtime_level(name):
    """Update a runtime level (admin only)"""
    user = get_current_user()
    db = get_db()

    user_row = db.execute('SELECT role FROM users WHERE id = %s', (user['id'],)).fetchone()
    if not user_row or user_row.get('role') not in ('admin', 'superadmin'):
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json() or {}

    try:
        from phonos_app.helpers.runtime_config import get_runtime_config
        rc = get_runtime_config()
        level = rc.update_level(name, **data)
        if not level:
            return jsonify({'error': 'Runtime level not found'}), 404
        return jsonify(level)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error updating runtime: {e}")
        return jsonify({'error': str(e)}), 500


@notebook_bp.route('/runtimes/<name>', methods=['DELETE'])
@login_required
def delete_runtime_level(name):
    """Deactivate a runtime level (admin only)"""
    user = get_current_user()
    db = get_db()

    user_row = db.execute('SELECT role FROM users WHERE id = %s', (user['id'],)).fetchone()
    if not user_row or user_row.get('role') not in ('admin', 'superadmin'):
        return jsonify({'error': 'Admin access required'}), 403

    try:
        from phonos_app.helpers.runtime_config import get_runtime_config
        rc = get_runtime_config()
        deleted = rc.delete_level(name)
        if not deleted:
            return jsonify({'error': 'Runtime level not found or already inactive'}), 404
        return jsonify({'success': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error deleting runtime: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# USER PREFERENCES API
# ============================================================================

@notebook_bp.route('/preferences', methods=['GET'])
@login_required
def get_user_preferences():
    """Get current user's notebook preferences"""
    user = get_current_user()
    db = get_db()

    row = db.execute(
        'SELECT preferences FROM user_preferences WHERE user_id = %s',
        (user['id'],)
    ).fetchone()

    prefs = row['preferences'] if row else {}
    if isinstance(prefs, str):
        try:
            prefs = json.loads(prefs)
        except (ValueError, TypeError):
            prefs = {}

    return jsonify({'preferences': prefs})


@notebook_bp.route('/preferences', methods=['PUT'])
@login_required
def update_user_preferences():
    """Update current user's notebook preferences (UPSERT)"""
    user = get_current_user()
    data = request.get_json() or {}
    db = get_db()

    preferences = data.get('preferences', data)

    db.execute('''
        INSERT INTO user_preferences (user_id, preferences)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE
        SET preferences = EXCLUDED.preferences
    ''', (user['id'], json.dumps(preferences)))
    db.commit()

    return jsonify({'success': True, 'preferences': preferences})


# ============================================================================
# EXECUTION HISTORY API
# ============================================================================

@notebook_bp.route('/<int:notebook_id>/executions', methods=['GET'])
@login_required
def get_execution_history(notebook_id):
    """Get execution history for a notebook with timing data"""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    db = get_db()
    limit = request.args.get('limit', 50, type=int)
    limit = min(limit, 200)

    executions = db.execute('''
        SELECT ne.id, ne.cell_id, ne.execution_count, ne.source,
               ne.started_at, ne.finished_at, ne.status, ne.duration_ms,
               ne.tags, ne.annotation
        FROM notebook_executions ne
        JOIN notebook_sessions ns ON ne.session_id = ns.id
        WHERE ns.notebook_id = %s
        ORDER BY ne.started_at DESC
        LIMIT %s
    ''', (notebook_id, limit)).fetchall()

    result = []
    for ex in executions:
        tags = ex['tags']
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (ValueError, TypeError):
                tags = []

        result.append({
            'id': ex['id'],
            'cell_id': ex['cell_id'],
            'execution_count': ex['execution_count'],
            'source': ex['source'][:200],
            'started_at': ex['started_at'].isoformat() if ex['started_at'] else None,
            'finished_at': ex['finished_at'].isoformat() if ex['finished_at'] else None,
            'status': ex['status'],
            'duration_ms': ex['duration_ms'],
            'tags': tags,
            'annotation': ex['annotation'],
        })

    return jsonify({'executions': result})


# ============================================================================
# EXPORT WITH PROVENANCE
# ============================================================================

@notebook_bp.route('/<int:notebook_id>/export', methods=['GET'])
@login_required
def export_notebook_v2(notebook_id):
    """Export notebook as .ipynb file, optionally with provenance metadata"""
    user = get_current_user()

    notebook, error = check_notebook_access(notebook_id, user['id'])
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    db = get_db()
    nb = db.execute('SELECT * FROM notebooks WHERE id = %s', (notebook_id,)).fetchone()

    cells = json.loads(nb['cells']) if nb['cells'] else []
    metadata = json.loads(nb['metadata']) if nb['metadata'] else {}

    # Build ipynb format
    ipynb = {
        'nbformat': 4,
        'nbformat_minor': 5,
        'metadata': {
            'kernelspec': {
                'display_name': 'Python 3',
                'language': 'python',
                'name': nb.get('kernel_type') or 'python3'
            },
            'language_info': {
                'name': 'python',
                'version': '3.11.0'
            },
            'phonos': {
                'notebook_id': notebook_id,
                'name': nb['name'],
                'exported_at': datetime.utcnow().isoformat()
            },
            **metadata
        },
        'cells': []
    }

    for cell in cells:
        source = cell.get('source', '')
        source_lines = source.split('\n')
        formatted_source = [line + '\n' for line in source_lines[:-1]]
        if source_lines:
            formatted_source.append(source_lines[-1])

        ipynb_cell = {
            'cell_type': cell.get('type', 'code'),
            'metadata': cell.get('metadata', {}),
            'source': formatted_source
        }

        if cell.get('type') == 'code':
            ipynb_cell['execution_count'] = cell.get('execution_count')
            ipynb_cell['outputs'] = cell.get('outputs', [])

        ipynb['cells'].append(ipynb_cell)

    # Add provenance if requested
    include_provenance = request.args.get('provenance', '').lower() in ('true', '1', 'yes')
    if include_provenance:
        executions = db.execute('''
            SELECT ne.cell_id, ne.execution_count, ne.source,
                   ne.started_at, ne.finished_at, ne.status,
                   ne.duration_ms, ne.annotation
            FROM notebook_executions ne
            JOIN notebook_sessions ns ON ne.session_id = ns.id
            WHERE ns.notebook_id = %s
            ORDER BY ne.started_at
        ''', (notebook_id,)).fetchall()

        provenance_records = []
        for ex in executions:
            provenance_records.append({
                'cell_id': ex['cell_id'],
                'execution_count': ex['execution_count'],
                'started_at': ex['started_at'].isoformat() if ex['started_at'] else None,
                'finished_at': ex['finished_at'].isoformat() if ex['finished_at'] else None,
                'status': ex['status'],
                'duration_ms': ex['duration_ms'],
                'annotation': ex['annotation'],
            })

        ipynb['metadata']['phonos']['provenance'] = {
            'total_executions': len(provenance_records),
            'executions': provenance_records
        }

    return jsonify(ipynb)


# ============================================================================
# WOLFRAM USER CREDENTIALS
# ============================================================================
#
# Admin users use the container's global WOLFRAM_USERNAME / WOLFRAM_PASSWORD
# environment variables. Non-admin users register their own Wolfram Cloud
# account here; the credentials are AES-256-GCM encrypted at rest and are
# only used to forward per-request auth headers to the wolfram-svc container.

def _is_admin(user: dict) -> bool:
    if user.get('is_admin'):
        return True
    try:
        db = get_db()
        row = db.execute(
            "SELECT is_admin FROM users WHERE id = %s", (user['id'],)
        ).fetchone()
        return bool(row and row['is_admin'])
    except Exception:
        return False


@notebook_bp.route('/wolfram/credentials', methods=['GET'])
@login_required
def get_wolfram_credentials():
    """Return metadata about the current user's Wolfram credentials.
    Admins get info about the global container account instead of user creds."""
    user = get_current_user()
    admin = _is_admin(user)
    from phonos_app.helpers.wolfram_user_creds import get_credentials_metadata
    if admin:
        return jsonify({
            'configured': True,
            'is_admin': True,
            'source': 'container_env',
            'wolfram_id_email': None,
            'updated_at': None,
        })
    meta = get_credentials_metadata(user['id'])
    meta['is_admin'] = False
    meta['source'] = 'user_saved'
    return jsonify(meta)


@notebook_bp.route('/wolfram/credentials', methods=['PUT'])
@login_required
def put_wolfram_credentials():
    """Save (or replace) the current user's Wolfram Cloud credentials.

    Admin users cannot save personal credentials; they always use the container
    account. Body: { username, password }."""
    user = get_current_user()
    if _is_admin(user):
        return jsonify({
            'error': 'Admin users use the global Wolfram account configured in the '
                     'container environment. Personal credentials are not allowed.'
        }), 403

    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'error': 'username and password are required'}), 400
    import re
    if not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', username):
        return jsonify({'error': 'Wolfram ID must be a valid email address'}), 400

    from phonos_app.helpers.wolfram_user_creds import save_credentials
    try:
        save_credentials(user['id'], username, password, email=username)
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.exception("Failed to save Wolfram credentials")
        return jsonify({'error': f'Failed to save credentials: {e}'}), 500

    return jsonify({'success': True})


@notebook_bp.route('/wolfram/credentials', methods=['DELETE'])
@login_required
def delete_wolfram_credentials():
    """Remove the current user's Wolfram credentials."""
    user = get_current_user()
    if _is_admin(user):
        return jsonify({'error': 'Admins have no personal credentials to delete'}), 400
    from phonos_app.helpers.wolfram_user_creds import delete_credentials
    deleted = delete_credentials(user['id'])
    return jsonify({'success': True, 'deleted': deleted})


@notebook_bp.route('/wolfram/credentials/verify', methods=['POST'])
@login_required
def verify_wolfram_credentials():
    """Probe the Wolfram service with the user's credentials to confirm they work."""
    user = get_current_user()
    admin = _is_admin(user)
    if admin:
        return jsonify({'success': True, 'source': 'container_env', 'note': 'admin uses container credentials'})

    from phonos_app.helpers.wolfram_user_creds import get_credentials, record_verification
    creds = get_credentials(user['id'])
    if not creds:
        return jsonify({'success': False, 'error': 'No credentials saved'}), 400

    username, password = creds
    service_url = os.environ.get('WOLFRAM_SERVICE_URL', 'http://localhost:5100')
    import base64
    import json as _json
    from urllib import request as _urlrequest, error as _urlerror

    auth_header = 'Basic ' + base64.b64encode(f"{username}:{password}".encode()).decode()
    payload = _json.dumps({'code': '$ActivationKey', 'timeout': 20}).encode()
    req = _urlrequest.Request(
        f"{service_url.rstrip('/')}/evaluate",
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'X-Wolfram-Auth': auth_header,
        },
        method='POST',
    )
    try:
        with _urlrequest.urlopen(req, timeout=30) as r:
            data = _json.loads(r.read())
        ok = bool(data.get('success'))
        record_verification(user['id'], ok=ok, error=None if ok else data.get('error'), email=username)
        return jsonify({'success': ok, 'result': data.get('text') if ok else None, 'error': data.get('error')})
    except _urlerror.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='replace')
        record_verification(user['id'], ok=False, error=f"HTTP {e.code}: {err_body[:200]}", email=username)
        return jsonify({'success': False, 'error': f"HTTP {e.code}"}), 502
    except Exception as e:
        record_verification(user['id'], ok=False, error=str(e))
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# WOLFRAM EVALUATION ENDPOINT (file-based .nb execution, no notebook context)
# ============================================================================

@notebook_bp.route('/wolfram/evaluate', methods=['POST'])
@login_required
def wolfram_evaluate():
    """Evaluate a single snippet of Wolfram Language code via the Wolfram service.

    Used by the file preview UI to execute cells parsed from a `.nb` file
    uploaded to a project. The user must have saved Wolfram Cloud credentials
    (non-admins) or the service must be configured with admin env credentials.

    Body: { code: str, timeout?: int, image_size?: int }
    """
    from phonos_app.helpers.wolfram_service import call_wolfram_service

    user = get_current_user()
    data = request.get_json(silent=True) or {}
    code = data.get('code')
    if not isinstance(code, str) or not code.strip():
        return jsonify({'success': False, 'error': 'No code provided'}), 400
    try:
        timeout = int(data.get('timeout', 60))
    except (TypeError, ValueError):
        timeout = 60
    try:
        image_size = data.get('image_size')
        image_size = int(image_size) if image_size is not None else None
    except (TypeError, ValueError):
        image_size = None

    db = get_db()
    try:
        result = call_wolfram_service(
            code=code,
            user_id=user['id'],
            db=db,
            timeout=timeout,
            image_size=image_size,
        )
        try:
            user_row = db.execute(
                "SELECT is_admin FROM users WHERE id = %s", (user['id'],)
            ).fetchone()
            is_admin = bool(user_row and user_row['is_admin'])
            from phonos_app.helpers.wolfram_user_creds import record_verification
            record_verification(
                user['id'],
                ok=bool(result.get('success')),
                error=None if result.get('success') else result.get('error'),
            )
        except Exception:
            pass
        return jsonify(result)
    except PermissionError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'missing_credentials'}), 403
    except RuntimeError as e:
        code_attr = getattr(e, 'error_code', 'unknown')
        status = 401 if code_attr == 'auth_failed' else 502
        logger.warning(f"Wolfram evaluate failed for user {user['id']} (admin={is_admin if 'is_admin' in locals() else '?'}): {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'error_code': code_attr,
            'detail': getattr(e, 'detail', None),
        }), status
    except Exception as e:
        logger.exception("Unexpected error in wolfram_evaluate")
        return jsonify({'success': False, 'error': str(e), 'error_code': 'internal'}), 500


@notebook_bp.route('/wolfram/bridge', methods=['POST'])
@login_required
def wolfram_bridge():
    """Python↔Wolfram bridge for the hybrid (experimental) runtime.

    Called by the in-kernel ``phonos.wolfram`` SDK helper, authenticated with
    the kernel's ``X-Notebook-Session`` token. Evaluations are keyed by that
    same session token on the Wolfram service, so Python bridge calls and
    Wolfram cells in the same notebook session SHARE kernel state: a variable
    set from a Wolfram cell is visible to ``phonos.wolfram.eval(...)`` and
    vice versa.

    Body: { code: str, timeout?: int, image_size?: int }
    """
    from phonos_app.helpers.wolfram_service import call_wolfram_service

    user = get_current_user()
    session_key = request.headers.get('X-Notebook-Session')
    if not session_key:
        return jsonify({
            'success': False,
            'error': 'The Wolfram bridge is only available from inside a notebook kernel '
                     '(missing X-Notebook-Session).',
            'error_code': 'no_notebook_session',
        }), 400

    data = request.get_json(silent=True) or {}
    code = data.get('code')
    if not isinstance(code, str) or not code.strip():
        return jsonify({'success': False, 'error': 'No code provided'}), 400
    try:
        timeout = max(1, min(600, int(data.get('timeout', 60))))
    except (TypeError, ValueError):
        timeout = 60
    try:
        image_size = data.get('image_size')
        image_size = int(image_size) if image_size is not None else None
    except (TypeError, ValueError):
        image_size = None
    persist = bool(data.get('persist', True))

    db = get_db()
    try:
        result = call_wolfram_service(
            code=code,
            user_id=user['id'],
            db=db,
            timeout=timeout,
            image_size=image_size,
            session_key=session_key,
            persist=persist,
        )
        return jsonify(result)
    except PermissionError as e:
        return jsonify({'success': False, 'error': str(e), 'error_code': 'missing_credentials'}), 403
    except RuntimeError as e:
        code_attr = getattr(e, 'error_code', 'unknown')
        status = 401 if code_attr == 'auth_failed' else 502
        logger.warning(f"Wolfram bridge failed for user {user['id']}: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'error_code': code_attr,
        }), status
    except Exception as e:
        logger.exception("Unexpected error in wolfram_bridge")
        return jsonify({'success': False, 'error': str(e), 'error_code': 'internal'}), 500


# ============================================================================
# TEMP: client-side debug log relay (education "Add Problem" investigation).
# Lets us read frontend console.log output from the server log instead of
# needing the user to relay it manually. Remove once the bug is found.
# ============================================================================

@notebook_bp.route('/<int:notebook_id>/source-status', methods=['GET'])
@login_required
def get_notebook_source_status(notebook_id):
    """Return whether a notebook is mirrored to an external .nb source file."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401
    db = get_db()
    nb = db.execute('SELECT id, user_id, project_id FROM notebooks WHERE id = %s', (notebook_id,)).fetchone()
    if not nb:
        return jsonify({'linked': False, 'exists': False})
    return jsonify({'linked': False, 'exists': False})


@notebook_bp.route('/<int:notebook_id>/sync-to-source', methods=['POST'])
@login_required
def sync_notebook_to_source(notebook_id):
    """Best-effort no-op sync when no external .nb mirror is attached."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401
    return jsonify({'synced': False, 'linked': False})


@notebook_bp.route('/debug_log', methods=['POST'])
@login_required
def client_debug_log():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    logger.info(f"[ClientDebug] user={user['id']} tag={data.get('tag')} data={data.get('data')}")
    return jsonify({'status': 'ok'})


# BLUEPRINT REGISTRATION
# ============================================================================

def register_notebook_routes(app):
    """Register the notebook blueprint with the Flask app"""
    app.register_blueprint(notebook_bp)
    app.logger.info("Notebook API routes registered successfully")
