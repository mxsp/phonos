"""Google OAuth 2.0 / OpenID Connect helpers.

Lays the foundation for Sign in with Google.  Not activated until
``GOOGLE_CLIENT_ID`` is set in the environment.

Two authentication flows are supported:

1. **Popup (SOTA)** — the frontend uses the Google Identity Services (GIS)
   library to render the branded button and obtain an ID token (JWT) in a popup.
   The token is POSTed to ``/auth/google/token`` for server-side verification.

2. **Server-side redirect** — ``/auth/google/login`` redirects to Google's
   consent screen, which then calls back to ``/auth/google/callback``.  This
   serves as a progressive enhancement / JS-disabled fallback.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from authlib.integrations.flask_client import OAuth
from authlib.jose import JsonWebKey, JsonWebToken
from flask import session, url_for

from config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy OAuth client — only initialised when Google is configured
# ---------------------------------------------------------------------------

_oauth: OAuth = None
_google: Any = None  # authlib client for the google remote app
_jwks_cache: dict = None


def _init_oauth() -> None:
    """Initialise the ``Authlib`` OAuth client and register the Google remote app.

    Safe to call multiple times; the underlying client is created once.
    """
    global _oauth, _google
    if _oauth is not None:
        return
    _oauth = OAuth()
    _google = _oauth.register(
        name="google",
        client_id=Config.GOOGLE_CLIENT_ID or "",
        client_secret=Config.GOOGLE_CLIENT_SECRET or "",
        server_metadata_url=Config.GOOGLE_DISCOVERY_URL,
        client_kwargs={
            "scope": "openid email profile",
            "prompt": "select_account",
        },
    )


def google_enabled() -> bool:
    """Return ``True`` when Google OAuth credentials have been configured."""
    return bool(Config.GOOGLE_CLIENT_ID and Config.GOOGLE_CLIENT_SECRET)


def get_google_client():
    """Return the Authlib Google remote app, or ``None`` if not configured."""
    if not google_enabled():
        return None
    _init_oauth()
    return _google


# ---------------------------------------------------------------------------
# Server-side redirect flow helpers
# ---------------------------------------------------------------------------


def google_login_redirect(redirect_uri: str = None) -> str:
    """Build the Google OAuth authorisation redirect URL.

    Parameters
    ----------
    redirect_uri:
        The full callback URL (``/auth/google/callback``).  Defaults to the
        ``auth_google_callback`` route.

    Returns
    -------
    The full Google Accounts authorisation URL.
    """
    client = get_google_client()
    if client is None:
        raise RuntimeError("Google OAuth is not configured")

    if redirect_uri is None:
        redirect_uri = url_for("auth_google_callback", _external=True)

    return client.authorize_redirect(redirect_uri)


def google_callback():
    """Exchange the authorisation code for tokens and return the user info.

    Returns
    -------
    A dict with ``sub`` (Google user ID), ``email``, ``email_verified``,
    ``name``, ``picture``, and ``given_name`` / ``family_name``.
    """
    client = get_google_client()
    if client is None:
        raise RuntimeError("Google OAuth is not configured")

    token = client.authorize_access_token()
    userinfo = token.get("userinfo")
    if userinfo is None:
        resp = client.get("https://openidconnect.googleapis.com/v1/userinfo")
        resp.raise_for_status()
        userinfo = resp.json()
    return userinfo


# ---------------------------------------------------------------------------
# JWT / ID token verification (popup flow)
# ---------------------------------------------------------------------------


def _fetch_jwks() -> dict:
    """Fetch and cache Google's public JWKS for ID token verification."""
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache

    import requests
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v3/certs",
        timeout=10,
    )
    resp.raise_for_status()
    _jwks_cache = resp.json()
    return _jwks_cache


def verify_google_token(token: str) -> dict:
    """Verify a Google ID token (JWT) and return the decoded claims.

    Uses Authlib's JWT verification with Google's published JWKS.  Returns
    ``None`` if verification fails for any reason.

    The returned dict contains at minimum:
    ``sub`` (Google user ID), ``email``, ``email_verified``, ``name``,
    ``picture``, ``iat``, ``exp``, ``aud``.
    """
    try:
        jwks = _fetch_jwks()
        key_set = JsonWebKey.import_key_set(jwks)
        jwt = JsonWebToken(["RS256"])

        claims = jwt.decode(
            token,
            key_set,
            claims_options={
                "iss": {"value": ["https://accounts.google.com", "accounts.google.com"]},
                "aud": {"value": Config.GOOGLE_CLIENT_ID},
            },
        )
        claims.validate()
        return claims
    except Exception as e:
        logger.warning("Google ID token verification failed: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def serialize_userinfo(userinfo: dict) -> dict:
    """Normalise user info dict (redirect or JWT) to a consistent shape."""
    return {
        "google_id": userinfo["sub"],
        "email": userinfo.get("email", ""),
        "email_verified": userinfo.get("email_verified", False),
        "name": userinfo.get("name", ""),
        "given_name": userinfo.get("given_name", ""),
        "family_name": userinfo.get("family_name", ""),
        "picture": userinfo.get("picture", ""),
    }
