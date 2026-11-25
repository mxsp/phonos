"""
Audit Logging System for Phonos
Tracks security events, data access, and user actions for compliance and security monitoring.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, Any
from flask import request, session, g

from .database import get_db


# Event Types
class AuditEventType:
    # Authentication Events
    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGOUT = "auth.logout"
    PASSWORD_CHANGE = "auth.password.change"
    PASSWORD_RESET = "auth.password.reset"
    TWO_FA_ENABLED = "auth.2fa.enabled"
    TWO_FA_DISABLED = "auth.2fa.disabled"
    TWO_FA_SUCCESS = "auth.2fa.success"
    TWO_FA_FAILURE = "auth.2fa.failure"
    ACCOUNT_LOCKED = "auth.account.locked"
    ACCOUNT_UNLOCKED = "auth.account.unlocked"

    # Authorization Events
    ACCESS_DENIED = "authz.access.denied"
    PERMISSION_CHANGE = "authz.permission.change"
    ROLE_CHANGE = "authz.role.change"

    # Data Access Events
    PROJECT_VIEWED = "data.project.viewed"
    PROJECT_CREATED = "data.project.created"
    PROJECT_UPDATED = "data.project.updated"
    PROJECT_DELETED = "data.project.deleted"
    FILE_UPLOADED = "data.file.uploaded"
    FILE_DOWNLOADED = "data.file.downloaded"
    FILE_DELETED = "data.file.deleted"
    JOB_SUBMITTED = "data.job.submitted"
    JOB_VIEWED = "data.job.viewed"
    JOB_DELETED = "data.job.deleted"

    # API Events
    API_KEY_CREATED = "api.key.created"
    API_KEY_USED = "api.key.used"
    API_KEY_REVOKED = "api.key.revoked"
    API_RATE_LIMITED = "api.rate_limited"

    # Security Events
    CSRF_VIOLATION = "security.csrf.violation"
    XSS_ATTEMPT = "security.xss.attempt"
    SQL_INJECTION_ATTEMPT = "security.sql_injection.attempt"
    PATH_TRAVERSAL_ATTEMPT = "security.path_traversal.attempt"
    INVALID_INPUT = "security.invalid_input"

    # System Events
    SETTINGS_CHANGED = "system.settings.changed"
    USER_CREATED = "system.user.created"
    USER_DELETED = "system.user.deleted"
    ADMIN_ACTION = "system.admin.action"

    # Consent Events (GDPR Art. 7(1) — proof of consent)
    CONSENT_GRANTED = "consent.analytics.granted"
    CONSENT_WITHDRAWN = "consent.analytics.withdrawn"


def ensure_audit_log_table():
    """Create audit_log table if it doesn't exist."""
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            event_type TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            ip_address TEXT,
            user_agent TEXT,
            resource_type TEXT,
            resource_id TEXT,
            action TEXT,
            result TEXT,
            details TEXT,
            session_id TEXT,
            request_id TEXT,
            severity TEXT DEFAULT 'info'
        )
    ''')

    # Create indexes separately (SQLite doesn't support inline INDEX in CREATE TABLE)
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_log(user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_log(severity)')

    db.commit()


def log_audit_event(
    event_type: str,
    action: str,
    result: str = 'success',
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    severity: str = 'info',
    user_id: Optional[int] = None,
    username: Optional[str] = None
):
    """
    Log an audit event.

    Args:
        event_type: Type of event (use AuditEventType constants)
        action: Description of the action performed
        result: Result of the action (success, failure, denied, etc.)
        resource_type: Type of resource affected (project, file, job, etc.)
        resource_id: ID of the affected resource
        details: Additional details as dictionary
        severity: Event severity (info, warning, error, critical)
        user_id: Override user_id (defaults to session user)
        username: Override username (defaults to session user)
    """
    try:
        ensure_audit_log_table()

        db = get_db()

        # Get user info from session if not provided
        if user_id is None:
            user_id = session.get('user_id')
        if username is None:
            username = session.get('username')

        # Get request info
        ip_address = request.remote_addr if request else None
        user_agent = request.user_agent.string if request else None
        session_id = session.get('_id') if session else None
        request_id = getattr(g, 'request_id', None) if g else None

        # Serialize details to JSON
        details_json = json.dumps(details) if details else None

        db.execute('''
            INSERT INTO audit_log (
                event_type, user_id, username, ip_address, user_agent,
                resource_type, resource_id, action, result, details,
                session_id, request_id, severity
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            event_type, user_id, username, ip_address, user_agent,
            resource_type, resource_id, action, result, details_json,
            session_id, request_id, severity
        ))

        db.commit()

    except Exception as e:
        # Log to Python logger but don't fail the request
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to log audit event: {e}", exc_info=True)


def get_audit_logs(
    user_id: Optional[int] = None,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0
) -> list[dict]:
    """
    Query audit logs with filters.

    Args:
        user_id: Filter by user ID
        event_type: Filter by event type
        severity: Filter by severity
        start_date: Filter events after this date
        end_date: Filter events before this date
        limit: Maximum number of results
        offset: Offset for pagination

    Returns:
        List of audit log entries
    """
    ensure_audit_log_table()

    db = get_db()

    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []

    if user_id:
        query += " AND user_id = %s"
        params.append(user_id)

    if event_type:
        query += " AND event_type = %s"
        params.append(event_type)

    if severity:
        query += " AND severity = %s"
        params.append(severity)

    if start_date:
        query += " AND timestamp >= %s"
        params.append(start_date)

    if end_date:
        query += " AND timestamp <= %s"
        params.append(end_date)

    query += " ORDER BY timestamp DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    rows = db.execute(query, params).fetchall()

    logs = []
    for row in rows:
        log_entry = dict(row)
        # Parse details JSON
        if log_entry.get('details'):
            try:
                log_entry['details'] = json.loads(log_entry['details'])
            except:
                pass
        logs.append(log_entry)

    return logs


def get_security_events(
    days: int = 7,
    severity: Optional[str] = None
) -> list[dict]:
    """
    Get security-related events from the last N days.

    Args:
        days: Number of days to look back
        severity: Filter by severity (warning, error, critical)

    Returns:
        List of security events
    """
    ensure_audit_log_table()

    db = get_db()

    start_date = datetime.utcnow() - timedelta(days=days)

    query = """
        SELECT * FROM audit_log
        WHERE event_type LIKE 'security.%'
        AND timestamp >= %s
    """
    params = [start_date]

    if severity:
        query += " AND severity = %s"
        params.append(severity)

    query += " ORDER BY timestamp DESC"

    rows = db.execute(query, params).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        if event.get('details'):
            try:
                event['details'] = json.loads(event['details'])
            except:
                pass
        events.append(event)

    return events


def get_user_activity_summary(user_id: int, days: int = 30) -> dict:
    """
    Get activity summary for a user.

    Args:
        user_id: User ID
        days: Number of days to summarize

    Returns:
        Dictionary with activity statistics
    """
    ensure_audit_log_table()

    db = get_db()

    start_date = datetime.utcnow() - timedelta(days=days)

    # Total events
    total = db.execute(
        "SELECT COUNT(*) as count FROM audit_log WHERE user_id = %s AND timestamp >= %s",
        (user_id, start_date)
    ).fetchone()['count']

    # Events by type
    by_type = db.execute('''
        SELECT event_type, COUNT(*) as count
        FROM audit_log
        WHERE user_id = %s AND timestamp >= %s
        GROUP BY event_type
        ORDER BY count DESC
    ''', (user_id, start_date)).fetchall()

    # Failed authentications
    failed_auth = db.execute(
        "SELECT COUNT(*) as count FROM audit_log WHERE user_id = %s AND event_type LIKE 'auth.%.failure' AND timestamp >= %s",
        (user_id, start_date)
    ).fetchone()['count']

    # Last login
    last_login = db.execute(
        "SELECT timestamp FROM audit_log WHERE user_id = %s AND event_type = 'auth.login.success' ORDER BY timestamp DESC LIMIT 1",
        (user_id,)
    ).fetchone()

    return {
        'total_events': total,
        'events_by_type': [dict(row) for row in by_type],
        'failed_authentications': failed_auth,
        'last_login': last_login['timestamp'] if last_login else None,
        'period_days': days
    }


def get_suspicious_activity(days: int = 7) -> list[dict]:
    """
    Detect suspicious activity patterns.

    Args:
        days: Number of days to analyze

    Returns:
        List of suspicious activity indicators
    """
    ensure_audit_log_table()

    db = get_db()

    start_date = datetime.utcnow() - timedelta(days=days)

    suspicious = []

    # Multiple failed logins
    failed_logins = db.execute('''
        SELECT user_id, username, ip_address, COUNT(*) as count
        FROM audit_log
        WHERE event_type = 'auth.login.failure'
        AND timestamp >= %s
        GROUP BY user_id, ip_address
        HAVING count >= 5
        ORDER BY count DESC
    ''', (start_date,)).fetchall()

    for row in failed_logins:
        suspicious.append({
            'type': 'multiple_failed_logins',
            'severity': 'warning',
            'user_id': row['user_id'],
            'username': row['username'],
            'ip_address': row['ip_address'],
            'count': row['count']
        })

    # Security violations
    violations = db.execute('''
        SELECT user_id, username, ip_address, event_type, COUNT(*) as count
        FROM audit_log
        WHERE event_type LIKE 'security.%'
        AND timestamp >= %s
        GROUP BY user_id, ip_address, event_type
        ORDER BY count DESC
    ''', (start_date,)).fetchall()

    for row in violations:
        suspicious.append({
            'type': 'security_violation',
            'severity': 'error',
            'user_id': row['user_id'],
            'username': row['username'],
            'ip_address': row['ip_address'],
            'event_type': row['event_type'],
            'count': row['count']
        })

    # Access from multiple IPs
    multiple_ips = db.execute('''
        SELECT user_id, username, COUNT(DISTINCT ip_address) as ip_count
        FROM audit_log
        WHERE timestamp >= %s
        AND event_type = 'auth.login.success'
        GROUP BY user_id
        HAVING ip_count >= 5
    ''', (start_date,)).fetchall()

    for row in multiple_ips:
        suspicious.append({
            'type': 'multiple_ip_addresses',
            'severity': 'info',
            'user_id': row['user_id'],
            'username': row['username'],
            'ip_count': row['ip_count']
        })

    return suspicious


# Convenience functions for common events

def log_login_success(user_id: int, username: str):
    """Log successful login."""
    log_audit_event(
        AuditEventType.LOGIN_SUCCESS,
        f"User {username} logged in successfully",
        user_id=user_id,
        username=username
    )


def log_login_failure(username: str, reason: str = "Invalid credentials"):
    """Log failed login attempt."""
    log_audit_event(
        AuditEventType.LOGIN_FAILURE,
        f"Failed login attempt for user {username}",
        result='failure',
        severity='warning',
        username=username,
        details={'reason': reason}
    )


def log_api_key_usage(key_id: str, endpoint: str):
    """Log API key usage."""
    log_audit_event(
        AuditEventType.API_KEY_USED,
        f"API key used for endpoint {endpoint}",
        resource_type='api_key',
        resource_id=key_id,
        details={'endpoint': endpoint}
    )


def log_file_access(action: str, file_id: str, project_id: int):
    """Log file access (upload, download, delete)."""
    event_map = {
        'upload': AuditEventType.FILE_UPLOADED,
        'download': AuditEventType.FILE_DOWNLOADED,
        'delete': AuditEventType.FILE_DELETED
    }

    log_audit_event(
        event_map.get(action, AuditEventType.FILE_DOWNLOADED),
        f"File {file_id} {action}",
        resource_type='file',
        resource_id=file_id,
        details={'project_id': project_id}
    )


def log_security_violation(violation_type: str, details: dict):
    """Log security violation."""
    event_types = {
        'csrf': AuditEventType.CSRF_VIOLATION,
        'xss': AuditEventType.XSS_ATTEMPT,
        'sql_injection': AuditEventType.SQL_INJECTION_ATTEMPT,
        'path_traversal': AuditEventType.PATH_TRAVERSAL_ATTEMPT
    }

    log_audit_event(
        event_types.get(violation_type, AuditEventType.INVALID_INPUT),
        f"Security violation detected: {violation_type}",
        result='blocked',
        severity='error',
        details=details
    )


from datetime import timedelta
