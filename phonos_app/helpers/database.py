"""Database session helpers with connection pooling and robust error handling."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

import psycopg2
from psycopg2 import pool, OperationalError, InterfaceError, DatabaseError
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import ISOLATION_LEVEL_READ_COMMITTED

from flask import g

from ..app import app

logger = logging.getLogger(__name__)

# Connection pool configuration
_connection_pool: Optional[pool.ThreadedConnectionPool] = None
POOL_MIN_CONNECTIONS = int(os.environ.get('DB_POOL_MIN', 2))
POOL_MAX_CONNECTIONS = int(os.environ.get('DB_POOL_MAX', 20))
CONNECTION_TIMEOUT = 10
STATEMENT_TIMEOUT = 30000  # 30 seconds in milliseconds
# Auto-abort transactions left idle (e.g. a request that errored without
# rolling back) so a pooled connection can't sit "idle in transaction"
# holding locks and bloating the server. 60s is comfortably above the
# 30s statement timeout.
IDLE_IN_TXN_TIMEOUT = 60000  # 60 seconds in milliseconds
MAX_RETRIES = 3
RETRY_DELAY = 0.5  # seconds


def _normalize_sqlite_query(query: str) -> str:
    """Translate PostgreSQL-oriented SQL into SQLite-friendly syntax."""
    normalized = query.replace("%s", "?")
    normalized = normalized.replace(" ILIKE ", " LIKE ")
    normalized = normalized.replace(" = TRUE", " = 1")
    normalized = normalized.replace(" = FALSE", " = 0")
    normalized = normalized.replace(" TRUE", " 1")
    normalized = normalized.replace(" FALSE", " 0")
    normalized = normalized.replace("GREATEST(", "MAX(")
    normalized = re.sub(
        r"\bBIGSERIAL\s+PRIMARY\s+KEY\b",
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bSERIAL\s+PRIMARY\s+KEY\b",
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\bJSONB\b", "TEXT", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bTIMESTAMPTZ\b", "TIMESTAMP", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", normalized, flags=re.IGNORECASE)
    # PostgreSQL JSONB text extraction -> SQLite json_extract
    normalized = re.sub(
        r"(\w+)::jsonb->>'([^']+)'",
        r"json_extract(\1, '$.\2')",
        normalized,
    )
    return normalized


class SQLiteCompatCursor:
    """Cursor wrapper that accepts both SQLite and PostgreSQL-style parameter markers."""

    def __init__(self, cursor):
        self._cursor = cursor
        self._cached_rows = None

    def _handle_information_schema_columns(self, query, params):
        match = re.search(r"table_name\s*=\s*'([^']+)'", query, re.IGNORECASE)
        table_name = match.group(1) if match else None

        if table_name is None and params:
            table_name = params[0]

        if not table_name:
            return False

        pragma_rows = self._cursor.execute(f"PRAGMA table_info({table_name})").fetchall()
        rows = [{'column_name': row['name'] if isinstance(row, sqlite3.Row) else row[1]} for row in pragma_rows]

        column_match = re.search(r"column_name\s*=\s*'([^']+)'", query, re.IGNORECASE)
        column_name = column_match.group(1) if column_match else None
        if column_name is None and params and len(params) > 1:
            column_name = params[1]
        if column_name:
            rows = [row for row in rows if row['column_name'] == column_name]

        self._cached_rows = rows
        return True

    def execute(self, query, params=None):
        self._cached_rows = None
        if 'information_schema.columns' in query.lower():
            self._handle_information_schema_columns(query, params)
            return self

        normalized_query = _normalize_sqlite_query(query)
        if 'insert into teams' in normalized_query.lower():
            normalized_query = normalized_query.replace('owner_id', 'creator_id')
            lower_query = normalized_query.lower()
            if 'insert into teams (name, creator_id)' in lower_query:
                name_match = re.search(r"values\s*\(\s*'([^']+)'", normalized_query, re.IGNORECASE)
                team_name = name_match.group(1) if name_match else 'team'
                team_slug = re.sub(r'[^a-z0-9]+', '-', team_name.lower()).strip('-') or 'team'
                normalized_query = normalized_query.replace(
                    'INSERT INTO teams (name, creator_id)',
                    'INSERT INTO teams (name, slug, creator_id)',
                )
                # The query has already been normalized so placeholders are '?',
                # but keep '%s' in the pattern to handle raw PostgreSQL-style SQL.
                normalized_query = re.sub(
                    rf"VALUES\s*\(\s*'{re.escape(team_name)}'\s*,\s*(%s|\?)\s*\)",
                    rf"VALUES ('{team_name}', ?, \1)",
                    normalized_query,
                    flags=re.IGNORECASE,
                )
                params = (team_slug, *params) if params else (team_slug,)

        is_insert = normalized_query.lstrip().upper().startswith('INSERT')
        if is_insert and 'RETURNING' not in normalized_query.upper():
            returning_query = normalized_query.rstrip().rstrip(';') + ' RETURNING id'
            try:
                if params is None:
                    self._cursor.execute(returning_query)
                else:
                    self._cursor.execute(returning_query, params)
                row = self._cursor.fetchone()
                self._cached_rows = [row] if row is not None else []
                return self
            except sqlite3.OperationalError:
                pass

        if params is None:
            self._cursor.execute(normalized_query)
        else:
            self._cursor.execute(normalized_query, params)
        if is_insert and 'RETURNING' in normalized_query.upper():
            row = self._cursor.fetchone()
            self._cached_rows = [row] if row is not None else []
        return self

    def executemany(self, query, params_list):
        self._cursor.executemany(_normalize_sqlite_query(query), params_list)
        return self

    def executescript(self, script):
        self._cursor.executescript(script)
        return self

    def fetchone(self):
        if self._cached_rows is not None:
            if not self._cached_rows:
                return None
            return self._cached_rows.pop(0)
        return self._cursor.fetchone()

    def fetchall(self):
        if self._cached_rows is not None:
            rows = self._cached_rows
            self._cached_rows = []
            return rows
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        if size is None:
            return self._cursor.fetchmany()
        return self._cursor.fetchmany(size)

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        if self._cached_rows is not None:
            return len(self._cached_rows)
        return self._cursor.rowcount

    def __iter__(self):
        return iter(self._cursor)


class SQLiteCompatCursorExecutor:
    """Provide SQLite cursor semantics with the same interface as the PostgreSQL helper."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, params=None):
        return self._connection.execute(query, params)

    def fetchone(self):
        raise NotImplementedError("Use execute() first, then fetchone() on the returned cursor")

    def fetchall(self):
        raise NotImplementedError("Use execute() first, then fetchall() on the returned cursor")

    @property
    def lastrowid(self):
        raise NotImplementedError("Access lastrowid from the cursor returned by execute()")


class SQLiteCompatConnection:
    """SQLite connection wrapper with placeholder normalization."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, query, params=None):
        cursor = self._conn.cursor()
        return SQLiteCompatCursor(cursor).execute(query, params)

    def executemany(self, query, params_list):
        cursor = self._conn.cursor()
        return SQLiteCompatCursor(cursor).executemany(query, params_list)

    def executescript(self, sql_script):
        self._conn.executescript(sql_script)

    def cursor(self):
        return SQLiteCompatCursorExecutor(self)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value


def _use_sqlite() -> bool:
    """Return True when the current app configuration should use SQLite."""
    if app.config.get("TESTING") and app.config.get("DATABASE_FILE"):
        return True

    db_url = app.config.get("DATABASE_URL")
    return bool(db_url and db_url.startswith("sqlite"))


def _resolve_sqlite_path() -> str:
    """Resolve the SQLite file path from app configuration."""
    db_path = app.config.get("DATABASE_FILE") or "phonos.db"
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(db_path)
    return db_path


class PostgreSQLConnection:
    """
    Wrapper around psycopg2 connection that provides SQLite-like interface.
    Automatically converts %s placeholders to %s for PostgreSQL compatibility.
    Includes retry logic for deadlocks and transient errors.
    """

    def __init__(self, conn):
        self._conn = conn
        self._cursor = None

    def _is_retryable_error(self, error) -> bool:
        """Check if an error is retryable (deadlock, serialization failure, etc.)."""
        if not isinstance(error, psycopg2.Error):
            return False

        # PostgreSQL error codes for retryable errors
        # 40001 = serialization_failure
        # 40P01 = deadlock_detected
        # 57014 = query_canceled (might be retryable in some cases)
        retryable_codes = ['40001', '40P01']

        if hasattr(error, 'pgcode') and error.pgcode in retryable_codes:
            return True

        return False

    def execute(self, query, params=None):
        """Execute a query with retry logic for transient errors.

        Automatically converts %s placeholders to %s for PostgreSQL compatibility.
        Retries on deadlocks and serialization failures.
        """
        # Convert SQLite-style ? placeholders to PostgreSQL-style %s
        converted_query = query.replace('?', '%s')

        # For INSERT statements, add RETURNING id to get the lastrowid
        is_insert = converted_query.strip().upper().startswith('INSERT')

        # Retry logic for transient errors
        retries = 0
        last_error = None

        while retries < MAX_RETRIES:
            try:
                if is_insert and 'RETURNING' not in converted_query.upper():
                    query_with_returning = converted_query.rstrip(';').rstrip() + ' RETURNING id'
                    cursor = self._conn.cursor()
                    try:
                        if params:
                            cursor.execute(query_with_returning, params)
                        else:
                            cursor.execute(query_with_returning)
                        return PostgreSQLCursor(cursor, is_insert=is_insert)
                    except psycopg2.errors.UndefinedColumn:
                        # Table doesn't have 'id' column, retry without RETURNING
                        self._conn.rollback()
                        cursor = self._conn.cursor()
                        if params:
                            cursor.execute(converted_query, params)
                        else:
                            cursor.execute(converted_query)
                        return PostgreSQLCursor(cursor, is_insert=False)

                cursor = self._conn.cursor()
                if params:
                    cursor.execute(converted_query, params)
                else:
                    cursor.execute(converted_query)
                return PostgreSQLCursor(cursor, is_insert=is_insert)

            except Exception as e:
                last_error = e

                # Check if this is a retryable error
                if self._is_retryable_error(e):
                    retries += 1
                    if retries < MAX_RETRIES:
                        logger.warning(f"Retryable database error (attempt {retries}/{MAX_RETRIES}): {e}")
                        self._conn.rollback()
                        time.sleep(RETRY_DELAY * retries)
                        continue

                # Not retryable or out of retries, rollback and raise the error
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

        # Should not reach here, but if we do, raise the last error
        raise last_error

    def executemany(self, query, params_list):
        """Execute a query multiple times with different parameters with retry logic."""
        converted_query = query.replace('?', '%s')

        retries = 0
        last_error = None

        while retries < MAX_RETRIES:
            try:
                cursor = self._conn.cursor()
                cursor.executemany(converted_query, params_list)
                return PostgreSQLCursor(cursor)

            except Exception as e:
                last_error = e

                # Check if this is a retryable error
                if self._is_retryable_error(e):
                    retries += 1
                    if retries < MAX_RETRIES:
                        logger.warning(f"Retryable database error in executemany (attempt {retries}/{MAX_RETRIES}): {e}")
                        self._conn.rollback()
                        time.sleep(RETRY_DELAY * retries)
                        continue

                # Not retryable or out of retries, rollback and raise the error
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

        # Should not reach here, but if we do, raise the last error
        raise last_error

    def cursor(self):
        """Return a new cursor wrapper for executing multiple queries."""
        return PostgreSQLCursorExecutor(self)

    def commit(self):
        """Commit the current transaction."""
        self._conn.commit()

    def rollback(self):
        """Rollback the current transaction."""
        self._conn.rollback()

    def close(self):
        """Close the connection."""
        self._conn.close()

    @property
    def row_factory(self):
        """Compatibility property."""
        return None

    @row_factory.setter
    def row_factory(self, value):
        """Compatibility property setter (no-op for PostgreSQL)."""
        pass


class PostgreSQLCursorExecutor:
    """
    Cursor wrapper that allows multiple execute() calls (SQLite-like interface).
    """

    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, params=None):
        """Execute a query and return a PostgreSQLCursor."""
        return self._connection.execute(query, params)

    def fetchone(self):
        """Compatibility method (not typically used after cursor())."""
        raise NotImplementedError("Use execute() first, then fetchone() on the returned cursor")

    def fetchall(self):
        """Compatibility method (not typically used after cursor())."""
        raise NotImplementedError("Use execute() first, then fetchall() on the returned cursor")

    @property
    def lastrowid(self):
        """Compatibility property."""
        raise NotImplementedError("Access lastrowid from the cursor returned by execute()")


class PostgreSQLCursor:
    """
    Wrapper around psycopg2 cursor that provides dict-like row access.
    """

    def __init__(self, cursor, is_insert=False):
        self._cursor = cursor
        self._lastrowid = None
        self._cached_row = None  # Cache the first row for INSERT operations
        # If this was an INSERT with RETURNING, fetch the id
        if is_insert:
            try:
                row = cursor.fetchone()
                if row:
                    self._cached_row = row  # Cache the row so it can be fetched again
                    if 'id' in row:
                        self._lastrowid = row['id']
            except Exception:
                pass

    def fetchone(self):
        """Fetch one row, returning it as a dict-like object."""
        # If we have a cached row from INSERT, return it first
        if self._cached_row is not None:
            row = self._cached_row
            self._cached_row = None  # Clear cache after first fetch
            return row
        row = self._cursor.fetchone()
        return row  # RealDictCursor already returns dict-like rows

    def fetchall(self):
        """Fetch all rows, returning them as dict-like objects."""
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        """Fetch many rows."""
        if size:
            return self._cursor.fetchmany(size)
        return self._cursor.fetchmany()

    @property
    def lastrowid(self):
        """Get the last inserted row ID (PostgreSQL uses RETURNING)."""
        return self._lastrowid

    @property
    def rowcount(self):
        """Get the number of rows affected."""
        return self._cursor.rowcount

    def __iter__(self):
        return iter(self._cursor)


def _get_connection_pool():
    """Get or create the connection pool (thread-safe)."""
    global _connection_pool

    if _connection_pool is None:
        try:
            logger.info(f"Initializing PostgreSQL connection pool (min={POOL_MIN_CONNECTIONS}, max={POOL_MAX_CONNECTIONS})")
            _connection_pool = pool.ThreadedConnectionPool(
                minconn=POOL_MIN_CONNECTIONS,
                maxconn=POOL_MAX_CONNECTIONS,
                dsn=app.config["DATABASE_URL"],
                cursor_factory=RealDictCursor,
                connect_timeout=CONNECTION_TIMEOUT,
                # Apply session limits once per physical connection instead of
                # issuing a SET on every checkout (saves a round-trip per request).
                options=(
                    f"-c statement_timeout={STATEMENT_TIMEOUT} "
                    f"-c idle_in_transaction_session_timeout={IDLE_IN_TXN_TIMEOUT}"
                ),
            )
            logger.info("PostgreSQL connection pool initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize connection pool: {e}")
            raise

    return _connection_pool


def _is_connection_alive(conn) -> bool:
    """Check if a connection is still alive and usable."""
    try:
        # Use a simple query to test the connection
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        return True
    except (OperationalError, InterfaceError, DatabaseError):
        return False


def _get_raw_connection():
    """Get a connection from the pool with validation and retry logic."""
    if _use_sqlite():
        conn = sqlite3.connect(
            _resolve_sqlite_path(),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    retries = 0
    last_error = None

    while retries < MAX_RETRIES:
        try:
            pool_obj = _get_connection_pool()
            conn = pool_obj.getconn()

            # Cheap, local staleness check (no server round-trip): psycopg2 sets
            # conn.closed != 0 once it knows the socket is dead. A `SELECT 1`
            # liveness probe on every checkout was removed because it added a DB
            # round-trip to every request; app and Postgres share localhost so a
            # connection dying while idle in the pool is rare, and execute() still
            # rolls back + surfaces errors if it does happen.
            if conn.closed:
                logger.warning("Got closed connection from pool, discarding and retrying")
                try:
                    pool_obj.putconn(conn, close=True)
                except Exception:
                    pass
                retries += 1
                time.sleep(RETRY_DELAY * retries)
                continue

            # Configure connection. Only roll back when the connection actually
            # has an open/aborted transaction (avoids a wasted ROLLBACK round-trip
            # on the common case where teardown already left it idle).
            try:
                if conn.info.transaction_status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                    conn.rollback()
            except Exception:
                pass
            conn.set_session(isolation_level=ISOLATION_LEVEL_READ_COMMITTED, autocommit=False)

            # statement_timeout / idle_in_transaction_session_timeout are applied
            # once per physical connection via the pool's `options` (see
            # _get_connection_pool), so no per-checkout SET round-trip is needed.

            return conn

        except pool.PoolError as e:
            logger.warning(f"Connection pool exhausted (attempt {retries + 1}/{MAX_RETRIES}): {e}")
            last_error = e
            retries += 1
            if retries < MAX_RETRIES:
                time.sleep(RETRY_DELAY * retries)
        except (OperationalError, InterfaceError) as e:
            logger.warning(f"Connection error (attempt {retries + 1}/{MAX_RETRIES}): {e}")
            last_error = e
            retries += 1
            if retries < MAX_RETRIES:
                time.sleep(RETRY_DELAY * retries)

    # All retries failed, raise the last error
    logger.error(f"Failed to get database connection after {MAX_RETRIES} attempts")
    raise last_error or OperationalError("Failed to get database connection")


def _release_connection(conn, close_conn=False):
    """Release a connection back to the pool."""
    try:
        pool_obj = _get_connection_pool()
        pool_obj.putconn(conn, close=close_conn)
    except Exception as e:
        logger.warning(f"Error releasing connection to pool: {e}")
        try:
            conn.close()
        except Exception:
            pass


def get_db():
    """Get a database connection wrapper for the current request context.

    This function ensures one connection per Flask request context,
    with automatic cleanup on request teardown.
    """
    if "db" not in g:
        try:
            conn = _get_raw_connection()
            if _use_sqlite():
                g.db = SQLiteCompatConnection(conn)
                g.db_from_pool = False
            else:
                g.db = PostgreSQLConnection(conn)
                g.db_from_pool = True
        except Exception as e:
            logger.error(f"Failed to get database connection: {e}")
            raise

    return g.db


@contextmanager
def transaction():
    """Context manager for explicit transaction management with automatic rollback on error.

    Usage:
        with transaction() as db:
            db.execute("INSERT INTO users (name) VALUES (%s)", ("John",))
            db.execute("UPDATE users SET credits = credits + 10 WHERE name = %s", ("John",))
        # Automatically commits on success or rolls back on exception
    """
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


@app.teardown_appcontext
def close_db(e=None):
    """Close the database connection and return it to the pool."""
    db = g.pop("db", None)
    from_pool = g.pop("db_from_pool", False)

    if db is not None:
        try:
            # Get the underlying connection
            conn = db._conn

            # If there was an error, rollback any pending transaction
            if e is not None:
                try:
                    conn.rollback()
                except Exception as rollback_error:
                    logger.warning(f"Error during rollback: {rollback_error}")

            # Return to pool or close
            if from_pool:
                _release_connection(conn, close_conn=(e is not None))
            else:
                conn.close()
        except Exception as close_error:
            logger.error(f"Error closing database connection: {close_error}")


def close_connection_pool():
    """Close all connections in the pool.

    This should be called during application shutdown.
    """
    global _connection_pool

    if _connection_pool is not None:
        try:
            logger.info("Closing database connection pool")
            _connection_pool.closeall()
            _connection_pool = None
            logger.info("Database connection pool closed successfully")
        except Exception as e:
            logger.error(f"Error closing connection pool: {e}")


def check_database_health() -> dict:
    """Check database connectivity and return health status.

    Returns:
        dict with keys:
            - status: 'healthy', 'unhealthy', or 'error'
            - message: Human-readable status message
            - details: Additional diagnostic information
    """
    result = {
        'status': 'unknown',
        'message': '',
        'details': {}
    }

    try:
        # Get a connection and test it
        db = get_db()

        # Check basic connectivity
        db.execute("SELECT 1").fetchone()

        if _using_sqlite():
            ver_row = db.execute("SELECT sqlite_version()").fetchone()
            sqlite_ver = ver_row[0] if ver_row else sqlite3.sqlite_version
            db_file = getattr(Config, "DATABASE_FILE", "phonos.db")
            size_bytes = os.path.getsize(db_file) if db_file and os.path.exists(db_file) else 0
            result['status'] = 'healthy'
            result['message'] = 'SQLite database connection successful'
            result['details'] = {
                'backend': 'sqlite',
                'version': f'SQLite {sqlite_ver}',
                'active_connections': 1,
                'database_size': f'{size_bytes / (1024 * 1024):.2f} MB',
                'long_running_queries': 0,
            }
            return result

        # Get PostgreSQL version
        version_row = db.execute("SELECT version()").fetchone()
        version = version_row[0] if version_row else "Unknown"

        # Get active connection count
        conn_row = db.execute("""
            SELECT count(*) as active_connections
            FROM pg_stat_activity
            WHERE datname = current_database()
        """).fetchone()
        active_conns = conn_row['active_connections'] if conn_row else 0

        # Get database size
        size_row = db.execute("""
            SELECT pg_size_pretty(pg_database_size(current_database())) as db_size
        """).fetchone()
        db_size = size_row['db_size'] if size_row else "Unknown"

        # Check for long-running queries
        long_query_row = db.execute("""
            SELECT count(*) as long_queries
            FROM pg_stat_activity
            WHERE state = 'active'
              AND query_start < now() - interval '1 minute'
              AND datname = current_database()
        """).fetchone()
        long_queries = long_query_row['long_queries'] if long_query_row else 0

        result['status'] = 'healthy'
        result['message'] = 'Database connection successful'
        result['details'] = {
            'version': version,
            'active_connections': active_conns,
            'database_size': db_size,
            'long_running_queries': long_queries,
            'pool_min': POOL_MIN_CONNECTIONS,
            'pool_max': POOL_MAX_CONNECTIONS,
        }

    except OperationalError as e:
        result['status'] = 'unhealthy'
        result['message'] = f'Connection failed: {str(e)}'
        logger.error(f"Database health check failed: {e}")
    except Exception as e:
        result['status'] = 'error'
        result['message'] = f'Health check error: {str(e)}'
        logger.error(f"Database health check error: {e}")

    return result


def get_job_status_counts(db) -> dict:
    """Get counts of jobs by status.

    Returns:
        dict with keys: processing, running, queued, submitted
    """
    try:
        result = db.execute("""
            SELECT status, COUNT(*) as count
            FROM jobs
            WHERE status IN ('processing', 'running', 'queued', 'submitted')
            GROUP BY status
        """).fetchall()

        counts = {
            'processing': 0,
            'running': 0,
            'queued': 0,
            'submitted': 0
        }

        for row in result:
            status = row['status']
            if status in counts:
                counts[status] = row['count']

        return counts
    except Exception as e:
        logger.warning(f"Error getting job status counts: {e}")
        return {
            'processing': 0,
            'running': 0,
            'queued': 0,
            'submitted': 0
        }


def get_pool_stats() -> dict:
    """Get statistics about the connection pool.

    Returns:
        dict with pool statistics or None if pool not initialized
    """
    if _connection_pool is None:
        return {
            'initialized': False,
            'message': 'Connection pool not initialized'
        }

    try:
        # Note: ThreadedConnectionPool doesn't expose detailed stats,
        # but we can provide basic configuration info
        return {
            'initialized': True,
            'min_connections': POOL_MIN_CONNECTIONS,
            'max_connections': POOL_MAX_CONNECTIONS,
            'connection_timeout': CONNECTION_TIMEOUT,
            'statement_timeout': STATEMENT_TIMEOUT,
        }
    except Exception as e:
        logger.error(f"Error getting pool stats: {e}")
        return {
            'initialized': True,
            'error': str(e)
        }
