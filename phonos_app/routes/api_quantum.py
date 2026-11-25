"""
Phonos Quantum Settings API Routes

Endpoints for the Quantum Settings UI surfaced in two places:
1. User Account Settings page (``/profile_settings``)
2. Notebook Settings Panel + right-sidebar Account panel

All settings are backed by the ``user_quantum_settings`` and
``user_ibm_credentials`` tables (AES-256-GCM encrypted).
"""

from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request, session

from phonos_app.helpers.auth import login_required
from phonos_app.helpers.database import get_db

logger = logging.getLogger(__name__)

quantum_bp = Blueprint("quantum", __name__, url_prefix="/api/quantum")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_id() -> int:
    uid = session.get("user_id")
    if not uid:
        raise RuntimeError("Not authenticated")
    return uid


def _ensure_tables():
    """Idempotent self-heal for the quantum settings and creds tables."""
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_quantum_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            execution_mode TEXT NOT NULL DEFAULT 'simulator',
            credential_source TEXT NOT NULL DEFAULT 'platform',
            preferred_backend TEXT NOT NULL DEFAULT 'automatic',
            auto_start_session INTEGER NOT NULL DEFAULT 0,
            session_timeout_sec INTEGER NOT NULL DEFAULT 60,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_ibm_credentials (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            api_key_enc TEXT NOT NULL,
            instance_crn TEXT,
            preferred_backend TEXT,
            last_verified_at TIMESTAMP,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@quantum_bp.route("/settings", methods=["GET"])
@login_required
def get_quantum_settings():
    """Return the user's quantum execution settings + Runtime Status Panel data."""
    from phonos_app.helpers.quantum_user_creds import get_settings
    from phonos_app.helpers.quantum_runtime import get_runtime_status

    _ensure_tables()
    settings = get_settings(_user_id())
    status = get_runtime_status(_user_id())
    return jsonify({"settings": settings, "status": status})


@quantum_bp.route("/settings", methods=["PUT"])
@login_required
def put_quantum_settings():
    """Save the user's quantum execution settings."""
    from phonos_app.helpers.quantum_user_creds import save_settings

    data = request.get_json(silent=True) or {}
    execution_mode = data.get("execution_mode", "simulator")
    credential_source = data.get("credential_source", "platform")
    preferred_backend = data.get("preferred_backend", "automatic")
    auto_start_session = bool(data.get("auto_start_session", False))
    session_timeout_sec = int(data.get("session_timeout_sec", 60))

    _ensure_tables()
    try:
        settings = save_settings(
            _user_id(),
            execution_mode=execution_mode,
            credential_source=credential_source,
            preferred_backend=preferred_backend,
            auto_start_session=auto_start_session,
            session_timeout_sec=session_timeout_sec,
        )
        return jsonify({"success": True, "settings": settings})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Credentials (personal IBM Quantum API key)
# ---------------------------------------------------------------------------

@quantum_bp.route("/credentials", methods=["GET"])
@login_required
def get_quantum_creds():
    """Return metadata about the user's personal IBM credentials.

    Never exposes the API key value — only ``has_key`` + non-secret fields.
    """
    from phonos_app.helpers.quantum_user_creds import get_credentials_metadata
    return jsonify(get_credentials_metadata(_user_id()))


@quantum_bp.route("/credentials", methods=["PUT"])
@login_required
def put_quantum_creds():
    """Save (or replace) the user's personal IBM Quantum API key.

    Body: ``{api_key, instance_crn?, preferred_backend?}``.
    The key is AES-256-GCM encrypted at rest and is never echoed in the
    response — the frontend shows ``API Key Saved / **************`` based
    on ``has_key: true`` from ``GET /credentials``.
    """
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400

    from phonos_app.helpers.quantum_user_creds import save_credentials
    try:
        save_credentials(
            _user_id(),
            api_key=api_key,
            instance_crn=data.get("instance_crn") or None,
            preferred_backend=data.get("preferred_backend") or None,
        )
        return jsonify({"success": True})
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.exception("Failed to save IBM credentials")
        return jsonify({"error": "Failed to save credentials"}), 500


@quantum_bp.route("/credentials", methods=["DELETE"])
@login_required
def delete_quantum_creds():
    """Remove the user's personal IBM credentials."""
    from phonos_app.helpers.quantum_user_creds import delete_credentials
    deleted = delete_credentials(_user_id())
    return jsonify({"success": True, "deleted": deleted})


@quantum_bp.route("/credentials/verify", methods=["POST"])
@login_required
def verify_quantum_creds():
    """Prove the stored personal IBM credentials work by listing backends.

    The user's credentials are decrypted, a throwaway
    ``QiskitRuntimeService`` connection is opened, and the result is
    recorded as ``last_verified_at`` / ``last_error`` on the record.
    The key value is never returned.
    """
    from phonos_app.helpers.quantum_user_creds import (
        get_credentials,
        has_credentials,
        get_platform_credentials,
        record_verification,
    )

    # Determine which credentials to test
    data = request.get_json(silent=True) or {}
    test_personal = data.get("test_personal", True)

    api_key, instance, _ = None, None, None
    source = "none"

    if test_personal:
        if not has_credentials(_user_id()):
            return jsonify({"success": False, "error": "No personal credentials saved"}), 400
        creds = get_credentials(_user_id())
        if creds:
            api_key, instance, _ = creds
            source = "personal"
    else:
        plat = get_platform_credentials()
        if plat:
            api_key, instance, _ = plat
            source = "platform"

    if not api_key:
        return jsonify({"success": False, "error": "No credentials available to verify"}), 400

    try:
        from phonos_app.helpers.quantum_runtime import connect_runtime_service
        service = connect_runtime_service(api_key, instance)
        backends = service.backends()
        count = len(backends)
        names = [b.name for b in backends[:5]]
        if test_personal:
            record_verification(_user_id(), ok=True)
        return jsonify({
            "success": True,
            "source": source,
            "backend_count": count,
            "backends": names,
        })
    except Exception as e:
        logger.warning("IBM credential verification failed for user %s: %s", _user_id(), e)
        msg = str(e).split("\n")[0][:200]
        if test_personal:
            record_verification(_user_id(), ok=False, error=msg)
        return jsonify({"success": False, "error": msg, "source": source}), 400


# ---------------------------------------------------------------------------
# Backend list
# ---------------------------------------------------------------------------

@quantum_bp.route("/backends", methods=["GET"])
@login_required
def list_quantum_backends():
    """Available quantum backends for the Settings UI.

    With resolvable IBM credentials the hardware entries are live (qubit
    count, queue depth, operational flag; least busy first) and ``live`` is
    true. ``?refresh=1`` bypasses the 60s cache.
    """
    from phonos_app.helpers.quantum_runtime import get_available_backends
    refresh = request.args.get("refresh") in ("1", "true")
    return jsonify(get_available_backends(_user_id(), refresh=refresh))


# ---------------------------------------------------------------------------
# Runtime status (for the Runtime Status Panel)
# ---------------------------------------------------------------------------

@quantum_bp.route("/status", methods=["GET"])
@login_required
def runtime_status():
    """Snapshot of the current Runtime Status Panel data."""
    from phonos_app.helpers.quantum_runtime import get_runtime_status
    return jsonify(get_runtime_status(_user_id()))


# ---------------------------------------------------------------------------
# Route registration (called from __init__.py)
# ---------------------------------------------------------------------------

def register_quantum_routes(app):
    app.register_blueprint(quantum_bp)
    app.logger.info("Quantum Settings API routes registered")