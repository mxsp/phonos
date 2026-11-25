"""Helper functions for user-defined models management."""

import json
import secrets
import string
import uuid
import logging
from typing import Optional, List, Dict, Any

from phonos_app.helpers.database import get_db

# Import the new crypto module for user models
from phonos_app.helpers.user_model_crypto import (
    encrypt_api_key_for_storage,
    decrypt_api_key_from_storage,
    encrypt_data,
    decrypt_data,
    hash_api_key as crypto_hash_api_key,
    generate_model_api_key
)

logger = logging.getLogger(__name__)
CUSTOM_MODEL_CREDIT_COST = 20


def _serialize_datetime(value: Any) -> Optional[str]:
    """Serialize datetime values safely across SQLite/PostgreSQL drivers."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read a DB row value safely across dict-like and sqlite row types."""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    try:
        return row[key]
    except Exception:
        return default


def generate_model_id(length: int = 16) -> str:
    """
    Generate a unique 16-character alphanumeric model ID.

    Uses a combination of:
    - Uppercase letters (A-Z)
    - Lowercase letters (a-z)
    - Digits (0-9)

    Args:
        length: Length of the ID (default 16)

    Returns:
        A unique alphanumeric string
    """
    # Use alphanumeric characters only (no special chars for URL safety)
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def get_unique_model_id() -> str:
    """
    Generate a unique model_id that doesn't exist in the database.

    Retries up to 10 times to find a unique ID.

    Returns:
        A unique 16-character model_id

    Raises:
        RuntimeError: If unable to generate unique ID after max retries
    """
    db = get_db()
    max_retries = 10

    for _ in range(max_retries):
        model_id = generate_model_id()

        # Check if it already exists
        result = db.execute(
            'SELECT 1 FROM user_models WHERE model_id = %s LIMIT 1',
            (model_id,)
        )

        if result.fetchone() is None:
            return model_id

    # Fallback: include timestamp for extra uniqueness
    import time
    timestamp_part = hex(int(time.time() * 1000))[2:][-8:]
    random_part = generate_model_id(8)
    return f"{random_part}{timestamp_part}"


def generate_api_key(key_type: str = 'model') -> tuple[str, str, str, str]:
    """
    Backwards-compatible API key generator.

    Returns the same tuple shape older call sites/tests expect:
    (full_key, key_hash, key_prefix, key_id)
    """
    api_key = generate_model_api_key(key_type)
    key_hash = crypto_hash_api_key(api_key)
    key_prefix = api_key[:12]
    key_id = str(uuid.uuid4())
    return api_key, key_hash, key_prefix, key_id

# Note: Encryption is now mandatory via user_model_crypto module
# The old optional encryption_util is deprecated


def create_user_model(
    user_id: int,
    model_name: str,
    model_description: str,
    input_schema: List[Dict[str, Any]],
    project_id: Optional[int] = None,
    model_type: str = 'custom',
    output_format: str = 'json',
    outline_color: str = 'gray',
    credit_cost: int = CUSTOM_MODEL_CREDIT_COST,
    estimated_runtime_seconds: int = 60,
    worker_endpoint_url: Optional[str] = None,
    is_public: bool = False,
    endpoint_mode: str = 'direct_http',
    auth_type: str = 'none',
    auth_header_name: str = 'Authorization',
    upstream_api_key: Optional[str] = None,
    custom_headers: Any = None,
    request_template_json: str = '',
    response_output_path: str = '',
    upstream_model_name: str = '',
) -> Dict[str, Any]:
    """
    Create a new user-defined model.

    Args:
        user_id: The user's ID
        model_name: Name of the model
        model_description: Description of what the model does
        input_schema: List of input field definitions (JSON schema)
        project_id: Optional project ID to associate model with
        model_type: Type of model ('custom', 'python', 'docker')
        output_format: Output file extension
        outline_color: Color for UI display
        credit_cost: Cost in credits per job
        estimated_runtime_seconds: Estimated runtime
        worker_endpoint_url: Optional custom worker endpoint
        is_public: Whether other users can use this model

    Returns:
        dict with model_id and metadata
    """
    db = get_db()
    from phonos_app.helpers.custom_endpoint_runner import (
        ALLOWED_AUTH_TYPES,
        ALLOWED_ENDPOINT_MODES,
        ensure_byom_schema,
        sanitize_custom_headers,
        seal_endpoint_key,
    )
    from phonos_app.helpers.model_providers import validate_endpoint_url

    ensure_byom_schema(db)

    # Validate input_schema
    if not isinstance(input_schema, list):
        raise ValueError("input_schema must be a list")

    mode_clean = (endpoint_mode or ('direct_http' if worker_endpoint_url else 'webhook_bridge')).strip().lower()
    if mode_clean not in ALLOWED_ENDPOINT_MODES:
        raise ValueError(f"endpoint_mode must be one of {', '.join(ALLOWED_ENDPOINT_MODES)}")

    auth_clean = (auth_type or 'none').strip().lower()
    if auth_clean not in ALLOWED_AUTH_TYPES:
        raise ValueError(f"auth_type must be one of {', '.join(ALLOWED_AUTH_TYPES)}")

    if worker_endpoint_url and str(worker_endpoint_url).strip():
        worker_endpoint_url = validate_endpoint_url(str(worker_endpoint_url).strip(), allow_private=True)
    else:
        worker_endpoint_url = None

    headers_dict = sanitize_custom_headers(custom_headers)
    sealed_key = seal_endpoint_key(upstream_api_key)

    # Custom models are fixed-price.
    credit_cost = CUSTOM_MODEL_CREDIT_COST

    # Generate unique 16-char model_id
    unique_model_id = get_unique_model_id()

    # Insert model
    result = db.execute("""
        INSERT INTO user_models (
            model_id, user_id, project_id, model_name, model_description,
            model_type, input_schema, output_format, outline_color,
            credit_cost, estimated_runtime_seconds, worker_endpoint_url,
            is_public, is_active, endpoint_mode, auth_type, auth_header_name,
            upstream_api_key_enc, custom_headers_json, request_template_json,
            response_output_path, upstream_model_name
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, model_id, created_at
    """, (
        unique_model_id, user_id, project_id, model_name, model_description,
        model_type, json.dumps(input_schema), output_format, outline_color,
        credit_cost, estimated_runtime_seconds, worker_endpoint_url,
        is_public, mode_clean, auth_clean, (auth_header_name or 'Authorization')[:80],
        sealed_key, json.dumps(headers_dict), (request_template_json or '')[:8000],
        (response_output_path or '')[:200], (upstream_model_name or '')[:160],
    ))

    row = result.fetchone()
    db.commit()

    return {
        'id': row['id'],
        'model_id': row['model_id'],
        'model_name': model_name,
        'endpoint_mode': mode_clean,
        'worker_endpoint_url': worker_endpoint_url,
        'has_upstream_key': bool(sealed_key),
        'created_at': _serialize_datetime(row['created_at'])
    }


def get_user_models(user_id: int, project_id: Optional[int] = None, include_public: bool = False) -> List[Dict[str, Any]]:
    """
    Get all models for a user or project.
    Includes models from projects the user has access to.

    Args:
        user_id: The user's ID
        project_id: Optional project ID to filter by
        include_public: Include public models from other users

    Returns:
        list of model metadata
    """
    db = get_db()
    from phonos_app.helpers.custom_endpoint_runner import ensure_byom_schema
    ensure_byom_schema(db)

    # Get models that the user owns, are public, OR are from projects the user has access to
    query = """
        SELECT DISTINCT
            um.id,
            um.model_id,
            um.user_id,
            um.project_id,
            um.model_name,
            um.model_description,
            um.model_type,
            um.input_schema,
            um.output_format,
            um.outline_color,
            um.badge,
            um.badge_color,
            um.credit_cost,
            um.estimated_runtime_seconds,
            um.worker_endpoint_url,
            um.endpoint_mode,
            um.auth_type,
            um.auth_header_name,
            um.upstream_api_key_enc,
            um.response_output_path,
            um.upstream_model_name,
            um.is_active,
            um.is_public,
            um.created_at,
            um.last_used_at,
            um.total_jobs_run,
            u.username as owner_username,
            p.name as project_name
        FROM user_models um
        JOIN users u ON um.user_id = u.id
        LEFT JOIN projects p ON um.project_id = p.id
        LEFT JOIN project_assignments pa ON um.project_id = pa.project_id AND pa.user_id = %s
        WHERE (
            um.user_id = %s
            OR (um.is_public = true AND %s)
            OR (um.project_id IS NOT NULL AND pa.user_id IS NOT NULL)
        )
    """

    params = [user_id, user_id, include_public]

    if project_id is not None:
        query += " AND um.project_id = %s"
        params.append(project_id)

    query += " ORDER BY um.created_at DESC"

    result = db.execute(query, tuple(params))

    models = []
    for row in result.fetchall():
        models.append({
            'id': row['id'],
            'model_id': row['model_id'],
            'user_id': row['user_id'],
            'project_id': row['project_id'],
            'project_name': row['project_name'],
            'model_name': row['model_name'],
            'model_description': row['model_description'],
            'model_type': row['model_type'],
            'input_schema': json.loads(row['input_schema']) if isinstance(row['input_schema'], str) else row['input_schema'],
            'output_format': row['output_format'],
            'outline_color': row['outline_color'],
            'badge': row['badge'],
            'badge_color': row['badge_color'],
            'credit_cost': row['credit_cost'],
            'estimated_runtime_seconds': row['estimated_runtime_seconds'],
            'worker_endpoint_url': _row_value(row, 'worker_endpoint_url'),
            'endpoint_mode': _row_value(row, 'endpoint_mode') or 'direct_http',
            'auth_type': _row_value(row, 'auth_type') or 'none',
            'auth_header_name': _row_value(row, 'auth_header_name') or 'Authorization',
            'has_upstream_key': bool(_row_value(row, 'upstream_api_key_enc')),
            'response_output_path': _row_value(row, 'response_output_path') or '',
            'upstream_model_name': _row_value(row, 'upstream_model_name') or '',
            'is_active': bool(row['is_active']),
            'is_public': bool(row['is_public']),
            'created_at': _serialize_datetime(row['created_at']),
            'last_used_at': _serialize_datetime(row['last_used_at']),
            'total_jobs_run': row['total_jobs_run'] or 0,
            'owner_username': row['owner_username'],
            'is_owned_by_user': row['user_id'] == user_id
        })

    return models


def get_user_model(db_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Get a specific user model by database ID (integer).
    Checks if user owns the model, it's public, or user has access to the project.

    Args:
        db_id: The model's database ID (integer)
        user_id: Optional user ID for permission checking

    Returns:
        model metadata or None if not found/not authorized
    """
    db = get_db()

    query = """
        SELECT DISTINCT
            um.id,
            um.model_id,
            um.user_id,
            um.project_id,
            um.model_name,
            um.model_description,
            um.model_type,
            um.input_schema,
            um.output_format,
            um.outline_color,
            um.badge,
            um.badge_color,
            um.credit_cost,
            um.estimated_runtime_seconds,
            um.worker_endpoint_url,
            um.is_active,
            um.is_public,
            um.created_at,
            um.last_used_at,
            um.total_jobs_run,
            u.username as owner_username,
            p.name as project_name
        FROM user_models um
        JOIN users u ON um.user_id = u.id
        LEFT JOIN projects p ON um.project_id = p.id
        LEFT JOIN project_assignments pa ON um.project_id = pa.project_id AND pa.user_id = %s
        WHERE um.id = %s
    """

    params = [user_id if user_id else 0, db_id]

    # Add permission check if user_id provided
    if user_id is not None:
        query += """ AND (
            um.user_id = %s
            OR um.is_public = true
            OR (um.project_id IS NOT NULL AND pa.user_id IS NOT NULL)
        )"""
        params.extend([user_id])

    result = db.execute(query, tuple(params))
    row = result.fetchone()

    if not row:
        return None

    return {
        'id': row['id'],
        'model_id': row['model_id'],
        'user_id': row['user_id'],
        'project_id': row['project_id'],
        'project_name': row['project_name'],
        'model_name': row['model_name'],
        'model_description': row['model_description'],
        'model_type': row['model_type'],
        'input_schema': json.loads(row['input_schema']) if isinstance(row['input_schema'], str) else row['input_schema'],
        'output_format': row['output_format'],
        'outline_color': row['outline_color'],
        'badge': row['badge'],
        'badge_color': row['badge_color'],
        'credit_cost': row['credit_cost'],
        'estimated_runtime_seconds': row['estimated_runtime_seconds'],
        'worker_endpoint_url': row['worker_endpoint_url'],
        'is_active': bool(row['is_active']),
        'is_public': bool(row['is_public']),
        'created_at': _serialize_datetime(row['created_at']),
        'last_used_at': _serialize_datetime(row['last_used_at']),
        'total_jobs_run': row['total_jobs_run'] or 0,
        'owner_username': row['owner_username']
    }


def get_user_model_by_model_id(model_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Get a specific user model by its unique 16-char model_id.
    Checks if user owns the model, it's public, or user has access to the project.

    Args:
        model_id: The model's unique 16-character identifier
        user_id: Optional user ID for permission checking

    Returns:
        model metadata or None if not found/not authorized
    """
    db = get_db()

    query = """
        SELECT DISTINCT
            um.id,
            um.model_id,
            um.user_id,
            um.project_id,
            um.model_name,
            um.model_description,
            um.model_type,
            um.input_schema,
            um.output_format,
            um.outline_color,
            um.badge,
            um.badge_color,
            um.credit_cost,
            um.estimated_runtime_seconds,
            um.worker_endpoint_url,
            um.is_active,
            um.is_public,
            um.created_at,
            um.last_used_at,
            um.total_jobs_run,
            u.username as owner_username,
            p.name as project_name
        FROM user_models um
        JOIN users u ON um.user_id = u.id
        LEFT JOIN projects p ON um.project_id = p.id
        LEFT JOIN project_assignments pa ON um.project_id = pa.project_id AND pa.user_id = %s
        WHERE um.model_id = %s
    """

    params = [user_id if user_id else 0, model_id]

    # Add permission check if user_id provided
    if user_id is not None:
        query += """ AND (
            um.user_id = %s
            OR um.is_public = true
            OR (um.project_id IS NOT NULL AND pa.user_id IS NOT NULL)
        )"""
        params.extend([user_id])

    result = db.execute(query, tuple(params))
    row = result.fetchone()

    if not row:
        return None

    return {
        'id': row['id'],
        'model_id': row['model_id'],
        'user_id': row['user_id'],
        'project_id': row['project_id'],
        'project_name': row['project_name'],
        'model_name': row['model_name'],
        'model_description': row['model_description'],
        'model_type': row['model_type'],
        'input_schema': json.loads(row['input_schema']) if isinstance(row['input_schema'], str) else row['input_schema'],
        'output_format': row['output_format'],
        'outline_color': row['outline_color'],
        'badge': row['badge'],
        'badge_color': row['badge_color'],
        'credit_cost': row['credit_cost'],
        'estimated_runtime_seconds': row['estimated_runtime_seconds'],
        'worker_endpoint_url': row['worker_endpoint_url'],
        'is_active': bool(row['is_active']),
        'is_public': bool(row['is_public']),
        'created_at': _serialize_datetime(row['created_at']),
        'last_used_at': _serialize_datetime(row['last_used_at']),
        'total_jobs_run': row['total_jobs_run'] or 0,
        'owner_username': row['owner_username']
    }


def update_user_model(
    model_id: int,
    user_id: int,
    **updates
) -> bool:
    """
    Update a user model.

    Args:
        model_id: The model's ID
        user_id: The user's ID (for permission checking)
        **updates: Fields to update. Allowed fields are 'model_name', 'model_description',
                   'input_schema', 'output_format', 'outline_color', 'badge',
                   'badge_color', 'credit_cost', 'estimated_runtime_seconds',
                   'worker_endpoint_url', 'is_active', 'is_public'.

    Returns:
        bool: True if updated, False otherwise

    Raises:
        ValueError: If 'input_schema' is provided but is not a list,
                    or if 'model_name' is provided but is empty or whitespace only.
    """
    db = get_db()

    # Only allow updating specific fields
    from phonos_app.helpers.custom_endpoint_runner import (
        ensure_byom_schema,
        sanitize_custom_headers,
        seal_endpoint_key,
    )
    from phonos_app.helpers.model_providers import validate_endpoint_url
    ensure_byom_schema(db)

    allowed_fields = {
        'model_name', 'model_description', 'input_schema', 'output_format',
        'outline_color', 'badge', 'badge_color', 'credit_cost',
        'estimated_runtime_seconds', 'worker_endpoint_url', 'is_active', 'is_public',
        'endpoint_mode', 'auth_type', 'auth_header_name', 'request_template_json',
        'response_output_path', 'upstream_model_name',
    }

    # Filter updates to allowed fields
    filtered_updates = {k: v for k, v in updates.items() if k in allowed_fields}
    if 'custom_headers' in updates:
        filtered_updates['custom_headers_json'] = json.dumps(sanitize_custom_headers(updates['custom_headers']))
    if updates.get('clear_upstream_key'):
        filtered_updates['upstream_api_key_enc'] = ''
    elif updates.get('upstream_api_key') and str(updates['upstream_api_key']).strip():
        filtered_updates['upstream_api_key_enc'] = seal_endpoint_key(str(updates['upstream_api_key']))
    if 'worker_endpoint_url' in filtered_updates and filtered_updates['worker_endpoint_url']:
        filtered_updates['worker_endpoint_url'] = validate_endpoint_url(str(filtered_updates['worker_endpoint_url']), allow_private=True)

    if not filtered_updates:
        return False

    # Validation
    if 'input_schema' in filtered_updates:
        if not isinstance(filtered_updates['input_schema'], list):
            raise ValueError("input_schema must be a list")

    if 'model_name' in filtered_updates:
        if not filtered_updates['model_name'] or not filtered_updates['model_name'].strip():
            raise ValueError("model_name cannot be empty")

    # Custom models remain fixed-price regardless of incoming payload.
    if 'credit_cost' in filtered_updates:
        filtered_updates['credit_cost'] = CUSTOM_MODEL_CREDIT_COST

    # Build UPDATE query
    set_clauses = []
    params = []

    for field, value in filtered_updates.items():
        if field == 'input_schema' and isinstance(value, (list, dict)):
            set_clauses.append(f"{field} = %s")
            params.append(json.dumps(value))
        else:
            set_clauses.append(f"{field} = %s")
            params.append(value)

    params.extend([model_id, user_id])

    query = f"""
        UPDATE user_models
        SET {', '.join(set_clauses)}
        WHERE id = %s AND user_id = %s
        RETURNING id
    """

    result = db.execute(query, tuple(params))
    updated = result.fetchone() is not None
    db.commit()

    return updated


def delete_user_model(model_id: int, user_id: int) -> bool:
    """
    Delete a user model.

    Args:
        model_id: The model's ID
        user_id: The user's ID (for permission checking)

    Returns:
        bool: True if deleted, False otherwise
    """
    db = get_db()

    result = db.execute("""
        DELETE FROM user_models
        WHERE id = %s AND user_id = %s
        RETURNING id
    """, (model_id, user_id))

    deleted = result.fetchone() is not None
    db.commit()

    return deleted


def increment_model_usage(model_id: int):
    """
    Increment the usage counter for a model.

    Args:
        model_id: The model's ID
    """
    db = get_db()

    db.execute("""
        UPDATE user_models
        SET total_jobs_run = total_jobs_run + 1,
            last_used_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (model_id,))

    db.commit()


# ============================================================================
# User Model API Keys
# ============================================================================

def create_model_api_key(
    user_id: int,
    name: str,
    user_model_id: Optional[int] = None,
    project_id: Optional[int] = None,
    rate_limit_per_minute: int = 60,
    key_type: str = 'model',
    encryption_enabled: bool = True,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new API key for a user model or project with mandatory encryption.

    Args:
        user_id: The user's ID
        name: Friendly name for the API key
        user_model_id: Optional model ID
        project_id: Optional project ID
        rate_limit_per_minute: Rate limit
        key_type: Type of key ('model', 'project', 'worker')
        encryption_enabled: Whether to enable encryption for this key's communications
        description: Optional note for key ownership/purpose

    Returns:
        dict with api_key (full key), key_id, encryption_enabled, and metadata
    """
    if user_model_id is None and project_id is None:
        raise ValueError("Either user_model_id or project_id must be provided")

    db = get_db()

    normalized_name = str(name or '').strip()
    if not normalized_name:
        raise ValueError("API key name is required")

    # Generate key material via compatibility wrapper so existing call sites/tests can patch it.
    api_key, key_hash, key_prefix, key_id = generate_api_key(key_type)
    normalized_description = str(description or '').strip() or None

    # Always encrypt the full key for storage (mandatory encryption)
    try:
        encrypted_key = encrypt_api_key_for_storage(api_key)
        logger.info(f"API key encrypted successfully for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to encrypt API key: {e}")
        raise ValueError("Failed to create encrypted API key - encryption is mandatory")

    # Store in database with encryption_enabled flag
    result = db.execute("""
        INSERT INTO user_model_api_keys (
            user_model_id, project_id, user_id, key_id, key_hash,
            key_prefix, name, description, is_active, rate_limit_per_minute, encrypted_key,
            encryption_enabled
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s)
        RETURNING id, created_at
    """, (
        user_model_id, project_id, user_id, key_id, key_hash,
        key_prefix, normalized_name, normalized_description, rate_limit_per_minute, encrypted_key,
        encryption_enabled
    ))

    row = result.fetchone()
    db.commit()

    return {
        'key_id': row['id'],
        'full_key': api_key,
        'key_prefix': key_prefix,
        'name': normalized_name,
        'description': normalized_description,
        'encryption_enabled': encryption_enabled,
        'created_at': _serialize_datetime(row['created_at'])
    }


def get_decrypted_api_key(key_id: int, user_id: int) -> Optional[str]:
    """
    Get the decrypted full API key using the new crypto module.

    Args:
        key_id: The API key's database ID
        user_id: The user's ID (for authorization)

    Returns:
        The decrypted API key string, or None if not found/authorized
    """
    db = get_db()

    # Fetch the encrypted key
    row = db.execute("""
        SELECT encrypted_key FROM user_model_api_keys
        WHERE id = %s AND user_id = %s
    """, (key_id, user_id)).fetchone()

    if not row or not row['encrypted_key']:
        return None

    try:
        # Use the new crypto module for decryption
        decrypted_key = decrypt_api_key_from_storage(row['encrypted_key'])
        return decrypted_key
    except Exception as e:
        logger.error(f"Failed to decrypt API key {key_id}: {e}")
        return None


def verify_model_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """
    Verify a model API key and return associated info including encryption settings.

    Args:
        api_key: The API key to verify

    Returns:
        dict with user_id, model_id, project_id, encryption_enabled if valid, None otherwise
    """
    db = get_db()

    # Hash the provided key using the crypto module
    key_hash = crypto_hash_api_key(api_key)

    result = db.execute("""
        SELECT id, user_model_id, project_id, user_id, rate_limit_per_minute,
               encryption_enabled
        FROM user_model_api_keys
        WHERE key_hash = %s AND is_active = true
    """, (key_hash,))

    row = result.fetchone()

    if row:
        # Update last_used_at and increment request counter
        db.execute("""
            UPDATE user_model_api_keys
            SET last_used_at = CURRENT_TIMESTAMP,
                total_requests = total_requests + 1
            WHERE id = %s
        """, (row['id'],))
        db.commit()

        return {
            'key_id': row['id'],
            'user_model_id': row['user_model_id'],
            'project_id': row['project_id'],
            'user_id': row['user_id'],
            'rate_limit': row['rate_limit_per_minute'],
            'encryption_enabled': _row_value(row, 'encryption_enabled', True),
            'api_key': api_key  # Include key for encryption operations
        }

    return None


def get_model_api_keys(user_id: int, user_model_id: Optional[int] = None, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get all API keys for a user's models or projects.

    Args:
        user_id: The user's ID
        user_model_id: Optional model ID to filter by
        project_id: Optional project ID to filter by

    Returns:
        list of API key metadata (without the actual keys)
    """
    db = get_db()

    query = """
        SELECT
            umak.id,
            umak.key_id,
            umak.key_prefix,
            umak.name,
            umak.description,
            umak.user_model_id,
            umak.project_id,
            umak.created_at,
            umak.last_used_at,
            umak.is_active,
            umak.total_requests,
            umak.rate_limit_per_minute,
            umak.encryption_enabled,
            um.model_name
        FROM user_model_api_keys umak
        LEFT JOIN user_models um ON umak.user_model_id = um.id
        WHERE umak.user_id = %s
    """

    params = [user_id]

    if user_model_id is not None:
        query += " AND umak.user_model_id = %s"
        params.append(user_model_id)

    if project_id is not None:
        query += " AND umak.project_id = %s"
        params.append(project_id)

    query += " ORDER BY umak.created_at DESC"

    try:
        result = db.execute(query, tuple(params))
    except Exception:
        # Backwards compatibility for databases not yet migrated with encryption metadata.
        fallback_query = """
            SELECT
                umak.id,
                umak.key_id,
                umak.key_prefix,
                umak.name,
                umak.description,
                umak.user_model_id,
                umak.project_id,
                umak.created_at,
                umak.last_used_at,
                umak.is_active,
                umak.total_requests,
                umak.rate_limit_per_minute,
                um.model_name
            FROM user_model_api_keys umak
            LEFT JOIN user_models um ON umak.user_model_id = um.id
            WHERE umak.user_id = %s
        """
        fallback_params = [user_id]
        if user_model_id is not None:
            fallback_query += " AND umak.user_model_id = %s"
            fallback_params.append(user_model_id)
        if project_id is not None:
            fallback_query += " AND umak.project_id = %s"
            fallback_params.append(project_id)
        fallback_query += " ORDER BY umak.created_at DESC"
        result = db.execute(fallback_query, tuple(fallback_params))

    keys = []
    for row in result.fetchall():
        key_prefix = row['key_prefix'] or (row['key_id'][:8] if row['key_id'] else 'mk_')

        keys.append({
            'id': row['id'],
            'key_prefix': key_prefix,
            'name': row['name'],
            'description': _row_value(row, 'description'),
            'user_model_id': row['user_model_id'],
            'project_id': row['project_id'],
            'model_name': row['model_name'],
            'created_at': _serialize_datetime(row['created_at']),
            'last_used_at': _serialize_datetime(row['last_used_at']),
            'is_active': bool(row['is_active']),
            'total_requests': row['total_requests'] or 0,
            'rate_limit_per_minute': _row_value(row, 'rate_limit_per_minute', 60) or 60,
            'encryption_enabled': bool(_row_value(row, 'encryption_enabled', True)),
        })

    return keys
