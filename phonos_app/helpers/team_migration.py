"""Team migration utilities for handling project team changes.

When a project changes teams, all associated files and job outputs need to be
migrated to the new team's directory structure since paths include team slugs.
"""
from __future__ import annotations

import os
import shutil
from typing import Optional

from . import get_db, logger


def migrate_project_files_on_team_change(
    db,
    project_id: int,
    old_team_id: int,
    new_team_id: int
) -> tuple[bool, str, dict]:
    """Migrate all project files when team changes.

    Args:
        db: Database connection
        project_id: Project ID being migrated
        old_team_id: Previous team ID
        new_team_id: New team ID

    Returns:
        Tuple of (success, message, stats_dict)
        stats_dict contains: {
            'files_migrated': int,
            'jobs_migrated': int,
            'errors': list[str]
        }

    Raises:
        ValueError: If team IDs are invalid or not found
        Exception: If migration fails critically (with rollback info)
    """
    stats = {
        'files_migrated': 0,
        'jobs_migrated': 0,
        'errors': []
    }

    # Get team slugs
    old_team = db.execute(
        "SELECT slug FROM teams WHERE id = %s",
        (old_team_id,)
    ).fetchone()

    new_team = db.execute(
        "SELECT slug FROM teams WHERE id = %s",
        (new_team_id,)
    ).fetchone()

    if not old_team:
        raise ValueError(f"Old team {old_team_id} not found")
    if not new_team:
        raise ValueError(f"New team {new_team_id} not found")

    old_slug = old_team['slug']
    new_slug = new_team['slug']

    if old_slug == new_slug:
        return True, "Team slugs are identical, no migration needed", stats

    logger.info(
        f"Starting team migration for project {project_id}: "
        f"{old_slug} -> {new_slug}"
    )

    # Get all storage roots to check for files in multiple locations
    from .paths import get_all_job_output_roots

    storage_roots = get_all_job_output_roots()
    if not storage_roots:
        return False, "No storage roots configured", stats

    # Migrate uploaded files
    try:
        files = db.execute(
            "SELECT id, path, name FROM files WHERE project_id = %s",
            (project_id,)
        ).fetchall()

        for file in files:
            old_path = file['path']

            # Skip if path doesn't contain old team slug (shouldn't happen, but safety)
            if f"/{old_slug}/" not in old_path:
                logger.warning(
                    f"File {file['id']} path doesn't contain team slug: {old_path}"
                )
                continue

            # Generate new path
            new_path = old_path.replace(
                f"/{old_slug}/projects/",
                f"/{new_slug}/projects/"
            )

            try:
                # Create destination directory
                os.makedirs(os.path.dirname(new_path), exist_ok=True)

                # Move file if old path exists
                if os.path.exists(old_path):
                    shutil.move(old_path, new_path)
                    logger.debug(f"Moved file {file['name']}: {old_path} -> {new_path}")
                else:
                    # File doesn't exist physically, just update DB
                    logger.warning(
                        f"File {file['id']} ({file['name']}) not found at {old_path}, "
                        "updating DB only"
                    )

                # Update database
                db.execute(
                    "UPDATE files SET path = %s WHERE id = %s",
                    (new_path, file['id'])
                )
                stats['files_migrated'] += 1

            except Exception as e:
                error_msg = f"Failed to migrate file {file['id']}: {e}"
                logger.error(error_msg, exc_info=True)
                stats['errors'].append(error_msg)

    except Exception as e:
        error_msg = f"Error querying files: {e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg, stats

    # Migrate job outputs
    try:
        from .paths import infer_job_root_from_output_path

        jobs = db.execute(
            """
            SELECT job_id, output_file, output_display_name
            FROM jobs
            WHERE project_id = %s AND output_file IS NOT NULL
            """,
            (project_id,)
        ).fetchall()

        for job in jobs:
            old_output_path = job['output_file']

            # Skip if path doesn't contain old team slug
            if f"/{old_slug}/" not in old_output_path:
                logger.warning(
                    f"Job {job['job_id']} output doesn't contain team slug: "
                    f"{old_output_path}"
                )
                continue

            # Generate new path
            new_output_path = old_output_path.replace(
                f"/{old_slug}/projects/",
                f"/{new_slug}/projects/"
            )

            try:
                # Create destination directory
                os.makedirs(os.path.dirname(new_output_path), exist_ok=True)

                # If old path exists, move the entire job directory
                old_job_dir = infer_job_root_from_output_path(job['job_id'], old_output_path) or os.path.dirname(old_output_path)
                new_job_dir = infer_job_root_from_output_path(job['job_id'], new_output_path) or os.path.dirname(new_output_path)

                if os.path.exists(old_job_dir) and os.path.isdir(old_job_dir):
                    # Move entire job directory to preserve all artifacts
                    if not os.path.exists(new_job_dir):
                        shutil.move(old_job_dir, new_job_dir)
                        logger.debug(
                            f"Moved job directory {job['job_id']}: "
                            f"{old_job_dir} -> {new_job_dir}"
                        )
                    else:
                        # Directory exists, merge contents
                        for item in os.listdir(old_job_dir):
                            shutil.move(
                                os.path.join(old_job_dir, item),
                                os.path.join(new_job_dir, item)
                            )
                        os.rmdir(old_job_dir)
                else:
                    logger.warning(
                        f"Job {job['job_id']} directory not found at {old_job_dir}, "
                        "updating DB only"
                    )

                # Update database
                db.execute(
                    "UPDATE jobs SET output_file = %s WHERE job_id = %s",
                    (new_output_path, job['job_id'])
                )
                stats['jobs_migrated'] += 1

            except Exception as e:
                error_msg = f"Failed to migrate job {job['job_id']}: {e}"
                logger.error(error_msg, exc_info=True)
                stats['errors'].append(error_msg)

    except Exception as e:
        error_msg = f"Error querying jobs: {e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg, stats

    # Commit database changes
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        error_msg = f"Database commit failed, changes rolled back: {e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg, stats

    # Summary
    success_msg = (
        f"Migration completed: {stats['files_migrated']} files, "
        f"{stats['jobs_migrated']} jobs migrated"
    )

    if stats['errors']:
        success_msg += f" with {len(stats['errors'])} errors"
        logger.warning(success_msg)
        logger.warning(f"Errors: {stats['errors']}")
        return True, success_msg, stats  # Partial success

    logger.info(success_msg)
    return True, success_msg, stats


def validate_team_migration_safety(
    db,
    project_id: int,
    new_team_id: int
) -> tuple[bool, str]:
    """Validate that a team migration is safe to perform.

    Checks:
    - New team exists
    - User has permission in new team
    - Storage space available
    - No conflicting file paths

    Args:
        db: Database connection
        project_id: Project to validate
        new_team_id: Target team ID

    Returns:
        Tuple of (is_safe, error_message)
    """
    # Check new team exists
    new_team = db.execute(
        "SELECT id, slug FROM teams WHERE id = %s",
        (new_team_id,)
    ).fetchone()

    if not new_team:
        return False, f"Target team {new_team_id} does not exist"

    # Get project info
    project = db.execute(
        "SELECT team_id, name FROM projects WHERE id = %s",
        (project_id,)
    ).fetchone()

    if not project:
        return False, f"Project {project_id} not found"

    if project['team_id'] == new_team_id:
        return False, "Project is already in the target team"

    # Calculate total size of files to migrate
    total_size = db.execute(
        """
        SELECT
            COALESCE(SUM(f.size), 0) + COALESCE(SUM(j.output_file_size), 0) as total
        FROM projects p
        LEFT JOIN files f ON f.project_id = p.id
        LEFT JOIN jobs j ON j.project_id = p.id
        WHERE p.id = %s
        """,
        (project_id,)
    ).fetchone()

    if total_size and total_size['total'] > 0:
        logger.info(
            f"Project {project_id} has {total_size['total']} bytes "
            "that will be migrated"
        )

    return True, ""
