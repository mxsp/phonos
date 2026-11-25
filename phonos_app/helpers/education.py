"""Education assignment grading helpers.

Pure functions that take a student answer + a problem's grading config and
return ``(score, feedback, is_correct)``. Used by ``api_education`` during
auto-grading. Manual-only types (text/file) return ``None`` for the score so
the caller knows to leave it for the teacher.

Correct answers are stored AES-encrypted (base64-wrapped) in the problem
config under ``correct_answer_enc``; they are decrypted here only at grade
time and never sent to the frontend.
"""
from __future__ import annotations

import base64
import json
import logging
import resource
import shutil
import subprocess
import sys
import tempfile
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Hard ceilings for student-submitted code, enforced regardless of what a
# teacher configures on the problem (config['timeout_sec'] etc). This is
# defense-in-depth only -- it restricts CPU/memory/process-count and strips
# the app's environment (secrets) but does NOT provide filesystem or network
# isolation, since that requires OS-level containment (namespaces/containers)
# rather than rlimits. A fully isolated worker (e.g. reusing the HPC
# pull-worker queue in api_hpc.py with a hardened, network-disabled container)
# is the tracked follow-up for true sandboxing.
_SANDBOX_MAX_TIMEOUT_SEC = 20
_SANDBOX_MAX_MEMORY_BYTES = 256 * 1024 * 1024
_SANDBOX_MAX_OUTPUT_CHARS = 64 * 1024
_SANDBOX_ENV_ALLOWLIST = ('PATH', 'LANG', 'LC_ALL')

# Re-exported so callers don't need to know the binary<->b64 dance.
try:
    from encryption_util import encrypt_string, decrypt_to_string
except ImportError:  # pragma: no cover
    encrypt_string = None
    decrypt_to_string = None


# ---------------------------------------------------------------------------
# Encryption helpers (base64-wrapped so values can live in JSONB)
# ---------------------------------------------------------------------------

def encrypt_answer(plaintext: str) -> Optional[str]:
    """AES-encrypt a correct-answer string and return base64 for JSON storage."""
    if encrypt_string is None or plaintext is None:
        return None
    enc = encrypt_string(plaintext)
    if enc is None:
        return None
    return base64.b64encode(enc).decode('ascii')


def decrypt_answer(enc_b64: str) -> Optional[str]:
    """Reverse of :func:`encrypt_answer`."""
    if decrypt_to_string is None or not enc_b64:
        return None
    try:
        raw = base64.b64decode(enc_b64)
    except Exception:
        return None
    return decrypt_to_string(raw)


# ---------------------------------------------------------------------------
# Per-type graders
# ---------------------------------------------------------------------------

def grade_numeric(answer: str, config: dict) -> Tuple[Optional[float], str, Optional[bool]]:
    """Grade a numeric answer against ``correct_answer_enc`` ± tolerance."""
    correct_enc = config.get('correct_answer_enc')
    correct = decrypt_answer(correct_enc) if correct_enc else None
    if correct is None:
        return None, 'No correct answer is configured for this problem.', None
    try:
        expected = float(correct)
    except (TypeError, ValueError):
        return 0.0, 'The stored correct answer is not a valid number.', False
    try:
        given = float(str(answer).strip())
    except (TypeError, ValueError):
        return 0.0, 'Your answer is not a valid number.', False
    tol = float(config.get('tolerance', 1e-6))
    rel = config.get('relative_tolerance', False)
    if rel:
        denom = abs(expected) if expected != 0 else 1.0
        ok = abs(given - expected) <= tol * denom
    else:
        ok = abs(given - expected) <= tol
    points = float(config.get('points', 1))
    if ok:
        return points, 'Correct.', True
    return 0.0, f'Incorrect (expected ≈ {expected}).', False


def grade_mc(answer, config: dict) -> Tuple[Optional[float], str, Optional[bool]]:
    """Grade a multiple-choice answer (index or value) against the encrypted index."""
    correct_enc = config.get('correct_index_enc')
    correct_idx_raw = decrypt_answer(correct_enc) if correct_enc else None
    if correct_idx_raw is None:
        return None, 'No correct answer is configured for this problem.', None
    try:
        correct_idx = int(correct_idx_raw)
    except (TypeError, ValueError):
        return 0.0, 'The stored correct answer is malformed.', False

    options = config.get('options') or []
    given_idx = None
    if isinstance(answer, int):
        given_idx = answer
    elif isinstance(answer, str):
        s = answer.strip()
        if s.lstrip('-').isdigit():
            try:
                given_idx = int(s)
            except ValueError:
                given_idx = None
        else:
            # Match by option value
            for i, opt in enumerate(options):
                if str(opt) == s:
                    given_idx = i
                    break
    if given_idx is None:
        return 0.0, 'Please select one of the provided options.', False

    points = float(config.get('points', 1))
    if given_idx == correct_idx:
        return points, 'Correct.', True
    return 0.0, 'Incorrect.', False


def _normalise_formula(expr: str) -> str:
    """Light normalisation for fallback string comparison."""
    if expr is None:
        return ''
    import re
    e = str(expr).strip().lower()
    e = e.replace(' ', '')
    e = re.sub(r'(\d)\(', r'\1*(', e)  # 2( -> 2*(
    return e


def grade_formula(answer: str, config: dict, user_id: int, db) -> Tuple[Optional[float], str, Optional[bool]]:
    """Grade a formula/expression answer.

    Tries symbolic equivalence via the Wolfram service when available; falls
    back to a normalised string compare.
    """
    correct_enc = config.get('correct_answer_enc')
    correct = decrypt_answer(correct_enc) if correct_enc else None
    if correct is None:
        return None, 'No correct answer is configured for this problem.', None

    points = float(config.get('points', 1))
    given = str(answer).strip()
    if not given:
        return 0.0, 'Empty answer.', False

    # Try Wolfram symbolic equivalence (best-effort; never hard-fail on service
    # errors — fall back to string compare so grading is still possible offline).
    try:
        from phonos_app.helpers.wolfram_service import call_wolfram_service, wolfram_string_literal
        code = (
            f'Equivalent[ToExpression[{wolfram_string_literal(given)}], '
            f'ToExpression[{wolfram_string_literal(correct)}]] // ToString'
        )
        result = call_wolfram_service(code, user_id, db, timeout=30)
        out = str(result.get('result', '')).strip()
        # Wolfram returns "True" when the expressions are equivalent.
        if out.lower() == 'true':
            return points, 'Correct.', True
        if out.lower() == 'false':
            return 0.0, 'Incorrect expression.', False
        # Ambiguous result → fall through to string compare
    except Exception as e:  # noqa: BLE001 - grading must never crash on service issues
        logger.debug('Wolfram formula grading unavailable: %s', e)

    # Fallback: normalised string compare
    if _normalise_formula(given) == _normalise_formula(correct):
        return points, 'Correct.', True
    return 0.0, 'Incorrect expression.', False


def grade_code(answer: str, config: dict) -> Tuple[Optional[float], str, Optional[bool]]:
    """Grade a code answer by running student code + hidden test cases.

    ``config.test_cases`` is a list of dicts ``{name, code, expected?}``. The
    student's code is prepended to each test case's code and executed in a
    sandboxed subprocess with a hard timeout. A test passes if:
      - it runs without raising, AND
      - either ``expected`` is unset (assert-based tests) OR stdout matches
        ``expected`` (after strip).
    """
    test_cases = config.get('test_cases') or []
    if not test_cases:
        return None, 'No test cases are configured for this problem.', None

    student_code = str(answer or '')
    timeout = int(config.get('timeout_sec', 10))
    total = len(test_cases)
    passed = 0
    failures = []

    for i, tc in enumerate(test_cases):
        tc_code = str(tc.get('code', ''))
        expected = tc.get('expected')
        name = tc.get('name') or f'Test {i + 1}'
        full_code = student_code + '\n\n' + tc_code
        ok, got_stdout, err = _run_python_sandbox(full_code, timeout)
        if ok and expected is not None and str(got_stdout).strip() != str(expected).strip():
            ok = False
            err = f'Expected output {expected!r}, got {got_stdout!r}'
        if ok:
            passed += 1
        else:
            failures.append(f'{name}: {err}')

    points = float(config.get('points', 1))
    score = points * (passed / total) if total else 0.0
    if passed == total:
        return points, f'All {total} tests passed.', True
    fb = f'{passed}/{total} tests passed.' + (' ' + '; '.join(failures[:3]) if failures else '')
    return score, fb, False


def _apply_sandbox_rlimits():
    """``preexec_fn`` for the student subprocess: cap CPU, memory and process count.

    Runs in the forked child before exec. RLIMIT_NPROC is a per-uid limit, but
    setting it low here only constrains *this* child (and anything it tries to
    fork) -- the parent app's own processes are unaffected since their rlimits
    are set independently.
    """
    for res, limit in (
        (resource.RLIMIT_CPU, _SANDBOX_MAX_TIMEOUT_SEC),
        (resource.RLIMIT_AS, _SANDBOX_MAX_MEMORY_BYTES),
        (resource.RLIMIT_NPROC, 1),
        (resource.RLIMIT_FSIZE, 1024 * 1024),
    ):
        try:
            resource.setrlimit(res, (limit, limit))
        except (ValueError, OSError):
            pass


def _run_python_sandbox(code: str, timeout: int) -> Tuple[bool, str, str]:
    """Run ``code`` in a throwaway, resource-limited subprocess; return (ok, stdout, error).

    See the module-level ``_SANDBOX_*`` comment for what is and isn't covered.
    """
    timeout = max(1, min(int(timeout), _SANDBOX_MAX_TIMEOUT_SEC))
    tmpdir = tempfile.mkdtemp(prefix='edu_grade_')
    path = os.path.join(tmpdir, 'submission.py')
    with open(path, 'w') as f:
        f.write(code)
    safe_env = {k: os.environ[k] for k in _SANDBOX_ENV_ALLOWLIST if k in os.environ}
    safe_env['HOME'] = tmpdir
    try:
        proc = subprocess.run(
            [sys.executable, '-I', path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=safe_env,
            cwd=tmpdir,
            preexec_fn=_apply_sandbox_rlimits if os.name == 'posix' else None,
        )
        stdout = (proc.stdout or '')[:_SANDBOX_MAX_OUTPUT_CHARS]
        if proc.returncode == 0:
            return True, stdout, ''
        err = (proc.stderr or '').strip().splitlines()
        return False, stdout, err[-1] if err else f'exit code {proc.returncode}'
    except subprocess.TimeoutExpired:
        return False, '', f'Timeout after {timeout}s'
    except Exception as e:  # noqa: BLE001
        return False, '', str(e)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def auto_grade(problem_type: str, answer, config: dict, user_id: int, db) -> Tuple[Optional[float], str, Optional[bool]]:
    """Dispatch to the right grader. Returns (score, feedback, is_correct).

    score is None for manual-only types (text/file) so the caller leaves the
    submission for the teacher to grade.
    """
    pt = (problem_type or '').lower()
    if pt == 'numeric':
        return grade_numeric(answer, config)
    if pt == 'mc':
        return grade_mc(answer, config)
    if pt == 'formula':
        return grade_formula(answer, config, user_id, db)
    if pt == 'code':
        return grade_code(answer, config)
    # text / file → manual
    return None, 'Submitted for manual grading.', None


def sanitize_problem_config_for_student(problem_type: str, config: dict) -> dict:
    """Strip secret fields (correct answers, hidden tests) before sending to a student."""
    safe = dict(config or {})
    safe.pop('correct_answer_enc', None)
    safe.pop('correct_index_enc', None)
    # Students must not see hidden test code — only that tests exist.
    if problem_type == 'code':
        tcs = safe.get('test_cases') or []
        safe['test_cases'] = [{'name': tc.get('name') or f'Test {i+1}'} for i, tc in enumerate(tcs)]
    return safe
