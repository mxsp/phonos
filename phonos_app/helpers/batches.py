"""Helper functions for batch jobs - organizing jobs within batches.

Batch jobs provide a Project-Batch-Jobs-JobDetail hierarchy:
- Project contains multiple batches
- Each batch contains multiple related jobs
- Each job has its own details and logs
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


# Batch status constants
BATCH_STATUS_PENDING = 'pending'      # Batch created but not started
BATCH_STATUS_RUNNING = 'running'      # Some jobs are processing
BATCH_STATUS_COMPLETED = 'completed'  # All jobs completed successfully
BATCH_STATUS_FAILED = 'failed'        # All jobs failed
BATCH_STATUS_CANCELLED = 'cancelled'  # User cancelled the batch
BATCH_STATUS_MIXED = 'mixed'          # Some succeeded, some failed


def create_batch(
    project_id: int,
    creator_id: int,
    name: str,
    description: str = None,
    metadata: Dict[str, Any] = None
) -> Dict[str, Any]:
    """Create a new batch.

    Args:
        project_id: Project ID to create batch in
        creator_id: User ID creating the batch
        name: Batch name
        description: Optional batch description
        metadata: Optional metadata for the batch

    Returns:
        Dict containing batch information
    """
    batch_id = f"batch_{int(time.time())}_{str(uuid.uuid4())[:8]}"

    try:
        db = get_db()
        cursor = db.execute(
            """INSERT INTO batches (batch_id, project_id, creator_id, name, description, metadata)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id, created_at, updated_at""",
            (batch_id, project_id, creator_id, name, description,
             json.dumps(metadata or {}))
        )

        result = cursor.fetchone()
        batch_db_id = result['id']
        created_at = result['created_at']
        updated_at = result['updated_at']

        db.commit()

        return {
            'id': batch_db_id,
            'batch_id': batch_id,
            'project_id': project_id,
            'creator_id': creator_id,
            'name': name,
            'description': description,
            'metadata': metadata or {},
            'status': BATCH_STATUS_PENDING,
            'total_jobs': 0,
            'completed_jobs': 0,
            'failed_jobs': 0,
            'cancelled_jobs': 0,
            'total_cost': 0.0,
            'created_at': created_at,
            'updated_at': updated_at
        }
    except Exception as e:
        logger.error(f"Error creating batch: {str(e)}", exc_info=True)
        raise


def get_batch(batch_id: int, include_jobs: bool = False) -> Optional[Dict[str, Any]]:
    """Get batch information by ID.

    Args:
        batch_id: Batch database ID
        include_jobs: Whether to include job details

    Returns:
        Dict containing batch information or None if not found
    """
    db = get_db()
    result = db.execute(
        """SELECT id, batch_id, project_id, creator_id, name, description,
                  status, total_jobs, completed_jobs, failed_jobs,
                  cancelled_jobs, total_cost, metadata, created_at, updated_at
           FROM batches WHERE id = %s""",
        (batch_id,)
    ).fetchone()

    if not result:
        return None

    batch = {
        'id': result['id'],
        'batch_id': result['batch_id'],
        'project_id': result['project_id'],
        'creator_id': result['creator_id'],
        'name': result['name'],
        'description': result['description'],
        'status': result['status'],
        'total_jobs': result['total_jobs'],
        'completed_jobs': result['completed_jobs'],
        'failed_jobs': result['failed_jobs'],
        'cancelled_jobs': result['cancelled_jobs'],
        'total_cost': float(result['total_cost']) if result['total_cost'] else 0.0,
        'metadata': json.loads(result['metadata']) if isinstance(result['metadata'], str) else (result['metadata'] or {}),
        'created_at': result['created_at'],
        'updated_at': result['updated_at']
    }

    if include_jobs:
        rows = db.execute(
            """SELECT id, job_id, name, model, status, message,
                      execution_time, cost, created_at, updated_at
               FROM jobs WHERE batch_id = %s
               ORDER BY created_at ASC""",
            (batch_id,)
        ).fetchall()

        jobs = []
        for row in rows:
            jobs.append({
                'id': row['id'],
                'job_id': row['job_id'],
                'name': row['name'],
                'model': row['model'],
                'status': row['status'],
                'message': row['message'],
                'execution_time': row['execution_time'],
                'cost': float(row['cost']) if row['cost'] else 0.0,
                'created_at': row['created_at'],
                'updated_at': row['updated_at']
            })

        batch['jobs'] = jobs

    return batch


def get_batches_for_project(
    project_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Get batches for a project.

    Args:
        project_id: Project ID
        status: Optional status filter
        limit: Maximum number of batches to return
        offset: Number of batches to skip

    Returns:
        List of batch dictionaries
    """
    db = get_db()

    query = """SELECT id, batch_id, project_id, creator_id, name, description,
                      status, total_jobs, completed_jobs, failed_jobs,
                      cancelled_jobs, total_cost, metadata, created_at, updated_at
               FROM batches WHERE project_id = %s"""
    params = [project_id]

    if status:
        query += " AND status = %s"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    rows = db.execute(query, params).fetchall()

    batches = []
    for result in rows:
        batches.append({
            'id': result['id'],
            'batch_id': result['batch_id'],
            'project_id': result['project_id'],
            'creator_id': result['creator_id'],
            'name': result['name'],
            'description': result['description'],
            'status': result['status'],
            'total_jobs': result['total_jobs'],
            'completed_jobs': result['completed_jobs'],
            'failed_jobs': result['failed_jobs'],
            'cancelled_jobs': result['cancelled_jobs'],
            'total_cost': float(result['total_cost']) if result['total_cost'] else 0.0,
            'metadata': json.loads(result['metadata']) if isinstance(result['metadata'], str) else (result['metadata'] or {}),
            'created_at': result['created_at'],
            'updated_at': result['updated_at']
        })

    return batches


def update_batch_status(
    batch_id: int,
    status: str
) -> bool:
    """Update batch status.

    Args:
        batch_id: Batch database ID
        status: New status

    Returns:
        True if successful, False otherwise
    """
    if status not in [BATCH_STATUS_PENDING, BATCH_STATUS_RUNNING, BATCH_STATUS_COMPLETED,
                     BATCH_STATUS_FAILED, BATCH_STATUS_CANCELLED, BATCH_STATUS_MIXED]:
        return False

    db = get_db()
    cursor = db.execute(
        "UPDATE batches SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (status, batch_id)
    )
    success = cursor.rowcount > 0
    db.commit()
    return success


def delete_batch(batch_id: int, cancel_jobs: bool = True) -> bool:
    """Delete a batch.

    Args:
        batch_id: Batch database ID
        cancel_jobs: Whether to cancel running jobs in the batch

    Returns:
        True if successful, False otherwise
    """
    db = get_db()

    if cancel_jobs:
        db.execute(
            """UPDATE jobs SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
               WHERE batch_id = %s AND status IN ('pending', 'running')""",
            (batch_id,)
        )

    cursor = db.execute("DELETE FROM batches WHERE id = %s", (batch_id,))
    success = cursor.rowcount > 0
    db.commit()
    return success


def get_batch_statistics(batch_id: int) -> Dict[str, Any]:
    """Get detailed statistics for a batch.

    Args:
        batch_id: Batch database ID

    Returns:
        Dict containing batch statistics
    """
    db = get_db()

    result = db.execute(
        """SELECT total_jobs, completed_jobs, failed_jobs,
                  cancelled_jobs, total_cost, status
           FROM batches WHERE id = %s""",
        (batch_id,)
    ).fetchone()

    if not result:
        return {}

    # Get job distribution by status
    status_rows = db.execute(
        "SELECT status, COUNT(*) as count FROM jobs WHERE batch_id = %s GROUP BY status",
        (batch_id,)
    ).fetchall()

    status_distribution = {row['status']: row['count'] for row in status_rows}

    # Get job distribution by model
    model_rows = db.execute(
        "SELECT model, COUNT(*) as count, SUM(cost) as total_cost FROM jobs WHERE batch_id = %s GROUP BY model",
        (batch_id,)
    ).fetchall()

    model_distribution = []
    for row in model_rows:
        model_distribution.append({
            'model': row['model'],
            'count': row['count'],
            'total_cost': float(row['total_cost']) if row['total_cost'] else 0.0
        })

    total_jobs = result['total_jobs']
    completed_jobs = result['completed_jobs']

    return {
        'total_jobs': total_jobs,
        'completed_jobs': completed_jobs,
        'failed_jobs': result['failed_jobs'],
        'cancelled_jobs': result['cancelled_jobs'],
        'total_cost': float(result['total_cost']) if result['total_cost'] else 0.0,
        'status': result['status'],
        'status_distribution': status_distribution,
        'model_distribution': model_distribution,
        'success_rate': (completed_jobs / total_jobs * 100) if total_jobs > 0 else 0
    }


def add_job_to_batch(
    batch_id: int,
    job_id: int
) -> bool:
    """Add an existing job to a batch.

    Args:
        batch_id: Batch database ID
        job_id: Job database ID

    Returns:
        True if successful, False otherwise
    """
    db = get_db()
    cursor = db.execute(
        """UPDATE jobs SET batch_id = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = %s AND batch_id IS NULL""",
        (batch_id, job_id)
    )
    success = cursor.rowcount > 0
    db.commit()
    return success


def get_batch_summary_dict(batch: Dict[str, Any]) -> Dict[str, Any]:
    """Get a summary dictionary of a batch for API responses.

    Args:
        batch: Batch dictionary

    Returns:
        Summary dictionary for API responses
    """
    return {
        'id': batch['id'],
        'batch_id': batch['batch_id'],
        'name': batch['name'],
        'description': batch['description'],
        'status': batch['status'],
        'total_jobs': batch['total_jobs'],
        'completed_jobs': batch['completed_jobs'],
        'failed_jobs': batch['failed_jobs'],
        'cancelled_jobs': batch['cancelled_jobs'],
        'total_cost': batch['total_cost'],
        'success_rate': (batch['completed_jobs'] / batch['total_jobs'] * 100) if batch['total_jobs'] > 0 else 0,
        'created_at': batch['created_at'],
        'updated_at': batch['updated_at']
    }
