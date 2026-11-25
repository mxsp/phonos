"""Guard against SQLite-only SQL and sqlite3.connect() leftovers in phonos_app/.

Phonos runs on PostgreSQL. Several routes were written for SQLite and 500 on
Postgres (`julianday`, `datetime('now')`, `sqlite3.connect(Config.DATABASE_FILE)`).
This test scans the source so such leftovers cannot come back unnoticed.

The only legitimate sqlite3 use is the optional local fallback inside
phonos_app/helpers/database.py (selected by _use_sqlite()).
"""
import os
import re

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
APP_DIR = os.path.join(ROOT, 'phonos_app')

PATTERNS = {
    'julianday': re.compile(r'julianday\s*\(', re.I),
    "datetime('now'": re.compile(r"datetime\(\s*['\"]now['\"]", re.I),
    'sqlite3.connect': re.compile(r'sqlite3\.connect\s*\('),
    'INSERT OR IGNORE/REPLACE': re.compile(r'INSERT\s+OR\s+(IGNORE|REPLACE)', re.I),
}

# path (relative to phonos_app/) -> set of pattern names that are tolerated.
ALLOWLIST = {
    # The DB layer itself: sqlite3 is the explicit local fallback (_use_sqlite()).
    'helpers/database.py': {'sqlite3.connect'},
    # Owned by another work package (extension routes); still has
    # datetime('now') at the PDB-import insert. Remove from here once fixed.
}


def _python_files():
    for dirpath, dirnames, filenames in os.walk(APP_DIR):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for name in filenames:
            if name.endswith('.py'):
                yield os.path.join(dirpath, name)


def _code_lines(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        for no, line in enumerate(fh, 1):
            if line.lstrip().startswith('#'):
                continue
            yield no, line


def test_no_sqlite_dialect_leftovers():
    offenders = []
    for path in _python_files():
        rel = os.path.relpath(path, APP_DIR)
        allowed = ALLOWLIST.get(rel, set())
        for no, line in _code_lines(path):
            for name, rx in PATTERNS.items():
                if name in allowed:
                    continue
                if rx.search(line):
                    offenders.append(f'phonos_app/{rel}:{no}: {name}: {line.strip()[:100]}')
    assert not offenders, 'SQLite-only constructs found:\n' + '\n'.join(offenders)


@pytest.mark.parametrize('rel', sorted(ALLOWLIST))
def test_allowlist_entries_still_needed(rel):
    """Keep the allowlist honest: an entry that no longer matches must be removed."""
    path = os.path.join(APP_DIR, rel)
    assert os.path.exists(path), f'{rel} is on the allowlist but does not exist'
    hits = set()
    for _, line in _code_lines(path):
        for name, rx in PATTERNS.items():
            if rx.search(line):
                hits.add(name)
    stale = ALLOWLIST[rel] - hits
    assert not stale, f'{rel}: allowlisted but not present any more: {sorted(stale)}'
