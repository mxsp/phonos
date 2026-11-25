"""Tests for the Phonos Education mode (Phase 1).

Covers:
  - Per-user education flag (default off, toggled via /api/settings)
  - Education project creation gating (blocked without flag, allowed with)
  - project_type returned in GET /api/projects
  - Inviting to an education project forces the Student role
  - /api/education/projects/<id>/info returns correct role + colour
  - Roster access control (teacher yes, student no)
  - Assignment list stub (students see only published)
  - remove_student endpoint
"""
import os
import sys
import json
import unittest
from datetime import datetime, timedelta

from unittest.mock import patch

# Path setup centralized in conftest.py (fallback kept for direct execution)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')

patch('production.config.Config.DEV_MODE', False).start()
patch('redis.ConnectionPool').start()
patch('production.database.validate_database_config', return_value=True).start()

from production.main import app
from production.database import init_db
from phonos_app.helpers.database import get_db


class EducationTestCaseMixin:
    """Shared setup + helper methods for education tests."""

    def setUp(self):
        import tempfile
        self.db_fd, self.db_path = tempfile.mkstemp()
        app.config['DATABASE_FILE'] = self.db_path
        app.config['TESTING'] = True
        app.config['MAIL_DEFAULT_SENDER'] = 'noreply@testing.com'
        self.app = app.test_client()
        with app.app_context():
            init_db()

    def tearDown(self):
        import os as _os
        _os.close(self.db_fd)
        if _os.path.exists(self.db_path):
            _os.unlink(self.db_path)

    # -- user helpers -----------------------------------------------------

    def _register_and_verify_user(self, username, email, legal_name, password='password'):
        with patch('phonos_app.routes.pages.send_email') as mock_send_email, \
             patch('phonos_app.routes.pages.secrets.token_urlsafe') as mock_token:
            mock_send_email.return_value = True
            mock_token.return_value = f"token-for-{username}"
            self.app.post('/register', data=dict(
                username=username, email=email, legal_name=legal_name,
                password=password, confirm_password=password, accept_tos='on',
            ), follow_redirects=True)
            self.app.get(f'/verify_email/token-for-{username}')
        with app.app_context():
            db = get_db()
            db.execute('UPDATE users SET credits = ? WHERE username = ?', (500, username))
            db.commit()

    def _login(self, username, password='password'):
        with patch('phonos_app.routes.pages.send_email') as mock_send_email:
            mock_send_email.return_value = True
            self.app.post('/login', data=dict(username=username, password=password))
            with self.app.session_transaction() as sess:
                two_fa_code = sess.get('2fa_code')
            return self.app.post('/verify_2fa', data={'2fa_code': two_fa_code}, follow_redirects=True)

    def _logout(self):
        self.app.get('/logout', follow_redirects=True)

    def _get_user_id(self, username):
        with app.app_context():
            row = get_db().execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            return row['id'] if row else None

    # -- education helpers ------------------------------------------------

    def _enable_education(self):
        return self.app.post('/api/settings', json={'education': {'enabled': True}})

    def _disable_education(self):
        return self.app.post('/api/settings', json={'education': {'enabled': False}})

    def _create_education_project(self, name, description='Edu project'):
        return self.app.post('/api/projects', json={
            'name': name, 'description': description, 'project_type': 'education',
        })

    def _create_legacy_project(self, name, description=''):
        return self.app.post('/api/projects', json={'name': name, 'description': description})

    def _invite(self, project_id, email, role='collaborator'):
        return self.app.post(f'/api/projects/{project_id}/invitations', json={
            'email': email, 'role': role,
        })

    def _bulk_invite(self, project_id, emails):
        return self.app.post(f'/api/projects/{project_id}/invitations/bulk', json={
            'emails': emails,
        })

    def _accept_invitation(self, token):
        return self.app.get(f'/invitations/accept/{token}', follow_redirects=True)

    def _edu_info(self, project_id):
        return self.app.get(f'/api/education/projects/{project_id}/info')

    def _roster(self, project_id):
        return self.app.get(f'/api/education/projects/{project_id}/roster')

    def _assignments(self, project_id):
        return self.app.get(f'/api/education/projects/{project_id}/assignments')

    def _remove_student(self, project_id, user_id):
        return self.app.post(f'/api/education/projects/{project_id}/roster/{user_id}/remove')

    def _create_notebook(self, project_id, name='Assignment NB'):
        r = self.app.post('/api/notebooks', json={'name': name, 'project_id': project_id})
        return r.get_json()['id']

    def _create_assignment(self, project_id, notebook_id, title='HW1', **kw):
        payload = {'notebook_id': notebook_id, 'title': title,
                   'max_submissions_per_problem': kw.get('max_submissions_per_problem', 3),
                   'total_points': kw.get('total_points', 10)}
        for k in ('open_at', 'close_at', 'time_limit_minutes', 'late_policy', 'description'):
            if k in kw:
                payload[k] = kw[k]
        return self.app.post(f'/api/education/projects/{project_id}/assignments', json=payload)

    def _get_assignment(self, aid):
        return self.app.get(f'/api/education/assignments/{aid}')

    def _publish_assignment(self, aid):
        return self.app.post(f'/api/education/assignments/{aid}/publish')

    def _upsert_problem(self, aid, cell_id, problem_type, config, points=1, sort_order=0):
        return self.app.put(
            f'/api/education/assignments/{aid}/problems/{cell_id}',
            json={'problem_type': problem_type, 'config': config, 'points': points, 'sort_order': sort_order},
        )

    def _open_assignment(self, aid):
        return self.app.post(f'/api/education/assignments/{aid}/open')

    def _submit_answer(self, aid, pid, answer):
        return self.app.post(
            f'/api/education/assignments/{aid}/problems/{pid}/submit',
            json={'answer': answer},
        )

    def _list_submissions(self, aid):
        return self.app.get(f'/api/education/assignments/{aid}/submissions')

    def _manual_grade(self, aid, sid, score, feedback=''):
        return self.app.post(
            f'/api/education/assignments/{aid}/submissions/{sid}/grade',
            json={'score': score, 'feedback': feedback},
        )

    def _gradebook(self, pid):
        return self.app.get(f'/api/education/projects/{pid}/gradebook')

    def _analytics(self, aid):
        return self.app.get(f'/api/education/assignments/{aid}/analytics')

    def _grant_extension(self, aid, student_id, **kw):
        return self.app.post(
            f'/api/education/assignments/{aid}/students/{student_id}/extend',
            json=kw,
        )

    def _get_problem_id(self, aid):
        with app.app_context():
            row = get_db().execute(
                'SELECT id FROM assignment_problems WHERE assignment_id=? ORDER BY id LIMIT 1',
                (aid,)).fetchone()
            return row['id'] if row else None


class EducationFlagTests(EducationTestCaseMixin, unittest.TestCase):
    """Education flag + project creation gating."""

    def test_flag_defaults_off(self):
        self._register_and_verify_user('teach1', 'teach1@test.com', 'Teacher One')
        self._login('teach1')
        r = self.app.get('/api/settings')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('education', data)
        self.assertFalse(data['education']['enabled'])

    def test_flag_can_be_enabled(self):
        self._register_and_verify_user('teach2', 'teach2@test.com', 'Teacher Two')
        self._login('teach2')
        r = self._enable_education()
        self.assertEqual(r.status_code, 200)
        r2 = self.app.get('/api/settings')
        self.assertTrue(r2.get_json()['education']['enabled'])

    def test_cannot_create_education_project_without_flag(self):
        self._register_and_verify_user('teach3', 'teach3@test.com', 'Teacher Three')
        self._login('teach3')
        r = self._create_education_project('Class 101')
        self.assertEqual(r.status_code, 403)
        self.assertIn('Education', r.get_json().get('message', ''))

    def test_can_create_education_project_with_flag(self):
        self._register_and_verify_user('teach4', 'teach4@test.com', 'Teacher Four')
        self._login('teach4')
        self._enable_education()
        r = self._create_education_project('Class 101')
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual(body.get('project_type'), 'education')

    def test_legacy_project_still_works(self):
        self._register_and_verify_user('teach5', 'teach5@test.com', 'Teacher Five')
        self._login('teach5')
        r = self._create_legacy_project('Regular Project')
        self.assertEqual(r.status_code, 201)

    def test_invalid_project_type_rejected(self):
        self._register_and_verify_user('teach6', 'teach6@test.com', 'Teacher Six')
        self._login('teach6')
        self._enable_education()
        r = self.app.post('/api/projects', json={'name': 'Bad', 'project_type': 'banana'})
        self.assertEqual(r.status_code, 400)


class EducationProjectTypeAndRoleTests(EducationTestCaseMixin, unittest.TestCase):
    """project_type in listing + role colours + invite forces student."""

    def test_project_type_returned_in_listing(self):
        self._register_and_verify_user('teach7', 'teach7@test.com', 'Teacher Seven')
        self._login('teach7')
        self._enable_education()
        self._create_education_project('Class Listing')
        r = self.app.get('/api/projects')
        self.assertEqual(r.status_code, 200)
        projects = r.get_json()
        self.assertTrue(any(p.get('project_type') == 'education' for p in projects))

    def test_info_endpoint_teacher_color(self):
        self._register_and_verify_user('teach8', 'teach8@test.com', 'Teacher Eight')
        self._login('teach8')
        self._enable_education()
        pid = self._create_education_project('Class Info').get_json()['id']
        r = self._edu_info(pid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['is_education'])
        self.assertTrue(body['is_teacher'])
        self.assertFalse(body['is_student'])
        self.assertEqual(body['color'], '#137CBD')

    def test_invite_forces_student_role(self):
        # Teacher
        self._register_and_verify_user('teach9', 'teach9@test.com', 'Teacher Nine')
        # Student
        self._register_and_verify_user('stud9', 'stud9@test.com', 'Student Nine')
        self._login('teach9')
        self._enable_education()
        pid = self._create_education_project('Class Invite').get_json()['id']

        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            r = self._invite(pid, 'stud9@test.com', role='collaborator')
        self.assertEqual(r.status_code, 200)

        # Accept the invitation as the student
        with app.app_context():
            db = get_db()
            inv = db.execute(
                'SELECT token FROM invitations WHERE project_id = ? ORDER BY id DESC LIMIT 1',
                (pid,),
            ).fetchone()
            token = inv['token']
        self._logout()
        self._login('stud9')
        self._accept_invitation(token)

        # Student info endpoint should report red / student
        r = self._edu_info(pid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['is_student'])
        self.assertFalse(body['is_teacher'])
        self.assertEqual(body['color'], '#C53030')


class EducationBulkInviteTests(EducationTestCaseMixin, unittest.TestCase):
    """Classroom bulk invite + inviting students who haven't signed up yet."""

    def _teacher_with_class(self, teacher='bteach'):
        self._register_and_verify_user(teacher, f'{teacher}@test.com', teacher.title())
        self._login(teacher)
        self._enable_education()
        return self._create_education_project('Bulk Class').get_json()['id']

    def test_can_invite_unregistered_student(self):
        """Education projects let a teacher invite an email with no account yet."""
        pid = self._teacher_with_class('bteach1')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            r = self._invite(pid, 'future-student@school.edu', role='collaborator')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'success')
        # A pending, Student-role invitation should exist for that email.
        with app.app_context():
            inv = get_db().execute(
                "SELECT role, status FROM invitations WHERE lower(invitee_email) = 'future-student@school.edu'"
            ).fetchone()
        self.assertIsNotNone(inv)
        self.assertEqual(inv['status'], 'pending')
        self.assertEqual(inv['role'], 'Student')

    def test_unregistered_invite_surfaces_after_signup(self):
        """After the student registers with the invited email, the pending
        invitation shows up on their dashboard (email-matched)."""
        pid = self._teacher_with_class('bteach2')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 'late@school.edu', role='collaborator')
        self._logout()
        # Student signs up later with that same address.
        self._register_and_verify_user('latejoiner', 'late@school.edu', 'Late Joiner')
        self._login('latejoiner')
        r = self.app.get('/api/invitations/pending')
        self.assertEqual(r.status_code, 200)
        pending = r.get_json()
        self.assertIsInstance(pending, list)
        match = [i for i in pending if i.get('project_id') == pid]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]['role'], 'Student')

    def test_non_education_project_rejects_unregistered(self):
        """Legacy projects still require the invitee to already have an account."""
        self._register_and_verify_user('legacyowner', 'legacyowner@test.com', 'Legacy Owner')
        self._login('legacyowner')
        pid = self._create_legacy_project('Legacy P').get_json()['id']
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            r = self._invite(pid, 'nobody@nowhere.com', role='collaborator')
        self.assertEqual(r.status_code, 404)

    def test_bulk_invite_mixed_emails(self):
        """One paste of several emails invites each, reporting per-email status."""
        self._register_and_verify_user('existing_stud', 'existing@school.edu', 'Existing Stud')
        pid = self._teacher_with_class('bteach3')
        emails = 'existing@school.edu, newbie1@school.edu\nnewbie2@school.edu ; newbie1@school.edu'
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            r = self._bulk_invite(pid, emails)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # 3 unique addresses (duplicate newbie1 collapsed), all invited.
        self.assertEqual(body['total'], 3)
        self.assertEqual(body['invited'], 3)
        statuses = {res['email']: res['status'] for res in body['results']}
        self.assertEqual(statuses['existing@school.edu'], 'invited')
        self.assertEqual(statuses['newbie1@school.edu'], 'invited')
        self.assertEqual(statuses['newbie2@school.edu'], 'invited')
        # All invitations are Student-role for an education project.
        with app.app_context():
            roles = {r['role'] for r in get_db().execute(
                'SELECT DISTINCT role FROM invitations WHERE project_id = ?', (pid,)).fetchall()}
        self.assertEqual(roles, {'Student'})

    def test_bulk_invite_reports_already_invited(self):
        """A second bulk pass reports already-invited addresses instead of dupes."""
        pid = self._teacher_with_class('bteach4')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._bulk_invite(pid, 'once@school.edu')
            r = self._bulk_invite(pid, 'once@school.edu, twice@school.edu')
        body = r.get_json()
        statuses = {res['email']: res['status'] for res in body['results']}
        self.assertEqual(statuses['once@school.edu'], 'already_invited')
        self.assertEqual(statuses['twice@school.edu'], 'invited')
        self.assertEqual(body['invited'], 1)

    def test_bulk_invite_mid_batch_exception_does_not_lose_earlier_invites(self):
        """A crash processing one address must not roll back invites already
        created earlier in the same batch, and must not abort the remaining
        ones -- the endpoint's own docstring promises independent processing."""
        pid = self._teacher_with_class('bteach6')
        from phonos_app.routes import api_projects as api_projects_module
        real_create = api_projects_module._create_single_invitation

        def flaky_create(db, project_id, project_name, inviter_id, inviter_name,
                          invitee_email, normalized_role, allow_unregistered=False):
            if invitee_email == 'boom@school.edu':
                raise RuntimeError('simulated failure')
            return real_create(db, project_id, project_name, inviter_id, inviter_name,
                                invitee_email, normalized_role, allow_unregistered=allow_unregistered)

        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True), \
             patch('phonos_app.routes.api_projects._create_single_invitation', side_effect=flaky_create):
            r = self._bulk_invite(pid, 'first@school.edu, boom@school.edu, last@school.edu')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        statuses = {res['email']: res['status'] for res in body['results']}
        self.assertEqual(statuses['first@school.edu'], 'invited')
        self.assertEqual(statuses['boom@school.edu'], 'error')
        self.assertEqual(statuses['last@school.edu'], 'invited')
        self.assertEqual(body['invited'], 2)
        with app.app_context():
            row = get_db().execute(
                "SELECT 1 FROM invitations WHERE project_id=? AND lower(invitee_email)=lower(?)",
                (pid, 'first@school.edu')).fetchone()
            self.assertIsNotNone(row)

    def test_bulk_invite_requires_manager(self):
        """A student can't bulk-invite classmates."""
        pid = self._teacher_with_class('bteach5')
        self._register_and_verify_user('bstud5', 'bstud5@test.com', 'B Stud5')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 'bstud5@test.com')
        with app.app_context():
            token = get_db().execute(
                'SELECT token FROM invitations WHERE project_id = ? ORDER BY id DESC LIMIT 1', (pid,)).fetchone()['token']
        self._logout()
        self._login('bstud5')
        self._accept_invitation(token)
        r = self._bulk_invite(pid, 'sneaky@school.edu')
        self.assertIn(r.status_code, (403, 401))


class InvitationResendTests(EducationTestCaseMixin, unittest.TestCase):
    """resend_project_invitation must pick the right email variant per invitee."""

    def _resend(self, project_id, invitation_id):
        return self.app.post(f'/api/projects/{project_id}/invitations/{invitation_id}/resend')

    def _teacher_with_class(self, teacher='rteach'):
        self._register_and_verify_user(teacher, f'{teacher}@test.com', teacher.title())
        self._login(teacher)
        self._enable_education()
        return self._create_education_project('Resend Class').get_json()['id']

    def test_resend_to_unregistered_invitee_sends_signup_link_variant(self):
        pid = self._teacher_with_class('bteach7')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 'not-yet-signed-up@school.edu')
        with app.app_context():
            inv_id = get_db().execute(
                'SELECT id FROM invitations WHERE project_id = ? ORDER BY id DESC LIMIT 1', (pid,)
            ).fetchone()['id']
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True) as mock_send:
            r = self._resend(pid, inv_id)
        self.assertEqual(r.status_code, 200)
        mock_send.assert_called_once()
        _, _, body = mock_send.call_args[0]
        self.assertIn('Create your Phonos account', body)

    def test_resend_to_registered_invitee_sends_accept_variant(self):
        self._register_and_verify_user('already_reg', 'already_reg@test.com', 'Already Reg')
        pid = self._teacher_with_class('bteach8')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 'already_reg@test.com')
        with app.app_context():
            inv_id = get_db().execute(
                'SELECT id FROM invitations WHERE project_id = ? ORDER BY id DESC LIMIT 1', (pid,)
            ).fetchone()['id']
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True) as mock_send:
            r = self._resend(pid, inv_id)
        self.assertEqual(r.status_code, 200)
        mock_send.assert_called_once()
        _, _, body = mock_send.call_args[0]
        self.assertNotIn('Create your Phonos account', body)
        self.assertIn('Accept Invitation', body)


class EducationRosterTests(EducationTestCaseMixin, unittest.TestCase):
    """Roster access control + removal."""

    def _setup_class_with_student(self, teacher='teach10', student='stud10'):
        self._register_and_verify_user(teacher, f'{teacher}@test.com', teacher.title())
        self._register_and_verify_user(student, f'{student}@test.com', student.title())
        self._login(teacher)
        self._enable_education()
        pid = self._create_education_project('Roster Class').get_json()['id']
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, f'{student}@test.com', role='collaborator')
        with app.app_context():
            db = get_db()
            inv = db.execute(
                'SELECT token FROM invitations WHERE project_id = ? ORDER BY id DESC LIMIT 1',
                (pid,),
            ).fetchone()
            token = inv['token']
        self._logout()
        self._login(student)
        self._accept_invitation(token)
        self._logout()
        self._login(teacher)
        return pid, student

    def test_teacher_can_view_roster(self):
        pid, student = self._setup_class_with_student()
        r = self._roster(pid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['student_count'], 1)
        self.assertEqual(body['teacher_count'], 1)
        self.assertTrue(any(m['is_student'] for m in body['members']))

    def test_student_cannot_view_roster(self):
        pid, student = self._setup_class_with_student()
        self._logout()
        self._login(student)
        r = self._roster(pid)
        self.assertEqual(r.status_code, 403)

    def test_teacher_can_remove_student(self):
        pid, student = self._setup_class_with_student()
        sid = self._get_user_id(student)
        r = self._remove_student(pid, sid)
        self.assertEqual(r.status_code, 200)
        # Roster should now have 0 students
        r2 = self._roster(pid)
        self.assertEqual(r2.get_json()['student_count'], 0)

    def test_teacher_cannot_remove_self(self):
        pid, student = self._setup_class_with_student(teacher='teach_self')
        tid = self._get_user_id('teach_self')
        r = self._remove_student(pid, tid)
        self.assertEqual(r.status_code, 400)

    def test_roster_rejected_for_non_education_project(self):
        self._register_and_verify_user('teach11', 'teach11@test.com', 'Teacher Eleven')
        self._login('teach11')
        pid = self._create_legacy_project('Legacy').get_json()['id']
        r = self._roster(pid)
        self.assertEqual(r.status_code, 400)


class EducationAssignmentListTests(EducationTestCaseMixin, unittest.TestCase):
    """Assignment list stub — students see only published."""

    def test_teacher_sees_empty_assignments(self):
        self._register_and_verify_user('teach12', 'teach12@test.com', 'Teacher Twelve')
        self._login('teach12')
        self._enable_education()
        pid = self._create_education_project('Assign Class').get_json()['id']
        r = self._assignments(pid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['assignments'], [])
        self.assertEqual(body['role'], 'owner')

    def test_assignments_rejected_for_non_education(self):
        self._register_and_verify_user('teach13', 'teach13@test.com', 'Teacher Thirteen')
        self._login('teach13')
        pid = self._create_legacy_project('Legacy Assign').get_json()['id']
        r = self._assignments(pid)
        self.assertEqual(r.status_code, 400)


class AssignmentCRUDTests(EducationTestCaseMixin, unittest.TestCase):
    """Assignment create / get / publish / update / delete."""

    def _setup_teacher_with_project(self, teacher='t_crud'):
        self._register_and_verify_user(teacher, f'{teacher}@test.com', teacher.title())
        self._login(teacher)
        self._enable_education()
        pid = self._create_education_project('CRUD Class').get_json()['id']
        nid = self._create_notebook(pid)
        return pid, nid

    def test_create_assignment(self):
        pid, nid = self._setup_teacher_with_project()
        r = self._create_assignment(pid, nid, title='Homework 1')
        self.assertEqual(r.status_code, 201)
        self.assertIn('assignment_id', r.get_json())

    def test_get_assignment_teacher_sees_config(self):
        pid, nid = self._setup_teacher_with_project('t_get')
        aid = self._create_assignment(pid, nid).get_json()['assignment_id']
        self._upsert_problem(aid, 'cell-1', 'numeric',
                             {'correct_answer': '42', 'tolerance': 0.1, 'points': 5})
        r = self._get_assignment(aid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['is_teacher'])
        # Teacher sees encrypted answer field (not the plaintext)
        self.assertIn('correct_answer_enc', body['problems'][0]['config'])
        self.assertNotIn('correct_answer', body['problems'][0]['config'])

    def test_publish_assignment(self):
        pid, nid = self._setup_teacher_with_project('t_pub')
        aid = self._create_assignment(pid, nid).get_json()['assignment_id']
        r = self._publish_assignment(aid)
        self.assertEqual(r.status_code, 200)
        r2 = self._get_assignment(aid)
        self.assertTrue(r2.get_json()['assignment']['published'])

    def test_publish_rejected_once_project_reverts_to_non_education(self):
        # An assignment can only be created while project_type == 'education',
        # but nothing stopped the project from being reverted afterwards --
        # publish_assignment must re-check project_type itself rather than
        # trusting that a still-existing assignment row implies it's valid.
        pid, nid = self._setup_teacher_with_project('t_pub3')
        aid = self._create_assignment(pid, nid).get_json()['assignment_id']
        with app.app_context():
            db = get_db()
            db.execute('UPDATE projects SET project_type = ? WHERE id = ?', ('default', pid))
            db.commit()
        r = self._publish_assignment(aid)
        self.assertEqual(r.status_code, 400)
        r2 = self._get_assignment(aid)
        self.assertFalse(r2.get_json()['assignment']['published'])

    def test_student_cannot_see_unpublished(self):
        pid, nid = self._setup_teacher_with_project('t_pub2')
        self._register_and_verify_user('s_pub2', 's_pub2@test.com', 'Student')
        aid = self._create_assignment(pid, nid).get_json()['assignment_id']
        # Accept invite
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 's_pub2@test.com')
        with app.app_context():
            tok = get_db().execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid,)).fetchone()['token']
        self._logout()
        self._login('s_pub2')
        self._accept_invitation(tok)
        r = self._get_assignment(aid)
        self.assertEqual(r.status_code, 404)

    def test_delete_assignment(self):
        pid, nid = self._setup_teacher_with_project('t_del')
        aid = self._create_assignment(pid, nid).get_json()['assignment_id']
        r = self.app.delete(f'/api/education/assignments/{aid}')
        self.assertEqual(r.status_code, 200)
        r2 = self._get_assignment(aid)
        self.assertEqual(r2.status_code, 404)

    def test_student_cannot_create_assignment(self):
        pid, nid = self._setup_teacher_with_project('t_perm')
        self._register_and_verify_user('s_perm', 's_perm@test.com', 'Student')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 's_perm@test.com')
        with app.app_context():
            tok = get_db().execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid,)).fetchone()['token']
        self._logout()
        self._login('s_perm')
        self._accept_invitation(tok)
        r = self._create_assignment(pid, nid)
        self.assertEqual(r.status_code, 403)


class ProblemAuthoringTests(EducationTestCaseMixin, unittest.TestCase):
    """Problem upsert + secret encryption + student config sanitization."""

    def test_numeric_answer_encrypted(self):
        self._register_and_verify_user('t_enc', 't_enc@test.com', 'Teacher')
        self._login('t_enc')
        self._enable_education()
        pid = self._create_education_project('Enc Class').get_json()['id']
        nid = self._create_notebook(pid)
        aid = self._create_assignment(pid, nid).get_json()['assignment_id']
        r = self._upsert_problem(aid, 'cell-1', 'numeric',
                                 {'correct_answer': '42', 'tolerance': 0.01, 'points': 5})
        self.assertEqual(r.status_code, 200)
        # Verify in DB that plaintext is gone and enc field exists
        with app.app_context():
            db = get_db()
            row = db.execute('SELECT config FROM assignment_problems WHERE assignment_id=? AND cell_id=?',
                             (aid, 'cell-1')).fetchone()
            cfg = json.loads(row['config'])
            self.assertNotIn('correct_answer', cfg)
            self.assertIn('correct_answer_enc', cfg)

    def test_student_sees_sanitized_config(self):
        self._register_and_verify_user('t_san', 't_san@test.com', 'Teacher')
        self._login('t_san')
        self._enable_education()
        pid = self._create_education_project('San Class').get_json()['id']
        nid = self._create_notebook(pid)
        aid = self._create_assignment(pid, nid).get_json()['assignment_id']
        self._upsert_problem(aid, 'cell-1', 'code',
                             {'test_cases': [{'name': 'test1', 'code': 'assert f(1)==2', 'expected': 'None'}],
                              'points': 3})
        self._publish_assignment(aid)
        # Student
        self._register_and_verify_user('s_san', 's_san@test.com', 'Student')
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 's_san@test.com')
        with app.app_context():
            tok = get_db().execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid,)).fetchone()['token']
        self._logout()
        self._login('s_san')
        self._accept_invitation(tok)
        r = self._get_assignment(aid)
        body = r.get_json()
        cfg = body['problems'][0]['config']
        # Student sees test names but NOT test code or expected values
        self.assertIn('test_cases', cfg)
        self.assertEqual(cfg['test_cases'], [{'name': 'test1'}])
        self.assertNotIn('correct_answer_enc', cfg)


class SubmissionAndGradingTests(EducationTestCaseMixin, unittest.TestCase):
    """Open, submit, auto-grade, max-attempts, manual grade, gradebook."""

    def _setup_class_with_assignment(self, teacher='t_sub', student='s_sub',
                                     max_subs=3, problem_type='numeric', config=None):
        self._register_and_verify_user(teacher, f'{teacher}@test.com', teacher.title())
        self._register_and_verify_user(student, f'{student}@test.com', student.title())
        self._login(teacher)
        self._enable_education()
        pid = self._create_education_project('Sub Class').get_json()['id']
        nid = self._create_notebook(pid)
        aid = self._create_assignment(pid, nid, max_submissions_per_problem=max_subs).get_json()['assignment_id']
        cfg = config or {'correct_answer': '42', 'tolerance': 0.5, 'points': 5}
        self._upsert_problem(aid, 'cell-1', problem_type, cfg, points=cfg.get('points', 5))
        self._publish_assignment(aid)
        # Invite + accept
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, f'{student}@test.com')
        with app.app_context():
            tok = get_db().execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid,)).fetchone()['token']
        self._logout()
        self._login(student)
        self._accept_invitation(tok)
        return pid, aid

    def test_open_assignment_starts_countdown(self):
        pid, aid = self._setup_class_with_assignment(
            't_open', 's_open', config={'correct_answer': '42', 'tolerance': 0.5, 'points': 5},
            # Can't easily pass time_limit via _setup helper, so set directly:
        )
        # Set a time limit on the assignment
        self._logout()
        self._login('t_open')
        self.app.patch(f'/api/education/assignments/{aid}', json={'time_limit_minutes': 60})
        self._logout()
        self._login('s_open')
        r = self._open_assignment(aid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIsNotNone(body['time_remaining_sec'])
        self.assertEqual(body['time_remaining_sec'], 3600)

    def test_numeric_auto_grade_correct(self):
        pid, aid = self._setup_class_with_assignment('t_nc', 's_nc')
        pid2 = self._get_problem_id(aid)
        r = self._submit_answer(aid, pid2, '42')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['graded'])
        self.assertEqual(body['auto_score'], 5.0)
        self.assertTrue(body['is_correct'])
        self.assertEqual(body['attempts_remaining'], 2)

    def test_numeric_auto_grade_wrong(self):
        pid, aid = self._setup_class_with_assignment('t_nw', 's_nw')
        pid2 = self._get_problem_id(aid)
        r = self._submit_answer(aid, pid2, '100')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['graded'])
        self.assertEqual(body['auto_score'], 0.0)
        self.assertFalse(body['is_correct'])

    def test_is_correct_survives_late_penalty(self):
        # A correct-but-late submission gets its score docked 20%, which would
        # make the old client-side "score >= points" heuristic misreport it as
        # incorrect. The server must report the raw correctness separately.
        self._register_and_verify_user('t_late', 't_late@test.com', 'T Late')
        self._register_and_verify_user('s_late', 's_late@test.com', 'S Late')
        self._login('t_late')
        self._enable_education()
        pid = self._create_education_project('Late Class').get_json()['id']
        nid = self._create_notebook(pid)
        close_at = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        aid = self._create_assignment(
            pid, nid, max_submissions_per_problem=3,
            close_at=close_at, late_policy='penalty',
        ).get_json()['assignment_id']
        cfg = {'correct_answer': '42', 'tolerance': 0.5, 'points': 5}
        self._upsert_problem(aid, 'cell-1', 'numeric', cfg, points=5)
        self._publish_assignment(aid)
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, 's_late@test.com')
        with app.app_context():
            tok = get_db().execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid,)).fetchone()['token']
        self._logout()
        self._login('s_late')
        self._accept_invitation(tok)

        prob_id = self._get_problem_id(aid)
        r = self._submit_answer(aid, prob_id, '42')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # Score docked below the points threshold...
        self.assertEqual(body['auto_score'], 4.0)
        # ...but is_correct still reflects the ungraded correctness.
        self.assertTrue(body['is_correct'])

        r2 = self._list_submissions(aid)
        self.assertEqual(r2.status_code, 200)
        subs = r2.get_json()['submissions']
        self.assertEqual(len(subs), 1)
        self.assertTrue(subs[0]['is_correct'])
        self.assertEqual(subs[0]['final_score'], 4.0)

    def test_mc_auto_grade(self):
        pid, aid = self._setup_class_with_assignment(
            't_mc', 's_mc', problem_type='mc',
            config={'correct_index': 1, 'options': ['red', 'blue', 'green'], 'points': 2})
        pid2 = self._get_problem_id(aid)
        # Correct: index 1
        r = self._submit_answer(aid, pid2, '1')
        self.assertEqual(r.get_json()['auto_score'], 2.0)
        self.assertTrue(r.get_json()['is_correct'])
        # Wrong: index 0
        r2 = self._submit_answer(aid, pid2, '0')
        self.assertEqual(r2.get_json()['auto_score'], 0.0)

    def test_code_auto_grade(self):
        pid, aid = self._setup_class_with_assignment(
            't_code', 's_code', problem_type='code',
            config={'test_cases': [{'name': 't1', 'code': 'assert add(1,2)==3'}], 'points': 4,
                    'timeout_sec': 5})
        pid2 = self._get_problem_id(aid)
        # Correct code
        r = self._submit_answer(aid, pid2, 'def add(a,b):\n    return a+b')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['graded'])
        self.assertEqual(body['auto_score'], 4.0)
        self.assertTrue(body['is_correct'])

    def test_code_auto_grade_wrong(self):
        pid, aid = self._setup_class_with_assignment(
            't_cw', 's_cw', problem_type='code',
            config={'test_cases': [{'name': 't1', 'code': 'assert add(1,2)==3'}], 'points': 4,
                    'timeout_sec': 5})
        pid2 = self._get_problem_id(aid)
        r = self._submit_answer(aid, pid2, 'def add(a,b):\n    return a-b')
        body = r.get_json()
        self.assertEqual(body['auto_score'], 0.0)
        self.assertFalse(body['is_correct'])

    def test_text_answer_pending_manual(self):
        pid, aid = self._setup_class_with_assignment(
            't_txt', 's_txt', problem_type='text', config={'points': 5})
        pid2 = self._get_problem_id(aid)
        r = self._submit_answer(aid, pid2, 'My essay answer...')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body['graded'])
        self.assertIsNone(body['auto_score'])

    def test_max_attempts_enforced(self):
        pid, aid = self._setup_class_with_assignment('t_max', 's_max', max_subs=2)
        pid2 = self._get_problem_id(aid)
        # Attempt 1
        self._submit_answer(aid, pid2, '41')
        # Attempt 2
        self._submit_answer(aid, pid2, '40')
        # Attempt 3 — should be blocked
        r = self._submit_answer(aid, pid2, '39')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()['attempts_remaining'], 0)

    def test_manual_grade_and_gradebook(self):
        pid, aid = self._setup_class_with_assignment(
            't_mg', 's_mg', problem_type='text', config={'points': 10})
        pid2 = self._get_problem_id(aid)
        # Student submits a text answer
        r = self._submit_answer(aid, pid2, 'My essay...')
        sid = r.get_json()['submission_id']
        # Teacher grades it
        self._logout()
        self._login('t_mg')
        r2 = self._manual_grade(aid, sid, 7.5, 'Good effort')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()['final_score'], 7.5)
        # Gradebook
        r3 = self._gradebook(pid)
        self.assertEqual(r3.status_code, 200)
        gb = r3.get_json()
        self.assertEqual(len(gb['students']), 1)
        self.assertEqual(gb['students'][0]['grades'][0]['score'], 7.5)

    def test_student_cannot_grade(self):
        pid, aid = self._setup_class_with_assignment('t_sg', 's_sg', problem_type='text')
        pid2 = self._get_problem_id(aid)
        r = self._submit_answer(aid, pid2, 'answer')
        sid = r.get_json()['submission_id']
        r2 = self._manual_grade(aid, sid, 10)
        self.assertEqual(r2.status_code, 403)

    def test_analytics(self):
        pid, aid = self._setup_class_with_assignment('t_an', 's_an')
        pid2 = self._get_problem_id(aid)
        self._submit_answer(aid, pid2, '42')  # correct
        self._logout()
        self._login('t_an')
        r = self._analytics(aid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body['problems']), 1)
        self.assertEqual(body['problems'][0]['attempts'], 1)
        self.assertEqual(body['problems'][0]['perfect_count'], 1)

    def test_student_submissions_own_only(self):
        pid, aid = self._setup_class_with_assignment('t_so', 's_so')
        pid2 = self._get_problem_id(aid)
        self._submit_answer(aid, pid2, '42')
        r = self._list_submissions(aid)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body['is_teacher'])
        self.assertEqual(len(body['submissions']), 1)

    def test_teacher_sees_all_submissions(self):
        pid, aid = self._setup_class_with_assignment('t_ta', 's_ta')
        pid2 = self._get_problem_id(aid)
        self._submit_answer(aid, pid2, '42')
        self._logout()
        self._login('t_ta')
        r = self._list_submissions(aid)
        body = r.get_json()
        self.assertTrue(body['is_teacher'])
        self.assertEqual(len(body['submissions']), 1)
        self.assertEqual(body['submissions'][0]['student'], 's_ta')

    def test_grant_extension_extra_attempts(self):
        pid, aid = self._setup_class_with_assignment('t_ext', 's_ext', max_subs=1)
        pid2 = self._get_problem_id(aid)
        sid = self._get_user_id('s_ext')
        # Use up the only normal attempt
        self._submit_answer(aid, pid2, '41')
        r = self._submit_answer(aid, pid2, '40')
        self.assertEqual(r.status_code, 403)
        # Teacher grants 2 extra attempts
        self._logout()
        self._login('t_ext')
        r2 = self._grant_extension(aid, sid, extra_attempts=2)
        self.assertEqual(r2.status_code, 200)
        # Student can now submit twice more
        self._logout()
        self._login('s_ext')
        r3 = self._submit_answer(aid, pid2, '40')
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.get_json()['attempts_remaining'], 1)
        r4 = self._submit_answer(aid, pid2, '39')
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.get_json()['attempts_remaining'], 0)
        r5 = self._submit_answer(aid, pid2, '38')
        self.assertEqual(r5.status_code, 403)

    def test_grant_extension_time_extension(self):
        pid, aid = self._setup_class_with_assignment('t_ext2', 's_ext2')
        sid = self._get_user_id('s_ext2')
        self._logout()
        self._login('t_ext2')
        self.app.patch(f'/api/education/assignments/{aid}', json={'time_limit_minutes': 60})
        self._logout()
        self._login('s_ext2')
        self._open_assignment(aid)
        self._logout()
        self._login('t_ext2')
        r = self._grant_extension(aid, sid, extend_minutes=30)
        self.assertEqual(r.status_code, 200)
        self._logout()
        self._login('s_ext2')
        r2 = self._open_assignment(aid)
        self.assertGreater(r2.get_json()['time_remaining_sec'], 3600)

    def test_grant_extension_repeated_calls_accumulate(self):
        pid, aid = self._setup_class_with_assignment('t_ext3', 's_ext3', max_subs=1)
        pid2 = self._get_problem_id(aid)
        sid = self._get_user_id('s_ext3')
        self._logout()
        self._login('t_ext3')
        r1 = self._grant_extension(aid, sid, extra_attempts=1)
        self.assertEqual(r1.status_code, 200)
        r2 = self._grant_extension(aid, sid, extra_attempts=1)
        self.assertEqual(r2.status_code, 200)
        self._logout()
        self._login('s_ext3')
        # 1 normal + 2 granted = 3 total attempts allowed
        self.assertEqual(self._submit_answer(aid, pid2, '1').status_code, 200)
        self.assertEqual(self._submit_answer(aid, pid2, '2').status_code, 200)
        self.assertEqual(self._submit_answer(aid, pid2, '3').status_code, 200)
        self.assertEqual(self._submit_answer(aid, pid2, '4').status_code, 403)


class TaskEndpointsTests(EducationTestCaseMixin, unittest.TestCase):
    """list_tasks / create_task / update_task."""

    def _setup(self, teacher='t_task', student='s_task'):
        self._register_and_verify_user(teacher, f'{teacher}@test.com', teacher.title())
        self._register_and_verify_user(student, f'{student}@test.com', student.title())
        self._login(teacher)
        self._enable_education()
        pid = self._create_education_project('Task Class').get_json()['id']
        nid = self._create_notebook(pid)
        aid = self._create_assignment(pid, nid, max_submissions_per_problem=2).get_json()['assignment_id']
        self._publish_assignment(aid)
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid, f'{student}@test.com')
        with app.app_context():
            tok = get_db().execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid,)).fetchone()['token']
        self._logout()
        self._login(student)
        self._accept_invitation(tok)
        self._logout()
        self._login(teacher)
        return pid, aid

    def test_teacher_lists_tasks_empty(self):
        _, aid = self._setup()
        r = self.app.get(f'/api/education/assignments/{aid}/tasks')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['tasks'], [])
        self.assertTrue(body['is_teacher'])

    def test_notebook_lazily_becomes_assignment_then_task(self):
        """Mirrors the notebook IDE flow: a fresh notebook (not yet an
        assignment) is turned into one on demand, then a problem is authored."""
        self._register_and_verify_user('t_lazy', 't_lazy@test.com', 'T Lazy')
        self._login('t_lazy')
        self._enable_education()
        pid = self._create_education_project('Lazy Class').get_json()['id']
        nid = self._create_notebook(pid)

        # 1. Before authoring, the notebook is not an assignment yet.
        r = self.app.get(f'/api/education/notebooks/{nid}/assignment')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()['assignment'])

        # 2. Frontend creates the assignment on first "save problem".
        r = self.app.post(f'/api/education/projects/{pid}/assignments', json={
            'notebook_id': nid, 'title': 'Assignment NB',
        })
        self.assertEqual(r.status_code, 201)
        aid = r.get_json()['assignment_id']

        # 3. Then it posts the task.
        r = self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'numeric', 'title': 'Q1', 'prompt': 'What is 2+2?',
            'config': {'correct_answer': '4', 'tolerance': 0.1}, 'points': 5,
        })
        self.assertEqual(r.status_code, 201)
        cell_id = r.get_json()['cell_id']

        # The task exists and the notebook gained a problem + answer cell.
        tasks = self.app.get(f'/api/education/assignments/{aid}/tasks').get_json()['tasks']
        self.assertEqual(len(tasks), 1)
        nb = self.app.get(f'/api/notebooks/{nid}').get_json()['notebook']
        roles = [c.get('metadata', {}).get('education_role') for c in nb['cells']]
        self.assertIn('problem', roles)
        self.assertIn('answer', roles)
        self.assertTrue(any(c['id'] == cell_id for c in nb['cells']))

    def test_notebook_assignment_create_is_idempotent(self):
        """A second create for the same notebook 409s (frontend then re-fetches)."""
        self._register_and_verify_user('t_idem', 't_idem@test.com', 'T Idem')
        self._login('t_idem')
        self._enable_education()
        pid = self._create_education_project('Idem Class').get_json()['id']
        nid = self._create_notebook(pid)
        first = self.app.post(f'/api/education/projects/{pid}/assignments', json={
            'notebook_id': nid, 'title': 'NB'})
        self.assertEqual(first.status_code, 201)
        second = self.app.post(f'/api/education/projects/{pid}/assignments', json={
            'notebook_id': nid, 'title': 'NB again'})
        self.assertEqual(second.status_code, 409)
        # The GET the frontend falls back to resolves the existing assignment.
        got = self.app.get(f'/api/education/notebooks/{nid}/assignment').get_json()
        self.assertIsNotNone(got['assignment'])
        self.assertEqual(got['assignment']['id'], first.get_json()['assignment_id'])

    def test_student_lists_tasks_with_sanitized_config(self):
        _, aid = self._setup()
        # Teacher creates a problem
        self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'numeric', 'title': 'Q1',
            'prompt': 'What is 2+2?',
            'config': {'correct_answer': '4', 'tolerance': 0.1, 'points': 5},
            'points': 5,
        })
        self._logout()
        self._login('s_task')
        r = self.app.get(f'/api/education/assignments/{aid}/tasks')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body['tasks']), 1)
        self.assertFalse(body['is_teacher'])
        cfg = body['tasks'][0]['config']
        self.assertNotIn('correct_answer', cfg)
        self.assertNotIn('correct_answer_enc', cfg)

    def test_create_task_returns_cell_id_and_appends_notebook(self):
        _, aid = self._setup()
        r = self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'code', 'title': 'Write add()',
            'prompt': 'Implement add(a, b)',
            'config': {'test_cases': [{'name': 't1', 'code': 'assert add(1,2)==3'}], 'points': 4},
            'points': 4, 'time_limit_minutes': 10,
        })
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertIn('cell_id', body)
        self.assertEqual(body['problem_type'], 'code')
        self.assertEqual(body['points'], 4)
        self.assertEqual(body['time_limit_minutes'], 10)
        # Verify the problem was stored
        r2 = self.app.get(f'/api/education/assignments/{aid}/tasks')
        self.assertEqual(len(r2.get_json()['tasks']), 1)

    def test_create_task_invalid_type_rejected(self):
        _, aid = self._setup()
        r = self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'invalid', 'title': 'Bad',
        })
        self.assertEqual(r.status_code, 400)

    def test_update_task_points_and_time_limit(self):
        _, aid = self._setup()
        r = self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'numeric', 'title': 'Q1',
            'config': {'correct_answer': '42', 'points': 5},
            'points': 5,
        })
        pid = self._get_problem_id(aid)
        r2 = self.app.patch(f'/api/education/assignments/{aid}/tasks/{pid}', json={
            'points': 8, 'time_limit_minutes': 15,
        })
        self.assertEqual(r2.status_code, 200)
        # Verify update
        with app.app_context():
            row = get_db().execute(
                'SELECT points, time_limit_minutes FROM assignment_problems WHERE id=?', (pid,)
            ).fetchone()
            self.assertEqual(float(row['points']), 8.0)
            self.assertEqual(int(row['time_limit_minutes']), 15)

    def test_update_config_encrypts_answer(self):
        _, aid = self._setup()
        r = self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'numeric', 'title': 'Q1',
            'config': {'correct_answer': '42', 'points': 5},
            'points': 5,
        })
        pid = self._get_problem_id(aid)
        self.app.patch(f'/api/education/assignments/{aid}/tasks/{pid}', json={
            'config': {'correct_answer': '100'},
        })
        with app.app_context():
            row = get_db().execute(
                'SELECT config FROM assignment_problems WHERE id=?', (pid,)
            ).fetchone()
            cfg = json.loads(row['config'])
            self.assertNotIn('correct_answer', cfg)
            self.assertIn('correct_answer_enc', cfg)

    def test_update_task_prompt_persists_and_syncs_notebook_cell(self):
        """update_task previously only merged data['config'], silently dropping
        the top-level title/prompt fields the Problem Editor actually sends,
        and never touched the notebook cell created by create_task -- so an
        edited prompt would vanish from the config and the IDE would keep
        rendering the stale original prompt."""
        pid_project, aid = self._setup()
        r = self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'numeric', 'title': 'Q1', 'prompt': 'What is 2+2?',
            'config': {'correct_answer': '4', 'points': 5},
            'points': 5,
        })
        cell_id = r.get_json()['cell_id']
        pid = self._get_problem_id(aid)

        r2 = self.app.patch(f'/api/education/assignments/{aid}/tasks/{pid}', json={
            'title': 'Q1 revised', 'prompt': 'What is 3+3?', 'config': {'points': 5},
        })
        self.assertEqual(r2.status_code, 200)

        with app.app_context():
            db = get_db()
            row = db.execute('SELECT config FROM assignment_problems WHERE id=?', (pid,)).fetchone()
            cfg = json.loads(row['config'])
            self.assertEqual(cfg['title'], 'Q1 revised')
            self.assertEqual(cfg['prompt'], 'What is 3+3?')

            aid_row = db.execute('SELECT notebook_id FROM education_assignments WHERE id=?', (aid,)).fetchone()
            nb = db.execute('SELECT cells FROM notebooks WHERE id=?', (aid_row['notebook_id'],)).fetchone()
            cells = json.loads(nb['cells']) if isinstance(nb['cells'], str) else nb['cells']
            problem_cell = next(c for c in cells if c['id'] == cell_id)
            self.assertEqual(problem_cell['source'], 'What is 3+3?')

    def test_update_nonexistent_task_404(self):
        _, aid = self._setup()
        r = self.app.patch(f'/api/education/assignments/{aid}/tasks/99999', json={'points': 1})
        self.assertEqual(r.status_code, 404)

    def test_student_cannot_create_task(self):
        _, aid = self._setup()
        self._logout()
        self._login('s_task')
        r = self.app.post(f'/api/education/assignments/{aid}/tasks', json={
            'problem_type': 'numeric', 'title': 'Hack',
            'config': {'correct_answer': '1', 'points': 1},
        })
        self.assertEqual(r.status_code, 403)

    def test_student_cannot_update_task(self):
        _, aid = self._setup()
        self._logout()
        self._login('s_task')
        pid = self._get_problem_id(aid)
        if pid:
            r = self.app.patch(f'/api/education/assignments/{aid}/tasks/{pid}', json={'points': 1})
            self.assertEqual(r.status_code, 403)


class CrossProjectDashboardTests(EducationTestCaseMixin, unittest.TestCase):
    """/api/education/my-classes and /api/education/my-assignments."""

    def _my_classes(self):
        return self.app.get('/api/education/my-classes')

    def _my_assignments(self):
        return self.app.get('/api/education/my-assignments')

    def test_my_classes_only_shows_taught_education_projects(self):
        self._register_and_verify_user('t_mc', 't_mc@test.com', 'T Mc')
        self._register_and_verify_user('s_mc', 's_mc@test.com', 'S Mc')
        self._login('t_mc')
        self._enable_education()

        # Project A: has an assignment, a student, and one ungraded submission.
        pid_a = self._create_education_project('Class A').get_json()['id']
        nid_a = self._create_notebook(pid_a)
        aid_a = self._create_assignment(pid_a, nid_a, title='HW A').get_json()['assignment_id']
        self._upsert_problem(aid_a, 'cell-1', 'text', {'points': 5}, points=5)
        self._publish_assignment(aid_a)
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid_a, 's_mc@test.com')
        with app.app_context():
            tok = get_db().execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid_a,)).fetchone()['token']
        self._logout()
        self._login('s_mc')
        self._accept_invitation(tok)
        pid_problem = self._get_problem_id(aid_a)
        self._submit_answer(aid_a, pid_problem, 'my free-text answer')
        self._logout()
        self._login('t_mc')

        # Project B: empty education project (no assignments, no students).
        self._create_education_project('Class B')

        # A legacy (non-education) project the same user owns -- must be excluded.
        self._create_legacy_project('Not A Class')

        r = self._my_classes()
        self.assertEqual(r.status_code, 200)
        classes = r.get_json()['classes']
        names = {c['project_name'] for c in classes}
        self.assertEqual(names, {'Class A', 'Class B'})

        class_a = next(c for c in classes if c['project_name'] == 'Class A')
        self.assertEqual(class_a['assignment_count'], 1)
        self.assertEqual(class_a['published_count'], 1)
        self.assertEqual(class_a['student_count'], 1)
        self.assertEqual(class_a['ungraded_count'], 1)

        class_b = next(c for c in classes if c['project_name'] == 'Class B')
        self.assertEqual(class_b['assignment_count'], 0)
        self.assertEqual(class_b['student_count'], 0)
        self.assertEqual(class_b['ungraded_count'], 0)

    def test_my_classes_empty_for_user_with_no_taught_projects(self):
        self._register_and_verify_user('t_mc_none', 't_mc_none@test.com', 'T None')
        self._login('t_mc_none')
        r = self._my_classes()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['classes'], [])

    def test_my_assignments_only_shows_published_from_enrolled_projects(self):
        self._register_and_verify_user('t_ma', 't_ma@test.com', 'T Ma')
        self._register_and_verify_user('s_ma', 's_ma@test.com', 'S Ma')
        self._login('t_ma')
        self._enable_education()

        # Project A: one published + one unpublished assignment.
        pid_a = self._create_education_project('Course A').get_json()['id']
        nid_a1 = self._create_notebook(pid_a, 'NB A1')
        aid_a_pub = self._create_assignment(pid_a, nid_a1, title='Published A').get_json()['assignment_id']
        self._upsert_problem(aid_a_pub, 'cell-1', 'numeric',
                              {'correct_answer': '1', 'tolerance': 0.1, 'points': 5}, points=5)
        self._publish_assignment(aid_a_pub)

        nid_a2 = self._create_notebook(pid_a, 'NB A2')
        self._create_assignment(pid_a, nid_a2, title='Draft A')  # left unpublished

        # Project B: one published assignment.
        pid_b = self._create_education_project('Course B').get_json()['id']
        nid_b = self._create_notebook(pid_b)
        aid_b_pub = self._create_assignment(pid_b, nid_b, title='Published B').get_json()['assignment_id']
        self._upsert_problem(aid_b_pub, 'cell-1', 'numeric',
                              {'correct_answer': '1', 'tolerance': 0.1, 'points': 5}, points=5)
        self._publish_assignment(aid_b_pub)

        # Enroll the student in both projects.
        with patch('phonos_app.routes.api_projects._send_project_email', return_value=True):
            self._invite(pid_a, 's_ma@test.com')
            self._invite(pid_b, 's_ma@test.com')
        with app.app_context():
            db = get_db()
            tok_a = db.execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid_a,)).fetchone()['token']
            tok_b = db.execute(
                'SELECT token FROM invitations WHERE project_id=? ORDER BY id DESC LIMIT 1',
                (pid_b,)).fetchone()['token']
        self._logout()
        self._login('s_ma')
        self._accept_invitation(tok_a)
        self._accept_invitation(tok_b)

        # Submit and get graded on assignment A to exercise score/status fields.
        pid_problem_a = self._get_problem_id(aid_a_pub)
        self._submit_answer(aid_a_pub, pid_problem_a, '1')

        r = self._my_assignments()
        self.assertEqual(r.status_code, 200)
        assignments = r.get_json()['assignments']
        titles = {a['title'] for a in assignments}
        self.assertEqual(titles, {'Published A', 'Published B'})

        a_entry = next(a for a in assignments if a['title'] == 'Published A')
        self.assertEqual(a_entry['project_name'], 'Course A')
        self.assertEqual(a_entry['problem_count'], 1)
        self.assertEqual(a_entry['submitted_problem_count'], 1)
        self.assertEqual(a_entry['score'], 5.0)
        self.assertEqual(a_entry['max_score'], 5.0)

        b_entry = next(a for a in assignments if a['title'] == 'Published B')
        self.assertEqual(b_entry['project_name'], 'Course B')
        self.assertEqual(b_entry['submitted_problem_count'], 0)
        self.assertEqual(b_entry['status'], 'not_started')
        self.assertIsNone(b_entry['score'])

    def test_my_assignments_empty_for_user_with_no_enrolled_projects(self):
        self._register_and_verify_user('s_ma_none', 's_ma_none@test.com', 'S None')
        self._login('s_ma_none')
        r = self._my_assignments()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['assignments'], [])
