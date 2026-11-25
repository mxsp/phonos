#!/usr/bin/env bash
#
# Pre-push checks for Phonos.
#
# Runs everything CI would catch, plus the frontend checks CI does *not* do:
# JavaScript syntax (including the large inline <script> blocks in templates),
# Jinja template parsing, and locale JSON validity/parity. Those three account
# for most of the "it lints fine but the page is blank" breakages, and they
# take a couple of seconds.
#
# Usage:
#   scripts/check.sh          # everything
#   scripts/check.sh py       # python lint + tests only
#   scripts/check.sh fe       # frontend checks only
#
# Exit code is non-zero if any check fails; every check runs regardless, so one
# invocation shows you the whole list rather than the first failure.

set -uo pipefail
cd "$(dirname "$0")/.."

WHAT="${1:-all}"
FAILED=()
GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'

run() {
    local label="$1"; shift
    printf '%s... ' "$label"
    local output
    if output=$("$@" 2>&1); then
        printf '%sok%s\n' "$GREEN" "$OFF"
    else
        printf '%sFAILED%s\n' "$RED" "$OFF"
        printf '%s%s%s\n' "$DIM" "$output" "$OFF" | head -40
        FAILED+=("$label")
    fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
if [[ "$WHAT" == "all" || "$WHAT" == "py" ]]; then
    if have flake8; then
        run "flake8 (syntax + undefined names)" \
            flake8 phonos_app/ production/ tests/ \
            --select=E999,F821,F822 \
            --exclude=venv,node_modules,.git,__pycache__
    else
        echo "flake8 not installed - skipping python lint"
    fi

    if have pytest || python3 -c 'import pytest' 2>/dev/null; then
        # test_model_io.py is dependency-free; test_main.py needs Flask + a DB.
        run "pytest (model_io)" env ENCRYPTION_KEY="a-secure-test-key-for-dev-use-32" \
            python3 -m pytest tests/test_model_io.py -q
        if python3 -c 'import flask' 2>/dev/null; then
            run "pytest (main)" env ENCRYPTION_KEY="a-secure-test-key-for-dev-use-32" TESTING=true \
                python3 -m pytest tests/test_main.py -q
        else
            echo "flask not installed - skipping tests/test_main.py"
        fi
    else
        echo "pytest not installed - skipping tests"
    fi
fi

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
if [[ "$WHAT" == "all" || "$WHAT" == "fe" ]]; then
    if have node; then
        run "javascript syntax (static/js)" bash -c '
            fail=0
            for f in static/js/*.js; do
                node --check "$f" || { echo "  ^ $f"; fail=1; }
            done
            exit $fail'

        # Inline template scripts carry thousands of lines of logic. Jinja tags
        # are neutralised before the syntax check: {{ expr }} becomes a literal
        # and {% block %} tags are dropped, which leaves valid JS when the
        # template is well-formed.
        run "javascript syntax (inline in templates)" python3 - <<'PY'
import glob, os, re, subprocess, sys, tempfile

SCRIPT_RE = re.compile(r'<script([^>]*)>(.*?)</script>', re.S | re.I)
TYPE_RE = re.compile(r'\btype\s*=\s*["\']([^"\']+)["\']', re.I)
# Only these types hold JavaScript. <script type="application/ld+json"> (SEO
# structured data) and friends are data, and would fail a JS parse by design.
JS_TYPES = {'', 'text/javascript', 'application/javascript', 'module'}


def error_line(stderr: str) -> str:
    for line in stderr.splitlines():
        if 'Error' in line:
            return line.strip()
    return 'syntax error'


failures = []
for path in sorted(glob.glob('templates/**/*.html', recursive=True)):
    src = open(path, encoding='utf-8').read()
    blocks = []
    for attrs, body in SCRIPT_RE.findall(src):
        if re.search(r'\bsrc\s*=', attrs, re.I):
            continue
        match = TYPE_RE.search(attrs)
        if (match.group(1).strip().lower() if match else '') not in JS_TYPES:
            continue
        blocks.append(body)
    if not blocks:
        continue

    code = '\n;\n'.join(blocks)
    code = re.sub(r'\{%-?.*?-?%\}', '', code, flags=re.S)
    code = re.sub(r'\{\{.*?\}\}', '0', code, flags=re.S)
    tmp = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
    tmp.write(code)
    tmp.close()
    result = subprocess.run(['node', '--check', tmp.name], capture_output=True, text=True)
    os.unlink(tmp.name)
    if result.returncode != 0:
        failures.append(f'{path}: {error_line(result.stderr)}')

for failure in failures:
    print(failure)
sys.exit(1 if failures else 0)
PY
    else
        echo "node not installed - skipping javascript checks"
    fi

    run "jinja templates parse" python3 - <<'PY'
import glob, sys
import jinja2

env = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))
failures = []
for path in sorted(glob.glob('templates/**/*.html', recursive=True)):
    try:
        env.parse(open(path, encoding='utf-8').read(), filename=path)
    except jinja2.TemplateSyntaxError as exc:
        failures.append(f'{path}:{exc.lineno}: {exc.message}')

for failure in failures:
    print(failure)
sys.exit(1 if failures else 0)
PY

    run "locale files valid + in sync" python3 - <<'PY'
import json, sys

def flatten(node, prefix=''):
    out = set()
    for key, value in node.items():
        path = f'{prefix}.{key}' if prefix else key
        if isinstance(value, dict):
            out |= flatten(value, path)
        else:
            out.add(path)
    return out

try:
    en = json.load(open('static/locales/en.json', encoding='utf-8'))
    de = json.load(open('static/locales/de.json', encoding='utf-8'))
except json.JSONDecodeError as exc:
    print(f'invalid JSON: {exc}')
    sys.exit(1)

en_keys, de_keys = flatten(en), flatten(de)
missing_de = sorted(en_keys - de_keys)
missing_en = sorted(de_keys - en_keys)

# Untranslated keys are a warning, not a failure: shipping an English string in
# the German bundle is worse than shipping nothing, but it should not block.
if missing_de:
    print(f'{len(missing_de)} key(s) missing from de.json, e.g. {missing_de[:5]}')
if missing_en:
    print(f'{len(missing_en)} key(s) missing from en.json, e.g. {missing_en[:5]}')
sys.exit(0)
PY
fi

# ---------------------------------------------------------------------------
echo
if (( ${#FAILED[@]} )); then
    printf '%s%d check(s) failed:%s %s\n' "$RED" "${#FAILED[@]}" "$OFF" "${FAILED[*]}"
    exit 1
fi
printf '%sall checks passed%s\n' "$GREEN" "$OFF"
