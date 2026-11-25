import importlib.util
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional, Sequence, Tuple
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
except ImportError as exc:  # pragma: no cover - handled at runtime
    # Defer hard failures until the database layer is actually invoked so the
    # application can still start in environments without psycopg2 available
    # (e.g., lightweight test runs or static analysis).
    psycopg2 = None
    pool = None
    RealDictCursor = None
    _psycopg2_import_error = exc
else:
    _psycopg2_import_error = None

from flask import current_app

try:  # Support both package and legacy module import styles
    from .config import Config
    from .plan_service import DEFAULT_PLAN
except ImportError:  # pragma: no cover - fallback for direct module execution
    from config import Config  # type: ignore
    from plan_service import DEFAULT_PLAN  # type: ignore


# Configure logging
logger = logging.getLogger(__name__)

DEFAULT_STORAGE_QUOTA = getattr(Config, "USER_STORAGE_QUOTA", 1024 * 1024 * 1024)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_FILE = os.path.join(os.path.dirname(BASE_DIR), "phonos.db")

# Connection pool instance (initialized on first use). When psycopg2 is not
# available, keep this as a generic object to avoid attribute errors during
# module import.
_connection_pool: Optional[object] = None

# Pool configuration
POOL_MIN_CONNECTIONS = int(os.environ.get('DB_POOL_MIN', 2))
POOL_MAX_CONNECTIONS = int(os.environ.get('DB_POOL_MAX', 20))

# Session timeouts applied to every connection (pool and overflow alike), in
# milliseconds, mirroring phonos_app/helpers/database.py.
STATEMENT_TIMEOUT = 30000
IDLE_IN_TXN_TIMEOUT = 60000

# When the pool is exhausted we may open a small, bounded number of direct
# "overflow" connections so background services degrade gracefully instead of
# failing outright. These are tracked here (by id(conn), since psycopg2
# connections do not allow arbitrary attributes) so they are closed on release
# rather than (incorrectly) handed to the pool, and so their total is capped —
# preventing runaway connections from exhausting PostgreSQL's own
# max_connections and the memory each backend consumes.
OVERFLOW_MAX = int(os.environ.get('DB_POOL_OVERFLOW', 5))

# Once the overflow cap is reached, retry the pool with backoff before giving
# up entirely.
MAX_RETRIES = 3
RETRY_DELAY = 0.5  # seconds

_overflow_lock = threading.Lock()
_overflow_count = 0  # live overflow connections (reserved slots included)
_overflow_ids: set = set()  # id(conn) of live overflow connections


def _require_psycopg2():
    """Ensure psycopg2 is available before performing PostgreSQL operations."""

    if psycopg2 is None:
        raise ImportError(
            "psycopg2 is required for PostgreSQL operations. Install psycopg2-binary "
            "or configure a compatible database driver."
        ) from _psycopg2_import_error


class DatabaseConfig:
    """Database configuration with validation and parsing."""

    def __init__(self, database_url: str):
        self.url = database_url
        self._parse_and_validate()

    def _parse_and_validate(self):
        """Parse and validate the database URL."""
        try:
            parsed = urlparse(self.url)

            if parsed.scheme not in ('postgresql', 'postgres'):
                raise ValueError(f"Invalid database scheme: {parsed.scheme}. Expected 'postgresql' or 'postgres'")

            self.host = parsed.hostname or 'localhost'
            self.port = parsed.port or 5432
            self.database = parsed.path.lstrip('/') or 'phonos'
            self.user = parsed.username or 'phonos_user'
            self.password = parsed.password or ''

            if not self.database:
                raise ValueError("Database name is required")

            if not self.user:
                raise ValueError("Database user is required")

        except Exception as e:
            raise ValueError(f"Invalid DATABASE_URL: {e}")

    def get_connection_params(self) -> dict:
        """Return connection parameters for psycopg2."""
        return {
            'host': self.host,
            'port': self.port,
            'database': self.database,
            'user': self.user,
            'password': self.password,
        }

    def __str__(self):
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"


def _resolve_database_url() -> str:
    """Return the database URL, respecting Flask overrides when available."""
    try:
        if current_app.config.get("TESTING") and current_app.config.get("DATABASE_FILE"):
            return f"sqlite:///{current_app.config['DATABASE_FILE']}"
        app_db = current_app.config.get("DATABASE_URL")
        if app_db:
            return app_db
    except RuntimeError:
        # ``current_app`` is only available inside an application context; fall back.
        pass
    return Config.DATABASE_URL


def _resolve_database_file() -> str:
    """Return the SQLite database file, respecting Flask overrides when available."""
    try:
        app_db = current_app.config.get("DATABASE_FILE")
        if app_db:
            return app_db
    except RuntimeError:
        pass
    return getattr(Config, "DATABASE_FILE", DATABASE_FILE)


def _using_sqlite() -> bool:
    """Return True when the active configuration points at SQLite."""
    db_url = _resolve_database_url()
    return not db_url or db_url.startswith("sqlite")


def _get_connection_pool() -> object:
    """Get or create the connection pool."""
    global _connection_pool

    if _connection_pool is None:
        _require_psycopg2()
        db_url = _resolve_database_url()
        db_config = DatabaseConfig(db_url)

        logger.info(f"Initializing PostgreSQL connection pool: {db_config}")
        logger.info(f"Pool size: min={POOL_MIN_CONNECTIONS}, max={POOL_MAX_CONNECTIONS}")

        try:
            _connection_pool = pool.ThreadedConnectionPool(
                minconn=POOL_MIN_CONNECTIONS,
                maxconn=POOL_MAX_CONNECTIONS,
                **db_config.get_connection_params(),
                cursor_factory=RealDictCursor,
                options=f"-c statement_timeout={STATEMENT_TIMEOUT} -c idle_in_transaction_session_timeout={IDLE_IN_TXN_TIMEOUT}",
            )
            logger.info("PostgreSQL connection pool initialized successfully")
        except psycopg2.Error as e:
            logger.error(f"Failed to initialize connection pool: {e}")
            raise

    return _connection_pool


def get_db_connection():
    """Get a database connection for the active backend."""
    if _using_sqlite():
        db_path = _resolve_database_file()
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)

        conn = sqlite3.connect(
            db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    global _overflow_count

    _require_psycopg2()
    try:
        conn = _get_connection_pool().getconn()
        conn.autocommit = False
        return conn
    except pool.PoolError as e:
        # Pool exhausted: fall back to a direct connection, but only up to a
        # bounded overflow so we never open an unlimited number of backends.
        # Reserve a slot under the lock so the cap check and increment are
        # atomic, then connect outside the lock.
        reserved = False
        with _overflow_lock:
            if _overflow_count < OVERFLOW_MAX:
                _overflow_count += 1
                reserved = True

        if reserved:
            try:
                conn = psycopg2.connect(
                    _resolve_database_url(),
                    cursor_factory=RealDictCursor,
                    connect_timeout=10,
                    options=f"-c statement_timeout={STATEMENT_TIMEOUT} -c idle_in_transaction_session_timeout={IDLE_IN_TXN_TIMEOUT}",
                )
            except Exception:
                # Release the reserved slot before propagating.
                with _overflow_lock:
                    _overflow_count -= 1
                raise
            conn.autocommit = False
            with _overflow_lock:
                _overflow_ids.add(id(conn))
                overflow_count = _overflow_count
            logger.warning(
                f"Pool exhausted, created overflow connection ({overflow_count}/{OVERFLOW_MAX}): {e}"
            )
            return conn

        # Overflow cap reached: retry the pool with backoff instead of opening
        # an unbounded direct connection.
        for attempt in range(1, MAX_RETRIES + 1):
            logger.warning(
                f"Pool exhausted and overflow cap ({OVERFLOW_MAX}) reached; "
                f"retrying pool (attempt {attempt}/{MAX_RETRIES}): {e}"
            )
            time.sleep(RETRY_DELAY * attempt)
            try:
                conn = _get_connection_pool().getconn()
                conn.autocommit = False
                return conn
            except pool.PoolError:
                continue

        logger.error(
            f"Pool exhausted, overflow cap ({OVERFLOW_MAX}) reached, and "
            f"{MAX_RETRIES} retries failed: {e}"
        )
        raise
    except psycopg2.Error as e:
        logger.error(f"Failed to get database connection: {e}")
        raise


def release_db_connection(conn):
    """Release a connection back to the pool (or close it if it's an overflow)."""
    global _overflow_count

    # Overflow connections were created directly (not from the pool), so close
    # them rather than calling putconn — which would raise for a foreign conn.
    with _overflow_lock:
        is_overflow = id(conn) in _overflow_ids
        if is_overflow:
            _overflow_ids.discard(id(conn))
            _overflow_count -= 1
    if is_overflow:
        try:
            conn.close()
        except Exception:
            pass
        return

    try:
        if _connection_pool:
            _connection_pool.putconn(conn)
        else:
            conn.close()
    except Exception as e:
        logger.warning(f"Error releasing connection: {e}")
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def db_connection():
    """Context manager for database connections with automatic cleanup."""
    conn = None
    try:
        conn = get_db_connection()
        yield conn
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            release_db_connection(conn)


def check_database_health() -> dict:
    """Check database connectivity and return health status."""
    _require_psycopg2()
    result = {
        'status': 'unknown',
        'message': '',
        'details': {}
    }

    try:
        with db_connection() as conn:
            cursor = conn.cursor()

            # Check connection
            cursor.execute("SELECT 1")

            # Get database version
            cursor.execute("SELECT version()")
            version = _get_row_value(cursor.fetchone())

            # Get connection count
            cursor.execute("""
                SELECT count(*) as active_connections
                FROM pg_stat_activity
                WHERE datname = current_database()
            """)
            active_conns = _get_row_value(cursor.fetchone(), 'active_connections')

            # Get database size
            cursor.execute("""
                SELECT pg_size_pretty(pg_database_size(current_database())) as db_size
            """)
            db_size = _get_row_value(cursor.fetchone(), 'db_size')

            result['status'] = 'healthy'
            result['message'] = 'Database connection successful'
            result['details'] = {
                'version': version,
                'active_connections': active_conns,
                'database_size': db_size,
                'pool_min': POOL_MIN_CONNECTIONS,
                'pool_max': POOL_MAX_CONNECTIONS,
            }

    except psycopg2.OperationalError as e:
        result['status'] = 'unhealthy'
        result['message'] = f'Connection failed: {str(e)}'
        logger.error(f"Database health check failed: {e}")
    except Exception as e:
        result['status'] = 'error'
        result['message'] = f'Health check error: {str(e)}'
        logger.error(f"Database health check error: {e}")

    return result


def validate_database_config():
    """Validate database configuration on startup."""
    db_url = _resolve_database_url()

    if _using_sqlite():
        db_path = _resolve_database_file()
        if not db_path:
            raise ValueError("DATABASE_FILE is not configured")

        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)

        parent_dir = os.path.dirname(db_path) or "."
        if not os.path.isdir(parent_dir):
            raise ValueError(f"SQLite database directory does not exist: {parent_dir}")

        logger.info("Validated SQLite database configuration: %s", db_path)
        return True

    if not db_url:
        raise ValueError("DATABASE_URL is not configured")

    # Validate URL format
    config = DatabaseConfig(db_url)

    # Test connection
    logger.info(f"Validating database configuration: {config}")

    try:
        conn = psycopg2.connect(
            db_url,
            cursor_factory=RealDictCursor,
            connect_timeout=10
        )

        cursor = conn.cursor()
        cursor.execute("SELECT 1")

        # Check if we can create tables (permissions check)
        cursor.execute("""
            SELECT has_schema_privilege(current_user, 'public', 'CREATE') as has_privilege
        """)
        result = cursor.fetchone()
        can_create = result['has_privilege'] if result else False

        if not can_create:
            logger.warning("Database user may not have CREATE permissions")

        conn.close()
        logger.info("Database configuration validated successfully")

        return True

    except psycopg2.OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        raise ValueError(f"Cannot connect to database: {e}")
    except Exception as e:
        logger.error(f"Database validation failed: {e}")
        raise


def close_connection_pool():
    """Close all connections in the pool."""
    global _connection_pool, _overflow_count

    if _connection_pool:
        logger.info("Closing database connection pool")
        _connection_pool.closeall()
        _connection_pool = None

    # Reset overflow tracking. We only retain ids of live overflow connections
    # (psycopg2 connections cannot hold arbitrary attributes), so they cannot be
    # closed here by reference — they are closed on release_db_connection().
    with _overflow_lock:
        _overflow_ids.clear()
        _overflow_count = 0


def _get_table_columns(cursor, table: str) -> set[str]:
    """Return the set of column names for ``table``."""
    if isinstance(cursor, sqlite3.Cursor):
        info = cursor.execute(f"PRAGMA table_info({table})").fetchall()
        columns: set[str] = set()
        for row in info:
            if isinstance(row, sqlite3.Row):
                columns.add(row["name"])
            else:
                columns.add(row[1])
        return columns

    cursor.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
    """, (table,))
    columns: set[str] = set()
    for row in cursor.fetchall():
        columns.add(_get_row_value(row, "column_name"))
    return columns


def _get_row_value(row, key: str = None):
    """Return a value from either tuple-based or dict-like DB rows."""
    if isinstance(row, dict):
        if key is None:
            return next(iter(row.values()))
        return row[key]
    return row[0]


def _ensure_columns(cursor, table: str, columns: Sequence[Tuple[str, str]]):
    """Add any missing columns defined in ``columns`` to ``table``."""

    existing = _get_table_columns(cursor, table)
    for name, definition in columns:
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _maybe_rename_column(cursor, table: str, old: str, new: str):
    """Rename ``old`` column to ``new`` if present."""

    columns = _get_table_columns(cursor, table)
    if old in columns and new not in columns:
        cursor.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def _create_sqlite_schema(cursor):
    """Create the SQLite schema using the legacy compatibility bootstrap.

    The legacy ``phonos-main`` mirror has been removed from the live tree. When
    the mirror file is missing, we create the notebook tables first and then
    run the main schema path so tests and fresh SQLite installs continue to
    work.
    """
    legacy_dir = os.path.join(os.path.dirname(BASE_DIR), "phonos-main", "production")
    legacy_db_path = os.path.join(legacy_dir, "database.py")
    use_legacy = os.path.exists(legacy_db_path)

    if not use_legacy:
        # Create notebook tables first; _create_schema relies on them existing
        # when it creates indexes at the end of the function.
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS notebooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                user_id INTEGER,
                creator_id INTEGER,
                name TEXT NOT NULL DEFAULT 'Untitled Notebook',
                description TEXT,
                kernel_type TEXT DEFAULT 'python3',
                metadata TEXT DEFAULT '{}',
                cells TEXT DEFAULT '[]',
                settings TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER DEFAULT 0,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS notebook_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                container_id TEXT,
                session_token TEXT UNIQUE NOT NULL,
                kernel_status TEXT DEFAULT 'starting',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                workspace_path TEXT,
                error_message TEXT,
                runtime_level TEXT DEFAULT 'basic',
                resource_config TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS notebook_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_type TEXT,
                size_bytes INTEGER DEFAULT 0,
                mime_type TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(notebook_id, file_path)
            );

            CREATE TABLE IF NOT EXISTS notebook_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
                cell_id TEXT NOT NULL,
                msg_id TEXT,
                execution_count INTEGER,
                source TEXT NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                status TEXT DEFAULT 'running',
                outputs TEXT DEFAULT '[]',
                error_output TEXT,
                duration_ms INTEGER,
                tags TEXT DEFAULT '[]',
                annotation TEXT
            );

            CREATE TABLE IF NOT EXISTS notebook_session_cleanup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
                cleanup_scheduled_at TIMESTAMP NOT NULL,
                cleanup_executed_at TIMESTAMP,
                workspace_deleted INTEGER DEFAULT 0,
                files_archived INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS notebook_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                commit_hash TEXT UNIQUE NOT NULL,
                parent_hash TEXT,
                message TEXT DEFAULT '',
                action TEXT NOT NULL DEFAULT 'auto_save',
                cells_snapshot TEXT NOT NULL DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_notebook_history_notebook ON notebook_history(notebook_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notebook_history_hash ON notebook_history(commit_hash);
            """
        )
        # _create_schema is PostgreSQL-flavored but contains SQLite branches.
        # Use a sqlite3.Cursor subclass that translates remaining PostgreSQL
        # syntax so it runs under SQLite during local/test installs.
        class _SQLiteCursor(sqlite3.Cursor):
            _REPLACEMENTS = [
                (re.compile(r"\bSERIAL\s+PRIMARY\s+KEY\b", re.IGNORECASE), "INTEGER PRIMARY KEY AUTOINCREMENT"),
                (re.compile(r"\bBIGSERIAL\b", re.IGNORECASE), "INTEGER"),
                (re.compile(r"\bSERIAL\b", re.IGNORECASE), "INTEGER"),
                (re.compile(r"\bBYTEA\b", re.IGNORECASE), "BLOB"),
                (re.compile(r"\bJSONB\b", re.IGNORECASE), "TEXT"),
                (re.compile(r"\bVARCHAR(?:\(\s*\d+\s*\))?\b", re.IGNORECASE), "TEXT"),
                (re.compile(r"\bNOW\s*\(\s*\)", re.IGNORECASE), "CURRENT_TIMESTAMP"),
                (re.compile(r"\bINTEGER\[\]\b", re.IGNORECASE), "TEXT"),
                (re.compile(r"\bDECIMAL\s*\(\s*\d+\s*,\s*\d+\s*\)", re.IGNORECASE), "REAL"),
            ]

            @classmethod
            def _translate(cls, sql):
                if not isinstance(sql, str):
                    return sql
                for pattern, replacement in cls._REPLACEMENTS:
                    sql = pattern.sub(replacement, sql)
                return sql

            def execute(self, sql, parameters=None):
                sql = self._translate(sql)
                if parameters is None:
                    return super().execute(sql)
                return super().execute(sql, parameters)

            def executescript(self, sql):
                return super().executescript(self._translate(sql))

        _create_schema(cursor.connection.cursor(_SQLiteCursor))
        return

    spec = importlib.util.spec_from_file_location("phonos_legacy_sqlite_database", legacy_db_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load SQLite schema bootstrap from {legacy_db_path}")

    module = importlib.util.module_from_spec(spec)
    inserted = False
    if legacy_dir not in sys.path:
        sys.path.insert(0, legacy_dir)
        inserted = True
    try:
        spec.loader.exec_module(module)
        module._create_schema(cursor)
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS notebooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                user_id INTEGER,
                creator_id INTEGER,
                name TEXT NOT NULL DEFAULT 'Untitled Notebook',
                description TEXT,
                kernel_type TEXT DEFAULT 'python3',
                metadata TEXT DEFAULT '{}',
                cells TEXT DEFAULT '[]',
                settings TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER DEFAULT 0,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS notebook_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                container_id TEXT,
                session_token TEXT UNIQUE NOT NULL,
                kernel_status TEXT DEFAULT 'starting',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                workspace_path TEXT,
                error_message TEXT,
                runtime_level TEXT DEFAULT 'basic',
                resource_config TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS notebook_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_type TEXT,
                size_bytes INTEGER DEFAULT 0,
                mime_type TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(notebook_id, file_path)
            );

            CREATE TABLE IF NOT EXISTS notebook_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
                cell_id TEXT NOT NULL,
                msg_id TEXT,
                execution_count INTEGER,
                source TEXT NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                status TEXT DEFAULT 'running',
                outputs TEXT DEFAULT '[]',
                error_output TEXT,
                duration_ms INTEGER,
                tags TEXT DEFAULT '[]',
                annotation TEXT
            );

            CREATE TABLE IF NOT EXISTS notebook_session_cleanup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
                cleanup_scheduled_at TIMESTAMP NOT NULL,
                cleanup_executed_at TIMESTAMP,
                workspace_deleted INTEGER DEFAULT 0,
                files_archived INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS notebook_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                commit_hash TEXT UNIQUE NOT NULL,
                parent_hash TEXT,
                message TEXT DEFAULT '',
                action TEXT NOT NULL DEFAULT 'auto_save',
                cells_snapshot TEXT NOT NULL DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_notebook_history_notebook ON notebook_history(notebook_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notebook_history_hash ON notebook_history(commit_hash);

            CREATE TABLE IF NOT EXISTS project_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id INTEGER,
                resource_name TEXT DEFAULT '',
                description TEXT DEFAULT '',
                snapshot_path TEXT,
                metadata TEXT DEFAULT '{}',
                retention_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_project_history_project ON project_history(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_project_history_resource ON project_history(resource_type, resource_id);

            CREATE TABLE IF NOT EXISTS user_wolfram_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                username_enc TEXT NOT NULL,
                password_enc TEXT NOT NULL,
                wolfram_id_email TEXT,
                last_verified_at TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_user_wolfram_creds_user ON user_wolfram_credentials(user_id);

            CREATE TABLE IF NOT EXISTS user_quantum_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                execution_mode TEXT NOT NULL DEFAULT 'simulator',
                credential_source TEXT NOT NULL DEFAULT 'platform',
                preferred_backend TEXT NOT NULL DEFAULT 'automatic',
                auto_start_session INTEGER NOT NULL DEFAULT 0,
                session_timeout_sec INTEGER NOT NULL DEFAULT 60,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_user_quantum_settings_user ON user_quantum_settings(user_id);

            CREATE TABLE IF NOT EXISTS user_ibm_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                api_key_enc TEXT NOT NULL,
                instance_crn TEXT,
                preferred_backend TEXT,
                last_verified_at TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_user_ibm_creds_user ON user_ibm_credentials(user_id);

            CREATE TABLE IF NOT EXISTS user_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                preferences TEXT NOT NULL DEFAULT '{}',
                -- QoL / notification settings consumed by /api/user/preferences.
                -- Must stay in sync with ensure_user_preferences_table() in
                -- phonos_app/routes/api_settings.py (the authoritative self-healer).
                -- Their absence here is what previously left this table drifted so
                -- desktop/sound notifications silently failed.
                keyboard_shortcuts_enabled INTEGER NOT NULL DEFAULT 1,
                command_palette_enabled INTEGER NOT NULL DEFAULT 1,
                desktop_notifications_enabled INTEGER NOT NULL DEFAULT 0,
                sound_notifications_enabled INTEGER NOT NULL DEFAULT 0,
                email_digest_frequency TEXT NOT NULL DEFAULT 'none',
                auto_retry_failed_jobs INTEGER NOT NULL DEFAULT 0,
                auto_retry_max_attempts INTEGER NOT NULL DEFAULT 3,
                auto_retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
                default_project_id INTEGER,
                sidebar_collapsed INTEGER NOT NULL DEFAULT 0,
                sidebar_width INTEGER NOT NULL DEFAULT 240,
                theme_mode TEXT NOT NULL DEFAULT 'system',
                saved_searches TEXT DEFAULT '[]',
                recent_searches TEXT DEFAULT '[]',
                pinned_items TEXT DEFAULT '[]',
                onboarding_checklist TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS runtime_levels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                backend_type TEXT NOT NULL,
                config_encrypted TEXT,
                mem_limit TEXT DEFAULT '2g',
                cpu_quota REAL DEFAULT 1.0,
                gpu_enabled INTEGER DEFAULT 0,
                max_sessions INTEGER DEFAULT 5,
                sort_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO runtime_levels
                (name, display_name, backend_type, mem_limit, cpu_quota, gpu_enabled, max_sessions, sort_order, is_active)
            VALUES
                ('basic', 'Basic', 'local_docker', '2g', 1.0, 0, 5, 0, 1);

            INSERT OR IGNORE INTO runtime_levels
                (name, display_name, backend_type, mem_limit, cpu_quota, gpu_enabled, max_sessions, sort_order, is_active)
            VALUES
                ('wolfram', 'Wolfram Language', 'wolfram_service', '1g', 0.5, 0, 3, 10, 1);
            """
        )
    finally:
        if inserted:
            sys.path.remove(legacy_dir)


def _ensure_user_model_tables(cursor):
    """Create and patch schema required by the custom user-model bridge workers."""

    is_sqlite = isinstance(cursor, sqlite3.Cursor)
    pk_type = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite else "SERIAL PRIMARY KEY"
    bool_type = "INTEGER" if is_sqlite else "BOOLEAN"
    true_default = "1" if is_sqlite else "TRUE"
    false_default = "0" if is_sqlite else "FALSE"

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS user_models (
            id {pk_type},
            model_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            project_id INTEGER,
            model_name TEXT NOT NULL,
            model_description TEXT,
            model_type TEXT DEFAULT 'custom',
            input_schema TEXT NOT NULL DEFAULT '[]',
            output_format TEXT DEFAULT 'json',
            outline_color TEXT DEFAULT 'gray',
            badge TEXT,
            badge_color TEXT,
            credit_cost INTEGER DEFAULT 5,
            estimated_runtime_seconds INTEGER DEFAULT 60,
            worker_endpoint_url TEXT,
            requires_api_key {bool_type} NOT NULL DEFAULT {true_default},
            is_active {bool_type} NOT NULL DEFAULT {true_default},
            is_public {bool_type} NOT NULL DEFAULT {false_default},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            total_jobs_run INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
        )
        """
    )

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS user_model_api_keys (
            id {pk_type},
            user_model_id INTEGER,
            project_id INTEGER,
            user_id INTEGER NOT NULL,
            key_id TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            is_active {bool_type} NOT NULL DEFAULT {true_default},
            rate_limit_per_minute INTEGER DEFAULT 60,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            total_requests INTEGER DEFAULT 0,
            encrypted_key TEXT,
            encryption_enabled {bool_type} NOT NULL DEFAULT {true_default},
            FOREIGN KEY(user_model_id) REFERENCES user_models(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            CHECK (user_model_id IS NOT NULL OR project_id IS NOT NULL)
        )
        """
    )

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS external_job_queue (
            id {pk_type},
            job_id INTEGER NOT NULL UNIQUE,
            user_model_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            assigned_at TIMESTAMP,
            completed_at TIMESTAMP,
            worker_api_key_id INTEGER,
            worker_identifier TEXT,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            timeout_seconds INTEGER DEFAULT 3600,
            encrypted {bool_type} NOT NULL DEFAULT {false_default},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
            FOREIGN KEY(user_model_id) REFERENCES user_models(id) ON DELETE CASCADE,
            FOREIGN KEY(worker_api_key_id) REFERENCES user_model_api_keys(id) ON DELETE SET NULL
        )
        """
    )

    _ensure_columns(
        cursor,
        "user_models",
        (
            ("model_id", "TEXT"),
            ("user_id", "INTEGER"),
            ("project_id", "INTEGER"),
            ("model_name", "TEXT"),
            ("model_description", "TEXT"),
            ("model_type", "TEXT"),
            ("input_schema", "TEXT"),
            ("output_format", "TEXT"),
            ("outline_color", "TEXT"),
            ("badge", "TEXT"),
            ("badge_color", "TEXT"),
            ("credit_cost", "INTEGER DEFAULT 5"),
            ("estimated_runtime_seconds", "INTEGER DEFAULT 60"),
            ("worker_endpoint_url", "TEXT"),
            ("requires_api_key", f"{bool_type} NOT NULL DEFAULT {true_default}"),
            ("is_active", f"{bool_type} NOT NULL DEFAULT {true_default}"),
            ("is_public", f"{bool_type} NOT NULL DEFAULT {false_default}"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("last_used_at", "TIMESTAMP"),
            ("total_jobs_run", "INTEGER DEFAULT 0"),
        ),
    )

    _ensure_columns(
        cursor,
        "user_model_api_keys",
        (
            ("user_model_id", "INTEGER"),
            ("project_id", "INTEGER"),
            ("user_id", "INTEGER"),
            ("key_id", "TEXT"),
            ("key_hash", "TEXT"),
            ("key_prefix", "TEXT"),
            ("name", "TEXT"),
            ("description", "TEXT"),
            ("is_active", f"{bool_type} NOT NULL DEFAULT {true_default}"),
            ("rate_limit_per_minute", "INTEGER DEFAULT 60"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("last_used_at", "TIMESTAMP"),
            ("total_requests", "INTEGER DEFAULT 0"),
            ("encrypted_key", "TEXT"),
            ("encryption_enabled", f"{bool_type} NOT NULL DEFAULT {true_default}"),
        ),
    )

    _ensure_columns(
        cursor,
        "external_job_queue",
        (
            ("job_id", "INTEGER"),
            ("user_model_id", "INTEGER"),
            ("status", "TEXT DEFAULT 'pending'"),
            ("assigned_at", "TIMESTAMP"),
            ("completed_at", "TIMESTAMP"),
            ("worker_api_key_id", "INTEGER"),
            ("worker_identifier", "TEXT"),
            ("retry_count", "INTEGER DEFAULT 0"),
            ("max_retries", "INTEGER DEFAULT 3"),
            ("timeout_seconds", "INTEGER DEFAULT 3600"),
            ("encrypted", f"{bool_type} NOT NULL DEFAULT {false_default}"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ),
    )

    # Enforce a single fixed credit price for custom user models.
    cursor.execute("UPDATE user_models SET credit_cost = 5 WHERE credit_cost IS NULL OR credit_cost != 5")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_models_model_id ON user_models(model_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_models_user_id ON user_models(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_models_project_id ON user_models(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_models_is_active ON user_models(is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_models_is_public ON user_models(is_public)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_user_model_id ON user_model_api_keys(user_model_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_project_id ON user_model_api_keys(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_user_id ON user_model_api_keys(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_key_hash ON user_model_api_keys(key_hash)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_model_api_keys_is_active ON user_model_api_keys(is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_external_job_queue_job_id ON external_job_queue(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_external_job_queue_user_model_id ON external_job_queue(user_model_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_external_job_queue_status ON external_job_queue(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_external_job_queue_created_at ON external_job_queue(created_at)")


def _apply_legacy_migrations(cursor):
    """Patch legacy schemas created by earlier bootstrap scripts."""

    _maybe_rename_column(cursor, "user_credentials", "password_hash", "password")
    _maybe_rename_column(cursor, "user_credentials", "is_verified", "email_verified")

    # Keyed hash of ip + user agent for the new-device check once login_history is sealed
    # (phonos_app/helpers/login_history.py). NULL and unused until PHONOS_DATA_KEYS is set.
    _ensure_columns(cursor, "login_history", (("device_idx", "TEXT"),))

    _ensure_columns(
        cursor,
        "user_credentials",
        (
            ("user_id", "INTEGER UNIQUE"),
            ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
            ("plan", f"TEXT NOT NULL DEFAULT '{DEFAULT_PLAN}'"),
            ("email_notifications_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("verification_token", "TEXT"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("tos_accepted", "INTEGER NOT NULL DEFAULT 0"),
            ("tos_accepted_at", "TIMESTAMP"),
            ("tos_ip_address", "TEXT"),
        ),
    )

    _ensure_columns(
        cursor,
        "users",
        (
            ("user_id", "TEXT UNIQUE"),
            ("user_credential_id", "INTEGER UNIQUE"),
            ("plan", f"TEXT NOT NULL DEFAULT '{DEFAULT_PLAN}'"),
            ("credits", "INTEGER NOT NULL DEFAULT 0"),
            ("storage_quota", f"INTEGER NOT NULL DEFAULT {int(DEFAULT_STORAGE_QUOTA)}"),
            ("storage_used", "INTEGER NOT NULL DEFAULT 0"),
            ("credits_last_reset", "TIMESTAMP"),
            ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
            ("legal_name", "TEXT"),
            ("address", "TEXT"),
            ("company", "TEXT"),
            ("university", "TEXT"),
            ("age", "INTEGER"),
            ("profile_picture_url", "TEXT"),
            ("primary_color", "TEXT"),
            ("background_image", "TEXT"),
            ("layout_density", "TEXT NOT NULL DEFAULT 'cozy'"),
            ("table_style", "TEXT NOT NULL DEFAULT 'zebra'"),
            ("notifications_last_cleared", "TIMESTAMP"),
            ("last_login_at", "TIMESTAMP"),
            ("last_login_ip", "TEXT"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("stripe_customer_id", "TEXT"),
            ("stripe_subscription_id", "TEXT"),
            ("subscription_status", "TEXT"),
            ("subscription_end_date", "TIMESTAMP"),
            ("trial_end_date", "TIMESTAMP"),
        ),
    )

    _maybe_rename_column(cursor, "projects", "is_personal", "is_private")
    _maybe_rename_column(cursor, "projects", "organization_id", "team_id")
    _ensure_columns(
        cursor,
        "projects",
        (
            ("team_id", "INTEGER"),
            ("organization_id", "INTEGER"),
            ("group_id", "INTEGER"),
            ("creator_id", "INTEGER"),
            ("project_type", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("is_private", "INTEGER NOT NULL DEFAULT 0"),
            ("is_archived", "INTEGER NOT NULL DEFAULT 0"),
            ("archived_at", "TIMESTAMP"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("collaboration_billing_model", "TEXT NOT NULL DEFAULT 'admin_credits'"),
            ("auto_open_chat", "INTEGER NOT NULL DEFAULT 0"),
            ("display_anonymous", "INTEGER NOT NULL DEFAULT 0"),
            ("category_id", "INTEGER"),
            ("view_count", "INTEGER NOT NULL DEFAULT 0"),
            ("download_count", "INTEGER NOT NULL DEFAULT 0"),
            ("vote_score", "INTEGER NOT NULL DEFAULT 0"),
            ("published_at", "TIMESTAMP"),
            ("notes", "TEXT"),
        ),
    )

    is_sqlite = isinstance(cursor, sqlite3.Cursor)
    pk_type = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite else "SERIAL PRIMARY KEY"
    metadata_type = "TEXT" if is_sqlite else "JSONB"
    cost_type = "REAL" if is_sqlite else "DECIMAL(10,6)"

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS batches (
            id {pk_type},
            batch_id VARCHAR(50) UNIQUE NOT NULL,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            creator_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            name VARCHAR(255),
            description TEXT,
            status VARCHAR(50) DEFAULT 'pending',
            total_jobs INTEGER DEFAULT 0,
            completed_jobs INTEGER DEFAULT 0,
            failed_jobs INTEGER DEFAULT 0,
            cancelled_jobs INTEGER DEFAULT 0,
            total_cost {cost_type} DEFAULT 0,
            metadata {metadata_type} DEFAULT '{{}}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_project_id ON batches(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_creator_id ON batches(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_created_at ON batches(created_at)")

    cursor.execute(
        "UPDATE projects SET organization_id = team_id WHERE organization_id IS NULL AND team_id IS NOT NULL"
    )

    cursor.execute("DROP VIEW IF EXISTS organizations")
    cursor.execute(
        """
        CREATE VIEW organizations AS
            SELECT
                t.id,
                t.name,
                t.slug,
                t.description,
                t.creator_id AS owner_id,
                t.created_at,
                t.updated_at
            FROM teams t
        """
    )

    cursor.execute("DROP VIEW IF EXISTS organization_members")
    cursor.execute(
        """
        CREATE VIEW organization_members AS
            SELECT
                tm.id,
                tm.team_id AS organization_id,
                tm.user_id,
                tm.role,
                tm.joined_at
            FROM team_members tm
        """
    )

    cursor.execute("DROP VIEW IF EXISTS organization_invitations")
    cursor.execute(
        """
        CREATE VIEW organization_invitations AS
            SELECT
                ti.id,
                ti.team_id AS organization_id,
                ti.inviter_id,
                ti.invitee_email,
                ti.token,
                ti.role,
                ti.status,
                ti.expires_at,
                ti.created_at
            FROM team_invitations ti
        """
    )

    _ensure_columns(
        cursor,
        "collaboration_sessions",
        (
            ("collaboration_billing_model", "TEXT NOT NULL DEFAULT 'admin_credits'"),
        ),
    )

    _ensure_columns(
        cursor,
        "project_assignments",
        (
            ("role", "TEXT NOT NULL DEFAULT 'Project Contributor'"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ),
    )

    _maybe_rename_column(cursor, "jobs", "user_id", "creator_id")
    job_data_column_type = "BLOB" if isinstance(cursor, sqlite3.Cursor) else "BYTEA"
    _ensure_columns(
        cursor,
        "jobs",
        (
            ("project_id", "INTEGER NOT NULL"),
            ("creator_id", "INTEGER NOT NULL"),
            ("batch_id", "INTEGER"),
            ("pipeline_id", "TEXT"),
            ("pipeline_stage", "INTEGER"),
            ("name", "TEXT"),
            ("model", "TEXT NOT NULL"),
            ("sequence", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'queued'"),
            ("message", "TEXT"),
            ("output_file", "TEXT"),
            ("output_file_size", "INTEGER"),
            ("output_display_name", "TEXT"),
            ("execution_time", "REAL"),
            ("input_file_id", "INTEGER"),
            ("policy", "TEXT"),
            ("cost", "INTEGER DEFAULT 0"),
            ("job_data", job_data_column_type),
            ("metadata", "TEXT"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ),
    )

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch_id ON jobs(batch_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_id ON jobs(pipeline_id)")

    # Pipelines: backfill the richer columns onto pre-existing job_pipelines rows
    # (the table predates this feature and shipped with only the basic columns).
    if not isinstance(cursor, sqlite3.Cursor):
        _ensure_columns(
            cursor,
            "job_pipelines",
            (
                ("definition", "JSONB DEFAULT '{}'"),
                ("seed_input", "JSONB DEFAULT '{}'"),
                ("cost", "INTEGER DEFAULT 100"),
                ("billing_user_id", "INTEGER"),
                ("total_stages", "INTEGER DEFAULT 0"),
                ("current_stage", "INTEGER DEFAULT 0"),
                ("total_jobs", "INTEGER DEFAULT 0"),
                ("completed_jobs", "INTEGER DEFAULT 0"),
                ("failed_jobs", "INTEGER DEFAULT 0"),
                ("safety_cap", "INTEGER"),
                ("metadata", "JSONB DEFAULT '{}'"),
            ),
        )

    _ensure_columns(
        cursor,
        "user_privacy_settings",
        (
            ("allow_invitations", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_project_chat_messages", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_direct_messages", "INTEGER NOT NULL DEFAULT 1"),
            ("show_online_status", "INTEGER NOT NULL DEFAULT 1"),
            ("show_profile_picture", "INTEGER NOT NULL DEFAULT 1"),
            ("show_last_seen", "INTEGER NOT NULL DEFAULT 0"),
            ("allow_team_discovery", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_project_discovery", "INTEGER NOT NULL DEFAULT 0"),
            ("allow_email_notifications", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_collaboration_requests", "INTEGER NOT NULL DEFAULT 1"),
            ("profile_visibility", "TEXT NOT NULL DEFAULT 'public'"),
            ("project_visibility_default", "TEXT NOT NULL DEFAULT 'private'"),
            ("data_sharing_analytics", "INTEGER NOT NULL DEFAULT 1"),
            ("data_sharing_third_party", "INTEGER NOT NULL DEFAULT 0"),
            ("display_username_public_projects", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ),
    )

    _ensure_columns(
        cursor,
        "notifications",
        (
            ("metadata", "TEXT"),
        ),
    )

    _ensure_columns(
        cursor,
        "shared_links",
        (
            ("password", "TEXT"),
            ("allow_downloads", "INTEGER NOT NULL DEFAULT 1"),
            ("description", "TEXT"),
        ),
    )

    _ensure_user_model_tables(cursor)

    # Create indexes for Reddit-like features after columns are ensured to exist
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_category_id ON projects(category_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_vote_score ON projects(vote_score)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_is_private ON projects(is_private)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_published_at ON projects(published_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_votes_project_id ON project_votes(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_votes_user_id ON project_votes(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_comments_project_id ON project_comments(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_comments_parent_id ON project_comments(parent_comment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_comment_votes_comment_id ON project_comment_votes(comment_id)")

    # Notebook execution tracking: msg_id is required for exact matching of
    # kernel execute_reply messages so overlapping cell runs update the right row.
    _ensure_columns(
        cursor,
        "notebook_executions",
        (
            ("msg_id", "TEXT"),
            ("duration_ms", "INTEGER"),
            ("tags", "TEXT DEFAULT '[]'"),
            ("annotation", "TEXT"),
        ),
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_executions_msg_id ON notebook_executions(msg_id)")


def _create_schema(cursor):
    """Create the primary schema required by the application."""

    # PostgreSQL schema - using SERIAL for auto-increment
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            user_id TEXT UNIQUE NOT NULL,
            user_credential_id INTEGER UNIQUE,
            username TEXT UNIQUE NOT NULL,
            legal_name TEXT,
            address TEXT,
            company TEXT,
            university TEXT,
            age INTEGER,
            plan TEXT NOT NULL DEFAULT '{DEFAULT_PLAN}',
            credits INTEGER NOT NULL DEFAULT 0,
            storage_quota INTEGER NOT NULL DEFAULT {int(DEFAULT_STORAGE_QUOTA)},
            storage_used INTEGER NOT NULL DEFAULT 0,
            credits_last_reset TIMESTAMP,
            is_admin INTEGER NOT NULL DEFAULT 0,
            profile_picture_url TEXT,
            primary_color TEXT,
            background_image TEXT,
            layout_density TEXT NOT NULL DEFAULT 'cozy',
            table_style TEXT NOT NULL DEFAULT 'zebra',
            notifications_last_cleared TIMESTAMP,
            last_login_at TIMESTAMP,
            last_login_ip TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            stripe_customer_id TEXT UNIQUE,
            stripe_subscription_id TEXT,
            subscription_status TEXT,
            subscription_end_date TIMESTAMP,
            trial_end_date TIMESTAMP,
            auth_provider TEXT NOT NULL DEFAULT 'local',
            google_id TEXT UNIQUE
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS user_credentials (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE,
            email TEXT UNIQUE NOT NULL,
            password BYTEA NOT NULL,
            email_verified INTEGER NOT NULL DEFAULT 0,
            plan TEXT NOT NULL DEFAULT '{DEFAULT_PLAN}',
            email_notifications_enabled INTEGER NOT NULL DEFAULT 1,
            verification_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tos_accepted INTEGER NOT NULL DEFAULT 0,
            tos_accepted_at TIMESTAMP,
            tos_ip_address TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS two_factor_auth (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            secret_key TEXT NOT NULL,
            is_enabled INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id SERIAL PRIMARY KEY,
            user_credential_id INTEGER NOT NULL REFERENCES user_credentials(id) ON DELETE CASCADE,
            token TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_token
            ON password_reset_tokens(token)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            id SERIAL PRIMARY KEY,
            identifier TEXT NOT NULL,
            action TEXT NOT NULL,
            attempts INTEGER DEFAULT 1,
            window_start TIMESTAMP NOT NULL,
            last_attempt TIMESTAMP NOT NULL,
            UNIQUE(identifier, action)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_lockouts (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            failed_attempts INTEGER DEFAULT 1,
            first_failed_at TIMESTAMP NOT NULL,
            last_failed_at TIMESTAMP NOT NULL,
            locked_until TIMESTAMP,
            UNIQUE(username, ip_address)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            description TEXT,
            creator_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("DROP VIEW IF EXISTS organizations")
    cursor.execute("""
        CREATE VIEW organizations AS
            SELECT
                t.id,
                t.name,
                t.slug,
                t.description,
                t.creator_id AS owner_id,
                t.created_at,
                t.updated_at
            FROM teams t
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS team_members (
            id SERIAL PRIMARY KEY,
            team_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(team_id, user_id)
        )
    """)

    cursor.execute("DROP VIEW IF EXISTS organization_members")
    cursor.execute("""
        CREATE VIEW organization_members AS
            SELECT
                tm.id,
                tm.team_id AS organization_id,
                tm.user_id,
                tm.role,
                tm.joined_at
            FROM team_members tm
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS team_invitations (
            id SERIAL PRIMARY KEY,
            team_id INTEGER NOT NULL,
            inviter_id INTEGER NOT NULL,
            invitee_email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            role TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            FOREIGN KEY(inviter_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("DROP VIEW IF EXISTS organization_invitations")
    cursor.execute("""
        CREATE VIEW organization_invitations AS
            SELECT
                ti.id,
                ti.team_id AS organization_id,
                ti.inviter_id,
                ti.invitee_email,
                ti.token,
                ti.role,
                ti.status,
                ti.expires_at,
                ti.created_at
            FROM team_invitations ti
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id SERIAL PRIMARY KEY,
            team_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            UNIQUE(team_id, name)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_groups (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            team_id INTEGER,
            name TEXT NOT NULL,
            description TEXT,
            color TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_categories (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            description TEXT,
            icon TEXT,
            color TEXT,
            parent_id INTEGER,
            display_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(parent_id) REFERENCES project_categories(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            team_id INTEGER,
            organization_id INTEGER,
            group_id INTEGER,
            creator_id INTEGER,
            name TEXT NOT NULL,
            description TEXT,
            project_type TEXT NOT NULL DEFAULT 'legacy',
            is_private INTEGER NOT NULL DEFAULT 1,
            is_archived INTEGER NOT NULL DEFAULT 0,
            archived_at TIMESTAMP,
            collaboration_billing_model TEXT NOT NULL DEFAULT 'admin_credits',
            auto_open_chat INTEGER NOT NULL DEFAULT 0,
            display_anonymous INTEGER NOT NULL DEFAULT 0,
            category_id INTEGER,
            view_count INTEGER NOT NULL DEFAULT 0,
            download_count INTEGER NOT NULL DEFAULT 0,
            vote_score INTEGER NOT NULL DEFAULT 0,
            published_at TIMESTAMP,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE SET NULL,
            FOREIGN KEY(organization_id) REFERENCES teams(id) ON DELETE SET NULL,
            FOREIGN KEY(group_id) REFERENCES project_groups(id) ON DELETE SET NULL,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)

    # The project_type column may not exist yet on databases created before
    # this migration. _apply_legacy_migrations adds it via _ensure_columns
    # right after _create_schema returns; only create the index if the column
    # is already present (otherwise the whole transaction would abort on PG).
    # The index will be created on the next boot once the column exists.
    if 'project_type' in _get_table_columns(cursor, 'projects'):
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_type ON projects(project_type)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_assignments (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'Project Contributor',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(project_id, user_id)
        )
    """)

    cursor.execute("DROP VIEW IF EXISTS project_members")
    cursor.execute("""
        CREATE VIEW project_members AS
            SELECT
                id,
                project_id,
                user_id,
                CASE
                    WHEN role = 'Project Manager' THEN 'owner'
                    WHEN role = 'Project Contributor' THEN 'collaborator'
                    WHEN role = 'Project Viewer' THEN 'spectator'
                    ELSE role
                END AS role,
                created_at
            FROM project_assignments
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_votes (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            vote_type INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(project_id, user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_comments (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            parent_comment_id INTEGER,
            content TEXT NOT NULL,
            vote_score INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_comment_id) REFERENCES project_comments(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_comment_votes (
            id SERIAL PRIMARY KEY,
            comment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            vote_type INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(comment_id) REFERENCES project_comments(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(comment_id, user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_tags (
            project_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (project_id, tag_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bulk_job_schedules (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            cron_schedule TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_run_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            id SERIAL PRIMARY KEY,
            batch_id VARCHAR(50) UNIQUE NOT NULL,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            creator_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            name VARCHAR(255),
            description TEXT,
            status VARCHAR(50) DEFAULT 'pending',
            total_jobs INTEGER DEFAULT 0,
            completed_jobs INTEGER DEFAULT 0,
            failed_jobs INTEGER DEFAULT 0,
            cancelled_jobs INTEGER DEFAULT 0,
            total_cost DECIMAL(10,6) DEFAULT 0.000000,
            metadata JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_project_id ON batches(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_creator_id ON batches(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batches_created_at ON batches(created_at)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            job_id TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            batch_id INTEGER,
            pipeline_id TEXT,
            pipeline_stage INTEGER,
            name TEXT,
            model TEXT NOT NULL,
            sequence TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            message TEXT,
            output_file TEXT,
            output_file_size INTEGER,
            output_display_name TEXT,
            execution_time REAL,
            input_file_id INTEGER,
            policy TEXT,
            cost INTEGER DEFAULT 0,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(input_file_id) REFERENCES files(id) ON DELETE SET NULL,
            FOREIGN KEY(batch_id) REFERENCES batches(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS model_overrides (
            model_id TEXT PRIMARY KEY,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_visible INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_tags (
            job_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (job_id, tag_id),
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_logs (
            id SERIAL PRIMARY KEY,
            job_id TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Job Bunches - Group multiple jobs together within a project
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_bunches (
            id SERIAL PRIMARY KEY,
            bunch_id TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            total_jobs INTEGER DEFAULT 0,
            completed_jobs INTEGER DEFAULT 0,
            failed_jobs INTEGER DEFAULT 0,
            total_cost INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 2,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_bunch_items (
            id SERIAL PRIMARY KEY,
            bunch_id INTEGER NOT NULL,
            job_id INTEGER,
            sequence_order INTEGER DEFAULT 0,
            input_name TEXT,
            input_sequence TEXT,
            input_file_id INTEGER,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(bunch_id) REFERENCES job_bunches(id) ON DELETE CASCADE,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
            FOREIGN KEY(input_file_id) REFERENCES files(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_bunches_project_id ON job_bunches(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_bunches_creator_id ON job_bunches(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_bunches_status ON job_bunches(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_bunch_items_bunch_id ON job_bunch_items(bunch_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_bunch_items_job_id ON job_bunch_items(job_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flows (
            id SERIAL PRIMARY KEY,
            flow_id TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            flow_data TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flow_executions (
            id SERIAL PRIMARY KEY,
            execution_id TEXT UNIQUE NOT NULL,
            flow_id TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            current_step INTEGER DEFAULT 0,
            total_steps INTEGER NOT NULL,
            error_message TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flow_execution_steps (
            id SERIAL PRIMARY KEY,
            execution_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            step_type TEXT NOT NULL,
            step_config TEXT NOT NULL,
            job_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            error_message TEXT,
            UNIQUE(execution_id, step_index)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_flows_project_id ON flows(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_flows_creator_id ON flows(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_flow_executions_flow_id ON flow_executions(flow_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_flow_executions_project_id ON flow_executions(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_flow_execution_steps_execution_id ON flow_execution_steps(execution_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS direct_messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'text',
            attachment_url TEXT,
            attachment_name TEXT,
            attachment_size INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(recipient_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_messages (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'text',
            file_id INTEGER,
            gemini_response INTEGER NOT NULL DEFAULT 0,
            parent_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE SET NULL,
            FOREIGN KEY(parent_message_id) REFERENCES project_messages(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_messages_project_id ON project_messages(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_messages_created_at ON project_messages(created_at)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_attachments (
            id SERIAL PRIMARY KEY,
            message_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            thumbnail_path TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(message_id) REFERENCES project_messages(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS call_sessions (
            id SERIAL PRIMARY KEY,
            call_type TEXT NOT NULL,
            project_id INTEGER,
            initiator_id INTEGER NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'active',
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(initiator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS call_participants (
            id SERIAL PRIMARY KEY,
            call_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            left_at TIMESTAMP,
            is_muted INTEGER NOT NULL DEFAULT 0,
            is_video_on INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(call_id) REFERENCES call_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_reactions (
            id SERIAL PRIMARY KEY,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reaction TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(message_id) REFERENCES project_messages(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(message_id, user_id, reaction)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pinned_messages (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            pinned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(message_id) REFERENCES project_messages(id) ON DELETE CASCADE,
            UNIQUE(project_id, message_id)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_message_attachments_message_id ON message_attachments(message_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_sessions_project_id ON call_sessions(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_participants_call_id ON call_participants(call_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pinned_messages_project_id ON pinned_messages(project_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invitations (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            inviter_id INTEGER NOT NULL,
            invitee_email TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(inviter_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            category TEXT DEFAULT 'system',
            related_job_id TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promo_code_usage (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            code_hash TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            plan_granted TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customer_nodes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            node_id TEXT NOT NULL UNIQUE,
            public_key TEXT,
            approved INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS team_nodes (
            id SERIAL PRIMARY KEY,
            team_id INTEGER NOT NULL,
            node_id TEXT NOT NULL,
            display_name TEXT,
            worker_types TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            UNIQUE(team_id, node_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS help_tickets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'Medium',
            category TEXT,
            status TEXT NOT NULL DEFAULT 'Open',
            assignee_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(assignee_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS help_ticket_replies (
            id SERIAL PRIMARY KEY,
            ticket_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(ticket_id) REFERENCES help_tickets(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS help_ticket_attachments (
            id SERIAL PRIMARY KEY,
            ticket_id INTEGER NOT NULL,
            reply_id INTEGER,
            file_path TEXT NOT NULL,
            friendly_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(ticket_id) REFERENCES help_tickets(id) ON DELETE CASCADE,
            FOREIGN KEY(reply_id) REFERENCES help_ticket_replies(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_warnings (
            id SERIAL PRIMARY KEY,
            message TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'warning',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_pipelines (
            id SERIAL PRIMARY KEY,
            pipeline_id TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            definition JSONB DEFAULT '{}',
            seed_input JSONB DEFAULT '{}',
            cost INTEGER DEFAULT 100,
            billing_user_id INTEGER,
            total_stages INTEGER DEFAULT 0,
            current_stage INTEGER DEFAULT 0,
            total_jobs INTEGER DEFAULT 0,
            completed_jobs INTEGER DEFAULT 0,
            failed_jobs INTEGER DEFAULT 0,
            safety_cap INTEGER,
            metadata JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_pipeline_steps (
            id SERIAL PRIMARY KEY,
            pipeline_id INTEGER NOT NULL,
            step_order INTEGER NOT NULL,
            job_id INTEGER,
            model TEXT NOT NULL,
            config TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            depends_on_step_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(pipeline_id) REFERENCES job_pipelines(id) ON DELETE CASCADE,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
            FOREIGN KEY(depends_on_step_id) REFERENCES job_pipeline_steps(id) ON DELETE SET NULL
        )
    """)

    # Per-internal-job lineage for multi-stage pipelines (matrix expansion).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_jobs (
            id SERIAL PRIMARY KEY,
            pipeline_pk INTEGER NOT NULL REFERENCES job_pipelines(id) ON DELETE CASCADE,
            stage_index INTEGER NOT NULL,
            category TEXT NOT NULL,
            model TEXT NOT NULL,
            job_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            parent_pipeline_job_ids INTEGER[] DEFAULT '{}',
            combo_label TEXT,
            rank_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pipeline_pk, stage_index, job_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_pipeline_pk ON pipeline_jobs(pipeline_pk)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_job_id ON pipeline_jobs(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_stage ON pipeline_jobs(pipeline_pk, stage_index)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_pipelines_project_id ON job_pipelines(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_pipelines_creator_id ON job_pipelines(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_pipelines_status ON job_pipelines(status)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_templates (
            id SERIAL PRIMARY KEY,
            template_id TEXT UNIQUE NOT NULL,
            creator_id INTEGER NOT NULL,
            project_id INTEGER,
            team_id INTEGER,
            name TEXT NOT NULL,
            description TEXT,
            model TEXT NOT NULL,
            config TEXT NOT NULL,
            is_public INTEGER NOT NULL DEFAULT 0,
            use_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shared_links (
            id SERIAL PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            permissions TEXT NOT NULL DEFAULT 'read',
            expires_at TIMESTAMP,
            password TEXT,
            allow_downloads INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            access_count INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(creator_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS resource_annotations (
            id SERIAL PRIMARY KEY,
            resource_type TEXT NOT NULL,
            resource_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            position_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_versions (
            id SERIAL PRIMARY KEY,
            file_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            checksum TEXT,
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(file_id, version_number)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_preferences (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE NOT NULL,
            widgets_config TEXT NOT NULL,
            layout_config TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_privacy_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE NOT NULL,
            allow_invitations INTEGER NOT NULL DEFAULT 1,
            allow_project_chat_messages INTEGER NOT NULL DEFAULT 1,
            allow_direct_messages INTEGER NOT NULL DEFAULT 1,
            show_online_status INTEGER NOT NULL DEFAULT 1,
            show_profile_picture INTEGER NOT NULL DEFAULT 1,
            show_last_seen INTEGER NOT NULL DEFAULT 0,
            allow_team_discovery INTEGER NOT NULL DEFAULT 1,
            allow_project_discovery INTEGER NOT NULL DEFAULT 0,
            allow_email_notifications INTEGER NOT NULL DEFAULT 1,
            allow_collaboration_requests INTEGER NOT NULL DEFAULT 1,
            profile_visibility TEXT NOT NULL DEFAULT 'public',
            project_visibility_default TEXT NOT NULL DEFAULT 'private',
            data_sharing_analytics INTEGER NOT NULL DEFAULT 1,
            data_sharing_third_party INTEGER NOT NULL DEFAULT 0,
            display_username_public_projects INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            key_id TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            scopes TEXT,
            last_used_at TIMESTAMP,
            expires_at TIMESTAMP,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_rate_limits (
            id SERIAL PRIMARY KEY,
            key_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            window_start TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_analytics (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            project_id INTEGER,
            job_id INTEGER,
            event_type TEXT NOT NULL,
            credits_used INTEGER NOT NULL DEFAULT 0,
            storage_delta INTEGER NOT NULL DEFAULT 0,
            compute_time REAL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS budget_alerts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            team_id INTEGER,
            alert_type TEXT NOT NULL,
            threshold_value INTEGER NOT NULL,
            current_value INTEGER NOT NULL DEFAULT 0,
            is_triggered INTEGER NOT NULL DEFAULT 0,
            last_triggered_at TIMESTAMP,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_permissions (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id INTEGER,
            permission_level TEXT NOT NULL,
            granted_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documentation_pages (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL,
            search_vector TEXT,
            view_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collaboration_sessions (
            id SERIAL PRIMARY KEY,
            session_id TEXT UNIQUE NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id INTEGER NOT NULL,
            host_user_id INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            collaboration_billing_model TEXT NOT NULL DEFAULT 'admin_credits',
            FOREIGN KEY(host_user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collaboration_participants (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            left_at TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notebooks (
            id SERIAL PRIMARY KEY,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            creator_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            name TEXT NOT NULL DEFAULT 'Untitled Notebook',
            description TEXT,
            kernel_type TEXT DEFAULT 'python3',
            metadata JSONB DEFAULT '{}',
            cells JSONB DEFAULT '[]',
            settings JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_deleted INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notebook_sessions (
            id SERIAL PRIMARY KEY,
            notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            container_id TEXT,
            session_token TEXT UNIQUE NOT NULL,
            kernel_status TEXT DEFAULT 'starting',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            workspace_path TEXT,
            error_message TEXT,
            runtime_level TEXT DEFAULT 'basic',
            resource_config JSONB DEFAULT '{}'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notebook_files (
            id SERIAL PRIMARY KEY,
            notebook_id INTEGER REFERENCES notebooks(id) ON DELETE CASCADE,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT,
            size_bytes INTEGER DEFAULT 0,
            mime_type TEXT,
            metadata JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(notebook_id, file_path)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notebook_executions (
            id SERIAL PRIMARY KEY,
            session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
            cell_id TEXT NOT NULL,
            msg_id TEXT,
            execution_count INTEGER,
            source TEXT NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            status TEXT DEFAULT 'running',
            outputs JSONB DEFAULT '[]',
            error_output TEXT,
            duration_ms INTEGER,
            tags JSONB DEFAULT '[]',
            annotation TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notebook_session_cleanup (
            id SERIAL PRIMARY KEY,
            session_id INTEGER REFERENCES notebook_sessions(id) ON DELETE CASCADE,
            cleanup_scheduled_at TIMESTAMP NOT NULL,
            cleanup_executed_at TIMESTAMP,
            workspace_deleted INTEGER DEFAULT 0,
            files_archived INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notebook_history (
            id SERIAL PRIMARY KEY,
            notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            commit_hash TEXT UNIQUE NOT NULL,
            parent_hash TEXT,
            message TEXT DEFAULT '',
            action TEXT NOT NULL DEFAULT 'auto_save',
            cells_snapshot TEXT NOT NULL DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_history_notebook ON notebook_history(notebook_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_history_hash ON notebook_history(commit_hash)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_history (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id INTEGER,
            resource_name TEXT DEFAULT '',
            description TEXT DEFAULT '',
            snapshot_path TEXT,
            metadata TEXT DEFAULT '{}',
            retention_until TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_history_project ON project_history(project_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_history_resource ON project_history(resource_type, resource_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            activity_type TEXT NOT NULL,
            activity_description TEXT NOT NULL,
            resource_type TEXT,
            resource_id INTEGER,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_creator_id ON jobs(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    # Composite indexes for the hot "list jobs by project/creator, newest first"
    # access pattern: serves the WHERE filter and the ORDER BY created_at DESC
    # in a single index scan (no separate sort).
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_project_id_created_at ON jobs(project_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_creator_id_created_at ON jobs(creator_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_steps_pipeline_id ON job_pipeline_steps(pipeline_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shared_links_token ON shared_links(token)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_annotations_resource ON resource_annotations(resource_type, resource_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_versions_file_id ON file_versions(file_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_analytics_user_id ON usage_analytics(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_analytics_created_at ON usage_analytics(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_collaboration_sessions_resource ON collaboration_sessions(resource_type, resource_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_user_id ON activity_log(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_activity_type ON activity_log(activity_type)")

    # Missing indexes on frequently queried foreign keys
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebooks_project_id ON notebooks(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebooks_user_id ON notebooks(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_sessions_notebook_id ON notebook_sessions(notebook_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_sessions_user_id ON notebook_sessions(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_files_notebook_id ON notebook_files(notebook_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_files_project_id ON notebook_files(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notebook_executions_session_id ON notebook_executions(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_files_project_id ON files(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_files_creator_id ON files(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_credentials_user_id ON user_credentials(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_credentials_email ON user_credentials(email)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_teams_creator_id ON teams(creator_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_team_members_team_id ON team_members(team_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_team_members_user_id ON team_members(user_id)")

    # User connections/contacts for direct calling
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_connections (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            connected_user_id INTEGER NOT NULL,
            connection_type TEXT NOT NULL DEFAULT 'contact',
            status TEXT NOT NULL DEFAULT 'accepted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(connected_user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, connected_user_id)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_connections_user_id ON user_connections(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_connections_status ON user_connections(status)")

    # Call invitations for direct calls
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS call_invitations (
            id SERIAL PRIMARY KEY,
            call_id INTEGER NOT NULL,
            inviter_id INTEGER NOT NULL,
            invitee_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            responded_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(call_id) REFERENCES call_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(inviter_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(invitee_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_invitations_call_id ON call_invitations(call_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_invitations_invitee_id ON call_invitations(invitee_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_invitations_status ON call_invitations(status)")

    # Public profile customization
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public_profile_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE NOT NULL,
            show_bio INTEGER NOT NULL DEFAULT 1,
            show_affiliation INTEGER NOT NULL DEFAULT 1,
            show_location INTEGER NOT NULL DEFAULT 0,
            show_public_projects INTEGER NOT NULL DEFAULT 1,
            show_statistics INTEGER NOT NULL DEFAULT 1,
            show_badges INTEGER NOT NULL DEFAULT 1,
            show_activity INTEGER NOT NULL DEFAULT 0,
            show_social_links INTEGER NOT NULL DEFAULT 1,
            bio TEXT,
            affiliation TEXT,
            location TEXT,
            website_url TEXT,
            twitter_handle TEXT,
            github_handle TEXT,
            linkedin_url TEXT,
            custom_theme_color TEXT,
            custom_banner_image TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_public_profile_settings_user_id ON public_profile_settings(user_id)")

    # Project profile customization (what to show on public projects)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_profile_settings (
            id SERIAL PRIMARY KEY,
            project_id INTEGER UNIQUE NOT NULL,
            show_description INTEGER NOT NULL DEFAULT 1,
            show_collaborators INTEGER NOT NULL DEFAULT 1,
            show_files INTEGER NOT NULL DEFAULT 1,
            show_jobs INTEGER NOT NULL DEFAULT 1,
            show_statistics INTEGER NOT NULL DEFAULT 1,
            show_activity INTEGER NOT NULL DEFAULT 1,
            show_comments INTEGER NOT NULL DEFAULT 1,
            show_fork_button INTEGER NOT NULL DEFAULT 1,
            show_download_button INTEGER NOT NULL DEFAULT 1,
            custom_sections TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_profile_settings_project_id ON project_profile_settings(project_id)")

    # Molecule image/SDF/properties cache for MolMIM & GenMol 2D/3D ligand display
    # (see production/migrations/017_molecule_cache.sql). Folded in here so it
    # self-applies on boot — migrations/ are NOT auto-run, only _create_schema is.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS molecule_cache (
            id SERIAL PRIMARY KEY,
            smiles_hash VARCHAR(64) NOT NULL UNIQUE,
            smiles TEXT NOT NULL,
            image_png BYTEA,
            sdf_data TEXT,
            properties_json TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            last_accessed TIMESTAMP DEFAULT NOW()
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_molecule_cache_hash ON molecule_cache (smiles_hash)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_molecule_cache_accessed ON molecule_cache (last_accessed)")

    # Per-user Wolfram Cloud credentials (AES-256-GCM encrypted at rest)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_wolfram_credentials (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            username_enc TEXT NOT NULL,
            password_enc TEXT NOT NULL,
            wolfram_id_email TEXT,
            last_verified_at TIMESTAMP,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_wolfram_creds_user ON user_wolfram_credentials(user_id)")

    # Per-user Quantum execution settings (non-secret) — Quantum Settings UI
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_quantum_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            execution_mode TEXT NOT NULL DEFAULT 'simulator',
            credential_source TEXT NOT NULL DEFAULT 'platform',
            preferred_backend TEXT NOT NULL DEFAULT 'automatic',
            auto_start_session INTEGER NOT NULL DEFAULT 0,
            session_timeout_sec INTEGER NOT NULL DEFAULT 60,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_quantum_settings_user ON user_quantum_settings(user_id)")

    # Per-user IBM Quantum credentials (AES-256-GCM encrypted at rest). The
    # api_key is never returned to the frontend, logs, or APIs.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_ibm_credentials (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            api_key_enc TEXT NOT NULL,
            instance_crn TEXT,
            preferred_backend TEXT,
            last_verified_at TIMESTAMP,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_ibm_creds_user ON user_ibm_credentials(user_id)")

    # Ensure Google OAuth columns exist on the users table (for databases
    # created before these columns were added to CREATE TABLE).
    _ensure_columns(cursor, "users", (
        ("auth_provider", "TEXT NOT NULL DEFAULT 'local'"),
        ("google_id", "TEXT UNIQUE"),
    ))

    # ────────────────────────────────────────────────────────────────────
    # EDUCATION SYSTEM (migration 022) — a third project type that turns
    # the Notebook IDE into a WebWork-style assignment platform.
    # Folded in here so it self-applies on boot (migrations/ are NOT auto-run).
    # ────────────────────────────────────────────────────────────────────

    # Per-user education flag (self-service: any user can enable to become a teacher)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_education_settings (
            id                          SERIAL PRIMARY KEY,
            user_id                     INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            education_enabled           INTEGER NOT NULL DEFAULT 0,
            default_late_policy         TEXT    NOT NULL DEFAULT 'none',
            default_time_limit_minutes  INTEGER,
            created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_education_settings_user ON user_education_settings(user_id)")

    # Assignments: a notebook promoted to an assignment within an education project
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS education_assignments (
            id                          SERIAL PRIMARY KEY,
            notebook_id                 INTEGER NOT NULL UNIQUE REFERENCES notebooks(id) ON DELETE CASCADE,
            project_id                  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title                       TEXT NOT NULL,
            description                 TEXT,
            open_at                     TIMESTAMP,
            close_at                    TIMESTAMP,
            time_limit_minutes          INTEGER,
            late_policy                 TEXT    NOT NULL DEFAULT 'none',
            max_submissions_per_problem INTEGER NOT NULL DEFAULT 1,
            total_points                REAL    NOT NULL DEFAULT 0,
            published                   INTEGER NOT NULL DEFAULT 0,
            created_by                  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_edu_assignments_project ON education_assignments(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_edu_assignments_notebook ON education_assignments(notebook_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_edu_assignments_published ON education_assignments(published, open_at, close_at)")

    # Problems: cells inside an assignment notebook with type-specific grading config
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assignment_problems (
            id              SERIAL PRIMARY KEY,
            assignment_id   INTEGER NOT NULL REFERENCES education_assignments(id) ON DELETE CASCADE,
            cell_id         TEXT NOT NULL,
            problem_type    TEXT NOT NULL,
            config          JSONB NOT NULL DEFAULT '{}',
            points          REAL    NOT NULL DEFAULT 1,
            sort_order      INTEGER NOT NULL DEFAULT 0,
            time_limit_minutes INTEGER,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(assignment_id, cell_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assignment_problems_assignment ON assignment_problems(assignment_id)")

    # Submissions: one row per attempt per problem per student
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assignment_submissions (
            id                  SERIAL PRIMARY KEY,
            assignment_id       INTEGER NOT NULL REFERENCES education_assignments(id) ON DELETE CASCADE,
            problem_id          INTEGER NOT NULL REFERENCES assignment_problems(id) ON DELETE CASCADE,
            student_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            attempt_number      INTEGER NOT NULL DEFAULT 1,
            answer              TEXT,
            auto_score          REAL,
            final_score         REAL,
            status              TEXT NOT NULL DEFAULT 'pending',
            submitted_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            graded_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
            feedback            TEXT,
            time_started        TIMESTAMP,
            time_remaining_sec  INTEGER,
            is_correct          BOOLEAN,
            UNIQUE(assignment_id, problem_id, student_id, attempt_number)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assignment_submissions_assignment ON assignment_submissions(assignment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assignment_submissions_student ON assignment_submissions(student_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assignment_submissions_problem_student ON assignment_submissions(problem_id, student_id, attempt_number DESC)")

    # Gradebook: aggregate per student per assignment
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assignment_grades (
            id              SERIAL PRIMARY KEY,
            assignment_id   INTEGER NOT NULL REFERENCES education_assignments(id) ON DELETE CASCADE,
            student_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            total_score     REAL NOT NULL DEFAULT 0,
            max_score       REAL NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'in_progress',
            time_started    TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(assignment_id, student_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assignment_grades_assignment ON assignment_grades(assignment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assignment_grades_student ON assignment_grades(student_id)")

    # ────────────────────────────────────────────────────────────────────
    # TRUSTED TESTER PROGRAM (migration 023) — applicant → tester → regular.
    # See phonos_app/helpers/trusted_tester.py. Existing accounts default to
    # 'regular'; new ones start as 'applicant' only while TRUSTED_TESTER_GATE=true.
    # ────────────────────────────────────────────────────────────────────
    _ensure_columns(cursor, "users", (
        ("access_tier", "TEXT NOT NULL DEFAULT 'regular'"),
    ))
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trusted_tester_applications (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            status          TEXT    NOT NULL DEFAULT 'pending',
            motivation      TEXT,
            institution     TEXT,
            use_case        TEXT,
            applied_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at     TIMESTAMP,
            review_note     TEXT,
            graduated_at    TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trusted_tester_status ON trusted_tester_applications(status)")


def init_db():
    """Initialize the database with the required schema.

    This function:
    1. Validates the database configuration
    2. Creates all required tables and indexes
    3. Applies any legacy migrations for backwards compatibility
    4. Initializes the connection pool
    """
    logger.info("Starting database initialization...")

    # Validate configuration first
    try:
        validate_database_config()
    except Exception as e:
        logger.error(f"Database configuration validation failed: {e}")
        raise

    using_sqlite = _using_sqlite()

    if using_sqlite:
        db_path = _resolve_database_file()
        Config.DATABASE_FILE = db_path
        Config.DATABASE_URL = f"sqlite:///{db_path}"
        if os.environ.get("TESTING", "").strip().lower() in ("1", "true", "yes"):
            import tempfile
            job_output_dir = os.path.join(tempfile.gettempdir(), "phonos-test-job-outputs")
        else:
            job_output_dir = os.environ.get("JOB_OUTPUT_DIR") or os.path.join(os.path.dirname(os.path.abspath(db_path)), "job_outputs")
        os.makedirs(job_output_dir, exist_ok=True)
        Config.JOB_OUTPUT_DIR = job_output_dir
        Config.JOB_OUTPUT_DIR_FALLBACK = job_output_dir
        try:
            current_app.config["DATABASE_FILE"] = db_path
            current_app.config["DATABASE_URL"] = Config.DATABASE_URL
            current_app.config["JOB_OUTPUT_DIR"] = job_output_dir
            current_app.config["JOB_OUTPUT_DIR_FALLBACK"] = job_output_dir
        except RuntimeError:
            pass

    # Create schema
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        logger.info("Creating database schema...")
        if using_sqlite:
            _create_sqlite_schema(cursor)
        else:
            _create_schema(cursor)

        logger.info("Applying legacy migrations...")
        _apply_legacy_migrations(cursor)

        conn.commit()
        logger.info("Database schema created successfully")

        if using_sqlite:
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'")
            table_count = _get_row_value(cursor.fetchone())
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type = 'index'")
            index_count = _get_row_value(cursor.fetchone())
        else:
            cursor.execute("""
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """)
            table_count = _get_row_value(cursor.fetchone(), 'count')

            cursor.execute("""
                SELECT count(*) FROM pg_indexes
                WHERE schemaname = 'public'
            """)
            index_count = _get_row_value(cursor.fetchone(), 'count')

        logger.info(f"Database initialized: {table_count} tables, {index_count} indexes")

    except (psycopg2.Error, sqlite3.Error) as e:
        conn.rollback()
        logger.error(f"Database initialization failed: {e}")
        raise
    finally:
        conn.close()

    # Initialize the connection pool
    if not using_sqlite:
        try:
            _get_connection_pool()
            logger.info("Connection pool ready")
        except Exception as e:
            logger.warning(f"Connection pool initialization deferred: {e}")

    return True


def reset_db():
    """Drop all tables and recreate the schema (DANGEROUS - for development only)."""
    if not Config.DEV_MODE:
        raise RuntimeError("reset_db() can only be called in development mode")

    logger.warning("Resetting database - all data will be lost!")

    conn = psycopg2.connect(_resolve_database_url())
    try:
        cursor = conn.cursor()

        # Get all tables
        cursor.execute("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
        """)
        tables = [_get_row_value(row, 'tablename') for row in cursor.fetchall()]

        # Drop all tables
        for table in tables:
            cursor.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
            logger.info(f"Dropped table: {table}")

        conn.commit()
        logger.info("All tables dropped")

    finally:
        conn.close()

    # Reinitialize
    init_db()
    logger.info("Database reset complete")


if __name__ == "__main__":
    # Set up logging for standalone execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    import argparse
    parser = argparse.ArgumentParser(description='Database initialization utility')
    parser.add_argument('--health', action='store_true', help='Check database health')
    parser.add_argument('--validate', action='store_true', help='Validate configuration only')
    args = parser.parse_args()

    if args.health:
        health = check_database_health()
        print(f"Status: {health['status']}")
        print(f"Message: {health['message']}")
        if health['details']:
            print("Details:")
            for key, value in health['details'].items():
                print(f"  {key}: {value}")
        sys.exit(0 if health['status'] == 'healthy' else 1)

    elif args.validate:
        try:
            validate_database_config()
            print("Database configuration is valid")
            sys.exit(0)
        except Exception as e:
            print(f"Configuration error: {e}")
            sys.exit(1)

    else:
        try:
            init_db()
            print("Database initialized successfully")

            # Show health status
            health = check_database_health()
            print(f"\nHealth check: {health['status']}")
            if health['details']:
                print(f"  Database size: {health['details'].get('database_size', 'N/A')}")
                print(f"  Active connections: {health['details'].get('active_connections', 'N/A')}")

        except Exception as e:
            print(f"Initialization failed: {e}")
            sys.exit(1)
