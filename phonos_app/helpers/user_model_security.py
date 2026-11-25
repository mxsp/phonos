"""
Security helpers for User Models Bridge API

Provides:
- Output data sanitization (remove sensitive fields)
- API-key-based encryption/decryption
- Field filtering for different output formats
"""

import base64
import hashlib
import logging
from typing import Any, Dict, Optional
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Fields to always remove from output data
SENSITIVE_FIELDS = {
    'password', 'api_key', 'secret', 'token', 'private_key',
    'encryption_key', 'access_token', 'refresh_token',
    'internal_id', 'system_path', 'db_connection', 'redis_url',
    'user_id', 'creator_id', 'owner_id'  # Internal IDs
}

# Fields to keep for different output formats
FORMAT_ALLOWED_FIELDS = {
    'json': {'*'},  # Allow all non-sensitive fields
    'pdb': {'structure_pdb', 'structure', 'confidence_scores', 'plddt', 'model_quality'},
    'cif': {'structure_cif', 'structure', 'confidence_scores', 'unit_cell', 'space_group'},
    'sdf': {'structure_sdf', 'molecules', 'ligands', 'binding_poses', 'scores'},
    'mol': {'structure_mol', 'molecule', 'smiles', 'formula'},
    'mol2': {'structure_mol2', 'molecule', 'charges'},
    'png': {'image_base64', 'image_data', 'width', 'height', 'format'},
    'jpg': {'image_base64', 'image_data', 'width', 'height', 'format'},
    'txt': {'text', 'content', 'data'},
    'csv': {'csv_data', 'data', 'rows'},
    'zip': {'archive_base64', 'files', 'artifacts'}
}


def _is_metadata_key(key: str) -> bool:
    return key.startswith('_') or key in {'metrics', 'metadata', 'timestamp', 'version'}


def sanitize_output_data(data: Dict[str, Any], output_format: str = 'json') -> Dict[str, Any]:
    """
    Remove sensitive fields and filter based on output format.

    Args:
        data: Result data from worker
        output_format: Expected output format (pdb, json, png, etc.)

    Returns:
        Sanitized dictionary with only allowed fields
    """
    if not isinstance(data, dict):
        return data

    # Get allowed fields for this format
    allowed_fields = FORMAT_ALLOWED_FIELDS.get(output_format, {'*'})

    sanitized = {}

    for key, value in data.items():
        # Skip sensitive fields
        if any(sensitive in key.lower() for sensitive in SENSITIVE_FIELDS):
            logger.warning(f"Removing sensitive field from output: {key}")
            continue

        # Recursively sanitize nested dicts before format filtering.
        # Container keys (dict/list values) are always allowed to pass
        # through so format-specific data wrapped inside a result object
        # (e.g. {"result": {"structure_pdb": ...}}) is not lost. Format
        # filtering only applies at the leaf level.
        if isinstance(value, dict):
            sanitized_nested = sanitize_output_data(value, output_format)
            if sanitized_nested or _is_metadata_key(key):
                sanitized[key] = sanitized_nested
            continue
        elif isinstance(value, list):
            sanitized_list = [
                sanitize_output_data(item, output_format) if isinstance(item, dict) else item
                for item in value
            ]
            sanitized[key] = sanitized_list
            continue

        # Filter leaf values by format if not wildcard
        if allowed_fields != {'*'} and key not in allowed_fields and not _is_metadata_key(key):
            logger.debug(f"Filtering field '{key}' for format '{output_format}'")
            continue

        sanitized[key] = value

    return sanitized


def derive_encryption_key_from_api_key(api_key: str) -> bytes:
    """
    Derive a Fernet-compatible encryption key from the API key.

    Uses PBKDF2-HMAC-SHA256 to derive a 32-byte key from the API key.
    The salt is deterministic (derived from the key itself) so the same
    API key always produces the same encryption key.

    Args:
        api_key: The worker's API key

    Returns:
        32-byte key suitable for Fernet encryption
    """
    # Use first 16 bytes of SHA256 hash as salt (deterministic)
    # This ensures the same API key always derives the same encryption key
    salt = hashlib.sha256(api_key.encode('utf-8')).digest()[:16]

    # Derive 32-byte key using PBKDF2
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,  # OWASP recommendation
        backend=default_backend()
    )

    key_bytes = kdf.derive(api_key.encode('utf-8'))

    # Fernet requires base64-encoded key
    return base64.urlsafe_b64encode(key_bytes)


def encrypt_with_api_key(data: bytes, api_key: str) -> Optional[bytes]:
    """
    Encrypt data using API key-derived encryption key.

    Args:
        data: Plaintext data to encrypt
        api_key: Worker's API key

    Returns:
        Encrypted data or None if encryption fails
    """
    try:
        encryption_key = derive_encryption_key_from_api_key(api_key)
        fernet = Fernet(encryption_key)
        return fernet.encrypt(data)
    except Exception as e:
        logger.error(f"Encryption with API key failed: {e}")
        return None


def decrypt_with_api_key(encrypted_data: bytes, api_key: str) -> Optional[bytes]:
    """
    Decrypt data using API key-derived encryption key.

    Args:
        encrypted_data: Encrypted data
        api_key: Worker's API key

    Returns:
        Decrypted data or None if decryption fails
    """
    try:
        encryption_key = derive_encryption_key_from_api_key(api_key)
        fernet = Fernet(encryption_key)
        return fernet.decrypt(encrypted_data)
    except Exception as e:
        logger.error(f"Decryption with API key failed: {e}")
        return None


def sanitize_job_input_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove sensitive fields from job input data before sending to worker.

    Workers should never receive:
    - User IDs or internal database IDs
    - System paths or configuration
    - Other users' data

    Args:
        data: Input data dictionary

    Returns:
        Sanitized input data
    """
    if not isinstance(data, dict):
        return data

    sanitized = {}

    for key, value in data.items():
        # Skip sensitive fields
        if any(sensitive in key.lower() for sensitive in SENSITIVE_FIELDS):
            continue

        # Recursively sanitize nested dicts
        if isinstance(value, dict):
            sanitized[key] = sanitize_job_input_data(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_job_input_data(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            sanitized[key] = value

    return sanitized
