"""Comprehensive verification suite for Phonos Open-Source, Account/RBAC, BYOM, Copilot & Local Kernel upgrades."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
PROD_DIR = os.path.join(PROJECT_ROOT, "production")
if PROD_DIR not in sys.path:
    sys.path.insert(0, PROD_DIR)

os.environ.setdefault("ENCRYPTION_KEY", "a-secure-test-key-for-dev-use-32-chars")
os.environ.setdefault("SECRET_KEY", "a-secure-test-secret-key-for-phonos-32")

patch("redis.ConnectionPool").start()
patch("production.database.validate_database_config", return_value=True).start()

from production.config import Config  # noqa: E402
from production.database import init_db  # noqa: E402
from production.main import app  # noqa: E402
from phonos_app.helpers.database import get_db  # noqa: E402
from phonos_app.helpers.lab_accounts import (  # noqa: E402
    create_local_account,
    ensure_default_lab_team,
    ensure_lab_accounts_schema,
)
from phonos_app.helpers.kernel_manager import LocalPythonBackend  # noqa: E402


class _MockBYOMHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"status": "healthy", "model": "mock-fold-1"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        seq = str(payload.get("sequence") or (payload.get("input") or {}).get("sequence") or "ACDEFGHIK")
        pdb = (
            "HEADER    MOCK BYOM PREDICTION\n"
            "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 92.50           C\n"
            "ATOM      2  CA  CYS A   2       4.800   2.100   3.100  1.00 88.10           C\n"
            "END\n"
        )
        resp = {
            "pdb": pdb,
            "metrics": {"mean_plddt": 90.3, "sequence_length": len(seq)},
        }
        out = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class PhonosOSSImprovementsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="phonos_oss_test_")
        self.db_path = os.path.join(self.tmp_dir, "test_phonos.db")
        app.config["DATABASE_FILE"] = self.db_path
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        Config.ENABLE_2FA = False
        Config.LOCAL_LAB_MODE = True
        Config.ALLOW_PUBLIC_SIGNUP = True
        Config.JOB_OUTPUT_DIR = os.path.join(self.tmp_dir, "job_outputs")
        os.makedirs(Config.JOB_OUTPUT_DIR, exist_ok=True)
        app.config["JOB_OUTPUT_DIR"] = Config.JOB_OUTPUT_DIR

        self.client = app.test_client()
        with app.app_context():
            init_db()
            ensure_lab_accounts_schema(get_db())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _csrf_headers(self) -> dict[str, str]:
        with self.client.session_transaction() as sess:
            tok = sess.get("csrf_token")
            if not tok:
                tok = "test-csrf-token-123456"
                sess["csrf_token"] = tok
        return {"X-CSRF-Token": tok}

    def _login_as(self, user_id: int, username: str, is_admin: bool = False, role: str = "scientist") -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = username
            sess["is_admin"] = is_admin
            sess["system_role"] = role

    def test_health_and_first_run_setup_wizard(self) -> None:
        r_health = self.client.get("/api/health")
        self.assertEqual(r_health.status_code, 200)
        self.assertTrue(r_health.get_json()["ok"])

        st = self.client.get("/api/accounts/setup-status").get_json()
        self.assertTrue(st["needs_setup"])

        r_setup = self.client.post(
            "/api/accounts/setup",
            json={
                "username": "labadmin",
                "password": "QuantumBioSetup!99",
                "lab_name": "Open Bio Lab",
                "allow_public_signup": False,
            },
        )
        self.assertEqual(r_setup.status_code, 201, r_setup.get_data(as_text=True))
        body = r_setup.get_json()
        self.assertEqual(body["account"]["system_role"], "admin")

        # Second call to /api/accounts/setup must be blocked with 409
        r_dup = self.client.post(
            "/api/accounts/setup",
            json={"username": "other", "password": "OtherStrongPassword!99"},
        )
        self.assertEqual(r_dup.status_code, 409)

    def test_2fa_dormant_direct_login_and_role_integration(self) -> None:
        with app.app_context():
            db = get_db()
            adm = create_local_account(db, username="admin1", password="QuantumAdminKey!11", system_role="admin")
            ensure_default_lab_team(db, adm["id"], lab_name="Core Lab")
            sci = create_local_account(db, username="sci1", password="HelixFoldPass!22", system_role="scientist")
            op = create_local_account(db, username="op1", password="DockingRunPass!33", system_role="operator")
            vw = create_local_account(db, username="view1", password="ReadOnlyGlass!44", system_role="viewer")
            db.commit()

        # Login with plain username + password goes directly to dashboard (no /verify_2fa redirect)
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "login-csrf-tok"
        resp = self.client.post(
            "/login",
            data={"username": "sci1", "password": "HelixFoldPass!22", "csrf_token": "login-csrf-tok"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("verify_2fa", resp.headers.get("Location", ""))

        # Scientist creates a private project
        self._login_as(sci["id"], "sci1", is_admin=False, role="scientist")
        r_proj = self.client.post(
            "/api/projects",
            headers=self._csrf_headers(),
            json={"name": "Sci Secret Target", "description": "Kinase study"},
        )
        self.assertEqual(r_proj.status_code, 201, r_proj.get_data(as_text=True))

        # Operator cannot create top-level projects
        self._login_as(op["id"], "op1", is_admin=False, role="operator")
        r_op_proj = self.client.post(
            "/api/projects",
            headers=self._csrf_headers(),
            json={"name": "Should Fail"},
        )
        self.assertEqual(r_op_proj.status_code, 403)

        # Viewer cannot create projects
        self._login_as(vw["id"], "view1", is_admin=False, role="viewer")
        r_vw_proj = self.client.post(
            "/api/projects",
            headers=self._csrf_headers(),
            json={"name": "Should Also Fail"},
        )
        self.assertEqual(r_vw_proj.status_code, 403)

        # Admin sees clean personal list by default, and ALL lab projects with ?scope=all_lab
        self._login_as(adm["id"], "admin1", is_admin=True, role="admin")
        mine = self.client.get("/api/projects").get_json()
        all_lab = self.client.get("/api/projects?scope=all_lab").get_json()
        mine_names = [p["name"] for p in (mine if isinstance(mine, list) else mine.get("projects", []))]
        all_names = [p["name"] for p in (all_lab if isinstance(all_lab, list) else all_lab.get("projects", []))]
        self.assertNotIn("Sci Secret Target", mine_names)
        self.assertIn("Sci Secret Target", all_names)

        # Last-admin protection blocks deleting admin1
        r_del_last = self.client.delete(
            f"/api/accounts/{adm['id']}",
            headers=self._csrf_headers(),
        )
        self.assertEqual(r_del_last.status_code, 409)

    def test_provider_vault_and_live_byom_direct_http_execution(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _MockBYOMHandler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with app.app_context():
                db = get_db()
                sci = create_local_account(db, username="builder", password="QuantumVaultKey!99", system_role="admin")
                db.commit()

            self._login_as(sci["id"], "builder", is_admin=True, role="admin")

            # 1. Verify Admin-only 3-provider vault (NVIDIA, Wolfram, IBM Cloud)
            with app.app_context():
                db = get_db()
                non_adm = create_local_account(db, username="sci_no_vault", password="QuantumVaultKey!88", system_role="scientist")
                db.commit()
            self._login_as(non_adm["id"], "sci_no_vault", is_admin=False, role="scientist")
            r_forbid = self.client.post(
                "/api/model-providers/nvidia-nim",
                headers=self._csrf_headers(),
                json={"api_key": "nvapi-should-fail"},
            )
            self.assertEqual(r_forbid.status_code, 403)

            self._login_as(sci["id"], "builder", is_admin=True, role="admin")
            r_bad_prov = self.client.post(
                "/api/model-providers/openai",
                headers=self._csrf_headers(),
                json={"api_key": "sk-unsupported"},
            )
            self.assertEqual(r_bad_prov.status_code, 400)

            r_vault = self.client.post(
                "/api/model-providers/nvidia-nim",
                headers=self._csrf_headers(),
                json={"api_key": "nvapi-test-encrypted-vault-key-123456"},
            )
            self.assertEqual(r_vault.status_code, 200, r_vault.get_data(as_text=True))
            r_wolf = self.client.post(
                "/api/model-providers/wolfram",
                headers=self._csrf_headers(),
                json={"api_key": "wolf-test-license-key-999", "base_url": "http://127.0.0.1:5100"},
            )
            self.assertEqual(r_wolf.status_code, 200, r_wolf.get_data(as_text=True))
            r_ibm = self.client.post(
                "/api/model-providers/ibm-cloud",
                headers=self._csrf_headers(),
                json={"api_key": "ibm-test-cloud-key-777", "instance_crn": "crn:v1:bluemix:public:quantum-computing:us-east:a/test::"},
            )
            self.assertEqual(r_ibm.status_code, 200, r_ibm.get_data(as_text=True))

            r_list_vault = self.client.get("/api/model-providers")
            self.assertEqual(r_list_vault.status_code, 200)
            prov_list = r_list_vault.get_json()["providers"]
            self.assertEqual([p["id"] for p in prov_list], ["nvidia-nim", "wolfram", "ibm-cloud"])
            providers = {p["id"]: p for p in prov_list}
            self.assertTrue(providers["nvidia-nim"]["has_key"])
            self.assertEqual(providers["nvidia-nim"]["key_source"], "instance")
            self.assertTrue(providers["wolfram"]["has_key"])
            self.assertEqual(providers["wolfram"]["key_source"], "instance")
            self.assertTrue(providers["ibm-cloud"]["has_key"])
            self.assertEqual(providers["ibm-cloud"]["key_source"], "instance")
            self.assertEqual(providers["ibm-cloud"]["extra"].get("instance_crn"), "crn:v1:bluemix:public:quantum-computing:us-east:a/test::")
            # Clear IBM Cloud key and confirm it returns cleanly to none
            r_clear_ibm = self.client.post(
                "/api/model-providers/ibm-cloud",
                headers=self._csrf_headers(),
                json={"clear_key": True},
            )
            self.assertEqual(r_clear_ibm.status_code, 200)
            after_clear = {p["id"]: p for p in r_clear_ibm.get_json()["providers"]}
            self.assertFalse(after_clear["ibm-cloud"]["has_key"])
            self.assertEqual(after_clear["ibm-cloud"]["key_source"], "none")
            # Ensure plaintext keys are never leaked back
            raw_body = r_list_vault.get_data(as_text=True)
            self.assertNotIn("nvapi-test-encrypted-vault-key-123456", raw_body)
            self.assertNotIn("wolf-test-license-key-999", raw_body)
            self.assertNotIn("ibm-test-cloud-key-777", raw_body)

            # 2. Register a direct_http BYOM custom model pointing to our mock HTTP server
            r_create_model = self.client.post(
                "/api/user-models",
                headers=self._csrf_headers(),
                json={
                    "model_name": "Local MockFold v1",
                    "model_description": "Local HTTP folding microservice",
                    "input_schema": [{"name": "sequence", "type": "sequence", "required": True}],
                    "worker_endpoint_url": f"http://127.0.0.1:{port}/predict",
                    "endpoint_mode": "direct_http",
                    "auth_type": "none",
                    "output_format": "pdb",
                },
            )
            self.assertEqual(r_create_model.status_code, 201, r_create_model.get_data(as_text=True))
            model_id = r_create_model.get_json()["model"]["id"]

            # 3. Live test-endpoint probe
            r_test_ep = self.client.post(
                f"/api/user-models/{model_id}/test-endpoint",
                headers=self._csrf_headers(),
                json={"sample_input": {"sequence": "MVLSPADKTNVK"}},
            )
            self.assertEqual(r_test_ep.status_code, 200, r_test_ep.get_data(as_text=True))
            self.assertTrue(r_test_ep.get_json()["ok"])

            # 4. Create project & execute full BYOM job end-to-end
            r_proj = self.client.post(
                "/api/projects",
                headers=self._csrf_headers(),
                json={"name": "BYOM Validation Project"},
            )
            self.assertEqual(r_proj.status_code, 201, r_proj.get_data(as_text=True))
            proj_raw = r_proj.get_json()
            proj_id = proj_raw["project"]["id"] if "project" in proj_raw else proj_raw["id"]

            r_run = self.client.post(
                "/api/jobs/submit",
                headers=self._csrf_headers(),
                json={
                    "model_id": model_id,
                    "project_id": proj_id,
                    "job_name": "Test Fold Run",
                    "input": {"sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSH"},
                },
            )
            self.assertEqual(r_run.status_code, 201, r_run.get_data(as_text=True))
            job_id = r_run.get_json()["job_id"]

            # Poll async completion (local HTTP completes in <100ms)
            final_status = None
            output_filename = None
            for _ in range(30):
                res_poll = self.client.get(f"/api/user-model-jobs/{job_id}/result")
                if res_poll.status_code == 200:
                    jdata = res_poll.get_json().get("job") or {}
                    st_val = jdata.get("job_status") or jdata.get("status")
                    if st_val in ("completed", "failed"):
                        final_status = st_val
                        output_filename = jdata.get("output_file")
                        break
                time.sleep(0.1)
            self.assertEqual(final_status, "completed")
            self.assertTrue(output_filename and output_filename.endswith(".pdb"))
            self.assertTrue(os.path.isfile(os.path.join(Config.JOB_OUTPUT_DIR, output_filename)))
        finally:
            server.shutdown()
            server.server_close()

    def test_copilot_discovery_tools_and_keyless_local_provider(self) -> None:
        from phonos_app.agent.tools import ToolContext
        from phonos_app.agent.bio_tools import (
            admet_lipinski_profile,
            design_mutation_scan,
            structure_confidence_plddt,
        )
        from phonos_app.agent import viewer_context

        with app.app_context():
            db = get_db()
            u = create_local_account(db, username="copilotuser", password="DiscoveryWave!88", system_role="scientist")
            db.commit()

            ctx = ToolContext(db=db, user_id=u["id"])

            # ADMET / Lipinski / QED on Aspirin
            admet = admet_lipinski_profile(ctx, {"smiles": "CC(=O)Oc1ccccc1C(=O)O"})
            self.assertEqual(len(admet["molecules"]), 1)
            self.assertTrue(admet["molecules"][0]["lipinski_pass"])
            self.assertGreater(admet["molecules"][0]["qed"], 0.4)

            # Structure pLDDT confidence analysis via live viewer context
            sample_pdb = (
                "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 94.20           C\n"
                "ATOM      2  CA  GLY A   2       3.800   0.000   0.000  1.00 58.00           C\n"
                "ATOM      3  CA  SER A   3       7.600   0.000   0.000  1.00 55.00           C\n"
                "ATOM      4  CA  THR A   4      11.400   0.000   0.000  1.00 52.00           C\n"
                "END\n"
            )
            viewer_context.put(u["id"], {"name": "test_fold.pdb", "format": "pdb", "structure_text": sample_pdb})
            conf = structure_confidence_plddt(ctx, {"use_viewer": True})
            self.assertEqual(conf["total_residues"], 4)
            self.assertGreaterEqual(len(conf["low_confidence_segments"]), 1)

            # Saturation mutagenesis scan generator
            mut = design_mutation_scan(ctx, {"sequence": "ACDEFGHIKLMNPQRSTVWY", "start_pos": 1, "end_pos": 4, "strategy": "solubility"})
            self.assertGreater(mut["count"], 5)
            self.assertIn(">A1K", mut["fasta"])

        self._login_as(u["id"], "copilotuser", role="scientist")
        # Copilot routes are unmounted (404) by default unless ENABLE_COPILOT=true
        self.assertEqual(self.client.get("/agent").status_code, 404)
        self.assertEqual(self.client.get("/api/agent/routines").status_code, 404)

    def test_zero_docker_local_notebook_kernel(self) -> None:
        backend = LocalPythonBackend()
        info = backend.start_container(
            session_id="testsession123456",
            workspace_path=self.tmp_dir,
            volumes={},
            env_vars={},
            labels={},
            mem_limit="512m",
            cpu_quota=1.0,
        )
        cid = info["id"]
        url = info["service_url"]
        token = info["auth_token"]

        def _post_kernel(path: str, body: dict) -> dict:
            req = urllib.request.Request(
                url + path,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Phonos-Kernel-Token": token},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            res1 = _post_kernel("/execute", {"code": "x = 21 * 2\nprint('answer =', x)", "timeout": 15})
            self.assertEqual(res1["status"], "ok", res1)
            out1_txt = "".join(o.get("text", "") for o in res1.get("outputs", []))
            self.assertIn("answer = 42", out1_txt)

            # Stateful persistence + RDKit show_smiles rich HTML/SVG output
            res2 = _post_kernel("/execute", {"code": "print('still', x)\nshow_smiles('c1ccccc1')", "timeout": 15})
            self.assertEqual(res2["status"], "ok", res2)
            out2_txt = "".join(o.get("text", "") for o in res2.get("outputs", []))
            self.assertIn("still 42", out2_txt)
            self.assertTrue(any(o.get("output_type") in ("display_data", "execute_result") for o in res2.get("outputs", [])))

            # Reset clears state cleanly
            rst = _post_kernel("/reset", {})
            self.assertTrue(rst["ok"])
            res3 = _post_kernel("/execute", {"code": "print('x' in globals())", "timeout": 10})
            out3_txt = "".join(o.get("text", "") for o in res3.get("outputs", []))
            self.assertIn("False", out3_txt)
        finally:
            backend.stop_container(cid)


if __name__ == "__main__":
    unittest.main()
