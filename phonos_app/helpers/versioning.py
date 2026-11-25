"""File versioning management helpers."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from .database import get_db


def calculate_checksum(file_path: str) -> str:
    """Calculate SHA256 checksum of a file."""
    sha256_hash = hashlib.sha256()

    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)

    return sha256_hash.hexdigest()


def create_file_version(file_id: int, file_path: str, user_id: int) -> dict:
    """Create a new version of a file."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Get the next version number
    result = db.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1 as next_version
        FROM file_versions
        WHERE file_id = %s
        """,
        (file_id,),
    )

    next_version = result.fetchone()["next_version"]

    # Get file size and checksum
    file_size = os.path.getsize(file_path)
    checksum = calculate_checksum(file_path)

    # Create versioned file path
    file_path_obj = Path(file_path)
    version_dir = file_path_obj.parent / ".versions"
    version_dir.mkdir(exist_ok=True)

    version_filename = f"{file_path_obj.stem}_v{next_version}{file_path_obj.suffix}"
    version_path = version_dir / version_filename

    # Copy file to version storage
    shutil.copy2(file_path, version_path)

    # Insert version record
    result = db.execute(
        """
        INSERT INTO file_versions (file_id, version_number, path, size, checksum, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (file_id, next_version, str(version_path), file_size, checksum, user_id),
    )

    version_id = result.lastrowid
    db.commit()

    return {
        "id": version_id,
        "file_id": file_id,
        "version_number": next_version,
        "path": str(version_path),
        "size": file_size,
        "checksum": checksum,
        "created_by": user_id,
    }


def get_file_versions(file_id: int) -> list[dict]:
    """Get all versions of a file."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT fv.id, fv.file_id, fv.version_number, fv.path, fv.size,
               fv.checksum, fv.created_by, fv.created_at, u.username
        FROM file_versions fv
        JOIN users u ON fv.created_by = u.id
        WHERE fv.file_id = %s
        ORDER BY fv.version_number DESC
        """,
        (file_id,),
    )

    versions = []
    for row in result.fetchall():
        versions.append(dict(row))

    return versions


def get_file_version(file_id: int, version_number: int) -> dict:
    """Get a specific version of a file."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT fv.id, fv.file_id, fv.version_number, fv.path, fv.size,
               fv.checksum, fv.created_by, fv.created_at, u.username
        FROM file_versions fv
        JOIN users u ON fv.created_by = u.id
        WHERE fv.file_id = %s AND fv.version_number = %s
        """,
        (file_id, version_number),
    )

    row = result.fetchone()
    if not row:
        return None

    return dict(row)


def revert_to_version(file_id: int, version_number: int, current_file_path: str, user_id: int) -> bool:
    """Revert a file to a previous version."""
    version = get_file_version(file_id, version_number)
    if not version:
        return False

    version_path = version["path"]
    if not os.path.exists(version_path):
        return False

    # Create a new version of the current file before reverting
    create_file_version(file_id, current_file_path, user_id)

    # Copy the old version to the current file
    shutil.copy2(version_path, current_file_path)

    return True


def delete_file_version(file_id: int, version_number: int) -> bool:
    """Delete a specific file version."""
    version = get_file_version(file_id, version_number)
    if not version:
        return False

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Delete the physical file
    version_path = version["path"]
    if os.path.exists(version_path):
        os.remove(version_path)

    # Delete the database record
    result = db.execute(
        """
        DELETE FROM file_versions
        WHERE file_id = %s AND version_number = %s
        """,
        (file_id, version_number),
    )

    db.commit()
    return result.rowcount > 0


def cleanup_old_versions(file_id: int, keep_latest: int = 10) -> int:
    """Clean up old versions, keeping only the latest N versions."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, path, version_number
        FROM file_versions
        WHERE file_id = %s
        ORDER BY version_number DESC
        LIMIT -1 OFFSET %s
        """,
        (file_id, keep_latest),
    )

    deleted_count = 0
    for row in result.fetchall():
        version_path = row["path"]
        if os.path.exists(version_path):
            os.remove(version_path)

        db.execute("DELETE FROM file_versions WHERE id = %s", (row["id"],))
        deleted_count += 1

    db.commit()
    return deleted_count
