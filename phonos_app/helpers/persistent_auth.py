"""Persistent ("remember me") login and per-IP 2FA-trust tokens.

Two opt-in, secure, revocable authentication mechanisms layered on top of the
normal Flask session:

* **Remember-me** — an OWASP selector:validator cookie (`phonos_remember`).
  The validator is rotated on every use (replay-resistant), a selector hit with
  a wrong validator wipes the whole series (theft detection), tokens slide to
  30 days on use but never live past a 90-day hard cap, and they are server-side
  revocable (logout / settings toggle / password change).

* **2FA trust** — a device token (`phonos_2fa_trust`) bound to the origin IP that
  lets a user skip the email 2FA step for 3 days after a successful 2FA. The
  trust is *absolute* (never renewed on use) so it always decays 3 days after the
  2FA event, and the skip requires BOTH the cookie AND the same IP.

Cookies are HttpOnly, Secure (in production), SameSite=Lax, host-only (no Domain).
Tables are created lazily, mirroring the self-healing idiom used by
``ensure_user_preferences_table`` / ``AccountLockout`` rather than editing the
boot schema.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from flask import g, request, session

from config import Config
from .database import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REMEMBER_COOKIE = 'phonos_remember'
REMEMBER_MAX_AGE = 30 * 24 * 3600          # sliding window, seconds (30 days)
REMEMBER_ABS_MAX_DAYS = 90                 # hard cap from created_at

TRUST_COOKIE = 'phonos_2fa_trust'
TRUST_MAX_AGE = 3 * 24 * 3600              # absolute, non-renewing (3 days)
TRUST_DEVICE_LIMIT = 10                    # keep newest N trust devices per user

# Paths the auto-login hook must never touch (cheap static / health endpoints).
_BYPASS_PREFIXES = (
    '/static/', '/favicon', '/robots.txt', '/sitemap.xml',
    '/health', '/api/health', '/.well-known/',
)
# Auth endpoints handle their own credential/2FA/logout flow — never auto-login.
_AUTH_ENDPOINTS = {'login', 'verify_2fa', 'resend_2fa', 'logout'}


def _cookie_secure() -> bool:
    """Mirror SESSION_COOKIE_SECURE: secure in production, relaxed in DEV/http."""
    return not Config.DEV_MODE


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _as_dt(value):
    """Coerce a DB timestamp (datetime or ISO string) to a naive datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Lazy table creation
# ---------------------------------------------------------------------------
_TABLES_ENSURED = False


def ensure_remember_tokens_table(db) -> None:
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS remember_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            selector TEXT UNIQUE NOT NULL,
            validator_hash TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
        '''
    )
    db.execute('CREATE INDEX IF NOT EXISTS idx_remember_user ON remember_tokens(user_id)')
    db.commit()


def ensure_trusted_2fa_devices_table(db) -> None:
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS trusted_2fa_devices (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            device_token_hash TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
        '''
    )
    db.execute(
        'CREATE INDEX IF NOT EXISTS idx_trust_user_ip ON trusted_2fa_devices(user_id, ip_address)'
    )
    db.commit()


def _ensure_tables(db) -> None:
    global _TABLES_ENSURED
    if _TABLES_ENSURED:
        return
    ensure_remember_tokens_table(db)
    ensure_trusted_2fa_devices_table(db)
    _TABLES_ENSURED = True


# ---------------------------------------------------------------------------
# Remember-me token operations
# ---------------------------------------------------------------------------
def issue_remember_token(db, user_id: int, ip: str, ua: str) -> str:
    """Mint a new remember-me token row and return the `selector:validator` cookie."""
    _ensure_tables(db)
    selector = secrets.token_urlsafe(16)
    validator = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=REMEMBER_MAX_AGE)
    db.execute(
        'INSERT INTO remember_tokens '
        '(user_id, selector, validator_hash, ip_address, user_agent, created_at, last_used_at, expires_at) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
        (user_id, selector, _hash(validator), ip, (ua or '')[:512], now, now, expires_at),
    )
    db.commit()
    return f"{selector}:{validator}"


def consume_remember_cookie(db, cookie_value: str):
    """Validate + rotate a remember-me cookie.

    Returns ``(user_id, new_cookie_value_or_None, theft_bool)``:
      * success → (user_id, rotated_cookie, False); rotated_cookie may be None
        when a concurrent request already rotated (proceed, don't reset cookie).
      * theft (valid selector, wrong validator) → (None, None, True); series wiped.
      * miss / expired → (None, None, False).
    """
    _ensure_tables(db)
    if not cookie_value or ':' not in cookie_value:
        return (None, None, False)
    selector, _, validator = cookie_value.partition(':')
    if not selector or not validator:
        return (None, None, False)

    row = db.execute(
        'SELECT user_id, validator_hash, created_at, expires_at '
        'FROM remember_tokens WHERE selector = %s',
        (selector,),
    ).fetchone()
    if not row:
        return (None, None, False)

    stored = str(row['validator_hash'])
    if not secrets.compare_digest(_hash(validator), stored):
        # Valid selector but wrong validator → token theft. Wipe the series.
        revoke_all_remember_tokens(db, row['user_id'])
        return (None, None, True)

    now = datetime.utcnow()
    expires_at = _as_dt(row['expires_at'])
    created_at = _as_dt(row['created_at'])
    if expires_at is None or expires_at <= now:
        db.execute('DELETE FROM remember_tokens WHERE selector = %s', (selector,))
        db.commit()
        return (None, None, False)
    if created_at is not None and created_at < now - timedelta(days=REMEMBER_ABS_MAX_DAYS):
        db.execute('DELETE FROM remember_tokens WHERE selector = %s', (selector,))
        db.commit()
        return (None, None, False)

    # Rotate the validator. Compare-and-swap on the old hash so two concurrent
    # tabs presenting the same cookie don't both "win" (and don't trip theft
    # detection): exactly one UPDATE matches, the other affects 0 rows.
    new_validator = secrets.token_urlsafe(32)
    new_expires = now + timedelta(seconds=REMEMBER_MAX_AGE)
    result = db.execute(
        'UPDATE remember_tokens SET validator_hash = %s, last_used_at = %s, expires_at = %s '
        'WHERE selector = %s AND validator_hash = %s',
        (_hash(new_validator), now, new_expires, selector, stored),
    )
    db.commit()
    if getattr(result, 'rowcount', 0) == 1:
        return (row['user_id'], f"{selector}:{new_validator}", False)
    # Concurrent rotation already happened — proceed with the session, but don't
    # overwrite the (now newer) cookie with a stale value.
    return (row['user_id'], None, False)


def revoke_remember_token_by_cookie(db, cookie_value: str) -> None:
    """Revoke just the device whose cookie is presented (single-device logout)."""
    _ensure_tables(db)
    if not cookie_value or ':' not in cookie_value:
        return
    selector = cookie_value.partition(':')[0]
    if selector:
        db.execute('DELETE FROM remember_tokens WHERE selector = %s', (selector,))
        db.commit()


def revoke_all_remember_tokens(db, user_id: int) -> None:
    """Revoke every remembered session for a user (settings off / password change)."""
    _ensure_tables(db)
    db.execute('DELETE FROM remember_tokens WHERE user_id = %s', (user_id,))
    db.commit()


# ---------------------------------------------------------------------------
# 2FA trust-device operations
# ---------------------------------------------------------------------------
def issue_trust_device(db, user_id: int, ip: str, ua: str) -> str:
    """Mint a 3-day device trust token (bound to `ip`) and return the cookie value."""
    _ensure_tables(db)
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=TRUST_MAX_AGE)
    db.execute(
        'INSERT INTO trusted_2fa_devices '
        '(user_id, device_token_hash, ip_address, user_agent, created_at, last_used_at, expires_at) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s)',
        (user_id, _hash(token), ip, (ua or '')[:512], now, now, expires_at),
    )
    # Keep only the newest TRUST_DEVICE_LIMIT rows for this user.
    db.execute(
        'DELETE FROM trusted_2fa_devices WHERE user_id = %s AND id NOT IN '
        '(SELECT id FROM trusted_2fa_devices WHERE user_id = %s ORDER BY created_at DESC LIMIT %s)',
        (user_id, user_id, TRUST_DEVICE_LIMIT),
    )
    db.commit()
    return token


def check_trust_device(db, user_id: int, cookie_value: str, ip: str) -> bool:
    """True if a non-expired trust row for (user_id, ip) matches the cookie token.

    Updates ``last_used_at`` but never extends ``expires_at`` — the trust always
    decays 3 days after the 2FA event that minted it.
    """
    _ensure_tables(db)
    if not cookie_value:
        return False
    rows = db.execute(
        'SELECT id, device_token_hash, expires_at FROM trusted_2fa_devices '
        'WHERE user_id = %s AND ip_address = %s',
        (user_id, ip),
    ).fetchall()
    if not rows:
        return False
    presented = _hash(cookie_value)
    now = datetime.utcnow()
    for row in rows:
        expires_at = _as_dt(row['expires_at'])
        if expires_at is None or expires_at <= now:
            continue
        if secrets.compare_digest(presented, str(row['device_token_hash'])):
            db.execute(
                'UPDATE trusted_2fa_devices SET last_used_at = %s WHERE id = %s',
                (now, row['id']),
            )
            db.commit()
            return True
    return False


def revoke_all_trust_devices(db, user_id: int) -> None:
    """Drop every trusted device for a user (settings off / password change)."""
    _ensure_tables(db)
    db.execute('DELETE FROM trusted_2fa_devices WHERE user_id = %s', (user_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------
def set_remember_cookie(resp, value: str) -> None:
    resp.set_cookie(
        REMEMBER_COOKIE, value,
        max_age=REMEMBER_MAX_AGE, httponly=True,
        secure=_cookie_secure(), samesite='Lax', path='/',
    )


def clear_remember_cookie(resp) -> None:
    resp.set_cookie(
        REMEMBER_COOKIE, '', max_age=0, expires=0, httponly=True,
        secure=_cookie_secure(), samesite='Lax', path='/',
    )


def set_trust_cookie(resp, value: str) -> None:
    resp.set_cookie(
        TRUST_COOKIE, value,
        max_age=TRUST_MAX_AGE, httponly=True,
        secure=_cookie_secure(), samesite='Lax', path='/',
    )


def clear_trust_cookie(resp) -> None:
    resp.set_cookie(
        TRUST_COOKIE, '', max_age=0, expires=0, httponly=True,
        secure=_cookie_secure(), samesite='Lax', path='/',
    )


# ---------------------------------------------------------------------------
# Auto-login before/after request hooks
# ---------------------------------------------------------------------------
def _auto_login() -> None:
    """Restore a session from a valid remember-me cookie (no-op otherwise)."""
    if request.method == 'OPTIONS':
        return
    endpoint = request.endpoint
    if endpoint == 'static' or endpoint in _AUTH_ENDPOINTS:
        return
    path = request.path or ''
    if any(path.startswith(p) for p in _BYPASS_PREFIXES):
        return
    if session.get('user_id'):
        return
    cookie = request.cookies.get(REMEMBER_COOKIE)
    if not cookie:
        return

    db = get_db()
    user_id, new_cookie, theft = consume_remember_cookie(db, cookie)

    if theft:
        g._clear_remember = True
        try:
            from .audit_log import log_audit_event, AuditEventType
            log_audit_event(
                AuditEventType.LOGIN_FAILURE,
                'Remember-me token theft detected; series revoked',
                result='denied', severity='warning',
                details={'method': 'remember_me'},
            )
        except Exception:
            pass
        return

    if user_id is None:
        g._clear_remember = True
        return

    row = db.execute(
        'SELECT username, is_admin FROM users WHERE id = %s', (user_id,)
    ).fetchone()
    if not row:
        g._clear_remember = True
        return

    session.clear()
    session.permanent = True
    session['user_id'] = user_id
    session['username'] = row['username']
    session['is_admin'] = bool(row['is_admin'])
    session['_remember_login'] = True
    if new_cookie:
        g._new_remember_cookie = new_cookie

    try:
        from .audit_log import log_audit_event, AuditEventType
        log_audit_event(
            AuditEventType.LOGIN_SUCCESS,
            'Auto-login via remember-me cookie',
            user_id=user_id, username=row['username'],
            details={'method': 'remember_me'},
        )
    except Exception:
        pass


def register_remember_me(app) -> None:
    """Register the remember-me auto-login before/after-request hooks.

    Must be registered AFTER geoblocking (and any future IP-block middleware) so
    those run first; a non-None return from an earlier before_request short-
    circuits this one, which is the desired ordering.
    """

    @app.before_request
    def remember_me_before_request():  # noqa: ANN202
        try:
            _auto_login()
        except Exception as e:  # never let remember-me 500 a page
            logger.warning(f"remember-me auto-login skipped: {e}")
        return None

    @app.after_request
    def apply_remember_cookie(resp):  # noqa: ANN202
        try:
            new_cookie = getattr(g, '_new_remember_cookie', None)
            if new_cookie:
                set_remember_cookie(resp, new_cookie)
            if getattr(g, '_clear_remember', False):
                clear_remember_cookie(resp)
        except Exception as e:
            logger.warning(f"remember-me cookie apply skipped: {e}")
        return resp
