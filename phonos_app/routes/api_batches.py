"""API routes for batch operations - Project-Batch-Jobs-JobDetail hierarchy."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import (
    abort,
    g,
    jsonify,
    request,
    session
)
from werkzeug.exceptions import HTTPException

from phonos_app.helpers.batches import (
    BATCH_STATUS_PENDING,
    BATCH_STATUS_RUNNING,
    BATCH_STATUS_COMPLETED,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_MIXED,
    create_batch,
    get_batch,
    get_batches_for_project,
    update_batch_status,
    delete_batch,
    get_batch_statistics,
    add_job_to_batch,
    get_batch_summary_dict
)
from phonos_app.helpers.permissions import (
    check_project_access,
    check_project_manager,
    require_project_access
)
from phonos_app.helpers.database import get_db

logger = logging.getLogger(__name__)


@require_project_access
def create_project_batch(project_id: int):
    """Create a new batch in a project."""
    try:
        data = request.get_json()
        if not data:
            abort(400, "No data provided")

        name = data.get('name')
        if not name or not name.strip():
            abort(400, "Batch name is required")

        description = data.get('description', '')
        metadata = data.get('metadata', {})

        batch = create_batch(
            project_id=project_id,
            creator_id=session['user_id'],
            name=name.strip(),
            description=description.strip() if description else None,
            metadata=metadata
        )

        return jsonify({
            'success': True,
            'batch': get_batch_summary_dict(batch)
        }), 201

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating batch in project {project_id}: {str(e)}", exc_info=True)
        abort(500, f"Failed to create batch: {str(e)}")


@require_project_access
def get_project_batches(project_id: int):
    """Get all batches for a project."""
    try:
        status = request.args.get('status')
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = max(int(request.args.get('offset', 0)), 0)

        batches = get_batches_for_project(
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset
        )

        # Get total count for pagination
        db = get_db()
        query = "SELECT COUNT(*) as cnt FROM batches WHERE project_id = %s"
        params = [project_id]

        if status:
            query += " AND status = %s"
            params.append(status)

        row = db.execute(query, params).fetchone()
        total_count = row['cnt'] if row else 0

        return jsonify({
            'success': True,
            'batches': [get_batch_summary_dict(batch) for batch in batches],
            'pagination': {
                'total': total_count,
                'limit': limit,
                'offset': offset,
                'has_more': offset + len(batches) < total_count
            }
        })

    except ValueError as e:
        abort(400, "Invalid limit or offset parameter")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting batches for project {project_id}: {str(e)}")
        abort(500, "Failed to get batches")


def get_batch_details(batch_id: int):
    """Get detailed information about a specific batch."""
    try:
        include_jobs = request.args.get('include_jobs', 'false').lower() == 'true'

        batch = get_batch(batch_id, include_jobs=include_jobs)
        if not batch:
            abort(404, "Batch not found")

        # Check if user has access to the project
        db = get_db()
        project_result = db.execute(
            """SELECT p.id FROM projects p
               JOIN batches b ON b.project_id = p.id
               WHERE b.id = %s""",
            (batch_id,)
        ).fetchone()

        if not project_result:
            abort(404, "Project not found")

        project_id = project_result['id']

        # Verify project access
        from phonos_app.helpers.permissions import check_project_access
        if not check_project_access(project_id):
            abort(403, "Access denied")

        response_data = {
            'success': True,
            'batch': batch
        }

        if include_jobs:
            response_data['statistics'] = get_batch_statistics(batch_id)

        return jsonify(response_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting batch details for {batch_id}: {str(e)}")
        abort(500, "Failed to get batch details")


def update_batch(batch_id: int):
    """Update batch information."""
    try:
        batch = get_batch(batch_id)
        if not batch:
            abort(404, "Batch not found")

        # Check project access and manager permissions
        from phonos_app.helpers.permissions import check_project_manager
        if not check_project_manager(batch['project_id']):
            abort(403, "Manager access required")

        data = request.get_json()
        if not data:
            abort(400, "No data provided")

        updates = []
        params = []

        if 'name' in data:
            if not data['name'] or not data['name'].strip():
                abort(400, "Batch name cannot be empty")
            updates.append("name = %s")
            params.append(data['name'].strip())

        if 'description' in data:
            description = data['description'].strip() if data['description'] else None
            updates.append("description = %s")
            params.append(description)

        if 'metadata' in data:
            updates.append("metadata = %s")
            params.append(json.dumps(data['metadata']))

        if 'status' in data:
            status = data['status']
            valid_statuses = [
                BATCH_STATUS_PENDING, BATCH_STATUS_RUNNING, BATCH_STATUS_COMPLETED,
                BATCH_STATUS_FAILED, BATCH_STATUS_CANCELLED, BATCH_STATUS_MIXED
            ]
            if status not in valid_statuses:
                abort(400, f"Invalid status. Must be one of: {', '.join(valid_statuses)}")
            updates.append("status = %s")
            params.append(status)

        if not updates:
            abort(400, "No valid fields to update")

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(batch_id)

        db = get_db()
        query = f"UPDATE batches SET {', '.join(updates)} WHERE id = %s"
        cursor = db.execute(query, params)

        if cursor.rowcount == 0:
            abort(404, "Batch not found")

        db.commit()

        # Return updated batch
        updated_batch = get_batch(batch_id)
        return jsonify({
            'success': True,
            'batch': get_batch_summary_dict(updated_batch)
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating batch {batch_id}: {str(e)}")
        abort(500, "Failed to update batch")


def delete_batch_endpoint(batch_id: int):
    """Delete a batch."""
    try:
        batch = get_batch(batch_id)
        if not batch:
            abort(404, "Batch not found")

        # Check project manager permissions
        from phonos_app.helpers.permissions import check_project_manager
        if not check_project_manager(batch['project_id']):
            abort(403, "Manager access required")

        cancel_jobs = request.args.get('cancel_jobs', 'true').lower() == 'true'
        success = delete_batch(batch_id, cancel_jobs=cancel_jobs)

        if not success:
            abort(404, "Batch not found")

        return jsonify({
            'success': True,
            'message': 'Batch deleted successfully'
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting batch {batch_id}: {str(e)}")
        abort(500, "Failed to delete batch")


def add_job_to_batch_endpoint(batch_id: int, job_id: int):
    """Add an existing job to a batch."""
    try:
        # Verify batch exists and user has access
        batch = get_batch(batch_id)
        if not batch:
            abort(404, "Batch not found")

        from phonos_app.helpers.permissions import check_project_manager
        if not check_project_manager(batch['project_id']):
            abort(403, "Manager access required")

        # Verify job exists and belongs to the same project
        db = get_db()
        job_result = db.execute(
            "SELECT id, project_id FROM jobs WHERE id = %s",
            (job_id,)
        ).fetchone()

        if not job_result:
            abort(404, "Job not found")

        if job_result['project_id'] != batch['project_id']:
            abort(400, "Job must belong to the same project as the batch")

        success = add_job_to_batch(batch_id, job_id)

        if not success:
            abort(400, "Failed to add job to batch. Job may already be in a batch.")

        return jsonify({
            'success': True,
            'message': 'Job added to batch successfully'
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding job {job_id} to batch {batch_id}: {str(e)}")
        abort(500, "Failed to add job to batch")


def get_batch_statistics_endpoint(batch_id: int):
    """Get detailed statistics for a batch."""
    try:
        batch = get_batch(batch_id)
        if not batch:
            abort(404, "Batch not found")

        # Check project access
        from phonos_app.helpers.permissions import check_project_access
        if not check_project_access(batch['project_id']):
            abort(403, "Access denied")

        statistics = get_batch_statistics(batch_id)

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'statistics': statistics
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting statistics for batch {batch_id}: {str(e)}")
        abort(500, "Failed to get batch statistics")


def register_batch_routes(app):
    """Register batch API routes with the Flask app."""

    # Project batch routes
    app.add_url_rule(
        '/api/projects/<int:project_id>/batches',
        'create_project_batch',
        create_project_batch,
        methods=['POST']
    )

    app.add_url_rule(
        '/api/projects/<int:project_id>/batches',
        'get_project_batches',
        get_project_batches,
        methods=['GET']
    )

    # Individual batch routes
    app.add_url_rule(
        '/api/batches/<int:batch_id>',
        'get_batch_details',
        get_batch_details,
        methods=['GET']
    )

    app.add_url_rule(
        '/api/batches/<int:batch_id>',
        'update_batch',
        update_batch,
        methods=['PUT']
    )

    app.add_url_rule(
        '/api/batches/<int:batch_id>',
        'delete_batch_endpoint',
        delete_batch_endpoint,
        methods=['DELETE']
    )

    app.add_url_rule(
        '/api/batches/<int:batch_id>/statistics',
        'get_batch_statistics_endpoint',
        get_batch_statistics_endpoint,
        methods=['GET']
    )

    # Batch job management routes
    app.add_url_rule(
        '/api/batches/<int:batch_id>/jobs/<int:job_id>',
        'add_job_to_batch_endpoint',
        add_job_to_batch_endpoint,
        methods=['POST']
    )
