"""Application setup and shared extensions for the Phonos web server."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Mapping, Sequence, Set

# Ensure every file/dir the app creates is owner-only (0600/0700) at rest.
# Defensive duplicate of wsgi.py in case the app is imported without wsgi;
# runs once at module import, before any file I/O or app creation.
os.umask(0o077)

import redis
from flask import Flask, jsonify
from flask_compress import Compress
from flask_mail import Mail
from flask_socketio import SocketIO
from jinja2 import ChoiceLoader, FileSystemLoader
from unittest.mock import MagicMock
from werkzeug.middleware.proxy_fix import ProxyFix

# Ensure the sibling 'production' package (plan_service, config, etc.) is importable
# even when the app is imported from tests, scripts, or different working directories.
import os as _os_for_path
import sys as _sys_for_path
_PROD_DIR = _os_for_path.path.join(_os_for_path.path.dirname(_os_for_path.path.abspath(__file__)), '..', 'production')
_PROD_DIR = _os_for_path.path.abspath(_PROD_DIR)
if _PROD_DIR not in _sys_for_path.path:
    _sys_for_path.path.insert(0, _PROD_DIR)

from production.config import Config

# Flask 3.x made RequestContext.session a read-only @property, but
# Flask-SocketIO (<= 5.3.6) still does ctx.session = session_obj to restore
# managed sessions.  Add a setter so those assignments don't crash.
import flask.ctx
_flask_session_prop = flask.ctx.RequestContext.session
flask.ctx.RequestContext.session = property(
    _flask_session_prop.fget,
    lambda self, value: setattr(self, '_session', value),
    None,
    _flask_session_prop.__doc__,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = getattr(_sys_for_path, "_MEIPASS", os.path.dirname(BASE_DIR))


def _create_app() -> Flask:
    """Initialise the Flask application with template fallbacks."""
    app = Flask(
        __name__,
        template_folder=os.path.join(PROJECT_ROOT, "templates"),
        static_folder=os.path.join(PROJECT_ROOT, "static"),
    )

    template_search_paths: list[str] = []

    primary_templates = os.path.join(PROJECT_ROOT, "templates")
    if os.path.isdir(primary_templates):
        template_search_paths.append(primary_templates)

    cwd_templates = os.path.join(os.getcwd(), "templates")
    if os.path.isdir(cwd_templates) and cwd_templates not in template_search_paths:
        template_search_paths.append(cwd_templates)

    if template_search_paths:
        loaders: list[FileSystemLoader] = [FileSystemLoader(path) for path in template_search_paths]
        if app.jinja_loader is not None:
            loaders.append(app.jinja_loader)
        app.jinja_loader = ChoiceLoader(loaders)

    app.config.from_object(Config)

    # Enable template auto-reload
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Mail configuration is stored on the config object and initialised here to
    # ensure the extension is ready for importers.
    app.config["MAIL_SERVER"] = Config.MAIL_SERVER
    app.config["MAIL_PORT"] = Config.MAIL_PORT
    app.config["MAIL_USE_TLS"] = Config.MAIL_USE_TLS
    app.config["MAIL_USERNAME"] = Config.MAIL_USERNAME
    app.config["MAIL_PASSWORD"] = Config.MAIL_PASSWORD
    app.config["MAIL_DEFAULT_SENDER"] = Config.MAIL_DEFAULT_SENDER

    # Configure URL scheme for generating external URLs
    # Only bind SERVER_NAME when explicitly set so local/LAN ports (e.g. 5040/5041) match cleanly
    if Config.SERVER_NAME:
        app.config["SERVER_NAME"] = Config.SERVER_NAME
    app.config["PREFERRED_URL_SCHEME"] = getattr(Config, "PREFERRED_URL_SCHEME", "http")

    # Only trust X-Forwarded-* headers when explicitly behind a trusted reverse proxy
    if os.environ.get("PHONOS_TRUST_PROXY", "0").strip().lower() in ("1", "true", "yes"):
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_prefix=1,
        )

    return app


app = _create_app()
Compress(app)
mail = Mail(app)
try:
    if getattr(_sys_for_path, "frozen", False):
        socketio = SocketIO(app, async_mode="threading")
    else:
        socketio = SocketIO(app, async_mode="eventlet")
except ValueError:
    socketio = SocketIO(app, async_mode="threading")

# Configure logging EARLY to avoid circular imports
# (helpers and routes may import logger during initialization)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _register_analysis_fallback_routes(app: Flask, reason: Exception | str) -> None:
    """Ensure structure-analysis endpoints exist even if route imports fail."""
    detail = str(reason)

    def analysis_unavailable_response():
        return (
            jsonify({
                "error": "Structure analysis is currently unavailable on this server",
                "detail": detail,
            }),
            503,
        )

    @app.route("/api/analysis/confidence/<job_id>", methods=["GET"], endpoint="fallback_get_confidence_analysis")
    def fallback_get_confidence_analysis(job_id: str):  # pragma: no cover - exercised via Flask routing
        return analysis_unavailable_response()

    @app.route("/api/analysis/disorder/<job_id>", methods=["GET"], endpoint="fallback_get_disorder_analysis")
    def fallback_get_disorder_analysis(job_id: str):  # pragma: no cover - exercised via Flask routing
        return analysis_unavailable_response()

    @app.route("/api/analysis/compare", methods=["POST"], endpoint="fallback_compare_structures")
    def fallback_compare_structures():  # pragma: no cover - exercised via Flask routing
        return analysis_unavailable_response()

    @app.route("/api/analysis/summary/<job_id>", methods=["GET"], endpoint="fallback_get_structure_summary")
    def fallback_get_structure_summary(job_id: str):  # pragma: no cover - exercised via Flask routing
        return analysis_unavailable_response()

    @app.route("/api/analysis/deep", methods=["POST"], endpoint="fallback_deep_structure_analysis")
    def fallback_deep_structure_analysis():  # pragma: no cover - exercised via Flask routing
        return analysis_unavailable_response()

import importlib
import importlib.util


def _load_direct_module(short_name: str, rel_parts: tuple[str, ...], pkg_fallback: str):
    """Load a module directly from disk without triggering package __init__.py, falling back cleanly in frozen bundles."""
    file_path = os.path.join(BASE_DIR, *rel_parts)
    if os.path.isfile(file_path):
        spec = importlib.util.spec_from_file_location(short_name, file_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return importlib.import_module(pkg_fallback)


_stripe_routes_module = _load_direct_module('stripe_routes', ('routes', 'stripe_routes.py'), 'phonos_app.routes.stripe_routes')
_stripe_routes_module.register_stripe_routes(app)

_database_routes_module = _load_direct_module('api_databases', ('routes', 'api_databases.py'), 'phonos_app.routes.api_databases')
_database_routes_module.register_database_routes(app)

try:
    _analysis_routes_module = _load_direct_module('api_analysis', ('routes', 'api_analysis.py'), 'phonos_app.routes.api_analysis')
    _analysis_routes_module.register_analysis_routes(app)
except Exception as e:
    logger.warning("Structure analysis routes unavailable, registering fallback endpoints: %s", e, exc_info=True)
    _register_analysis_fallback_routes(app, e)

_api_keys_module = _load_direct_module('api_keys_management', ('routes', 'api_keys_management.py'), 'phonos_app.routes.api_keys_management')
_api_keys_module.register_api_keys_routes(app)

_webhooks_module = _load_direct_module('api_webhooks_new', ('routes', 'api_webhooks_new.py'), 'phonos_app.routes.api_webhooks_new')
_webhooks_module.register_webhook_routes(app)
logger.info("Enhanced webhook routes registered successfully")

try:
    _notebook_socket_module = _load_direct_module('socket_handlers_notebook', ('socket_handlers_notebook.py',), 'phonos_app.socket_handlers_notebook')
    _notebook_socket_module.register_notebook_socket_handlers(socketio)
    logger.info("Notebook WebSocket handlers registered successfully")
except Exception as e:
    logger.warning(f"Could not load notebook socket handlers: {e}")

try:
    _user_models_module = _load_direct_module('api_user_models', ('routes', 'api_user_models.py'), 'phonos_app.routes.api_user_models')
    _user_models_module.register_user_model_routes(app)
    logger.info("User model routes registered successfully")
except Exception as e:
    logger.error(f"Error registering user model routes: {e}", exc_info=True)




# Add CSRF token to all template contexts
@app.context_processor
def inject_csrf_token():
    from .helpers.security import generate_csrf_token
    return {'csrf_token': generate_csrf_token()}

# Add analytics consent to all template contexts
@app.context_processor
def inject_analytics_consent():
    """Inject analytics_consent variable into all templates."""
    from flask import request as flask_request
    analytics_consent = False
    try:
        if flask_request.cookies.get('analytics_consent') == '1':
            analytics_consent = True
    except Exception:
        # Cookie access should never fail in normal request context; swallow defensively.
        pass
    return {'analytics_consent': analytics_consent}


@app.after_request
def add_security_headers(response):
    """Attach baseline browser security headers to every response."""
    from flask import request as flask_request
    from .helpers.security import apply_security_headers
    response = apply_security_headers(response)

    # Add caching headers for GET API responses to improve performance
    if flask_request and flask_request.path.startswith('/api/') and response.status_code == 200 and flask_request.method == 'GET':
        ct = response.headers.get('Content-Type', '')
        if ct.startswith('application/json'):
            # Let the browser cache JSON GETs briefly, but do NOT emit a 304
            # here: a 304 carries an empty body while keeping the
            # `application/json` Content-Type, and the fetch() wrapper then
            # calls response.json() on the empty body and throws
            # "Server returned invalid response" (dashboard jobs/projects fail
            # to load). The browser cannot always reconstruct the cached body
            # from a 304 (Vary/gzip interactions), so the raw empty 304 reaches
            # JS. max-age=30 alone gives the caching benefit without that risk.
            # Vary is Accept-Encoding only (auth is via session cookie, never an
            # Authorization header, so varying on it was pointless).
            response.headers['Cache-Control'] = 'private, max-age=30'
            response.headers['Vary'] = 'Accept-Encoding'
    elif flask_request and flask_request.path.startswith('/static/') and response.status_code == 200:
        # Cache static assets aggressively in browser
        if flask_request.path.endswith(('.js', '.css')):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif flask_request.path.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf')):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'

    return response


@app.before_request
def enforce_localnet_only():
    """When Config.LOCALNET_ONLY is enabled, restrict access to loopback, RFC1918, and ULA private subnets."""
    if not getattr(Config, "LOCALNET_ONLY", False) or app.config.get("TESTING"):
        return None
    import ipaddress
    from flask import request as flask_request
    remote = (flask_request.remote_addr or "").strip()
    if not remote or remote in ("local", "testclient"):
        return None
    allow_extra = {
        h.strip()
        for h in os.environ.get("PHONOS_LOCALNET_ALLOW", "").split(",")
        if h.strip()
    }
    candidates = [remote]
    xff = flask_request.headers.get("X-Forwarded-For", "")
    if xff:
        candidates.extend(p.strip() for p in xff.split(",") if p.strip())
    for raw_ip in candidates:
        if raw_ip in allow_extra:
            continue
        try:
            addr = ipaddress.ip_address(raw_ip)
            if not (addr.is_loopback or addr.is_private or addr.is_link_local):
                if flask_request.path.startswith("/api/"):
                    return jsonify({"error": "Localnet-only mode is active; public network access is disabled."}), 403
                return ("403 Forbidden — Phonos is configured for local network access only (PHONOS_LOCALNET_ONLY=1).", 403)
        except ValueError:
            pass
    return None


# Register geoblocking middleware (disabled by default in self-hosted LOCAL_LAB_MODE)
from .helpers.geoblocking import register_geoblocking
_geoblock_enabled = os.environ.get('GEOBLOCK_ENABLED', 'false' if getattr(Config, 'LOCAL_LAB_MODE', True) else 'true').lower() == 'true'
register_geoblocking(app, enabled=_geoblock_enabled)

# Register remember-me auto-login. MUST come after geoblocking (and any future
# IP-block middleware) so those run first — a non-None return from an earlier
# before_request short-circuits this hook, which is the ordering we want.
from .helpers.persistent_auth import register_remember_me
register_remember_me(app)

# Trusted Tester Program gate (TRUSTED_TESTER_GATE=true): applicants only reach the
# program page until approved. After remember-me, so a restored session is gated too.
from .helpers.trusted_tester import register_trusted_tester_gate
register_trusted_tester_gate(app)

# Note: i18n (internationalization) is implemented client-side via JavaScript
# The language switcher and translations are in /static/js/i18n.js
# Translation files are in /static/locales/en.json and /static/locales/de.json
logger.info("i18n system available (client-side implementation)")

# Ensure DB schema is up to date
# Flask 2.3+ deprecated before_first_request, use direct call or alternative
# We move this after logger definition and inside a function to avoid circular imports during init
# def run_migrations():
#     with app.app_context():
#         try:
#             from .helpers.db_migrations import ensure_jobs_cost_column
#             ensure_jobs_cost_column()
#         except Exception as e:
#             logger.error(f"Failed to run database migrations: {e}")

# Run migrations immediately if not imported
# if __name__ != "__main__":
#    run_migrations()

# Initialize background scheduler for periodic cleanup tasks
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import atexit

scheduler = BackgroundScheduler(daemon=True)

def periodic_call_cleanup():
    """Periodic cleanup job for stale calls - runs every 90 seconds"""
    with app.app_context():
        try:
            from .helpers.calls import cleanup_all_stale_calls
            stats = cleanup_all_stale_calls()
            if any(stats.values()):  # Only log if something was cleaned up
                logger.info(f"Periodic call cleanup: {stats}")
        except Exception as e:
            logger.error(f"Error in periodic call cleanup: {e}", exc_info=True)


def periodic_cache_cleanup():
    """Periodic cleanup of expired cache entries - runs every hour"""
    with app.app_context():
        try:
            r = get_redis_connection()
            # Clean up expired sequence pending entries
            # Redis handles TTL automatically, but we can log stats
            pending_keys = r.keys("hash:sequence_pending:*")
            if pending_keys:
                logger.info(f"Active sequence pending entries: {len(pending_keys)}")
        except Exception as e:
            logger.error(f"Error in cache cleanup: {e}", exc_info=True)


def periodic_metrics_aggregation():
    """Aggregate metrics for reporting - runs every 15 minutes"""
    with app.app_context():
        try:
            from production.mrs_service.runtime import get_pipeline_stats
            r = get_redis_connection()

            # Get current pipeline stats
            stats = get_pipeline_stats()

            # Store daily aggregates
            import time
            day_key = time.strftime("%Y%m%d")
            daily_stats_key = f"stats:daily:{day_key}"

            # Increment daily counters from hourly stats
            hourly = stats.get("hourly_stats", {})
            for key, value in hourly.items():
                if isinstance(key, str) and ':count' in key:
                    try:
                        r.hincrby(daily_stats_key, key, int(value))
                    except (ValueError, TypeError):
                        pass

            # Set expiry for daily stats (keep 30 days)
            r.expire(daily_stats_key, 86400 * 30)

            logger.debug(f"Metrics aggregated for {day_key}")
        except Exception as e:
            logger.error(f"Error in metrics aggregation: {e}", exc_info=True)


def periodic_notebook_cleanup():
    """Cleanup expired notebook sessions - runs every hour"""
    with app.app_context():
        try:
            from .helpers.kernel_manager import kernel_manager
            stats = kernel_manager.cleanup_idle_sessions_sync()
            if stats.get('cleaned', 0) > 0:
                logger.info(f"Notebook session cleanup: {stats}")
        except Exception as e:
            logger.error(f"Error in notebook session cleanup: {e}", exc_info=True)


# Schedule cleanup every 90 seconds
scheduler.add_job(
    func=periodic_call_cleanup,
    trigger=IntervalTrigger(seconds=90),
    id='call_cleanup_job',
    name='Periodic call state cleanup',
    replace_existing=True
)

# Schedule cache cleanup every hour
scheduler.add_job(
    func=periodic_cache_cleanup,
    trigger=IntervalTrigger(hours=1),
    id='cache_cleanup_job',
    name='Periodic cache cleanup',
    replace_existing=True
)

# Schedule metrics aggregation every 15 minutes
scheduler.add_job(
    func=periodic_metrics_aggregation,
    trigger=IntervalTrigger(minutes=15),
    id='metrics_aggregation_job',
    name='Periodic metrics aggregation',
    replace_existing=True
)

# Schedule notebook session cleanup every hour
scheduler.add_job(
    func=periodic_notebook_cleanup,
    trigger=IntervalTrigger(hours=1),
    id='notebook_cleanup_job',
    name='Periodic notebook session cleanup',
    replace_existing=True
)

# Start the scheduler
scheduler.start()
logger.info("Background scheduler started with periodic jobs:")
logger.info("  - Call cleanup (every 90s)")
logger.info("  - Cache cleanup (every 1h)")
logger.info("  - Metrics aggregation (every 15m)")
logger.info("  - Notebook cleanup (every 1h)")

# Shut down scheduler when exiting
atexit.register(lambda: scheduler.shutdown())

SUPPORTED_MODEL_IDS: Set[str] = {model["id"] for model in Config.SUPPORTED_MODELS}
NODE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{3,63}$")

_redis_pool_kwargs = {
    "host": Config.REDIS_HOST,
    "port": Config.REDIS_PORT,
    "db": Config.REDIS_DB,
    "max_connections": 50,
    "socket_connect_timeout": 5,
    "socket_timeout": 5,
    "socket_keepalive": True,
    "health_check_interval": 30,
    "retry_on_timeout": True,
}

# Add password if configured
if hasattr(Config, 'REDIS_PASSWORD') and Config.REDIS_PASSWORD:
    _redis_pool_kwargs["password"] = Config.REDIS_PASSWORD

# Add SSL/TLS if configured
if hasattr(Config, 'REDIS_SSL') and Config.REDIS_SSL:
    import ssl
    _redis_pool_kwargs["connection_class"] = redis.SSLConnection
    _redis_pool_kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED

redis_pool = redis.ConnectionPool(**_redis_pool_kwargs)


class _InMemoryPubSub:
    def __init__(self) -> None:
        self._patterns = set()

    def psubscribe(self, *patterns):
        self._patterns.update(patterns)

    def listen(self):  # pragma: no cover - generator behaviour
        if False:
            yield None
        return

    def close(self) -> None:
        self._patterns.clear()


class _InMemoryRedis:
    def __init__(self) -> None:
        self._kv_store: dict[str, tuple[bytes, datetime | None]] = {}
        self._lists: dict[str, list[bytes]] = {}
        self._hashes: dict[str, dict[bytes, bytes]] = {}
        self._pubsub_channels: dict[str, list] = {}

    def _normalize_key(self, key) -> str:
        if isinstance(key, bytes):
            return key.decode("utf-8")
        return str(key)

    def _serialize(self, value) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        return json.dumps(value).encode("utf-8")

    def _compute_expiry(self, timeout) -> datetime | None:
        if timeout is None:
            return None
        if isinstance(timeout, timedelta):
            delta = timeout
        else:
            try:
                delta = timedelta(seconds=int(timeout))
            except (TypeError, ValueError):
                delta = timedelta(0)
        return datetime.utcnow() + delta if delta.total_seconds() > 0 else None

    def ping(self) -> bool:
        return True

    def set(self, key, value, ex=None, px=None, nx=False, xx=False):
        normalized_key = self._normalize_key(key)
        existing = self.get(normalized_key)
        if nx and existing is not None:
            return False
        if xx and existing is None:
            return False
        timeout = ex if ex is not None else (px / 1000.0 if px is not None else None)
        expiry = self._compute_expiry(timeout)
        self._kv_store[normalized_key] = (self._serialize(value), expiry)
        return True

    def setex(self, key, timeout, value):
        normalized_key = self._normalize_key(key)
        expiry = self._compute_expiry(timeout)
        self._kv_store[normalized_key] = (self._serialize(value), expiry)
        return True

    def get(self, key):
        normalized_key = self._normalize_key(key)
        entry = self._kv_store.get(normalized_key)
        if not entry:
            return None
        value, expiry = entry
        if expiry and expiry < datetime.utcnow():
            self._kv_store.pop(normalized_key, None)
            return None
        return value

    def exists(self, *keys) -> int:
        return sum(1 for k in keys if self.get(k) is not None or self._normalize_key(k) in self._hashes or self._normalize_key(k) in self._lists)

    def incr(self, key, amount: int = 1) -> int:
        normalized_key = self._normalize_key(key)
        entry = self._kv_store.get(normalized_key)
        expiry = None
        cur = 0
        if entry:
            raw, expiry = entry
            if expiry and expiry < datetime.utcnow():
                expiry = None
            else:
                try:
                    cur = int(raw.decode("utf-8"))
                except Exception:
                    cur = 0
        cur += int(amount)
        self._kv_store[normalized_key] = (str(cur).encode("utf-8"), expiry)
        return cur

    def expire(self, key, time_s) -> bool:
        normalized_key = self._normalize_key(key)
        entry = self._kv_store.get(normalized_key)
        if not entry:
            return False
        value, _ = entry
        self._kv_store[normalized_key] = (value, self._compute_expiry(time_s))
        return True

    def ttl(self, key) -> int:
        normalized_key = self._normalize_key(key)
        entry = self._kv_store.get(normalized_key)
        if not entry:
            return -2
        _, expiry = entry
        if expiry is None:
            return -1
        rem = int((expiry - datetime.utcnow()).total_seconds())
        return rem if rem >= 0 else -2

    def delete(self, *keys):
        removed = 0
        for key in keys:
            normalized_key = self._normalize_key(key)
            if self._kv_store.pop(normalized_key, None) is not None:
                removed += 1
            if self._lists.pop(normalized_key, None) is not None:
                removed += 1
            if self._hashes.pop(normalized_key, None) is not None:
                removed += 1
        return max(removed, 1)

    def keys(self, pattern: str = "*"):
        import fnmatch
        pat = pattern.decode("utf-8") if isinstance(pattern, bytes) else str(pattern)
        all_keys = set(self._kv_store.keys()) | set(self._lists.keys()) | set(self._hashes.keys())
        return [k.encode("utf-8") for k in all_keys if fnmatch.fnmatch(k, pat)]

    def scan_iter(self, match: str = "*", count: int | None = None):
        for k in self.keys(match or "*"):
            yield k

    def lpush(self, key, *values):
        normalized_key = self._normalize_key(key)
        bucket = self._lists.setdefault(normalized_key, [])
        for value in values:
            bucket.insert(0, self._serialize(value))
        return len(bucket)

    def rpush(self, key, *values):
        normalized_key = self._normalize_key(key)
        bucket = self._lists.setdefault(normalized_key, [])
        for value in values:
            bucket.append(self._serialize(value))
        return len(bucket)

    def lpop(self, key):
        bucket = self._lists.get(self._normalize_key(key))
        return bucket.pop(0) if bucket else None

    def rpop(self, key):
        bucket = self._lists.get(self._normalize_key(key))
        return bucket.pop() if bucket else None

    def llen(self, key) -> int:
        return len(self._lists.get(self._normalize_key(key), []))

    def brpop(self, key, timeout=0):
        keys = [key] if isinstance(key, (str, bytes)) else list(key)
        for k in keys:
            normalized_key = self._normalize_key(k)
            bucket = self._lists.get(normalized_key)
            if bucket:
                return normalized_key.encode("utf-8"), bucket.pop()
        return None

    def hset(self, key, field=None, value=None, mapping=None):
        h = self._hashes.setdefault(self._normalize_key(key), {})
        added = 0
        if field is not None:
            fb = self._serialize(field)
            if fb not in h:
                added += 1
            h[fb] = self._serialize(value)
        if mapping:
            for mf, mv in mapping.items():
                fb = self._serialize(mf)
                if fb not in h:
                    added += 1
                h[fb] = self._serialize(mv)
        return added

    def hget(self, key, field):
        h = self._hashes.get(self._normalize_key(key), {})
        return h.get(self._serialize(field))

    def hdel(self, key, *fields):
        h = self._hashes.get(self._normalize_key(key), {})
        removed = 0
        for f in fields:
            if h.pop(self._serialize(f), None) is not None:
                removed += 1
        return removed

    def hgetall(self, key):
        return dict(self._hashes.get(self._normalize_key(key), {}))

    def pubsub(self, **kwargs):
        return _InMemoryPubSub()

    def publish(self, channel, message):
        return 0

    def __getattr__(self, name):  # pragma: no cover - convenience for stubs
        def _no_op(*args, **kwargs):
            return None

        return _no_op


_redis_fallback_instance: _InMemoryRedis = None
_redis_probe_state: dict[str, float | bool] = {"checked_at": 0.0, "ok": False}


def resolve_debug_mode(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Determine whether the development server should run in debug mode."""

    import sys

    argument_parser = argparse.ArgumentParser(add_help=False)
    argument_parser.add_argument("--debug", dest="debug", action="store_true")
    argument_parser.add_argument("--no-debug", dest="no_debug", action="store_true")

    if argv is None:
        argv = sys.argv[1:]
    if env is None:
        env = os.environ

    parsed, _ = argument_parser.parse_known_args(argv)
    if parsed.debug:
        return True
    if parsed.no_debug:
        return False

    return env.get("FLASK_DEBUG", "false").lower() == "true"


def get_redis_connection(*, binary_mode: bool = False):
    global _redis_fallback_instance

    if os.environ.get("PHONOS_REDIS_MODE", "").strip().lower() in ("memory", "inmemory", "disabled", "none"):
        if _redis_fallback_instance is None:
            _redis_fallback_instance = _InMemoryRedis()
        return _redis_fallback_instance

    if MagicMock is not None and isinstance(redis_pool, MagicMock):
        if _redis_fallback_instance is None:
            _redis_fallback_instance = _InMemoryRedis()
        return _redis_fallback_instance

    try:
        client = redis.Redis(connection_pool=redis_pool)
        import time as _time
        now = _time.monotonic()
        if now - float(_redis_probe_state.get("checked_at", 0.0)) > 15.0:
            _redis_probe_state["checked_at"] = now
            try:
                client.ping()
                _redis_probe_state["ok"] = True
            except Exception:
                _redis_probe_state["ok"] = False
        if not _redis_probe_state.get("ok", False):
            if _redis_fallback_instance is None:
                _redis_fallback_instance = _InMemoryRedis()
            return _redis_fallback_instance
        return client
    except (redis.RedisError, TypeError, ValueError, OSError):
        if _redis_fallback_instance is None:
            _redis_fallback_instance = _InMemoryRedis()
        return _redis_fallback_instance


__all__ = [
    "app",
    "mail",
    "socketio",
    "logger",
    "SUPPORTED_MODEL_IDS",
    "NODE_ID_PATTERN",
    "redis_pool",
    "get_redis_connection",
    "resolve_debug_mode",
]

# Import api_extensions routes at the very end to avoid circular imports
# These routes depend on app being fully initialized
try:
    from .routes import api_extensions  # noqa: F401
    logger.info("Extension routes (PDB import, etc.) registered successfully")
except Exception as e:
    logger.warning(f"Could not load extension routes: {e}")
