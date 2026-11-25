#!/usr/bin/env python3
"""Cleanup orphaned files and validate file system integrity.

This script identifies and optionally removes:
1. Database records for files that don't exist physically
2. Physical files that aren't tracked in the database

Usage:
    # Preview what would be cleaned (safe, default)
    python cleanup_orphaned_files.py --dry-run

    # Actually clean orphaned records
    python cleanup_orphaned_files.py --execute

    # Check for untracked physical files
    python cleanup_orphaned_files.py --check-untracked

    # Generate detailed report
    python cleanup_orphaned_files.py --report
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from production.database import get_db_connection


def format_bytes(num_bytes: int) -> str:
    """Format bytes in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


def find_orphaned_database_records(db) -> list[dict[str, Any]]:
    """Find database records for files that don't exist physically.

    Returns:
        List of dicts with keys: id, path, size, creator_id, project_id, created_at
    """
    print("\n[1/3] Scanning database for file records...")

    files = db.execute(
        "SELECT id, path, size, creator_id, project_id, created_at, name "
        "FROM files ORDER BY created_at DESC"
    ).fetchall()

    print(f"Found {len(files)} file records in database")

    orphaned = []
    checked = 0

    for file in files:
        checked += 1
        if checked % 100 == 0:
            print(f"  Checked {checked}/{len(files)} files...", end='\r')

        if not os.path.exists(file['path']):
            orphaned.append(dict(file))

    print(f"\n  ✓ Checked {checked} files")
    print(f"  ⚠ Found {len(orphaned)} orphaned database records")

    return orphaned


def find_orphaned_job_outputs(db) -> list[dict[str, Any]]:
    """Find database records for job outputs that don't exist physically.

    Returns:
        List of dicts with keys: job_id, output_file, output_file_size, creator_id, project_id
    """
    print("\n[2/3] Scanning database for job output records...")

    jobs = db.execute(
        """
        SELECT job_id, output_file, output_file_size, creator_id, project_id, created_at
        FROM jobs
        WHERE output_file IS NOT NULL
        ORDER BY created_at DESC
        """
    ).fetchall()

    print(f"Found {len(jobs)} job output records in database")

    orphaned = []
    checked = 0

    for job in jobs:
        checked += 1
        if checked % 100 == 0:
            print(f"  Checked {checked}/{len(jobs)} jobs...", end='\r')

        if not os.path.exists(job['output_file']):
            orphaned.append(dict(job))

    print(f"\n  ✓ Checked {checked} job outputs")
    print(f"  ⚠ Found {len(orphaned)} orphaned job output records")

    return orphaned


def find_untracked_physical_files(db) -> list[str]:
    """Find physical files not tracked in database.

    Warning: This can be slow for large filesystems.

    Returns:
        List of file paths not in database
    """
    print("\n[3/3] Scanning filesystem for untracked files...")

    # Get all tracked paths from database
    tracked_files = set()

    # Add uploaded files
    file_paths = db.execute("SELECT path FROM files").fetchall()
    for row in file_paths:
        tracked_files.add(row['path'])

    # Add job outputs
    job_paths = db.execute(
        "SELECT output_file FROM jobs WHERE output_file IS NOT NULL"
    ).fetchall()
    for row in job_paths:
        tracked_files.add(row['output_file'])

    print(f"Database tracks {len(tracked_files)} file paths")

    # Get storage roots from config
    from phonos_app.helpers.paths import get_all_job_output_roots

    storage_roots = get_all_job_output_roots()
    print(f"Scanning {len(storage_roots)} storage locations...")

    untracked = []

    for root_path in storage_roots:
        if not os.path.exists(root_path):
            print(f"  ⚠ Storage root doesn't exist: {root_path}")
            continue

        print(f"  Scanning {root_path}...")

        for root, dirs, files in os.walk(root_path):
            for filename in files:
                filepath = os.path.join(root, filename)

                # Skip temporary files
                if filename.endswith('.tmp') or filename.startswith('.'):
                    continue

                if filepath not in tracked_files:
                    untracked.append(filepath)

            # Progress indicator
            if len(untracked) % 100 == 0 and untracked:
                print(f"    Found {len(untracked)} untracked files so far...", end='\r')

    print(f"\n  ⚠ Found {len(untracked)} untracked physical files")

    return untracked


def cleanup_orphaned_records(db, orphaned_files: list, orphaned_jobs: list, dry_run: bool = True) -> dict:
    """Remove database records for missing files.

    Args:
        db: Database connection
        orphaned_files: List of orphaned file records
        orphaned_jobs: List of orphaned job output records
        dry_run: If True, only preview without making changes

    Returns:
        Dict with statistics: {
            'files_deleted': int,
            'jobs_updated': int,
            'storage_reclaimed': int (bytes)
        }
    """
    stats = {
        'files_deleted': 0,
        'jobs_updated': 0,
        'storage_reclaimed': 0
    }

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Cleaning orphaned records...")

    # Clean orphaned file records
    if orphaned_files:
        print(f"\n  Processing {len(orphaned_files)} orphaned file records...")

        for file in orphaned_files:
            if not dry_run:
                try:
                    # Delete record
                    db.execute("DELETE FROM files WHERE id = %s", (file['id'],))

                    # Adjust storage quota
                    if file['size'] > 0:
                        db.execute(
                            "UPDATE users SET storage_used = GREATEST(0, storage_used - %s) WHERE id = %s",
                            (file['size'], file['creator_id'])
                        )

                    stats['files_deleted'] += 1
                    stats['storage_reclaimed'] += file['size']

                except Exception as e:
                    print(f"    ✗ Error deleting file {file['id']}: {e}")
            else:
                stats['files_deleted'] += 1
                stats['storage_reclaimed'] += file['size']

        print(f"  {'Would delete' if dry_run else 'Deleted'} {stats['files_deleted']} file records")
        print(f"  {'Would reclaim' if dry_run else 'Reclaimed'} {format_bytes(stats['storage_reclaimed'])}")

    # Clean orphaned job output references
    if orphaned_jobs:
        print(f"\n  Processing {len(orphaned_jobs)} orphaned job output records...")

        for job in orphaned_jobs:
            if not dry_run:
                try:
                    # Clear output file reference (don't delete job record, just clear the output)
                    db.execute(
                        """
                        UPDATE jobs
                        SET output_file = NULL,
                            output_file_size = NULL,
                            output_display_name = NULL
                        WHERE job_id = %s
                        """,
                        (job['job_id'],)
                    )

                    # Adjust storage quota
                    if job['output_file_size'] and job['output_file_size'] > 0:
                        db.execute(
                            "UPDATE users SET storage_used = GREATEST(0, storage_used - %s) WHERE id = %s",
                            (job['output_file_size'], job['creator_id'])
                        )

                    stats['jobs_updated'] += 1
                    stats['storage_reclaimed'] += job['output_file_size'] or 0

                except Exception as e:
                    print(f"    ✗ Error updating job {job['job_id']}: {e}")
            else:
                stats['jobs_updated'] += 1
                stats['storage_reclaimed'] += job['output_file_size'] or 0

        print(f"  {'Would update' if dry_run else 'Updated'} {stats['jobs_updated']} job records")

    # Commit if not dry run
    if not dry_run:
        try:
            db.commit()
            print("\n  ✓ Changes committed to database")
        except Exception as e:
            db.rollback()
            print(f"\n  ✗ Error committing changes, rolled back: {e}")
            stats = {'files_deleted': 0, 'jobs_updated': 0, 'storage_reclaimed': 0}

    return stats


def generate_report(
    orphaned_files: list,
    orphaned_jobs: list,
    untracked_files: list = None,
    output_file: str = 'orphaned_files_report.txt'
) -> None:
    """Generate a detailed report of orphaned files."""
    with open(output_file, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("PHONOS ORPHANED FILES REPORT\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n")
        f.write("=" * 80 + "\n\n")

        # Orphaned database records
        f.write(f"ORPHANED DATABASE RECORDS (files): {len(orphaned_files)}\n")
        f.write("-" * 80 + "\n")

        total_size = sum(f['size'] for f in orphaned_files)
        f.write(f"Total size: {format_bytes(total_size)}\n\n")

        if orphaned_files:
            f.write("Details:\n")
            for file in orphaned_files[:100]:  # Limit to first 100
                f.write(f"  ID: {file['id']}\n")
                f.write(f"    Name: {file['name']}\n")
                f.write(f"    Path: {file['path']}\n")
                f.write(f"    Size: {format_bytes(file['size'])}\n")
                f.write(f"    Creator: {file['creator_id']}\n")
                f.write(f"    Project: {file['project_id']}\n")
                f.write(f"    Created: {file['created_at']}\n\n")

            if len(orphaned_files) > 100:
                f.write(f"  ... and {len(orphaned_files) - 100} more\n\n")

        # Orphaned job outputs
        f.write(f"\nORPHANED JOB OUTPUTS: {len(orphaned_jobs)}\n")
        f.write("-" * 80 + "\n")

        total_job_size = sum(j['output_file_size'] or 0 for j in orphaned_jobs)
        f.write(f"Total size: {format_bytes(total_job_size)}\n\n")

        if orphaned_jobs:
            f.write("Details:\n")
            for job in orphaned_jobs[:100]:
                f.write(f"  Job ID: {job['job_id']}\n")
                f.write(f"    Output: {job['output_file']}\n")
                f.write(f"    Size: {format_bytes(job['output_file_size'] or 0)}\n")
                f.write(f"    Creator: {job['creator_id']}\n")
                f.write(f"    Project: {job['project_id']}\n\n")

            if len(orphaned_jobs) > 100:
                f.write(f"  ... and {len(orphaned_jobs) - 100} more\n\n")

        # Untracked physical files
        if untracked_files is not None:
            f.write(f"\nUNTRACKED PHYSICAL FILES: {len(untracked_files)}\n")
            f.write("-" * 80 + "\n\n")

            if untracked_files:
                f.write("Sample (first 100):\n")
                for filepath in untracked_files[:100]:
                    try:
                        size = os.path.getsize(filepath)
                        f.write(f"  {filepath} ({format_bytes(size)})\n")
                    except Exception:
                        f.write(f"  {filepath} (size unknown)\n")

                if len(untracked_files) > 100:
                    f.write(f"\n  ... and {len(untracked_files) - 100} more\n")

    print(f"\n✓ Report saved to: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Cleanup orphaned files in Phonos database and filesystem',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=True,
        help='Preview changes without executing (default)'
    )

    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually execute cleanup (removes orphaned database records)'
    )

    parser.add_argument(
        '--check-untracked',
        action='store_true',
        help='Also check for untracked physical files (slow)'
    )

    parser.add_argument(
        '--report',
        action='store_true',
        help='Generate detailed report file'
    )

    parser.add_argument(
        '--report-file',
        default='orphaned_files_report.txt',
        help='Output file for report (default: orphaned_files_report.txt)'
    )

    args = parser.parse_args()

    # Override dry_run if --execute is specified
    dry_run = not args.execute

    print("=" * 80)
    print("PHONOS FILE SYSTEM INTEGRITY CHECK")
    print("=" * 80)
    print(f"Mode: {'DRY RUN (no changes will be made)' if dry_run else 'EXECUTE (will modify database)'}")
    print()

    # Connect to database
    try:
        db = get_db_connection()
        print("✓ Connected to database")
    except Exception as e:
        print(f"✗ Failed to connect to database: {e}")
        sys.exit(1)

    # Find orphaned records
    orphaned_files = find_orphaned_database_records(db)
    orphaned_jobs = find_orphaned_job_outputs(db)

    untracked_files = None
    if args.check_untracked:
        untracked_files = find_untracked_physical_files(db)

    # Generate report if requested
    if args.report:
        generate_report(orphaned_files, orphaned_jobs, untracked_files, args.report_file)

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Orphaned file records: {len(orphaned_files)}")
    print(f"Orphaned job outputs: {len(orphaned_jobs)}")
    if untracked_files is not None:
        print(f"Untracked physical files: {len(untracked_files)}")

    total_reclaimable = (
        sum(f['size'] for f in orphaned_files) +
        sum(j['output_file_size'] or 0 for j in orphaned_jobs)
    )
    print(f"Total reclaimable storage: {format_bytes(total_reclaimable)}")

    # Cleanup if there are orphaned records
    if orphaned_files or orphaned_jobs:
        if dry_run:
            print("\n⚠ This is a DRY RUN. No changes will be made.")
            print("  To actually clean these records, run with --execute flag")
        else:
            print("\n⚠ WARNING: You are about to delete orphaned database records!")
            response = input("  Continue? (yes/no): ")
            if response.lower() != 'yes':
                print("  Aborted.")
                sys.exit(0)

        stats = cleanup_orphaned_records(db, orphaned_files, orphaned_jobs, dry_run)

        print("\n" + "=" * 80)
        print("CLEANUP RESULTS")
        print("=" * 80)
        print(f"File records {'would be' if dry_run else ''} deleted: {stats['files_deleted']}")
        print(f"Job records {'would be' if dry_run else ''} updated: {stats['jobs_updated']}")
        print(f"Storage {'would be' if dry_run else ''} reclaimed: {format_bytes(stats['storage_reclaimed'])}")

    else:
        print("\n✓ No orphaned records found! File system is clean.")

    # Close database connection
    db.close()


if __name__ == "__main__":
    main()
