"""Listens for job status updates on Redis and updates the application database."""
from __future__ import annotations

import os
import threading

from .app import logger
from .helpers.listeners import listen_for_job_updates
from .housekeeper import start_timeout_monitor


def _listener_entrypoint() -> None:
    """Adapter that runs the shared listener logic in a background thread."""
    logger.info("Job update listener thread started; delegating to helpers.listeners.listen_for_job_updates().")
    try:
        listen_for_job_updates()
    except Exception:  # pragma: no cover - defensive safeguard for background thread
        logger.exception("Job update listener encountered an unexpected error and will exit.")


def start_job_update_listener() -> None:
    """Start the Redis job update listener if it is not already running."""
    if os.environ.get("FLASK_ENV") == "development" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        # In debug mode with the reloader, this function runs twice.
        # Only start the listener in the primary process.
        return

    thread = threading.Thread(target=_listener_entrypoint, daemon=True, name="JobUpdateListener")
    thread.start()
    logger.info("Job update listener background thread has been started.")

    # Start housekeeping (timeouts)
    start_timeout_monitor()
