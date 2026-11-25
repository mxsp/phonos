"""
Security enhancements for Phonos platform.
Includes CSRF protection, rate limiting, input sanitization, and security headers.
"""
from __future__ import annotations

import os
import re
import secrets
import html
import bleach
from functools import wraps
from datetime import datetime, timedelta
from typing import Optional, Callable

from flask import current_app, request, jsonify, session, g
from werkzeug.exceptions import TooManyRequests

from .database import get_db


# ==================== CSRF Protection ====================

def generate_csrf_token() -> str:
    """Generate a CSRF token for the current session."""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


def validate_csrf_token(token: str) -> bool:
    """Validate a CSRF token against the session token."""
    expected = session.get('csrf_token')
    if not token or not expected:
        return False
    return secrets.compare_digest(str(expected), str(token))


def _csrf_token_from_request() -> str:
    """Return a CSRF token from supported form fields or headers."""
    return (
        request.form.get('csrf_token')
        or request.headers.get('X-CSRF-Token')
        or request.headers.get('X-CSRFToken')
    )


def csrf_protect(f: Callable) -> Callable:
    """
    Decorator to protect routes from CSRF attacks.
    Validates CSRF token for POST, PUT, PATCH, DELETE requests.

    Skips CSRF validation for requests authenticated via the kernel
    notebook-session token (``X-Notebook-Session``) — those calls
    originate from inside a sandboxed kernel that does not share the
    browser's CSRF cookie, and they are already gated by an opaque
    server-issued session token.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_app.config.get('TESTING'):
            return f(*args, **kwargs)

        if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            if getattr(g, 'is_notebook_request', False):
                return f(*args, **kwargs)

            token = _csrf_token_from_request()

            if not validate_csrf_token(token):
                if request.path.startswith('/api/'):
                    return jsonify({
                        'status': 'error',
                        'message': 'CSRF token validation failed.'
                    }), 403
                return 'CSRF token validation failed.', 403

        return f(*args, **kwargs)

    return decorated_function


# ==================== Rate Limiting ====================

def _ensure_rate_limits_table() -> None:
    """Create the rate limit table when the active database has not been migrated yet."""
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            id SERIAL PRIMARY KEY,
            identifier TEXT NOT NULL,
            action TEXT NOT NULL,
            attempts INTEGER DEFAULT 1,
            window_start TIMESTAMP NOT NULL,
            last_attempt TIMESTAMP NOT NULL,
            UNIQUE(identifier, action)
        )
    ''')
    db.commit()


def _ensure_account_lockouts_table() -> None:
    """Create the account lockout table when the active database has not been migrated yet."""
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS account_lockouts (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            failed_attempts INTEGER DEFAULT 1,
            first_failed_at TIMESTAMP NOT NULL,
            last_failed_at TIMESTAMP NOT NULL,
            locked_until TIMESTAMP,
            UNIQUE(username, ip_address)
        )
    ''')
    db.commit()


def _coerce_datetime(value, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return fallback
    return fallback


class RateLimiter:
    """Simple in-memory rate limiter using database storage."""

    @staticmethod
    def is_rate_limited(
        identifier: str,
        limit: int = 5,
        window_seconds: int = 300,
        action: str = 'general'
    ) -> tuple[bool, Optional[int]]:
        """
        Check if an identifier is rate limited.

        Args:
            identifier: Unique identifier (IP, user_id, etc.)
            limit: Maximum attempts within window
            window_seconds: Time window in seconds
            action: Action type (login, register, api_call, etc.)

        Returns:
            Tuple of (is_limited, retry_after_seconds)
        """
        _ensure_rate_limits_table()
        db = get_db()
        now = datetime.utcnow()
        window_start = now - timedelta(seconds=window_seconds)

        # Check current attempts
        record = db.execute(
            'SELECT attempts, window_start, last_attempt FROM rate_limits '
            'WHERE identifier = %s AND action = %s',
            (identifier, action)
        ).fetchone()

        if not record:
            # First attempt - create record
            db.execute(
                'INSERT INTO rate_limits (identifier, action, attempts, window_start, last_attempt) '
                'VALUES (%s, %s, 1, %s, %s)',
                (identifier, action, now, now)
            )
            db.commit()
            return False, None

        # Handle both string and datetime types from database
        window_start_db = record['window_start']
        window_start_db = _coerce_datetime(window_start_db, now)

        attempts = record['attempts']

        # Check if window has expired
        if window_start_db < window_start:
            # Reset window
            db.execute(
                'UPDATE rate_limits SET attempts = 1, window_start = %s, last_attempt = %s '
                'WHERE identifier = %s AND action = %s',
                (now, now, identifier, action)
            )
            db.commit()
            return False, None

        # Increment attempts
        if attempts >= limit:
            # Rate limited
            last_attempt = record['last_attempt']
            last_attempt = _coerce_datetime(last_attempt, now)
            window_end = window_start_db + timedelta(seconds=window_seconds)
            retry_after = int((window_end - now).total_seconds())
            return True, max(retry_after, 1)

        # Increment attempt counter
        db.execute(
            'UPDATE rate_limits SET attempts = attempts + 1, last_attempt = %s '
            'WHERE identifier = %s AND action = %s',
            (now, identifier, action)
        )
        db.commit()

        return False, None

    @staticmethod
    def reset_rate_limit(identifier: str, action: str = 'general'):
        """Reset rate limit for an identifier and action."""
        _ensure_rate_limits_table()
        db = get_db()

        db.execute(
            'DELETE FROM rate_limits WHERE identifier = %s AND action = %s',
            (identifier, action)
        )
        db.commit()


def rate_limit(limit: int = 5, window: int = 300, action: str = 'general'):
    """
    Decorator to rate limit route access.

    Args:
        limit: Maximum attempts within window
        window: Time window in seconds
        action: Action type for tracking
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Use IP address as identifier, or user_id if authenticated
            identifier = session.get('user_id', request.remote_addr)
            identifier_str = f"{identifier}_{request.remote_addr}"

            is_limited, retry_after = RateLimiter.is_rate_limited(
                identifier_str, limit, window, action
            )

            if is_limited:
                if request.path.startswith('/api/'):
                    return jsonify({
                        'status': 'error',
                        'message': f'Too many requests. Please try again in {retry_after} seconds.',
                        'retry_after': retry_after
                    }), 429
                raise TooManyRequests(
                    description=f'Too many requests. Please try again in {retry_after} seconds.'
                )

            return f(*args, **kwargs)

        return decorated_function

    return decorator


# ==================== Account Lockout ====================

class AccountLockout:
    """Track and manage account lockouts after failed login attempts."""

    @staticmethod
    def record_failed_login(username: str, ip_address: str):
        """Record a failed login attempt."""
        _ensure_account_lockouts_table()
        db = get_db()
        now = datetime.utcnow()

        record = db.execute(
            'SELECT * FROM account_lockouts WHERE username = %s AND ip_address = %s',
            (username, ip_address)
        ).fetchone()

        if not record:
            db.execute(
                'INSERT INTO account_lockouts (username, ip_address, failed_attempts, first_failed_at, last_failed_at) '
                'VALUES (%s, %s, 1, %s, %s)',
                (username, ip_address, now, now)
            )
        else:
            failed_attempts = record['failed_attempts'] + 1
            first_failed_at = _coerce_datetime(record['first_failed_at'], now)

            # Reset counter if last failed attempt was more than 1 hour ago
            one_hour_ago = now - timedelta(hours=1)
            if first_failed_at < one_hour_ago:
                failed_attempts = 1
                first_failed_at = now

            # Lock account after 5 failed attempts
            locked_until = None
            if failed_attempts >= 5:
                locked_until = now + timedelta(minutes=15)

            db.execute(
                'UPDATE account_lockouts SET failed_attempts = %s, first_failed_at = %s, '
                'last_failed_at = %s, locked_until = %s WHERE username = %s AND ip_address = %s',
                (failed_attempts, first_failed_at, now, locked_until, username, ip_address)
            )

        db.commit()

    @staticmethod
    def is_locked(username: str, ip_address: str) -> tuple[bool, Optional[int]]:
        """
        Check if an account is locked.

        Returns:
            Tuple of (is_locked, minutes_remaining)
        """
        _ensure_account_lockouts_table()
        db = get_db()
        now = datetime.utcnow()

        record = db.execute(
            'SELECT locked_until FROM account_lockouts WHERE username = %s AND ip_address = %s',
            (username, ip_address)
        ).fetchone()

        if not record or not record['locked_until']:
            return False, None

        locked_until = _coerce_datetime(record['locked_until'], now)

        if locked_until > now:
            minutes_remaining = int((locked_until - now).total_seconds() / 60) + 1
            return True, minutes_remaining

        # Lockout expired - clear it
        db.execute(
            'UPDATE account_lockouts SET locked_until = NULL, failed_attempts = 0 '
            'WHERE username = %s AND ip_address = %s',
            (username, ip_address)
        )
        db.commit()

        return False, None

    @staticmethod
    def clear_lockout(username: str, ip_address: str):
        """Clear lockout after successful login."""
        _ensure_account_lockouts_table()
        db = get_db()

        db.execute(
            'DELETE FROM account_lockouts WHERE username = %s AND ip_address = %s',
            (username, ip_address)
        )
        db.commit()


# ==================== Input Sanitization ====================

def sanitize_html(text: str, allowed_tags: Optional[list[str]] = None) -> str:
    """
    Sanitize HTML input to prevent XSS attacks.

    Args:
        text: Input text
        allowed_tags: List of allowed HTML tags (default: safe tags only)
    """
    if allowed_tags is None:
        allowed_tags = ['b', 'i', 'u', 'em', 'strong', 'a', 'p', 'br', 'ul', 'ol', 'li', 'code', 'pre']

    allowed_attrs = {
        'a': ['href', 'title', 'rel'],
        'img': ['src', 'alt', 'title']
    }

    return bleach.clean(
        text,
        tags=allowed_tags,
        attributes=allowed_attrs,
        strip=True
    )


def sanitize_input(text: str, max_length: int = 10000) -> str:
    """
    Sanitize general text input.
    Removes dangerous characters and limits length.
    """
    if not text:
        return ''

    # Limit length
    text = text[:max_length]

    # Escape HTML entities
    text = html.escape(text)

    return text.strip()


def validate_smiles(smiles: str) -> bool:
    """
    Validate SMILES string for molecular structures.
    Prevents injection attacks through chemical notation.
    """
    if not smiles:
        return False

    # Basic SMILES validation - only allow safe characters
    # SMILES use: C, N, O, S, P, F, Cl, Br, I, H, +, -, =, #, (, ), [, ], @, %, numbers
    safe_pattern = r'^[A-Za-z0-9\[\]()=#@+\-\\\/\.%]+$'

    if not re.match(safe_pattern, smiles):
        return False

    # Limit length
    if len(smiles) > 1000:
        return False

    return True


def sanitize_filename_secure(filename: str) -> str:
    """
    Enhanced filename sanitization beyond werkzeug.secure_filename.
    """
    import uuid
    from werkzeug.utils import secure_filename

    # Remove null bytes and special chars
    filename = filename.replace('\x00', '')

    # Use werkzeug's secure_filename
    filename = secure_filename(filename)

    # Remove path traversal attempts
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')

    # Limit length
    name, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
    if len(name) > 200:
        name = name[:200]

    # Add unique prefix to prevent collisions
    unique_id = str(uuid.uuid4())[:8]

    return f"{unique_id}_{name}.{ext}" if ext else f"{unique_id}_{name}"


# ==================== Session Security ====================

def regenerate_session():
    """Regenerate session ID to prevent session fixation attacks."""
    # Store current session data
    data = dict(session)

    # Clear and regenerate
    session.clear()
    session.permanent = True

    # Restore data
    for key, value in data.items():
        session[key] = value

    # Generate new CSRF token
    generate_csrf_token()


# ==================== Security Headers ====================

def apply_security_headers(response):
    """Apply security headers to response."""
    try:
        from config import Config
    except ImportError:  # pragma: no cover - depends on runtime import path
        from production.config import Config

    # Apply headers from config
    for header, value in Config.SECURE_HEADERS.items():
        if header.lower() == 'content-security-policy':
            enforce_csp = (
                current_app.config.get('ENABLE_CONTENT_SECURITY_POLICY')
                or os.environ.get('ENABLE_CONTENT_SECURITY_POLICY', 'false').lower() == 'true'
            )
            if enforce_csp:
                response.headers.setdefault(header, value)
            else:
                response.headers.setdefault('Content-Security-Policy-Report-Only', value)
            continue
        response.headers.setdefault(header, value)

    # Additional headers
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # Copilot Live needs the microphone on its two surfaces; everywhere else it
    # stays disabled. (The browser still asks the user for permission.)
    permissions = 'geolocation=(), camera=()'
    if request.path in ('/dashboard', '/agent'):
        permissions += ', microphone=(self)'
    else:
        permissions += ', microphone=()'
    response.headers.setdefault('Permissions-Policy', permissions)
    response.headers.setdefault('X-Permitted-Cross-Domain-Policies', 'none')

    # CORS compatibility for API endpoints across canonical Phonos domains.
    # This keeps same-site API calls working even when the app is reached via
    # either the .com or .de hostname through proxies.
    if request.path.startswith('/api/'):
        origin = request.headers.get('Origin')
        if origin:
            vary = response.headers.get('Vary', '')
            if 'Origin' not in vary:
                response.headers['Vary'] = (f'{vary}, Origin'.strip(', ')).strip()

            request_origin = f"{request.scheme}://{request.host}"
            allowed_origins = {
                request_origin,
                'https://phonos.fyreflysystems.com',
                'https://phonos.fyreflysystems.de',
            }
            configured_origins = (
                current_app.config.get('CORS_ALLOWED_ORIGINS')
                or os.environ.get('CORS_ALLOWED_ORIGINS', '')
            )
            if isinstance(configured_origins, str):
                allowed_origins.update(
                    item.strip().rstrip('/')
                    for item in configured_origins.split(',')
                    if item.strip()
                )
            else:
                allowed_origins.update(str(item).rstrip('/') for item in configured_origins if item)

            if current_app.config.get('TESTING') or current_app.config.get('DEV_MODE'):
                allowed_origins.update({
                    'http://localhost:5000',
                    'http://127.0.0.1:5000',
                })

            if origin.rstrip('/') in allowed_origins:
                response.headers['Access-Control-Allow-Origin'] = origin
                response.headers['Access-Control-Allow-Credentials'] = 'true'
                response.headers['Access-Control-Allow-Headers'] = (
                    'Content-Type, Authorization, X-CSRF-Token, x-csrf-token, X-Requested-With, X-Notebook-Session'
                )
                response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, OPTIONS'

    return response


# ==================== API Input Sanitization Middleware ====================

def sanitize_json_inputs(fields: dict[str, str]) -> Callable:
    """
    Decorator to automatically sanitize JSON request inputs.

    Args:
        fields: Dictionary mapping field names to sanitization types
                'html' - Allow safe HTML tags
                'text' - Plain text, HTML escaped
                'smiles' - SMILES string validation
                'filename' - Secure filename
                'integer' - Convert to integer
                'float' - Convert to float
                'boolean' - Convert to boolean

    Example:
        @sanitize_json_inputs({'name': 'text', 'description': 'html', 'smiles': 'smiles'})
        def create_project():
            data = request.get_json()
            # data['name'] and data['description'] are now sanitized
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.is_json:
                data = request.get_json()

                if data and isinstance(data, dict):
                    sanitized_data = {}

                    for key, value in data.items():
                        if key not in fields:
                            # Pass through unspecified fields
                            sanitized_data[key] = value
                            continue

                        sanitize_type = fields[key]

                        if value is None:
                            sanitized_data[key] = None
                            continue

                        # Apply sanitization based on type
                        try:
                            if sanitize_type == 'html':
                                sanitized_data[key] = sanitize_html(str(value))
                            elif sanitize_type == 'text':
                                sanitized_data[key] = sanitize_input(str(value))
                            elif sanitize_type == 'smiles':
                                if validate_smiles(str(value)):
                                    sanitized_data[key] = str(value)
                                else:
                                    return jsonify({
                                        'status': 'error',
                                        'message': f'Invalid SMILES string in field: {key}'
                                    }), 400
                            elif sanitize_type == 'filename':
                                sanitized_data[key] = sanitize_filename_secure(str(value))
                            elif sanitize_type == 'integer':
                                sanitized_data[key] = int(value)
                            elif sanitize_type == 'float':
                                sanitized_data[key] = float(value)
                            elif sanitize_type == 'boolean':
                                sanitized_data[key] = bool(value)
                            else:
                                sanitized_data[key] = value

                        except (ValueError, TypeError) as e:
                            return jsonify({
                                'status': 'error',
                                'message': f'Invalid value for field {key}: {str(e)}'
                            }), 400

                    # Replace request JSON with sanitized data
                    request._cached_json = (sanitized_data, sanitized_data)

            return f(*args, **kwargs)

        return decorated_function

    return decorator


# ==================== Path Traversal Prevention ====================

def prevent_path_traversal(path: str, base_dir: str) -> bool:
    """
    Check if a path is safe and doesn't attempt directory traversal.

    Args:
        path: The path to check
        base_dir: The base directory that should contain the path

    Returns:
        True if safe, False if traversal attempt detected
    """
    import os

    # Resolve both paths to absolute
    abs_base = os.path.abspath(base_dir)
    abs_path = os.path.abspath(os.path.join(base_dir, path))

    # Check if the absolute path starts with the base directory
    return abs_path.startswith(abs_base)


# ==================== SQL Injection Prevention ====================

def validate_sql_identifier(identifier: str) -> bool:
    """
    Validate that a string is safe to use as a SQL identifier (table/column name).
    Only allows alphanumeric characters and underscores.
    """
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier))


# ==================== Additional Security Utilities ====================

def is_safe_redirect_url(url: str) -> bool:
    """
    Check if a URL is safe for redirect (prevents open redirect vulnerability).
    Only allows relative URLs or URLs to the same domain.
    """
    if not url:
        return False

    # Allow relative URLs
    if url.startswith('/'):
        return not url.startswith('//')

    # Reject absolute URLs to other domains
    return False


def generate_secure_token(length: int = 32) -> str:
    """Generate a cryptographically secure random token."""
    import secrets
    return secrets.token_urlsafe(length)


def hash_api_key(api_key: str) -> str:
    """Hash an API key for secure storage."""
    import hashlib
    return hashlib.sha256(api_key.encode()).hexdigest()
