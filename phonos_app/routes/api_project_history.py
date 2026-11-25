"""
Project History API Routes - Activity log and file restore for projects.
Provides endpoints for viewing project history and restoring files within 24h window.
"""
from __future__ import annotations

import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, session

from phonos_app.helpers.auth import login_required
from phonos_app.helpers.database import get_db

logger = logging.getLogger(__name__)

project_history_bp = Blueprint('project_history', __name__, url_prefix='/api/projects')


def _get_user():
    """Get current user from session."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    return {'id': user_id}


def _check_project_access(project_id: int, user_id: int, require_write: bool = False):
    """Check if user has access to project. Returns (project, error)."""
    db = get_db()

    project = db.execute(
        'SELECT id, name FROM projects WHERE id = %s AND is_archived = 0',
        (project_id,)
    ).fetchone()

    if not project:
        return None, 'Project not found'

    assignment = db.execute(
        'SELECT role FROM project_assignments WHERE project_id = %s AND user_id = %s',
        (project_id, user_id)
    ).fetchone()

    if assignment:
        role = (assignment['role'] or '').lower()
        viewer_roles = {'project contributor', 'project manager', 'project viewer', 'owner', 'admin'}
        writer_roles = {'project contributor', 'project manager', 'owner', 'admin'}
        if require_write:
            if role in writer_roles:
                return dict(project), None
        else:
            if role in viewer_roles:
                return dict(project), None

    team_check = db.execute(
        '''
        SELECT tm.role FROM team_members tm
        JOIN projects p ON (p.team_id = tm.team_id OR p.organization_id = tm.team_id)
        WHERE p.id = %s AND tm.user_id = %s
        ''',
        (project_id, user_id)
    ).fetchone()

    if team_check:
        role = (team_check['role'] or '').lower()
        if role in ('owner', 'admin'):
            return dict(project), None
        if not require_write and role == 'member':
            return dict(project), None

    return None, 'Access denied'


@project_history_bp.route('/<int:project_id>/history', methods=['GET'])
@login_required
def get_project_history(project_id):
    """Get paginated project history."""
    user = _get_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    project, error = _check_project_access(project_id, user['id'], require_write=False)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    action = request.args.get('action', None)
    resource_type = request.args.get('resource_type', None)
    user_id_filter = request.args.get('user_id', None, type=int)
    since_param = request.args.get('since', None)
    summary = request.args.get('summary', '').lower() == 'true'

    since = None
    if since_param:
        try:
            since = datetime.fromisoformat(since_param)
        except ValueError:
            return jsonify({'error': 'Invalid since format (use ISO datetime)'}), 400

    try:
        from phonos_app.helpers.project_history import (
            get_project_history as get_history,
            get_project_history_summary,
            cleanup_expired_history,
        )

        cleanup_expired_history(project_id)

        if summary:
            days = request.args.get('days', 7, type=int)
            return jsonify(get_project_history_summary(project_id, days=days))

        history = get_history(
            project_id=project_id,
            limit=limit,
            offset=offset,
            action_filter=action,
            resource_type_filter=resource_type,
            user_id_filter=user_id_filter,
            since=since,
        )

        db = get_db()
        total = db.execute(
            'SELECT COUNT(*) as count FROM project_history WHERE project_id = %s',
            (project_id,)
        ).fetchone()['count']

        return jsonify({
            'history': history,
            'total': total,
            'limit': limit,
            'offset': offset,
        })
    except Exception as e:
        logger.error(f"Failed to get project history: {e}")
        return jsonify({'error': str(e)}), 500


@project_history_bp.route('/<int:project_id>/history/files', methods=['GET'])
@login_required
def get_project_file_history(project_id):
    """Get file change history for a project."""
    user = _get_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    project, error = _check_project_access(project_id, user['id'], require_write=False)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    file_id = request.args.get('file_id', None, type=int)
    limit = request.args.get('limit', 50, type=int)

    try:
        from phonos_app.helpers.project_history import get_project_file_history
        if file_id:
            history = get_project_file_history(project_id, file_id, limit=limit)
        else:
            db = get_db()
            rows = db.execute(
                '''
                SELECT ph.*, u.username
                FROM project_history ph
                JOIN users u ON ph.user_id = u.id
                WHERE ph.project_id = %s AND ph.resource_type = 'file'
                ORDER BY ph.created_at DESC
                LIMIT %s
                ''',
                (project_id, limit)
            ).fetchall()
            history = []
            for row in rows:
                entry = dict(row)
                entry['metadata'] = __import__('json').loads(entry.get('metadata') or '{}')
                if entry.get('created_at') and hasattr(entry['created_at'], 'isoformat'):
                    entry['created_at'] = entry['created_at'].isoformat()
                history.append(entry)

        return jsonify({'history': history})
    except Exception as e:
        logger.error(f"Failed to get file history: {e}")
        return jsonify({'error': str(e)}), 500


@project_history_bp.route('/<int:project_id>/history/files/<int:file_id>/restore', methods=['POST'])
@login_required
def restore_project_file(project_id, file_id):
    """Restore a file from the most recent snapshot within 24h."""
    user = _get_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    project, error = _check_project_access(project_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    try:
        from phonos_app.helpers.project_history import (
            restore_project_file as restore_file,
            get_project_file_history,
        )
        from phonos_app.helpers.paths import get_safe_project_path

        base_path = get_safe_project_path(project_id)
        db = get_db()
        file_row = db.execute(
            'SELECT id, name, path FROM files WHERE id = %s AND project_id = %s',
            (file_id, project_id)
        ).fetchone()

        if not file_row:
            return jsonify({'error': 'File not found'}), 404

        current_file_path = f"{base_path}/{file_row['path'].lstrip('/')}"

        restore_file(project_id, file_id, current_file_path, user['id'])

        history = get_project_file_history(project_id, file_id, limit=1)

        return jsonify({
            'success': True,
            'message': f"File '{file_row['name']}' restored successfully",
            'file': {'id': file_row['id'], 'name': file_row['name']},
            'last_events': history,
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to restore file: {e}")
        return jsonify({'error': str(e)}), 500


@project_history_bp.route('/<int:project_id>/history/cleanup', methods=['POST'])
@login_required
def cleanup_project_history(project_id):
    """Manually trigger cleanup of expired project history entries."""
    user = _get_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    project, error = _check_project_access(project_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    try:
        from phonos_app.helpers.project_history import cleanup_expired_history
        cleaned = cleanup_expired_history(project_id)
        return jsonify({'success': True, 'cleaned_entries': cleaned})
    except Exception as e:
        logger.error(f"Failed to clean up project history: {e}")
        return jsonify({'error': str(e)}), 500


@project_history_bp.route('/<int:project_id>/history/events', methods=['POST'])
@login_required
def log_project_event(project_id):
    """Log a custom event to project history."""
    user = _get_user()
    if not user:
        return jsonify({'error': 'Authentication required'}), 401

    project, error = _check_project_access(project_id, user['id'], require_write=True)
    if error:
        return jsonify({'error': error}), 404 if 'not found' in error.lower() else 403

    data = request.get_json() or {}
    required = ['action', 'resource_type']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400

    try:
        from phonos_app.helpers.project_history import log_project_event
        event = log_project_event(
            project_id=project_id,
            user_id=user['id'],
            action=data['action'],
            resource_type=data['resource_type'],
            resource_id=data.get('resource_id'),
            resource_name=data.get('resource_name', ''),
            description=data.get('description', ''),
            metadata=data.get('metadata'),
        )
        return jsonify({'event': event, 'success': True}), 201
    except Exception as e:
        logger.error(f"Failed to log project event: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# REGISTRATION
# ============================================================================

def register_project_history_routes(app):
    """Register the project history routes with the Flask app."""
    app.register_blueprint(project_history_bp)
    app.logger.info("Project history routes registered")
