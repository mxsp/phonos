"""Tests for the Trusted Tester Program (applicant -> tester -> regular access).

Covers:
  - New accounts start as 'regular' with the gate off and as 'applicant' with it on
  - The gate keeps applicants on /trusted-tester (pages redirect, APIs return 403)
  - Applying through the page form and the JSON API, duplicate applications, validation
  - Admin review: approve, reject, graduate, revoke, illegal transitions, non-admin 403
  - Graduation moves the account to regular access; revoking puts a tester back to applicant
  - Opt-in track for existing regular users while the gate is off
"""
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')

patch('production.config.Config.DEV_MODE', False).start()
patch('redis.ConnectionPool').start()
patch('production.database.validate_database_config', return_value=True).start()

from production.main import app  # noqa: E402
from production.database import init_db  # noqa: E402
from phonos_app.helpers.database import get_db  # noqa: E402

MOTIVATION = 'I want to test docking pipelines on our kinase targets and report UI issues.'


class TrustedTesterTestCase(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        app.config['MAIL_DEFAULT_SENDER'] = 'noreply@testing.com'
        self.app = app.test_client()
        with app.app_context():
            init_db()
        self._gate(False)
        self.mail = patch('phonos_app.routes.api_trusted_tester.send_email', return_value=True).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        os.close(self.db_fd)
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    # -- helpers ------------------------------------------------------------

    def _gate(self, on):
        env = patch.dict(os.environ, {'TRUSTED_TESTER_GATE': 'true' if on else 'false'})
        env.start()
        self.addCleanup(env.stop)

    def _register(self, username, email=None, password='password1'):
        email = email or f'{username}@example.org'
        with patch('phonos_app.routes.pages.send_email', return_value=True), \
             patch('phonos_app.routes.pages.secrets.token_urlsafe', return_value=f'token-for-{username}'):
            self.app.post('/register', data=dict(
                username=username, email=email, legal_name=username.title(),
                password=password, confirm_password=password, accept_tos='on',
            ))
            return self.app.get(f'/verify_email/token-for-{username}')

    def _user(self, username):
        with app.app_context():
            return get_db().execute('SELECT id, access_tier FROM users WHERE username = %s',
                                    (username,)).fetchone()

    def _login_as(self, username, admin=False):
        row = self._user(username)
        if admin:
            with app.app_context():
                db = get_db()
                db.execute('UPDATE users SET is_admin = 1 WHERE id = %s', (row['id'],))
                db.commit()
        with self.app.session_transaction() as sess:
            sess.clear()
            sess['user_id'] = row['id']
            sess['username'] = username
            sess['is_admin'] = admin
        return row['id']

    def _apply(self, **extra):
        return self.app.post('/api/trusted-tester/apply', json={'motivation': MOTIVATION, **extra})

    def _application_id(self, username):
        with app.app_context():
            return get_db().execute(
                'SELECT a.id FROM trusted_tester_applications a JOIN users u ON u.id = a.user_id '
                'WHERE u.username = %s', (username,)).fetchone()['id']

    def _review(self, application_id, action, note=None):
        return self.app.post(f'/api/admin/trusted-testers/{application_id}/{action}',
                             json={'note': note} if note else {})

    # -- sign-up tier -------------------------------------------------------

    def test_new_account_is_regular_when_gate_off(self):
        self._register('alice')
        self.assertEqual(self._user('alice')['access_tier'], 'regular')

    def test_new_account_is_applicant_when_gate_on(self):
        self._gate(True)
        resp = self._register('bob')
        self.assertIn(b'Trusted Tester Program', resp.data)
        self.assertEqual(self._user('bob')['access_tier'], 'applicant')

    # -- landing page / register framing ------------------------------------

    def test_landing_page_offers_apply_cta_not_sign_up(self):
        resp = self.app.get('/')
        self.assertIn(b'auth.applyCta', resp.data)
        self.assertNotIn(b'auth.signUpCta', resp.data)

    def test_register_page_shows_trusted_tester_framing_when_gate_on(self):
        self._gate(True)
        resp = self.app.get('/register')
        self.assertIn(b'register.trustedTesterTitle', resp.data)

    def test_register_page_has_no_trusted_tester_framing_when_gate_off(self):
        resp = self.app.get('/register')
        self.assertNotIn(b'register.trustedTesterTitle', resp.data)

    # -- gate ---------------------------------------------------------------

    def test_gate_redirects_applicant_pages_and_blocks_apis(self):
        self._gate(True)
        self._register('carol')
        self._login_as('carol')
        resp = self.app.get('/dashboard')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/trusted-tester'))
        resp = self.app.get('/api/projects')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['code'], 'trusted_tester_pending')
        self.assertEqual(self.app.get('/trusted-tester').status_code, 200)
        self.assertEqual(self.app.get('/api/trusted-tester/status').status_code, 200)

    def test_gate_off_lets_applicant_tier_through(self):
        self._gate(True)
        self._register('dave')
        self._gate(False)
        self._login_as('dave')
        self.assertNotEqual(self.app.get('/api/projects').status_code, 403)

    def test_gate_never_blocks_admins(self):
        self._gate(True)
        self._register('erin')
        self._login_as('erin', admin=True)
        self.assertNotEqual(self.app.get('/api/projects').status_code, 403)

    # -- applying -----------------------------------------------------------

    def test_apply_requires_login(self):
        self.assertEqual(self._apply().status_code, 401)

    def test_apply_validates_motivation(self):
        self._register('frank')
        self._login_as('frank')
        resp = self.app.post('/api/trusted-tester/apply', json={'motivation': 'too short'})
        self.assertEqual(resp.status_code, 400)

    def test_apply_then_duplicate(self):
        self._register('gina')
        self._login_as('gina')
        resp = self._apply(institution='HS Anhalt', use_case='docking')
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body['application']['status'], 'pending')
        self.assertEqual(body['application']['institution'], 'HS Anhalt')
        self.assertEqual(self._apply().status_code, 409)

    def test_apply_through_page_form(self):
        self._gate(True)
        self._register('hank')
        self._login_as('hank')
        resp = self.app.post('/trusted-tester', data={'motivation': MOTIVATION, 'use_case': 'design'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('notice=applied', resp.headers['Location'])
        page = self.app.get('/trusted-tester?notice=applied')
        self.assertIn(b'Pending review', page.data)

    # -- review -------------------------------------------------------------

    def test_full_path_applicant_to_tester_to_regular(self):
        self._gate(True)
        self._register('ivy')
        self._register('root')
        self._login_as('ivy')
        self._apply()
        app_id = self._application_id('ivy')

        self._login_as('root', admin=True)
        listing = self.app.get('/api/admin/trusted-testers?status=pending').get_json()
        self.assertEqual([a['username'] for a in listing['applications']], ['ivy'])

        resp = self._review(app_id, 'approve', note='Welcome aboard')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['access_tier'], 'tester')
        self.assertEqual(self._user('ivy')['access_tier'], 'tester')
        self.mail.assert_called()

        self._login_as('ivy')
        self.assertNotEqual(self.app.get('/api/projects').status_code, 403)
        status = self.app.get('/api/trusted-tester/status').get_json()
        self.assertTrue(status['is_trusted_tester'])

        self._login_as('root', admin=True)
        resp = self._review(app_id, 'graduate')
        self.assertEqual(resp.get_json()['access_tier'], 'regular')
        self._login_as('ivy')
        status = self.app.get('/api/trusted-tester/status').get_json()
        self.assertEqual(status['application']['status'], 'graduated')
        self.assertIsNotNone(status['application']['graduated_at'])
        self.assertTrue(status['has_regular_access'])

    def test_reject_then_reapply(self):
        self._gate(True)
        self._register('jack')
        self._register('root')
        self._login_as('jack')
        self._apply()
        app_id = self._application_id('jack')
        self._login_as('root', admin=True)
        self.assertEqual(self._review(app_id, 'reject').status_code, 200)
        self.assertEqual(self._user('jack')['access_tier'], 'applicant')
        self.assertEqual(self._review(app_id, 'revoke').status_code, 409)
        self._login_as('jack')
        self.assertEqual(self._apply().status_code, 201)

    def test_revoke_returns_tester_to_applicant(self):
        self._gate(True)
        self._register('kim')
        self._register('root')
        self._login_as('kim')
        self._apply()
        app_id = self._application_id('kim')
        self._login_as('root', admin=True)
        self._review(app_id, 'approve')
        self._review(app_id, 'revoke')
        self.assertEqual(self._user('kim')['access_tier'], 'applicant')

    def test_opt_in_regular_user_keeps_regular_tier(self):
        self._register('lea')
        self._register('root')
        self._login_as('lea')
        self._apply()
        app_id = self._application_id('lea')
        self._login_as('root', admin=True)
        self._review(app_id, 'approve')
        self.assertEqual(self._user('lea')['access_tier'], 'regular')
        self._review(app_id, 'revoke')
        self.assertEqual(self._user('lea')['access_tier'], 'regular')

    def test_review_requires_admin_and_known_action(self):
        self._register('mia')
        self._login_as('mia')
        self._apply()
        app_id = self._application_id('mia')
        self.assertEqual(self._review(app_id, 'approve').status_code, 403)
        self.assertEqual(self.app.get('/api/admin/trusted-testers').status_code, 403)
        self._register('root')
        self._login_as('root', admin=True)
        self.assertEqual(self._review(app_id, 'promote').status_code, 404)
        self.assertEqual(self._review(99999, 'approve').status_code, 404)
        self.assertEqual(self.app.get('/api/admin/trusted-testers?status=bogus').status_code, 400)


if __name__ == '__main__':
    unittest.main()
