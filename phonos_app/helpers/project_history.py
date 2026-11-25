"""
Project History Manager - Activity log and file restore for projects.
Tracks all project events and supports 24-hour file snapshots.
"""
from __future__ import annotations

import json
import os
import shutil
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .database import get_db


SNAPSHOTS_DIR = '/data/project_snapshots'
DEFAULT_RETENTION_HOURS = 24


def _ensure_snapshots_dir():
    """Create the snapshots directory if it doesn't exist."""
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)


def _snapshot_path(project_id: int, resource_type: str, resource_id: int, timestamp: str) -> str:
    """Generate a path for a snapshot file."""
    _ensure_snapshots_dir()
    proj_dir = os.path.join(SNAPSHOTS_DIR, str(project_id))
    os.makedirs(proj_dir, exist_ok=True)
    safe_ts = timestamp.replace(':', '-').replace(' ', '_')
    return os.path.join(proj_dir, f"{resource_type}_{resource_id}_{safe_ts}.snap")


def log_project_event(
    project_id: int,
    user_id: int,
    action: str,
    resource_type: str,
    resource_id: Optional[int] = None,
    resource_name: str = '',
    description: str = '',
    metadata: Optional[dict] = None,
    file_snapshot_path: Optional[str] = None,
    retention_hours: int = DEFAULT_RETENTION_HOURS
) -> dict:
    """
    Log an event in the project history.

    Args:
        project_id: ID of the project
        user_id: ID of the user performing the action
        action: Action type ('file_upload', 'file_edit', 'file_delete', 'file_rename',
                'notebook_edit', 'member_add', 'member_remove', 'settings_change', etc.)
        resource_type: Type of resource ('file', 'notebook', 'project_settings', 'member')
        resource_id: ID of the resource
        resource_name: Human-readable name of the resource
        description: Human-readable description of the event
        metadata: Additional JSON-serializable context
        file_snapshot_path: Path to a stored file snapshot (if applicable)
        retention_hours: Hours to retain this entry (default 24)

    Returns:
        Dict with the created event info
    """
    db = get_db()

    meta_json = json.dumps(metadata or {})
    retention_until = datetime.utcnow() + timedelta(hours=retention_hours)

    db.execute(
        '''
        INSERT INTO project_history
        (project_id, user_id, action, resource_type, resource_id, resource_name,
         description, snapshot_path, metadata, retention_until)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''',
        (project_id, user_id, action, resource_type, resource_id, resource_name,
         description, file_snapshot_path, meta_json, retention_until)
    )
    db.commit()

    result = db.execute(
        'SELECT id FROM project_history WHERE project_id = %s ORDER BY created_at DESC LIMIT 1',
        (project_id,)
    ).fetchone()

    return {'id': result['id'] if result else None, 'action': action, 'resource_type': resource_type}


def get_project_history(
    project_id: int,
    limit: int = 100,
    offset: int = 0,
    action_filter: Optional[str] = None,
    resource_type_filter: Optional[str] = None,
    user_id_filter: Optional[int] = None,
    since: Optional[datetime] = None
) -> list[dict]:
    """Get paginated, filtered history for a project."""
    db = get_db()

    where = 'ph.project_id = %s'
    params = [project_id]

    if action_filter:
        where += ' AND ph.action = %s'
        params.append(action_filter)

    if resource_type_filter:
        where += ' AND ph.resource_type = %s'
        params.append(resource_type_filter)

    if user_id_filter:
        where += ' AND ph.user_id = %s'
        params.append(user_id_filter)

    if since:
        where += ' AND ph.created_at >= %s'
        params.append(since)

    rows = db.execute(
        f'''
        SELECT ph.*, u.username
        FROM project_history ph
        JOIN users u ON ph.user_id = u.id
        WHERE {where}
        ORDER BY ph.created_at DESC
        LIMIT %s OFFSET %s
        ''',
        tuple(params + [limit, offset])
    ).fetchall()

    result = []
    for row in rows:
        entry = dict(row)
        entry['metadata'] = json.loads(entry.get('metadata') or '{}')
        if entry.get('created_at') and hasattr(entry['created_at'], 'isoformat'):
            entry['created_at'] = entry['created_at'].isoformat()
        if entry.get('retention_until') and hasattr(entry['retention_until'], 'isoformat'):
            entry['retention_until'] = entry['retention_until'].isoformat()
        result.append(entry)

    return result


def snapshot_project_file(project_id: int, file_id: int, file_path: str, user_id: int) -> str:
    """
    Create a snapshot of a project file for 24-hour restore capability.

    Args:
        project_id: Project ID
        file_id: File ID
        file_path: Absolute path to the file on disk
        user_id: User performing the action

    Returns:
        Path to the snapshot file
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    snap = _snapshot_path(project_id, 'file', file_id, timestamp)
    _ensure_snapshots_dir()

    shutil.copy2(file_path, snap)

    log_project_event(
        project_id=project_id,
        user_id=user_id,
        action='file_edit',
        resource_type='file',
        resource_id=file_id,
        resource_name=os.path.basename(file_path),
        description=f"Auto-snapshot of {os.path.basename(file_path)}",
        file_snapshot_path=snap,
    )

    return snap


def get_file_snapshot_within_window(project_id: int, file_id: int, max_age_hours: int = DEFAULT_RETENTION_HOURS) -> Optional[str]:
    """
    Find the most recent file snapshot within the retention window.

    Args:
        project_id: Project ID
        file_id: File ID
        max_age_hours: Maximum age of snapshot to consider

    Returns:
        Path to the snapshot file, or None
    """
    db = get_db()
    since = datetime.utcnow() - timedelta(hours=max_age_hours)

    row = db.execute(
        '''
        SELECT snapshot_path FROM project_history
        WHERE project_id = %s AND resource_type = 'file' AND resource_id = %s
        AND snapshot_path IS NOT NULL AND created_at >= %s
        ORDER BY created_at DESC LIMIT 1
        ''',
        (project_id, file_id, since)
    ).fetchone()

    if row and row['snapshot_path'] and os.path.exists(row['snapshot_path']):
        return row['snapshot_path']
    return None


def restore_project_file(project_id: int, file_id: int, current_file_path: str, user_id: int) -> bool:
    """
    Restore a project file from the most recent snapshot within 24 hours.

    Args:
        project_id: Project ID
        file_id: File ID
        current_file_path: Current file path to overwrite
        user_id: User performing the restore

    Returns:
        True if restored successfully
    """
    snapshot = get_file_snapshot_within_window(project_id, file_id)
    if not snapshot:
        raise ValueError(f"No snapshot found for file {file_id} within retention window")

    if not os.path.exists(current_file_path):
        os.makedirs(os.path.dirname(current_file_path), exist_ok=True)

    shutil.copy2(snapshot, current_file_path)

    log_project_event(
        project_id=project_id,
        user_id=user_id,
        action='file_restore',
        resource_type='file',
        resource_id=file_id,
        resource_name=os.path.basename(current_file_path),
        description=f"Restored from snapshot"
    )

    return True


def get_project_file_history(project_id: int, file_id: int, limit: int = 50) -> list[dict]:
    """Get history entries for a specific file in a project."""
    db = get_db()

    rows = db.execute(
        '''
        SELECT ph.*, u.username
        FROM project_history ph
        JOIN users u ON ph.user_id = u.id
        WHERE ph.project_id = %s AND ph.resource_type = 'file' AND ph.resource_id = %s
        ORDER BY ph.created_at DESC
        LIMIT %s
        ''',
        (project_id, file_id, limit)
    ).fetchall()

    result = []
    for row in rows:
        entry = dict(row)
        entry['metadata'] = json.loads(entry.get('metadata') or '{}')
        if entry.get('created_at') and hasattr(entry['created_at'], 'isoformat'):
            entry['created_at'] = entry['created_at'].isoformat()
        result.append(entry)

    return result


def cleanup_expired_history(project_id: Optional[int] = None) -> int:
    """
    Remove expired project history entries (past retention_until).

    Args:
        project_id: If specified, only clean up for this project

    Returns:
        Number of entries cleaned up
    """
    db = get_db()

    where = 'retention_until IS NOT NULL AND retention_until < CURRENT_TIMESTAMP'
    params = []

    if project_id:
        where += ' AND project_id = %s'
        params.append(project_id)

    rows = db.execute(
        f'SELECT id, snapshot_path FROM project_history WHERE {where}',
        tuple(params) if params else None
    ).fetchall()

    cleaned = 0
    for row in rows:
        if row['snapshot_path'] and os.path.exists(row['snapshot_path']):
            try:
                os.remove(row['snapshot_path'])
            except OSError:
                pass
        db.execute('DELETE FROM project_history WHERE id = %s', (row['id'],))
        cleaned += 1

    if cleaned:
        db.commit()

    return cleaned


def get_project_history_summary(project_id: int, days: int = 7) -> dict:
    """Get a summary of project activity over a time period."""
    db = get_db()
    since = datetime.utcnow() - timedelta(days=days)

    total = db.execute(
        'SELECT COUNT(*) as count FROM project_history WHERE project_id = %s AND created_at >= %s',
        (project_id, since)
    ).fetchone()['count']

    by_action = db.execute(
        '''
        SELECT action, COUNT(*) as count
        FROM project_history
        WHERE project_id = %s AND created_at >= %s
        GROUP BY action ORDER BY count DESC
        ''',
        (project_id, since)
    ).fetchall()

    by_user = db.execute(
        '''
        SELECT ph.user_id, u.username, COUNT(*) as count
        FROM project_history ph
        JOIN users u ON ph.user_id = u.id
        WHERE ph.project_id = %s AND ph.created_at >= %s
        GROUP BY ph.user_id, u.username ORDER BY count DESC LIMIT 10
        ''',
        (project_id, since)
    ).fetchall()

    return {
        'total_events': total,
        'by_action': [dict(r) for r in by_action],
        'by_user': [dict(r) for r in by_user],
        'period_days': days,
    }
