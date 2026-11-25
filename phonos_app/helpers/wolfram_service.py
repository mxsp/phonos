"""Shared helper for calling the Wolfram Language evaluation service.

Centralizes the resolution of service URL + per-request credentials so the
notebook socket handler, the credentials/verify endpoint, and the file-based
.nb execution endpoint all behave identically.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple
from urllib import error as _urlerror
from urllib import request as _urlrequest

logger = logging.getLogger(__name__)


def wolfram_string_literal(value: str) -> str:
    """Render a Python string as an injection-safe Wolfram Language string expression.

    Never interpolate untrusted text directly into a WL string literal (naive
    quote-stripping is not sufficient -- e.g. a trailing backslash can still
    escape the closing quote and let the rest of the generated source run as
    code instead of data). Base64 has no quote/backslash characters, so
    round-tripping through it removes the entire class of string-breakout
    injection regardless of what ``value`` contains.
    """
    b64 = base64.b64encode(str(value).encode('utf-8')).decode('ascii')
    return f'ByteArrayToString[BaseDecode["{b64}"]]'


def resolve_wolfram_service_url(db=None) -> str:
    """Resolve the Wolfram service URL.

    Precedence:
        1. ``runtime_levels.wolfram.config.service_url`` (DB)
        2. ``WOLFRAM_SERVICE_URL`` environment variable
        3. ``http://localhost:5100`` (default for live gunicorn host)
    """
    default = os.environ.get('WOLFRAM_SERVICE_URL', 'http://localhost:5100')
    try:
        from phonos_app.helpers.runtime_config import get_runtime_config
        rc = get_runtime_config(db) if db is not None else get_runtime_config()
        level = rc.get_level('wolfram')
        if level and level.get('config') and level['config'].get('service_url'):
            return level['config']['service_url']
    except Exception as e:
        logger.warning(f"Could not load wolfram service config from DB: {e}")
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
    return default


def resolve_user_wolfram_credentials(user_id: int, is_admin: bool) -> Optional[Tuple[str, str]]:
    """Return ``(username, password)`` for the given user, or None.

    Admins get None and the container's global WOLFRAM_USERNAME / WOLFRAM_PASSWORD
    is used by the Wolfram service itself. Non-admins without saved credentials
    also get None — callers should fail fast with a MissingCredentials error.
    """
    if is_admin:
        return None
    try:
        from phonos_app.helpers.wolfram_user_creds import get_credentials
        return get_credentials(user_id)
    except Exception as e:
        logger.warning(f"Failed to load wolfram credentials for user {user_id}: {e}")
        return None


def _is_admin_user(db, user_id: int) -> bool:
    try:
        row = db.execute(
            "SELECT is_admin FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        return bool(row and row['is_admin'])
    except Exception:
        return False


def call_wolfram_service(
    code: str,
    user_id: int,
    db,
    timeout: int = 60,
    image_size: Optional[int] = None,
    service_url: Optional[str] = None,
    session_key: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Evaluate ``code`` via the Wolfram service for ``user_id``.

    Resolves credentials (admin -> container env, non-admin -> per-user DB),
    builds the Basic auth header, and POSTs to ``<service_url>/evaluate``.
    When ``session_key`` is given, posts to ``/evaluate-session`` instead so
    state persists across calls sharing that key (used by the hybrid
    Python↔Wolfram bridge, which shares the notebook session's Wolfram state).

    Returns the parsed JSON response dict, augmented with:
      - ``_http_status``: HTTP status from the service
      - ``_auth_source``: 'admin_env' or 'user_db' or 'none'
      - ``_timings_ms``: wall-clock time spent
    Raises ``PermissionError`` for unauthenticated non-admin users and
    ``RuntimeError`` for HTTP/transport errors. ``RuntimeError`` may carry
    ``error_code='auth_failed'`` for 401/403.
    """
    import time

    service_url = service_url or resolve_wolfram_service_url(db)
    is_admin = _is_admin_user(db, user_id)
    user_creds = resolve_user_wolfram_credentials(user_id, is_admin)

    if not is_admin and not user_creds:
        raise PermissionError(
            "No Wolfram Cloud credentials saved for this user. "
            "Open Notebook IDE Settings → Wolfram Account to add them."
        )

    headers = {'Content-Type': 'application/json'}
    auth_source = 'admin_env' if is_admin else ('user_db' if user_creds else 'none')
    if user_creds:
        token = base64.b64encode(
            f"{user_creds[0]}:{user_creds[1]}".encode('utf-8')
        ).decode('ascii')
        headers['X-Wolfram-Auth'] = f"Basic {token}"

    payload = {'code': code, 'timeout': max(1, min(int(timeout), 600))}
    if image_size is not None:
        payload['image_size'] = max(64, min(int(image_size), 2400))

    endpoint = '/evaluate'
    if session_key:
        payload['session_id'] = session_key
        payload['persist'] = bool(persist)
        endpoint = '/evaluate-session'

    body = json.dumps(payload).encode('utf-8')
    req = _urlrequest.Request(
        f"{service_url.rstrip('/')}{endpoint}",
        data=body,
        headers=headers,
        method='POST',
    )

    t0 = time.monotonic()
    try:
        with _urlrequest.urlopen(req, timeout=timeout + 30) as resp:
            raw = resp.read().decode('utf-8')
            http_status = resp.status
            data = json.loads(raw)
    except _urlerror.HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        if e.code in (401, 403):
            err = RuntimeError(
                f"Wolfram Cloud rejected your credentials (HTTP {e.code}). "
                f"Update them in Notebook IDE Settings → Wolfram Account."
            )
            err.error_code = 'auth_failed'
            err.detail = err_body
            raise err
        err = RuntimeError(f"Wolfram service request failed: HTTP {e.code} {err_body[:200]}")
        err.error_code = 'http_error'
        raise err
    except _urlerror.URLError as e:
        err = RuntimeError(f"Wolfram service unreachable at {service_url}: {e}")
        err.error_code = 'unreachable'
        raise err
    except Exception as e:
        err = RuntimeError(f"Wolfram service call failed: {e}")
        err.error_code = 'unknown'
        raise err
    finally:
        pass

    data['_http_status'] = http_status
    data['_auth_source'] = auth_source
    data['_timings_ms'] = int((time.monotonic() - t0) * 1000)
    return data



