"""
Pytest configuration and shared fixtures for Phonos tests.

Centralizes path setup and common mocks so individual test files
don't have to duplicate fragile sys.path hacks.
"""
import os
import sys
from unittest.mock import Mock

import pytest

# --- Centralized path setup (run early for all tests) ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PRODUCTION_DIR = os.path.join(PROJECT_ROOT, 'production')
if PRODUCTION_DIR not in sys.path:
    sys.path.insert(0, PRODUCTION_DIR)

# Ensure a test encryption key is always present
os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')


@pytest.fixture
def mock_db():
    """Provide a mock DB connection that matches the execute().fetchone() style
    used throughout phonos_app.helpers (and production code).
    """
    db = Mock()
    # Default: execute() returns something with fetchone/fetchall
    result = Mock()
    result.fetchone.return_value = {'id': 1}
    result.fetchall.return_value = []
    db.execute.return_value = result

    # Also support direct attributes some code might poke at
    db.cursor.return_value = result

    return db


@pytest.fixture
def mock_get_db(mock_db):
    """Patched get_db() that returns the standard mock_db fixture."""
    with pytest.MonkeyPatch().context() as mp:
        from phonos_app.helpers import database as db_mod
        mp.setattr(db_mod, 'get_db', lambda: mock_db)
        # Also patch in bunches if it imports directly
        try:
            import phonos_app.helpers.bunches as bunches_mod
            mp.setattr(bunches_mod, 'get_db', lambda: mock_db)
        except Exception:
            pass
        yield mock_db


# Common environment patches that many tests need
@pytest.fixture(autouse=True)
def _base_patches(monkeypatch):
    """Lightweight auto-applied patches for noisy or external dependencies."""
    # Prevent real Redis connections in most unit tests
    monkeypatch.setenv('REDIS_HOST', 'localhost')
    # Make sure we don't hit real external services accidentally
    monkeypatch.setenv('PHONOS_API_URL', 'http://localhost:5000')
    # Silence some optional heavy imports during test collection if needed
    # (actual heavy deps like langchain are handled by skipping in their modules)
