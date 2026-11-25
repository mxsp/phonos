"""API routes for job bunches - group multiple jobs together.

Endpoints:
- GET    /api/projects/<project_id>/bunches       - List project bunches
- POST   /api/projects/<project_id>/bunches       - Create new bunch
- GET    /api/bunches/<bunch_id>                  - Get bunch details
- GET    /api/bunches/<bunch_id>/items            - Get bunch items
- POST   /api/bunches/<bunch_id>/start            - Start bunch execution
- POST   /api/bunches/<bunch_id>/cancel           - Cancel bunch
- DELETE /api/bunches/<bunch_id>                  - Delete bunch
- POST   /api/bunches/<bunch_id>/items            - Add items to bunch
- DELETE /api/bunches/<bunch_id>/items/<item_id>  - Remove item from bunch
"""
from __future__ import annotations

import logging
from flask import jsonify, request, g

from phonos_app.app import app
from phonos_app.helpers import (
    login_required,
    get_db,
    project_role_required,
)
from phonos_app.helpers.security import csrf_protect, sanitize_json_inputs
from phonos_app.helpers.bunches import (
    create_bunch,
    get_bunch,
    get_bunch_by_id,
    get_project_bunches,
    get_bunch_items,
    start_bunch,
    cancel_bunch,
    delete_bunch,
    BUNCH_STATUS_PENDING,
)

from production.config import Config

logger = logging.getLogger(__name__)

PROJECT_ROLE_MANAGER = Config.PROJECT_ROLE_MANAGER
PROJECT_ROLE_CONTRIBUTOR = Config.PROJECT_ROLE_CONTRIBUTOR
PROJECT_ROLE_VIEWER = Config.PROJECT_ROLE_VIEWER
JOB_SUBMISSION_ALLOWED_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}
PROJECT_VIEW_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER}


@app.route('/api/projects/<int:project_id>/bunches', methods=['GET'])
@login_required
@project_role_required(PROJECT_VIEW_ROLES)
def list_project_bunches(project_id: int):
    """List all job bunches for a project.

    Query params:
    - limit: Max bunches to return (default 50)
    - offset: Pagination offset (default 0)
    """
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    # Cap limit at 100
    limit = min(limit, 100)

    bunches = get_project_bunches(project_id, limit=limit, offset=offset)

    return jsonify({
        'status': 'success',
        'bunches': bunches,
        'count': len(bunches)
    })


@app.route('/api/projects/<int:project_id>/bunches', methods=['POST'])
@login_required
@csrf_protect
@project_role_required(JOB_SUBMISSION_ALLOWED_ROLES)
def create_project_bunch(project_id: int):
    """Create a new job bunch.

    Request body:
    {
        "name": "My Batch Job",
        "model": "type-0",
        "description": "Optional description",
        "priority": 2,  // Optional: 0=CRITICAL, 1=PREMIUM, 2=STANDARD, 3=BACKGROUND
        "items": [
            {"name": "Protein 1", "sequence": "MKTAYIAK..."},
            {"name": "Protein 2", "sequence": "MVLSPAD..."},
            {"name": "From File", "file_id": 123}
        ]
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'status': 'error', 'message': 'No data provided'}), 400

    # Validate required fields
    name = data.get('name')
    model = data.get('model')
    items = data.get('items', [])

    if not name:
        return jsonify({'status': 'error', 'message': 'Name is required'}), 400

    if not model:
        return jsonify({'status': 'error', 'message': 'Model is required'}), 400

    if not items or not isinstance(items, list):
        return jsonify({'status': 'error', 'message': 'Items array is required'}), 400

    if len(items) > 1000:
        return jsonify({'status': 'error', 'message': 'Maximum 1000 items per bunch'}), 400

    # Validate model
    model_info = Config.get_model_definition(model)
    if not model_info:
        return jsonify({'status': 'error', 'message': f'Unknown model: {model}'}), 400

    # Validate items
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return jsonify({'status': 'error', 'message': f'Item {idx} must be an object'}), 400

        has_sequence = item.get('sequence')
        has_file = item.get('file_id')

        if not has_sequence and not has_file:
            return jsonify({
                'status': 'error',
                'message': f'Item {idx} must have either sequence or file_id'
            }), 400

    # Get optional fields
    description = data.get('description', '')
    priority = data.get('priority', 2)

    # Validate priority
    if priority not in (0, 1, 2, 3):
        priority = 2

    # Create the bunch
    user_id = g.user['id']
    result = create_bunch(
        project_id=project_id,
        creator_id=user_id,
        name=name,
        model=model,
        items=items,
        description=description,
        priority=priority
    )

    if result['status'] == 'error':
        return jsonify(result), 400

    return jsonify(result), 201


@app.route('/api/bunches/<string:bunch_id>', methods=['GET'])
@login_required
def get_bunch_details(bunch_id: str):
    """Get bunch details."""
    bunch = get_bunch(bunch_id)

    if not bunch:
        return jsonify({'status': 'error', 'message': 'Bunch not found'}), 404

    # Check user has access to this project
    db = get_db()
    result = db.execute("""
        SELECT role FROM project_assignments
        WHERE project_id = %s AND user_id = %s
    """, (bunch['project_id'], g.user['id']))

    if not result.fetchone():
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    return jsonify({
        'status': 'success',
        'bunch': bunch
    })


@app.route('/api/bunches/<string:bunch_id>/items', methods=['GET'])
@login_required
def get_bunch_items_route(bunch_id: str):
    """Get all items in a bunch."""
    bunch = get_bunch(bunch_id)

    if not bunch:
        return jsonify({'status': 'error', 'message': 'Bunch not found'}), 404

    # Check user has access
    db = get_db()
    result = db.execute("""
        SELECT role FROM project_assignments
        WHERE project_id = %s AND user_id = %s
    """, (bunch['project_id'], g.user['id']))

    if not result.fetchone():
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    items = get_bunch_items(bunch['id'])

    return jsonify({
        'status': 'success',
        'bunch_id': bunch_id,
        'items': items,
        'count': len(items)
    })


@app.route('/api/bunches/<string:bunch_id>/start', methods=['POST'])
@login_required
@csrf_protect
def start_bunch_route(bunch_id: str):
    """Start processing a bunch - submit all jobs."""
    user_id = g.user['id']

    # Check bunch exists and user has access
    bunch = get_bunch(bunch_id)
    if not bunch:
        return jsonify({'status': 'error', 'message': 'Bunch not found'}), 404

    # Check project role
    db = get_db()
    result = db.execute("""
        SELECT role FROM project_assignments
        WHERE project_id = %s AND user_id = %s
    """, (bunch['project_id'], user_id))

    assignment = result.fetchone()
    if not assignment or assignment['role'] not in JOB_SUBMISSION_ALLOWED_ROLES:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    # Start the bunch
    result = start_bunch(bunch_id, user_id)

    if result['status'] == 'error':
        return jsonify(result), 400

    return jsonify(result)


@app.route('/api/bunches/<string:bunch_id>/cancel', methods=['POST'])
@login_required
@csrf_protect
def cancel_bunch_route(bunch_id: str):
    """Cancel a bunch and refund credits for pending jobs."""
    user_id = g.user['id']

    result = cancel_bunch(bunch_id, user_id)

    if result['status'] == 'error':
        return jsonify(result), 400

    return jsonify(result)


@app.route('/api/bunches/<string:bunch_id>', methods=['DELETE'])
@login_required
@csrf_protect
def delete_bunch_route(bunch_id: str):
    """Delete a bunch (only pending or cancelled bunches)."""
    user_id = g.user['id']

    result = delete_bunch(bunch_id, user_id)

    if result['status'] == 'error':
        return jsonify(result), 400

    return jsonify(result)


@app.route('/api/bunches/<string:bunch_id>/items', methods=['POST'])
@login_required
@csrf_protect
def add_bunch_items(bunch_id: str):
    """Add items to an existing bunch (only if pending).

    Request body:
    {
        "items": [
            {"name": "New Protein", "sequence": "MKTAYIAK..."}
        ]
    }
    """
    user_id = g.user['id']

    # Get bunch
    bunch = get_bunch(bunch_id)
    if not bunch:
        return jsonify({'status': 'error', 'message': 'Bunch not found'}), 404

    # Check permissions
    if bunch['creator_id'] != user_id:
        return jsonify({'status': 'error', 'message': 'Only the creator can modify this bunch'}), 403

    if bunch['status'] != BUNCH_STATUS_PENDING:
        return jsonify({'status': 'error', 'message': 'Can only add items to pending bunches'}), 400

    data = request.get_json()
    items = data.get('items', [])

    if not items:
        return jsonify({'status': 'error', 'message': 'No items provided'}), 400

    db = get_db()
    bunch_db_id = bunch['id']

    # Get current max sequence_order
    result = db.execute("""
        SELECT COALESCE(MAX(sequence_order), -1) as max_order
        FROM job_bunch_items WHERE bunch_id = %s
    """, (bunch_db_id,))
    max_order = result.fetchone()['max_order']

    # Add new items
    added = 0
    for idx, item in enumerate(items):
        sequence = item.get('sequence')
        file_id = item.get('file_id')
        item_name = item.get('name', f"Item {max_order + idx + 2}")

        if not sequence and not file_id:
            continue

        db.execute("""
            INSERT INTO job_bunch_items
            (bunch_id, sequence_order, input_name, input_sequence, input_file_id, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
        """, (bunch_db_id, max_order + idx + 1, item_name, sequence, file_id))
        added += 1

    # Update total count
    db.execute("""
        UPDATE job_bunches
        SET total_jobs = total_jobs + %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (added, bunch_db_id))

    db.commit()

    return jsonify({
        'status': 'success',
        'added': added,
        'message': f'Added {added} items to bunch'
    })


@app.route('/api/bunches/<string:bunch_id>/items/<int:item_id>', methods=['DELETE'])
@login_required
@csrf_protect
def remove_bunch_item(bunch_id: str, item_id: int):
    """Remove an item from a bunch (only if pending)."""
    user_id = g.user['id']

    # Get bunch
    bunch = get_bunch(bunch_id)
    if not bunch:
        return jsonify({'status': 'error', 'message': 'Bunch not found'}), 404

    # Check permissions
    if bunch['creator_id'] != user_id:
        return jsonify({'status': 'error', 'message': 'Only the creator can modify this bunch'}), 403

    if bunch['status'] != BUNCH_STATUS_PENDING:
        return jsonify({'status': 'error', 'message': 'Can only remove items from pending bunches'}), 400

    db = get_db()
    bunch_db_id = bunch['id']

    # Check item exists and belongs to this bunch
    result = db.execute("""
        SELECT id FROM job_bunch_items
        WHERE id = %s AND bunch_id = %s AND status = 'pending'
    """, (item_id, bunch_db_id))

    if not result.fetchone():
        return jsonify({'status': 'error', 'message': 'Item not found or already processed'}), 404

    # Delete item
    db.execute("DELETE FROM job_bunch_items WHERE id = %s", (item_id,))

    # Update total count
    db.execute("""
        UPDATE job_bunches
        SET total_jobs = total_jobs - 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (bunch_db_id,))

    db.commit()

    return jsonify({
        'status': 'success',
        'message': 'Item removed from bunch'
    })


# =============================================================================
# BUNCH STATISTICS ENDPOINT
# =============================================================================

@app.route('/api/bunches/<string:bunch_id>/stats', methods=['GET'])
@login_required
def get_bunch_stats(bunch_id: str):
    """Get detailed statistics for a bunch."""
    bunch = get_bunch(bunch_id)

    if not bunch:
        return jsonify({'status': 'error', 'message': 'Bunch not found'}), 404

    # Check user has access
    db = get_db()
    result = db.execute("""
        SELECT role FROM project_assignments
        WHERE project_id = %s AND user_id = %s
    """, (bunch['project_id'], g.user['id']))

    if not result.fetchone():
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    bunch_db_id = bunch['id']

    # Get detailed status counts
    result = db.execute("""
        SELECT
            status,
            COUNT(*) as count
        FROM job_bunch_items
        WHERE bunch_id = %s
        GROUP BY status
    """, (bunch_db_id,))

    status_counts = {row['status']: row['count'] for row in result.fetchall()}

    # Get execution time stats for completed jobs
    result = db.execute("""
        SELECT
            AVG(j.execution_time) as avg_time,
            MIN(j.execution_time) as min_time,
            MAX(j.execution_time) as max_time,
            SUM(j.execution_time) as total_time
        FROM job_bunch_items jbi
        JOIN jobs j ON jbi.job_id = j.id
        WHERE jbi.bunch_id = %s AND jbi.status = 'completed'
    """, (bunch_db_id,))

    time_stats = result.fetchone()

    return jsonify({
        'status': 'success',
        'bunch_id': bunch_id,
        'stats': {
            'total_items': bunch['total_jobs'],
            'completed': bunch['completed_jobs'],
            'failed': bunch['failed_jobs'],
            'pending': status_counts.get('pending', 0),
            'submitted': status_counts.get('submitted', 0),
            'running': status_counts.get('running', 0),
            'cancelled': status_counts.get('cancelled', 0),
            'progress_percent': bunch['progress_percent'],
            'total_cost': bunch['total_cost'],
            'execution_time': {
                'average_seconds': round(time_stats['avg_time'] or 0, 2),
                'min_seconds': round(time_stats['min_time'] or 0, 2),
                'max_seconds': round(time_stats['max_time'] or 0, 2),
                'total_seconds': round(time_stats['total_time'] or 0, 2)
            }
        }
    })


def register_bunch_routes(flask_app):
    """Register bunch routes with the Flask app.

    Called from app.py to register routes.
    """
    # Routes are already registered via decorators
    logger.info("Job bunch routes registered")
