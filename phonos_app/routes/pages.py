"""HTML page routes and authentication flows."""
from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import redis
from flask import jsonify, make_response, redirect, render_template, request, session, url_for

from config import Config
from plan_service import DEFAULT_PLAN, normalize_plan
from stripe_service import StripeService

from ..app import app, get_redis_connection, logger
from ..helpers import admin_required, get_db, login_required, parse_sqlite_timestamp
from ..helpers.storage import format_storage_bytes, get_user_storage_state
from ..helpers.emails import send_email
from ..helpers.login_history import is_known_device, record_login
from ..helpers.trusted_tester import gate_enabled, initial_access_tier
from ..helpers.collaboration import get_pending_invitations
from ..helpers.security import AccountLockout, csrf_protect
from ..helpers.worker_runtime import apply_runtime_status_to_models
from ..helpers.persistent_auth import (
    REMEMBER_COOKIE,
    TRUST_COOKIE,
    check_trust_device,
    clear_remember_cookie,
    clear_trust_cookie,
    issue_remember_token,
    issue_trust_device,
    revoke_remember_token_by_cookie,
    set_remember_cookie,
    set_trust_cookie,
)


# ---------------------------------------------------------------------------
# i18n helpers for legal pages
# ---------------------------------------------------------------------------

LANGUAGE_COOKIE = 'phonos-language'
SUPPORTED_LANGS = ('en', 'de')
DEFAULT_LANG = 'en'


def _detect_lang() -> str:
    """Pick the best language for a request.

    Order: explicit ?lang= query > phonos-language cookie > Accept-Language > default.
    Always returns a value in SUPPORTED_LANGS.
    """
    q = request.args.get('lang', '').lower()
    if q in SUPPORTED_LANGS:
        return q
    cookie = request.cookies.get(LANGUAGE_COOKIE, '').lower()
    if cookie in SUPPORTED_LANGS:
        return cookie
    accept = (request.headers.get('Accept-Language') or '').lower()
    if accept.startswith('de'):
        return 'de'
    return DEFAULT_LANG


def _render_legal(default_template: str, lang_overrides: dict, **context):
    """Render a legal page in the language chosen by the user.

    `default_template` is rendered when no language-specific variant exists
    (or when the user picked the canonical language). `lang_overrides` maps
    'en' / 'de' to the template filename to use for that language.
    """
    lang = _detect_lang()
    template = lang_overrides.get(lang, default_template)
    response = make_response(render_template(template, lang=lang, **context))
    # Persist the user's choice for one year so subsequent requests pick it up
    response.set_cookie(
        LANGUAGE_COOKIE,
        lang,
        max_age=60 * 60 * 24 * 365,
        samesite='Lax',
        secure=Config.SESSION_COOKIE_SECURE,
    )
    return response


@app.route('/favicon.ico')
def favicon():
    """Serve favicon from static directory."""
    from flask import send_from_directory
    import os
    static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'static')
    return send_from_directory(static_dir, 'favicon.ico', mimetype='image/x-icon')


# Public pages exposed to search-engine crawlers:
#   (endpoint, changefreq, priority, bilingual)
# `bilingual` pages are served in both en/de at the same URL via content
# negotiation, so the sitemap advertises the per-language ?lang= variants as
# hreflang alternates (matching the <link rel="alternate"> tags in their heads).
SITEMAP_PAGES = [
    ('index', 'weekly', '1.0', False),
    ('login', 'monthly', '0.7', False),
    ('public_projects', 'daily', '0.7', False),
    ('api_docs', 'monthly', '0.5', False),
    ('terms_of_service', 'yearly', '0.3', True),
    ('datenschutz', 'yearly', '0.3', True),
    ('impressum', 'yearly', '0.3', True),
    ('licenses', 'yearly', '0.3', True),
]


@app.route('/sitemap.xml')
def sitemap_xml():
    """Generate sitemap.xml of public, crawlable pages for search engines."""
    from flask import Response
    from xml.sax.saxutils import escape, quoteattr

    lastmod = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    entries = []
    for endpoint, changefreq, priority, bilingual in SITEMAP_PAGES:
        try:
            loc = url_for(endpoint, _external=True)
        except Exception:
            continue

        alternates = ""
        if bilingual:
            links = []
            for hreflang, lang in (('en', 'en'), ('de', 'de'), ('x-default', None)):
                href = url_for(endpoint, _external=True, **({'lang': lang} if lang else {}))
                links.append(
                    f'    <xhtml:link rel="alternate" hreflang={quoteattr(hreflang)} href={quoteattr(href)}/>'
                )
            alternates = "\n" + "\n".join(links)

        entries.append(
            "  <url>\n"
            f"    <loc>{escape(loc)}</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>{alternates}\n"
            "  </url>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(xml, mimetype='application/xml')


@app.route('/robots.txt')
def robots_txt():
    """Serve robots.txt: allow public pages, block private areas, point to sitemap."""
    from flask import Response

    sitemap_url = url_for('sitemap_xml', _external=True)
    lines = [
        "User-agent: *",
        "Disallow: /dashboard",
        "Disallow: /api/",
        "Disallow: /admin",
        "Disallow: /profile_settings",
        "Disallow: /profile/",
        "Disallow: /custom_models",
        "Disallow: /notebook",
        "Disallow: /bureau",
        "Disallow: /oversight",
        "Disallow: /analytics",
        "Disallow: /viewer",
        "Disallow: /help-desk",
        "Disallow: /logout",
        "",
        f"Sitemap: {sitemap_url}",
        "",
    ]
    return Response("\n".join(lines), mimetype='text/plain')


@app.route('/api/consent', methods=['POST'])
def record_consent():
    """Record a server-side proof of cookie consent (GDPR Art. 7(1) accountability).

    Public and CSRF-free on purpose: the consent banner posts here via
    navigator.sendBeacon before the user is ever logged in, and the worst an
    attacker could forge is an extra audit row. Consent itself is enforced
    client-side by PhonosConsent; this endpoint is only the durable audit trail
    of when, and to what version, a visitor (anonymous or signed-in) consented.

    Best-effort: it never blocks or breaks the client consent flow.
    """
    analytics = False
    consent_version = None
    try:
        data = request.get_json(silent=True) or {}
        analytics = bool(data.get('analytics'))
        consent_version = data.get('version')
    except Exception:  # noqa: BLE001 - malformed body must not 500 the beacon
        pass

    try:
        from ..helpers.audit_log import log_audit_event, AuditEventType
        log_audit_event(
            AuditEventType.CONSENT_GRANTED if analytics else AuditEventType.CONSENT_WITHDRAWN,
            action=f"Analytics consent {'granted' if analytics else 'withdrawn'}",
            result='success',
            resource_type='consent',
            details={
                'analytics': analytics,
                'consent_version': consent_version,
                'path': request.path,
            },
            severity='info',
        )
    except Exception:  # noqa: BLE001 - the audit write is best-effort
        logger.exception('Failed to record consent audit event')

    # 204: the client uses sendBeacon and ignores the response body.
    return ('', 204)


def _summarize_storage(storage_state: dict) -> dict[str, str]:
    """Prepare human-readable storage usage strings for templates."""

    used = int(storage_state.get('used', 0) or 0)
    quota = int(storage_state.get('quota', 0) or 0)
    available = int(storage_state.get('available', 0) or 0)

    if quota <= 0:
        summary_text = f"{format_storage_bytes(used)} used"
        available_text = "No storage quota configured"
    else:
        summary_text = f"{format_storage_bytes(used)} / {format_storage_bytes(quota)}"
        available_text = f"{format_storage_bytes(available)} available"

    return {
        'summary': summary_text,
        'available': available_text,
    }


@app.route('/')
def index():
    """Present the intro animation before guiding visitors to the login page."""
    if 'user_id' in session:
        return redirect(url_for('oversight', auto=1))
    from .. import __version__
    return render_template('index.html', version=__version__)

# Demo route disabled - users must register/login
# @app.route('/demo')
# def demo():
#     """ESMFold demo page."""
#     return render_template('demo.html')

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user = db.execute(
        'SELECT u.id, u.user_credential_id, uc.plan FROM users u JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE u.id = %s',
        (session['user_id'],)
    ).fetchone()
    current_plan = DEFAULT_PLAN
    if user:
        normalized = normalize_plan(user['plan'])
        current_plan = normalized
        if normalized != user['plan']:
            db.execute('UPDATE user_credentials SET plan = %s WHERE id = %s', (normalized, user['user_credential_id']))
            db.commit()
    storage_state = get_user_storage_state(db, session['user_id'])
    storage_summary = _summarize_storage(storage_state)
    pending_invitations = get_pending_invitations(session['user_id'])
    # Enrich models with live worker/NIM runtime status so cards
    # are greyed out dynamically when no workers are available.
    enriched_models = apply_runtime_status_to_models(
        [dict(m) for m in Config.SUPPORTED_MODELS]
    )
    # Gate the "My Education" nav item behind actual participation (as
    # teacher or student) in at least one education project, rather than
    # showing it to every user.
    has_education = db.execute(
        '''SELECT 1 FROM project_assignments pa
           JOIN projects p ON p.id = pa.project_id
           WHERE pa.user_id = %s AND p.project_type = 'education'
           LIMIT 1''',
        (session['user_id'],)
    ).fetchone() is not None

    response = make_response(render_template(
        'dashboard.html',
        current_plan=current_plan,
        supported_models=enriched_models,
        storage_summary=storage_summary,
        pending_invitations=pending_invitations,
        has_education=has_education,
    ))
    # Force no caching
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response


def _current_user_info():
    """Return a small dict with the logged-in user's id, username, email,
    and admin flag — used to embed as window.PHONOS_USER in templates.

    Returns an empty dict (with is_authenticated=False) if no user is signed
    in (e.g. before login_required resolved a notebook-session token)."""
    info = {
        'is_authenticated': False,
        'id': None,
        'username': '',
        'email': '',
        'is_admin': False,
    }
    user_id = session.get('user_id')
    if not user_id:
        return info
    try:
        db = get_db()
        row = db.execute(
            'SELECT u.id, u.username, u.is_admin, uc.email '
            'FROM users u JOIN user_credentials uc ON u.user_credential_id = uc.id '
            'WHERE u.id = %s',
            (user_id,),
        ).fetchone()
        if row:
            info.update({
                'is_authenticated': True,
                'id': row['id'],
                'username': row['username'] or '',
                'email': row['email'] or '',
                'is_admin': bool(row['is_admin']),
            })
    except Exception:
        pass
    return info


@app.route('/notebook')
@login_required
def notebook_page():
    """Renders the Notebook IDE page."""
    # Optional: support opening a specific notebook via query param
    notebook_id = request.args.get('id')
    project_id = request.args.get('project_id')

    user_info = _current_user_info()
    return render_template(
        'notebook.html',
        notebook_id=notebook_id,
        current_project_id=project_id,
        current_user=user_info,
    )


@app.route('/notebook-standalone')
@app.route('/bureau')
@login_required
def notebook_standalone():
    """Legacy aliases that now serve the Notebook IDE.

    Bureau has been retired as a separate product - Notebook is the single
    workspace surface, reached from the launcher and from the Phonos dashboard.
    Both paths stay registered rather than 404ing, because the notebook-only
    login flow and existing bookmarks still point at them; they simply render
    the same Notebook page as ``/notebook``.
    """
    return notebook_page()


@app.route('/public_projects')
def public_projects():
    return render_template('public_projects.html')


@app.route('/partials/storage-stat')
@login_required
def storage_stat_partial():
    """Render the storage summary fragment for HTMX requests."""

    db = get_db()
    storage_state = get_user_storage_state(db, session['user_id'])
    storage_summary = _summarize_storage(storage_state)
    return render_template('partials/_storage_stat.html', storage_summary=storage_summary)


@app.route('/oversight')
@login_required
def oversight():
    """The post-login launcher: Phonos, Notebook and 3D Analytics.

    Three surfaces over one backend — the same session, the same settings and
    the same project database. Which one a user lands on after signing in is a
    client-side preference (see static/js/oversight.js); this page is where it
    is chosen and where every product's header button returns to.
    """
    user_info = _current_user_info()
    return render_template(
        'oversight.html',
        current_user=user_info,
        current_user_name=user_info.get('username') or '',
    )


@app.route('/analytics')
@login_required
def analytics():
    """3D Analytics — the molecular visualizer as its own product surface.

    Semi-standalone: the page hosts the same App/Views runtime as the Phonos
    dashboard and pins it to the `visualizer` view, so project, file and job
    access are identical to using the visualizer inside Phonos.
    """
    return render_template('analytics.html', current_user=_current_user_info())


@app.route('/viewer')
@login_required
def viewer():
    """Renders the 3D PDB viewer page."""
    return render_template('viewer.html')


@app.route('/experimental-viewer')
@login_required
def experimental_viewer():
    """Renders the experimental 3D viewer with MediaPipe hand tracking."""
    return render_template('experimental_viewer.html')


@app.route('/viewer-enhanced')
@login_required
def viewer_enhanced():
    """Renders the enhanced 3D viewer with multi-file tabs."""
    return render_template('viewer_enhanced.html')


@app.route('/evo2')
@login_required
def evo2_page():
    """Renders the Evo2 DNA generation page."""
    # no-store so browsers always fetch fresh HTML (the seed-loader markup is
    # inline in the document, not in a versioned asset, so a stale cached page
    # would hide it).
    user = get_db().execute('SELECT credits FROM users WHERE id = %s', (session.get('user_id'),)).fetchone()
    response = make_response(render_template('evo2.html', user_credits=user['credits'] if user else 0))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/offline')
def offline_page():
    """Renders the offline fallback page for PWA."""
    return render_template('offline.html')


@app.route('/admin')
@admin_required
def admin_dashboard():
    """Renders the main admin monitoring dashboard."""
    return render_template('admin.html')

@app.route('/admin/helpdesk')
@admin_required
def admin_helpdesk():
    """Renders the admin help desk interface."""
    return render_template('admin_helpdesk.html')

@app.route('/admin/helpdesk/<int:ticket_id>')
@admin_required
def admin_ticket_view(ticket_id):
    """Renders the detailed view for a single help desk ticket."""
    return render_template('admin_ticket_view.html', ticket_id=ticket_id)

@app.route('/help-desk')
@login_required
def help_desk():
    """Renders the user-facing help desk page."""
    return render_template('help_desk.html')

@app.route('/help-desk/<int:ticket_id>')
@login_required
def user_ticket_view(ticket_id):
    """Renders the detailed view for a single help desk ticket for a user."""
    return render_template('user_ticket_view.html', ticket_id=ticket_id)

def _get_login_prefs(db, user_id):
    """Read the two security toggles defensively.

    Returns (remember_me_enabled, two_fa_trust_ip_enabled). Falls back to the
    secure defaults (remember on, 2FA-trust off) if the row/columns are missing
    (e.g. a legacy user_preferences table not yet self-healed by the settings
    endpoints) — a failure therefore keeps 2FA mandatory.
    """
    try:
        row = db.execute(
            'SELECT remember_me_enabled, two_fa_trust_ip_enabled '
            'FROM user_preferences WHERE user_id = %s',
            (user_id,),
        ).fetchone()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return (True, False)
    if not row:
        return (True, False)
    return (bool(row['remember_me_enabled']), bool(row['two_fa_trust_ip_enabled']))


def _is_2fa_disabled(db, user_id):
    """Return True if the user has explicitly disabled 2FA in two_factor_auth."""
    try:
        row = db.execute(
            'SELECT is_enabled FROM two_factor_auth WHERE user_id = %s',
            (user_id,),
        ).fetchone()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False
    return row is not None and not bool(row['is_enabled'])


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if not Config.ENABLE_2FA and not app.config.get('TESTING') and not os.environ.get('TESTING'):
            try:
                db_check = get_db()
                cnt_row = db_check.execute('SELECT COUNT(*) as cnt FROM users').fetchone()
                cnt = cnt_row['cnt'] if isinstance(cnt_row, dict) or hasattr(cnt_row, 'keys') else cnt_row[0]
                if int(cnt or 0) == 0:
                    return redirect('/setup')
            except Exception:
                pass
    if request.method == 'POST':
        username_or_email = request.form.get('username')
        password_raw = request.form.get('password')
        if not username_or_email or not password_raw:
            return render_template('login.html', error="Username/email and password are required."), 400
        password = password_raw.encode('utf-8')
        notebook_only = request.form.get('notebook_only') == 'true'
        remember_me = request.form.get('remember_me') in ('on', 'true', '1', 'yes')

        # Store notebook_only preference in session for 2FA flow
        if notebook_only:
            session['notebook_only'] = True

        ip_address = request.remote_addr or 'unknown'
        user_agent = request.user_agent.string

        # Brute-force protection: reject while the (username, IP) pair is locked.
        is_account_locked, lock_minutes = AccountLockout.is_locked(username_or_email, ip_address)
        if is_account_locked:
            logger.warning(f"Login blocked (locked out): {username_or_email} from {ip_address}")
            return render_template(
                'login.html',
                error=f'Too many failed login attempts. Please try again in {lock_minutes} minute(s).',
            )

        db = get_db()

        logger.info(f"Login attempt for: {username_or_email}")

        # Try to find user by username or email (case-insensitive)
        from ..helpers.lab_accounts import ensure_lab_accounts_schema, constant_time_burn
        ensure_lab_accounts_schema(db)
        user_query = db.execute(
            (
                'SELECT u.id, u.username, u.is_admin, u.system_role, u.is_active, u.last_login_at, '
                'uc.password, uc.email, uc.email_verified '
                'FROM users u '
                'JOIN user_credentials uc ON u.user_credential_id = uc.id '
                'WHERE LOWER(u.username) = LOWER(%s) OR LOWER(uc.email) = LOWER(%s)'
            ),
            (username_or_email.strip(), username_or_email.strip()),
        ).fetchone()

        if not user_query:
            constant_time_burn(password_raw)
            logger.warning(f"User not found: {username_or_email}")
        elif user_query['is_active'] is not None and not bool(user_query['is_active']):
            constant_time_burn(password_raw)
            logger.warning(f"Deactivated account login rejected: {username_or_email}")
            AccountLockout.record_failed_login(username_or_email, ip_address)
            return render_template('login.html', error='Invalid email/username or password')
        else:
            logger.info(f"User found: {user_query['username']}, email_verified: {user_query['email_verified']}")

        password_matches = False
        if user_query:
            stored_password = user_query['password']
            logger.info(f"Password type: {type(stored_password)}")
            if isinstance(stored_password, str):
                stored_password = stored_password.encode('utf-8')
            elif isinstance(stored_password, memoryview):
                stored_password = stored_password.tobytes()

            if isinstance(stored_password, (bytes, bytearray)):
                password_matches = bcrypt.checkpw(password, stored_password)
                logger.info(f"Password matches: {password_matches}")

        if user_query and password_matches:
            try:
                logger.info("Password verified successfully, proceeding with login logic")

                # Email verification disabled
                # if not user_query['email_verified'] and not user_query['is_admin']:
                #     logger.warning(f"Login blocked: email not verified for {user_query['username']}")
                #     return render_template('login.html', error='Please verify your email before logging in.')

                user_id = user_query['id']

                # Successful password auth clears any failed-attempt lockout.
                AccountLockout.clear_lockout(username_or_email, ip_address)

                # Per-user security toggles (defensive defaults: remember on, 2FA-trust off).
                remember_pref, trust_pref = _get_login_prefs(db, user_id)
                effective_remember = remember_me and remember_pref
                session['_remember_me_effective'] = effective_remember

                last_login_at_str = user_query['last_login_at']
                seven_days_ago = datetime.utcnow() - timedelta(days=7)

                logger.info(f"last_login_at: {last_login_at_str}, type: {type(last_login_at_str)}")

                # Condition 1: Last login was more than 7 days ago
                is_last_login_old = True
                if last_login_at_str:
                    last_login_at = parse_sqlite_timestamp(last_login_at_str)
                    logger.info(f"Parsed last_login_at: {last_login_at}")

                    # Handle offset-aware vs offset-naive comparison safely
                    if last_login_at:
                        if last_login_at.tzinfo is not None and seven_days_ago.tzinfo is None:
                            # Convert naive target to aware (UTC) if source is aware
                            seven_days_ago = seven_days_ago.replace(tzinfo=timezone.utc)
                        elif last_login_at.tzinfo is None and seven_days_ago.tzinfo is not None:
                            # Should not happen with utcnow(), but just in case
                            last_login_at = last_login_at.replace(tzinfo=timezone.utc)

                        if last_login_at > seven_days_ago:
                            is_last_login_old = False

                # Condition 2: New device/IP (ip_address/user_agent computed above)
                logger.info(f"Checking device: IP={ip_address}, UA={user_agent[:50]}...")
                known_device = is_known_device(db, user_id, ip_address, user_agent)
                logger.info(f"Known device: {known_device is not None}, old login: {is_last_login_old}")

                # 2FA is disabled by default for open-source/local deployments (kept intact below as dormant code)
                if not Config.ENABLE_2FA or Config.DEV_MODE:
                    logger.info(f"Direct login (2FA disabled / DEV_MODE) for user {user_query['username']}.")
                    session.permanent = True
                    session['user_id'] = user_id
                    session['username'] = user_query['username']
                    session['is_admin'] = bool(user_query['is_admin'])
                    session['system_role'] = user_query['system_role'] or ('admin' if user_query['is_admin'] else 'scientist')

                    # Update login history
                    now = datetime.utcnow()
                    db.execute('UPDATE users SET last_login_at = %s, last_login_ip = %s WHERE id = %s', (now, ip_address, user_id))
                    record_login(db, user_id, ip_address, user_agent)
                    db.commit()

                    logger.info(f"Login successful for {user_query['username']}, redirecting to {'notebook' if notebook_only else 'dashboard'}")
                    session.pop('_remember_me_effective', None)
                    session.pop('notebook_only', None)
                    resp = make_response(redirect(url_for('notebook_standalone') if notebook_only else url_for('oversight', auto=1)))
                    if effective_remember:
                        set_remember_cookie(resp, issue_remember_token(db, user_id, ip_address, user_agent))
                    return resp
                else:
                    # Per-user 2FA disable: if the user has a two_factor_auth row with
                    # is_enabled = 0, log in directly without sending an email code.
                    if _is_2fa_disabled(db, user_id):
                        logger.info(f"2FA disabled for user {user_query['username']}; logging in directly.")
                        session.permanent = True
                        session['user_id'] = user_id
                        session['username'] = user_query['username']
                        session['is_admin'] = bool(user_query['is_admin'])

                        now = datetime.utcnow()
                        db.execute('UPDATE users SET last_login_at = %s, last_login_ip = %s WHERE id = %s', (now, ip_address, user_id))
                        record_login(db, user_id, ip_address, user_agent)
                        db.commit()

                        session.pop('_remember_me_effective', None)
                        session.pop('notebook_only', None)
                        resp = make_response(redirect(url_for('notebook_standalone') if notebook_only else url_for('oversight', auto=1)))
                        if effective_remember:
                            set_remember_cookie(resp, issue_remember_token(db, user_id, ip_address, user_agent))
                        return resp

                    # 2FA-trust skip: if the user enabled per-IP trust and presents a
                    # valid, non-expired trust cookie from the SAME origin IP, skip the
                    # email code. Defaults keep 2FA mandatory; the skip never extends
                    # the 3-day trust window (it decays from the original 2FA event).
                    trust_cookie = request.cookies.get(TRUST_COOKIE)
                    if trust_pref and trust_cookie and check_trust_device(db, user_id, trust_cookie, ip_address):
                        logger.info(f"2FA skipped via trusted device+IP for user {user_query['username']}")
                        session.permanent = True
                        session['user_id'] = user_id
                        session['username'] = user_query['username']
                        session['is_admin'] = bool(user_query['is_admin'])
                        now = datetime.utcnow()
                        db.execute('UPDATE users SET last_login_at = %s, last_login_ip = %s WHERE id = %s', (now, ip_address, user_id))
                        record_login(db, user_id, ip_address, user_agent)
                        db.commit()
                        session.pop('_remember_me_effective', None)
                        session.pop('notebook_only', None)
                        resp = make_response(redirect(url_for('notebook_standalone') if notebook_only else url_for('oversight', auto=1)))
                        if effective_remember:
                            set_remember_cookie(resp, issue_remember_token(db, user_id, ip_address, user_agent))
                        return resp

                    # 2FA is ALWAYS required in production
                    logger.info(f"2FA required for ALL logins: user={user_query['username']}, old_login={is_last_login_old}, new_device={not known_device}")
                    two_fa_code = secrets.token_hex(3).upper()
                    session['2fa_code'] = two_fa_code
                    session['2fa_user_id'] = user_id
                    session['2fa_email'] = user_query['email']
                    logger.info(f"Triggering 2FA for user {user_query['username']}.")

                    email_sent = send_email(user_query['email'], 'Your Phonos Login Code', f'<p>Your login verification code is:</p><h2 style="font-size:32px;letter-spacing:6px;font-family:monospace;color:#10b981;">{two_fa_code}</h2><p style="color:#888;font-size:13px;">This code expires after use. If you did not attempt to log in, please ignore this email.</p>', async_=False)
                    if not email_sent:
                        logger.error(f"Failed to send 2FA email to {user_query['email']}. Login blocked.")
                        return render_template('login.html', error='Could not send 2FA code. Please try again later or contact support.')

                    logger.info("Redirecting to 2FA verification")
                    return redirect(url_for('verify_2fa'))
            except Exception as e:
                logger.error(f"Login error after password verification: {e}", exc_info=True)
                return render_template('login.html', error='An error occurred during login. Please try again.')
        else:
            # Wrong username/password — count it toward the (username, IP) lockout.
            AccountLockout.record_failed_login(username_or_email, ip_address)
            return render_template('login.html', error='Invalid email/username or password')
    return render_template('login.html')

@app.route('/verify_2fa', methods=['GET', 'POST'])
@csrf_protect
def verify_2fa():
    logger.info(f"verify_2fa accessed, session keys: {list(session.keys())}")
    if '2fa_user_id' not in session:
        logger.warning("2FA verification failed: no 2fa_user_id in session, redirecting to login")
        return redirect(url_for('login'))

    if request.method == 'POST':
        code_raw = request.form.get('2fa_code')
        if not code_raw:
            return render_template('verify_2fa.html', error='Please enter your 2FA code.'), 400
        code = code_raw.upper()

        user_id = session['2fa_user_id']
        ip_address = request.remote_addr or 'unknown'
        # Namespaced so 2FA-code guessing attempts are tracked separately from
        # username/password lockouts, reusing AccountLockout's existing table
        # and 5-attempts/15-minute policy rather than adding a new one.
        lockout_key = f"2fa:{user_id}"

        is_code_locked, lock_minutes = AccountLockout.is_locked(lockout_key, ip_address)
        if is_code_locked:
            logger.warning(f"2FA verification blocked (locked out): user_id={user_id} from {ip_address}")
            return render_template(
                'verify_2fa.html',
                error=f'Too many failed attempts. Please try again in {lock_minutes} minute(s).',
            )

        if '2fa_code' in session and code == session['2fa_code']:
            AccountLockout.clear_lockout(lockout_key, ip_address)

            db = get_db()
            user = db.execute('SELECT * FROM users WHERE id = %s', (user_id,)).fetchone()
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = bool(user['is_admin'])

            # --- NEW: Update login history after successful 2FA ---
            user_agent = request.user_agent.string
            now = datetime.utcnow()

            db.execute('UPDATE users SET last_login_at = %s, last_login_ip = %s WHERE id = %s', (now, ip_address, user_id))
            # Only insert if it's a new device, otherwise the login function already handled it.
            # On second thought, let's just always log the successful 2FA login.
            record_login(db, user_id, ip_address, user_agent)
            db.commit()

            # Clear 2FA data from session
            session.pop('2fa_code', None)
            session.pop('2fa_user_id', None)
            session.pop('2fa_email', None)
            
            # Check if user selected notebook-only mode
            notebook_only = session.pop('notebook_only', False)
            resp = make_response(redirect(
                url_for('notebook_standalone') if notebook_only else url_for('oversight', auto=1)
            ))

            # Mint a remember-me cookie if the user opted in on the login form
            # (carried across the 2FA round-trip via the session).
            if session.pop('_remember_me_effective', False):
                try:
                    set_remember_cookie(resp, issue_remember_token(db, user_id, ip_address, user_agent))
                except Exception as e:
                    logger.warning(f"Could not issue remember-me token: {e}")

            # Mint a 3-day device+IP trust token if the user enabled 2FA-trust, so
            # future password logins from this device+IP can skip the email code.
            try:
                _, trust_pref = _get_login_prefs(db, user_id)
                if trust_pref:
                    set_trust_cookie(resp, issue_trust_device(db, user_id, ip_address, user_agent))
            except Exception as e:
                logger.warning(f"Could not issue 2FA-trust token: {e}")

            return resp
        else:
            AccountLockout.record_failed_login(lockout_key, ip_address)
            return render_template('verify_2fa.html', error='Invalid 2FA code.')
    return render_template('verify_2fa.html')


@app.route('/resend_2fa', methods=['POST'])
@csrf_protect
def resend_2fa():
    """Resend a new 2FA verification code to the user's email."""
    if '2fa_user_id' not in session or '2fa_email' not in session:
        logger.warning("Resend 2FA failed: no 2fa_user_id or 2fa_email in session")
        return redirect(url_for('login'))

    # Generate new code
    new_code = secrets.token_hex(3).upper()
    session['2fa_code'] = new_code
    email = session['2fa_email']

    logger.info(f"Resending 2FA code to {email}.")

    # Send email
    email_sent = send_email(email, 'Your New Phonos Login Code', f'<p>Your new login verification code is:</p><h2 style="font-size:32px;letter-spacing:6px;font-family:monospace;color:#10b981;">{new_code}</h2><p style="color:#888;font-size:13px;">If you did not request this code, please ignore this email.</p>', async_=False)

    if email_sent:
        return render_template('verify_2fa.html', success='A new verification code has been sent to your email.')
    else:
        logger.error(f"Failed to resend 2FA email to {email}")
        return render_template('verify_2fa.html', error='Failed to send verification code. Please try again.')


@app.route('/terms-of-service')
def terms_of_service():
    """Terms of Service. The English version is canonical; a German
    translation is served when the user's language preference is German."""
    return _render_legal(
        default_template='terms_of_service.html',
        lang_overrides={'de': 'terms_of_service_de.html'},
        current_date=datetime.now().strftime('%B %Y'),
    )


@app.route('/licenses')
def licenses():
    """Render the licenses and attributions page in the user's language."""
    return _render_legal(
        default_template='licenses.html',
        lang_overrides={'de': 'licenses_de.html'},
    )


@app.route('/api-docs')
def api_docs():
    """Render the API documentation page."""
    import time
    response = make_response(render_template('api_docs.html', v=int(time.time())))
    # Prevent caching to ensure users always see latest version
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/datenschutz')
def datenschutz():
    """Render the data protection (Datenschutz/Privacy Policy) page in
    the user's language. The German version is canonical."""
    return _render_legal(
        default_template='datenschutz.html',
        lang_overrides={'en': 'datenschutz_en.html'},
        current_date=datetime.now().strftime('%B %Y'),
    )


@app.route('/impressum')
def impressum():
    """Render the imprint (Impressum) page in the user's language. The
    German version is canonical as required by German law."""
    return _render_legal(
        default_template='impressum.html',
        lang_overrides={'en': 'impressum_en.html'},
    )


@app.route('/register', methods=['GET', 'POST'])
def register():
    """Handle user registration with email verification.

    Presents itself as the Trusted Tester application while the gate is on
    (new accounts only reach regular access after admin review)."""
    tt_mode = gate_enabled()
    if request.method == 'GET':
        return render_template('register.html', trusted_tester_mode=tt_mode)

    # POST - Process registration
    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip().lower()
    legal_name = (request.form.get('legal_name') or '').strip()
    university = (request.form.get('university') or '').strip() or None
    age = request.form.get('age')
    address = (request.form.get('address') or '').strip() or None
    password = request.form.get('password') or ''
    confirm_password = request.form.get('confirm_password') or ''
    accept_tos = request.form.get('accept_tos')

    # Validation
    if not username or not email or not legal_name or not password:
        return render_template('register.html', trusted_tester_mode=tt_mode, error='Please fill in all required fields.')

    if password != confirm_password:
        return render_template('register.html', trusted_tester_mode=tt_mode, error='Passwords do not match.')

    if len(password) < 8:
        return render_template('register.html', trusted_tester_mode=tt_mode, error='Password must be at least 8 characters.')

    if not accept_tos:
        return render_template('register.html', trusted_tester_mode=tt_mode, error='You must accept the Terms and Privacy Policy.')

    # Check if user already exists
    db = get_db()
    if db.execute('SELECT id FROM users WHERE username = %s', (username,)).fetchone():
        return render_template('register.html', trusted_tester_mode=tt_mode, error='This username is already taken.')

    if db.execute('SELECT id FROM user_credentials WHERE email = %s', (email,)).fetchone():
        return render_template('register.html', trusted_tester_mode=tt_mode, error='This email is already registered.')

    # Direct account creation when 2FA/email-verification is disabled (all 2FA/email code preserved below as dormant code)
    if not Config.ENABLE_2FA:
        from ..helpers.lab_accounts import create_local_account, ensure_default_lab_team, ROLE_ADMIN, ROLE_SCIENTIST
        if not getattr(Config, 'ALLOW_PUBLIC_SIGNUP', True):
            cnt_row = db.execute('SELECT COUNT(*) as cnt FROM users').fetchone()
            cnt = cnt_row['cnt'] if isinstance(cnt_row, dict) or hasattr(cnt_row, 'keys') else cnt_row[0]
            if int(cnt or 0) > 0:
                return render_template('register.html', trusted_tester_mode=tt_mode, error='Public registration is disabled on this lab instance. Please ask your Lab Administrator to create your account.')
        try:
            cnt_row = db.execute('SELECT COUNT(*) as cnt FROM users').fetchone()
            cnt = int((cnt_row['cnt'] if isinstance(cnt_row, dict) or hasattr(cnt_row, 'keys') else cnt_row[0]) or 0)
            assigned_role = ROLE_ADMIN if cnt == 0 else ROLE_SCIENTIST
            acct = create_local_account(
                db,
                username=username,
                password=password,
                system_role=assigned_role,
                email=email,
                legal_name=legal_name or username,
                actor=username,
            )
            if cnt == 0:
                ensure_default_lab_team(db, acct['id'], os.environ.get('PHONOS_LAB_NAME', 'Phonos Lab'))
                db.commit()
            if university or address or (age and str(age).isdigit()):
                db.execute(
                    'UPDATE users SET university = %s, address = %s, age = %s WHERE id = %s',
                    (university, address, int(age) if age and str(age).isdigit() else None, acct['id']),
                )
                db.commit()
            session.permanent = True
            session['user_id'] = acct['id']
            session['username'] = acct['username']
            session['is_admin'] = acct['is_admin']
            session['system_role'] = acct['system_role']
            return redirect(url_for('oversight', auto=1))
        except ValueError as ve:
            return render_template('register.html', trusted_tester_mode=tt_mode, error=str(ve))
        except Exception as exc:
            logger.error(f"Direct registration error: {exc}", exc_info=True)
            return render_template('register.html', trusted_tester_mode=tt_mode, error='An internal error occurred during account creation.')

    # Hash password (dormant email-verification / 2FA branch when ENABLE_2FA=true)
    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

    # Generate verification token and store user data in Redis
    verification_token = secrets.token_urlsafe(32)
    user_data = {
        'username': username,
        'email': email,
        'legal_name': legal_name,
        'university': university,
        'age': int(age) if age and age.isdigit() else None,
        'address': address,
        'password': hashed_password.decode('utf-8'),
        'tos_ip_address': request.remote_addr or 'unknown'
    }

    r = get_redis_connection()
    try:
        r.setex(f"verification:{verification_token}", 86400, json.dumps(user_data))  # 24 hours
    except redis.RedisError as e:
        logger.error(f"Redis error during registration: {e}", exc_info=True)
        return render_template('register.html', trusted_tester_mode=tt_mode, error='An internal error occurred. Please try again later.')

    # Send verification email
    verification_url = url_for('verify_email', token=verification_token, _external=True)
    subject = 'Verify your Phonos account'
    body = f'''<p>Thank you for registering with Phonos!</p>
<p>Please click the link below to verify your email address and complete your registration:</p>
<p><a href="{verification_url}" style="background: #10B981; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; display: inline-block;">Verify Email</a></p>
<p>Or copy this link: {verification_url}</p>
<p style="color: #888; font-size: 13px;">This link expires in 24 hours.</p>'''

    if not send_email(email, subject, body, async_=False):
        logger.error(f"Failed to send verification email to {email}")
        return render_template('register.html', trusted_tester_mode=tt_mode, error='Failed to send verification email. Please contact support.')

    # Show success message with disclaimer modal
    return render_template('register_success.html')


@app.route('/verify_email/<token>')
def verify_email(token):
    r = get_redis_connection()
    try:
        user_data_json = r.get(f"verification:{token}")
    except redis.RedisError as e:
        logger.error(f"Redis error during email verification: {e}", exc_info=True)
        return render_template('message.html', message='An internal error occurred. Please try again later.')

    if not user_data_json:
        return render_template('message.html', message='Invalid or expired verification token.')

    if isinstance(user_data_json, bytes):
        user_data_json = user_data_json.decode('utf-8')
    elif not isinstance(user_data_json, str):
        logger.error("Unexpected verification payload type from Redis: %s", type(user_data_json).__name__)
        return render_template('message.html', message='Invalid or expired verification token.')

    user_data = json.loads(user_data_json)
    db = get_db()

    # Double-check for integrity errors, in case another user registered with the same details
    if db.execute('SELECT id FROM users WHERE username = %s', (user_data['username'],)).fetchone() or \
       db.execute('SELECT id FROM user_credentials WHERE email = %s', (user_data['email'],)).fetchone():
        try:
            r.delete(f"verification:{token}")
        except redis.RedisError:
            pass
        return render_template('message.html', message='This username or email has already been registered.')

    db.execute('BEGIN')
    try:
        user_uuid = str(uuid.uuid4())
        user_plan = user_data.get('plan', DEFAULT_PLAN)
        result = db.execute('INSERT INTO users (user_id, username, university, age, legal_name, address, plan, access_tier) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                            (user_uuid, user_data['username'], user_data.get('university'), user_data.get('age'), user_data.get('legal_name'), user_data.get('address'), user_plan, initial_access_tier()))
        
        user_id = result.lastrowid

        hashed_password_bytes = user_data['password'].encode('utf-8')
        tos_ip = user_data.get('tos_ip_address', 'unknown')
        db.execute('INSERT INTO user_credentials (user_id, email, password, email_verified, plan, tos_accepted, tos_accepted_at, tos_ip_address) VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)',
                   (user_id, user_data['email'], hashed_password_bytes, 1, user_plan, 1, tos_ip))

        db.execute('UPDATE users SET user_credential_id = (SELECT id FROM user_credentials WHERE user_id = %s) WHERE id = %s', (user_id, user_id))
        db.commit()

        message = 'Email verified successfully! You can now log in.'
        if gate_enabled():
            message = ('Email verified successfully! Phonos is open through the Trusted Tester Program: '
                       'log in to apply for access.')

        try:
            r.delete(f"verification:{token}")
        except redis.RedisError as e:
            logger.error(f"Failed to delete verification token {token} from Redis after successful registration.", exc_info=True)

        return render_template('message.html', message=message)

    except psycopg2.IntegrityError:
        db.rollback()
        return render_template('message.html', message='This username or email has already been registered.')
    except Exception as e:
        db.rollback()
        logger.error(f"Database error during user creation from verification token for {user_data.get('username')}: {str(e)}", exc_info=True)
        return render_template('message.html', message='An internal error occurred during account creation.')


@app.route('/logout')
def logout():
    # Revoke this device's remember-me token (single-device sign-out) before
    # dropping the session, then clear both auth cookies on the response.
    remember_cookie = request.cookies.get(REMEMBER_COOKIE)
    if remember_cookie:
        try:
            revoke_remember_token_by_cookie(get_db(), remember_cookie)
        except Exception as e:
            logger.warning(f"Could not revoke remember-me token on logout: {e}")
    session.clear()
    resp = make_response(redirect(url_for('login')))
    clear_remember_cookie(resp)
    clear_trust_cookie(resp)
    return resp

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        db = get_db()
        user_credential = db.execute('SELECT * FROM user_credentials WHERE email = %s', (email,)).fetchone() if email else None
        if user_credential:
            if not Config.ENABLE_2FA:
                reset_token = secrets.token_urlsafe(32)
                expires_at = datetime.utcnow() + timedelta(hours=1)
                db.execute(
                    'INSERT INTO password_reset_tokens (user_credential_id, token, expires_at) VALUES (%s, %s, %s)',
                    (user_credential['id'], reset_token, expires_at),
                )
                db.commit()
                reset_url = url_for('reset_password', token=reset_token, _external=True)
                send_email(email, 'Password Reset Request', f'Please click this link to reset your password: {reset_url}')
                return render_template('message.html', message='If SMTP is configured, a password reset link has been sent. On local lab instances without SMTP, ask your Lab Administrator to reset your password via Account Manager.')
            r = get_redis_connection()
            two_fa_code = secrets.token_hex(3).upper()
            temp_token = secrets.token_urlsafe(32)

            # Store 2FA code and email in Redis for verification
            r.setex(f"password_reset_2fa:{temp_token}", timedelta(minutes=15), json.dumps({
                'email': email,
                '2fa_code': two_fa_code,
                'user_credential_id': user_credential['id']
            }))

            send_email(email, 'Password Reset Verification', f'Your verification code is: {two_fa_code}')
            return redirect(url_for('verify_password_reset_2fa', token=temp_token))
        else:
            return render_template('forgot_password.html', error='Email not found.')
    return render_template('forgot_password.html')

@app.route('/verify_password_reset_2fa/<token>', methods=['GET', 'POST'])
@csrf_protect
def verify_password_reset_2fa(token):
    r = get_redis_connection()
    pending_data_json = r.get(f"password_reset_2fa:{token}")

    if not pending_data_json:
        return render_template('message.html', message='Invalid or expired verification token.')

    pending_data = json.loads(pending_data_json)

    if request.method == 'POST':
        # Codes are uppercase hex (secrets.token_hex(3).upper()); accept any
        # case/whitespace and compare in constant time.
        code = (request.form.get('2fa_code') or '').strip().upper()
        expected = str(pending_data.get('2fa_code', '')).strip().upper()
        if code and expected and secrets.compare_digest(code, expected):
            try:
                db = get_db()
                reset_token = secrets.token_urlsafe(32)
                expires_at = datetime.utcnow() + timedelta(hours=1)
                db.execute('INSERT INTO password_reset_tokens (user_credential_id, token, expires_at) VALUES (%s, %s, %s)',
                           (pending_data['user_credential_id'], reset_token, expires_at))
                db.commit()

                reset_url = url_for('reset_password', token=reset_token, _external=True)
                send_email(pending_data['email'], 'Password Reset Request', f'Please click this link to reset your password: {reset_url}')

                r.delete(f"password_reset_2fa:{token}")
            except Exception as e:
                logger.error(f"Error issuing password reset token for {token}: {e}", exc_info=True)
                return render_template('verify_password_reset_2fa.html', token=token,
                                       error='Something went wrong sending your reset link. Please try again.')
            return render_template('message.html', message='A password reset link has been sent to your email.')
        else:
            return render_template('verify_password_reset_2fa.html', token=token, error='Invalid verification code.')

    return render_template('verify_password_reset_2fa.html', token=token)


@app.route('/resend_password_reset_2fa/<token>', methods=['POST'])
@csrf_protect
def resend_password_reset_2fa(token):
    """Resend 2FA code for password reset verification."""
    # Called via fetch() from the verify page, which expects a JSON response.
    r = get_redis_connection()
    pending_data_json = r.get(f"password_reset_2fa:{token}")

    if not pending_data_json:
        return jsonify({'error': 'Invalid or expired verification token.'}), 400

    try:
        pending_data = json.loads(pending_data_json)
        email = pending_data['email']

        # Generate new 2FA code
        new_code = secrets.token_hex(3).upper()
        pending_data['2fa_code'] = new_code

        # Update Redis with new code (keep same expiration)
        ttl = r.ttl(f"password_reset_2fa:{token}")
        if ttl and ttl > 0:
            r.setex(f"password_reset_2fa:{token}", ttl, json.dumps(pending_data))
        else:
            # If TTL expired during processing, set a fresh 15 minutes
            r.setex(f"password_reset_2fa:{token}", timedelta(minutes=15), json.dumps(pending_data))

        # Send new code
        email_sent = send_email(email, 'Password Reset Verification', f'Your new verification code is: {new_code}\n\nThis code will expire in 15 minutes.')

        if email_sent:
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Failed to send verification code. Please try again.'}), 502
    except Exception as e:
        logger.error(f"Error resending password reset 2FA for token {token}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to resend verification code.'}), 500


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    db = get_db()
    token_data = db.execute('SELECT * FROM password_reset_tokens WHERE token = %s', (token,)).fetchone()
    if not token_data or token_data['expires_at'] < datetime.utcnow():
        return render_template('message.html', message='Invalid or expired password reset token.')

    if request.method == 'POST':
        new_password = request.form.get('password') or ''
        if not new_password:
            return render_template('reset_password.html', token=token, error='Please enter a new password.')
        hashed_password = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
        db.execute('UPDATE user_credentials SET password = %s WHERE id = %s', (hashed_password, token_data['user_credential_id']))
        db.execute('DELETE FROM password_reset_tokens WHERE id = %s', (token_data['id'],))
        db.commit()
        return render_template('message.html', message='Your password has been reset successfully.')

    return render_template('reset_password.html', token=token)

# --- Profile Settings ---
@app.route('/profile_settings')
@login_required
def profile_settings():
    """Public profile customization page"""
    return render_template('profile_settings.html')


@app.route('/custom_models')
@login_required
def custom_models():
    """Custom user models management page"""
    return render_template('custom_models.html')


@app.route('/user_model_builder')
@app.route('/user_model_builder/<int:model_id>')
@login_required
def user_model_builder(model_id: int = None):
    """Standalone custom-model schema builder (used for deep links and full-page editing)."""
    return render_template('user_model_builder.html', model_id=model_id)


@app.route('/profile/<int:user_id>')
def public_profile(user_id):
    """View a user's public profile"""
    from ..helpers.profile_settings import get_user_profile_settings

    db = get_db()

    # Get user basic info
    user = db.execute('''
        SELECT id, username, profile_picture_url, company, university, created_at
        FROM users WHERE id = %s
    ''', (user_id,)).fetchone()

    if not user:
        return render_template('404.html'), 404

    # Get profile settings
    settings = get_user_profile_settings(None, user_id)

    # Get public projects
    projects = db.execute('''
        SELECT p.id, p.name, p.description, p.view_count, p.vote_score, p.created_at,
               p.category_id
        FROM projects p
        JOIN project_assignments pa ON p.id = pa.project_id
        WHERE pa.user_id = %s AND p.is_private = 0
        ORDER BY p.created_at DESC
        LIMIT 10
    ''', (user_id,)).fetchall()

    # Get statistics
    total_views = sum(p['view_count'] for p in projects)
    total_votes = sum(p['vote_score'] for p in projects)

    return render_template('public_profile.html',
                         user=user,
                         settings=settings,
                         projects=projects,
                         total_projects=len(projects),
                         total_views=total_views,
                         total_votes=total_votes)


# --- Demo API Routes (DISABLED - users must register/login) ---
# All demo routes have been removed. Users must create an account to use the platform.

# --- Documentation Routes ---
# documentation.html / documentation_page.html were removed in 25aa88bdf; the
# public docs live at /api-docs. Nothing links to /docs/<slug>, so both redirect.
@app.route('/docs')
@app.route('/docs/<slug>')
def documentation_redirect(slug=None):
    return redirect(url_for('api_docs'), code=302)


# --- Retired model pages ---
# NVIDIA retired these endpoints (see production/config.py, is_active False).
# The page routes live in api_esmfold_re.py / api_alphafold2_re.py, so the
# redirect is a before_request hook: it wins whatever the registration order.
# There is no separate AF2-multimer page (only API routes under /api/alphafold2-re/multimer*).
RETIRED_MODEL_PAGES = {
    '/esmfold-re': 'esmfold-re',
    '/alphafold2-re': 'alphafold2-re',
}


@app.before_request
def redirect_retired_model_pages():
    model_id = RETIRED_MODEL_PAGES.get(request.path.rstrip('/') or '/')
    if model_id and request.method == 'GET':
        return redirect(f'/dashboard?retired={model_id}#submit-job', code=302)


# --- Centralized Error Handlers ---
@app.errorhandler(404)
def not_found_error(error):
    if request.path.startswith('/api/'):
        return jsonify({'status': 'error', 'message': 'The requested resource was not found.'}), 404
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    if request.path.startswith('/api/'):
        return jsonify({'status': 'error', 'message': 'An internal server error occurred.'}), 500
    return render_template('500.html'), 500

