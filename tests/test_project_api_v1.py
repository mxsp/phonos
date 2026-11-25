#!/usr/bin/env python3
"""
In-depth tests for the Project API v1 (/api/project/v1).

Covers, against a real Flask app on a throwaway SQLite DB:
  - auth (missing / invalid / non-project keys rejected)
  - project + models endpoints (models expose their parameter schema)
  - running a job with model + sequence + params (real submit_job_helper path)
  - INPUT is stored encrypted at rest (DB holds ciphertext, not the sequence)
  - job detail decrypts the submitted input + params back (round trip)
  - RESULT retrieval decrypts, and caches an encrypted result at rest
  - the wrong key cannot decrypt another key's payloads (dual-use isolation)
  - project scoping (a key cannot see another project's job)
  - the 10 requests/minute rate limit returns 429 on the 11th call

Run:
    python3.11 -m pytest tests/test_project_api_v1.py -v
    python3.11 tests/test_project_api_v1.py        # plain unittest
"""
import io
import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("ENCRYPTION_KEY", "a-secure-test-key-for-dev-use-32")

# Pre-import patches (mirror tests/test_main.py) so the app imports cleanly.
patch("production.config.Config.DEV_MODE", False).start()
patch("redis.ConnectionPool").start()
patch("production.database.validate_database_config", return_value=True).start()

from production.main import app  # noqa: E402
from production.database import init_db  # noqa: E402
from phonos_app.helpers.database import get_db  # noqa: E402
from phonos_app.helpers.paths import get_safe_project_path  # noqa: E402
from phonos_app.helpers import project_api_crypto as crypto  # noqa: E402
from phonos_app.helpers.project_api_keys import create_project_api_key  # noqa: E402

PREFIX = "/api/project/v1"
TEST_SEQUENCE = "MKFLILLFNILCLFPVLAADNHGVTETKAAVDETKAAVDET"
TEST_PARAMS = {"num_recycles": 7, "return_representations": True, "tag": "secret-payload-xyz"}


class ProjectAPITestCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.out_dir = tempfile.mkdtemp(prefix="phonos-test-out-")
        app.config["DATABASE_FILE"] = self.db_path
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["JOB_OUTPUT_DIR_FALLBACK"] = self.out_dir
        app.config["JOB_OUTPUT_DIR"] = self.out_dir
        app.config["MAIL_DEFAULT_SENDER"] = "noreply@testing.com"

        # submit_job_helper enqueues to Redis; there's no Redis here, so make
        # that step a no-op (the job is committed to the DB before enqueue).
        self._redis_patch = patch(
            "phonos_app.helpers.jobs.get_redis_connection",
            side_effect=RuntimeError("no redis in test"),
        )
        self._redis_patch.start()

        self.client = app.test_client()
        with app.app_context():
            init_db()
            self._seed()

    def tearDown(self):
        self._redis_patch.stop()
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil
        shutil.rmtree(self.out_dir, ignore_errors=True)

    # -- fixtures -----------------------------------------------------------
    def _seed(self):
        # NOTE: the core schema declares id columns as SERIAL, which under
        # SQLite does not auto-increment (id stays NULL), so we assign explicit
        # ids to keep foreign keys (FKs are enforced) resolvable.
        db = get_db()
        self.user_id = 1
        self.team_id = 1
        self.project_id = 1
        db.execute(
            "INSERT INTO users (id, user_id, username, credits) VALUES (%s, %s, %s, %s)",
            (self.user_id, "u-apitest", "apitester", 1000000),
        )
        db.execute(
            "INSERT INTO user_credentials (user_id, email, password) VALUES (%s, %s, %s)",
            (self.user_id, "apitester@example.com", b"x"),
        )
        db.execute(
            "INSERT INTO teams (id, name, slug, creator_id) VALUES (%s, %s, %s, %s)",
            (self.team_id, "Test Team", "test-team", self.user_id),
        )
        db.execute(
            "INSERT INTO projects (id, team_id, creator_id, name, description) "
            "VALUES (%s, %s, %s, %s, %s)",
            (self.project_id, self.team_id, self.user_id, "API Test Project", "for project api tests"),
        )
        db.execute(
            "INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, %s)",
            (self.project_id, self.user_id, "Project Manager"),
        )
        db.commit()

        # Functional key: bump the limit so the flow tests don't hit 10/min.
        keyinfo = create_project_api_key(self.user_id, self.project_id, "functional")
        self.api_key = keyinfo["api_key"]
        self.key_db_id = keyinfo["id"]
        db.execute(
            "UPDATE project_api_keys SET rate_limit_per_minute = 100000 WHERE id = %s",
            (self.key_db_id,),
        )
        db.commit()

    def _auth(self, key=None):
        return {"Authorization": f"Bearer {key or self.api_key}"}

    def _row(self, sql, params):
        with app.app_context():
            return get_db().execute(sql, params).fetchone()

    # -- auth ---------------------------------------------------------------
    def test_info_no_auth(self):
        r = self.client.get(f"{PREFIX}/info")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["rate_limit_per_minute"], 10)
        self.assertIn("encryption", body)

    def test_auth_rejections(self):
        self.assertEqual(self.client.get(f"{PREFIX}/project").status_code, 401)  # missing
        self.assertEqual(
            self.client.get(f"{PREFIX}/project", headers={"Authorization": "Bearer pj_nope"}).status_code,
            401,
        )
        # A non-project (public-style) token must not authenticate here.
        self.assertEqual(
            self.client.get(f"{PREFIX}/project", headers={"Authorization": "Bearer pk_something"}).status_code,
            401,
        )

    # -- project + models ---------------------------------------------------
    def test_project_and_models(self):
        r = self.client.get(f"{PREFIX}/project", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["project"]["id"], self.project_id)

        r = self.client.get(f"{PREFIX}/models", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        models = r.get_json()["models"]
        self.assertTrue(len(models) > 0)
        # Every model advertises its parameter schema.
        self.assertTrue(all("parameters" in m for m in models))
        esm = next((m for m in models if m["id"] == "type-0"), None)
        self.assertIsNotNone(esm)
        self.assertTrue(any(p["name"] == "num_recycles" for p in esm["parameters"]))

    def test_members(self):
        r = self.client.get(f"{PREFIX}/members", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        members = r.get_json()["members"]
        self.assertEqual(members[0]["email"], "apitester@example.com")

    # -- run job: input + params + at-rest encryption -----------------------
    def _submit_job(self, model="esmfold", sequence=TEST_SEQUENCE, params=TEST_PARAMS, name="job-1"):
        return self.client.post(
            f"{PREFIX}/jobs",
            headers=self._auth(),
            json={"model": model, "sequence": sequence, "params": params, "name": name},
        )

    def test_submit_validation(self):
        self.assertEqual(self._submit_job(model=None).status_code, 400)
        self.assertEqual(self._submit_job(sequence=None).status_code, 400)
        self.assertEqual(self._submit_job(model="not-a-real-model").status_code, 400)

    def test_run_job_encrypts_input_at_rest(self):
        r = self._submit_job()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        job = r.get_json()["job"]
        job_id = job["job_id"]
        self.assertTrue(job["input_encrypted"])
        self.assertEqual(job["model"], "type-0")

        # The job exists in the project, queued.
        jrow = self._row("SELECT status, project_id, sequence FROM jobs WHERE job_id = %s", (job_id,))
        self.assertEqual(jrow["status"], "queued")
        self.assertEqual(jrow["project_id"], self.project_id)

        # AT REST: enc_input is a pjenc ciphertext token that does NOT leak the
        # sequence or the secret param value.
        prow = self._row("SELECT enc_input FROM project_api_job_payloads WHERE job_uuid = %s", (job_id,))
        enc_input = prow["enc_input"]
        self.assertTrue(crypto.is_token(enc_input))
        self.assertNotIn(TEST_SEQUENCE, enc_input)
        self.assertNotIn("secret-payload-xyz", enc_input)

        # Job detail decrypts the input + params back exactly (round trip).
        r = self.client.get(f"{PREFIX}/jobs/{job_id}", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        decoded = r.get_json()["job"]["input"]
        self.assertEqual(decoded["sequence"], TEST_SEQUENCE)
        self.assertEqual(decoded["params"], TEST_PARAMS)
        self.assertEqual(decoded["model"], "type-0")

    def test_result_decrypts_and_caches_encrypted(self):
        job_id = self._submit_job().get_json()["job"]["job_id"]

        # Simulate a worker completing the job with a real output file.
        with app.app_context():
            ppath = get_safe_project_path(self.project_id, self.team_id)
            os.makedirs(ppath, exist_ok=True)
            out_path = os.path.join(ppath, f"{job_id}.pdb")
            pdb_text = "HEADER TEST\nATOM      1  N   ALA A   1      0.0 0.0 0.0\nEND\n"
            with open(out_path, "w") as fh:
                fh.write(pdb_text)
            db = get_db()
            db.execute(
                "UPDATE jobs SET status='completed', message=%s, output_file=%s, "
                "output_display_name=%s WHERE job_id=%s",
                (json.dumps({"plddt": 88.5}), out_path, "result.pdb", job_id),
            )
            db.commit()

        # First fetch builds + returns the result, and caches it encrypted.
        r = self.client.get(f"{PREFIX}/jobs/{job_id}/result", headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        result = r.get_json()["result"]
        self.assertEqual(result["metadata"], {"plddt": 88.5})
        self.assertIn("ATOM", result["output_content"])
        self.assertTrue(r.get_json()["encrypted_at_rest"])

        # The cached result is ciphertext at rest (no plaintext metadata leak).
        prow = self._row("SELECT enc_result FROM project_api_job_payloads WHERE job_uuid=%s", (job_id,))
        self.assertTrue(crypto.is_token(prow["enc_result"]))
        self.assertNotIn("88.5", prow["enc_result"])
        self.assertNotIn("ATOM", prow["enc_result"])

        # Second fetch is served from the encrypted cache, identical content.
        r2 = self.client.get(f"{PREFIX}/jobs/{job_id}/result", headers=self._auth())
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["result"]["metadata"], {"plddt": 88.5})

        # Raw artifact download works too.
        rd = self.client.get(f"{PREFIX}/jobs/{job_id}/output", headers=self._auth())
        self.assertEqual(rd.status_code, 200)
        self.assertIn(b"ATOM", rd.data)

    def test_status_cancel_delete(self):
        job_id = self._submit_job(name="cancel-me").get_json()["job"]["job_id"]

        r = self.client.get(f"{PREFIX}/jobs/{job_id}/status", headers=self._auth())
        self.assertEqual(r.get_json()["job_status"], "queued")

        r = self.client.post(f"{PREFIX}/jobs/{job_id}/cancel", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._row("SELECT status FROM jobs WHERE job_id=%s", (job_id,))["status"], "cancelled")

        r = self.client.delete(f"{PREFIX}/jobs/{job_id}", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self._row("SELECT id FROM jobs WHERE job_id=%s", (job_id,)))
        # Encrypted payload row is cleaned up as well.
        self.assertIsNone(self._row("SELECT id FROM project_api_job_payloads WHERE job_uuid=%s", (job_id,)))

    # -- dual-use isolation: wrong key cannot decrypt -----------------------
    def test_wrong_key_cannot_decrypt(self):
        job_id = self._submit_job().get_json()["job"]["job_id"]
        enc_input = self._row(
            "SELECT enc_input FROM project_api_job_payloads WHERE job_uuid=%s", (job_id,)
        )["enc_input"]

        # Make a second key (different salt => different data key).
        with app.app_context():
            other = create_project_api_key(self.user_id, self.project_id, "other")
            other_salt = self._row(
                "SELECT enc_salt FROM project_api_keys WHERE id=%s", (other["id"],)
            )["enc_salt"]
        wrong_dk = crypto.derive_data_key(other["api_key"], crypto.salt_from_text(other_salt))

        # Wrong data key fails to decrypt (GCM auth failure -> None).
        self.assertIsNone(crypto.decrypt_json(wrong_dk, enc_input))

    # -- project scoping ----------------------------------------------------
    def test_project_scoping(self):
        job_id = self._submit_job().get_json()["job"]["job_id"]
        with app.app_context():
            db = get_db()
            other_pid = 2
            db.execute(
                "INSERT INTO projects (id, team_id, creator_id, name) VALUES (%s,%s,%s,%s)",
                (other_pid, self.team_id, self.user_id, "Other Project"),
            )
            db.execute(
                "INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s,%s,%s)",
                (other_pid, self.user_id, "Project Manager"),
            )
            db.commit()
            other_key = create_project_api_key(self.user_id, other_pid, "scoped")["api_key"]

        # A key scoped to another project cannot read this job.
        r = self.client.get(f"{PREFIX}/jobs/{job_id}", headers=self._auth(other_key))
        self.assertEqual(r.status_code, 404)

    # -- rate limit ---------------------------------------------------------
    def test_rate_limit_10_per_minute(self):
        with app.app_context():
            limited = create_project_api_key(self.user_id, self.project_id, "limited")["api_key"]
        statuses = [
            self.client.get(f"{PREFIX}/project", headers=self._auth(limited)).status_code
            for _ in range(12)
        ]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)
        self.assertEqual(statuses[11], 429)

    # -- files --------------------------------------------------------------
    def test_files_upload_creates_row_and_saves_to_disk(self):
        # Upload through the endpoint; verify a row is created and the bytes
        # land on disk. (Under SQLite the SERIAL files.id is NULL, so we don't
        # rely on the returned id here - that path is covered below.)
        import config as _cfg
        with patch.object(_cfg.Config, "UPLOAD_FOLDER", self.out_dir):
            data = {"file": (io.BytesIO(b">seq\nMKLV\n"), "test.fasta"), "name": "my.fasta"}
            r = self.client.post(f"{PREFIX}/files/upload", headers=self._auth(),
                                 data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()["file"]
        self.assertEqual(body["name"], "my.fasta")
        self.assertEqual(body["type"], "sequence")
        # Row exists and the saved file contains our bytes.
        row = self._row("SELECT path FROM files WHERE project_id=%s", (self.project_id,))
        self.assertIsNotNone(row)
        with open(row["path"], "rb") as fh:
            self.assertIn(b"MKLV", fh.read())

    def test_files_list_download_delete_by_id(self):
        # Insert a file row with an explicit id (PG would auto-assign one) and
        # exercise list / download / delete against it.
        fid = 5001
        with app.app_context():
            ppath = get_safe_project_path(self.project_id, self.team_id)
            os.makedirs(ppath, exist_ok=True)
            fpath = os.path.join(ppath, "explicit.pdb")
            with open(fpath, "w") as fh:
                fh.write("ATOM FILE CONTENT")
            db = get_db()
            db.execute(
                "INSERT INTO files (id, project_id, creator_id, name, path, size) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (fid, self.project_id, self.user_id, "explicit.pdb", fpath, 17),
            )
            db.commit()

        r = self.client.get(f"{PREFIX}/files", headers=self._auth())
        self.assertTrue(any(f["id"] == fid for f in r.get_json()["files"]))

        r = self.client.get(f"{PREFIX}/files/{fid}/download", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"ATOM FILE CONTENT", r.data)

        r = self.client.delete(f"{PREFIX}/files/{fid}", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self._row("SELECT id FROM files WHERE id=%s", (fid,)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
