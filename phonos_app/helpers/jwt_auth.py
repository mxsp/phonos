"""
JWT Token Authentication for API Access
Provides stateless authentication alternative to session-based auth.
"""
from __future__ import annotations

import jwt
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from functools import wraps
from flask import request, jsonify, g

from .database import get_db
from .audit_log import log_audit_event, log_api_key_usage, AuditEventType


from config import Config

class JWTConfig:
    """JWT configuration."""

    # Get from environment in production
    SECRET_KEY = Config.SECRET_KEY
    ALGORITHM = 'HS256'
    ACCESS_TOKEN_EXPIRE_MINUTES = 60  # 1 hour
    REFRESH_TOKEN_EXPIRE_DAYS = 30  # 30 days
    ISSUER = 'phonos-api'


class JWTManager:
    """JWT token management."""

    @staticmethod
    def create_access_token(
        user_id: int,
        username: str,
        scopes: list[str] = None,
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """
        Create JWT access token.

        Args:
            user_id: User ID
            username: Username
            scopes: List of permission scopes
            expires_delta: Custom expiration time

        Returns:
            JWT token string
        """
        if expires_delta is None:
            expires_delta = timedelta(minutes=JWTConfig.ACCESS_TOKEN_EXPIRE_MINUTES)

        expire = datetime.utcnow() + expires_delta

        payload = {
            'sub': str(user_id),  # Subject (user ID)
            'username': username,
            'scopes': scopes or [],
            'type': 'access',
            'exp': expire,
            'iat': datetime.utcnow(),
            'iss': JWTConfig.ISSUER
        }

        token = jwt.encode(payload, JWTConfig.SECRET_KEY, algorithm=JWTConfig.ALGORITHM)
        return token

    @staticmethod
    def create_refresh_token(
        user_id: int,
        token_id: Optional[str] = None
    ) -> str:
        """
        Create JWT refresh token.

        Args:
            user_id: User ID
            token_id: Unique token identifier

        Returns:
            JWT refresh token string
        """
        if token_id is None:
            token_id = secrets.token_urlsafe(16)

        expire = datetime.utcnow() + timedelta(days=JWTConfig.REFRESH_TOKEN_EXPIRE_DAYS)

        payload = {
            'sub': str(user_id),
            'jti': token_id,  # JWT ID
            'type': 'refresh',
            'exp': expire,
            'iat': datetime.utcnow(),
            'iss': JWTConfig.ISSUER
        }

        token = jwt.encode(payload, JWTConfig.SECRET_KEY, algorithm=JWTConfig.ALGORITHM)

        # Store refresh token in database
        db = get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS jwt_refresh_tokens (
                id SERIAL PRIMARY KEY,
                token_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revoked BOOLEAN DEFAULT 0
            )
        ''')

        db.execute('''
            INSERT INTO jwt_refresh_tokens (token_id, user_id, expires_at)
            VALUES (%s, %s, %s)
        ''', (token_id, user_id, expire))

        db.commit()

        return token

    @staticmethod
    def verify_token(token: str, token_type: str = 'access') -> Optional[Dict[str, Any]]:
        """
        Verify JWT token.

        Args:
            token: JWT token string
            token_type: 'access' or 'refresh'

        Returns:
            Decoded payload if valid, None otherwise
        """
        try:
            payload = jwt.decode(
                token,
                JWTConfig.SECRET_KEY,
                algorithms=[JWTConfig.ALGORITHM],
                issuer=JWTConfig.ISSUER
            )

            # Check token type
            if payload.get('type') != token_type:
                return None

            # For refresh tokens, check if revoked
            if token_type == 'refresh':
                token_id = payload.get('jti')
                if not JWTManager._is_refresh_token_valid(token_id):
                    return None

            return payload

        except jwt.ExpiredSignatureError:
            # Token has expired
            return None
        except jwt.InvalidTokenError:
            # Token is invalid
            return None

    @staticmethod
    def _is_refresh_token_valid(token_id: str) -> bool:
        """Check if refresh token is valid and not revoked."""
        db = get_db()

        # Ensure table exists
        db.execute('''
            CREATE TABLE IF NOT EXISTS jwt_refresh_tokens (
                id SERIAL PRIMARY KEY,
                token_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revoked BOOLEAN DEFAULT 0
            )
        ''')

        result = db.execute('''
            SELECT revoked, expires_at FROM jwt_refresh_tokens
            WHERE token_id = %s
        ''', (token_id,)).fetchone()

        if not result:
            return False

        if result['revoked']:
            return False

        expires_at = datetime.fromisoformat(result['expires_at'])
        if expires_at < datetime.utcnow():
            return False

        return True

    @staticmethod
    def revoke_refresh_token(token_id: str) -> bool:
        """Revoke a refresh token."""
        db = get_db()

        db.execute('''
            UPDATE jwt_refresh_tokens
            SET revoked = 1
            WHERE token_id = %s
        ''', (token_id,))

        db.commit()
        return db.total_changes > 0

    @staticmethod
    def revoke_all_user_tokens(user_id: int):
        """Revoke all refresh tokens for a user."""
        db = get_db()

        db.execute('''
            UPDATE jwt_refresh_tokens
            SET revoked = 1
            WHERE user_id = %s
        ''', (user_id,))

        db.commit()

    @staticmethod
    def refresh_access_token(refresh_token: str) -> Optional[str]:
        """
        Create new access token using refresh token.

        Args:
            refresh_token: Valid refresh token

        Returns:
            New access token or None if invalid
        """
        payload = JWTManager.verify_token(refresh_token, token_type='refresh')

        if not payload:
            return None

        user_id = int(payload['sub'])

        # Get user info
        db = get_db()
        user = db.execute(
            'SELECT username FROM users WHERE id = %s',
            (user_id,)
        ).fetchone()

        if not user:
            return None

        # Create new access token
        access_token = JWTManager.create_access_token(user_id, user['username'])

        return access_token


def jwt_required(scopes: list[str] = None):
    """
    Decorator to require JWT authentication.

    Args:
        scopes: Required permission scopes (optional)

    Example:
        @app.route('/api/data')
        @jwt_required(scopes=['read:data'])
        def get_data():
            user_id = g.jwt_user_id
            username = g.jwt_username
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Get token from Authorization header
            auth_header = request.headers.get('Authorization')

            if not auth_header or not auth_header.startswith('Bearer '):
                return jsonify({
                    'status': 'error',
                    'message': 'Missing or invalid Authorization header'
                }), 401

            token = auth_header[7:]  # Remove 'Bearer ' prefix

            # Verify token
            payload = JWTManager.verify_token(token, token_type='access')

            if not payload:
                return jsonify({
                    'status': 'error',
                    'message': 'Invalid or expired token'
                }), 401

            # Check scopes if required
            if scopes:
                token_scopes = payload.get('scopes', [])
                if not all(scope in token_scopes for scope in scopes):
                    log_audit_event(
                        AuditEventType.ACCESS_DENIED,
                        f"Insufficient scopes for {request.path}",
                        result='denied',
                        severity='warning',
                        user_id=int(payload['sub']),
                        username=payload['username'],
                        details={'required': scopes, 'provided': token_scopes}
                    )
                    return jsonify({
                        'status': 'error',
                        'message': 'Insufficient permissions'
                    }), 403

            # Store user info in g for access in route
            g.jwt_user_id = int(payload['sub'])
            g.jwt_username = payload['username']
            g.jwt_scopes = payload.get('scopes', [])

            # Log API usage
            log_api_key_usage(
                f"jwt-{payload['username']}",
                request.path
            )

            return f(*args, **kwargs)

        return decorated_function

    return decorator


class OAuth2Scopes:
    """
    Standard OAuth2-like scopes for API permissions.
    """

    # User operations
    READ_USER = 'read:user'
    WRITE_USER = 'write:user'

    # Project operations
    READ_PROJECTS = 'read:projects'
    WRITE_PROJECTS = 'write:projects'
    DELETE_PROJECTS = 'delete:projects'

    # File operations
    READ_FILES = 'read:files'
    WRITE_FILES = 'write:files'
    DELETE_FILES = 'delete:files'

    # Job operations
    READ_JOBS = 'read:jobs'
    SUBMIT_JOBS = 'submit:jobs'
    CANCEL_JOBS = 'cancel:jobs'

    # Admin operations
    ADMIN = 'admin:all'

    @classmethod
    def all_scopes(cls) -> list[str]:
        """Get all available scopes."""
        return [
            cls.READ_USER, cls.WRITE_USER,
            cls.READ_PROJECTS, cls.WRITE_PROJECTS, cls.DELETE_PROJECTS,
            cls.READ_FILES, cls.WRITE_FILES, cls.DELETE_FILES,
            cls.READ_JOBS, cls.SUBMIT_JOBS, cls.CANCEL_JOBS,
            cls.ADMIN
        ]


# API endpoint helpers
def create_token_pair(user_id: int, username: str, scopes: list[str] = None) -> Dict[str, str]:
    """
    Create access and refresh token pair.

    Returns:
        Dictionary with 'access_token' and 'refresh_token'
    """
    access_token = JWTManager.create_access_token(user_id, username, scopes)
    refresh_token = JWTManager.create_refresh_token(user_id)

    return {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'token_type': 'Bearer',
        'expires_in': JWTConfig.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    }
