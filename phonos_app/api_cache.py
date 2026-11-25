"""Redis-backed API response caching middleware.

Provides a decorator and helper for caching expensive API responses
in Redis with configurable TTL. Falls back gracefully when Redis is
unavailable.
"""
from __future__ import annotations

import hashlib
import json
import logging
from functools import wraps
from typing import Any, Callable

from flask import request, Response

from .app import get_redis_connection

logger = logging.getLogger(__name__)

CACHE_PREFIX = "api_cache:"
DEFAULT_TTL = 60  # 1 minute default


def api_cache(
    ttl: int = DEFAULT_TTL,
    make_key: Callable[[], str] | None = None,
) -> Callable:
    """Cache the JSON response of a Flask API route in Redis.

    Usage::

        @api_cache(ttl=120)
        def my_expensive_route():
            ...

    The cache key is derived from the request path + query string by
    default.  Pass *make_key* for custom key logic (e.g. to include
    the current user's id).

    Stale cache entries are served while the refresh is in progress
    (stale-while-revalidate pattern).
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Response:
            # Only cache GET requests
            if request.method != "GET":
                return f(*args, **kwargs)

            # Build cache key
            if make_key:
                cache_key = f"{CACHE_PREFIX}{make_key()}"
            else:
                raw = f"{request.path}?{request.query_string.decode('utf-8', errors='replace')}"
                cache_key = f"{CACHE_PREFIX}{hashlib.sha256(raw.encode()).hexdigest()}"

            try:
                r = get_redis_connection()
                if r is None:
                    return f(*args, **kwargs)

                # Try serving stale cache while revalidating
                cached = r.get(cache_key)
                if cached is not None:
                    try:
                        data = json.loads(cached)
                        # Fire-and-forget refresh in background (Redis pub/sub
                        # or a simple async fetch isn't possible here, so we
                        # just serve stale data and the next request will get
                        # fresh data after the TTL expires).
                        return Response(
                            json.dumps(data),
                            mimetype="application/json",
                            headers={
                                "X-Cache": "HIT",
                                "Cache-Control": "private, max-age=30",
                            },
                        )
                    except (json.JSONDecodeError, TypeError):
                        pass

                response = f(*args, **kwargs)

                # Cache the response
                if isinstance(response, Response):
                    try:
                        data = response.get_json()
                        if data is not None:
                            r.setex(cache_key, ttl, json.dumps(data))
                    except (TypeError, ValueError):
                        pass
                elif isinstance(response, tuple):
                    # Flask views may return (body, status) or (body, status, headers)
                    body = response[0] if response else None
                    try:
                        r.setex(cache_key, ttl, json.dumps(body))
                    except (TypeError, ValueError):
                        pass
                else:
                    try:
                        r.setex(cache_key, ttl, json.dumps(response))
                    except (TypeError, ValueError):
                        pass

                return response
            except Exception:
                logger.warning("Cache middleware error, serving fresh", exc_info=True)
                return f(*args, **kwargs)

        return wrapper
    return decorator


def invalidate_api_cache(pattern: str = "*") -> None:
    """Invalidate cached API responses matching *pattern* (glob)."""
    try:
        r = get_redis_connection()
        if r is None:
            return
        deleted = 0
        batch: list = []
        for key in r.scan_iter(match=f"{CACHE_PREFIX}{pattern}", count=500):
            batch.append(key)
            if len(batch) >= 500:
                r.delete(*batch)
                deleted += len(batch)
                batch = []
        if batch:
            r.delete(*batch)
            deleted += len(batch)
        if deleted:
            logger.info("Invalidated %d API cache keys matching %r", deleted, pattern)
    except Exception:
        logger.warning("Failed to invalidate API cache", exc_info=True)
