#!/usr/bin/env python3
"""Scan the Phonos Python backend for language-related issues."""

import ast
import os
import re
from pathlib import Path

BASE = Path("/opt/phonos")
ROUTE_DIR = BASE / "phonos_app" / "routes"
HELPERS_DIR = BASE / "phonos_app" / "helpers"
PROD_DIR = BASE / "production"
TEMPLATE_DIR = BASE / "templates"

SKIP_FILES = {"__init__.py", "__pycache__"}

results = []

# ---------------------------------------------------------------------------
# 1. Hardcoded English in jsonify() calls
# ---------------------------------------------------------------------------
def scan_jsonify_hardcoded():
    hits = 0
    for pyfile in sorted(ROUTE_DIR.rglob("*.py")):
        if pyfile.name in SKIP_FILES:
            continue
        text = pyfile.read_text()
        # Look for jsonify({"error": ... "message": ...} with inline strings
        pattern = r'jsonify\(\s*\{[^}]*["\'](?:error|message|success|status)["\']\s*:\s*["\']([^"\']+)'
        for m in re.finditer(pattern, text, re.DOTALL):
            line_no = text[: m.start()].count("\n") + 1
            snippet = m.group(0)[:80]
            hits += 1
    return hits

def collect_jsonify_errors():
    entries = []
    for pyfile in sorted(ROUTE_DIR.rglob("*.py")):
        if pyfile.name in SKIP_FILES:
            continue
        text = pyfile.read_text()
        lines = text.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "jsonify" in stripped and ('"message"' in stripped or "'message'" in stripped):
                if not any(k in stripped for k in ("get_translation", "t(", "i18n")):
                    entries.append((pyfile.relative_to(BASE), i, stripped[:120]))
    return entries

def collect_render_template_errors():
    entries = []
    for pyfile in set(sorted(ROUTE_DIR.rglob("*.py"))) | set(sorted(PROD_DIR.rglob("*.py"))):
        if pyfile.name in SKIP_FILES:
            continue
        text = pyfile.read_text()
        lines = text.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "render_template" in stripped and "error=" in stripped:
                if "t(" not in stripped and "get_translation" not in stripped:
                    m = re.search(r"error=([\"'])([^\"']+)", stripped)
                    if m:
                        entries.append((pyfile.relative_to(BASE), i, m.group(2)))
    return entries

# ---------------------------------------------------------------------------
# 2. Check i18n helper
# ---------------------------------------------------------------------------
def check_i18n_helper():
    i18n_path = HELPERS_DIR / "i18n.py"
    if not i18n_path.exists():
        results.append("[FAIL] helpers/i18n.py not found!")
        return
    text = i18n_path.read_text()

    issues = []

    # Check if init_i18n is ever called
    grep_init = re.search(r"init_i18n\(", text)
    if not grep_init:
        issues.append("init_i18n(app) is defined but NEVER called from app.py or anywhere else")

    # Check if t() is imported/used in routes
    route_files = list(ROUTE_DIR.rglob("*.py"))
    t_used = 0
    for rf in route_files:
        if rf.name in SKIP_FILES:
            continue
        rt = rf.read_text()
        if "from ..helpers.i18n" in rt or "from phonos_app.helpers.i18n" in rt:
            t_used += 1
        if " t(" in rt or "get_translation(" in rt:
            t_used += 1
    if t_used == 0:
        issues.append("t() / get_translation() is NEVER imported or called in any route file — server-side i18n is entirely unused")

    # Language detection gap: cookie not read
    if "phonos-language" not in text and "LANGUAGE_COOKIE" not in text:
        issues.append("get_user_language() does NOT check the 'phonos-language' cookie — JS sets this cookie but Python ignores it")

    return issues

# ---------------------------------------------------------------------------
# 3. Hardcoded English in template error strings
# ---------------------------------------------------------------------------
def check_template_error_strings():
    entries = []
    for pyfile in sorted(ROUTE_DIR.rglob("*.py")):
        if pyfile.name in SKIP_FILES:
            continue
        text = pyfile.read_text()
        lines = text.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "error='" in stripped or 'error="' in stripped:
                if "render_template" not in stripped:
                    continue
                m = re.search(r"error=([\"'])([^\"']+)", stripped)
                if m:
                    entries.append((pyfile.relative_to(BASE), i, m.group(2)))
    return entries

# ---------------------------------------------------------------------------
# 4. Server-side language detection / cookie sync
# ---------------------------------------------------------------------------
def check_language_cookie_sync():
    issues = []
    # JS sets phonos-language cookie; does Python read it properly?
    pages_path = ROUTE_DIR / "pages.py"
    ptext = pages_path.read_text()
    if "LANGUAGE_COOKIE" in ptext and "phonos-language" in ptext:
        # pages.py detects it for legal pages, but:
        lines = ptext.split("\n")
        for i, line in enumerate(lines, 1):
            if "DEFAULT_LANG" in line and "en" in line:
                break
        issues.append("pages.py _detect_lang() reads the phonos-language cookie (good) — but only for legal pages (ToS, Datenschutz, etc.)")
        issues.append("pages.py DEFAULT_LANG = 'en' — hardcoded fallback")
    else:
        issues.append("CRITICAL: pages.py does not use the phonos-language cookie at all!")

    # Check if /api/language endpoint is registered (init_i18n)
    if not any("api_language" in f.read_text() for f in ROUTE_DIR.rglob("*.py") if f.name not in SKIP_FILES):
        issues.append("/api/language endpoint (from init_i18n) is NEVER registered — the session-based language setter is dead code")

    return issues

# ---------------------------------------------------------------------------
# 5. Template context variables usage
# ---------------------------------------------------------------------------
def check_template_context():
    entries = []
    for tf in sorted(TEMPLATE_DIR.rglob("*.html")):
        text = tf.read_text()
        lines = text.split("\n")
        for i, line in enumerate(lines, 1):
            if "user_language" in line or "get_language()" in line or "{{ t(" in line:
                entries.append((tf.relative_to(BASE), i, line.strip()))
    return entries

# ---------------------------------------------------------------------------
# 6. Language defaults in configs
# ---------------------------------------------------------------------------
def check_language_defaults():
    entries = []
    for pyfile in set(sorted(ROUTE_DIR.rglob("*.py"))) | set(sorted((BASE / "phonos_app").rglob("*.py"))):
        if pyfile.name in SKIP_FILES:
            continue
        text = pyfile.read_text()
        lines = text.split("\n")
        for i, line in enumerate(lines, 1):
            if "default_lang" in line.lower() or "DEFAULT_LANG" in line:
                entries.append((pyfile.relative_to(BASE), i, line.strip()))
    return entries


# ---------------------------------------------------------------------------
# _summarize_storage hardcoded English
# ---------------------------------------------------------------------------
def check_summarize_storage():
    pages_path = ROUTE_DIR / "pages.py"
    text = pages_path.read_text()
    lines = text.split("\n")
    entries = []
    for i, line in enumerate(lines, 1):
        if "_summarize_storage" in line or "summary_text" in line or "available_text" in line:
            if "used" in line or "quota" in line or "available" in line:
                entries.append((pages_path.relative_to(BASE), i, line.strip()))
    return entries


# ===========================================================================
print("=" * 76)
print("  Phonos i18n / Language Scan Report")
print("=" * 76)

# 1. jsonify hardcoded messages
print("\n" + "-" * 76)
print("1. HARDCODED ENGLISH IN jsonify() CALLS (error/message/success strings)")
print("-" * 76)
jsonify_errors = collect_jsonify_errors()
print(f"   Total: {len(jsonify_errors)} jsonify({{'error':..., 'message':...}}) calls with hardcoded English.")
for f, line, snippet in jsonify_errors:
    print(f"   {f}:{line}: {snippet}")
if not jsonify_errors:
    print("   (none found — good)")

# 2. render_template error= strings
print("\n" + "-" * 76)
print("2. HARDCODED ENGLISH IN render_template(error=...) CALLS")
print("-" * 76)
rt_errors = collect_render_template_errors()
print(f"   Total: {len(rt_errors)} render_template(..., error='...') calls with hardcoded English.")
for f, line, msg in rt_errors:
    print(f"   {f}:{line}: error={msg!r}")

# 3. i18n helper analysis
print("\n" + "-" * 76)
print("3. i18n HELPER ANALYSIS (helpers/i18n.py)")
print("-" * 76)
i18n_issues = check_i18n_helper()
if i18n_issues:
    for issue in i18n_issues:
        print(f"   [!] {issue}")
else:
    print("   No issues found.")

# 4. Language cookie sync
print("\n" + "-" * 76)
print("4. LANGUAGE COOKIE / SERVER-SIDE SYNC")
print("-" * 76)
cookie_issues = check_language_cookie_sync()
for issue in cookie_issues:
    print(f"   [!] {issue}")

# 5. Template context variables
print("\n" + "-" * 76)
print("5. TEMPLATE CONTEXT: {{ user_language }} / {{ get_language() }} USAGE")
print("-" * 76)
tmpl_context = check_template_context()
if tmpl_context:
    for f, line, snippet in tmpl_context:
        print(f"   {f}:{line}: {snippet}")
else:
    print("   NOT USED at all. Templates rely entirely on client-side data-i18n attributes.")

# 6. Language defaults
print("\n" + "-" * 76)
print("6. DEFAULT LANGUAGE CONFIGURATION")
print("-" * 76)
defaults = check_language_defaults()
for f, line, snippet in defaults:
    print(f"   {f}:{line}: {snippet}")

# Summarize storage hardcoded English
print("\n" + "-" * 76)
print("7. _summarize_storage() HARDCODED ENGLISH (pages.py)")
print("-" * 76)
storage_issues = check_summarize_storage()
for f, line, snippet in storage_issues:
    print(f"   {f}:{line}: {snippet}")

# ===========================================================================
print("\n" + "=" * 76)
print("  SUMMARY")
print("=" * 76)
print(f"""
CRITICAL FINDINGS:

1. init_i18n() is NEVER called
   The i18n module (phonos_app/helpers/i18n.py:99) defines init_i18n(app) which
   registers the context_processor (injecting 't', 'get_language', 'user_language'
   into templates) AND the /api/language endpoint — but it is never called from
   app.py or anywhere else. This means:
   - The context_processor is NOT registered
   - /api/language GET/POST endpoint is NOT registered
   - Templates have no access to {{ user_language }}, {{ get_language() }}, {{ t() }}
   - The session['language'] approach is entirely dead code

2. t() / get_translation() is NEVER used in route handlers
   No route file imports or calls the translation function. All ~""" + str(len(jsonify_errors)) + """+
   JSON error messages and ~""" + str(len(rt_errors)) + """+ template error strings are hardcoded
   in English only.

3. Server-side language detection is fragmented:
   - pages.py _detect_lang() reads 'phonos-language' cookie (JS sets it), but only
     for legal pages (ToS, Datenschutz, Impressum, Licenses).
   - i18n.py get_user_language() checks session['language'] (never set) then
     Accept-Language header (not the cookie!) — it IGNORES the cookie entirely.
   - The result: the JS sets the cookie, and legal pages respect it, but no other
     Python code uses it for language detection.

4. Error handlers (pages.py:1198-1208) return hardcoded English:
   - 404: 'The requested resource was not found.'
   - 500: 'An internal server error occurred.'

5. _summarize_storage() (pages.py:224-241) returns hardcoded English strings:
   - '{used} used' / 'No storage quota configured' / '{available} available'

6. ~""" + str(len(jsonify_errors)) + """ jsonify() calls across route files have hardcoded
   English user-facing messages that never flow through any i18n system.

7. Translation files exist at static/locales/en.json and de.json (~2400 keys each),
   and the client-side data-i18n approach works well for UI text. But server-sent
   error messages bypass this entirely.

RECOMMENDATIONS:

A. Call init_i18n(app) in app.py after app creation to register the context
   processor and /api/language endpoint.

B. Have get_user_language() check the 'phonos-language' cookie (matching the JS)
   before falling back to Accept-Language.

C. Create translation keys for all server-sent error messages in locales/*.json,
   then replace hardcoded strings in routes with t('error.some_key').

D. For non-critical errors (validation errors in API routes), a practical first
   step is to at least wrap the string in a call to t() so German users see
   translated messages.
""")
