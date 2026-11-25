"""Google OAuth 2.0 login / register routes.

Lays the groundwork for "Sign in with Google".  All routes short-circuit with a
404 when ``GOOGLE_CLIENT_ID`` is not set, so they are safe to register
unconditionally.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime

import bcrypt
import psycopg2

from config import Config
from plan_service import DEFAULT_PLAN

from flask import session

from ..app import app, get_redis_connection, logger
from ..helpers.database import get_db
from ..helpers.emails import send_email
from ..helpers.login_history import record_login
from ..helpers.trusted_tester import initial_access_tier
from ..helpers.oauth import (
    google_enabled,
    google_login_redirect,
    google_callback,
    verify_google_token,
    serialize_userinfo,
)
from ..helpers.persistent_auth import (
    clear_remember_cookie,
    clear_trust_cookie,
    issue_remember_token,
    set_remember_cookie,
)

logger = logging.getLogger(__name__)


def _oauth_not_available():
    """Return a 404 response when Google OAuth is not configured."""
    from flask import abort
    abort(404)


# ---------------------------------------------------------------------------
# Route: POST /auth/google/token  (SOTA popup flow)
# ---------------------------------------------------------------------------

@app.route("/auth/google/token", methods=["POST"])
def auth_google_token():
    """Verify a Google ID token (JWT) from the GIS popup flow and log in or
    register the user.

    Expects a JSON body: ``{"credential": "<JWT>", "is_register": bool}``

    Returns a 302 redirect on success, or a JSON error on failure.
    """
    if not google_enabled():
        return _oauth_not_available()

    from flask import jsonify, make_response, redirect, render_template, request, session, url_for

    try:
        body = request.get_json(silent=True) or {}
        token = body.get("credential", "")
        is_register = body.get("is_register", False)
    except Exception:
        return jsonify({"error": "Invalid request body"}), 400

    if not token:
        return jsonify({"error": "Missing credential"}), 400

    claims = verify_google_token(token)
    if claims is None:
        return jsonify({"error": "Invalid Google token"}), 401

    info = serialize_userinfo(claims)
    return _handle_google_user(info, is_register)


# ---------------------------------------------------------------------------
# Route: /auth/google/login  (redirect fallback)
# ---------------------------------------------------------------------------

@app.route("/auth/google/login")
def auth_google_login():
    """Initiate the Google OAuth 2.0 authorisation flow (redirect).

    Redirects the user to Google's consent screen.  Returns 404 if Google
    OAuth has not been configured.
    """
    if not google_enabled():
        return _oauth_not_available()

    try:
        return google_login_redirect()
    except Exception as e:
        logger.error("Failed to initiate Google OAuth login: %s", e, exc_info=True)
        from flask import render_template
        return render_template("message.html", message="Failed to initiate Google sign-in. Please try again later.")


# ---------------------------------------------------------------------------
# Route: /auth/google/register  (redirect fallback)
# ---------------------------------------------------------------------------

@app.route("/auth/google/register")
def auth_google_register():
    """Initiate Google OAuth for registration (redirect fallback).

    Identical to login, but sets a flag in the session so the callback knows
    this is a *registration* flow rather than a login flow.
    """
    if not google_enabled():
        return _oauth_not_available()

    session["google_register_flow"] = True
    try:
        return google_login_redirect()
    except Exception as e:
        logger.error("Failed to initiate Google OAuth registration: %s", e, exc_info=True)
        session.pop("google_register_flow", None)
        from flask import render_template
        return render_template("message.html", message="Failed to initiate Google sign-up. Please try again later.")


# ---------------------------------------------------------------------------
# Route: /auth/google/callback  (redirect callback)
# ---------------------------------------------------------------------------

@app.route("/auth/google/callback")
def auth_google_callback():
    """Handle the Google OAuth 2.0 callback (redirect flow).

    Exchanges the authorisation code for user info, then either:
    * Logs the user in (existing Google-linked account), or
    * Links the Google account to an existing email-matching account, or
    * Creates a new account (registration flow).

    Returns 404 if Google OAuth has not been configured.
    """
    if not google_enabled():
        return _oauth_not_available()

    from flask import make_response, redirect, render_template, request, session, url_for

    try:
        userinfo = google_callback()
    except Exception as e:
        logger.error("Google OAuth callback error: %s", e, exc_info=True)
        return render_template("message.html", message="Google sign-in failed. Please try again.")

    info = serialize_userinfo(userinfo)
    is_register = session.pop("google_register_flow", False)
    return _handle_google_user(info, is_register)


# ---------------------------------------------------------------------------
# Shared user resolution (used by both popup and redirect flows)
# ---------------------------------------------------------------------------

def _handle_google_user(info: dict, is_register: bool):
    """Given verified Google user info, log the user in or create an account.

    Three scenarios:
    1. ``google_id`` already in DB → log in
    2. Email already in DB → link Google account and log in
    3. No match → create new account (only if in register flow)
    """
    from flask import jsonify, make_response, redirect, render_template, request, session, url_for

    google_id = info["google_id"]
    email = info["email"]
    name = info.get("name") or email.split("@")[0]

    if not email:
        msg = "Google account has no email address. Please use a different sign-in method."
        if request.path == "/auth/google/token":
            return jsonify({"error": msg}), 400
        return render_template("message.html", message=msg)

    db = get_db()

    # --- Step 1: Try to find existing user by google_id ---
    existing = db.execute(
        "SELECT u.id, u.username, u.is_admin FROM users u WHERE u.google_id = %s",
        (google_id,),
    ).fetchone()

    if existing:
        return _finalise_login(db, existing["id"], existing["username"], bool(existing["is_admin"]))

    # --- Step 2: Check if email already exists (link accounts) ---
    email_user = db.execute(
        "SELECT u.id, u.username, u.is_admin, u.auth_provider FROM users u "
        "JOIN user_credentials uc ON u.user_credential_id = uc.id "
        "WHERE uc.email = %s",
        (email,),
    ).fetchone()

    if email_user:
        db.execute(
            "UPDATE users SET google_id = %s, auth_provider = 'google' WHERE id = %s",
            (google_id, email_user["id"]),
        )
        db.commit()
        return _finalise_login(db, email_user["id"], email_user["username"], bool(email_user["is_admin"]))

    # --- Step 3: No existing user → register a new one ---
    base_username = name.lower().replace(" ", "_").replace(".", "_")
    username = base_username
    counter = 1
    while db.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone():
        username = f"{base_username}_{counter}"
        counter += 1

    user_uuid = str(uuid.uuid4())
    random_password = secrets.token_urlsafe(32)
    hashed_password = bcrypt.hashpw(random_password.encode("utf-8"), bcrypt.gensalt())

    db.execute("BEGIN")
    try:
        user_plan = DEFAULT_PLAN
        user_ip = request.remote_addr or request.environ.get("HTTP_X_FORWARDED_FOR", "unknown")

        result = db.execute(
            "INSERT INTO users (user_id, username, legal_name, plan, auth_provider, google_id, access_tier) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (user_uuid, username, info.get("name"), user_plan, "google", google_id, initial_access_tier()),
        )
        user_id = result.lastrowid

        db.execute(
            "INSERT INTO user_credentials (user_id, email, password, email_verified, plan, tos_accepted, tos_accepted_at, tos_ip_address) "
            "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)",
            (user_id, email, hashed_password, 1, user_plan, 1, user_ip),
        )

        db.execute(
            "UPDATE users SET user_credential_id = (SELECT id FROM user_credentials WHERE user_id = %s) WHERE id = %s",
            (user_id, user_id),
        )
        db.commit()

        logger.info("New user registered via Google OAuth: %s (%s)", username, email)
    except psycopg2.IntegrityError:
        db.rollback()
        msg = "This Google account is already registered."
        if request.path == "/auth/google/token":
            return jsonify({"error": msg}), 409
        return render_template("message.html", message=msg)
    except Exception as e:
        db.rollback()
        logger.error("Failed to create user via Google OAuth: %s", e, exc_info=True)
        msg = "Failed to create account. Please try again."
        if request.path == "/auth/google/token":
            return jsonify({"error": msg}), 500
        return render_template("message.html", message=msg)

    return _finalise_login(db, user_id, username, is_admin=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _finalise_login(db, user_id: int, username: str, is_admin: bool):
    """Set the Flask session and cookie headers for a newly authenticated user.

    Mirrors the session-setup logic in ``pages.py``'s login route (the 2FA-
    bypassed / DEV_MODE path) so OAuth users get the same auth signals.
    """
    from flask import make_response, redirect, request, session, url_for

    ip_address = request.remote_addr or "unknown"
    user_agent = request.user_agent.string
    now = datetime.utcnow()

    session.permanent = True
    session["user_id"] = user_id
    session["username"] = username
    session["is_admin"] = is_admin

    db.execute(
        "UPDATE users SET last_login_at = %s, last_login_ip = %s WHERE id = %s",
        (now, ip_address, user_id),
    )
    record_login(db, user_id, ip_address, user_agent)
    db.commit()

    is_popup = request.path == "/auth/google/token"
    if is_popup:
        resp = make_response("", 302)
        resp.headers["Location"] = url_for("dashboard")
        set_remember_cookie(resp, issue_remember_token(db, user_id, ip_address, user_agent))
        return resp
    else:
        resp = make_response(redirect(url_for("dashboard")))
        set_remember_cookie(resp, issue_remember_token(db, user_id, ip_address, user_agent))
        return resp
