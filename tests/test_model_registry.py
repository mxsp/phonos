"""Model registry tests (roadmap task 19, step 2: truth about models and credits).

Config-level checks need no app. The route checks use the same throw-away
SQLite setup as test_access_control.py.

    venv/bin/python -m pytest tests/test_model_registry.py -q
"""
from __future__ import annotations

import os
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

from production.config import Config  # noqa: E402

MODELS = Config.SUPPORTED_MODELS
BY_ID = {m["id"]: m for m in MODELS}


class ConfigRegistryTests(unittest.TestCase):
    def test_every_model_has_explicit_is_active(self):
        for m in MODELS:
            self.assertIn("is_active", m, m["id"])
            self.assertIsInstance(m["is_active"], bool, m["id"])

    def test_retired_models_are_complete(self):
        retired = [m for m in MODELS if m.get("retired")]
        self.assertTrue(retired)
        for m in retired:
            self.assertIs(m["is_active"], False, m["id"])
            self.assertTrue(m.get("offline_reason"), m["id"])  # processors.py _retired_reason needs this
            self.assertIn(m.get("replacement"), BY_ID, m["id"])
            self.assertFalse(BY_ID[m["replacement"]].get("retired"), m["id"])

    def test_nvidia_retired_endpoints(self):
        for mid in ("type-0", "type-2", "esmfold-re", "alphafold2-re", "alphafold2-re-multimer"):
            self.assertTrue(BY_ID[mid].get("retired"), mid)
            self.assertIs(BY_ID[mid]["is_active"], False, mid)

    def test_alias_points_at_canonical_model(self):
        self.assertEqual(BY_ID["type-8"]["alias_of"], "evo2")
        self.assertIn("evo2", BY_ID)

    def test_hosted_by_and_credit_cost_everywhere(self):
        for m in MODELS:
            hb = m.get("hosted_by")
            self.assertTrue(hb and hb.get("provider") and hb.get("region"), m["id"])
            self.assertIsInstance(m.get("credit_cost"), int, m["id"])
            self.assertEqual(Config.calculate_adaptive_credit_cost(m["id"]), m["credit_cost"], m["id"])

    def test_ui_block_everywhere(self):
        for m in MODELS:
            ui = m.get("ui") or {}
            for key in ("short_name", "abbr", "color", "glyph"):
                self.assertTrue(ui.get(key), f"{m['id']}.{key}")

    def test_nim_models_run_at_nvidia_usa_by_default(self):
        hb = BY_ID["type-9"]["hosted_by"]
        self.assertEqual((hb["provider"], hb["region"]), ("NVIDIA NIM", "USA"))

    def test_public_view_states(self):
        self.assertEqual(Config.public_model_view(BY_ID["type-2"])["ui_state"], "retired")
        self.assertEqual(Config.public_model_view(BY_ID["type-9"])["ui_state"], "active")
        self.assertEqual(Config.public_model_view(BY_ID["msa-search"])["ui_state"], "offline")
        self.assertTrue(Config.public_model_view(BY_ID["msa-search"])["offline_reason"])
        self.assertTrue(Config.public_model_view(BY_ID["proteinmpnn-re"])["degraded"])

    def test_landing_table_lists_only_live_models(self):
        ids = {r["id"] for r in Config.public_model_table()}
        self.assertTrue(ids)
        for mid in ids:
            self.assertTrue(BY_ID[mid]["is_active"] and not BY_ID[mid].get("retired"), mid)

    def test_hosting_is_configuration(self):
        import importlib
        with patch.dict(os.environ, {"PHONOS_DEFAULT_HOSTED_PROVIDER": "AWS", "PHONOS_DEFAULT_HOSTED_REGION": "eu-central-1"}):
            import production.config as cfg
            cfg = importlib.reload(cfg)
            hb = {m["id"]: m for m in cfg.Config.SUPPORTED_MODELS}["type-9"]["hosted_by"]
            self.assertEqual((hb["provider"], hb["region"]), ("AWS", "eu-central-1"))
        importlib.reload(cfg)

    def test_mrs_still_fails_retired_jobs(self):
        # Mirrors production/mrs_service/processors.py::_retired_reason.
        for m in MODELS:
            blocked = m.get("is_active") is False and bool(m.get("offline_reason"))
            self.assertEqual(blocked, bool(m.get("retired")), m["id"])


from production.main import app  # noqa: E402
from production.database import init_db  # noqa: E402
from phonos_app.helpers.database import get_db  # noqa: E402


class RegistryRouteTests(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        app.config["DATABASE_FILE"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()
        with app.app_context():
            init_db()
            db = get_db()
            db.execute("INSERT INTO users (id, user_id, username, credits, is_admin) VALUES (1,'u-a','a',1000,0)")
            db.commit()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def login(self):
        with self.client.session_transaction() as s:
            s.clear()
            s["user_id"] = 1

    def test_available_models_shape(self):
        self.login()
        r = self.client.get("/api/available_models")
        self.assertEqual(r.status_code, 200)
        models = {m["id"]: m for m in r.get_json()}
        self.assertIn("type-9", models)
        for m in models.values():
            for key in ("ui_state", "offline_reason", "replacement", "hosted_by", "credit_cost", "ui"):
                self.assertIn(key, m, m["id"])
            self.assertIn(m["ui_state"], ("active", "offline", "retired"))
        self.assertEqual(models["type-2"]["ui_state"], "retired")
        self.assertEqual(models["type-2"]["replacement"], "openfold3")
        self.assertEqual(models["esmfold-re"]["ui_state"], "retired")

    def test_estimate_requires_login(self):
        r = self.client.get("/api/models/type-9/estimate")
        self.assertIn(r.status_code, (302, 401, 403))

    def test_estimate_returns_cost_and_balance(self):
        self.login()
        r = self.client.post("/api/models/type-9/estimate", json={"sequence": "MKT" * 10})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["credit_cost"], Config.calculate_adaptive_credit_cost("type-9"))
        self.assertEqual(d["balance"], 1000)
        self.assertEqual(d["balance_after"], 1000 - d["credit_cost"])
        self.assertTrue(d["sufficient"])
        self.assertEqual(d["hosted_by"]["provider"], Config.DEFAULT_HOSTED_BY["provider"])

    def test_estimate_unknown_model(self):
        self.login()
        self.assertEqual(self.client.get("/api/models/nope/estimate").status_code, 404)

    def test_version_endpoint_matches_package(self):
        import phonos_app
        r = self.client.get("/api/version")
        self.assertEqual(r.get_json()["version"], phonos_app.__version__)


if __name__ == "__main__":
    unittest.main()
