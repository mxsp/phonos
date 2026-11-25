"""Cross-worker chat locks and cancellation flags.

Gunicorn runs up to three eventlet workers.  A local ``threading.Event`` only
stops the run that happens to live in the same process, so the active lock and
the cancel flag are mirrored in Redis when it is available.  If Redis is not
reachable (tests, a degraded host) the code falls back to the in-process dict;
nothing about the assistant becomes unavailable.
"""

from __future__ import annotations

import threading
import time

ACTIVE_PREFIX = "phonos_agent:active:"
CANCEL_PREFIX = "phonos_agent:cancel:"
ACTIVE_TTL = 900
CANCEL_TTL = 900


def redis_client():
    try:
        from ..app import get_redis_connection

        client = get_redis_connection(binary_mode=True)
        if client is None:
            return None
        ping = getattr(client, "ping", None)
        if callable(ping):
            try:
                if not ping():
                    return None
            except Exception:
                return None
        return client
    except Exception:
        return None


def acquire(chat_id: int) -> bool:
    """Take the chat lock; ``False`` when another worker is already running it."""
    client = redis_client()
    if client is not None:
        try:
            return bool(client.set(ACTIVE_PREFIX + str(chat_id), "1", nx=True, ex=ACTIVE_TTL))
        except Exception:
            pass
    return True


def release(chat_id: int) -> None:
    client = redis_client()
    if client is None:
        return
    try:
        client.delete(ACTIVE_PREFIX + str(chat_id))
    except Exception:
        pass


def cancel(chat_id: int) -> None:
    client = redis_client()
    if client is None:
        return
    try:
        client.set(CANCEL_PREFIX + str(chat_id), "1", ex=CANCEL_TTL)
    except Exception:
        pass


def clear_cancel(chat_id: int) -> None:
    client = redis_client()
    if client is None:
        return
    try:
        client.delete(CANCEL_PREFIX + str(chat_id))
    except Exception:
        pass


def is_cancelled(chat_id: int) -> bool:
    client = redis_client()
    if client is None:
        return False
    try:
        return bool(client.exists(CANCEL_PREFIX + str(chat_id)))
    except Exception:
        return False


class CancelToken:
    """``threading.Event`` plus a throttled Redis check for cross-worker stops."""

    def __init__(self, chat_id: int, event: threading.Event):
        self.chat_id = chat_id
        self.event = event
        self._last_check = 0.0

    def is_set(self) -> bool:
        if self.event.is_set():
            return True
        now = time.monotonic()
        if now - self._last_check < 0.5:
            return False
        self._last_check = now
        if is_cancelled(self.chat_id):
            self.event.set()
            return True
        return False

    def set(self) -> None:
        self.event.set()
        cancel(self.chat_id)
