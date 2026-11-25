"""Tests for the education auto-grading security hardening in helpers/education.py.

Covers:
  - Wolfram formula grading no longer interpolates raw student/teacher text
    into generated WL source (injection fix)
  - Student code sandbox: secrets are not exposed via os.environ, a
    server-side timeout ceiling is enforced regardless of teacher config,
    and process-spawning is blocked (fork-bomb defense)
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')

patch('production.config.Config.DEV_MODE', False).start()
patch('redis.ConnectionPool').start()
patch('production.database.validate_database_config', return_value=True).start()

from production.main import app  # noqa: E402  (side-effecting import, sets up sys.path)
from phonos_app.helpers.wolfram_service import wolfram_string_literal  # noqa: E402
from phonos_app.helpers import education as edu  # noqa: E402


class WolframStringLiteralTests(unittest.TestCase):
    def test_round_trips_plain_text(self):
        lit = wolfram_string_literal('x^2 + 1')
        self.assertTrue(lit.startswith('ByteArrayToString[BaseDecode["'))
        self.assertTrue(lit.endswith('"]]'))

    def test_no_quote_or_backslash_leaks_into_source(self):
        payload = '"], Print[secret]; ToExpression["'
        lit = wolfram_string_literal(payload)
        inner = lit.split('"')[1]
        self.assertNotIn('"', inner)
        self.assertNotIn('\\', inner)

    def test_trailing_backslash_cannot_escape_closing_quote(self):
        # This exact shape is what broke naive quote-stripping: a trailing
        # backslash would escape the literal's closing quote in the old code.
        payload = 'anything\\'
        lit = wolfram_string_literal(payload)
        # The whole thing must remain a single well-formed ByteArrayToString[...] call.
        self.assertEqual(lit.count('"'), 2)


class GradeFormulaInjectionTests(unittest.TestCase):
    def test_grade_formula_never_interpolates_raw_answers_into_wl_source(self):
        captured = {}

        def fake_call(code, user_id, db, timeout=30):
            captured['code'] = code
            return {'result': 'True'}

        config = {'correct_answer_enc': edu.encrypt_answer('x^2+1')}
        malicious_answer = '"], Print[ToExpression["x^2+1"]]; x&["'

        with patch('phonos_app.helpers.wolfram_service.call_wolfram_service', side_effect=fake_call):
            score, feedback, correct = edu.grade_formula(malicious_answer, config, user_id=1, db=None)

        code = captured['code']
        # The raw malicious payload must never appear verbatim in the generated
        # Wolfram source -- it must only appear base64-encoded inside BaseDecode.
        self.assertNotIn(malicious_answer, code)
        self.assertIn('BaseDecode', code)
        self.assertIn('ByteArrayToString', code)


class SandboxHardeningTests(unittest.TestCase):
    def test_legit_code_still_runs(self):
        ok, out, err = edu._run_python_sandbox('print(2 + 2)', 10)
        self.assertTrue(ok, err)
        self.assertEqual(out.strip(), '4')

    def test_app_secrets_not_visible_to_student_code(self):
        os.environ['EDU_TEST_SECRET'] = 'super-secret-value'
        try:
            ok, out, err = edu._run_python_sandbox(
                'import os; print(os.environ.get("EDU_TEST_SECRET"))', 10
            )
        finally:
            del os.environ['EDU_TEST_SECRET']
        self.assertNotIn('super-secret-value', out)

    def test_timeout_ceiling_enforced_regardless_of_requested_value(self):
        t0 = time.time()
        ok, out, err = edu._run_python_sandbox('while True: pass', 999999)
        elapsed = time.time() - t0
        self.assertFalse(ok)
        self.assertLess(elapsed, edu._SANDBOX_MAX_TIMEOUT_SEC + 10)

    def test_process_spawning_is_blocked(self):
        code = (
            'import os\n'
            'try:\n'
            '    os.fork()\n'
            '    print("FORK OK")\n'
            'except OSError:\n'
            '    print("FORK BLOCKED")\n'
        )
        ok, out, err = edu._run_python_sandbox(code, 10)
        self.assertNotIn('FORK OK', out)


if __name__ == '__main__':
    unittest.main()
