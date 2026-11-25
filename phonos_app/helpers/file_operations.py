"""
Enhanced File Operations - Bulk rename, copy, preview for project files.
Extends the basic file management with advanced operations.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import mimetypes
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

TEXT_PREVIEW_MAX_SIZE = 1024 * 1024  # 1MB for text preview
PREVIEW_LINE_LIMIT = 2000
PREVIEW_CHAR_LIMIT = 50000
IMAGE_PREVIEW_MAX_SIZE = 10 * 1024 * 1024  # 10MB for image preview
STRUCTURE_EXTENSIONS = {'.pdb', '.cif', '.mmcif', '.mol', '.mol2', '.sdf', '.xyz'}
DATA_EXTENSIONS = {'.csv', '.tsv', '.json', '.xml', '.yaml', '.yml'}
CODE_EXTENSIONS = {'.py', '.r', '.jl', '.sh', '.js', '.ts', '.ipynb',
                   '.nb', '.wl', '.wls', '.m'}


def bulk_rename_files(
    project_id: int,
    file_ids: list[int],
    pattern: str,
    replacement: str,
    use_regex: bool = False,
    dry_run: bool = False
) -> dict:
    """
    Rename multiple files using pattern matching.

    Args:
        project_id: Project ID (for security verification)
        file_ids: List of file IDs to rename
        pattern: Pattern to match (glob or regex depending on use_regex)
        replacement: Replacement string ($1, $2 for regex groups)
        use_regex: If True, pattern is treated as regex
        dry_run: If True, only return what would change without executing

    Returns:
        Dict with 'results' list and 'errors' list

    Each result: {'file_id': int, 'old_name': str, 'new_name': str, 'success': bool}
    """
    from .database import get_db
    from .paths import get_safe_project_path

    db = get_db()
    base_path = get_safe_project_path(project_id)
    results = []
    errors = []

    compiled = re.compile(pattern) if use_regex else None

    for file_id in file_ids:
        try:
            row = db.execute(
                'SELECT id, name, path FROM files WHERE id = %s AND project_id = %s',
                (file_id, project_id)
            ).fetchone()

            if not row:
                errors.append({'file_id': file_id, 'error': 'File not found'})
                continue

            old_name = row['name']

            if use_regex and compiled:
                new_name = compiled.sub(replacement, old_name)
            else:
                new_name = old_name.replace(pattern, replacement)

            if new_name == old_name:
                errors.append({'file_id': file_id, 'error': 'Pattern produced same name', 'name': old_name})
                continue

            new_name = secure_filename(new_name)
            if not new_name:
                errors.append({'file_id': file_id, 'error': 'Invalid name after sanitization', 'name': old_name})
                continue

            new_path = str(Path(row['path']).parent / new_name)
            old_full = os.path.join(base_path, row['path'])
            new_full = os.path.join(base_path, new_path)

            if not dry_run:
                if os.path.exists(new_full):
                    errors.append({'file_id': file_id, 'error': 'Target already exists', 'name': old_name})
                    continue

                if not os.path.exists(old_full):
                    errors.append({'file_id': file_id, 'error': 'Source file not found on disk', 'name': old_name})
                    continue

                os.rename(old_full, new_full)
                db.execute(
                    'UPDATE files SET name = %s, path = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
                    (new_name, new_path, file_id)
                )
                db.commit()

            results.append({
                'file_id': file_id,
                'old_name': old_name,
                'new_name': new_name,
                'success': True
            })

        except Exception as e:
            logger.error(f"Bulk rename error for file {file_id}: {e}")
            errors.append({'file_id': file_id, 'error': str(e)})

    return {'results': results, 'errors': errors, 'dry_run': dry_run}


def copy_file(
    project_id: int,
    file_id: int,
    new_name: Optional[str] = None,
    target_path: Optional[str] = None,
    user_id: Optional[int] = None
) -> dict:
    """
    Copy/duplicate a file within a project.

    Args:
        project_id: Project ID
        file_id: File ID to copy
        new_name: New name (if None, appends '_copy')
        target_path: Target directory path (if None, same directory)
        user_id: User performing the copy

    Returns:
        Dict with the new file info
    """
    from .database import get_db
    from .paths import get_safe_project_path

    db = get_db()
    base_path = get_safe_project_path(project_id)

    source = db.execute(
        'SELECT id, name, path FROM files WHERE id = %s AND project_id = %s',
        (file_id, project_id)
    ).fetchone()

    if not source:
        raise ValueError(f"File {file_id} not found")

    old_name = source['name']
    old_path = source['path']
    old_full = os.path.join(base_path, old_path)

    if not os.path.exists(old_full):
        raise FileNotFoundError(f"Source file not found on disk: {old_full}")

    if new_name:
        new_filename = secure_filename(new_name)
    else:
        stem, ext = os.path.splitext(old_name)
        new_filename = secure_filename(f"{stem}_copy{ext}")

    if target_path:
        new_rel_dir = target_path.lstrip('/')
    else:
        new_rel_dir = str(Path(old_path).parent)

    new_rel_path = os.path.join(new_rel_dir, new_filename).replace('\\', '/')
    new_full = os.path.join(base_path, new_rel_path)

    if os.path.exists(new_full):
        stem, ext = os.path.splitext(new_filename)
        counter = 1
        while os.path.exists(new_full):
            new_filename = secure_filename(f"{stem}_{counter}{ext}")
            new_rel_path = os.path.join(new_rel_dir, new_filename).replace('\\', '/')
            new_full = os.path.join(base_path, new_rel_path)
            counter += 1

    os.makedirs(os.path.dirname(new_full), exist_ok=True)
    shutil.copy2(old_full, new_full)

    result = db.execute(
        '''
        INSERT INTO files (project_id, creator_id, name, path, size, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id
        ''',
        (project_id, user_id or 0, new_filename, new_rel_path, os.path.getsize(new_full))
    )
    new_id = result.fetchone()['id']
    db.commit()

    return {
        'id': new_id,
        'name': new_filename,
        'path': new_rel_path,
        'size': os.path.getsize(new_full),
        'source_file_id': file_id,
    }


def get_file_preview(project_id: int, file_id: int) -> dict:
    """
    Generate a preview of a file.

    Returns structured data suitable for frontend rendering:
    - type: 'text', 'image', 'structure', 'data', 'binary', 'too_large'
    - content: preview content (truncated)
    - metadata: file info
    """
    from .database import get_db
    from .paths import get_safe_project_path

    db = get_db()
    base_path = get_safe_project_path(project_id)

    row = db.execute(
        'SELECT id, name, path, size, mime_type FROM files WHERE id = %s AND project_id = %s',
        (file_id, project_id)
    ).fetchone()

    if not row:
        raise ValueError(f"File {file_id} not found")

    full_path = os.path.join(base_path, row['path'])
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"File not found on disk: {full_path}")

    size = row['size'] or os.path.getsize(full_path)
    name = row['name']
    mime_type = row['mime_type'] or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    ext = Path(name).suffix.lower()

    base_info = {
        'file_id': file_id,
        'name': name,
        'path': row['path'],
        'size': size,
        'mime_type': mime_type,
        'modified': datetime.fromtimestamp(os.path.getmtime(full_path)).isoformat(),
    }

    if ext in IMAGE_PREVIEW_EXTENSIONS():
        if size > IMAGE_PREVIEW_MAX_SIZE:
            return {**base_info, 'type': 'too_large', 'message': 'Image too large to preview'}
        return {**base_info, 'type': 'image'}

    if ext in STRUCTURE_EXTENSIONS:
        content = _read_text_preview(full_path)
        return {**base_info, 'type': 'structure', 'content': content}

    if ext in DATA_EXTENSIONS:
        content = _read_text_preview(full_path)
        return {**base_info, 'type': 'data', 'content': content}

    text_types = {'.txt', '.md', '.log', '.py', '.r', '.jl', '.sh', '.js', '.ts',
                  '.html', '.css', '.xml', '.yaml', '.yml', '.json', '.csv', '.tsv',
                  '.cfg', '.conf', '.ini', '.env', '.gitignore', '.dockerfile'}
    if ext in text_types or mime_type.startswith('text/'):
        if size > TEXT_PREVIEW_MAX_SIZE:
            return {**base_info, 'type': 'too_large', 'message': 'File too large for text preview'}
        content = _read_text_preview(full_path)
        return {**base_info, 'type': 'text', 'content': content}

    return {**base_info, 'type': 'binary', 'message': 'Preview not available for this file type'}


def IMAGE_PREVIEW_EXTENSIONS():
    return {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp'}


def _read_text_preview(file_path: str) -> str:
    """Read a text file with line and character limits."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = []
            char_count = 0
            for i, line in enumerate(f):
                if i >= PREVIEW_LINE_LIMIT:
                    lines.append(f'\n... (truncated at {PREVIEW_LINE_LIMIT} lines)')
                    break
                char_count += len(line)
                if char_count > PREVIEW_CHAR_LIMIT:
                    lines.append(line[:PREVIEW_CHAR_LIMIT - (char_count - len(line))])
                    lines.append(f'\n... (truncated at {PREVIEW_CHAR_LIMIT} characters)')
                    break
                lines.append(line)
            return ''.join(lines)
    except Exception as e:
        logger.warning(f"Error reading preview for {file_path}: {e}")
        return f"Error reading file: {e}"


def ensure_file_snapshot_on_change(project_id: int, file_id: int, file_path: str, user_id: int) -> Optional[str]:
    """
    Helper to create a project history snapshot when a file changes.
    Call this from file upload/edit endpoints.

    Args:
        project_id: Project ID
        file_id: File ID
        file_path: Absolute path to the file
        user_id: User performing the action

    Returns:
        Snapshot path or None
    """
    try:
        from .project_history import snapshot_project_file
        return snapshot_project_file(project_id, file_id, file_path, user_id)
    except Exception as e:
        logger.warning(f"Failed to snapshot file {file_id}: {e}")
        return None
