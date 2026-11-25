"""
Notebook History Manager - Git-like version control for notebooks.
Tracks cell snapshots, creates commits, enables restore and diff.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Optional

from .database import get_db

# Auto-saves are cheap and frequent (every dirty edit, every 30s), so they're
# pruned to the most recent N per notebook. Manual commits, restores, renames
# and duplicates are intentional checkpoints and are never pruned.
AUTO_SAVE_RETENTION_LIMIT = 50


def _compute_commit_hash(notebook_id: int, cells_json: str, parent_hash: Optional[str], timestamp: float, action: str = '') -> str:
    """Compute a unique SHA256 commit hash for a notebook snapshot."""
    raw = f"{notebook_id}:{cells_json}:{parent_hash or ''}:{timestamp}:{action}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _content_signature(cells: list) -> str:
    """Signature of the parts of a notebook that represent actual authored
    content (cell order, type, source) — excluding volatile fields like
    ``outputs`` and ``execution_count`` that change on every cell run without
    the user editing anything. Used to skip auto-save snapshots that would
    otherwise spam the history list with entries that have no visible diff.
    """
    sig_cells = [
        {
            'id': c.get('id'),
            'type': c.get('type', c.get('cell_type', 'code')),
            'source': c.get('source', ''),
        }
        for c in (cells or [])
    ]
    return hashlib.sha256(json.dumps(sig_cells, sort_keys=True).encode('utf-8')).hexdigest()


def _prune_auto_saves(db, notebook_id: int, keep: int = AUTO_SAVE_RETENTION_LIMIT) -> None:
    """Delete old auto_save history rows for a notebook beyond ``keep`` most
    recent, so long editing sessions don't grow the table unbounded. Never
    touches manual_commit/restore/rename/duplicate rows.
    """
    rows = db.execute(
        '''
        SELECT id FROM notebook_history
        WHERE notebook_id = %s AND action = 'auto_save'
        ORDER BY created_at DESC
        ''',
        (notebook_id,)
    ).fetchall()
    stale_ids = [r['id'] for r in rows[keep:]]
    if not stale_ids:
        return
    placeholders = ', '.join(['%s'] * len(stale_ids))
    db.execute(f'DELETE FROM notebook_history WHERE id IN ({placeholders})', tuple(stale_ids))
    db.commit()


def _serialize_timestamp(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def create_notebook_snapshot(
    notebook_id: int,
    user_id: int,
    action: str = 'auto_save',
    message: str = '',
    metadata: Optional[dict] = None
) -> dict:
    """
    Create a new commit for a notebook by snapshotting its current cells.

    Args:
        notebook_id: ID of the notebook
        user_id: ID of the user creating the snapshot
        action: Type of action ('auto_save', 'manual_commit', 'cell_change', 'rename', etc.)
        message: Commit message (required for manual_commit)
        metadata: Additional metadata (changed_cell_ids, diff_summary, etc.)

    Returns:
        Dict with commit info
    """
    db = get_db()

    notebook = db.execute(
        'SELECT id, name, cells FROM notebooks WHERE id = %s AND (is_deleted IS NULL OR is_deleted::int = 0)',
        (notebook_id,)
    ).fetchone()
    if not notebook:
        raise ValueError(f"Notebook {notebook_id} not found")

    if action == 'manual_commit' and not message.strip():
        raise ValueError("Commit message is required for manual commits")

    cells_json = json.dumps(json.loads(notebook['cells'])) if isinstance(notebook['cells'], str) else json.dumps(notebook['cells'])

    parent = db.execute(
        'SELECT id, commit_hash, cells_snapshot FROM notebook_history WHERE notebook_id = %s ORDER BY created_at DESC LIMIT 1',
        (notebook_id,)
    ).fetchone()
    parent_hash = parent['commit_hash'] if parent else None

    # Auto-saves fire on every dirty edit and every 30s tick, but re-running a
    # cell (new outputs, same code) or saving with no real edits shouldn't
    # create a new "version" with an empty diff. Skip the insert and return
    # the parent commit unchanged. Manual commits/restores/renames/duplicates
    # always create a real entry since the user explicitly asked for one.
    if action == 'auto_save' and parent is not None:
        parent_cells = json.loads(parent['cells_snapshot']) if isinstance(parent['cells_snapshot'], str) else parent['cells_snapshot']
        new_cells = json.loads(cells_json)
        if _content_signature(parent_cells) == _content_signature(new_cells):
            return _get_commit_info(parent['id'])

    timestamp = time.time()
    commit_hash = _compute_commit_hash(notebook_id, cells_json, parent_hash, timestamp, action)

    existing = db.execute(
        'SELECT id FROM notebook_history WHERE commit_hash = %s',
        (commit_hash,)
    ).fetchone()
    if existing:
        return _get_commit_info(existing['id'])

    meta_json = json.dumps(metadata or {})
    db.execute(
        '''
        INSERT INTO notebook_history
        (notebook_id, user_id, commit_hash, parent_hash, message, action, cells_snapshot, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''',
        (notebook_id, user_id, commit_hash, parent_hash, message, action, cells_json, meta_json)
    )
    db.commit()

    if action == 'auto_save':
        _prune_auto_saves(db, notebook_id)

    result = db.execute(
        'SELECT id FROM notebook_history WHERE commit_hash = %s',
        (commit_hash,)
    ).fetchone()
    return _get_commit_info(result['id'])


def _get_commit_info(history_id: int) -> Optional[dict]:
    """Get full commit info by history record ID."""
    db = get_db()
    row = db.execute(
        '''
        SELECT nh.*, u.username
        FROM notebook_history nh
        JOIN users u ON nh.user_id = u.id
        WHERE nh.id = %s
        ''',
        (history_id,)
    ).fetchone()
    if not row:
        return None

    info = dict(row)
    info['cells'] = json.loads(info.pop('cells_snapshot'))
    info['metadata'] = json.loads(info.get('metadata') or '{}')
    info['created_at'] = _serialize_timestamp(info.get('created_at'))

    cells_list = info.get('cells', []) or []
    info['cell_count'] = len(cells_list)
    info['cells_preview'] = [
        {
            'id': c.get('id', f'idx_{i}'),
            'cell_type': c.get('cell_type', 'code'),
            'source_preview': (c.get('source') or '')[:200],
        }
        for i, c in enumerate(cells_list[:50])
    ]
    return info


def get_notebook_history(
    notebook_id: int,
    limit: int = 50,
    offset: int = 0,
    action_filter: Optional[str] = None,
    search_query: Optional[str] = None
) -> list[dict]:
    """Get paginated commit history for a notebook."""
    db = get_db()

    where = 'nh.notebook_id = %s'
    params = [notebook_id]

    if action_filter:
        where += ' AND nh.action = %s'
        params.append(action_filter)

    if search_query:
        where += " AND (nh.message ILIKE %s OR u.username ILIKE %s OR nh.commit_hash ILIKE %s)"
        term = f"%{search_query}%"
        params.extend([term, term, term])

    rows = db.execute(
        f'''
        SELECT nh.*, u.username
        FROM notebook_history nh
        JOIN users u ON nh.user_id = u.id
        WHERE {where}
        ORDER BY nh.created_at DESC
        LIMIT %s OFFSET %s
        ''',
        tuple(params + [limit, offset])
    ).fetchall()

    result = []
    for row in rows:
        entry = dict(row)
        entry['cells_snapshot'] = json.loads(entry['cells_snapshot']) if isinstance(entry['cells_snapshot'], str) else entry['cells_snapshot']
        entry['metadata'] = json.loads(entry.get('metadata') or '{}')
        entry['created_at'] = _serialize_timestamp(entry.get('created_at'))
        result.append(entry)

    return result


def get_notebook_snapshot(notebook_id: int, commit_hash: str) -> Optional[dict]:
    """Get the full cells snapshot for a specific commit."""
    db = get_db()
    row = db.execute(
        '''
        SELECT nh.*, u.username
        FROM notebook_history nh
        JOIN users u ON nh.user_id = u.id
        WHERE nh.notebook_id = %s AND nh.commit_hash = %s
        ''',
        (notebook_id, commit_hash)
    ).fetchone()
    if not row:
        return None

    info = dict(row)
    info['cells'] = json.loads(info.pop('cells_snapshot'))
    info['metadata'] = json.loads(info.get('metadata') or '{}')
    info['created_at'] = _serialize_timestamp(info.get('created_at'))
    cells_list = info.get('cells', []) or []
    info['cell_count'] = len(cells_list)
    info['cells_preview'] = [
        {
            'id': c.get('id', f'idx_{i}'),
            'cell_type': c.get('cell_type', 'code'),
            'source_preview': (c.get('source') or '')[:200],
        }
        for i, c in enumerate(cells_list[:50])
    ]
    return info


def restore_notebook_version(notebook_id: int, commit_hash: str, user_id: int) -> dict:
    """
    Restore notebook to a previous commit.
    Creates a new commit with the restored cells (don't lose history).

    Returns the new commit info.
    """
    snapshot = get_notebook_snapshot(notebook_id, commit_hash)
    if not snapshot:
        raise ValueError(f"Commit {commit_hash} not found for notebook {notebook_id}")

    db = get_db()

    # Route the historical snapshot back through the same validation the
    # live save path uses (cell count cap, source length cap, allowed cell
    # types) so an old commit can't restore a notebook into a state that
    # bypasses current limits.
    from phonos_app.routes.api_notebooks import _normalize_cells
    cells = _normalize_cells(snapshot['cells'])

    cells_json = json.dumps(cells)
    db.execute(
        'UPDATE notebooks SET cells = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
        (cells_json, notebook_id)
    )
    db.commit()

    return create_notebook_snapshot(
        notebook_id=notebook_id,
        user_id=user_id,
        action='restore',
        message=f"Restored to commit {commit_hash[:8]}",
        metadata={'restored_from': commit_hash, 'restored_from_message': snapshot.get('message', '')}
    )


def _line_diff(text_a: str, text_b: str, max_lines: int = 200) -> list:
    """Compute a unified line-level diff (LCS) between two source strings.

    Returns a list of entries: ``{'type': 'ctx'|'add'|'del', 'text': str,
    'old_no': int|None, 'new_no': int|None}``. Truncated to ``max_lines`` so
    very large cells don't blow up the modal payload.
    """
    a = (text_a or '').splitlines()
    b = (text_b or '').splitlines()

    # Classic LCS dynamic programming. Cap each side to avoid pathological
    # memory on huge cells (the diff is for human review, not patching).
    cap = 400
    a = a[:cap]
    b = b[:cap]
    n, m = len(a), len(b)

    # dp[i][j] = length of LCS of a[i:] and b[j:]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if a[i] == b[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])

    result = []
    i = j = 0
    old_no = new_no = 0
    truncated = False
    while i < n or j < m:
        if i < n and j < m and a[i] == b[j]:
            if len(result) >= max_lines:
                truncated = True
                break
            old_no += 1
            new_no += 1
            result.append({'type': 'ctx', 'text': a[i], 'old_no': old_no, 'new_no': new_no})
            i += 1
            j += 1
        elif j < m and (i >= n or dp[i][j + 1] >= dp[i + 1][j]):
            if len(result) >= max_lines:
                truncated = True
                break
            new_no += 1
            result.append({'type': 'add', 'text': b[j], 'old_no': None, 'new_no': new_no})
            j += 1
        else:
            if len(result) >= max_lines:
                truncated = True
                break
            old_no += 1
            result.append({'type': 'del', 'text': a[i], 'old_no': old_no, 'new_no': None})
            i += 1
    if truncated:
        result.append({'type': 'ctx', 'text': '… (diff truncated)', 'old_no': None, 'new_no': None})
    return result


def diff_snapshots(cells_a: list, cells_b: list) -> dict:
    """
    Compute a simple cell-level diff between two snapshots.

    Returns dict with:
      - changed: list of cell_ids that differ
      - added: list of cell_ids only in cells_b
      - removed: list of cell_ids only in cells_a
      - summary: human-readable summary string
      - changed_details: list of dicts with per-cell before/after previews
        and a unified line-level diff (diff_lines) for git-style rendering.
    """
    map_a = {c.get('id', f'idx_{i}'): c for i, c in enumerate(cells_a)}
    map_b = {c.get('id', f'idx_{i}'): c for i, c in enumerate(cells_b)}

    ids_a = set(map_a.keys())
    ids_b = set(map_b.keys())

    added = list(ids_b - ids_a)
    removed = list(ids_a - ids_b)
    common = ids_a & ids_b

    # Detailed entries for added/removed cells (with a full-add / full-del
    # line diff) so the modal can show their content git-style. Kept
    # alongside the plain id lists for backward compatibility.
    added_details = []
    for cid in added:
        src = map_b[cid].get('source', '')
        added_details.append({
            'cell_id': cid,
            'cell_type': map_b[cid].get('cell_type', map_b[cid].get('type', 'code')),
            'after_preview': src[:160],
            'diff_lines': _line_diff('', src),
        })
    removed_details = []
    for cid in removed:
        src = map_a[cid].get('source', '')
        removed_details.append({
            'cell_id': cid,
            'cell_type': map_a[cid].get('cell_type', map_a[cid].get('type', 'code')),
            'before_preview': src[:160],
            'diff_lines': _line_diff(src, ''),
        })

    changed = []
    changed_details = []
    for cid in common:
        source_a = map_a[cid].get('source', '')
        source_b = map_b[cid].get('source', '')
        type_a = map_a[cid].get('cell_type', map_a[cid].get('type', 'code'))
        type_b = map_b[cid].get('cell_type', map_b[cid].get('type', 'code'))
        if source_a != source_b or type_a != type_b:
            changed.append(cid)
            changed_details.append({
                'cell_id': cid,
                'cell_type': type_b,
                'before_preview': source_a[:160],
                'after_preview': source_b[:160],
                # Real line-level unified diff for the detail/compare modal.
                'diff_lines': _line_diff(source_a, source_b),
            })

    parts = []
    if added:
        parts.append(f"{len(added)} cell(s) added")
    if removed:
        parts.append(f"{len(removed)} cell(s) removed")
    if changed:
        parts.append(f"{len(changed)} cell(s) modified")

    return {
        'changed': changed,
        'added': added,
        'removed': removed,
        'added_details': added_details,
        'removed_details': removed_details,
        'changed_details': changed_details,
        'summary': ', '.join(parts) if parts else 'No changes',
        'total_a': len(cells_a),
        'total_b': len(cells_b),
    }


def get_commit_diff(notebook_id: int, commit_hash: str) -> Optional[dict]:
    """Get diff between a commit and its parent."""
    snapshot = get_notebook_snapshot(notebook_id, commit_hash)
    if not snapshot:
        return None

    parent_hash = snapshot.get('parent_hash')
    if not parent_hash:
        return {
            'commit': snapshot,
            'parent': None,
            'diff': {'summary': 'Initial commit', 'changed': [], 'added': [], 'removed': [], 'total_a': 0, 'total_b': len(snapshot.get('cells', []))}
        }

    parent = get_notebook_snapshot(notebook_id, parent_hash)
    if not parent:
        return None

    diff = diff_snapshots(parent.get('cells', []), snapshot.get('cells', []))
    return {'commit': snapshot, 'parent': parent, 'diff': diff}


def compare_commits(notebook_id: int, base_hash: str, target_hash: str) -> Optional[dict]:
    """Get diff between two arbitrary commits."""
    base = get_notebook_snapshot(notebook_id, base_hash)
    target = get_notebook_snapshot(notebook_id, target_hash)
    if not base or not target:
        return None

    diff = diff_snapshots(base.get('cells', []), target.get('cells', []))
    return {
        'base': base,
        'target': target,
        'diff': diff,
    }


def count_notebook_commits(
    notebook_id: int,
    action_filter: Optional[str] = None,
    search_query: Optional[str] = None
) -> int:
    """Get total commit count for a notebook."""
    db = get_db()

    where = 'nh.notebook_id = %s'
    params = [notebook_id]

    if action_filter:
        where += ' AND nh.action = %s'
        params.append(action_filter)

    if search_query:
        where += " AND (nh.message ILIKE %s OR u.username ILIKE %s OR nh.commit_hash ILIKE %s)"
        term = f"%{search_query}%"
        params.extend([term, term, term])

    result = db.execute(
        f'''
        SELECT COUNT(*) as count
        FROM notebook_history nh
        JOIN users u ON nh.user_id = u.id
        WHERE {where}
        ''',
        tuple(params)
    ).fetchone()
    return result['count'] if result else 0


def get_notebook_sessions(notebook_id: int, limit: int = 50) -> list[dict]:
    """Get recent kernel sessions for a notebook."""
    db = get_db()
    rows = db.execute(
        '''
        SELECT
            id, session_token, container_id, kernel_status,
            runtime_level, workspace_path, started_at, last_activity, ended_at
        FROM notebook_sessions
        WHERE notebook_id = %s
        ORDER BY started_at DESC
        LIMIT %s
        ''',
        (notebook_id, limit)
    ).fetchall()

    result = []
    for row in rows:
        info = dict(row)
        info['started_at'] = _serialize_timestamp(info.get('started_at'))
        info['last_activity'] = _serialize_timestamp(info.get('last_activity'))
        info['ended_at'] = _serialize_timestamp(info.get('ended_at'))
        result.append(info)
    return result
