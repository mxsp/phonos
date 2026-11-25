import unittest
import os
import sys
import sqlite3
import json
import base64
import shutil
import hashlib
import re

from unittest.mock import patch, Mock

# Ensure the project root is available for module imports before patching
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Explicitly add the 'phonos' directory to sys.path
PHONOS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PHONOS_DIR not in sys.path:
    sys.path.insert(0, PHONOS_DIR)

# Ensure required configuration values exist for test execution
os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')

# --- Pre-import patches ---
# Patch configurations before the 'main' app is imported to ensure they are set correctly
# for the legacy 2FA + SaaS plan test cases in this module.
patch('production.config.Config.DEV_MODE', False).start()
patch('production.config.Config.ENABLE_2FA', True).start()
patch('production.config.Config.LOCAL_LAB_MODE', False).start()
patch('redis.ConnectionPool').start()
patch('production.database.validate_database_config', return_value=True).start()


import tempfile
from flask import Flask, session
from production.main import app, resolve_debug_mode
from phonos_app.app import _register_analysis_fallback_routes
from phonos_app.helpers.database import get_db
from phonos_app.helpers.paths import get_safe_project_path, is_path_within, is_path_within_any
from phonos_app.helpers.security import validate_csrf_token
from production.plan_service import get_plan_definition
from production.config import Config
from production.database import init_db
from production.encryption_util import generate_key_pair
from production.model_overrides import upsert_model_override

class ResolveDebugModeTestCase(unittest.TestCase):
    def test_defaults_to_env_false(self):
        self.assertFalse(resolve_debug_mode([], {}))

    def test_respects_env_true(self):
        self.assertTrue(resolve_debug_mode([], {'FLASK_DEBUG': 'true'}))

    def test_debug_flag_overrides_env(self):
        self.assertTrue(resolve_debug_mode(['--debug'], {'FLASK_DEBUG': 'false'}))

    def test_no_debug_flag_overrides_env(self):
        self.assertFalse(resolve_debug_mode(['--no-debug'], {'FLASK_DEBUG': 'true'}))


class MainAppTestCase(unittest.TestCase):
    def setUp(self):
        """Set up a test client and initialize the database."""
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        app.config['MAIL_DEFAULT_SENDER'] = 'noreply@testing.com'
        self.app = app.test_client()
        with app.app_context():
            init_db()

    def tearDown(self):
        """Tear down the database."""
        os.close(self.db_fd)
        os.unlink(app.config['DATABASE_FILE'])

    def test_index_page(self):
        """Anonymous visitors should receive the SEO-optimized landing page."""
        response = self.app.get('/')
        self.assertEqual(response.status_code, 200)
        # Verify SEO improvements
        self.assertIn(b'Phonos:', response.data)
        self.assertIn(b'AI-Powered Molecular Discovery', response.data)
        self.assertNotIn(b'content="6;url=/login"', response.data)

        login_page = self.app.get('/login')
        self.assertEqual(login_page.status_code, 200)
        self.assertIn(b'Sign In', login_page.data)

    def test_deep_analysis_endpoint_is_registered(self):
        """The deep analysis API should return an API error, not a 404."""
        response = self.app.post(
            '/api/analysis/deep',
            data=json.dumps({}),
            content_type='application/json',
        )
        self.assertNotEqual(response.status_code, 404)
        self.assertIn(response.status_code, (400, 401, 503))

    def test_analysis_fallback_routes_return_json_error(self):
        """Fallback analysis routes should return a JSON 503 instead of a 404."""
        fallback_app = Flask(__name__)
        _register_analysis_fallback_routes(fallback_app, 'missing optional dependency')
        client = fallback_app.test_client()

        response = client.post(
            '/api/analysis/deep',
            data=json.dumps({'pdb_text': 'ATOM' * 20}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {
            'error': 'Structure analysis is currently unavailable on this server',
            'detail': 'missing optional dependency',
        })

    def test_security_headers_are_applied(self):
        response = self.app.get('/login')

        # X-Frame-Options is intentionally absent: the Notebook IDE embeds
        # interactive Wolfram Cloud notebooks via iframe, and X-Frame-Options
        # has no per-origin whitelist. The CSP `frame-src` directive is the
        # modern, origin-scoped equivalent and covers the same threat model.
        self.assertIsNone(response.headers.get('X-Frame-Options'))
        self.assertEqual(response.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(response.headers.get('Referrer-Policy'), 'strict-origin-when-cross-origin')
        self.assertEqual(response.headers.get('X-Permitted-Cross-Domain-Policies'), 'none')
        self.assertIn('Content-Security-Policy-Report-Only', response.headers)
        # The CSP must allow the Wolfram Cloud host for embedded notebooks.
        csp = response.headers.get('Content-Security-Policy-Report-Only', '')
        self.assertIn('frame-src', csp)
        self.assertIn('wolframcloud.com', csp)

    def test_api_cors_only_allows_trusted_origins(self):
        allowed_origin = 'https://phonos.fyreflysystems.com'
        allowed_response = self.app.post(
            '/api/analysis/deep',
            data=json.dumps({}),
            content_type='application/json',
            headers={'Origin': allowed_origin},
        )

        self.assertEqual(allowed_response.headers.get('Access-Control-Allow-Origin'), allowed_origin)
        self.assertIn('Origin', allowed_response.headers.get('Vary', ''))

        denied_response = self.app.post(
            '/api/analysis/deep',
            data=json.dumps({}),
            content_type='application/json',
            headers={'Origin': 'https://evil.example'},
        )

        self.assertNotIn('Access-Control-Allow-Origin', denied_response.headers)
        self.assertIn('Origin', denied_response.headers.get('Vary', ''))

    def test_csrf_token_validation_requires_exact_match(self):
        with app.test_request_context('/'):
            session['csrf_token'] = 'known-token'

            self.assertTrue(validate_csrf_token('known-token'))
            self.assertFalse(validate_csrf_token('known-token '))
            self.assertFalse(validate_csrf_token(''))

    def test_path_boundary_check_rejects_sibling_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            allowed_root = os.path.join(temp_dir, 'project')
            sibling_root = os.path.join(temp_dir, 'project_evil')
            os.makedirs(allowed_root)
            os.makedirs(sibling_root)

            allowed_file = os.path.join(allowed_root, 'output.pdb')
            sibling_file = os.path.join(sibling_root, 'secret.txt')

            self.assertTrue(is_path_within(allowed_file, allowed_root))
            self.assertTrue(is_path_within_any(allowed_file, [allowed_root]))
            self.assertFalse(is_path_within(sibling_file, allowed_root))
            self.assertFalse(is_path_within_any(sibling_file, [allowed_root]))

    @patch('phonos_app.routes.pages.send_email')
    @patch('phonos_app.routes.pages.secrets.token_urlsafe')
    def test_user_registration_and_verification(self, mock_token, mock_send_email):
        """Test user registration and email verification."""
        mock_token.return_value = 'test-verification-token'
        mock_send_email.return_value = True

        # 1. Register user
        response = self.app.post('/register', data=dict(
            username='testuser',
            email='test@example.com',
            legal_name='Test User',
            password='password',
            confirm_password='password',
            accept_tos='on'
        ), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"We've sent a verification email to your inbox.", response.data)

        # Verify email was called
        mock_send_email.assert_called_once()
        self.assertIn('test-verification-token', mock_send_email.call_args[0][2])

        # 2. Verify email
        response = self.app.get('/verify_email/test-verification-token', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Email verified successfully! You can now log in.', response.data)

        with app.app_context():
            db = get_db()
            user_credential = db.execute('SELECT * FROM user_credentials WHERE email = ?', ('test@example.com',)).fetchone()
            self.assertIsNotNone(user_credential)
            self.assertEqual(user_credential['email_verified'], 1)
            user = db.execute('SELECT * FROM users WHERE id = ?', (user_credential['user_id'],)).fetchone()
            self.assertEqual(user['legal_name'], 'Test User')

    def test_registration_with_missing_legal_name(self):
        """Test registration with a missing legal name."""
        response = self.app.post('/register', data=dict(
            username='testuser',
            email='test@example.com',
            password='password',
            confirm_password='password',
            accept_tos='on'
            # legal_name is missing
        ), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Please fill in all required fields.', response.data)

    @patch('phonos_app.routes.pages.send_email')
    def test_registration_with_existing_email(self, mock_send_email):
        """Test registration with an existing email."""
        mock_send_email.return_value = True
        # Register a user and verify them so they are in the DB
        with patch('phonos_app.routes.pages.secrets.token_urlsafe') as mock_token:
            mock_token.return_value = 'token1'
            self.app.post('/register', data=dict(
                username='testuser',
                email='test@example.com',
                legal_name='Test User',
                password='password',
                confirm_password='password',
                accept_tos='on'
            ), follow_redirects=True)
            self.app.get('/verify_email/token1')

        # Try to register another user with the same email
        response = self.app.post('/register', data=dict(
            username='testuser2',
            email='test@example.com',
            legal_name='Test User 2',
            password='password',
            confirm_password='password',
            accept_tos='on'
        ), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'This email is already registered.', response.data)

    def test_registration_with_mismatched_passwords(self):
        """Test registration with mismatched passwords."""
        response = self.app.post('/register', data=dict(
            username='testuser',
            email='test@example.com',
            legal_name='Test User',
            password='password',
            confirm_password='wrong_password',
            accept_tos='on'
        ), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Passwords do not match.', response.data)

    @patch('phonos_app.routes.pages.send_email')
    def test_login_with_text_stored_password_hash(self, mock_send_email):
        """Ensure login succeeds even if the stored bcrypt hash is persisted as TEXT."""
        mock_send_email.return_value = True

        with patch('phonos_app.routes.pages.secrets.token_urlsafe') as mock_token:
            mock_token.return_value = 'text-hash-token'
            self.app.post(
                '/register',
                data=dict(
                    username='textuser',
                    email='text@example.com',
                    legal_name='Text Hash',
                    password='password',
                    confirm_password='password',
                    accept_tos='on'
                ),
                follow_redirects=True,
            )

        self.app.get('/verify_email/text-hash-token', follow_redirects=True)

        with app.app_context():
            db = get_db()
            credential = db.execute(
                'SELECT id, password FROM user_credentials WHERE email = ?',
                ('text@example.com',),
            ).fetchone()
            stored_password = credential['password']
            if isinstance(stored_password, memoryview):
                stored_password = stored_password.tobytes()
            if isinstance(stored_password, (bytes, bytearray)):
                stored_password = stored_password.decode('utf-8')

            db.execute(
                'UPDATE user_credentials SET password = ? WHERE id = ?',
                (stored_password, credential['id']),
            )
            db.commit()

        login_response = self.app.post(
            '/login',
            data=dict(username='textuser', password='password'),
        )
        self.assertEqual(login_response.status_code, 302)
        self.assertEqual(login_response.headers['Location'], '/verify_2fa')

        with self.app.session_transaction() as sess:
            two_fa_code = sess.get('2fa_code')
            self.assertIsNotNone(two_fa_code)

        verify_response = self.app.post(
            '/verify_2fa',
            data={'2fa_code': two_fa_code},
            follow_redirects=True,
        )
        self.assertEqual(verify_response.status_code, 200)

        with self.app.session_transaction() as sess:
            self.assertEqual(sess.get('username'), 'textuser')
            self.assertIn('user_id', sess)

class CollaborationTestCase(MainAppTestCase):

    def _register_and_verify_user(self, username, email, legal_name, password='password'):
        with patch('phonos_app.routes.pages.send_email') as mock_send_email, \
             patch('phonos_app.routes.pages.secrets.token_urlsafe') as mock_token:
            mock_send_email.return_value = True
            mock_token.return_value = f"token-for-{username}"

            self.app.post('/register', data=dict(
                username=username,
                email=email,
                legal_name=legal_name,
                password=password,
                confirm_password=password,
                accept_tos='on'
            ), follow_redirects=True)

            self.app.get(f'/verify_email/token-for-{username}')

        # Give the newly verified user enough credits for job submissions.
        with app.app_context():
            db = get_db()
            db.execute('UPDATE users SET credits = ? WHERE username = ?', (500, username))
            db.commit()

    def _login(self, username, password='password'):
        with app.app_context():
            user_credential = get_db().execute('SELECT uc.user_id, uc.email FROM user_credentials uc JOIN users u ON uc.user_id = u.id WHERE u.username = ?', (username,)).fetchone()

        with patch('phonos_app.routes.pages.send_email') as mock_send_email:
            mock_send_email.return_value = True
            self.app.post('/login', data=dict(username=username, password=password))
            # In a real test, you'd get the 2FA code from the email mock. For simplicity, we grab it from the session.
            with self.app.session_transaction() as sess:
                two_fa_code = sess.get('2fa_code')

            return self.app.post('/verify_2fa', data={'2fa_code': two_fa_code}, follow_redirects=True)


    def _create_project(self, name, description=''):
        return self.app.post('/api/projects', json=dict(name=name, description=description))

    def _create_project_with_access_record(self, user_id, name, description=''):
        with app.app_context():
            db = get_db()
            next_team_id = db.execute('SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM teams').fetchone()['next_id']
            next_project_id = db.execute('SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM projects').fetchone()['next_id']

            db.execute(
                '''
                INSERT INTO teams (id, name, slug, description, creator_id)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (next_team_id, f'{name} Team', f'team-{next_team_id}', description or name, user_id),
            )
            db.execute(
                '''
                INSERT INTO team_members (team_id, user_id, role)
                VALUES (?, ?, ?)
                ''',
                (next_team_id, user_id, 'owner'),
            )

            project_columns = {
                row['name']
                for row in db.execute('PRAGMA table_info(projects)').fetchall()
            }
            insert_columns = ['id', 'team_id', 'name', 'description', 'is_private', 'display_anonymous']
            insert_values = [next_project_id, next_team_id, name, description, 1, 0]
            if 'organization_id' in project_columns:
                insert_columns.insert(2, 'organization_id')
                insert_values.insert(2, next_team_id)

            placeholders = ', '.join('?' for _ in insert_columns)
            db.execute(
                f"INSERT INTO projects ({', '.join(insert_columns)}) VALUES ({placeholders})",
                tuple(insert_values),
            )
            db.execute(
                '''
                INSERT INTO project_assignments (project_id, user_id, role)
                VALUES (?, ?, ?)
                ''',
                (next_project_id, user_id, 'Project Manager'),
            )
            db.commit()

        return next_project_id

    def _create_organization(self, name, description=''):
        return self.app.post('/api/organizations', json=dict(name=name, description=description))

    def _get_default_model(self):
        return next(model['id'] for model in Config.SUPPORTED_MODELS if model.get('available', True))

    def _create_logged_in_user(self, username='apitester'):
        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO users (user_id, username, legal_name, plan, credits) VALUES (?, ?, ?, ?, ?)',
                (f'uid-{username}', username, f'{username} user', 'Standard', 500),
            )
            db.commit()
            user_id = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()['id']

        with self.app.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['username'] = username
            sess['email'] = f'{username}@example.com'

        return user_id

    def _create_direct_user_session(self, username='apitester'):
        with app.app_context():
            db = get_db()
            next_user_id = db.execute('SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM users').fetchone()['next_id']
            db.execute(
                '''
                INSERT INTO users (id, user_id, username, legal_name, plan, credits, is_admin)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (next_user_id, f'uid-{username}', username, f'{username} user', 'Standard', 500, 0),
            )
            db.commit()

        with self.app.session_transaction() as sess:
            sess['user_id'] = next_user_id
            sess['username'] = username
            sess['email'] = f'{username}@example.com'

        return next_user_id

    def _create_project_file(self, project_id, filename, content, creator_username=None, creator_id=None):
        with app.app_context():
            db = get_db()
            team_row = db.execute('SELECT team_id FROM projects WHERE id = ?', (project_id,)).fetchone()
            self.assertIsNotNone(team_row)
            if creator_id is None:
                user_row = db.execute('SELECT id FROM users WHERE username = ?', (creator_username,)).fetchone()
                self.assertIsNotNone(user_row)
                creator_id = user_row['id']

            project_path = get_safe_project_path(project_id, team_row['team_id'])
            self.assertIsNotNone(project_path)
            os.makedirs(project_path, exist_ok=True)

            file_path = os.path.join(project_path, filename)
            with open(file_path, 'w', encoding='utf-8') as handle:
                handle.write(content)

            file_id = db.execute('SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM files').fetchone()['next_id']
            db.execute(
                'INSERT INTO files (id, project_id, creator_id, name, path, size) VALUES (?, ?, ?, ?, ?, ?)',
                (file_id, project_id, creator_id, filename, file_path, os.path.getsize(file_path)),
            )
            db.commit()

        return file_id, file_path

    def _create_completed_job_output(self, project_id, job_id, filename, content, creator_username=None, creator_id=None, model='esmfold'):
        with app.app_context():
            db = get_db()
            team_row = db.execute('SELECT team_id FROM projects WHERE id = ?', (project_id,)).fetchone()
            self.assertIsNotNone(team_row)
            if creator_id is None:
                user_row = db.execute('SELECT id FROM users WHERE username = ?', (creator_username,)).fetchone()
                self.assertIsNotNone(user_row)
                creator_id = user_row['id']

            project_path = get_safe_project_path(project_id, team_row['team_id'])
            self.assertIsNotNone(project_path)
            output_dir = os.path.join(project_path, 'jobs', job_id, 'main')
            os.makedirs(output_dir, exist_ok=True)

            output_path = os.path.join(output_dir, filename)
            with open(output_path, 'w', encoding='utf-8') as handle:
                handle.write(content)

            db.execute(
                'INSERT INTO jobs (project_id, creator_id, job_id, model, status, output_file, output_file_size, output_display_name) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    project_id,
                    creator_id,
                    job_id,
                    model,
                    'completed',
                    output_path,
                    os.path.getsize(output_path),
                    filename,
                ),
            )
            db.commit()

        return output_path

    def _create_direct_project(self, name, owner_username=None, owner_user_id=None, description=''):
        with app.app_context():
            db = get_db()
            if owner_user_id is None:
                user_row = db.execute('SELECT id FROM users WHERE username = ?', (owner_username,)).fetchone()
                self.assertIsNotNone(user_row)
                owner_user_id = user_row['id']

            team_id = db.execute('SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM teams').fetchone()['next_id']
            project_id = db.execute('SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM projects').fetchone()['next_id']
            owner_slug = owner_username or f'user-{owner_user_id}'
            team_slug = f'{owner_slug}-team-{team_id}'

            db.execute(
                'INSERT INTO teams (id, name, slug, description, creator_id) VALUES (?, ?, ?, ?, ?)',
                (team_id, f'{name} Team', team_slug, description, owner_user_id),
            )
            db.execute(
                'INSERT INTO team_members (team_id, user_id, role) VALUES (?, ?, ?)',
                (team_id, owner_user_id, 'owner'),
            )

            project_columns = {
                row['column_name']
                for row in db.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'"
                ).fetchall()
            }
            insert_columns = ['id', 'team_id', 'name', 'description', 'is_private', 'display_anonymous']
            insert_values = [project_id, team_id, name, description, 1, 0]
            if 'organization_id' in project_columns:
                insert_columns.insert(2, 'organization_id')
                insert_values.insert(2, team_id)

            placeholders = ', '.join('?' for _ in insert_columns)
            db.execute(
                f"INSERT INTO projects ({', '.join(insert_columns)}) VALUES ({placeholders})",
                tuple(insert_values),
            )
            db.execute(
                'INSERT INTO project_assignments (project_id, user_id, role) VALUES (?, ?, ?)',
                (project_id, owner_user_id, 'Project Manager'),
            )
            db.commit()

        return project_id

    def test_database_import_endpoint_handles_validation_without_internal_import_error(self):
        self._create_logged_in_user('dbimporter')

        response = self.app.post(
            '/api/databases/pdb/import',
            json={'project_id': 1},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'error')
        self.assertIn('project_id and identifier are required', payload.get('message', ''))

    def test_test_user_custom_model_worker_returns_dummy_pdb(self):
        with app.app_context():
            db = get_db()
            test_user_id = 1
            db.execute(
                '''
                INSERT INTO users (id, user_id, username, legal_name, plan, credits, is_admin)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (test_user_id, 'uid-test', 'test', 'Test User', 'Standard', 1000, 0),
            )
            db.commit()

        with self.app.session_transaction() as sess:
            sess['user_id'] = test_user_id
            sess['username'] = 'test'
            sess['email'] = 'test@example.com'
        with app.app_context():
            db = get_db()
            team_id = 1
            project_id = 1

            db.execute(
                '''
                INSERT INTO teams (id, name, slug, description, creator_id)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (team_id, 'Test Team', 'test-team', 'Test owned team', test_user_id),
            )
            db.execute(
                '''
                INSERT INTO team_members (team_id, user_id, role)
                VALUES (?, ?, ?)
                ''',
                (team_id, test_user_id, 'owner'),
            )

            project_columns = {
                row['column_name']
                for row in db.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'"
                ).fetchall()
            }
            insert_columns = ['id', 'team_id', 'name', 'description', 'is_private', 'display_anonymous']
            insert_values = [project_id, team_id, 'Test Custom Model Project', 'Project for hello world worker test', 1, 0]
            if 'organization_id' in project_columns:
                insert_columns.insert(2, 'organization_id')
                insert_values.insert(2, team_id)

            placeholders = ', '.join('?' for _ in insert_columns)
            db.execute(
                f"INSERT INTO projects ({', '.join(insert_columns)}) VALUES ({placeholders})",
                tuple(insert_values),
            )
            db.execute(
                '''
                INSERT INTO project_assignments (project_id, user_id, role)
                VALUES (?, ?, ?)
                ''',
                (project_id, test_user_id, 'Project Manager'),
            )
            db.commit()

        create_model_response = self.app.post(
            '/api/user-models',
            json={
                'model_name': 'Hello World Dummy PDB Model',
                'model_description': 'Returns a deterministic hello world dummy pdb payload',
                'project_id': project_id,
                'output_format': 'pdb',
                'input_schema': [
                    {'name': 'sequence', 'label': 'Sequence', 'type': 'text', 'required': True}
                ],
            },
        )
        self.assertEqual(create_model_response.status_code, 201, create_model_response.get_json())
        model_db_id = create_model_response.get_json()['model']['id']

        create_key_response = self.app.post(
            f'/api/user-models/{model_db_id}/api-keys',
            json={
                'name': 'Test Worker Key',
                'encryption_enabled': False,
            },
        )
        self.assertEqual(create_key_response.status_code, 201, create_key_response.get_json())
        worker_api_key = create_key_response.get_json()['api_key']['full_key']

        with app.app_context():
            db = get_db()
            job_db_id = 101
            job_id = 'test-custom-model-job-001'
            queued_payload = json.dumps({
                'job_id': job_id,
                'model_id': model_db_id,
                'input_data': {'sequence': 'ACDEFGHIKLMNPQRSTVWY'},
                'custom_params': {},
            })
            db.execute(
                '''
                INSERT INTO jobs (id, job_id, project_id, creator_id, name, model, status, job_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''',
                (job_db_id, job_id, project_id, test_user_id, 'test-dummy-pdb-job', f'user-model-{model_db_id}', 'queued', queued_payload),
            )
            db.execute(
                '''
                INSERT INTO external_job_queue (job_id, user_model_id, status, timeout_seconds, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''',
                (job_db_id, model_db_id, 'pending', 3600),
            )
            db.commit()

        poll_response = self.app.post(
            '/api/bridge/v1/jobs/poll',
            headers={'Authorization': f'Bearer {worker_api_key}'},
            json={'max_jobs': 1, 'worker_id': 'test-custom-worker'},
        )
        self.assertEqual(poll_response.status_code, 200, poll_response.get_json())
        poll_payload = poll_response.get_json()
        self.assertEqual(poll_payload['status'], 'success')
        self.assertEqual(len(poll_payload['jobs']), 1)

        assigned_job = poll_payload['jobs'][0]
        self.assertFalse(assigned_job.get('encrypted', False))
        self.assertEqual(assigned_job['job_id'], job_id)
        self.assertEqual(assigned_job['model_id'], model_db_id)
        self.assertEqual(assigned_job['output_format'], 'pdb')

        dummy_pdb = (
            "HEADER    DUMMY STRUCTURE\n"
            "ATOM      1  N   ALA A   1       1.000   2.000   3.000  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       2.200   2.100   3.300  1.00  0.00           C\n"
            "TER\n"
            "END\n"
        )

        submit_result_response = self.app.post(
            f'/api/bridge/v1/jobs/{job_id}/result',
            headers={'Authorization': f'Bearer {worker_api_key}'},
            json={
                'status': 'completed',
                'result': {
                    'structure_pdb': dummy_pdb,
                    'confidence_scores': {'plddt': [98.7, 97.4]},
                },
                'metrics': {'runtime_seconds': 0.12},
            },
        )
        self.assertEqual(submit_result_response.status_code, 200, submit_result_response.get_json())

        fetch_result_response = self.app.get(f'/api/user-model-jobs/{job_id}/result')
        self.assertEqual(fetch_result_response.status_code, 200, fetch_result_response.get_json())
        job_payload = fetch_result_response.get_json()['job']

        self.assertEqual(job_payload['job_status'], 'completed')
        self.assertEqual(job_payload['queue_status'], 'completed')
        self.assertEqual(job_payload['user_model']['id'], model_db_id)
        self.assertEqual(job_payload['result']['structure_pdb'], dummy_pdb)

    def test_notebook_create_normalizes_payload(self):
        self._register_and_verify_user('nbuser', 'nbuser@test.com', 'Notebook User')
        self._login('nbuser')

        project_response = self._create_project('Notebook Project')
        self.assertEqual(project_response.status_code, 201)
        project_id = project_response.get_json()['id']

        response = self.app.post('/api/notebooks', json={
            'name': '   ' + ('X' * 200),
            'description': '  demo desc  ',
            'project_id': project_id,
            'cells': [
                {'type': 'code', 'source': 'print(1)', 'outputs': ['ok']},
                {'type': 'unexpected', 'source': 12345},
                'bad-cell'
            ],
            'metadata': []
        })
        self.assertEqual(response.status_code, 201)
        notebook_id = response.get_json()['id']

        notebook_response = self.app.get(f'/api/notebooks/{notebook_id}')
        self.assertEqual(notebook_response.status_code, 200)
        notebook = notebook_response.get_json()['notebook']

        self.assertEqual(len(notebook['name']), 120)
        self.assertEqual(notebook['description'], 'demo desc')
        self.assertEqual(len(notebook['cells']), 2)
        self.assertEqual(notebook['cells'][1]['type'], 'code')
        self.assertEqual(notebook['cells'][1]['source'], '12345')

    def test_notebook_list_supports_project_filter(self):
        self._register_and_verify_user('nbfilter', 'nbfilter@test.com', 'Notebook Filter')
        self._login('nbfilter')

        p1 = self._create_project('Project One').get_json()['id']
        p2 = self._create_project('Project Two').get_json()['id']

        self.app.post('/api/notebooks', json={'name': 'Notebook One', 'project_id': p1})
        self.app.post('/api/notebooks', json={'name': 'Notebook Two', 'project_id': p2})

        response = self.app.get(f'/api/notebooks?project_id={p1}&limit=50')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload['count'], 1)
        self.assertEqual(payload['notebooks'][0]['project_id'], p1)

    def test_project_job_submission_accepts_post(self):
        self._register_and_verify_user('submitter', 'submitter@test.com', 'Submit User')
        self._login('submitter')

        project_response = self._create_project('Job Project')
        self.assertEqual(project_response.status_code, 201)
        project_id = project_response.get_json()['id']

        model_id = self._get_default_model()
        job_response = self.app.post(
            f'/api/projects/{project_id}/jobs',
            json={'model': model_id, 'sequence': 'ACDEFGHIKL'},
        )

        self.assertEqual(job_response.status_code, 202)
        payload = job_response.get_json()
        self.assertIsNotNone(payload)
        self.assertEqual(payload['project_id'], project_id)
        self.assertEqual(payload['model'], model_id)

    @patch('phonos_app.routes.api_jobs.get_redis_connection')
    @patch('phonos_app.routes.api_jobs.determine_job_target_queue', return_value=None)
    @patch('phonos_app.routes.api_jobs.get_model_state', return_value=(True, True))
    @patch('phonos_app.routes.api_jobs.ensure_monthly_credit_reset')
    def test_project_job_submission_accepts_dna_fasta_file_for_boltz2(
        self,
        _mock_credit_reset,
        _mock_model_state,
        _mock_target_queue,
        mock_get_redis,
    ):
        if type(self) is not CollaborationTestCase:
            self.skipTest('Runs only on CollaborationTestCase')

        user_id = self._create_direct_user_session('dnasubmitter')
        project_id = self._create_project_with_access_record(user_id, 'DNA Job Project')

        mock_redis = Mock()
        mock_get_redis.return_value = mock_redis

        response = self.app.post(
            f'/api/projects/{project_id}/jobs',
            json={
                'model': 'type-9',
                'sequence': '>DNA_Input\nACGTACGT\n',
                'custom_params': {
                    'molecule_type': 'dna',
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['model'], 'type-9')
        self.assertTrue(mock_redis.lpush.called)

        queue_name, queued_payload_raw = mock_redis.lpush.call_args[0]
        self.assertEqual(queue_name, 'queue:new_jobs')
        queued_payload = json.loads(queued_payload_raw)
        self.assertEqual(queued_payload['params']['sequence'], 'ACGTACGT')
        self.assertEqual(queued_payload['params']['molecule_type'], 'dna')

        with app.app_context():
            db = get_db()
            stored_job = db.execute(
                'SELECT sequence FROM jobs WHERE job_id = ?',
                (payload['job_id'],),
            ).fetchone()

        self.assertIsNotNone(stored_job)
        self.assertEqual(stored_job['sequence'], 'ACGTACGT')

    @patch('phonos_app.routes.api_jobs.add_job_to_batch')
    @patch('phonos_app.routes.api_jobs.create_batch', return_value={'id': 1, 'batch_id': 'batch-1'})
    @patch('phonos_app.routes.api_jobs.get_redis_connection')
    @patch('phonos_app.routes.api_jobs.determine_job_target_queue', return_value=None)
    @patch('phonos_app.routes.api_jobs.get_model_state', return_value=(True, True))
    @patch('phonos_app.routes.api_jobs.ensure_monthly_credit_reset')
    def test_batch_job_submission_preserves_dna_molecule_type(
        self,
        _mock_credit_reset,
        _mock_model_state,
        _mock_target_queue,
        mock_get_redis,
        _mock_create_batch,
        _mock_add_job_to_batch,
    ):
        if type(self) is not CollaborationTestCase:
            self.skipTest('Runs only on CollaborationTestCase')

        user_id = self._create_direct_user_session('dnabatch')
        project_id = self._create_project_with_access_record(user_id, 'DNA Batch Project')

        mock_redis = Mock()
        mock_get_redis.return_value = mock_redis

        response = self.app.post(
            '/api/batch/jobs',
            json={
                'project_id': project_id,
                'model': 'type-9',
                'items': [
                    {'name': 'DNA 1', 'sequence': 'ACGTACGT'},
                    {'name': 'DNA 2', 'sequence': 'TGCATGCA'},
                ],
                'config': {
                    'custom_params': {
                        'molecule_type': 'dna',
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['jobs_count'], 2)
        self.assertEqual(mock_redis.lpush.call_count, 2)

        for call in mock_redis.lpush.call_args_list:
            queue_name, queued_payload_raw = call[0]
            self.assertEqual(queue_name, 'queue:new_jobs')
            queued_payload = json.loads(queued_payload_raw)
            self.assertEqual(queued_payload['params']['molecule_type'], 'dna')

    def test_available_models_hides_offline_entries(self):
        self._register_and_verify_user('modeladmin', 'modeladmin@test.com', 'Model Admin')
        self._login('modeladmin')

        hidden_model = Config.SUPPORTED_MODELS[0]['id']
        with app.app_context():
            upsert_model_override(get_db(), hidden_model, is_active=False, is_visible=False)

        response = self.app.get('/api/available_models')
        self.assertEqual(response.status_code, 200)
        models = response.get_json()
        self.assertIsInstance(models, list)
        model_ids = [model['id'] for model in models]
        self.assertNotIn(hidden_model, model_ids)
        for model in models:
            self.assertTrue(model.get('is_visible', True))

    def test_available_models_match_ui_visibility(self):
        self._create_logged_in_user('ui_models')

        response = self.app.get('/api/available_models')
        self.assertEqual(response.status_code, 200)
        models = response.get_json()
        self.assertIsInstance(models, list)

        models_by_id = {model['id']: model for model in models}
        self.assertNotIn('type-5', models_by_id)
        self.assertNotIn('boltz2-enhanced', models_by_id)

        for model_id in ('alphafold2-re', 'alphafold2-re-multimer'):
            self.assertIn(model_id, models_by_id)
            model_state = models_by_id[model_id]
            self.assertFalse(model_state.get('is_active', True))
            self.assertFalse(model_state.get('available', True))
            self.assertTrue(model_state.get('is_visible', False))

    @patch('production.boltz2_worker.requests.post')
    def test_boltz2_predict_forwards_write_full_pae(self, mock_post):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {'Content-Type': 'application/json'}
        mock_response.json.return_value = {'confidence_scores': [0.91], 'sequence': 'ACDE'}
        mock_post.return_value = mock_response

        from production.boltz2_worker import call_boltz2_api
        call_boltz2_api(
            polymers=[{'id': 'A', 'molecule_type': 'protein', 'sequence': 'ACDEFGHIK'}],
            write_full_pae=True,
        )
        payload = mock_post.call_args.kwargs['json']
        self.assertTrue(payload.get('write_full_pae'))

    @patch('production.evo2_worker.requests.post')
    def test_evo2_generate_forwards_extended_generation_params(self, mock_post):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {'Content-Type': 'application/json'}
        mock_response.json.return_value = {'sequence': 'ATCGATCG'}
        mock_post.return_value = mock_response

        from production.evo2_worker import call_evo2_api
        call_evo2_api(
            sequence='ATCGATCG',
            num_tokens=64,
            enable_logits=True,
            enable_elapsed_ms_per_token=True,
            random_seed=12345,
        )
        payload = mock_post.call_args.kwargs['json']
        self.assertTrue(payload.get('enable_logits'))
        self.assertTrue(payload.get('enable_elapsed_ms_per_token'))
        self.assertEqual(payload.get('random_seed'), 12345)
        self.assertTrue(payload.get('enable_sampled_probs'))

    @patch('production.alphafold2_re_worker.poll_nvcf_status')
    @patch('production.alphafold2_re_worker.requests.post')
    def test_alphafold2_re_api_polls_async_status(self, mock_post, mock_poll):
        sample_pdb = (
            "ATOM      1  N   ALA A   1      11.104  13.207   9.257  1.00 95.00           N\n"
            "END\n"
        )
        mock_response = Mock()
        mock_response.status_code = 202
        mock_response.headers = {'NVCF-REQID': 'task-123', 'Content-Type': 'application/json'}
        mock_response.text = ''
        mock_post.return_value = mock_response
        mock_poll.return_value = {'pdb_content': sample_pdb, 'format': 'pdb'}

        from production.alphafold2_re_worker import call_alphafold2_re_api

        with patch('production.alphafold2_re_worker.NVIDIA_API_KEY', 'test-key'):
            result = call_alphafold2_re_api(sequence='ACDE')

        self.assertEqual(result['format'], 'pdb')
        args, _ = mock_poll.call_args
        self.assertEqual(args[0], 'task-123')
        self.assertEqual(args[1]['Authorization'], 'Bearer test-key')

    @patch('production.alphafold2_re_worker.poll_nvcf_status')
    @patch('production.alphafold2_re_worker.requests.post')
    def test_alphafold2_re_api_polls_504_with_reqid(self, mock_post, mock_poll):
        mock_response = Mock()
        mock_response.status_code = 504
        mock_response.headers = {
            'NVCF-REQID': 'task-504',
            'NVCF-STATUS': 'errored',
            'Content-Type': 'application/json',
        }
        mock_response.text = ''
        mock_post.return_value = mock_response
        mock_poll.return_value = {'pdb_content': 'ATOM      1  N   ALA A   1\nEND\n', 'format': 'pdb'}

        from production.alphafold2_re_worker import call_alphafold2_re_api

        with patch('production.alphafold2_re_worker.NVIDIA_API_KEY', 'test-key'):
            result = call_alphafold2_re_api(sequence='ACDE')

        self.assertEqual(result['format'], 'pdb')
        args, _ = mock_poll.call_args
        self.assertEqual(args[0], 'task-504')

    @patch('production.alphafold2_re_worker.time.sleep')
    @patch('production.alphafold2_re_worker.requests.post')
    def test_alphafold2_re_api_retries_empty_504_without_reqid(self, mock_post, mock_sleep):
        retry_response = Mock()
        retry_response.status_code = 504
        retry_response.headers = {'NVCF-STATUS': 'errored'}
        retry_response.text = ''

        success_response = Mock()
        success_response.status_code = 200
        success_response.headers = {'Content-Type': 'application/json'}
        success_response.json.return_value = {'pdb_content': 'ATOM      1  N   ALA A   1\nEND\n', 'format': 'pdb'}

        mock_post.side_effect = [retry_response, success_response]

        from production.alphafold2_re_worker import call_alphafold2_re_api

        with patch('production.alphafold2_re_worker.NVIDIA_API_KEY', 'test-key'):
            result = call_alphafold2_re_api(sequence='ACDE')

        self.assertEqual(result['format'], 'pdb')
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    @patch('production.alphafold2_re_worker.call_alphafold2_re_api')
    def test_alphafold2_re_worker_accepts_4096_residue_sequences(self, mock_call):
        sample_pdb = (
            "ATOM      1  N   ALA A   1      11.104  13.207   9.257  1.00 95.00           N\n"
            "END\n"
        )
        mock_call.return_value = {'pdbs': [sample_pdb], 'format': 'pdb'}

        from production.alphafold2_re_worker import process_alphafold2_re_job

        sequence = 'A' * 4096
        result = process_alphafold2_re_job(
            {'job_id': 'af2-long', 'params': {'sequence': sequence}, 'output_path': '/tmp/af2-long.pdb'},
            'worker-1',
        )

        self.assertEqual(result['status'], 'success')
        self.assertEqual(mock_call.call_args.kwargs['sequence'], sequence)
        structure_text = base64.b64decode(result['structure_content']).decode('utf-8')
        self.assertIn('ATOM', structure_text)

    @patch('production.alphafold2_re_multimer_worker.call_alphafold2_multimer_api')
    def test_alphafold2_re_multimer_worker_normalizes_chain_payloads(self, mock_call):
        sample_pdb = (
            "ATOM      1  N   ALA A   1      11.104  13.207   9.257  1.00 90.00           N\n"
            "END\n"
        )
        mock_call.return_value = {
            'outputs': [
                {
                    'structures': [
                        {'content': sample_pdb, 'format': 'pdb'},
                    ],
                },
            ],
        }

        from production.alphafold2_re_multimer_worker import process_alphafold2_multimer_job

        result = process_alphafold2_multimer_job(
            {
                'job_id': 'af2-multimer',
                'params': {
                    'chains': [
                        {'sequence': 'ACDE', 'chain_id': 'A'},
                        {'protein': {'sequence': 'FGHI'}, 'chain_id': 'B'},
                    ],
                },
                'output_path': '/tmp/af2-multimer.pdb',
            },
            'worker-1',
        )

        self.assertEqual(result['status'], 'success')
        self.assertEqual(mock_call.call_args.kwargs['sequences'], ['ACDE', 'FGHI'])

    def test_job_submission_blocked_when_model_offline(self):
        self._register_and_verify_user('offline', 'offline@test.com', 'Offline User')
        self._login('offline')

        project_response = self._create_project('Offline Project')
        self.assertEqual(project_response.status_code, 201)
        project_id = project_response.get_json()['id']

        offline_model = Config.SUPPORTED_MODELS[0]['id']
        with app.app_context():
            upsert_model_override(get_db(), offline_model, is_active=False, is_visible=False)

        job_response = self.app.post(
            f'/api/projects/{project_id}/jobs',
            json={'model': offline_model, 'sequence': 'ACDEFG'},
        )

        self.assertEqual(job_response.status_code, 400)
        message = job_response.get_json().get('message', '')
        self.assertIn('offline', message.lower())

    def test_project_job_submission_rejects_viewer(self):
        self._register_and_verify_user('manager', 'manager@test.com', 'Manager User')
        self._register_and_verify_user('viewer', 'viewer@test.com', 'Viewer User')

        self._login('manager')
        project_response = self._create_project('Viewer Project')
        project_id = project_response.get_json()['id']

        with app.app_context():
            db = get_db()
            viewer_id = db.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()['id']
            db.execute(
                'INSERT INTO project_assignments (project_id, user_id, role) VALUES (?, ?, ?)',
                (project_id, viewer_id, 'Project Viewer'),
            )
            db.commit()

        self._login('viewer')
        model_id = self._get_default_model()
        job_response = self.app.post(
            f'/api/projects/{project_id}/jobs',
            json={'model': model_id, 'sequence': 'ACDEFGHIKL'},
        )

        self.assertEqual(job_response.status_code, 403)
        payload = job_response.get_json()
        self.assertIsNotNone(payload)
        self.assertEqual(payload['status'], 'error')
        self.assertIn('permission', payload['message'].lower())

    def test_project_creation_adds_owner(self):
        """Test that creating a project makes the user the owner."""
        self._register_and_verify_user('owner', 'owner@test.com', 'Owner User')
        self._login('owner')

        response = self._create_project('Test Project')
        self.assertEqual(response.status_code, 201)
        project_data = response.get_json()
        project_id = project_data['id']

        with app.app_context():
            db = get_db()
            user = db.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()
            member = db.execute("SELECT * FROM project_members WHERE project_id = ? AND user_id = ?", (project_id, user['id'])).fetchone()
            self.assertIsNotNone(member)
            self.assertEqual(member['role'], 'owner')

    def test_enterprise_plan_is_temporarily_unavailable(self):
        """Enterprise plan should surface a pause notice and reject upgrades."""
        self._register_and_verify_user('planuser', 'planuser@test.com', 'Plan User')
        self._login('planuser')

        catalog_response = self.app.get('/api/account/plan')
        self.assertEqual(catalog_response.status_code, 200)
        catalog_payload = catalog_response.get_json()
        self.assertIsNotNone(catalog_payload)

        enterprise_entry = next((plan for plan in catalog_payload.get('plans', []) if plan.get('name') == 'Enterprise'), None)
        self.assertIsNotNone(enterprise_entry)
        self.assertFalse(enterprise_entry.get('self_service'))
        self.assertTrue(enterprise_entry.get('status_notice'))

        upgrade_response = self.app.post('/api/account/plan', json={'plan': 'Enterprise'})
        self.assertEqual(upgrade_response.status_code, 403)
        upgrade_payload = upgrade_response.get_json()
        self.assertEqual(upgrade_payload.get('status'), 'error')
        self.assertIn('paused', upgrade_payload.get('message', '').lower())

    def test_premium_plan_updates_user_credentials(self):
        """Upgrading to Premium should sync both user and credential plan metadata."""

        self._register_and_verify_user('premiumsync', 'premiumsync@test.com', 'Premium Sync')
        self._login('premiumsync')

        response = self.app.post('/api/account/plan', json={'plan': 'Premium'})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get('plan'), 'Premium')

        with app.app_context():
            db = get_db()
            user_row = db.execute(
                "SELECT id, plan, user_credential_id FROM users WHERE username = ?",
                ('premiumsync',),
            ).fetchone()
            self.assertIsNotNone(user_row)
            self.assertEqual(user_row['plan'], 'Premium')

            credential_plan = db.execute(
                "SELECT plan FROM user_credentials WHERE id = ?",
                (user_row['user_credential_id'],),
            ).fetchone()
            if credential_plan is None:
                credential_plan = db.execute(
                    "SELECT plan FROM user_credentials WHERE user_id = ?",
                    (user_row['id'],),
                ).fetchone()

            self.assertIsNotNone(credential_plan)
            self.assertEqual(credential_plan['plan'], 'Premium')

    def test_organization_creation_and_admin_permissions(self):
        """Ensure organizations can be created and admins can collaborate."""
        self._register_and_verify_user('founder', 'founder@example.com', 'Founder User')
        self._register_and_verify_user('teammate', 'teammate@example.com', 'Team Mate')
        self._register_and_verify_user('outsider', 'outsider@example.com', 'Outside User')
        self._register_and_verify_user('regular_user', 'regular@example.com', 'Regular User')

        with app.app_context():
            db = get_db()
            db.execute("UPDATE users SET plan = 'Enterprise' WHERE username IN ('founder', 'teammate', 'regular_user')")
            db.commit()

        self._login('founder')
        org_response = self._create_organization('Acme Research', 'Internal R&D group')
        self.assertEqual(org_response.status_code, 201)
        organization = org_response.get_json()
        organization_id = organization['id']
        self.assertEqual(organization['role'], 'owner')

        add_admin_resp = self.app.post(
            f'/api/organizations/{organization_id}/members',
            json={'email': 'teammate@example.com', 'role': 'admin'}
        )
        self.assertEqual(add_admin_resp.status_code, 201)
        admin_payload = add_admin_resp.get_json()['member']
        self.assertEqual(admin_payload['role'], 'admin')

        # Founder adds a regular member
        add_member_resp = self.app.post(
            f'/api/organizations/{organization_id}/members',
            json={'email': 'regular@example.com', 'role': 'member'}
        )
        self.assertEqual(add_member_resp.status_code, 201)
        member_payload = add_member_resp.get_json()['member']
        self.assertEqual(member_payload['role'], 'member')


        # Outsider cannot manage organization
        self._login('outsider')
        forbidden_resp = self.app.post(
            f'/api/organizations/{organization_id}/members',
            json={'email': 'outsider@example.com'}
        )
        self.assertEqual(forbidden_resp.status_code, 404)

        # Admin can create organization projects
        self._login('teammate')
        project_resp = self.app.post(
            '/api/projects',
            json={'name': 'Org Project', 'organization_id': organization_id}
        )
        self.assertEqual(project_resp.status_code, 201)
        project_payload = project_resp.get_json()
        self.assertEqual(project_payload['organization_id'], organization_id)

        # Admin can change a member's role, but cannot demote owner
        self._login('teammate')
        member_id = member_payload['user_id']
        change_role_resp = self.app.patch(
            f'/api/organizations/{organization_id}/members/{member_id}',
            json={'role': 'billing'}
        )
        self.assertEqual(change_role_resp.status_code, 200)
        self.assertEqual(change_role_resp.get_json()['member']['role'], 'billing')

        with app.app_context():
            founder_id = get_db().execute("SELECT id FROM users WHERE username = 'founder'").fetchone()['id']

        fail_demote_owner = self.app.patch(
            f'/api/organizations/{organization_id}/members/{founder_id}',
            json={'role': 'member'}
        )
        self.assertEqual(fail_demote_owner.status_code, 400)

    @patch('phonos_app.routes.api.send_email')
    def test_invitation_flow(self, mock_send_email):
        """Test the complete invitation flow: invite, accept, and verify membership."""
        mock_send_email.return_value = True

        # 1. Register Owner and Collaborator
        self._register_and_verify_user('owner2', 'owner2@test.com', 'Owner Two')
        self._register_and_verify_user('collaborator', 'collab@test.com', 'Collab User')

        # 2. Owner logs in and creates a project
        self._login('owner2')
        project_res = self._create_project('Collaboration Project')
        project_id = project_res.get_json()['id']

        # 3. Owner invites Collaborator
        invite_res = self.app.post(f'/api/projects/{project_id}/invitations', json=dict(email='collab@test.com', role='collaborator'))
        self.assertEqual(invite_res.status_code, 200)
        json_data = invite_res.get_json()
        self.assertEqual(json_data['status'], 'success')
        self.assertIn('Invitation sent to collab@test.com', json_data['message'])

        with app.app_context():
            db = get_db()
            collab_user_id = db.execute("SELECT id FROM users WHERE username = 'collaborator'").fetchone()['id']
            notification = db.execute(
                'SELECT title, message, category FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 1',
                (collab_user_id,),
            ).fetchone()
            self.assertIsNotNone(notification)
            self.assertEqual(notification['title'], 'Project invitation')
            self.assertEqual(notification['category'], 'project')
            self.assertIn('Collaboration Project', notification['message'])

        # 4. Get invitation token from database
        with app.app_context():
            token = get_db().execute("SELECT token FROM invitations WHERE invitee_email = 'collab@test.com'").fetchone()['token']

        # 5. Collaborator logs in and accepts the invitation
        self.app.get('/logout') # Log out owner
        self._login('collaborator')
        accept_res = self.app.get(f'/invitations/accept/{token}', follow_redirects=True)
        self.assertEqual(accept_res.status_code, 200)

        # 6. Verify Collaborator is now a member
        with app.app_context():
            db = get_db()
            collab_user = db.execute("SELECT id FROM users WHERE username = 'collaborator'").fetchone()
            member = db.execute("SELECT * FROM project_members WHERE project_id = ? AND user_id = ?", (project_id, collab_user['id'])).fetchone()
            self.assertIsNotNone(member)
            self.assertEqual(member['role'], 'collaborator')

            # Verify invitation status is 'accepted'
            invitation = db.execute("SELECT status, accepted_at FROM invitations WHERE token = ?", (token,)).fetchone()
            self.assertEqual(invitation['status'], 'accepted')
            self.assertIsNotNone(invitation['accepted_at'])

            owner_user = db.execute("SELECT id FROM users WHERE username = 'owner2'").fetchone()['id']
            owner_notification = db.execute(
                'SELECT message FROM notifications WHERE user_id = ? AND title = ? ORDER BY id DESC LIMIT 1',
                (owner_user, 'Invitation accepted'),
            ).fetchone()
            self.assertIsNotNone(owner_notification)
            self.assertIn('accepted the invitation', owner_notification['message'])

    @patch('phonos_app.routes.api.send_email')
    def test_project_assignments_include_pending_invitations(self, mock_send_email):
        mock_send_email.return_value = True

        self._register_and_verify_user('owner_pending', 'owner_pending@test.com', 'Owner Pending')
        self._register_and_verify_user('pending_collab', 'pending@test.com', 'Pending User')

        self._login('owner_pending')
        project_res = self._create_project('Pending Project')
        project_id = project_res.get_json()['id']

        invite_res = self.app.post(
            f'/api/projects/{project_id}/invitations',
            json=dict(email='pending@test.com', role='collaborator'),
        )
        self.assertEqual(invite_res.status_code, 200)

        roster_res = self.app.get(f'/api/projects/{project_id}/assignments')
        self.assertEqual(roster_res.status_code, 200)
        payload = roster_res.get_json()
        self.assertIsInstance(payload, dict)
        self.assertIn('members', payload)
        self.assertIn('pending_invitations', payload)
        self.assertGreaterEqual(len(payload['members']), 1)
        self.assertEqual(len(payload['pending_invitations']), 1)
        invitation = payload['pending_invitations'][0]
        self.assertEqual(invitation['email'], 'pending@test.com')
        self.assertEqual(invitation['status'], 'pending')

    @patch('phonos_app.routes.api.send_email')
    def test_can_resend_and_cancel_project_invitation(self, mock_send_email):
        mock_send_email.return_value = True

        self._register_and_verify_user('owner_cancel', 'owner_cancel@test.com', 'Owner Cancel')
        self._register_and_verify_user('cancel_collab', 'cancel@test.com', 'Cancel User')

        self._login('owner_cancel')
        project_res = self._create_project('Action Project')
        project_id = project_res.get_json()['id']

        invite_res = self.app.post(
            f'/api/projects/{project_id}/invitations',
            json=dict(email='cancel@test.com', role='collaborator'),
        )
        self.assertEqual(invite_res.status_code, 200)

        with app.app_context():
            db = get_db()
            invitation_row = db.execute(
                "SELECT id, token FROM invitations WHERE project_id = ? AND invitee_email = ?",
                (project_id, 'cancel@test.com'),
            ).fetchone()
            invitation_id = invitation_row['id']
            original_token = invitation_row['token']

        resend_res = self.app.post(f'/api/projects/{project_id}/invitations/{invitation_id}/resend')
        self.assertEqual(resend_res.status_code, 200)
        self.assertIn('Invitation resent', resend_res.get_json()['message'])
        self.assertGreaterEqual(mock_send_email.call_count, 2)

        with app.app_context():
            db = get_db()
            refreshed = db.execute(
                'SELECT token, status FROM invitations WHERE id = ?', (invitation_id,),
            ).fetchone()
            self.assertEqual(refreshed['status'], 'pending')
            self.assertNotEqual(refreshed['token'], original_token)

        cancel_res = self.app.delete(f'/api/projects/{project_id}/invitations/{invitation_id}')
        self.assertEqual(cancel_res.status_code, 200)
        self.assertIn('Invitation cancelled', cancel_res.get_json()['message'])

        with app.app_context():
            db = get_db()
            status = db.execute('SELECT status FROM invitations WHERE id = ?', (invitation_id,)).fetchone()['status']
            self.assertEqual(status, 'cancelled')

    @patch('phonos_app.routes.api.send_email')
    def test_role_based_access_control(self, mock_send_email):
        """Test that roles (owner, collaborator, spectator) have correct permissions."""
        mock_send_email.return_value = True

        # 1. Register users
        self._register_and_verify_user('owner3', 'owner3@test.com', 'Owner Three')
        self._register_and_verify_user('collab2', 'collab2@test.com', 'Collab Two')
        self._register_and_verify_user('spectator', 'spectator@test.com', 'Spec Tator')

        # 2. Owner creates project
        self._login('owner3')
        project_res = self._create_project('Access Control Project')
        project_id = project_res.get_json()['id']

        # 3. Owner invites collaborator and spectator
        self.app.post(f'/api/projects/{project_id}/invitations', json=dict(email='collab2@test.com', role='collaborator'))
        self.app.post(f'/api/projects/{project_id}/invitations', json=dict(email='spectator@test.com', role='spectator'))

        with app.app_context():
            collab_token = get_db().execute("SELECT token FROM invitations WHERE invitee_email = 'collab2@test.com'").fetchone()['token']
            spec_token = get_db().execute("SELECT token FROM invitations WHERE invitee_email = 'spectator@test.com'").fetchone()['token']

        # 4. Collaborator accepts
        self.app.get('/logout')
        self._login('collab2')
        self.app.get(f'/invitations/accept/{collab_token}')

        # 5. Spectator accepts
        self.app.get('/logout')
        self._login('spectator')
        self.app.get(f'/invitations/accept/{spec_token}')
        self.app.get('/logout')

        # 6. Test permissions
        # Collaborator CAN edit
        self._login('collab2')
        update_res = self.app.patch(f'/api/projects/{project_id}', json=dict(name='New Name by Collab'))
        self.assertEqual(update_res.status_code, 200)
        self.app.get('/logout')

        # Spectator CANNOT edit
        self._login('spectator')
        update_res_spec = self.app.patch(f'/api/projects/{project_id}', json=dict(name='This Should Fail'))
        self.assertEqual(update_res_spec.status_code, 403)
        self.app.get('/logout')

        # Owner CAN delete
        self._login('owner3')
        delete_res = self.app.delete(f'/api/projects/{project_id}')
        self.assertEqual(delete_res.status_code, 200)

    @patch('phonos_app.routes.api.send_email')
    def test_organization_invite_generates_notification(self, mock_send_email):
        mock_send_email.return_value = True

        self._register_and_verify_user('orgowner', 'orgowner@test.com', 'Org Owner')
        self._register_and_verify_user('orgmember', 'orgmember@test.com', 'Org Member')

        with app.app_context():
            db = get_db()
            db.execute("UPDATE users SET plan = 'Enterprise' WHERE username = 'orgowner'")
            db.commit()

        self._login('orgowner')
        organization = self._create_organization('Notify Org').get_json()
        organization_id = organization['id']

        invite_response = self.app.post(
            f'/api/organizations/{organization_id}/invitations',
            json={'email': 'orgmember@test.com', 'role': 'member'},
        )
        self.assertEqual(invite_response.status_code, 201)
        self.assertEqual(invite_response.get_json()['status'], 'success')

        with app.app_context():
            db = get_db()
            member_id = db.execute("SELECT id FROM users WHERE username = 'orgmember'").fetchone()['id']
            notification = db.execute(
                'SELECT title, message, category FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 1',
                (member_id,),
            ).fetchone()
            self.assertIsNotNone(notification)
            self.assertEqual(notification['title'], 'Organization invitation')
            self.assertEqual(notification['category'], 'organization')
            self.assertIn('Notify Org', notification['message'])

    def test_plan_catalog_endpoint_returns_expected_structure(self):
        """Ensure the consolidated plan catalog API exposes consistent metadata."""
        self._register_and_verify_user('planviewer', 'planviewer@example.com', 'Plan Viewer')
        self._login('planviewer')

        response = self.app.get('/api/account/plan')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['plan'], 'Standard')
        self.assertIn('plans', payload)
        self.assertGreaterEqual(len(payload['plans']), 3)

        standard_entry = next((plan for plan in payload['plans'] if plan.get('name') == 'Standard'), None)
        self.assertIsNotNone(standard_entry)
        self.assertIn('storage_quota_bytes', standard_entry)
        self.assertIn('storage_quota_human', standard_entry)
        self.assertIn('monthly_credits', standard_entry)
        self.assertTrue(any(item for item in standard_entry.get('highlights', []) if 'storage' in item.lower()))

    def test_plan_update_via_api(self):
        """Users can upgrade and downgrade plans through the new account endpoint."""
        self._register_and_verify_user('planupdate', 'planupdate@example.com', 'Plan Update')
        self._login('planupdate')

        upgrade_resp = self.app.post('/api/account/plan', json={'plan': 'Premium'})
        self.assertEqual(upgrade_resp.status_code, 200)
        upgrade_payload = upgrade_resp.get_json()
        self.assertEqual(upgrade_payload['plan'], 'Premium')
        self.assertEqual(upgrade_payload['credits'], get_plan_definition('Premium').monthly_credits)

        with app.app_context():
            db = get_db()
            row = db.execute("SELECT plan, credits FROM users WHERE username = 'planupdate'").fetchone()
            self.assertEqual(row['plan'], 'Premium')
            self.assertEqual(row['credits'], get_plan_definition('Premium').monthly_credits)

        alias_resp = self.app.post('/api/account/plan', json={'plan': 'Pro'})
        self.assertEqual(alias_resp.status_code, 200)
        self.assertEqual(alias_resp.get_json()['plan'], 'Premium')

        downgrade_resp = self.app.post('/api/account/plan', json={'plan': 'Standard'})
        self.assertEqual(downgrade_resp.status_code, 200)
        downgrade_payload = downgrade_resp.get_json()
        self.assertEqual(downgrade_payload['plan'], 'Standard')
        self.assertIn('plans', downgrade_payload)

        invalid_resp = self.app.post('/api/account/plan', json={'plan': 'Invalid'})
        self.assertEqual(invalid_resp.status_code, 400)
        self.assertEqual(invalid_resp.get_json()['status'], 'error')

    @patch('phonos_app.routes.api.send_email')
    def test_comments_permissions(self, mock_send_email):
        """Test that spectators cannot add comments."""
        mock_send_email.return_value = True
        self._register_and_verify_user('owner4', 'owner4@test.com', 'Owner Four')
        self._register_and_verify_user('spectator2', 'spectator2@test.com', 'Spec Tator II')

        self._login('owner4')
        project_res = self._create_project('Comment Project')
        project_id = project_res.get_json()['id']

        # Owner invites spectator
        self.app.post(f'/api/projects/{project_id}/invitations', json=dict(email='spectator2@test.com', role='spectator'))

        with app.app_context():
            spec_token = get_db().execute("SELECT token FROM invitations WHERE invitee_email = 'spectator2@test.com'").fetchone()['token']

        self.app.get('/logout')
        self._login('spectator2')
        self.app.get(f'/invitations/accept/{spec_token}')

        # Spectator tries to add a comment to the old, now-removed endpoint
        comment_res = self.app.post(f'/api/projects/{project_id}/comments', json=dict(content='This should fail'))
        # The endpoint should not exist anymore, so we expect a 404 Not Found.
        self.assertEqual(comment_res.status_code, 404)

    def test_all_files_view_includes_unassigned_legacy_records(self):
        """Users should see files and job outputs they created even without project assignments."""
        self._register_and_verify_user('creator', 'creator@example.com', 'Creator User')
        self._login('creator')

        project_res = self._create_project('Assigned Project')
        self.assertEqual(project_res.status_code, 201)
        assigned_project_id = project_res.get_json()['id']

        with app.app_context():
            db = get_db()
            user_row = db.execute("SELECT id FROM users WHERE username = 'creator'").fetchone()
            self.assertIsNotNone(user_row)
            user_id = user_row['id']

            # Baseline file tied to an assigned project ensures project_ids is populated
            db.execute(
                'INSERT INTO files (project_id, creator_id, name, path, size) VALUES (?, ?, ?, ?, ?)',
                (assigned_project_id, user_id, 'assigned.txt', '/tmp/assigned.txt', 42),
            )

            org_row = db.execute(
                'SELECT organization_id FROM projects WHERE id = ?',
                (assigned_project_id,),
            ).fetchone()
            legacy_project_id = db.execute(
                'INSERT INTO projects (organization_id, name, description, is_private) VALUES (?, ?, ?, ?)',
                (org_row['organization_id'], 'Legacy Project', '', 0),
            ).lastrowid

            db.execute(
                'INSERT INTO files (project_id, creator_id, name, path, size) VALUES (?, ?, ?, ?, ?)',
                (legacy_project_id, user_id, 'legacy.txt', '/tmp/legacy.txt', 84),
            )

            db.execute(
                'INSERT INTO jobs (project_id, creator_id, job_id, model, status, output_file, output_file_size, output_display_name) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    legacy_project_id,
                    user_id,
                    'legacy-job-123',
                    'esmfold',
                    'completed',
                    '/tmp/legacy-output.pdb',
                    1234,
                    'legacy-output.pdb',
                ),
            )

            db.commit()

        response = self.app.get('/api/all_files_view')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertIsInstance(payload, list)
        names = {entry['name']: entry for entry in payload}
        self.assertIn('legacy.txt', names)
        self.assertEqual(names['legacy.txt']['project_name'], 'Legacy Project')

        output_entry = next((entry for entry in payload if entry.get('job_id') == 'legacy-job-123'), None)
        self.assertIsNotNone(output_entry)
        self.assertEqual(output_entry['name'], 'legacy-output.pdb')
        self.assertEqual(output_entry['project_name'], 'Legacy Project')

    def test_job_output_preview_allows_legacy_paths_under_output_root(self):
        """Legacy output paths under the configured job output root should remain previewable."""
        self._register_and_verify_user('previewer', 'previewer@example.com', 'Preview User')
        self._login('previewer')

        project_res = self._create_project('Legacy Output Preview')
        self.assertEqual(project_res.status_code, 201)
        project_id = project_res.get_json()['id']

        job_id = 'legacy-preview-job'
        legacy_dir = tempfile.mkdtemp(dir=app.config['JOB_OUTPUT_DIR'])
        self.addCleanup(shutil.rmtree, legacy_dir, True)
        output_path = os.path.join(legacy_dir, 'legacy-output.txt')

        with open(output_path, 'w', encoding='utf-8') as handle:
            handle.write('legacy preview content')

        with app.app_context():
            db = get_db()
            user_row = db.execute("SELECT id FROM users WHERE username = 'previewer'").fetchone()
            self.assertIsNotNone(user_row)

            db.execute(
                'INSERT INTO jobs (project_id, creator_id, job_id, model, status, output_file, output_file_size, output_display_name) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    project_id,
                    user_row['id'],
                    job_id,
                    'esmfold',
                    'completed',
                    output_path,
                    os.path.getsize(output_path),
                    'legacy-output.txt',
                ),
            )
            db.commit()

        response = self.app.get(f'/api/jobs/{job_id}/output_content')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['content'], 'legacy preview content')
        self.assertEqual(payload['format'], 'txt')

    def test_job_output_preview_allows_repo_local_job_outputs(self):
        """Preview should allow outputs stored under the app-local job_outputs directory."""
        self._register_and_verify_user('localpreview', 'localpreview@example.com', 'Local Preview User')
        self._login('localpreview')

        project_res = self._create_project('Repo Local Output Preview')
        self.assertEqual(project_res.status_code, 201)
        project_id = project_res.get_json()['id']

        repo_output_root = os.path.join(os.getcwd(), 'job_outputs')
        os.makedirs(repo_output_root, exist_ok=True)

        job_id = 'repo-local-preview-job'
        local_dir = tempfile.mkdtemp(dir=repo_output_root)
        self.addCleanup(shutil.rmtree, local_dir, True)
        output_path = os.path.join(local_dir, 'repo-local-output.txt')

        with open(output_path, 'w', encoding='utf-8') as handle:
            handle.write('repo local preview content')

        with app.app_context():
            db = get_db()
            user_row = db.execute("SELECT id FROM users WHERE username = 'localpreview'").fetchone()
            self.assertIsNotNone(user_row)

            db.execute(
                'INSERT INTO jobs (project_id, creator_id, job_id, model, status, output_file, output_file_size, output_display_name) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    project_id,
                    user_row['id'],
                    job_id,
                    'esmfold',
                    'completed',
                    output_path,
                    os.path.getsize(output_path),
                    'repo-local-output.txt',
                ),
            )
            db.commit()

        response = self.app.get(f'/api/jobs/{job_id}/output_content')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['content'], 'repo local preview content')
        self.assertEqual(payload['format'], 'txt')

    def test_view_output_renders_diffdock_re_combined_ligand_artifact_without_confidence(self):
        """DiffDock-RE view_output should render when the combined SDF artifact has no confidence field."""
        isolated_output_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, isolated_output_root, True)

        old_job_output_dir = app.config.get('JOB_OUTPUT_DIR')
        old_job_output_fallback = app.config.get('JOB_OUTPUT_DIR_FALLBACK')
        app.config['JOB_OUTPUT_DIR'] = isolated_output_root
        app.config['JOB_OUTPUT_DIR_FALLBACK'] = isolated_output_root
        self.addCleanup(app.config.__setitem__, 'JOB_OUTPUT_DIR', old_job_output_dir)
        self.addCleanup(app.config.__setitem__, 'JOB_OUTPUT_DIR_FALLBACK', old_job_output_fallback)

        user_id = 9101
        team_id = 9201
        project_id = 9301

        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO users (id, user_id, username, legal_name, plan, credits) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, 'uid-diffdockreviewer', 'diffdockreviewer', 'DiffDock Reviewer', 'Standard', 500),
            )
            db.commit()

        with self.app.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['username'] = 'diffdockreviewer'
            sess['email'] = 'diffdockreviewer@example.com'

        job_id = 'diffdock-re-view-output-job'
        protein_filename = f'{job_id}_protein.pdb'
        combined_ligand_filename = f'{job_id}_docked_ligand.sdf'
        pose_filename = f'{job_id}_pose_0.sdf'
        output_filename = f'{job_id}_result.json'

        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO teams (id, name, slug, description, creator_id) VALUES (?, ?, ?, ?, ?)',
                (team_id, 'DiffDock RE Test Team', 'diffdock-re-test-team', 'DiffDock RE test workspace', user_id),
            )
            db.execute(
                'INSERT INTO projects (id, team_id, organization_id, name, description, is_private, display_anonymous) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (project_id, team_id, team_id, 'DiffDock RE Output Page', 'Regression fixture', 1, 0),
            )
            db.commit()

            project_path = get_safe_project_path(project_id, team_id)
            self.assertIsNotNone(project_path)

        job_dir = os.path.join(project_path, 'jobs', job_id)
        os.makedirs(job_dir, exist_ok=True)

        protein_path = os.path.join(job_dir, protein_filename)
        combined_ligand_path = os.path.join(job_dir, combined_ligand_filename)
        pose_path = os.path.join(job_dir, pose_filename)
        output_path = os.path.join(job_dir, output_filename)

        protein_pdb = (
            "ATOM      1  N   GLY A   1      11.104  13.207  12.345  1.00 72.00           N\n"
            "ATOM      2  CA  GLY A   1      12.560  13.102  12.123  1.00 71.00           C\n"
            "ATOM      3  C   GLY A   1      13.021  11.678  11.741  1.00 70.00           C\n"
            "ATOM      4  O   GLY A   1      12.290  10.710  11.977  1.00 69.00           O\n"
            "END\n"
        )
        ligand_pose_sdf = (
            "DiffDockPose\n"
            "  RDKit          3D\n"
            "\n"
            "  3  2  0  0  0  0            999 V2000\n"
            "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
            "    1.2094    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n"
            "   -0.6047    1.0469    0.0000 N   0  0  0  0  0  0  0  0  0  0  0  0\n"
            "  1  2  1  0\n"
            "  1  3  1  0\n"
            "M  END\n"
            "$$$$\n"
        )

        with open(protein_path, 'w', encoding='utf-8') as handle:
            handle.write(protein_pdb)
        with open(combined_ligand_path, 'w', encoding='utf-8') as handle:
            handle.write(ligand_pose_sdf + ligand_pose_sdf)
        with open(pose_path, 'w', encoding='utf-8') as handle:
            handle.write(ligand_pose_sdf)
        with open(output_path, 'w', encoding='utf-8') as handle:
            json.dump({'status': 'completed'}, handle)

        policy = json.dumps({
            'model_name': 'DiffDock-RE',
            'artifacts': [
                {
                    'filename': combined_ligand_filename,
                    'name': 'docked_ligand',
                    'display_name': 'Docked Ligand (2 poses)',
                    'type': 'ligand',
                },
                {
                    'filename': pose_filename,
                    'name': 'docked_pose',
                    'display_name': 'Pose 1 (conf: 0.823)',
                    'pose_index': 0,
                    'confidence': 0.823,
                    'type': 'ligand',
                },
                {
                    'filename': protein_filename,
                    'name': 'visualization_protein',
                    'display_name': 'Protein (PDB)',
                    'type': 'protein',
                },
            ],
            'pose_confidence': [0.823],
            'best_pose_index': 0,
            'best_confidence': 0.823,
            'num_poses': 1,
            'docking_style': True,
            'ligand_file_type': 'smiles',
            'input_ligand_smiles': 'CCO',
        })

        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO jobs (project_id, creator_id, job_id, model, status, output_file, output_file_size, output_display_name, policy) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    project_id,
                    user_id,
                    job_id,
                    'diffdock-re',
                    'completed',
                    output_path,
                    os.path.getsize(output_path),
                    output_filename,
                    policy,
                ),
            )
            db.commit()

        response = self.app.get(f'/view_output/{job_id}')
        response_text = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200, response_text)
        self.assertIn('Ligand Poses', response_text)
        self.assertIn('Docked Ligand (2 poses)', response_text)
        self.assertIn('Pose 1 (conf: 0.823)', response_text)
        self.assertNotIn('debug_error', response_text)

        combined_toggle = re.search(
            rf'class="structure-toggle ligand-toggle"\s+data-filename="{re.escape(combined_ligand_filename)}"(?P<attrs>.*?)onchange=',
            response_text,
            re.S,
        )
        pose_toggle = re.search(
            rf'class="structure-toggle ligand-toggle"\s+data-filename="{re.escape(pose_filename)}"(?P<attrs>.*?)onchange=',
            response_text,
            re.S,
        )
        self.assertIsNotNone(combined_toggle)
        self.assertIsNotNone(pose_toggle)
        self.assertNotIn('checked', combined_toggle.group('attrs'))
        self.assertIn('checked', pose_toggle.group('attrs'))

    def test_create_project_datatype_mismatch(self):
        """Test that creating a project with team_id works."""
        self._register_and_verify_user('testuser', 'test@example.com', 'Test User')
        self._login('testuser')

        with app.app_context():
            db = get_db()
            user_id = db.execute("SELECT id FROM users WHERE username = 'testuser'").fetchone()['id']
            team_id = db.execute("INSERT INTO teams (name, owner_id) VALUES ('test_team', ?) RETURNING id", (user_id,)).fetchone()['id']
            db.commit()

        response = self.app.post('/api/projects', json={
            'name': 'Test Project',
            'project_type': 'team',
            'team_id': team_id
        })

        self.assertEqual(response.status_code, 201)


class ArtifactContentFallbackTestCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        app.config['MAIL_DEFAULT_SENDER'] = 'noreply@testing.com'
        self.app = app.test_client()
        with app.app_context():
            init_db()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(app.config['DATABASE_FILE'])

    def test_job_artifact_content_fallback_uses_metadata_when_file_missing(self):
        """Artifact content endpoint should use policy metadata content when disk artifact is missing."""
        user_id = 4501

        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO users (id, user_id, username, legal_name, plan, credits) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, 'uid-artifactfallback', 'artifactfallback', 'Artifact Fallback', 'Standard', 500),
            )
            db.commit()

        with self.app.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['username'] = 'artifactfallback'
            sess['email'] = 'artifactfallback@example.com'

        project_id = 4701
        team_id = 4601
        job_id = 'artifact-fallback-job'
        artifact_filename = f'{job_id}_full.fasta'
        artifact_content = '>artifact-fallback\nATCGATCGATCG\n'

        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO teams (id, name, slug, description, creator_id) VALUES (?, ?, ?, ?, ?)',
                (team_id, 'Artifact Fallback Team', 'artifact-fallback-team', 'Artifact fallback test team', user_id),
            )
            db.execute(
                'INSERT INTO team_members (team_id, user_id, role) VALUES (?, ?, ?)',
                (team_id, user_id, 'owner'),
            )
            db.execute(
                'INSERT INTO projects (id, team_id, organization_id, name, description, is_private, display_anonymous) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (project_id, team_id, team_id, 'Artifact Fallback Project', 'Artifact fallback test project', 1, 0),
            )

            policy = json.dumps({
                'artifacts_info': [
                    {
                        'filename': artifact_filename,
                        'display_name': 'Full Sequence - Input + Output (FASTA)',
                        'content_b64': base64.b64encode(artifact_content.encode('utf-8')).decode('utf-8'),
                        'type': 'sequence',
                    }
                ]
            })

            db.execute(
                'INSERT INTO jobs (project_id, creator_id, job_id, model, status, output_file, output_file_size, output_display_name, policy) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    project_id,
                    user_id,
                    job_id,
                    'evo2',
                    'completed',
                    '/tmp/nonexistent-output.json',
                    0,
                    'result.json',
                    policy,
                ),
            )
            db.commit()

        response = self.app.get(f'/api/jobs/{job_id}/artifacts/{artifact_filename}/content')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['content'], artifact_content)
        self.assertEqual(payload['format'], 'fasta')

    def test_job_artifact_content_reads_from_output_file_directory(self):
        """Artifact content endpoint should resolve artifacts next to output_file when not in project/jobs/<job_id>."""
        user_id = 4502
        team_id = 4602
        project_id = 4702
        job_id = 'artifact-external-dir-job'
        artifact_filename = f'{job_id}_full.fasta'
        artifact_content = '>external-artifact\nMTEYKLVVVGAGGVGKSAL\n'

        isolated_output_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, isolated_output_root, True)

        old_job_output_dir = app.config.get('JOB_OUTPUT_DIR')
        old_job_output_fallback = app.config.get('JOB_OUTPUT_DIR_FALLBACK')
        app.config['JOB_OUTPUT_DIR'] = isolated_output_root
        app.config['JOB_OUTPUT_DIR_FALLBACK'] = isolated_output_root
        self.addCleanup(app.config.__setitem__, 'JOB_OUTPUT_DIR', old_job_output_dir)
        self.addCleanup(app.config.__setitem__, 'JOB_OUTPUT_DIR_FALLBACK', old_job_output_fallback)

        external_job_dir = os.path.join(isolated_output_root, 'external-artifacts', job_id)
        os.makedirs(external_job_dir, exist_ok=True)
        output_path = os.path.join(external_job_dir, f'{job_id}_result.json')
        artifact_path = os.path.join(external_job_dir, artifact_filename)

        with open(output_path, 'w', encoding='utf-8') as out_handle:
            json.dump({'status': 'completed'}, out_handle)
        with open(artifact_path, 'w', encoding='utf-8') as artifact_handle:
            artifact_handle.write(artifact_content)

        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO users (id, user_id, username, legal_name, plan, credits) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, 'uid-artifactexternal', 'artifactexternal', 'Artifact External', 'Standard', 500),
            )
            db.execute(
                'INSERT INTO teams (id, name, slug, description, creator_id) VALUES (?, ?, ?, ?, ?)',
                (team_id, 'Artifact External Team', 'artifact-external-team', 'Artifact external test team', user_id),
            )
            db.execute(
                'INSERT INTO team_members (team_id, user_id, role) VALUES (?, ?, ?)',
                (team_id, user_id, 'owner'),
            )
            db.execute(
                'INSERT INTO projects (id, team_id, organization_id, name, description, is_private, display_anonymous) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (project_id, team_id, team_id, 'Artifact External Project', 'Artifact external test project', 1, 0),
            )

            policy = json.dumps({
                'artifacts_info': [
                    {
                        'filename': artifact_filename,
                        'display_name': 'Full Sequence (FASTA)',
                        'type': 'sequence',
                    }
                ]
            })
            db.execute(
                'INSERT INTO jobs (project_id, creator_id, job_id, model, status, output_file, output_file_size, output_display_name, policy) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    project_id,
                    user_id,
                    job_id,
                    'evo2',
                    'completed',
                    output_path,
                    os.path.getsize(output_path),
                    f'{job_id}_result.json',
                    policy,
                ),
            )
            db.commit()

        with self.app.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['username'] = 'artifactexternal'
            sess['email'] = 'artifactexternal@example.com'

        response = self.app.get(f'/api/jobs/{job_id}/artifacts/{artifact_filename}/content')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['content'], artifact_content)
        self.assertEqual(payload['format'], 'fasta')


class BugReportApiTestCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        app.config['MAIL_DEFAULT_SENDER'] = 'noreply@testing.com'
        self.app = app.test_client()
        with app.app_context():
            init_db()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(app.config['DATABASE_FILE'])

    def _create_logged_in_user(self, username='bugreporter'):
        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO users (user_id, username, legal_name, plan, credits) VALUES (?, ?, ?, ?, ?)',
                (f'uid-{username}', username, f'{username} user', 'Standard', 500),
            )
            db.commit()
            user_id = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()['id']

        with self.app.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['username'] = username
            sess['email'] = f'{username}@example.com'

        return user_id

    @patch('phonos_app.routes.api_tickets.send_email')
    def test_bug_report_endpoint_sends_admin_email(self, mock_send_email):
        mock_send_email.return_value = True
        self._create_logged_in_user('bugreporter')

        payload = {
            'title': 'Job upload freezes on submit',
            'area': 'jobs',
            'severity': 'high',
            'steps_to_reproduce': '1. Open Jobs view\n2. Upload a file\n3. Click submit',
            'expected_behavior': 'The job should be submitted immediately.',
            'actual_behavior': 'The submit button stays in loading state forever.',
            'additional_context': 'Happens on every attempt.',
            'current_view': 'jobs',
            'page_url': 'https://example.com/dashboard#jobs',
            'client_details': {
                'user_agent': 'Mozilla/5.0',
                'platform': 'Linux x86_64',
                'language': 'en-US',
                'viewport': '1920x1080',
                'screen': '1920x1080',
                'timezone': 'UTC',
            },
        }

        response = self.app.post('/api/bug-reports', json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json().get('status'), 'success')

        mock_send_email.assert_called_once()
        self.assertEqual(mock_send_email.call_args[0][0], 'admin@example.org')
        self.assertIn('[HIGH]', mock_send_email.call_args[0][1])
        self.assertIn('Job upload freezes on submit', mock_send_email.call_args[0][1])
        self.assertIn('Happens on every attempt.', mock_send_email.call_args[0][2])
        self.assertFalse(mock_send_email.call_args.kwargs.get('async_', True))

    @patch('phonos_app.routes.api_tickets.send_email')
    def test_bug_report_endpoint_validates_required_fields(self, mock_send_email):
        self._create_logged_in_user('bugreporter2')

        response = self.app.post('/api/bug-reports', json={
            'title': 'Too short',
            'steps_to_reproduce': '',
            'expected_behavior': 'X',
            'actual_behavior': 'Y',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json().get('status'), 'error')
        mock_send_email.assert_not_called()

    @patch('phonos_app.routes.api_tickets.send_email')
    def test_bug_report_ticket_fallback_sends_admin_email(self, mock_send_email):
        mock_send_email.return_value = True
        with app.app_context():
            db = get_db()
            db.execute(
                'INSERT INTO users (id, user_id, username, legal_name, plan, credits) VALUES (?, ?, ?, ?, ?, ?)',
                (101, 'uid-bugreporter3', 'bugreporter3', 'bugreporter3 user', 'Standard', 500),
            )
            db.commit()

        with self.app.session_transaction() as sess:
            sess['user_id'] = 101
            sess['username'] = 'bugreporter3'
            sess['email'] = 'bugreporter3@example.com'

        response = self.app.post('/api/tickets', json={
            'subject': '[Bug] Viewer hangs after loading structure',
            'message': 'Bug report submitted from web app.\nSteps to reproduce:\n1. Open viewer\n2. Load structure',
            'priority': 'High',
            'category': 'Technical Support',
        })

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json().get('status'), 'success')
        mock_send_email.assert_called_once()
        self.assertEqual(mock_send_email.call_args[0][0], 'admin@example.org')
        self.assertIn('[Fallback]', mock_send_email.call_args[0][1])


class NodeRegistrationTestCase(CollaborationTestCase):

    @patch('phonos_app.routes.api.get_redis_connection')
    @patch('phonos_app.helpers.SUPPORTED_MODEL_IDS', new={'esm'})
    def test_home_node_registration_and_approval(self, mock_get_redis):
        """Test that registering a home node publishes the correct approval message to Redis."""
        # 1. Setup: Create users, enterprise plan, organization
        self._register_and_verify_user('nodeowner', 'nodeowner@test.com', 'Node Owner')
        with app.app_context():
            db = get_db()
            db.execute("UPDATE users SET plan = 'Enterprise' WHERE username = 'nodeowner'")
            db.commit()

        self._login('nodeowner')
        org_res = self._create_organization('Node Testing Inc')
        organization_id = org_res.get_json()['id']

        # 2. Mock Redis
        mock_redis_instance = Mock()
        mock_get_redis.return_value = mock_redis_instance

        # 3. Register a new home node via the API
        node_id = 'test-node-123'
        _private_key, public_key = generate_key_pair()

        register_node_res = self.app.post(
            f'/api/organizations/{organization_id}/nodes',
            json={
                'node_id': node_id,
                'display_name': 'Test ESM Node',
                'worker_types': ['esm'],
                'public_key': public_key,
                'is_primary': True,
            }
        )
        self.assertEqual(register_node_res.status_code, 201, f"API call failed with response: {register_node_res.data.decode()}")

        # 4. Assert that Redis.publish was called correctly
        expected_channel = f"pubsub:node_registration_confirmation:{node_id}"
        expected_payload = {
            'status': 'approved',
            'organization_id': organization_id
        }
        mock_redis_instance.publish.assert_called_once()
        call_args = mock_redis_instance.publish.call_args[0]
        self.assertEqual(call_args[0], expected_channel)
        self.assertEqual(json.loads(call_args[1]), expected_payload)


class DeveloperOptionsSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        self.app = app.test_client()
        with app.app_context():
            init_db()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(app.config['DATABASE_FILE'])

    def _create_logged_in_user(self, username='devopts'):
        with app.app_context():
            db = get_db()
            db.execute(
                '''
                INSERT INTO users (id, user_id, username, legal_name, plan, credits)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (1, 'uid-devopts', username, 'Developer Options', 'Standard', 100),
            ).fetchone()
            db.commit()
            user_id = 1

        with self.app.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['username'] = username
            sess['email'] = f'{username}@example.com'

        return user_id

    def test_developer_options_are_encrypted_and_require_user_api_key(self):
        user_id = self._create_logged_in_user()
        api_key = 'pk_devopts_test_key_1234567890'
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        with app.app_context():
            db = get_db()
            db.execute(
                '''
                INSERT INTO api_keys (key_id, key_hash, user_id, name, is_active)
                VALUES (?, ?, ?, ?, 1)
                ''',
                ('devopts-key-id', key_hash, user_id, 'Developer Options Key'),
            )
            db.commit()

        options_payload = {
            'developer_mode': True,
            'show_debug_panels': True,
            'verbose_api_logging': True,
            'allow_experimental_features': False,
            'default_worker_transport': 'encrypted',
            'request_timeout_seconds': 42,
        }

        save_response = self.app.post(
            '/api/user/developer-options',
            json={'api_key': api_key, 'options': options_payload},
        )
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(save_response.get_json().get('status'), 'success')

        bad_reveal = self.app.post(
            '/api/user/developer-options/reveal',
            json={'api_key': 'pk_invalid_key_for_test'},
        )
        self.assertEqual(bad_reveal.status_code, 403)

        reveal_response = self.app.post(
            '/api/user/developer-options/reveal',
            json={'api_key': api_key},
        )
        self.assertEqual(reveal_response.status_code, 200)
        reveal_payload = reveal_response.get_json()
        self.assertEqual(reveal_payload.get('status'), 'success')
        self.assertEqual(reveal_payload['options']['developer_mode'], True)
        self.assertEqual(reveal_payload['options']['show_debug_panels'], True)
        self.assertEqual(reveal_payload['options']['verbose_api_logging'], True)
        self.assertEqual(reveal_payload['options']['default_worker_transport'], 'encrypted')
        self.assertEqual(reveal_payload['options']['request_timeout_seconds'], 42)

        with app.app_context():
            db = get_db()
            prefs_row = db.execute(
                'SELECT developer_options_encrypted FROM user_preferences WHERE user_id = ?',
                (user_id,),
            ).fetchone()

            self.assertIsNotNone(prefs_row)
            encrypted_blob = prefs_row['developer_options_encrypted']
            self.assertIsNotNone(encrypted_blob)
            self.assertNotEqual(encrypted_blob, json.dumps(options_payload, sort_keys=True))

        meta_response = self.app.get('/api/user/developer-options/meta')
        self.assertEqual(meta_response.status_code, 200)
        self.assertEqual(meta_response.get_json().get('has_options'), True)

        clear_response = self.app.post('/api/user/developer-options/clear', json={})
        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(clear_response.get_json().get('status'), 'success')

        cleared_meta = self.app.get('/api/user/developer-options/meta')
        self.assertEqual(cleared_meta.status_code, 200)
        self.assertEqual(cleared_meta.get_json().get('has_options'), False)


class JobRetryTestCase(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        app.config['MAIL_DEFAULT_SENDER'] = 'noreply@testing.com'
        self.app = app.test_client()
        with app.app_context():
            init_db()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(app.config['DATABASE_FILE'])

    def _create_project_with_access(self, user_id, project_id=1, team_id=1):
        with app.app_context():
            db = get_db()
            db.execute(
                '''
                INSERT INTO teams (id, name, slug, description, creator_id)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (team_id, 'Retry Test Team', f'retry-team-{team_id}', 'Retry test team', user_id),
            )
            db.execute(
                '''
                INSERT INTO team_members (team_id, user_id, role)
                VALUES (?, ?, ?)
                ''',
                (team_id, user_id, 'owner'),
            )

            project_columns = {
                row['name']
                for row in db.execute('PRAGMA table_info(projects)').fetchall()
            }
            insert_columns = ['id', 'team_id', 'name', 'description', 'is_private', 'display_anonymous']
            insert_values = [project_id, team_id, 'Retry Test Project', 'Project for retry tests', 1, 0]
            if 'organization_id' in project_columns:
                insert_columns.insert(2, 'organization_id')
                insert_values.insert(2, team_id)

            placeholders = ', '.join('?' for _ in insert_columns)
            db.execute(
                f"INSERT INTO projects ({', '.join(insert_columns)}) VALUES ({placeholders})",
                tuple(insert_values),
            )
            db.execute(
                '''
                INSERT INTO project_assignments (project_id, user_id, role)
                VALUES (?, ?, ?)
                ''',
                (project_id, user_id, 'Project Manager'),
            )
            db.commit()

    @patch('phonos_app.routes.api_jobs.get_redis_connection')
    @patch('phonos_app.routes.api_jobs.determine_job_target_queue', return_value=None)
    @patch('phonos_app.routes.api_jobs.get_model_state', return_value=(True, True))
    @patch('phonos_app.routes.api_jobs.ensure_monthly_credit_reset')
    def test_retry_job_preserves_openfold3_ligand_params_and_cost(
        self,
        _mock_credit_reset,
        _mock_model_state,
        _mock_target_queue,
        mock_get_redis,
    ):
        user_id = 901
        with app.app_context():
            db = get_db()
            db.execute(
                '''
                INSERT INTO users (id, user_id, username, legal_name, plan, credits)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (user_id, 'uid-retrytester', 'retrytester', 'Retry Tester', 'Standard', 500),
            )
            db.commit()

        with self.app.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['username'] = 'retrytester'
            sess['email'] = 'retrytester@example.com'

        self._create_project_with_access(user_id, project_id=42, team_id=7)

        mock_redis = Mock()
        mock_get_redis.return_value = mock_redis

        job_id = 'openfold3-retry-001'
        original_policy = {
            'input_type': 'sequence',
            'node_id': 'openfold3',
            'task': 'protein_structure_prediction',
            'custom_params': {
                'molecule_type': 'protein',
                'chain_id': 'A',
                'output_format': 'pdb',
                'diffusion_samples': 1,
                'ligand_input_type': 'ccd',
                'ligand_ccd': 'FAD',
            },
        }

        with app.app_context():
            db = get_db()
            db.execute(
                '''
                INSERT INTO jobs (project_id, creator_id, job_id, name, model, sequence, status, input_file_id, policy, cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    42,
                    user_id,
                    job_id,
                    'Retry OF3 Ligand Job',
                    'openfold3',
                    'ACDEFGHIK',
                    'failed',
                    None,
                    json.dumps(original_policy),
                    5,
                ),
            )
            db.commit()

        response = self.app.post(f'/api/jobs/{job_id}/retry', json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(mock_redis.lpush.called)

        queue_name, queued_payload_raw = mock_redis.lpush.call_args[0]
        self.assertEqual(queue_name, 'queue:new_jobs')

        queued_payload = json.loads(queued_payload_raw)
        self.assertEqual(queued_payload['model'], 'openfold3')
        self.assertEqual(queued_payload['task'], 'structure_prediction')
        self.assertEqual(queued_payload['params']['sequence'], 'ACDEFGHIK')
        self.assertEqual(queued_payload['params']['ligand_input_type'], 'ccd')
        self.assertEqual(queued_payload['params']['ligand_ccd'], 'FAD')
        self.assertEqual(queued_payload['params']['molecule_type'], 'protein')
        self.assertEqual(queued_payload['params']['chain_id'], 'A')

        with app.app_context():
            db = get_db()
            retried_job = db.execute(
                'SELECT status, policy FROM jobs WHERE job_id = ?',
                (job_id,),
            ).fetchone()
            updated_policy = json.loads(retried_job['policy'])
            remaining_credits = db.execute(
                'SELECT credits FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()['credits']

        self.assertEqual(retried_job['status'], 'submitted')
        self.assertEqual(updated_policy['custom_params']['ligand_ccd'], 'FAD')
        self.assertEqual(updated_policy['custom_params']['ligand_input_type'], 'ccd')
        self.assertEqual(remaining_credits, 495)


if __name__ == '__main__':
    import tempfile
    import json
    unittest.main()


class NodeRegistrationTestCase(CollaborationTestCase):

    @patch('phonos_app.routes.api.get_redis_connection')
    @patch('phonos_app.helpers.SUPPORTED_MODEL_IDS', new={'esm'})
    def test_home_node_registration_and_approval(self, mock_get_redis):
        """Test that registering a home node publishes the correct approval message to Redis."""
        # 1. Setup: Create users, enterprise plan, organization
        self._register_and_verify_user('nodeowner', 'nodeowner@test.com', 'Node Owner')
        with app.app_context():
            db = get_db()
            db.execute("UPDATE users SET plan = 'Enterprise' WHERE username = 'nodeowner'")
            db.commit()

        self._login('nodeowner')
        org_res = self._create_organization('Node Testing Inc')
        organization_id = org_res.get_json()['id']

        # 2. Mock Redis
        mock_redis_instance = Mock()
        mock_get_redis.return_value = mock_redis_instance

        # 3. Register a new home node via the API
        node_id = 'test-node-123'
        _private_key, public_key = generate_key_pair()

        register_node_res = self.app.post(
            f'/api/organizations/{organization_id}/nodes',
            json={
                'node_id': node_id,
                'display_name': 'Test ESM Node',
                'worker_types': ['esm'],
                'public_key': public_key,
                'is_primary': True,
            }
        )
        self.assertEqual(register_node_res.status_code, 201, f"API call failed with response: {register_node_res.data.decode()}")

        # 4. Assert that Redis.publish was called correctly
        expected_channel = f"pubsub:node_registration_confirmation:{node_id}"
        expected_payload = {
            'status': 'approved',
            'organization_id': organization_id
        }
        mock_redis_instance.publish.assert_called_once()
        call_args = mock_redis_instance.publish.call_args[0]
        self.assertEqual(call_args[0], expected_channel)
        self.assertEqual(json.loads(call_args[1]), expected_payload)


if __name__ == '__main__':
    import tempfile
    import json
    unittest.main()
