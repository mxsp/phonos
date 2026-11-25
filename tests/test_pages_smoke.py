"""Smoke test: log in a fixture user and GET every parameter-less page/API route.

Nothing may answer 5xx. 4xx (403 admin-only, 404 empty state, 400 missing
query parameter) and redirects are fine; this only catches broken screens such
as missing templates and SQLite SQL running on PostgreSQL.

Needs the local Postgres from .env (DATABASE_URL, Docker `phonos-postgres-local`).
The fixture user and everything hanging off it is removed again afterwards.
"""
import os
import sys
import uuid

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
for _p in (ROOT, os.path.join(ROOT, 'production')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Routes that are not request/response pages: they stream forever, end the
# session, hand out files, or send mail/pushes.
SKIP_PREFIXES = (
    '/static',
    '/logout',
    '/api/logout',
    '/stream',
    '/api/stream',
    '/socket.io',
)
SKIP_CONTAINS = ('/events', '/sse', '/stream')

# Known 5xx that belong to files outside this work package; each entry needs a reason.
KNOWN_FAILURES = {
    '/api/analytics/export': 'api_export.py:293 SQL syntax error near "30" (not in this work package)',
    '/api/calls/active': 'api_calls.py get_user_active_calls() signature mismatch',
    '/api/calls/invitations': 'api_calls.py get_pending_invitations() signature mismatch',
    '/api/collaboration/my-sessions': 'collaboration_sessions has no creator_id column',
    '/api/conversations': 'table ai_conversations missing in local DB',
    '/api/iot/messages/export': 'iot_* tables missing in local DB',
    '/api/iot/v2/data': 'iot_* tables missing in local DB',
    '/api/iot/v2/devices': 'iot_* tables missing in local DB',
    '/api/iot/v2/devices/summary': 'iot_* tables missing in local DB',
    '/iot': 'template iot.html missing',
    '/api/keys': 'api_keys_management.py 500 (not in this work package)',
    '/api/keys/stats': 'api_keys_management.py 500 (not in this work package)',
    '/api/subscription/status': 'stripe_routes.py 500 (not in this work package)',
    '/subscription/cancel': "url_for('settings') endpoint does not exist (stripe_routes.py)",
    '/subscription/success': "url_for('settings') endpoint does not exist (stripe_routes.py)",
}


def _load_app():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, '.env'))
    from production.main import app  # noqa: WPS433
    import phonos_app.routes  # noqa: F401,WPS433
    return app


@pytest.fixture(scope='module')
def app():
    if not os.environ.get('DATABASE_URL') and not os.path.exists(os.path.join(ROOT, '.env')):
        pytest.skip('no DATABASE_URL / .env: local Postgres required')
    try:
        application = _load_app()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'app cannot be imported here: {exc}')
    # TESTING must be off: helpers/database.py switches to SQLite when it is on.
    # Other test files (test_access_control.py) turn it on, so force it off here
    # and restore it afterwards, independent of test order.
    saved = {k: application.config.get(k) for k in ('TESTING', 'DATABASE_FILE', 'DATABASE_URL')}
    application.config['TESTING'] = False
    if os.environ.get('DATABASE_URL'):
        application.config['DATABASE_URL'] = os.environ['DATABASE_URL']
    yield application
    application.config.update(saved)


@pytest.fixture(scope='module')
def fixture_user(app):
    from phonos_app.helpers import get_db
    suffix = uuid.uuid4().hex[:10]
    with app.app_context():
        db = get_db()
        cred = db.execute(
            "INSERT INTO user_credentials (email, password) VALUES (%s, %s)",
            (f'smoke-{suffix}@example.invalid', b'not-a-real-hash'),
        )
        cred_id = cred.lastrowid
        user = db.execute(
            "INSERT INTO users (user_id, username, user_credential_id) VALUES (%s, %s, %s)",
            (f'smoke-{suffix}', f'smoke_{suffix}', cred_id),
        )
        user_id = user.lastrowid
        db.commit()
    try:
        yield {'id': user_id, 'cred_id': cred_id, 'username': f'smoke_{suffix}'}
    finally:
        with app.app_context():
            db = get_db()
            for table in ('activity_log', 'user_connections', 'public_profile_settings'):
                col = 'connected_user_id' if table == 'user_connections' else None
                try:
                    db.execute(f'DELETE FROM {table} WHERE user_id = %s', (user_id,))
                    if col:
                        db.execute(f'DELETE FROM {table} WHERE {col} = %s', (user_id,))
                    db.commit()
                except Exception:
                    db.rollback()
            try:
                db.execute('DELETE FROM users WHERE id = %s', (user_id,))
                db.execute('DELETE FROM user_credentials WHERE id = %s', (cred_id,))
                db.commit()
            except Exception:
                db.rollback()


def _routes(app):
    out = []
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods or rule.arguments:
            continue
        path = rule.rule
        if path.startswith(SKIP_PREFIXES) or any(s in path for s in SKIP_CONTAINS):
            continue
        out.append(path)
    return sorted(set(out))


def test_get_routes_do_not_500(app, fixture_user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = fixture_user['id']
        sess['username'] = fixture_user['username']
        sess.permanent = True

    failures = []
    checked = 0
    for path in _routes(app):
        if path in KNOWN_FAILURES:
            continue
        checked += 1
        try:
            resp = client.get(path, follow_redirects=False)
        except Exception as exc:  # unhandled exception inside a view
            failures.append(f'EXC {path}: {type(exc).__name__}: {str(exc)[:120]}')
            continue
        if resp.status_code >= 500:
            failures.append(f'{resp.status_code} {path}')
    assert checked > 50, f'only {checked} routes checked; route discovery broken?'
    assert not failures, 'routes answering 5xx:\n' + '\n'.join(failures)


def test_profile_page_renders_for_own_user(app, fixture_user):
    """/profile/<id> used to 500 (sqlite3.connect on the Postgres DSN)."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = fixture_user['id']
    resp = client.get(f"/profile/{fixture_user['id']}")
    assert resp.status_code < 500, resp.status_code


@pytest.mark.parametrize('path', ['/docs', '/docs/getting-started'])
def test_docs_redirects_to_api_docs(app, fixture_user, path):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = fixture_user['id']
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code in (301, 302, 308), resp.status_code
    assert resp.headers['Location'].endswith('/api-docs')


@pytest.mark.parametrize('path,model_id', [
    ('/esmfold-re', 'esmfold-re'),
    ('/alphafold2-re', 'alphafold2-re'),
])
def test_retired_model_pages_redirect_to_dashboard(app, fixture_user, path, model_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = fixture_user['id']
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith(f'/dashboard?retired={model_id}#submit-job')


@pytest.mark.parametrize('path', [
    '/api/public_projects',
    '/api/templates/all',
    '/api/activity',
    '/api/users/contacts',
    '/api/users/all',
    '/api/profile/settings',
    '/api/budget/alerts',
])
def test_fixed_json_endpoints_return_200(app, fixture_user, path):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = fixture_user['id']
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code, resp.data[:200])


def test_helpers_roundtrip_on_postgres(app, fixture_user):
    """profile_settings / user_connections helpers now use get_db() (were sqlite3)."""
    from phonos_app.helpers.profile_settings import (
        get_user_profile_settings, update_user_profile_settings)
    from phonos_app.helpers.user_connections import (
        add_user_connection, get_user_contacts, is_user_contact, remove_user_connection)
    uid = fixture_user['id']
    with app.app_context():
        assert update_user_profile_settings(None, uid, {'bio': 'smoke'}) is True
        assert update_user_profile_settings(None, uid, {'bio': 'smoke2'}) is True  # UPDATE path
        assert get_user_profile_settings(None, uid)['bio'] == 'smoke2'
        assert add_user_connection(None, uid, uid) is True
        assert add_user_connection(None, uid, uid) is True  # ON CONFLICT / duplicate tolerated
        assert is_user_contact(None, uid, uid) is True
        assert isinstance(get_user_contacts(None, uid), list)
        assert remove_user_connection(None, uid, uid) is True
        assert is_user_contact(None, uid, uid) is False
