"""WSGI entry point for Gunicorn."""
import os

# Ensure every file/dir the app creates is owner-only (0600/0700) at rest.
# Must run before the app is imported so no file I/O escapes the umask.
os.umask(0o077)

import sys

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

# Make psycopg2 cooperative under eventlet so DB I/O yields the hub instead of
# blocking every greenlet on the worker. Must run before any psycopg2
# connection is opened (the pool is created lazily on first request). No-op
# (with a warning) if psycogreen isn't installed, so the app still boots.
import logging as _logging
try:
    from psycogreen.eventlet import patch_psycopg as _patch_psycopg
    _patch_psycopg()
    _logging.getLogger("phonos.wsgi").info("psycopg2 patched for eventlet (psycogreen)")
except Exception as _patch_err:  # pragma: no cover - depends on deploy env
    _logging.getLogger("phonos.wsgi").warning(
        "psycogreen not active; psycopg2 will block the eventlet hub: %s", _patch_err
    )

# Add the production directory to path
production_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'production')
sys.path.insert(0, production_dir)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import main to run initialization
from production.main import ensure_database, app
from phonos_app import socketio
from phonos_app.database_updater import start_job_update_listener
from datetime import datetime

# Initialize database and start background services
ensure_database()
start_job_update_listener()

# Set start time
app.start_time = datetime.utcnow()

# Create job output directory
os.makedirs(app.config["JOB_OUTPUT_DIR"], exist_ok=True)

# Import routes
import phonos_app.routes  # noqa: F401

# This is what Gunicorn will use
# For eventlet worker with Flask-SocketIO, we return the Flask app
# and the SocketIO middleware will be applied automatically
application = app
