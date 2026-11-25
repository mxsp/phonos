"""Tests for the per-notebook workspace folder + bulk file actions.

Covers the 2026-07-07 work:
  - Workspace folder named ``<slug>_workspace`` for *new* notebooks
  - Backward compat: a pre-existing ``<notebook_id>/`` folder keeps being used
  - create-directory / list / upload / content / rename / single-delete
  - bulk-delete / bulk-move / bulk-copy / bulk-download (zip)
  - path-traversal rejection

Run with:
    ENCRYPTION_KEY="a-secure-test-key-for-dev-use-32" pytest tests/test_notebook_workspace.py -v
"""
from __future__ import annotations

import io
import os
import sys
import json
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
PRODUCTION_DIR = os.path.join(PROJECT_ROOT, 'production')
if PRODUCTION_DIR not in sys.path:
    sys.path.insert(0, PRODUCTION_DIR)

os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')

patch('production.config.Config.DEV_MODE', False).start()
patch('redis.ConnectionPool').start()
patch('production.database.validate_database_config', return_value=True).start()

from production.main import app  # noqa: E402
from production.database import init_db  # noqa: E402
from phonos_app.helpers.database import get_db  # noqa: E402


class NotebookWorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.ws_root = tempfile.mkdtemp(prefix='nb_ws_')
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        app.config['MAIL_DEFAULT_SENDER'] = 'noreply@testing.com'
        app.config['NOTEBOOK_FILES_DIR'] = self.ws_root
        self.app = app.test_client()
        with app.app_context():
            init_db()
        self._register_and_login('wsuser', 'wsuser@test.com', 'WS User')

    def tearDown(self):
        ws_root = self.ws_root
        db_path = self.db_path
        os.close(self.db_fd)
        for path in (db_path, ws_root):
            if os.path.exists(path):
                try:
                    os.unlink(path) if os.path.isfile(path) else shutil.rmtree(path)
                except OSError:
                    pass

    # -- auth helpers (mirrors test_education.py) -------------------------

    def _register_and_login(self, username, email, legal_name):
        with patch('phonos_app.routes.pages.send_email') as mock_send, \
             patch('phonos_app.routes.pages.secrets.token_urlsafe') as mock_token:
            mock_send.return_value = True
            mock_token.return_value = 'token-' + username
            self.app.post('/register', data=dict(
                username=username, email=email, legal_name=legal_name,
                password='password', confirm_password='password', accept_tos='on',
            ), follow_redirects=True)
            self.app.get('/verify_email/token-' + username)
        with patch('phonos_app.routes.pages.send_email') as mock_send:
            mock_send.return_value = True
            self.app.post('/login', data=dict(username=username, password='password'))
            with self.app.session_transaction() as sess:
                code = sess.get('2fa_code')
            self.app.post('/verify_2fa', data={'2fa_code': code}, follow_redirects=True)

    def _user_id(self):
        with app.app_context():
            row = get_db().execute('SELECT id FROM users WHERE username = ?', ('wsuser',)).fetchone()
            return row['id'] if row else None

    # -- fixtures ---------------------------------------------------------

    def _create_project(self, name='WS Project'):
        r = self.app.post('/api/projects', json={'name': name, 'description': ''})
        return r.get_json()['id']

    def _create_notebook(self, project_id, name='My Lab Notebook'):
        r = self.app.post('/api/notebooks', json={'name': name, 'project_id': project_id})
        return r.get_json()['id'], r.get_json()

    def _setup_notebook(self, name='My Lab Notebook'):
        pid = self._create_project()
        nb_id, _ = self._create_notebook(pid, name)
        return nb_id

    def _ws_path(self, *parts):
        return os.path.join(self.ws_root, *parts)

    # -- tests ------------------------------------------------------------

    def test_slugify_helper(self):
        from phonos_app.routes.api_notebooks import _slugify_notebook_name
        self.assertEqual(_slugify_notebook_name('My Lab Notebook'), 'my_lab_notebook')
        self.assertEqual(_slugify_notebook_name('Ünïcödé 2!'), 'unicode_2')
        self.assertEqual(_slugify_notebook_name(''), 'notebook')
        self.assertEqual(_slugify_notebook_name('   '), 'notebook')

    def test_new_notebook_uses_slug_workspace_folder(self):
        nb_id = self._setup_notebook('My Lab Notebook')
        # Creating a directory materialises the <slug>_workspace folder.
        r = self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'subdir'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(os.path.isdir(self._ws_path('my_lab_notebook_workspace')))
        self.assertTrue(os.path.isdir(self._ws_path('my_lab_notebook_workspace', 'subdir')))
        # Legacy <id>/ folder must NOT have been created for a new notebook.
        self.assertFalse(os.path.exists(self._ws_path(str(nb_id))))

    def test_legacy_id_folder_still_used(self):
        nb_id = self._setup_notebook('Legacy NB')
        # Pre-create the legacy <id>/ folder with a file, simulating an
        # existing notebook whose files live under the old scheme.
        legacy = self._ws_path(str(nb_id))
        os.makedirs(legacy)
        with open(os.path.join(legacy, 'old.txt'), 'w') as f:
            f.write('legacy')
        # Listing must surface the legacy file (workspace resolution fell back).
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        self.assertEqual(r.status_code, 200)
        names = [f['name'] for f in r.get_json()['files']]
        self.assertIn('old.txt', names)
        # The new <slug>_workspace folder must NOT have been created.
        self.assertFalse(os.path.exists(self._ws_path('legacy_nb_workspace')))

    def test_create_directory_and_list(self):
        nb_id = self._setup_notebook()
        r = self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'data/runs'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['created'])
        # Listing root shows "data" dir.
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertIn('data', names)
        # Listing the subfolder shows "runs".
        r = self.app.get(f'/api/notebooks/{nb_id}/files?path=data')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertIn('runs', names)

    def test_create_directory_rejects_traversal(self):
        nb_id = self._setup_notebook()
        r = self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': '../escape'})
        self.assertEqual(r.status_code, 400)

    def test_upload_list_and_content(self):
        nb_id = self._setup_notebook()
        content = b'print("hi")'
        r = self.app.post(
            f'/api/notebooks/{nb_id}/files',
            data={'path': '', 'file': (io.BytesIO(content), 'script.py')},
            content_type='multipart/form-data',
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['name'], 'script.py')

        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertIn('script.py', names)

        r = self.app.get(f'/api/notebooks/{nb_id}/files/content?path=script.py')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['content'], 'print("hi")')
        # Text files return no 'binary' key (only binary/large files do).
        self.assertFalse(r.get_json().get('binary'))

    def test_content_download_streams_file(self):
        nb_id = self._setup_notebook()
        self.app.post(
            f'/api/notebooks/{nb_id}/files',
            data={'path': '', 'file': (io.BytesIO(b'abc'), 't.txt')},
            content_type='multipart/form-data',
        )
        r = self.app.get(f'/api/notebooks/{nb_id}/files/content?path=t.txt&download=1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b'abc')

    def test_rename(self):
        nb_id = self._setup_notebook()
        self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'dir1'})
        r = self.app.post(f'/api/notebooks/{nb_id}/files/rename',
                          json={'path': 'dir1', 'new_name': 'renamed'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['new_path'], 'renamed')
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertIn('renamed', names)
        self.assertNotIn('dir1', names)

    def test_single_delete(self):
        nb_id = self._setup_notebook()
        self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'todelete'})
        r = self.app.delete(f'/api/notebooks/{nb_id}/files?path=todelete')
        self.assertEqual(r.status_code, 200)
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertNotIn('todelete', names)

    def test_bulk_delete(self):
        nb_id = self._setup_notebook()
        for n in ('a.txt', 'b.txt', 'c.txt'):
            self.app.post(f'/api/notebooks/{nb_id}/files',
                          data={'path': '', 'file': (io.BytesIO(b'x'), n)},
                          content_type='multipart/form-data')
        r = self.app.post(f'/api/notebooks/{nb_id}/files/bulk-delete',
                          json={'paths': ['a.txt', 'c.txt']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['deleted_count'], 2)
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertEqual(names, ['b.txt'])

    def test_bulk_move(self):
        nb_id = self._setup_notebook()
        self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'target'})
        for n in ('a.txt', 'b.txt'):
            self.app.post(f'/api/notebooks/{nb_id}/files',
                          data={'path': '', 'file': (io.BytesIO(b'x'), n)},
                          content_type='multipart/form-data')
        r = self.app.post(f'/api/notebooks/{nb_id}/files/bulk-move',
                          json={'paths': ['a.txt', 'b.txt'], 'dest_path': 'target'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['moved_count'], 2)
        # Root no longer has the files
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertEqual(names, ['target'])
        # Target has them
        r = self.app.get(f'/api/notebooks/{nb_id}/files?path=target')
        names = sorted(f['name'] for f in r.get_json()['files'])
        self.assertEqual(names, ['a.txt', 'b.txt'])

    def test_bulk_move_rejects_move_into_self(self):
        nb_id = self._setup_notebook()
        self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'outer'})
        r = self.app.post(f'/api/notebooks/{nb_id}/files/bulk-move',
                          json={'paths': ['outer'], 'dest_path': 'outer'})
        self.assertEqual(r.status_code, 200)
        # The move must have failed (folder cannot move into itself).
        self.assertEqual(r.get_json()['moved_count'], 0)
        self.assertEqual(r.get_json()['failed_count'], 1)

    def test_bulk_copy(self):
        nb_id = self._setup_notebook()
        self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'dupes'})
        self.app.post(f'/api/notebooks/{nb_id}/files',
                      data={'path': '', 'file': (io.BytesIO(b'orig'), 'src.txt')},
                      content_type='multipart/form-data')
        r = self.app.post(f'/api/notebooks/{nb_id}/files/bulk-copy',
                          json={'paths': ['src.txt'], 'dest_path': 'dupes'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['copied_count'], 1)
        # Original still present at root
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertIn('src.txt', names)
        # Copy present in dupes/
        r = self.app.get(f'/api/notebooks/{nb_id}/files?path=dupes')
        names = [f['name'] for f in r.get_json()['files']]
        self.assertIn('src.txt', names)

    def test_bulk_download_zip(self):
        nb_id = self._setup_notebook()
        self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'afolder'})
        # Put a file inside afolder so the folder has content to zip.
        self.app.post(f'/api/notebooks/{nb_id}/files',
                      data={'path': 'afolder', 'file': (io.BytesIO(b'in-folder'), 'inner.txt')},
                      content_type='multipart/form-data')
        for n in ('a.txt', 'b.txt'):
            self.app.post(f'/api/notebooks/{nb_id}/files',
                          data={'path': '', 'file': (io.BytesIO(b'content-' + n.encode()), n)},
                          content_type='multipart/form-data')
        r = self.app.post(f'/api/notebooks/{nb_id}/files/bulk-download',
                          json={'paths': ['a.txt', 'afolder', 'b.txt']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'application/zip')
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = zf.namelist()
        self.assertIn('a.txt', names)
        self.assertIn('b.txt', names)
        self.assertIn('afolder/inner.txt', names)
        self.assertEqual(zf.read('a.txt'), b'content-a.txt')
        # The hidden .nbowner ownership marker must not leak into the archive.
        self.assertNotIn('.nbowner', names)
        self.assertFalse(any(n.endswith('.nbowner') for n in names))

    def test_bulk_download_empty_returns_404(self):
        nb_id = self._setup_notebook()
        r = self.app.post(f'/api/notebooks/{nb_id}/files/bulk-download',
                          json={'paths': ['nonexistent.txt']})
        self.assertEqual(r.status_code, 404)

    def test_traversal_in_bulk_delete_rejected(self):
        nb_id = self._setup_notebook()
        self.app.post(f'/api/notebooks/{nb_id}/files/directory', json={'path': 'safe'})
        r = self.app.post(f'/api/notebooks/{nb_id}/files/bulk-delete',
                          json={'paths': ['../../etc/passwd', 'safe']})
        self.assertEqual(r.status_code, 200)
        # 'safe' deleted, traversal entry failed
        self.assertEqual(r.get_json()['deleted_count'], 1)
        self.assertEqual(r.get_json()['failed_count'], 1)

    def test_content_missing_file_404(self):
        nb_id = self._setup_notebook()
        r = self.app.get(f'/api/notebooks/{nb_id}/files/content?path=nope.txt')
        self.assertEqual(r.status_code, 404)

    def test_unauthenticated_user_blocked(self):
        nb_id = self._setup_notebook()
        self.app.get('/logout', follow_redirects=True)
        r = self.app.get(f'/api/notebooks/{nb_id}/files')
        # @login_required redirects (302) or 401
        self.assertIn(r.status_code, (302, 401, 403))


if __name__ == '__main__':
    unittest.main()
