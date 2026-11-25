#!/usr/bin/env python3
"""
Cleanup script for stuck Redis jobs and Dead Letter Queue.

This script performs two cleanup operations:
1. Identifies and deletes jobs from the ACTIVE_JOBS_HASH that have been running 
for more than a specified time (default 8 hours).
2. Removes failed jobs from the DEAD_LETTER_QUEUE that are older than a 
specified time (default 24 hours).

It can be run manually or as a scheduled cron job.

Usage:
    python3 cleanup_stuck_jobs.py [--dry-run] [--stuck-hours HOURS] [--dead-hours HOURS]

Options:
    --dry-run           Show what would be deleted without actually deleting
    --stuck-hours HOURS Timeout for active stuck jobs (default: 8)
    --dead-hours HOURS  Timeout for dead/failed jobs (default: 24)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add production directory to path
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))

from mrs_service.runtime import ACTIVE_JOBS_HASH, DEAD_LETTER_QUEUE, get_redis_connection, logger


def cleanup_stuck_jobs(timeout_hours: float = 8.0, dry_run: bool = False) -> dict[str, int]:
    """
    Remove stuck jobs from Redis that have been running longer than timeout_hours.

    Args:
        timeout_hours: Number of hours after which a job is considered stuck
        dry_run: If True, only report what would be deleted without deleting

    Returns:
        Dictionary with statistics: deleted, errors, total_active
    """
    timeout_seconds = timeout_hours * 3600
    current_time = time.time()

    r = get_redis_connection()

    stats = {
        "deleted": 0,
        "errors": 0,
        "total_active": 0,
        "stuck_jobs": []
    }

    try:
        # Get all active jobs
        active_jobs = r.hgetall(ACTIVE_JOBS_HASH)
        stats["total_active"] = len(active_jobs)

        logger.info(
            "Starting active job cleanup - Found %d active jobs (timeout: %.1f hours)",
            stats["total_active"],
            timeout_hours
        )

        for job_id, job_info_json in active_jobs.items():
            try:
                job_info = json.loads(job_info_json)
                start_time = job_info.get("start_time", 0)

                # Calculate how long the job has been running
                elapsed_seconds = current_time - start_time
                elapsed_hours = elapsed_seconds / 3600

                # Check if job has exceeded timeout
                if elapsed_seconds > timeout_seconds:
                    job_data = {}
                    try:
                        job_data_str = job_info.get("job_data", "{}")
                        job_data = json.loads(job_data_str) if isinstance(job_data_str, str) else job_data_str
                    except (json.JSONDecodeError, TypeError):
                        pass

                    stuck_job_info = {
                        "job_id": job_id,
                        "elapsed_hours": round(elapsed_hours, 2),
                        "model": job_data.get("model", "unknown"),
                        "status": job_info.get("status", "unknown"),
                        "start_time": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()
                    }

                    stats["stuck_jobs"].append(stuck_job_info)

                    if dry_run:
                        logger.info(
                            "[DRY RUN] Would delete stuck job %s (running for %.2f hours, model: %s)",
                            job_id,
                            elapsed_hours,
                            stuck_job_info["model"]
                        )
                    else:
                        # Delete the stuck job from active jobs hash
                        r.hdel(ACTIVE_JOBS_HASH, job_id)
                        stats["deleted"] += 1

                        logger.warning(
                            "Deleted stuck job %s (running for %.2f hours, model: %s)",
                            job_id,
                            elapsed_hours,
                            stuck_job_info["model"]
                        )

            except json.JSONDecodeError as e:
                logger.error("Failed to parse job info for %s: %s", job_id, e)
                stats["errors"] += 1
            except Exception as e:
                logger.error("Error processing job %s: %s", job_id, e)
                stats["errors"] += 1

    except Exception as e:
        logger.error("Fatal error during cleanup: %s", e, exc_info=True)
        raise

    return stats

def cleanup_dead_jobs(timeout_hours: float = 24.0, dry_run: bool = False) -> dict:
    """
    Remove failed jobs from DEAD_LETTER_QUEUE that are older than timeout_hours.

    Args:
        timeout_hours: Number of hours after which a dead job is removed
        dry_run: If True, only report what would be deleted

    Returns:
        Dictionary with statistics
    """
    timeout_seconds = timeout_hours * 3600
    current_time = time.time()
    r = get_redis_connection()

    stats = {
        "deleted": 0,
        "errors": 0,
        "total_dead": 0,
        "removed_jobs": []
    }

    try:
        # Get all dead jobs
        dead_jobs = r.lrange(DEAD_LETTER_QUEUE, 0, -1)
        stats["total_dead"] = len(dead_jobs)

        logger.info(
            "Starting dead job cleanup - Found %d dead jobs (timeout: %.1f hours)",
            stats["total_dead"],
            timeout_hours
        )

        for job_json_str in dead_jobs:
            try:
                job_info = json.loads(job_json_str)
                failed_at = job_info.get("failed_at", 0)
                
                # Check timestamp validity
                if not isinstance(failed_at, (int, float)):
                    failed_at = 0
                
                elapsed_seconds = current_time - failed_at
                elapsed_hours = elapsed_seconds / 3600

                if elapsed_seconds > timeout_seconds:
                    job_id = job_info.get("job_id", "unknown")
                    final_error = job_info.get("final_error", "unknown")
                    
                    removed_job_info = {
                        "job_id": job_id,
                        "elapsed_hours": round(elapsed_hours, 2),
                        "error": final_error,
                        "failed_at": datetime.fromtimestamp(failed_at, tz=timezone.utc).isoformat() if failed_at else "unknown"
                    }
                    stats["removed_jobs"].append(removed_job_info)

                    if dry_run:
                        logger.info(
                            "[DRY RUN] Would remove dead job %s (failed %.2f hours ago: %s)",
                            job_id, elapsed_hours, final_error
                        )
                    else:
                        # Remove from list. LREM removes elements equal to value.
                        # count=1 removes first occurrence.
                        r.lrem(DEAD_LETTER_QUEUE, 1, job_json_str)
                        stats["deleted"] += 1
                        logger.warning(
                            "Removed dead job %s (failed %.2f hours ago)",
                            job_id, elapsed_hours
                        )
                        
            except json.JSONDecodeError:
                stats["errors"] += 1
            except Exception as e:
                logger.error("Error processing dead job: %s", e)
                stats["errors"] += 1

    except Exception as e:
        logger.error("Fatal error during dead job cleanup: %s", e, exc_info=True)
        raise

    return stats

def main():
    """Main entry point for the cleanup script."""
    parser = argparse.ArgumentParser(
        description="Clean up stuck Redis jobs and old Dead Letter Queue entries"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting"
    )
    parser.add_argument(
        "--hours", # Backwards compatibility
        dest="stuck_hours",
        type=float,
        default=8.0,
        help="Deprecated: Use --stuck-hours instead"
    )
    parser.add_argument(
        "--stuck-hours",
        type=float,
        default=8.0,
        help="Number of hours after which an ACTIVE job is considered stuck (default: 8)"
    )
    parser.add_argument(
        "--dead-hours",
        type=float,
        default=24.0,
        help="Number of hours after which a FAILED job is removed from Dead Letter Queue (default: 24)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format"
    )

    args = parser.parse_args()
    
    # Handle the case where user might use the old --hours flag or the new --stuck-hours
    # If --hours is explicitly provided (and differs from default or we just use it), it sets stuck_hours.
    # The 'dest="stuck_hours"' in the --hours argument definition maps it directly.
    # However, if both are provided, the last one wins.
    # Since we set default=8.0 for both, it's fine.

    try:
        stuck_stats = cleanup_stuck_jobs(timeout_hours=args.stuck_hours, dry_run=args.dry_run)
        dead_stats = cleanup_dead_jobs(timeout_hours=args.dead_hours, dry_run=args.dry_run)

        combined_stats = {
            "stuck_jobs": stuck_stats,
            "dead_jobs": dead_stats
        }

        if args.json:
            print(json.dumps(combined_stats, indent=2))
        else:
            print("\n" + "="*60)
            print("CLEANUP SUMMARY")
            print("="*60)
            
            print(f"--- Active Jobs (Timeout: {args.stuck_hours}h) ---")
            print(f"Total active jobs:     {stuck_stats['total_active']}")
            print(f"Stuck jobs found:      {len(stuck_stats['stuck_jobs'])}")
            if args.dry_run:
                print(f"Would delete:          {len(stuck_stats['stuck_jobs'])}")
            else:
                print(f"Jobs deleted:          {stuck_stats['deleted']}")
            print(f"Errors:                {stuck_stats['errors']}")
            
            print(f"\n--- Dead Jobs (Timeout: {args.dead_hours}h) ---")
            print(f"Total dead jobs:       {dead_stats['total_dead']}")
            print(f"Old dead jobs found:   {len(dead_stats['removed_jobs'])}")
            if args.dry_run:
                print(f"Would remove:          {len(dead_stats['removed_jobs'])}")
            else:
                print(f"Jobs removed:          {dead_stats['deleted']}")
            print(f"Errors:                {dead_stats['errors']}")
            
            print("="*60)

            if stuck_stats['stuck_jobs']:
                print("\nStuck Active Jobs Details:")
                for job in stuck_stats['stuck_jobs']:
                    print(f"  - Job ID: {job['job_id']}")
                    print(f"    Model: {job['model']}, Status: {job['status']}")
                    print(f"    Running for: {job['elapsed_hours']} hours")
                    print()
            
            if dead_stats['removed_jobs']:
                print("\nRemoved Dead Jobs Details:")
                for job in dead_stats['removed_jobs']:
                    print(f"  - Job ID: {job['job_id']}")
                    print(f"    Failed: {job['elapsed_hours']} hours ago")
                    print(f"    Error: {job['error']}")
                    print()

        # Exit with error code if there were errors
        total_errors = stuck_stats['errors'] + dead_stats['errors']
        sys.exit(1 if total_errors > 0 else 0)

    except KeyboardInterrupt:
        logger.info("Cleanup interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error("Cleanup failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()