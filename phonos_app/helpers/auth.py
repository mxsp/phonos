"""Authentication and authorisation decorators."""
from __future__ import annotations

from functools import wraps
import logging

from flask import abort, g, jsonify, redirect, request, session, url_for

from .database import get_db
from .organizations import get_organization_membership
from .projects import get_project_assignment

logger = logging.getLogger(__name__)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # logger.info(f"login_required: path={request.path}, session={session}")
        
        # Check for Notebook Session Token (Service-to-Service / Kernel Auth)
        if "user_id" not in session:
            nb_token = request.headers.get('X-Notebook-Session')
            if nb_token:
                try:
                    db = get_db()
                    # Check for active session
                    row = db.execute(
                        "SELECT user_id FROM notebook_sessions WHERE session_token = %s AND kernel_status IN ('idle', 'busy', 'starting')",
                        (nb_token,)
                    ).fetchone()
                    if row:
                        session['user_id'] = row['user_id']
                        # Mark as notebook session to potentially avoid setting permanent cookies if needed
                        g.is_notebook_request = True
                        logger.info(f"Authenticated request via notebook token for user {row['user_id']}")
                except Exception as e:
                    logger.error(f"Error checking notebook token: {e}")

        if "user_id" not in session:
            # If user is in 2FA flow, only allow access to the 2FA page
            if '2fa_user_id' in session and request.path == url_for('verify_2fa'):
                return f(*args, **kwargs)

            # Return JSON 401 for API routes and download routes
            if request.path.startswith("/api/") or request.path.startswith("/download"):
                return jsonify({"status": "error", "message": "Authentication required."}), 401
            return redirect(url_for("login"))

        try:
            from .lab_accounts import get_user_auth_state
            state = get_user_auth_state(get_db(), session["user_id"])
            if state.get("exists") and not state.get("active"):
                session.clear()
                if request.path.startswith("/api/") or request.path.startswith("/download"):
                    return jsonify({"status": "error", "message": "Account is deactivated."}), 401
                return redirect(url_for("login"))
            g.current_system_role = state.get("system_role", "scientist")
            g.current_user_is_admin = bool(state.get("is_admin"))
        except Exception:
            pass
        return f(*args, **kwargs)

    return decorated_function


def project_role_required(allowed_roles: set[str]):
    """Ensure the current user holds one of the provided roles for the project."""

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            project_id = kwargs.get("project_id")
            if not project_id:
                return (
                    jsonify({
                        "status": "error",
                        "message": "Project ID missing from request URL.",
                    }),
                    400,
                )

            if "user_id" not in session:
                return jsonify({"status": "error", "message": "Authentication required."}), 401

            db = get_db()
            user_id = session["user_id"]
            role = get_project_assignment(db, user_id, project_id)

            if not role or role not in allowed_roles:
                return (
                    jsonify({
                        "status": "error",
                        "message": "You do not have the required permissions for this project.",
                    }),
                    403,
                )

            g.current_project_role = role
            return f(*args, **kwargs)

        return decorated_function

    return decorator


def organization_role_required(allowed_roles: set[str]):
    """Ensure the current user holds one of the provided roles for the organisation."""

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            organization_id = kwargs.get("organization_id")
            if organization_id is None:
                organization_id = kwargs.get("team_id")
            if organization_id is None and args:
                organization_id = args[0]

            if not organization_id:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Organization ID missing from request URL.",
                        }
                    ),
                    400,
                )

            try:
                organization_id = int(organization_id)
            except (TypeError, ValueError):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Organization ID missing from request URL.",
                        }
                    ),
                    400,
                )

            if "user_id" not in session:
                return jsonify({"status": "error", "message": "Authentication required."}), 401

            db = get_db()
            user_id = session["user_id"]
            role = get_organization_membership(db, user_id, organization_id)

            if not role:
                return jsonify({"status": "error", "message": "Organization not found."}), 404
            # Normalize role comparison to lowercase to match database values
            if role.lower() not in {r.lower() for r in allowed_roles}:
                return (
                    jsonify({
                        "status": "error",
                        "message": "You do not have the required permissions for this organization.",
                    }),
                    403,
                )

            g.current_organization_role = role
            g.current_team_role = role
            return f(*args, **kwargs)

        return decorated_function

    return decorator


def admin_required(f):
    """Ensure the current user is an authenticated administrator."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        wants_json = request.path.startswith("/api/")

        if "user_id" not in session:
            if wants_json:
                return jsonify({"status": "error", "message": "Authentication required."}), 401
            return redirect(url_for("login"))

        db = get_db()
        user = db.execute(
            "SELECT is_admin FROM users WHERE id = %s", (session["user_id"],)
        ).fetchone()

        if not user or not user["is_admin"]:
            if wants_json:
                return (
                    jsonify({
                        "status": "error",
                        "message": "Administrator privileges required.",
                    }),
                    403,
                )
            abort(403)

        g.current_user_is_admin = True
        return f(*args, **kwargs)

    return decorated_function
