"""Helper functions for API key management.

This module provides unified API key management for:
- Public API access (type='public')
- External worker access (type='worker')
- Model-specific access (type='model')
- Project-wide access (type='project')
"""

import secrets
import hashlib
import time
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from phonos_app.helpers.database import get_db

logger = logging.getLogger(__name__)

# Key type prefixes for easy identification
KEY_PREFIXES = {
    'public': 'pk_',      # Public API key
    'worker': 'wk_',      # Worker/bridge key
    'model': 'mk_',       # Model-specific key
    'project': 'pj_',     # Project-wide key
}


def generate_api_key(key_type: str = 'public'):
    """
    Generate a secure API key.

    Args:
        key_type: Type of key to generate ('public', 'worker', 'model', 'project')

    Returns:
        tuple: (api_key, key_hash, key_prefix, key_id) where:
            - api_key is the full key to show to user (only once)
            - key_hash is what we store in database
            - key_prefix is first 8 chars for identification
            - key_id is unique identifier for the key
    """
    prefix = KEY_PREFIXES.get(key_type, 'pk_')

    # Generate a random 32-byte key
    api_key = prefix + secrets.token_urlsafe(32)

    # Hash the key for storage (using SHA-256)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Store prefix + first 8 characters for identification
    key_prefix = api_key[:len(prefix) + 8]

    # Generate unique key_id
    key_id = str(uuid.uuid4())

    return api_key, key_hash, key_prefix, key_id


def create_api_key(user_id: int, name: str) -> dict:
    """
    Create a new API key for a user.

    Args:
        user_id: The user's ID
        name: Friendly name for the API key

    Returns:
        dict with api_key (full key - show only once), key_id, and metadata
    """
    db = get_db()

    # Generate the key
    api_key, key_hash, key_prefix, key_id = generate_api_key()

    # Store in database
    result = db.execute("""
        INSERT INTO api_keys (user_id, key_id, key_hash, key_prefix, name, is_active)
        VALUES (%s, %s, %s, %s, %s, 1)
        RETURNING id, created_at
    """, (user_id, key_id, key_hash, key_prefix, name))

    row = result.fetchone()
    db.commit()

    return {
        'key_id': row['id'],
        'api_key': api_key,  # Only returned once!
        'key_prefix': key_prefix,
        'name': name,
        'created_at': row['created_at'].isoformat() if row['created_at'] else None
    }


def get_user_api_keys(user_id: int) -> list:
    """
    Get all API keys for a user.

    Args:
        user_id: The user's ID

    Returns:
        list of API key metadata (without the actual keys)
    """
    db = get_db()

    result = db.execute("""
        SELECT
            ak.id,
            ak.key_id,
            ak.name,
            ak.created_at,
            ak.last_used_at,
            ak.is_active,
            COUNT(au.id) as total_requests
        FROM api_keys ak
        LEFT JOIN api_usage au ON ak.id = au.api_key_id
        WHERE ak.user_id = %s
        GROUP BY ak.id, ak.key_id, ak.name, ak.created_at, ak.last_used_at, ak.is_active
        ORDER BY ak.created_at DESC
    """, (user_id,))

    keys = []
    for row in result.fetchall():
        # Generate a key prefix from the first 8 characters of key_id
        key_prefix = row['key_id'][:8] + '...' if row['key_id'] else 'pk_...'

        keys.append({
            'id': row['id'],
            'key_prefix': key_prefix,
            'name': row['name'],
            'created_at': row['created_at'].isoformat() if row['created_at'] else None,
            'last_used_at': row['last_used_at'].isoformat() if row['last_used_at'] else None,
            'is_active': bool(row['is_active']),
            'total_requests': row['total_requests'] or 0
        })

    return keys


def verify_api_key(api_key: str) -> dict:
    """
    Verify an API key and return associated user info.

    Args:
        api_key: The API key to verify

    Returns:
        dict with user_id and key_id if valid, None otherwise
    """
    db = get_db()

    # Hash the provided key
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    result = db.execute("""
        SELECT id, user_id, rate_limit_per_minute
        FROM api_keys
        WHERE key_hash = %s AND is_active = 1
    """, (key_hash,))

    row = result.fetchone()

    if row:
        # Update last_used_at and increment request counter
        db.execute("""
            UPDATE api_keys
            SET last_used_at = CURRENT_TIMESTAMP,
                total_requests = total_requests + 1
            WHERE id = %s
        """, (row['id'],))
        db.commit()

        return {
            'key_id': row['id'],
            'user_id': row['user_id'],
            'rate_limit': row['rate_limit_per_minute']
        }

    return None


def delete_api_key(user_id: int, key_id: int) -> bool:
    """
    Delete an API key.

    Args:
        user_id: The user's ID (for security)
        key_id: The API key ID to delete

    Returns:
        bool: True if deleted, False otherwise
    """
    db = get_db()

    result = db.execute("""
        DELETE FROM api_keys
        WHERE id = %s AND user_id = %s
        RETURNING id
    """, (key_id, user_id))

    deleted = result.fetchone() is not None
    db.commit()

    return deleted


def toggle_api_key(user_id: int, key_id: int, is_active: bool) -> bool:
    """
    Enable or disable an API key.

    Args:
        user_id: The user's ID (for security)
        key_id: The API key ID
        is_active: True to enable, False to disable

    Returns:
        bool: True if updated, False otherwise
    """
    db = get_db()

    result = db.execute("""
        UPDATE api_keys
        SET is_active = %s
        WHERE id = %s AND user_id = %s
        RETURNING id
    """, (is_active, key_id, user_id))

    updated = result.fetchone() is not None
    db.commit()

    return updated


def log_api_usage(api_key_id: int, user_id: int, endpoint: str, model_type: str,
                  status_code: int, response_time_ms: int, credits_used: int,
                  ip_address: str = None, user_agent: str = None):
    """
    Log an API request for analytics and billing.

    Args:
        api_key_id: The API key ID used
        user_id: The user's ID
        endpoint: The API endpoint called
        model_type: The model type (e.g., 'type-0', 'type-2')
        status_code: HTTP status code
        response_time_ms: Response time in milliseconds
        credits_used: Number of credits deducted
        ip_address: Client IP address
        user_agent: Client user agent
    """
    db = get_db()

    db.execute("""
        INSERT INTO api_usage
        (api_key_id, user_id, endpoint, model_type, status_code,
         response_time_ms, credits_used, ip_address, user_agent)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (api_key_id, user_id, endpoint, model_type, status_code,
          response_time_ms, credits_used, ip_address, user_agent))

    db.commit()


def check_rate_limit(api_key_id: int, rate_limit: int) -> bool:
    """
    Check if API key has exceeded rate limit.

    Args:
        api_key_id: The API key ID
        rate_limit: Requests per minute allowed

    Returns:
        bool: True if within limit, False if exceeded
    """
    db = get_db()

    # Count requests in the last minute
    result = db.execute("""
        SELECT COUNT(*) as request_count
        FROM api_usage
        WHERE api_key_id = %s
        AND timestamp > CURRENT_TIMESTAMP - INTERVAL '1 minute'
    """, (api_key_id,))

    row = result.fetchone()
    request_count = row['request_count'] if row else 0

    return request_count < rate_limit


# =============================================================================
# UNIFIED API KEY VERIFICATION (supports both public and worker keys)
# =============================================================================

def verify_unified_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """
    Verify any type of API key (public, worker, model, project) and return auth context.

    This is the unified verification function that checks:
    1. Public API keys (api_keys table)
    2. Worker/Bridge keys (api_keys table with key_type='worker')
    3. Model-specific keys (user_model_api_keys table)

    Args:
        api_key: The API key to verify

    Returns:
        dict with auth context if valid:
            - key_id: Database ID of the key
            - key_type: Type of key ('public', 'worker', 'model', 'project')
            - user_id: User ID
            - rate_limit: Requests per minute
            - model_id: Optional model ID (for model keys)
            - project_id: Optional project ID (for project keys)
            - permissions: List of permissions
        None if invalid
    """
    if not api_key:
        return None

    db = get_db()
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # First try api_keys table (public and worker keys)
    result = db.execute("""
        SELECT id, user_id, rate_limit_per_minute, key_type,
               permissions, scope_model_type, is_active
        FROM api_keys
        WHERE key_hash = %s AND is_active = 1
    """, (key_hash,))

    row = result.fetchone()

    if row:
        # Update last_used_at and increment request counter
        db.execute("""
            UPDATE api_keys
            SET last_used_at = CURRENT_TIMESTAMP,
                total_requests = total_requests + 1
            WHERE id = %s
        """, (row['id'],))
        db.commit()

        key_type = row.get('key_type', 'public') or 'public'
        permissions = []
        if row.get('permissions'):
            try:
                import json
                permissions = json.loads(row['permissions'])
            except:
                permissions = []

        return {
            'key_id': row['id'],
            'key_type': key_type,
            'user_id': row['user_id'],
            'rate_limit': row['rate_limit_per_minute'] or 60,
            'scope_model_type': row.get('scope_model_type'),
            'permissions': permissions,
            'source': 'api_keys'
        }

    # Try user_model_api_keys table (for backwards compatibility)
    result = db.execute("""
        SELECT id, user_model_id, project_id, user_id, rate_limit_per_minute
        FROM user_model_api_keys
        WHERE key_hash = %s AND is_active = true
    """, (key_hash,))

    row = result.fetchone()

    if row:
        db.execute("""
            UPDATE user_model_api_keys
            SET last_used_at = CURRENT_TIMESTAMP,
                total_requests = total_requests + 1
            WHERE id = %s
        """, (row['id'],))
        db.commit()

        key_type = 'model' if row['user_model_id'] else 'project'

        return {
            'key_id': row['id'],
            'key_type': key_type,
            'user_id': row['user_id'],
            'rate_limit': row['rate_limit_per_minute'] or 60,
            'user_model_id': row['user_model_id'],
            'project_id': row['project_id'],
            'source': 'user_model_api_keys'
        }

    return None


def create_worker_api_key(user_id: int, name: str, scope_model_type: str = None,
                          permissions: List[str] = None) -> Dict[str, Any]:
    """
    Create a new worker API key for external job processing.

    Args:
        user_id: The user's ID
        name: Friendly name for the key
        scope_model_type: Optional model type to restrict (e.g., 'type-0' for ESMFold)
        permissions: List of permissions (e.g., ['poll', 'submit', 'cancel'])

    Returns:
        dict with api_key (show only once), key_id, and metadata
    """
    import json
    db = get_db()

    # Generate the key with worker prefix
    api_key, key_hash, key_prefix, key_uuid = generate_api_key('worker')

    # Default permissions for worker keys
    if permissions is None:
        permissions = ['poll', 'submit', 'status']

    # Store in database
    result = db.execute("""
        INSERT INTO api_keys (user_id, key_id, key_hash, key_prefix, name,
                              key_type, scope_model_type, permissions, is_active)
        VALUES (%s, %s, %s, %s, %s, 'worker', %s, %s, 1)
        RETURNING id, created_at
    """, (user_id, key_uuid, key_hash, key_prefix, name,
          scope_model_type, json.dumps(permissions)))

    row = result.fetchone()
    db.commit()

    return {
        'key_id': row['id'],
        'api_key': api_key,
        'key_prefix': key_prefix,
        'key_type': 'worker',
        'name': name,
        'scope_model_type': scope_model_type,
        'permissions': permissions,
        'created_at': row['created_at'].isoformat() if row['created_at'] else None
    }


def get_user_worker_keys(user_id: int) -> List[Dict[str, Any]]:
    """
    Get all worker API keys for a user.

    Args:
        user_id: The user's ID

    Returns:
        list of worker key metadata
    """
    import json
    db = get_db()

    result = db.execute("""
        SELECT id, key_id, key_prefix, name, key_type, scope_model_type,
               permissions, created_at, last_used_at, is_active, total_requests
        FROM api_keys
        WHERE user_id = %s AND key_type = 'worker'
        ORDER BY created_at DESC
    """, (user_id,))

    keys = []
    for row in result.fetchall():
        permissions = []
        if row.get('permissions'):
            try:
                permissions = json.loads(row['permissions'])
            except:
                pass

        keys.append({
            'id': row['id'],
            'key_prefix': row['key_prefix'] or (row['key_id'][:8] + '...'),
            'name': row['name'],
            'scope_model_type': row['scope_model_type'],
            'permissions': permissions,
            'created_at': row['created_at'].isoformat() if row['created_at'] else None,
            'last_used_at': row['last_used_at'].isoformat() if row['last_used_at'] else None,
            'is_active': bool(row['is_active']),
            'total_requests': row['total_requests'] or 0
        })

    return keys


# =============================================================================
# EXTERNAL JOB QUEUE MANAGEMENT
# =============================================================================

def queue_external_job(job_id: int, job_uuid: str, user_id: int, model_type: str,
                       job_data: dict, timeout_seconds: int = 3600) -> bool:
    """
    Add a job to the external job queue for worker processing.

    Args:
        job_id: Database ID of the job
        job_uuid: UUID string of the job
        user_id: User ID who submitted the job
        model_type: Model type (e.g., 'type-0', 'type-2')
        job_data: Job input data (will be JSON serialized)
        timeout_seconds: Timeout for job processing

    Returns:
        bool: True if queued successfully
    """
    import json
    db = get_db()

    try:
        # Check if job already in queue
        existing = db.execute("""
            SELECT id FROM external_api_job_queue
            WHERE job_id = %s
        """, (job_id,)).fetchone()

        if existing:
            logger.warning(f"Job {job_uuid} already in external queue")
            return True

        db.execute("""
            INSERT INTO external_api_job_queue
            (job_id, job_uuid, user_id, model_type, job_data, timeout_seconds, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        """, (job_id, job_uuid, user_id, model_type, json.dumps(job_data), timeout_seconds))

        db.commit()
        logger.info(f"Queued external job {job_uuid} for model {model_type}")
        return True

    except Exception as e:
        logger.error(f"Failed to queue external job {job_uuid}: {e}")
        db.rollback()
        return False


def get_pending_external_jobs(user_id: int, model_type: str = None,
                              max_jobs: int = 10) -> List[Dict[str, Any]]:
    """
    Get pending jobs from the external queue for a user's workers.

    Args:
        user_id: User ID of the worker owner
        model_type: Optional filter by model type
        max_jobs: Maximum jobs to return

    Returns:
        List of pending jobs with decrypted data
    """
    import json
    db = get_db()

    query = """
        SELECT eq.id, eq.job_id, eq.job_uuid, eq.model_type, eq.job_data,
               eq.timeout_seconds, eq.created_at,
               j.name as job_name, j.sequence
        FROM external_api_job_queue eq
        JOIN jobs j ON eq.job_id = j.id
        WHERE eq.user_id = %s AND eq.status = 'pending'
    """
    params = [user_id]

    if model_type:
        query += " AND eq.model_type = %s"
        params.append(model_type)

    query += " ORDER BY eq.created_at ASC LIMIT %s"
    params.append(max_jobs)

    result = db.execute(query, tuple(params))
    jobs = []

    for row in result.fetchall():
        job_data = {}
        if row['job_data']:
            try:
                job_data = json.loads(row['job_data'])
            except:
                pass

        jobs.append({
            'queue_id': row['id'],
            'job_id': row['job_id'],
            'job_uuid': row['job_uuid'],
            'model_type': row['model_type'],
            'job_name': row['job_name'],
            'sequence': row['sequence'],
            'job_data': job_data,
            'timeout_seconds': row['timeout_seconds'],
            'created_at': row['created_at'].isoformat() if row['created_at'] else None
        })

    return jobs


def assign_external_job(queue_id: int, worker_key_id: int,
                        worker_identifier: str = 'unknown') -> bool:
    """
    Mark an external job as assigned to a worker.

    Args:
        queue_id: ID in external_api_job_queue
        worker_key_id: API key ID of the worker
        worker_identifier: Optional worker name/ID

    Returns:
        bool: True if assigned successfully
    """
    db = get_db()

    try:
        db.execute("""
            UPDATE external_api_job_queue
            SET status = 'assigned',
                assigned_at = CURRENT_TIMESTAMP,
                worker_api_key_id = %s,
                worker_identifier = %s
            WHERE id = %s AND status = 'pending'
        """, (worker_key_id, worker_identifier, queue_id))

        db.commit()
        return True

    except Exception as e:
        logger.error(f"Failed to assign job {queue_id}: {e}")
        db.rollback()
        return False


def complete_external_job(job_uuid: str, status: str, result_data: dict = None,
                          error_message: str = None, metrics: dict = None) -> bool:
    """
    Mark an external job as completed or failed.

    Args:
        job_uuid: UUID of the job
        status: 'completed' or 'failed'
        result_data: Result data (for completed jobs)
        error_message: Error message (for failed jobs)
        metrics: Processing metrics

    Returns:
        bool: True if updated successfully
    """
    import json
    db = get_db()

    try:
        # Update external queue
        db.execute("""
            UPDATE external_api_job_queue
            SET status = %s,
                completed_at = CURRENT_TIMESTAMP
            WHERE job_uuid = %s
        """, (status, job_uuid))

        # Prepare result message
        result_message = None
        if status == 'completed' and result_data:
            result_message = json.dumps(result_data)
        elif status == 'failed' and error_message:
            result_message = json.dumps({'error': error_message})

        # Update main jobs table
        runtime = metrics.get('runtime_seconds') if metrics else None
        db.execute("""
            UPDATE jobs
            SET status = %s,
                message = %s,
                execution_time = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE job_id = %s
        """, (status, result_message, runtime, job_uuid))

        db.commit()
        logger.info(f"Completed external job {job_uuid} with status {status}")
        return True

    except Exception as e:
        logger.error(f"Failed to complete external job {job_uuid}: {e}")
        db.rollback()
        return False
