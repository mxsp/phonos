"""Helper functions for job submission and management."""

import uuid
import json
import logging
from datetime import datetime
from phonos_app.helpers.database import get_db
from phonos_app.helpers.paths import get_job_branch_path
from production.config import Config
from production.mrs_service.runtime import get_redis_connection, NEW_JOBS_QUEUE

logger = logging.getLogger(__name__)


def submit_job_helper(user_id: int, project_id: int, model_type: str, sequence: str, name: str = None, custom_params: dict = None):
    """
    Submit a job for a user via API or internal routes.

    Args:
        user_id: User ID submitting the job
        project_id: Project ID for the job
        model_type: Model type (e.g., 'type-0', 'type-2')
        sequence: Input sequence
        name: Optional job name
        custom_params: Optional custom parameters

    Returns:
        dict with status, job_id, credits_used, and message
    """
    db = get_db()

    # Validate model
    model_info = Config.get_model_definition(model_type)
    if not model_info:
        return {'status': 'error', 'message': f'Unknown model type: {model_type}'}

    # Check if model is active (simplified check)
    # Note: You may want to add get_model_state check here if needed

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Use provided name or generate default
    if not name:
        model_short_name = model_info.get('short_name', model_info.get('name', 'Job'))
        job_name = f"{model_short_name}-{job_id[:8]}"
    else:
        job_name = name

    # Calculate credits
    credits_cost = model_info.get('cost', 10)  # Default to 10 if not specified

    # Check user has enough credits
    user = db.execute('SELECT credits FROM users WHERE id = %s', (user_id,)).fetchone()
    if not user:
        return {'status': 'error', 'message': 'User not found'}

    if user['credits'] < credits_cost:
        return {
            'status': 'error',
            'message': f'Insufficient credits. Required: {credits_cost}, Available: {user["credits"]}'
        }

    # Deduct credits
    db.execute('UPDATE users SET credits = credits - %s WHERE id = %s', (credits_cost, user_id))

    # Create job record
    try:
        result = db.execute("""
            INSERT INTO jobs (job_id, creator_id, project_id, name, model, status, sequence, created_at)
            VALUES (%s, %s, %s, %s, %s, 'queued', %s, CURRENT_TIMESTAMP)
            RETURNING job_id
        """, (job_id, user_id, project_id, job_name, model_type, sequence))

        db.commit()

        # Enqueue the job to Redis for the MRS to process
        try:
            import os
            redis_client = get_redis_connection()

            # Create output directory and path
            project_path = os.path.join(Config.USER_UPLOADS_FOLDER, f"user-{user_id}-personal", "projects", str(project_id))
            output_dir = get_job_branch_path(project_path, job_id, branch='main', create=True)
            if not output_dir:
                raise RuntimeError("Failed to resolve job output branch directory")

            # Get output extension from model info
            output_extension = model_info.get('output_extension') or '.pdb'
            output_filename = f"{job_id}{output_extension}"
            output_path = os.path.join(output_dir, output_filename)

            # Get task type from model info
            job_task = model_info.get('task') or 'protein_folding'

            # Build job payload in the format workers expect
            job_payload = {
                'job_id': job_id,
                'model': model_type,
                'task': job_task,
                'params': {'sequence': sequence},
                'output_path': output_path,
                'output_display_name': output_filename
            }

            # Add custom params if provided
            if custom_params:
                job_payload['params'].update(custom_params)

            redis_client.lpush(NEW_JOBS_QUEUE, json.dumps(job_payload))

        except Exception as redis_error:
            # Log the error but don't fail the request since job is already in DB
            # The MRS recovery mechanism should pick it up later
            logger.warning("Failed to queue job to Redis: %s", redis_error)

        return {
            'status': 'success',
            'job_id': job_id,
            'credits_used': credits_cost,
            'message': 'Job submitted successfully'
        }

    except Exception as e:
        db.rollback()
        return {
            'status': 'error',
            'message': f'Failed to create job: {str(e)}'
        }
