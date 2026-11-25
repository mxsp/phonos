"""Convenience imports for the Phonos application package."""
__version__ = "v2.6.0-S"

from .app import (
    NODE_ID_PATTERN,
    SUPPORTED_MODEL_IDS,
    app,
    get_redis_connection,
    logger,
    mail,
    redis_pool,
    resolve_debug_mode,
    socketio,
)

__all__ = [
    "__version__",
    "app",
    "socketio",
    "mail",
    "logger",
    "SUPPORTED_MODEL_IDS",
    "NODE_ID_PATTERN",
    "redis_pool",
    "get_redis_connection",
    "resolve_debug_mode",
]
