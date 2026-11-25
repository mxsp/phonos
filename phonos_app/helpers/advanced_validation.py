"""
Advanced Request Validation and Sanitization
Provides comprehensive input validation, schema validation, and advanced sanitization.
"""
from __future__ import annotations

import re
import json
from typing import Any, Dict, List, Optional, Callable
from functools import wraps
from flask import request, jsonify

from .audit_log import log_audit_event, AuditEventType


class ValidationError(Exception):
    """Custom validation error."""
    pass


class SchemaValidator:
    """JSON Schema-like validation for request data."""

    TYPES = {
        'string': str,
        'integer': int,
        'number': (int, float),
        'boolean': bool,
        'array': list,
        'object': dict
    }

    @classmethod
    def validate(cls, data: Any, schema: Dict) -> tuple[bool, Optional[str]]:
        """
        Validate data against a schema.

        Schema format:
        {
            'type': 'string',  # Required
            'required': True,  # Optional
            'min_length': 5,   # For strings
            'max_length': 100, # For strings
            'min': 0,          # For numbers
            'max': 100,        # For numbers
            'pattern': r'^[a-z]+$',  # Regex for strings
            'enum': ['val1', 'val2'],  # Allowed values
            'items': {...},    # Schema for array items
            'properties': {...}  # Schema for object properties
        }
        """
        # Check required
        if schema.get('required', False) and data is None:
            return False, "Field is required"

        if data is None:
            return True, None

        # Check type
        expected_type = schema.get('type')
        if expected_type:
            type_class = cls.TYPES.get(expected_type)
            if not isinstance(data, type_class):
                return False, f"Expected type {expected_type}, got {type(data).__name__}"

        # String validations
        if isinstance(data, str):
            min_length = schema.get('min_length')
            if min_length and len(data) < min_length:
                return False, f"String must be at least {min_length} characters"

            max_length = schema.get('max_length')
            if max_length and len(data) > max_length:
                return False, f"String must not exceed {max_length} characters"

            pattern = schema.get('pattern')
            if pattern and not re.match(pattern, data):
                return False, f"String does not match required pattern"

        # Number validations
        if isinstance(data, (int, float)):
            min_val = schema.get('min')
            if min_val is not None and data < min_val:
                return False, f"Value must be at least {min_val}"

            max_val = schema.get('max')
            if max_val is not None and data > max_val:
                return False, f"Value must not exceed {max_val}"

        # Enum validation
        enum = schema.get('enum')
        if enum and data not in enum:
            return False, f"Value must be one of: {', '.join(map(str, enum))}"

        # Array validation
        if isinstance(data, list):
            items_schema = schema.get('items')
            if items_schema:
                for i, item in enumerate(data):
                    valid, error = cls.validate(item, items_schema)
                    if not valid:
                        return False, f"Item {i}: {error}"

        # Object validation
        if isinstance(data, dict):
            properties = schema.get('properties', {})
            for prop_name, prop_schema in properties.items():
                prop_data = data.get(prop_name)
                valid, error = cls.validate(prop_data, prop_schema)
                if not valid:
                    return False, f"Property '{prop_name}': {error}"

        return True, None


def validate_request_schema(schema: Dict) -> Callable:
    """
    Decorator to validate request JSON against a schema.

    Example:
        @app.route('/api/create-user', methods=['POST'])
        @validate_request_schema({
            'username': {'type': 'string', 'required': True, 'min_length': 3, 'max_length': 50},
            'email': {'type': 'string', 'required': True, 'pattern': r'^[\w\.-]+@[\w\.-]+\.\w+$'},
            'age': {'type': 'integer', 'min': 18, 'max': 120}
        })
        def create_user():
            data = request.get_json()
            # data is now validated
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not request.is_json:
                return jsonify({
                    'status': 'error',
                    'message': 'Content-Type must be application/json'
                }), 400

            data = request.get_json()

            # Validate each field in schema
            errors = {}
            for field_name, field_schema in schema.items():
                field_data = data.get(field_name)
                valid, error = SchemaValidator.validate(field_data, field_schema)

                if not valid:
                    errors[field_name] = error

            if errors:
                log_audit_event(
                    AuditEventType.INVALID_INPUT,
                    f"Request validation failed: {request.path}",
                    result='rejected',
                    severity='warning',
                    details={'errors': errors}
                )
                return jsonify({
                    'status': 'error',
                    'message': 'Validation failed',
                    'errors': errors
                }), 400

            return f(*args, **kwargs)

        return decorated_function

    return decorator


class AdvancedSanitizer:
    """Advanced input sanitization beyond basic HTML escaping."""

    @staticmethod
    def sanitize_sql_identifier(identifier: str) -> str:
        """
        Sanitize SQL identifiers (table/column names).
        Only allows alphanumeric and underscore.
        """
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier):
            raise ValidationError(f"Invalid SQL identifier: {identifier}")
        return identifier

    @staticmethod
    def sanitize_file_path(path: str, base_dir: str) -> str:
        """
        Sanitize file paths to prevent directory traversal.
        Returns absolute path within base_dir.
        """
        import os

        # Remove null bytes
        path = path.replace('\x00', '')

        # Join with base directory
        abs_path = os.path.abspath(os.path.join(base_dir, path))
        abs_base = os.path.abspath(base_dir)

        # Ensure path is within base directory
        if not abs_path.startswith(abs_base):
            raise ValidationError("Path traversal detected")

        return abs_path

    @staticmethod
    def sanitize_email(email: str) -> str:
        """
        Validate and sanitize email addresses.
        """
        # Basic email pattern
        pattern = r'^[\w\.\-\+]+@[\w\.\-]+\.[a-zA-Z]{2,}$'

        if not re.match(pattern, email):
            raise ValidationError(f"Invalid email format: {email}")

        # Convert to lowercase
        email = email.lower().strip()

        # Check length
        if len(email) > 254:
            raise ValidationError("Email too long")

        return email

    @staticmethod
    def sanitize_url(url: str, allowed_schemes: List[str] = None) -> str:
        """
        Validate and sanitize URLs.
        """
        from urllib.parse import urlparse

        if allowed_schemes is None:
            allowed_schemes = ['http', 'https']

        parsed = urlparse(url)

        if parsed.scheme not in allowed_schemes:
            raise ValidationError(f"URL scheme must be one of: {', '.join(allowed_schemes)}")

        # Check for malicious patterns
        dangerous_patterns = [
            'javascript:',
            'data:',
            'vbscript:',
            'file:',
        ]

        url_lower = url.lower()
        for pattern in dangerous_patterns:
            if pattern in url_lower:
                raise ValidationError(f"Dangerous URL pattern detected: {pattern}")

        return url

    @staticmethod
    def sanitize_json(data: Any, max_depth: int = 10) -> Any:
        """
        Sanitize JSON data recursively.
        Prevents deeply nested structures (DoS attack).
        """
        def _sanitize(obj: Any, depth: int) -> Any:
            if depth > max_depth:
                raise ValidationError(f"JSON structure too deeply nested (max {max_depth})")

            if isinstance(obj, dict):
                return {
                    str(k): _sanitize(v, depth + 1)
                    for k, v in obj.items()
                }
            elif isinstance(obj, list):
                return [_sanitize(item, depth + 1) for item in obj]
            elif isinstance(obj, str):
                # Truncate very long strings
                return obj[:10000]
            else:
                return obj

        return _sanitize(data, 0)

    @staticmethod
    def sanitize_smiles(smiles: str) -> str:
        """
        Validate and sanitize SMILES chemical notation.
        """
        if not smiles:
            raise ValidationError("SMILES string is empty")

        # Check length
        if len(smiles) > 1000:
            raise ValidationError("SMILES string too long")

        # Only allow valid SMILES characters
        valid_pattern = r'^[A-Za-z0-9\[\]()=#@+\-\\\/\.%*]+$'
        if not re.match(valid_pattern, smiles):
            raise ValidationError("Invalid characters in SMILES string")

        return smiles.strip()

    @staticmethod
    def validate_pdb_has_protein(pdb_content: str) -> tuple[bool, Optional[str]]:
        """
        Validate that a PDB file contains protein structure with amino acids.

        Args:
            pdb_content: The PDB file content as a string

        Returns:
            Tuple of (is_valid, error_message)

        Raises:
            ValidationError: If PDB file is invalid or doesn't contain protein
        """
        if not pdb_content or not pdb_content.strip():
            return False, "PDB file is empty"

        # Standard amino acid three-letter codes
        amino_acids = {
            'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY',
            'HIS', 'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER',
            'THR', 'TRP', 'TYR', 'VAL',
            # Common modified amino acids
            'MSE', 'SEP', 'TPO', 'PTR', 'HYP', 'MLY', 'ALY'
        }

        has_atom_record = False
        has_amino_acid = False
        atom_count = 0

        for line in pdb_content.splitlines():
            # Check for ATOM or HETATM records
            if line.startswith('ATOM  ') or line.startswith('HETATM'):
                has_atom_record = True
                atom_count += 1

                # PDB format: residue name is at columns 17-20 (0-indexed: 17:20)
                # Format: "ATOM      1  N   ALA A   1    ..."
                if len(line) >= 20:
                    residue = line[17:20].strip()

                    if residue in amino_acids:
                        has_amino_acid = True
                        # Found at least one amino acid, validation passes
                        return True, None

        # If no ATOM records found at all
        if not has_atom_record:
            return False, "PDB file contains no ATOM or HETATM records. This may not be a valid protein structure file."

        # If ATOM records exist but no amino acids found
        if atom_count > 0 and not has_amino_acid:
            return False, (
                "PDB file contains no protein amino acids in ATOM records. "
                "DiffDock requires a protein structure (receptor) for molecular docking. "
                "Please ensure your PDB file contains protein residues (e.g., ALA, GLY, etc.), "
                "not just ligands, water molecules, or other non-protein molecules."
            )

        # Fallback - shouldn't reach here
        return False, "Unable to validate PDB structure"


def sanitize_query_params(allowed_params: List[str]) -> Callable:
    """
    Decorator to whitelist and sanitize query parameters.

    Example:
        @app.route('/api/search')
        @sanitize_query_params(['q', 'page', 'limit'])
        def search():
            # Only 'q', 'page', 'limit' will be in request.args
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Check for unexpected parameters
            actual_params = set(request.args.keys())
            expected_params = set(allowed_params)
            unexpected = actual_params - expected_params

            if unexpected:
                log_audit_event(
                    AuditEventType.INVALID_INPUT,
                    f"Unexpected query parameters: {unexpected}",
                    result='warning',
                    severity='info',
                    details={'unexpected': list(unexpected)}
                )

            return f(*args, **kwargs)

        return decorated_function

    return decorator


def validate_content_length(max_bytes: int) -> Callable:
    """
    Decorator to validate request content length.
    Prevents large payload DoS attacks.
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated_function(*args, **kwargs):
            content_length = request.content_length

            if content_length and content_length > max_bytes:
                log_audit_event(
                    AuditEventType.INVALID_INPUT,
                    f"Request too large: {content_length} bytes",
                    result='rejected',
                    severity='warning',
                    details={'size': content_length, 'max': max_bytes}
                )
                return jsonify({
                    'status': 'error',
                    'message': f'Request too large. Maximum size: {max_bytes} bytes'
                }), 413

            return f(*args, **kwargs)

        return decorated_function

    return decorator


class BiometricValidator:
    """Validate biometric-like patterns for additional security."""

    @staticmethod
    def validate_typing_pattern(pattern: Dict) -> bool:
        """
        Validate typing patterns for keystroke dynamics.
        Can detect bots vs humans based on typing speed.

        Pattern format:
        {
            'keystroke_timings': [120, 145, 89, ...],  # ms between keystrokes
            'total_time': 5432  # Total time to type
        }
        """
        timings = pattern.get('keystroke_timings', [])
        total_time = pattern.get('total_time', 0)

        if not timings or not total_time:
            return True  # Skip if no pattern provided

        # Check for impossibly fast typing (bot)
        avg_timing = sum(timings) / len(timings)
        if avg_timing < 20:  # < 20ms average is inhuman
            return False

        # Check for too consistent timing (bot)
        if len(timings) > 5:
            variance = sum((t - avg_timing) ** 2 for t in timings) / len(timings)
            if variance < 10:  # Too consistent
                return False

        return True

    @staticmethod
    def validate_mouse_movement(movements: List[Dict]) -> bool:
        """
        Validate mouse movement patterns.
        Bots often have linear/perfect movements.

        Movements format:
        [
            {'x': 100, 'y': 200, 'timestamp': 1234567890},
            ...
        ]
        """
        if not movements or len(movements) < 3:
            return True  # Skip if not enough data

        # Check for perfectly linear movements (bot indicator)
        for i in range(2, len(movements)):
            p1 = movements[i-2]
            p2 = movements[i-1]
            p3 = movements[i]

            # Calculate if points are perfectly collinear
            dx1 = p2['x'] - p1['x']
            dy1 = p2['y'] - p1['y']
            dx2 = p3['x'] - p2['x']
            dy2 = p3['y'] - p2['y']

            # Cross product (should be non-zero for non-linear path)
            cross = dx1 * dy2 - dy1 * dx2

            if abs(cross) < 0.1:  # Too linear
                return False

        return True


def require_human_verification(f: Callable) -> Callable:
    """
    Decorator requiring human verification (typing/mouse patterns).
    Optional - only validates if data is provided.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.is_json:
            data = request.get_json()
            bio_data = data.get('_biometric', {})

            # Validate typing pattern
            typing_pattern = bio_data.get('typing')
            if typing_pattern and not BiometricValidator.validate_typing_pattern(typing_pattern):
                log_audit_event(
                    AuditEventType.INVALID_INPUT,
                    "Suspicious typing pattern detected (possible bot)",
                    result='rejected',
                    severity='warning',
                    details={'path': request.path}
                )
                return jsonify({
                    'status': 'error',
                    'message': 'Verification failed. Please try again.'
                }), 403

            # Validate mouse movement
            mouse_movements = bio_data.get('mouse')
            if mouse_movements and not BiometricValidator.validate_mouse_movement(mouse_movements):
                log_audit_event(
                    AuditEventType.INVALID_INPUT,
                    "Suspicious mouse movement detected (possible bot)",
                    result='rejected',
                    severity='warning',
                    details={'path': request.path}
                )
                return jsonify({
                    'status': 'error',
                    'message': 'Verification failed. Please try again.'
                }), 403

        return f(*args, **kwargs)

    return decorated_function
