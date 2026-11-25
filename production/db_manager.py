"""
Enhanced Database Manager for Phonos
Provides connection pooling, query optimization, caching, and better interconnection
"""

import psycopg2
from psycopg2 import pool, extras
from contextlib import contextmanager
from functools import wraps
import time
import logging
import json
from typing import Optional, Dict, List, Any, Tuple
from threading import Lock

logger = logging.getLogger(__name__)


class DatabaseConnectionManager:
    """
    Enhanced database connection manager with:
    - Connection pooling with health checks
    - Query result caching
    - Query performance monitoring
    - Automatic retry logic
    - Read/write splitting support
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.pool = None
        self.read_pool = None  # For read replicas
        self.query_cache = {}
        self.cache_lock = Lock()
        self.query_stats = {}  # Track query performance
        self._initialize_pools()

    def _initialize_pools(self):
        """Initialize connection pools with health checks"""
        try:
            # Main pool (read/write)
            self.pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=self.config.get('DB_POOL_MIN', 10),
                maxconn=self.config.get('DB_POOL_MAX', 50),
                host=self.config.get('DB_HOST'),
                database=self.config.get('DB_NAME'),
                user=self.config.get('DB_USER'),
                password=self.config.get('DB_PASSWORD'),
                port=self.config.get('DB_PORT', 5432),
                connect_timeout=10,
                options='-c statement_timeout=30000'  # 30 second query timeout
            )

            # Read replica pool (if configured)
            if self.config.get('DB_READ_HOST'):
                self.read_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=self.config.get('DB_READ_POOL_MIN', 5),
                    maxconn=self.config.get('DB_READ_POOL_MAX', 30),
                    host=self.config.get('DB_READ_HOST'),
                    database=self.config.get('DB_NAME'),
                    user=self.config.get('DB_USER'),
                    password=self.config.get('DB_PASSWORD'),
                    port=self.config.get('DB_PORT', 5432),
                    connect_timeout=10
                )

            logger.info("Database connection pools initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize database pools: {e}")
            raise

    @contextmanager
    def get_connection(self, readonly=False):
        """
        Get a database connection from the pool

        Args:
            readonly: If True, use read replica if available

        Yields:
            psycopg2 connection with RealDictCursor
        """
        pool_to_use = self.read_pool if (readonly and self.read_pool) else self.pool
        conn = None

        try:
            conn = pool_to_use.getconn()
            conn.autocommit = False
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Database connection error: {e}")
            raise
        finally:
            if conn:
                pool_to_use.putconn(conn)

    @contextmanager
    def get_cursor(self, readonly=False):
        """
        Get a cursor with automatic connection management

        Args:
            readonly: If True, use read replica if available

        Yields:
            RealDictCursor for dictionary-like row access
        """
        with self.get_connection(readonly=readonly) as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            try:
                yield cursor
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise
            finally:
                cursor.close()

    def execute_query(
        self,
        query: str,
        params: Optional[Tuple] = None,
        readonly: bool = False,
        fetch: str = 'all'
    ) -> Optional[List[Dict]]:
        """
        Execute a query with performance tracking

        Args:
            query: SQL query string
            params: Query parameters
            readonly: Use read replica if available
            fetch: 'all', 'one', or 'none'

        Returns:
            Query results as list of dicts (or single dict for 'one')
        """
        start_time = time.time()

        try:
            with self.get_cursor(readonly=readonly) as cursor:
                cursor.execute(query, params or ())

                # Fetch results based on type
                if fetch == 'all':
                    result = cursor.fetchall()
                elif fetch == 'one':
                    result = cursor.fetchone()
                else:
                    result = None

                # Track query performance
                duration = time.time() - start_time
                self._track_query_stats(query, duration)

                if duration > 1.0:  # Log slow queries
                    logger.warning(f"Slow query ({duration:.2f}s): {query[:100]}")

                return result

        except Exception as e:
            logger.error(f"Query execution failed: {e}\nQuery: {query}")
            raise

    def execute_batch(
        self,
        query: str,
        params_list: List[Tuple],
        page_size: int = 100
    ) -> None:
        """
        Execute batch inserts/updates efficiently

        Args:
            query: SQL query with placeholders
            params_list: List of parameter tuples
            page_size: Number of rows per batch
        """
        with self.get_cursor() as cursor:
            extras.execute_batch(cursor, query, params_list, page_size=page_size)

    def execute_values(
        self,
        query: str,
        values: List[Tuple],
        template: Optional[str] = None
    ) -> None:
        """
        Fast bulk insert using execute_values

        Args:
            query: INSERT query with VALUES placeholder
            values: List of value tuples
            template: Optional template for value formatting
        """
        with self.get_cursor() as cursor:
            extras.execute_values(cursor, query, values, template=template)

    def cached_query(
        self,
        query: str,
        params: Optional[Tuple] = None,
        ttl: int = 300
    ) -> Optional[List[Dict]]:
        """
        Execute query with result caching

        Args:
            query: SQL query
            params: Query parameters
            ttl: Cache TTL in seconds

        Returns:
            Cached or fresh query results
        """
        cache_key = f"{query}:{params}"

        with self.cache_lock:
            # Check cache
            if cache_key in self.query_cache:
                cached_data, timestamp = self.query_cache[cache_key]
                if time.time() - timestamp < ttl:
                    logger.debug(f"Cache hit for query: {query[:50]}")
                    return cached_data

        # Execute query
        result = self.execute_query(query, params, readonly=True)

        # Cache result
        with self.cache_lock:
            self.query_cache[cache_key] = (result, time.time())

        return result

    def invalidate_cache(self, pattern: Optional[str] = None):
        """
        Invalidate cached queries

        Args:
            pattern: Optional pattern to match cache keys (clears all if None)
        """
        with self.cache_lock:
            if pattern:
                keys_to_remove = [k for k in self.query_cache if pattern in k]
                for key in keys_to_remove:
                    del self.query_cache[key]
                logger.debug(f"Invalidated {len(keys_to_remove)} cached queries matching: {pattern}")
            else:
                self.query_cache.clear()
                logger.debug("Cleared all cached queries")

    @contextmanager
    def transaction(self):
        """
        Context manager for transactions with savepoint support

        Usage:
            with db_manager.transaction():
                # Multiple queries here
                # Auto-commit on success, rollback on exception
        """
        with self.get_connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"Transaction failed: {e}")
                raise

    def _track_query_stats(self, query: str, duration: float):
        """Track query performance for monitoring"""
        query_key = query[:100]  # Use first 100 chars as key

        if query_key not in self.query_stats:
            self.query_stats[query_key] = {
                'count': 0,
                'total_time': 0,
                'max_time': 0,
                'min_time': float('inf')
            }

        stats = self.query_stats[query_key]
        stats['count'] += 1
        stats['total_time'] += duration
        stats['max_time'] = max(stats['max_time'], duration)
        stats['min_time'] = min(stats['min_time'], duration)

    def get_query_stats(self) -> Dict[str, Any]:
        """Get query performance statistics"""
        return {
            query: {
                **stats,
                'avg_time': stats['total_time'] / stats['count']
            }
            for query, stats in self.query_stats.items()
        }

    def health_check(self) -> bool:
        """Check database connectivity"""
        try:
            with self.get_cursor(readonly=True) as cursor:
                cursor.execute('SELECT 1')
                return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False

    def close_pools(self):
        """Close all connection pools"""
        if self.pool:
            self.pool.closeall()
        if self.read_pool:
            self.read_pool.closeall()
        logger.info("Database connection pools closed")


class QueryBuilder:
    """
    Simple query builder to avoid common SQL pitfalls
    """

    def __init__(self, table: str):
        self.table = table
        self.select_fields = ['*']
        self.where_conditions = []
        self.where_params = []
        self.order_by_clause = None
        self.limit_value = None
        self.offset_value = None
        self.join_clauses = []

    def select(self, *fields):
        """Specify fields to select"""
        self.select_fields = list(fields)
        return self

    def where(self, condition: str, *params):
        """Add WHERE condition"""
        self.where_conditions.append(condition)
        self.where_params.extend(params)
        return self

    def order_by(self, field: str, direction: str = 'ASC'):
        """Add ORDER BY clause"""
        self.order_by_clause = f"{field} {direction}"
        return self

    def limit(self, count: int):
        """Add LIMIT clause"""
        self.limit_value = count
        return self

    def offset(self, count: int):
        """Add OFFSET clause"""
        self.offset_value = count
        return self

    def join(self, table: str, condition: str, join_type: str = 'LEFT'):
        """Add JOIN clause"""
        self.join_clauses.append(f"{join_type} JOIN {table} ON {condition}")
        return self

    def build(self) -> Tuple[str, Tuple]:
        """Build the SQL query"""
        fields = ', '.join(self.select_fields)
        query = f"SELECT {fields} FROM {self.table}"

        # Add JOINs
        if self.join_clauses:
            query += ' ' + ' '.join(self.join_clauses)

        # Add WHERE
        if self.where_conditions:
            query += ' WHERE ' + ' AND '.join(self.where_conditions)

        # Add ORDER BY
        if self.order_by_clause:
            query += f' ORDER BY {self.order_by_clause}'

        # Add LIMIT and OFFSET
        if self.limit_value:
            query += f' LIMIT {self.limit_value}'
        if self.offset_value:
            query += f' OFFSET {self.offset_value}'

        return query, tuple(self.where_params)


# Helper decorators
def retry_on_db_error(max_retries=3, delay=1):
    """Decorator to retry queries on transient database errors"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(f"Database error (attempt {attempt + 1}/{max_retries}): {e}")
                    time.sleep(delay * (attempt + 1))  # Exponential backoff
            return None
        return wrapper
    return decorator


def log_query_time(func):
    """Decorator to log query execution time"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        duration = time.time() - start
        if duration > 0.5:
            logger.info(f"{func.__name__} took {duration:.2f}s")
        return result
    return wrapper
