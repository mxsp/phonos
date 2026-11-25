"""Helper functions for job bunches - grouping multiple jobs together.

Job bunches allow users to:
- Submit multiple sequences/files as a single batch
- Track progress of the entire batch
- Get aggregated statistics
- Manage all jobs in the bunch together
"""
from __future__ import annotations

import json
import uuid
import time
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .database import get_db

logger = logging.getLogger(__name__)


def generate_bunch_id() -> str:
    """Generate a bunch ID in the format bunch_<timestamp>_<random>."""
    import time
    import random
    import string
    ts = int(time.time())
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"bunch_{ts}_{rand}"


def get_redis_connection():
    """Lazy import to avoid circular dependency."""
    from phonos_app.app import get_redis_connection as _get_redis
    return _get_redis()


# Bunch status constants
BUNCH_STATUS_PENDING = 'pending'      # Bunch created but not started
BUNCH_STATUS_RUNNING = 'running'      # Some jobs are processing
BUNCH_STATUS_COMPLETED = 'completed'  # All jobs completed successfully
BUNCH_STATUS_FAILED = 'failed'        # All jobs failed
BUNCH_STATUS_MIXED = 'mixed'          # Some succeeded, some failed
BUNCH_STATUS_CANCELLED = 'cancelled'  # User cancelled the bunch


def create_bunch(
    project_id: int,
    creator_id: int,
    name: str,
    model: str,
    items: List[Dict[str, Any]],
    description: str = None,
    priority: int = 2
) -> Dict[str, Any]:
    """Create a new job bunch with items.

    Args:
        project_id: Project ID to create bunch in
        creator_id: User ID creating the bunch
        name: Name for the bunch
        model: Model type for all jobs in bunch
        items: List of items, each with 'name', 'sequence' or 'file_id'
        description: Optional description
        priority: Priority level (0-3, default 2=STANDARD)

    Returns:
        Dict with bunch details including bunch_id
    """
    db = get_db()
    bunch_id = generate_bunch_id()

    try:
        # Create the bunch record
        result = db.execute("""
            INSERT INTO job_bunches
            (bunch_id, project_id, creator_id, name, description, model, status, total_jobs, priority, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING id
        """, (bunch_id, project_id, creator_id, name, description, model, BUNCH_STATUS_PENDING, len(items), priority))

        bunch_db_id = result.fetchone()['id']

        # Create bunch items
        for idx, item in enumerate(items):
            item_name = item.get('name', f"Item {idx + 1}")
            sequence = item.get('sequence')
            file_id = item.get('file_id')

            db.execute("""
                INSERT INTO job_bunch_items
                (bunch_id, sequence_order, input_name, input_sequence, input_file_id, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (bunch_db_id, idx, item_name, sequence, file_id, 'pending'))

        db.commit()

        logger.info("Created job bunch %s with %d items for project %d", bunch_id, len(items), project_id)

        return {
            'status': 'success',
            'bunch_id': bunch_id,
            'id': bunch_db_id,
            'name': name,
            'model': model,
            'total_items': len(items),
            'message': f'Bunch created with {len(items)} items'
        }

    except Exception as e:
        db.rollback()
        logger.error("Failed to create job bunch: %s", e, exc_info=True)
        return {
            'status': 'error',
            'message': f'Failed to create bunch: {str(e)}'
        }


def get_bunch(bunch_id: str) -> Optional[Dict[str, Any]]:
    """Get bunch details by bunch_id."""
    db = get_db()

    result = db.execute("""
        SELECT jb.*, u.username as creator_username, p.name as project_name
        FROM job_bunches jb
        JOIN users u ON jb.creator_id = u.id
        JOIN projects p ON jb.project_id = p.id
        WHERE jb.bunch_id = %s
    """, (bunch_id,))

    row = result.fetchone()
    if not row:
        return None

    return _serialize_bunch(row)


def get_bunch_by_id(bunch_db_id: int) -> Optional[Dict[str, Any]]:
    """Get bunch details by database ID."""
    db = get_db()

    result = db.execute("""
        SELECT jb.*, u.username as creator_username, p.name as project_name
        FROM job_bunches jb
        JOIN users u ON jb.creator_id = u.id
        JOIN projects p ON jb.project_id = p.id
        WHERE jb.id = %s
    """, (bunch_db_id,))

    row = result.fetchone()
    if not row:
        return None

    return _serialize_bunch(row)


def get_project_bunches(project_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Get all bunches for a project."""
    db = get_db()

    result = db.execute("""
        SELECT jb.*, u.username as creator_username
        FROM job_bunches jb
        JOIN users u ON jb.creator_id = u.id
        WHERE jb.project_id = %s
        ORDER BY jb.created_at DESC
        LIMIT %s OFFSET %s
    """, (project_id, limit, offset))

    bunches = []
    for row in result.fetchall():
        bunches.append(_serialize_bunch(row))

    return bunches


def get_bunch_items(bunch_db_id: int) -> List[Dict[str, Any]]:
    """Get all items in a bunch."""
    db = get_db()

    result = db.execute("""
        SELECT jbi.*, j.job_id as job_uuid, j.status as job_status,
               j.output_file, j.execution_time, j.message as job_message
        FROM job_bunch_items jbi
        LEFT JOIN jobs j ON jbi.job_id = j.id
        WHERE jbi.bunch_id = %s
        ORDER BY jbi.sequence_order
    """, (bunch_db_id,))

    items = []
    for row in result.fetchall():
        items.append({
            'id': row['id'],
            'sequence_order': row['sequence_order'],
            'input_name': row['input_name'],
            'input_sequence': row['input_sequence'][:100] + '...' if row['input_sequence'] and len(row['input_sequence']) > 100 else row['input_sequence'],
            'input_file_id': row['input_file_id'],
            'status': row['status'],
            'error_message': row['error_message'],
            'job_id': row['job_id'],
            'job_uuid': row['job_uuid'],
            'job_status': row['job_status'],
            'output_file': row['output_file'],
            'execution_time': row['execution_time'],
            'job_message': row['job_message'],
            'created_at': str(row['created_at']) if row['created_at'] else None
        })

    return items


def start_bunch(bunch_id: str, user_id: int) -> Dict[str, Any]:
    """Start processing a bunch - submit all jobs.

    This will:
    1. Check user has enough credits
    2. Create job records for each item
    3. Queue jobs to Redis
    4. Update bunch status to running

    Args:
        bunch_id: The bunch UUID
        user_id: User starting the bunch

    Returns:
        Dict with status and submitted job count
    """
    from production.config import Config
    from production.mrs_service.runtime import NEW_JOBS_QUEUE, JobPriority

    db = get_db()

    # Get bunch details
    result = db.execute("""
        SELECT jb.*, p.name as project_name
        FROM job_bunches jb
        JOIN projects p ON jb.project_id = p.id
        WHERE jb.bunch_id = %s
    """, (bunch_id,))

    bunch = result.fetchone()
    if not bunch:
        return {'status': 'error', 'message': 'Bunch not found'}

    if bunch['status'] != BUNCH_STATUS_PENDING:
        return {'status': 'error', 'message': f'Bunch already {bunch["status"]}'}

    # Check permissions (creator only for now)
    if bunch['creator_id'] != user_id:
        return {'status': 'error', 'message': 'Only the creator can start this bunch'}

    bunch_db_id = bunch['id']
    project_id = bunch['project_id']
    model_type = bunch['model']
    priority = bunch['priority']

    # Get model info
    model_info = Config.get_model_definition(model_type)
    if not model_info:
        return {'status': 'error', 'message': f'Unknown model: {model_type}'}

    cost_per_job = 20

    # Get pending items
    items_result = db.execute("""
        SELECT * FROM job_bunch_items
        WHERE bunch_id = %s AND status = 'pending'
        ORDER BY sequence_order
    """, (bunch_db_id,))

    items = items_result.fetchall()
    if not items:
        return {'status': 'error', 'message': 'No pending items in bunch'}

    total_cost = cost_per_job * len(items)

    # Check user credits
    user_result = db.execute("SELECT credits FROM users WHERE id = %s", (user_id,))
    user = user_result.fetchone()
    if not user or user['credits'] < total_cost:
        return {
            'status': 'error',
            'message': f'Insufficient credits. Need {total_cost}, have {user["credits"] if user else 0}'
        }

    # Deduct credits upfront
    db.execute("UPDATE users SET credits = credits - %s WHERE id = %s", (total_cost, user_id))

    # Update bunch status
    db.execute("""
        UPDATE job_bunches
        SET status = %s, started_at = CURRENT_TIMESTAMP, total_cost = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (BUNCH_STATUS_RUNNING, total_cost, bunch_db_id))

    # Create and queue jobs for each item
    r = get_redis_connection()
    submitted_jobs = []
    failed_items = []

    for item in items:
        try:
            job_id = str(uuid.uuid4())

            # Determine input
            sequence = item['input_sequence']
            input_file_id = item['input_file_id']

            if not sequence and input_file_id:
                # Load sequence from file if needed
                file_result = db.execute("SELECT path FROM files WHERE id = %s", (input_file_id,))
                file_row = file_result.fetchone()
                if file_row:
                    try:
                        with open(file_row['path'], 'r') as f:
                            sequence = f.read().strip()
                    except Exception as e:
                        logger.warning("Failed to read file for bunch item: %s", e)

            if not sequence:
                # Mark item as failed
                db.execute("""
                    UPDATE job_bunch_items
                    SET status = 'failed', error_message = 'No sequence provided'
                    WHERE id = %s
                """, (item['id'],))
                failed_items.append(item['id'])
                continue

            # Create job name
            job_name = f"{bunch['name']} - {item['input_name']}"

            # Create job record
            db.execute("""
                INSERT INTO jobs
                (job_id, project_id, creator_id, name, model, sequence, status, cost, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, CURRENT_TIMESTAMP)
                RETURNING id
            """, (job_id, project_id, user_id, job_name, model_type, sequence, cost_per_job))

            job_db_id = db.execute("SELECT id FROM jobs WHERE job_id = %s", (job_id,)).fetchone()['id']

            # Link job to bunch item
            db.execute("""
                UPDATE job_bunch_items
                SET job_id = %s, status = 'submitted'
                WHERE id = %s
            """, (job_db_id, item['id']))

            # Build job payload
            job_payload = {
                'job_id': job_id,
                'model': model_type,
                'task': model_info.get('task', 'protein_folding'),
                'params': {'sequence': sequence},
                'bunch_id': bunch_id,
                'priority': priority,
                'submitted_at': time.time()
            }

            # Queue to Redis
            r.lpush(NEW_JOBS_QUEUE, json.dumps(job_payload))
            submitted_jobs.append(job_id)

        except Exception as e:
            logger.error("Failed to submit job for bunch item %d: %s", item['id'], e)
            db.execute("""
                UPDATE job_bunch_items
                SET status = 'failed', error_message = %s
                WHERE id = %s
            """, (str(e), item['id']))
            failed_items.append(item['id'])

    db.commit()

    logger.info("Started bunch %s: %d jobs submitted, %d failed", bunch_id, len(submitted_jobs), len(failed_items))

    return {
        'status': 'success',
        'bunch_id': bunch_id,
        'submitted': len(submitted_jobs),
        'failed': len(failed_items),
        'total_cost': total_cost,
        'message': f'Started {len(submitted_jobs)} jobs'
    }


def update_bunch_status(bunch_db_id: int = None, bunch_id: int = None) -> str:
    """Update bunch status based on job statuses.

    Called when a job in the bunch completes or fails.
    Accepts either bunch_db_id (preferred) or bunch_id for test/backcompat.
    Returns the new bunch status.
    """
    if bunch_db_id is None:
        bunch_db_id = bunch_id
    if bunch_db_id is None:
        raise ValueError("bunch_db_id or bunch_id is required")

    db = get_db()

    # Count job statuses
    result = db.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(CASE WHEN jbi.status = 'completed' THEN 1 END) as completed,
            COUNT(CASE WHEN jbi.status = 'failed' THEN 1 END) as failed,
            COUNT(CASE WHEN jbi.status IN ('pending', 'submitted', 'running') THEN 1 END) as in_progress
        FROM job_bunch_items jbi
        WHERE jbi.bunch_id = %s
    """, (bunch_db_id,))

    counts = result.fetchone()
    total = counts['total']
    completed = counts['completed']
    failed = counts['failed']
    in_progress = counts['in_progress']

    # Determine new status
    if in_progress > 0:
        new_status = BUNCH_STATUS_RUNNING
        completed_at = None
    elif completed == total:
        new_status = BUNCH_STATUS_COMPLETED
        completed_at = 'CURRENT_TIMESTAMP'
    elif failed == total:
        new_status = BUNCH_STATUS_FAILED
        completed_at = 'CURRENT_TIMESTAMP'
    else:
        new_status = BUNCH_STATUS_MIXED
        completed_at = 'CURRENT_TIMESTAMP'

    # Update bunch
    if completed_at:
        db.execute("""
            UPDATE job_bunches
            SET status = %s, completed_jobs = %s, failed_jobs = %s,
                completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (new_status, completed, failed, bunch_db_id))
    else:
        db.execute("""
            UPDATE job_bunches
            SET status = %s, completed_jobs = %s, failed_jobs = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (new_status, completed, failed, bunch_db_id))

    db.commit()

    return new_status


def update_bunch_item_from_job(job_id: str, status: str, error_message: str = None) -> Optional[int]:
    """Update bunch item status when a job completes.

    Args:
        job_id: The job UUID
        status: New status ('completed' or 'failed')
        error_message: Optional error message for failures

    Returns:
        bunch_db_id if this job belongs to a bunch, None otherwise
    """
    db = get_db()

    # Find the bunch item for this job
    result = db.execute("""
        SELECT jbi.id, jbi.bunch_id
        FROM job_bunch_items jbi
        JOIN jobs j ON jbi.job_id = j.id
        WHERE j.job_id = %s
    """, (job_id,))

    row = result.fetchone()
    if not row:
        return None

    item_id = row['id']
    bunch_db_id = row['bunch_id']

    # Update the item status
    db.execute("""
        UPDATE job_bunch_items
        SET status = %s, error_message = %s
        WHERE id = %s
    """, (status, error_message, item_id))

    db.commit()

    # Update the bunch status
    update_bunch_status(bunch_db_id)

    return bunch_db_id


def cancel_bunch(bunch_id: str, user_id: int) -> Dict[str, Any]:
    """Cancel a bunch and all its pending jobs.

    Args:
        bunch_id: The bunch UUID
        user_id: User requesting cancellation

    Returns:
        Dict with status and refund amount
    """
    db = get_db()

    # Get bunch
    result = db.execute("""
        SELECT * FROM job_bunches WHERE bunch_id = %s
    """, (bunch_id,))

    bunch = result.fetchone()
    if not bunch:
        return {'status': 'error', 'message': 'Bunch not found'}

    # Check permissions
    if bunch['creator_id'] != user_id:
        return {'status': 'error', 'message': 'Only the creator can cancel this bunch'}

    if bunch['status'] in (BUNCH_STATUS_COMPLETED, BUNCH_STATUS_CANCELLED):
        return {'status': 'error', 'message': f'Bunch already {bunch["status"]}'}

    bunch_db_id = bunch['id']

    # Count pending items for refund calculation
    from production.config import Config
    model_info = Config.get_model_definition(bunch['model'])
    cost_per_job = 20

    pending_result = db.execute("""
        SELECT COUNT(*) as count FROM job_bunch_items
        WHERE bunch_id = %s AND status IN ('pending', 'submitted')
    """, (bunch_db_id,))

    pending_count = pending_result.fetchone()['count']
    refund_amount = pending_count * cost_per_job

    # Mark pending items as cancelled
    db.execute("""
        UPDATE job_bunch_items
        SET status = 'cancelled'
        WHERE bunch_id = %s AND status IN ('pending', 'submitted')
    """, (bunch_db_id,))

    # Update bunch status
    db.execute("""
        UPDATE job_bunches
        SET status = %s, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (BUNCH_STATUS_CANCELLED, bunch_db_id))

    # Refund credits
    if refund_amount > 0:
        db.execute("""
            UPDATE users SET credits = credits + %s WHERE id = %s
        """, (refund_amount, user_id))

    db.commit()

    logger.info("Cancelled bunch %s, refunded %d credits", bunch_id, refund_amount)

    return {
        'status': 'success',
        'bunch_id': bunch_id,
        'cancelled_jobs': pending_count,
        'refund_amount': refund_amount,
        'message': f'Cancelled {pending_count} pending jobs, refunded {refund_amount} credits'
    }


def delete_bunch(bunch_id: str, user_id: int) -> Dict[str, Any]:
    """Delete a bunch and all its data.

    Only allowed for bunches in 'pending' or 'cancelled' status.

    Args:
        bunch_id: The bunch UUID
        user_id: User requesting deletion

    Returns:
        Dict with status
    """
    db = get_db()

    result = db.execute("""
        SELECT * FROM job_bunches WHERE bunch_id = %s
    """, (bunch_id,))

    bunch = result.fetchone()
    if not bunch:
        return {'status': 'error', 'message': 'Bunch not found'}

    if bunch['creator_id'] != user_id:
        return {'status': 'error', 'message': 'Only the creator can delete this bunch'}

    if bunch['status'] not in (BUNCH_STATUS_PENDING, BUNCH_STATUS_CANCELLED):
        return {'status': 'error', 'message': 'Can only delete pending or cancelled bunches'}

    # Delete bunch (items will cascade)
    db.execute("DELETE FROM job_bunches WHERE bunch_id = %s", (bunch_id,))
    db.commit()

    logger.info("Deleted bunch %s", bunch_id)

    return {'status': 'success', 'message': 'Bunch deleted'}


def _serialize_bunch(row) -> Dict[str, Any]:
    """Serialize a bunch database row to dict."""
    return {
        'id': row['id'],
        'bunch_id': row['bunch_id'],
        'project_id': row['project_id'],
        'project_name': row.get('project_name'),
        'creator_id': row['creator_id'],
        'creator_username': row.get('creator_username'),
        'name': row['name'],
        'description': row['description'],
        'model': row['model'],
        'status': row['status'],
        'total_jobs': row['total_jobs'],
        'completed_jobs': row['completed_jobs'],
        'failed_jobs': row['failed_jobs'],
        'total_cost': row['total_cost'],
        'priority': row['priority'],
        'created_at': str(row['created_at']) if row['created_at'] else None,
        'updated_at': str(row['updated_at']) if row['updated_at'] else None,
        'started_at': str(row['started_at']) if row['started_at'] else None,
        'completed_at': str(row['completed_at']) if row['completed_at'] else None,
        'progress_percent': round(
            (row['completed_jobs'] + row['failed_jobs']) / max(row['total_jobs'], 1) * 100, 1
        )
    }
