"""Trusted Tester Program: access tiers, application states and the access gate.

Path into Phonos while the program runs:

    sign-up ──▶ applicant ──apply──▶ (pending) ──approve──▶ tester ──graduate──▶ regular

- ``users.access_tier`` holds the tier: ``applicant`` (signed up, no workspace access yet),
  ``tester`` (approved Trusted Tester, full access during the program) or ``regular``
  (normal access; every account that existed before the program is ``regular``).
- ``trusted_tester_applications`` holds one row per user with the application and its
  review state: pending, approved, rejected, graduated or revoked.
- The gate is off unless ``TRUSTED_TESTER_GATE=true``. While it is off, new accounts start
  as ``regular`` and the program is an opt-in early-access track; while it is on, new
  accounts start as ``applicant`` and may only reach the pages in ``APPLICANT_ALLOWED``
  until an admin approves them.
"""
from __future__ import annotations

import logging
import os

from flask import jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)

TIER_APPLICANT = 'applicant'
TIER_TESTER = 'tester'
TIER_REGULAR = 'regular'
TIERS = (TIER_APPLICANT, TIER_TESTER, TIER_REGULAR)

STATUS_PENDING = 'pending'
STATUS_APPROVED = 'approved'
STATUS_REJECTED = 'rejected'
STATUS_GRADUATED = 'graduated'
STATUS_REVOKED = 'revoked'
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_GRADUATED, STATUS_REVOKED)

# Admin review actions: action -> (statuses it may start from, new status)
ACTIONS = {
    'approve': ((STATUS_PENDING, STATUS_REJECTED, STATUS_REVOKED), STATUS_APPROVED),
    'reject': ((STATUS_PENDING,), STATUS_REJECTED),
    'graduate': ((STATUS_PENDING, STATUS_APPROVED), STATUS_GRADUATED),
    'revoke': ((STATUS_APPROVED,), STATUS_REVOKED),
}

MOTIVATION_MAX = 2000
FIELD_MAX = 200

# What an applicant may reach while the gate is on: sign-in/out, account recovery,
# the program page and its API, legal pages and static assets.
APPLICANT_ALLOWED_EXACT = frozenset({
    '/', '/login', '/logout', '/register', '/verify_2fa', '/resend_2fa', '/forgot_password',
    '/terms-of-service', '/datenschutz', '/impressum', '/licenses',
    '/favicon.ico', '/robots.txt', '/sitemap.xml', '/offline', '/api/consent',
    '/trusted-tester',
})
APPLICANT_ALLOWED_PREFIXES = (
    '/static/', '/api/trusted-tester/', '/auth/', '/verify_email/',
    '/verify_password_reset_2fa/', '/resend_password_reset_2fa/', '/reset_password/',
)


def gate_enabled() -> bool:
    """True when new accounts must pass the program before regular access."""
    return os.environ.get('TRUSTED_TESTER_GATE', 'false').lower() == 'true'


def initial_access_tier() -> str:
    """The tier a newly created account starts in."""
    return TIER_APPLICANT if gate_enabled() else TIER_REGULAR


def get_access_tier(db, user_id) -> str | None:
    row = db.execute('SELECT access_tier FROM users WHERE id = %s', (user_id,)).fetchone()
    if not row:
        return None
    return row['access_tier'] or TIER_REGULAR


def get_application(db, user_id):
    return db.execute(
        'SELECT id, user_id, status, motivation, institution, use_case, applied_at, '
        'reviewed_at, review_note, graduated_at FROM trusted_tester_applications WHERE user_id = %s',
        (user_id,),
    ).fetchone()


def applicant_may_access(path: str) -> bool:
    return path in APPLICANT_ALLOWED_EXACT or path.startswith(APPLICANT_ALLOWED_PREFIXES)


def register_trusted_tester_gate(app):
    """Keep applicants on the program page until they are approved.

    Registered after the remember-me hook so a restored session is already in place.
    Admins are never gated. With the gate off this hook returns at once.
    """

    @app.before_request
    def _trusted_tester_gate():
        if not gate_enabled() or 'user_id' not in session:
            return None
        if applicant_may_access(request.path):
            return None

        from .database import get_db  # local import: helpers.database imports the app

        try:
            db = get_db()
            row = db.execute(
                'SELECT access_tier, is_admin FROM users WHERE id = %s', (session['user_id'],)
            ).fetchone()
        except Exception:  # never lock everyone out because of a lookup failure
            logger.exception('Trusted tester gate: tier lookup failed')
            return None
        if not row or row['is_admin'] or (row['access_tier'] or TIER_REGULAR) != TIER_APPLICANT:
            return None

        if request.path.startswith('/api/') or request.path.startswith('/download'):
            return jsonify({
                'status': 'error',
                'code': 'trusted_tester_pending',
                'message': 'Your account is waiting for access through the Trusted Tester Program.',
                'apply_url': url_for('trusted_tester_page'),
            }), 403
        return redirect(url_for('trusted_tester_page'))

    return _trusted_tester_gate
