"""
IP-Based Security and Threat Detection
Provides IP blocking, threat detection, and geographic restrictions.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta
from typing import Optional, Tuple
from flask import request

from .database import get_db
from .audit_log import log_audit_event, AuditEventType


class IPSecurity:
    """IP-based security management."""

    # Threat scoring thresholds
    BLOCK_THRESHOLD = 100
    WARN_THRESHOLD = 50

    # Default block duration in hours
    DEFAULT_BLOCK_DURATION = 24

    @staticmethod
    def ensure_tables():
        """Create IP security tables if they don't exist."""
        db = get_db()

        # IP blocklist table
        db.execute('''
            CREATE TABLE IF NOT EXISTS ip_blocklist (
                id SERIAL PRIMARY KEY,
                ip_address TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL,
                blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                blocked_until TIMESTAMP,
                blocked_by INTEGER,
                threat_score INTEGER DEFAULT 0,
                is_permanent BOOLEAN DEFAULT 0,
                details TEXT
            )
        ''')

        # IP whitelist table
        db.execute('''
            CREATE TABLE IF NOT EXISTS ip_whitelist (
                id SERIAL PRIMARY KEY,
                ip_address TEXT NOT NULL UNIQUE,
                reason TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                added_by INTEGER
            )
        ''')

        # IP threat score table
        db.execute('''
            CREATE TABLE IF NOT EXISTS ip_threat_scores (
                id SERIAL PRIMARY KEY,
                ip_address TEXT NOT NULL,
                event_type TEXT NOT NULL,
                score_delta INTEGER NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                details TEXT
            )
        ''')

        # Create indexes
        db.execute('CREATE INDEX IF NOT EXISTS idx_ip_blocklist_address ON ip_blocklist(ip_address)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_ip_whitelist_address ON ip_whitelist(ip_address)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_ip_threat_address ON ip_threat_scores(ip_address)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_ip_threat_timestamp ON ip_threat_scores(timestamp)')

        db.commit()

    @classmethod
    def is_ip_blocked(cls, ip_address: str) -> Tuple[bool, Optional[str]]:
        """
        Check if an IP address is blocked.

        Returns:
            Tuple of (is_blocked, reason)
        """
        cls.ensure_tables()

        # Check whitelist first
        if cls.is_ip_whitelisted(ip_address):
            return False, None

        db = get_db()
        now = datetime.utcnow()

        # Check for active blocks
        block = db.execute('''
            SELECT reason, blocked_until, is_permanent
            FROM ip_blocklist
            WHERE ip_address = %s
            AND (is_permanent = 1 OR blocked_until IS NULL OR blocked_until > %s)
        ''', (ip_address, now)).fetchone()

        if block:
            return True, block['reason']

        # Check threat score
        threat_score = cls.get_threat_score(ip_address)
        if threat_score >= cls.BLOCK_THRESHOLD:
            # Auto-block high-threat IPs
            cls.block_ip(
                ip_address,
                reason="Automatic block due to high threat score",
                duration_hours=cls.DEFAULT_BLOCK_DURATION,
                threat_score=threat_score
            )
            return True, "High threat score"

        return False, None

    @classmethod
    def is_ip_whitelisted(cls, ip_address: str) -> bool:
        """Check if an IP address is whitelisted."""
        cls.ensure_tables()

        db = get_db()
        whitelist = db.execute(
            'SELECT 1 FROM ip_whitelist WHERE ip_address = %s',
            (ip_address,)
        ).fetchone()

        return whitelist is not None

    @classmethod
    def block_ip(
        cls,
        ip_address: str,
        reason: str,
        duration_hours: Optional[int] = None,
        permanent: bool = False,
        blocked_by: Optional[int] = None,
        threat_score: int = 0,
        details: Optional[str] = None
    ) -> bool:
        """
        Block an IP address.

        Args:
            ip_address: IP address to block
            reason: Reason for blocking
            duration_hours: Duration in hours (None for indefinite)
            permanent: Whether the block is permanent
            blocked_by: User ID who initiated the block
            threat_score: Current threat score
            details: Additional details

        Returns:
            True if successful
        """
        cls.ensure_tables()

        try:
            # Validate IP address
            ipaddress.ip_address(ip_address)
        except ValueError:
            return False

        # Check if already whitelisted
        if cls.is_ip_whitelisted(ip_address):
            return False

        db = get_db()

        blocked_until = None
        if duration_hours and not permanent:
            blocked_until = datetime.utcnow() + timedelta(hours=duration_hours)

        # Insert or update block
        db.execute('''
            INSERT INTO ip_blocklist
            (ip_address, reason, blocked_until, blocked_by, threat_score, is_permanent, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ip_address) DO UPDATE SET
                reason = EXCLUDED.reason,
                blocked_until = EXCLUDED.blocked_until,
                blocked_by = EXCLUDED.blocked_by,
                threat_score = EXCLUDED.threat_score,
                is_permanent = EXCLUDED.is_permanent,
                details = EXCLUDED.details
        ''', (ip_address, reason, blocked_until, blocked_by, threat_score, permanent, details))

        db.commit()

        # Log the block
        log_audit_event(
            AuditEventType.ADMIN_ACTION,
            f"IP address {ip_address} blocked: {reason}",
            severity='warning',
            details={
                'ip_address': ip_address,
                'reason': reason,
                'duration_hours': duration_hours,
                'permanent': permanent,
                'threat_score': threat_score
            }
        )

        return True

    @classmethod
    def unblock_ip(cls, ip_address: str, unblocked_by: Optional[int] = None) -> bool:
        """
        Unblock an IP address.

        Returns:
            True if successful
        """
        cls.ensure_tables()

        db = get_db()
        db.execute('DELETE FROM ip_blocklist WHERE ip_address = %s', (ip_address,))
        db.commit()

        # Log the unblock
        log_audit_event(
            AuditEventType.ADMIN_ACTION,
            f"IP address {ip_address} unblocked",
            details={'ip_address': ip_address}
        )

        return True

    @classmethod
    def whitelist_ip(
        cls,
        ip_address: str,
        reason: Optional[str] = None,
        added_by: Optional[int] = None
    ) -> bool:
        """
        Add IP address to whitelist.

        Returns:
            True if successful
        """
        cls.ensure_tables()

        try:
            ipaddress.ip_address(ip_address)
        except ValueError:
            return False

        db = get_db()

        # Remove from blocklist if present
        db.execute('DELETE FROM ip_blocklist WHERE ip_address = %s', (ip_address,))

        # Add to whitelist
        db.execute('''
            INSERT INTO ip_whitelist (ip_address, reason, added_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (ip_address) DO UPDATE SET
                reason = EXCLUDED.reason,
                added_by = EXCLUDED.added_by
        ''', (ip_address, reason, added_by))

        db.commit()

        # Log the whitelisting
        log_audit_event(
            AuditEventType.ADMIN_ACTION,
            f"IP address {ip_address} whitelisted",
            details={'ip_address': ip_address, 'reason': reason}
        )

        return True

    @classmethod
    def remove_from_whitelist(cls, ip_address: str) -> bool:
        """Remove IP address from whitelist."""
        cls.ensure_tables()

        db = get_db()
        db.execute('DELETE FROM ip_whitelist WHERE ip_address = %s', (ip_address,))
        db.commit()

        return True

    @classmethod
    def record_threat_event(
        cls,
        ip_address: str,
        event_type: str,
        score_delta: int,
        details: Optional[str] = None
    ):
        """
        Record a threat event and update IP threat score.

        Args:
            ip_address: IP address
            event_type: Type of threat event
            score_delta: Points to add to threat score
            details: Additional details
        """
        cls.ensure_tables()

        db = get_db()

        # Record the event
        db.execute('''
            INSERT INTO ip_threat_scores (ip_address, event_type, score_delta, details)
            VALUES (%s, %s, %s, %s)
        ''', (ip_address, event_type, score_delta, details))

        db.commit()

        # Check if auto-block is needed
        threat_score = cls.get_threat_score(ip_address)
        if threat_score >= cls.BLOCK_THRESHOLD and not cls.is_ip_whitelisted(ip_address):
            cls.block_ip(
                ip_address,
                reason=f"Automatic block: threat score {threat_score}",
                duration_hours=cls.DEFAULT_BLOCK_DURATION,
                threat_score=threat_score
            )

    @classmethod
    def get_threat_score(cls, ip_address: str, hours: int = 24) -> int:
        """
        Calculate current threat score for an IP address.

        Args:
            ip_address: IP address
            hours: Number of hours to look back

        Returns:
            Current threat score
        """
        cls.ensure_tables()

        db = get_db()
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        result = db.execute('''
            SELECT SUM(score_delta) as total_score
            FROM ip_threat_scores
            WHERE ip_address = %s AND timestamp >= %s
        ''', (ip_address, cutoff)).fetchone()

        return result['total_score'] or 0

    @classmethod
    def get_blocked_ips(cls, include_expired: bool = False) -> list[dict]:
        """Get list of blocked IP addresses."""
        cls.ensure_tables()

        db = get_db()

        if include_expired:
            query = 'SELECT * FROM ip_blocklist ORDER BY blocked_at DESC'
            rows = db.execute(query).fetchall()
        else:
            now = datetime.utcnow()
            query = '''
                SELECT * FROM ip_blocklist
                WHERE is_permanent = 1 OR blocked_until IS NULL OR blocked_until > %s
                ORDER BY blocked_at DESC
            '''
            rows = db.execute(query, (now,)).fetchall()

        return [dict(row) for row in rows]

    @classmethod
    def get_whitelisted_ips(cls) -> list[dict]:
        """Get list of whitelisted IP addresses."""
        cls.ensure_tables()

        db = get_db()
        rows = db.execute('SELECT * FROM ip_whitelist ORDER BY added_at DESC').fetchall()

        return [dict(row) for row in rows]

    @classmethod
    def cleanup_expired_blocks(cls):
        """Remove expired IP blocks."""
        cls.ensure_tables()

        db = get_db()
        now = datetime.utcnow()

        db.execute('''
            DELETE FROM ip_blocklist
            WHERE is_permanent = 0 AND blocked_until IS NOT NULL AND blocked_until <= %s
        ''', (now,))

        db.commit()

    @classmethod
    def cleanup_old_threat_scores(cls, days: int = 30):
        """Remove old threat score records."""
        cls.ensure_tables()

        db = get_db()
        cutoff = datetime.utcnow() - timedelta(days=days)

        db.execute('DELETE FROM ip_threat_scores WHERE timestamp < %s', (cutoff,))
        db.commit()


# Threat event scoring
class ThreatEvent:
    """Threat event types and their scores."""

    # Authentication threats
    FAILED_LOGIN = 10
    INVALID_CSRF_TOKEN = 15
    SESSION_HIJACK_ATTEMPT = 50

    # Injection attempts
    SQL_INJECTION = 30
    XSS_ATTEMPT = 25
    PATH_TRAVERSAL = 20
    COMMAND_INJECTION = 40

    # Abuse
    RATE_LIMIT_EXCEEDED = 5
    INVALID_INPUT = 5
    SUSPICIOUS_REQUEST = 10

    # Severe threats
    MALWARE_UPLOAD = 100
    EXPLOITATION_ATTEMPT = 75


def check_ip_block_middleware():
    """
    Middleware to check if request IP is blocked.
    Call this at the beginning of request processing.
    """
    ip_address = request.remote_addr

    is_blocked, reason = IPSecurity.is_ip_blocked(ip_address)

    if is_blocked:
        from flask import abort
        log_audit_event(
            AuditEventType.ACCESS_DENIED,
            f"Blocked IP {ip_address} attempted access",
            result='blocked',
            severity='warning',
            details={'ip_address': ip_address, 'reason': reason}
        )
        abort(403, description=f"Access denied: {reason}")
