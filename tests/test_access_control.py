"""Access-control tests for phonos_app/routes/api_extensions.py and the
notebook collaboration socket guard (roadmap task 19, step 0).

Each protected route gets: a non-member is refused (403/404) and a
member/owner still succeeds. Runs on a throw-away SQLite file (TESTING mode),
never on the local Postgres.

    venv/bin/python -m pytest tests/test_access_control.py -q
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("ENCRYPTION_KEY", "a-secure-test-key-for-dev-use-32")

patch("production.config.Config.DEV_MODE", False).start()
patch("redis.ConnectionPool").start()
patch("production.database.validate_database_config", return_value=True).start()

from production.main import app  # noqa: E402
from production.database import init_db  # noqa: E402
from phonos_app.helpers.database import get_db  # noqa: E402

MGR, CONTRIB, VIEWER, OUTSIDER, ADMIN = 1, 2, 3, 4, 5


class AccessControlBase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.tmp = tempfile.mkdtemp(prefix="phonos-ac-")
        self._saved_config = {k: app.config.get(k) for k in ("DATABASE_FILE", "DATABASE_URL", "TESTING")}
        app.config["DATABASE_FILE"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()
        with app.app_context():
            init_db()
            self._seed()

    def tearDown(self):
        # Restore the config so later test files (e.g. the Postgres smoke test) are unaffected.
        app.config.update(self._saved_config)
        os.close(self.db_fd)
        os.unlink(self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        db = get_db()
        for uid, name, adm in ((MGR, "mgr", 0), (CONTRIB, "contrib", 0), (VIEWER, "viewer", 0),
                               (OUTSIDER, "outsider", 0), (ADMIN, "admin", 1)):
            db.execute("INSERT INTO users (id, user_id, username, credits, is_admin) VALUES (%s,%s,%s,%s,%s)",
                       (uid, f"u-{name}", name, 100, adm))
        db.execute("INSERT INTO teams (id, name, slug, creator_id) VALUES (1,'T1','t1',%s)", (MGR,))
        db.execute("INSERT INTO teams (id, name, slug, creator_id) VALUES (2,'T2','t2',%s)", (OUTSIDER,))
        db.execute("INSERT INTO team_members (team_id, user_id, role) VALUES (1,%s,'owner')", (MGR,))
        db.execute("INSERT INTO team_members (team_id, user_id, role) VALUES (2,%s,'owner')", (OUTSIDER,))
        db.execute("INSERT INTO projects (id, team_id, creator_id, name, description) VALUES (1,1,%s,'P1','')", (MGR,))
        db.execute("INSERT INTO projects (id, team_id, creator_id, name, description) VALUES (2,2,%s,'P2','')", (OUTSIDER,))
        for uid, role in ((MGR, "Project Manager"), (CONTRIB, "Project Contributor"), (VIEWER, "Project Viewer")):
            db.execute("INSERT INTO project_assignments (project_id, user_id, role) VALUES (1,%s,%s)", (uid, role))
        db.execute("INSERT INTO project_assignments (project_id, user_id, role) VALUES (2,%s,'Project Manager')", (OUTSIDER,))

        self.file_path = os.path.join(self.tmp, "a.txt")
        with open(self.file_path, "w") as f:
            f.write("v1")
        self.ver_path = os.path.join(self.tmp, "a_v1.txt")
        with open(self.ver_path, "w") as f:
            f.write("old")
        db.execute("INSERT INTO files (id, project_id, creator_id, name, path) VALUES (1,1,%s,'a.txt',%s)",
                   (MGR, self.file_path))
        db.execute("INSERT INTO file_versions (id, file_id, version_number, path, size, created_by) "
                   "VALUES (1,1,1,%s,3,%s)", (self.ver_path, MGR))
        db.execute("INSERT INTO project_permissions (id, project_id, user_id, resource_type, permission_level, granted_by) "
                   "VALUES (1,1,%s,'project','read',%s)", (VIEWER, MGR))
        db.execute("INSERT INTO resource_annotations (id, resource_type, resource_id, user_id, content) "
                   "VALUES (1,'files',1,%s,'note')", (MGR,))
        db.execute("INSERT INTO budget_alerts (id, user_id, alert_type, threshold_value) VALUES (1,%s,'credits',10)", (MGR,))
        db.execute("INSERT INTO documentation_pages (id, slug, title, content, category) "
                   "VALUES (1,'intro','Intro','x','general')")
        db.execute("INSERT INTO notebooks (id, project_id, user_id, creator_id, name) VALUES (1,1,%s,%s,'NB')", (MGR, MGR))
        db.commit()

    def login(self, uid):
        with self.client.session_transaction() as s:
            s.clear()
            s["user_id"] = uid

    def row(self, sql, params=()):
        with app.app_context():
            return get_db().execute(sql, params).fetchone()


class PermissionRouteTests(AccessControlBase):
    def test_outsider_cannot_update_or_revoke(self):
        self.login(OUTSIDER)
        r = self.client.put("/api/permissions/1", json={"permission_level": "owner"})
        self.assertIn(r.status_code, (403, 404))
        r = self.client.delete("/api/permissions/1")
        self.assertIn(r.status_code, (403, 404))
        self.assertEqual(self.row("SELECT permission_level FROM project_permissions WHERE id=1")["permission_level"], "read")

    def test_non_manager_member_refused(self):
        self.login(CONTRIB)
        self.assertEqual(self.client.put("/api/permissions/1", json={"permission_level": "write"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/permissions/1").status_code, 403)

    def test_unknown_id_404(self):
        self.login(MGR)
        self.assertEqual(self.client.put("/api/permissions/99", json={"permission_level": "write"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/permissions/99").status_code, 404)

    def test_manager_succeeds(self):
        self.login(MGR)
        self.assertEqual(self.client.put("/api/permissions/1", json={"permission_level": "write"}).status_code, 200)
        self.assertEqual(self.client.delete("/api/permissions/1").status_code, 200)
        self.assertIsNone(self.row("SELECT id FROM project_permissions WHERE id=1"))


class AnalyticsTests(AccessControlBase):
    def test_project_analytics(self):
        self.login(OUTSIDER)
        self.assertIn(self.client.get("/api/projects/1/analytics").status_code, (403, 404))
        self.login(VIEWER)
        self.assertEqual(self.client.get("/api/projects/1/analytics").status_code, 200)

    def test_team_analytics(self):
        self.login(OUTSIDER)
        self.assertIn(self.client.get("/api/teams/1/analytics").status_code, (403, 404))
        self.login(MGR)
        # helpers/analytics.get_team_analytics has an unrelated placeholder bug
        # (",".join("%s" * n)); stub it, this test is about access only.
        with patch("phonos_app.routes.api_extensions.get_team_analytics", return_value={}):
            self.assertEqual(self.client.get("/api/teams/1/analytics").status_code, 200)


class FileVersionTests(AccessControlBase):
    def test_outsider_refused_everywhere(self):
        self.login(OUTSIDER)
        c = self.client
        self.assertIn(c.get("/api/files/1/versions").status_code, (403, 404))
        self.assertIn(c.post("/api/files/1/versions", json={"file_path": self.file_path}).status_code, (403, 404))
        self.assertIn(c.get("/api/files/1/versions/1").status_code, (403, 404))
        self.assertIn(c.delete("/api/files/1/versions/1").status_code, (403, 404))
        self.assertIn(c.post("/api/files/1/revert",
                             json={"version_number": 1, "current_file_path": self.file_path}).status_code, (403, 404))
        self.assertTrue(os.path.exists(self.ver_path))
        with open(self.file_path) as f:
            self.assertEqual(f.read(), "v1")

    def test_member_can_read(self):
        self.login(VIEWER)
        self.assertEqual(self.client.get("/api/files/1/versions").status_code, 200)
        self.assertEqual(self.client.get("/api/files/1/versions/1").status_code, 200)

    def test_viewer_cannot_write(self):
        self.login(VIEWER)
        self.assertEqual(self.client.delete("/api/files/1/versions/1").status_code, 403)
        self.assertEqual(self.client.post("/api/files/1/revert",
                         json={"version_number": 1, "current_file_path": self.file_path}).status_code, 403)

    def test_contributor_revert_and_create(self):
        self.login(CONTRIB)
        r = self.client.post("/api/files/1/revert", json={"version_number": 1, "current_file_path": self.file_path})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with open(self.file_path) as f:
            self.assertEqual(f.read(), "old")
        r = self.client.post("/api/files/1/versions", json={"file_path": self.file_path})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))

    def test_client_cannot_point_at_other_paths(self):
        self.login(MGR)
        other = os.path.join(self.tmp, "victim.txt")
        with open(other, "w") as f:
            f.write("keep")
        r = self.client.post("/api/files/1/revert", json={"version_number": 1, "current_file_path": other})
        self.assertEqual(r.status_code, 400)
        with open(other) as f:
            self.assertEqual(f.read(), "keep")
        self.assertEqual(self.client.post("/api/files/1/versions", json={"file_path": "/etc/passwd"}).status_code, 400)

    def test_manager_can_delete_version(self):
        self.login(MGR)
        self.assertEqual(self.client.delete("/api/files/1/versions/1").status_code, 200)


class AnnotationTests(AccessControlBase):
    def test_outsider_refused(self):
        self.login(OUTSIDER)
        self.assertIn(self.client.get("/api/annotations/1").status_code, (403, 404))
        self.assertIn(self.client.get("/api/files/1/annotations").status_code, (403, 404))
        self.assertIn(self.client.post("/api/annotations", json={
            "resource_type": "files", "resource_id": 1, "content": "x"}).status_code, (403, 404))

    def test_member_allowed(self):
        self.login(VIEWER)
        self.assertEqual(self.client.get("/api/annotations/1").status_code, 200)
        self.assertEqual(self.client.get("/api/files/1/annotations").status_code, 200)

    def test_unknown_resource_type_refused(self):
        self.login(MGR)
        self.assertEqual(self.client.get("/api/widgets/1/annotations").status_code, 403)


class DocumentationTests(AccessControlBase):
    def test_anonymous_refused(self):
        self.assertEqual(self.client.get("/api/documentation/intro").status_code, 401)
        self.assertEqual(self.client.get("/api/documentation").status_code, 401)

    def test_user_reads_but_cannot_mutate(self):
        self.login(MGR)
        self.assertEqual(self.client.get("/api/documentation/intro").status_code, 200)
        self.assertEqual(self.client.put("/api/documentation/intro", json={"title": "h"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/documentation/intro").status_code, 403)
        self.assertEqual(self.client.post("/api/documentation", json={
            "title": "t", "content": "c", "category": "g"}).status_code, 403)

    def test_admin_mutates(self):
        self.login(ADMIN)
        self.assertEqual(self.client.put("/api/documentation/intro", json={"title": "New"}).status_code, 200)


class BudgetAlertTests(AccessControlBase):
    def test_owner_only(self):
        self.login(OUTSIDER)
        self.assertEqual(self.client.get("/api/budget-alerts/1").status_code, 404)
        self.assertEqual(self.client.delete("/api/budget-alerts/1").status_code, 404)
        self.login(MGR)
        self.assertEqual(self.client.get("/api/budget-alerts/1").status_code, 200)

    def test_cannot_create_team_alert_for_foreign_team(self):
        self.login(OUTSIDER)
        r = self.client.post("/api/budget-alerts", json={"alert_type": "credits", "threshold_value": 5, "team_id": 1})
        self.assertEqual(r.status_code, 403)


class ShareAndTemplateTests(AccessControlBase):
    def test_cannot_share_foreign_resource(self):
        self.login(OUTSIDER)
        r = self.client.post("/api/share", json={"resource_type": "files", "resource_id": 1})
        self.assertIn(r.status_code, (403, 404))
        self.login(MGR)
        self.assertEqual(self.client.post("/api/share", json={"resource_type": "files", "resource_id": 1}).status_code, 201)

    def test_only_creator_revokes_link(self):
        self.login(MGR)
        token = self.client.post("/api/share", json={"resource_type": "files", "resource_id": 1}).get_json()["link"]["token"]
        self.login(OUTSIDER)
        self.assertEqual(self.client.delete(f"/api/share/{token}").status_code, 403)
        self.login(MGR)
        self.assertEqual(self.client.delete(f"/api/share/{token}").status_code, 200)

    def test_project_templates_need_membership(self):
        self.login(OUTSIDER)
        self.assertEqual(self.client.get("/api/projects/1/templates").status_code, 403)
        self.login(VIEWER)
        self.assertEqual(self.client.get("/api/projects/1/templates").status_code, 200)


class CsrfTests(AccessControlBase):
    """With TESTING off, mutating extension routes demand the CSRF header."""

    def test_missing_token_rejected_and_valid_accepted(self):
        self.login(MGR)
        app.config["TESTING"] = False
        with self.client.session_transaction() as s:
            s["csrf_token"] = "tok"
        for method, url, body in (
            ("post", "/api/templates", {}),
            ("put", "/api/permissions/1", {"permission_level": "write"}),
            ("delete", "/api/permissions/1", None),
            ("post", "/api/annotations", {}),
            ("post", "/api/files/1/versions", {}),
            ("post", "/api/files/1/revert", {}),
            ("delete", "/api/annotations/1", None),
            ("post", "/api/budget-alerts", {}),
            ("delete", "/api/budget-alerts/1", None),
            ("post", "/api/share", {}),
            ("post", "/api/api-keys", {}),
            ("put", "/api/documentation/intro", {}),
        ):
            r = getattr(self.client, method)(url, json=body) if body is not None else getattr(self.client, method)(url)
            self.assertEqual(r.status_code, 403, f"{method} {url} not CSRF protected")
            self.assertIn("CSRF", r.get_data(as_text=True))
        r = self.client.put("/api/permissions/1", json={"permission_level": "write"},
                            headers={"X-CSRF-Token": "tok"})
        self.assertEqual(r.status_code, 200)


class _FakeSocketIO:
    def __init__(self):
        self.handlers = {}

    def on(self, event, namespace=None):
        def deco(fn):
            self.handlers[event] = fn
            return fn
        return deco

    def start_background_task(self, *a, **k):
        return None


class CollabAccessTests(AccessControlBase):
    def setUp(self):
        super().setUp()
        import phonos_app.socket_handlers_notebook as nb
        self.nb = nb
        nb._collab_presence.clear()
        nb._collab_editors.clear()
        self.sio = _FakeSocketIO()
        self.emitted = []
        self.joined = []
        for target, repl in (
            ("emit", lambda *a, **k: self.emitted.append(a[0])),
            ("join_room", lambda room: self.joined.append(room)),
            ("_broadcast_presence", lambda *a, **k: None),
        ):
            p = patch.object(nb, target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.ctx = app.test_request_context("/")
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        from flask import request
        request.sid = "sid-test"
        nb.register_notebook_socket_handlers(self.sio)

    def tearDown(self):
        self.nb._collab_presence.clear()
        self.nb._collab_editors.clear()
        super().tearDown()

    def as_user(self, uid):
        return patch.object(self.nb, "get_user_from_session", return_value={"id": uid, "username": f"u{uid}"})

    def test_access_rule_matches_http_rule(self):
        a = self.nb._notebook_collab_access
        self.assertEqual(a(1, MGR), (True, True))
        self.assertEqual(a(1, CONTRIB), (True, True))
        self.assertEqual(a(1, VIEWER), (True, False))
        self.assertEqual(a(1, OUTSIDER), (False, False))
        self.assertEqual(a(999, MGR), (False, False))

    def test_outsider_join_denied_without_joining_room(self):
        with self.as_user(OUTSIDER):
            self.sio.handlers["notebook:join_collab"]({"notebook_id": 1})
        self.assertEqual(self.joined, [])
        self.assertNotIn(1, self.nb._collab_presence)
        self.assertIn("notebook:error", self.emitted)

    def test_member_join_allowed(self):
        with self.as_user(VIEWER):
            self.sio.handlers["notebook:join_collab"]({"notebook_id": 1})
        self.assertEqual(self.joined, ["notebook_collab:1"])
        self.assertIn("sid-test", self.nb._collab_presence[1])

    def test_string_notebook_id_is_normalised(self):
        with self.as_user(MGR):
            self.sio.handlers["notebook:join_collab"]({"notebook_id": "1"})
        self.assertIn(1, self.nb._collab_presence)

    def test_outsider_events_do_nothing(self):
        h = self.sio.handlers
        with self.as_user(OUTSIDER):
            h["notebook:cell_edit"]({"notebook_id": 1, "cell_id": "c", "source": "evil"})
            h["notebook:cursor_sync"]({"notebook_id": 1, "cell_id": "c", "cursor_pos": 1})
            h["notebook:cell_select"]({"notebook_id": 1, "cell_id": "c"})
            h["notebook:heartbeat"]({"notebook_id": 1})
            h["notebook:typing"]({"notebook_id": 1, "cell_id": "c", "is_typing": True})
        self.assertEqual(self.emitted, [])
        self.assertEqual(self.joined, [])
        self.assertNotIn(1, self.nb._collab_presence)

    def test_edit_needs_write_role(self):
        payload = {"notebook_id": 1, "cell_id": "c", "source": "x"}
        with self.as_user(VIEWER):
            self.sio.handlers["notebook:cell_edit"](payload)
        self.assertNotIn("notebook:cell_synced", self.emitted)
        self.nb._collab_presence.clear()  # a different socket
        with self.as_user(CONTRIB):
            self.sio.handlers["notebook:cell_edit"](payload)
        self.assertIn("notebook:cell_synced", self.emitted)

    def test_pruned_member_recovers_but_outsider_does_not(self):
        with self.as_user(VIEWER):
            self.sio.handlers["notebook:heartbeat"]({"notebook_id": 1})
        self.assertIn("sid-test", self.nb._collab_presence[1])


if __name__ == "__main__":
    unittest.main()
