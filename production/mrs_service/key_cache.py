"""Public key lookup and caching utilities for worker validation."""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from .runtime import Config, logger

# Cache entry format: (public_key, timestamp)
PUBLIC_KEY_CACHE: OrderedDict[str, Tuple[Optional[str], float]] = OrderedDict()
CACHE_LOCK = threading.Lock()
CACHE_TTL = 3600  # 1 hour TTL for cached keys
CACHE_MAX_SIZE = 1000  # Maximum number of entries

# Cache statistics
CACHE_STATS = {
    "hits": 0,
    "misses": 0,
    "evictions": 0,
}


def clear_public_key_cache(node_id: Optional[str] = None) -> None:
    """Clear either a specific cached key or the entire cache."""
    with CACHE_LOCK:
        if node_id is not None:
            if node_id in PUBLIC_KEY_CACHE:
                del PUBLIC_KEY_CACHE[node_id]
                logger.info("Cleared public key cache for node: %s", node_id)
        else:
            PUBLIC_KEY_CACHE.clear()
            CACHE_STATS["hits"] = 0
            CACHE_STATS["misses"] = 0
            CACHE_STATS["evictions"] = 0
            logger.info("Cleared the entire public key cache and statistics.")


def get_cache_stats() -> Dict[str, int]:
    """Return cache performance statistics."""
    with CACHE_LOCK:
        total_requests = CACHE_STATS["hits"] + CACHE_STATS["misses"]
        hit_rate = (CACHE_STATS["hits"] / total_requests * 100) if total_requests > 0 else 0
        return {
            "hits": CACHE_STATS["hits"],
            "misses": CACHE_STATS["misses"],
            "evictions": CACHE_STATS["evictions"],
            "size": len(PUBLIC_KEY_CACHE),
            "hit_rate_percent": round(hit_rate, 2),
        }


def _evict_expired_entries() -> None:
    """Remove expired entries from cache (must be called with CACHE_LOCK held)."""
    current_time = time.time()
    expired_keys = [
        node_id
        for node_id, (_, timestamp) in PUBLIC_KEY_CACHE.items()
        if current_time - timestamp > CACHE_TTL
    ]
    for key in expired_keys:
        del PUBLIC_KEY_CACHE[key]
        CACHE_STATS["evictions"] += 1


def _evict_lru_if_needed() -> None:
    """Evict least recently used entry if cache is full (must be called with CACHE_LOCK held)."""
    if len(PUBLIC_KEY_CACHE) >= CACHE_MAX_SIZE:
        # Remove oldest entry (LRU)
        oldest_key = next(iter(PUBLIC_KEY_CACHE))
        del PUBLIC_KEY_CACHE[oldest_key]
        CACHE_STATS["evictions"] += 1
        logger.debug("Evicted LRU cache entry for key: %s", oldest_key)


def get_public_key_for_node(node_id: str) -> Optional[str]:
    """
    Look up the public key for ``node_id`` using an LRU cache with TTL.

    This function implements an efficient caching strategy:
    - LRU eviction when cache is full
    - TTL-based expiration (1 hour by default)
    - Thread-safe with lock-protected operations
    - Cache statistics tracking for monitoring
    """
    current_time = time.time()

    # Check cache first
    with CACHE_LOCK:
        if node_id in PUBLIC_KEY_CACHE:
            key, timestamp = PUBLIC_KEY_CACHE[node_id]
            # Check if entry is still valid
            if current_time - timestamp <= CACHE_TTL:
                # Move to end (mark as recently used)
                PUBLIC_KEY_CACHE.move_to_end(node_id)
                CACHE_STATS["hits"] += 1
                logger.debug("Cache hit for node: %s", node_id)
                return key
            else:
                # Expired entry
                del PUBLIC_KEY_CACHE[node_id]
                CACHE_STATS["evictions"] += 1

        CACHE_STATS["misses"] += 1

    # Check if it's a publisher node (auto-provisioned local or configured keys)
    if node_id not in Config.PUBLISHER_NODES:
        try:
            from local_worker_keys import load_local_publisher_public_keys
            Config.PUBLISHER_NODES.update(load_local_publisher_public_keys())
        except Exception:
            pass

    if node_id in Config.PUBLISHER_NODES:
        key = Config.PUBLISHER_NODES[node_id]
        with CACHE_LOCK:
            _evict_lru_if_needed()
            PUBLIC_KEY_CACHE[node_id] = (key, current_time)
            PUBLIC_KEY_CACHE.move_to_end(node_id)
        logger.debug("Cached publisher node key: %s", node_id)
        return key

    # Query database for customer nodes
    try:
        conn = sqlite3.connect(Config.DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT public_key FROM customer_nodes WHERE node_id = ? AND approved = 1",
            (node_id,),
        )
        result = cursor.fetchone()
        conn.close()

        key = result[0] if result else None
        with CACHE_LOCK:
            _evict_expired_entries()
            _evict_lru_if_needed()
            PUBLIC_KEY_CACHE[node_id] = (key, current_time)
            PUBLIC_KEY_CACHE.move_to_end(node_id)

        if key:
            logger.debug("Cached customer node key: %s", node_id)
        else:
            logger.warning("No approved public key found for node: %s", node_id)

        return key
    except sqlite3.Error as exc:
        logger.error("Database error when fetching public key for node '%s': %s", node_id, exc)
        return None


__all__ = ["clear_public_key_cache", "get_public_key_for_node", "get_cache_stats"]
