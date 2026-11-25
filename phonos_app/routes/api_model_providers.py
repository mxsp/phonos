"""REST API routes for Unified Model Providers, Encrypted API Keys & Built-in Endpoint Overrides."""
from __future__ import annotations

import logging
from flask import jsonify, request, session

from config import Config
from ..app import app
from ..helpers import admin_required, get_db, login_required
from ..helpers.lab_accounts import get_user_auth_state
from ..helpers.model_providers import (
    PRESET_BY_ID,
    ensure_provider_schema,
    list_providers_public,
    save_instance_provider,
    save_user_provider_key,
    test_provider_connection,
    validate_endpoint_url,
)
from ..helpers.security import csrf_protect

logger = logging.getLogger(__name__)


@app.route("/api/model-providers", methods=["GET"])
@login_required
def api_list_model_providers():
    db = get_db()
    uid = int(session["user_id"])
    state = get_user_auth_state(db, uid)
    providers = list_providers_public(db, user_id=uid)

    # Also list any built-in model endpoint overrides
    overrides = {}
    try:
        rows = db.execute("SELECT model_id, endpoint_url, provider_id FROM phonos_model_endpoint_overrides").fetchall()
        for r in rows:
            d = dict(r) if isinstance(r, dict) or hasattr(r, "keys") else {"model_id": r[0], "endpoint_url": r[1], "provider_id": r[2]}
            overrides[d["model_id"]] = {"endpoint_url": d["endpoint_url"], "provider_id": d["provider_id"]}
    except Exception:
        pass

    builtin_models = [
        {"id": m.get("id"), "name": m.get("name"), "active": bool(m.get("is_active", True)), "override": overrides.get(m.get("id"))}
        for m in getattr(Config, "SUPPORTED_MODELS", [])
        if m.get("id") and not m.get("retired")
    ]
    return jsonify({
        "status": "success",
        "is_admin": bool(state.get("is_admin")),
        "system_role": state.get("system_role", "scientist"),
        "providers": providers,
        "builtin_models": builtin_models,
    })


@app.route("/api/model-providers/<provider_id>", methods=["POST"])
@login_required
@csrf_protect
def api_save_model_provider(provider_id: str):
    if provider_id not in PRESET_BY_ID:
        return jsonify({"status": "error", "message": f"Unknown provider '{provider_id}'."}), 400

    db = get_db()
    uid = int(session["user_id"])
    state = get_user_auth_state(db, uid)
    data = request.get_json(silent=True) or {}
    scope = (data.get("scope") or ("instance" if state.get("is_admin") else "user")).strip().lower()

    try:
        if scope == "instance":
            if not state.get("is_admin"):
                return jsonify({"status": "error", "message": "Administrator privileges required for instance-wide keys. Use scope='user' for personal BYOK keys."}), 403
            save_instance_provider(
                db,
                provider_id,
                api_key=data.get("api_key"),
                base_url=data.get("base_url"),
                clear_key=bool(data.get("clear_key")),
                actor_id=uid,
            )
        else:
            if state.get("system_role") == "viewer":
                return jsonify({"status": "error", "message": "Viewer accounts are read-only."}), 403
            save_user_provider_key(
                db,
                uid,
                provider_id,
                api_key=data.get("api_key"),
                base_url_override=data.get("base_url"),
                clear_key=bool(data.get("clear_key")),
            )
    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400
    except Exception as exc:
        logger.error("Failed saving provider %s: %s", provider_id, exc, exc_info=True)
        return jsonify({"status": "error", "message": "Could not store provider credentials."}), 500

    return jsonify({"status": "success", "providers": list_providers_public(db, user_id=uid)})


@app.route("/api/model-providers/<provider_id>/test", methods=["POST"])
@login_required
@csrf_protect
def api_test_model_provider(provider_id: str):
    db = get_db()
    uid = int(session["user_id"])
    data = request.get_json(silent=True) or {}
    res = test_provider_connection(
        provider_id,
        user_id=uid,
        db=db,
        custom_url=data.get("base_url"),
        raw_key=data.get("api_key"),
    )
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route("/api/model-providers/builtin-override", methods=["POST"])
@admin_required
@csrf_protect
def api_save_builtin_model_override():
    db = get_db()
    ensure_provider_schema(db)
    uid = int(session["user_id"])
    data = request.get_json(silent=True) or {}
    model_id = (data.get("model_id") or "").strip()
    endpoint_url = (data.get("endpoint_url") or "").strip()
    provider_id = (data.get("provider_id") or "nvidia-nim").strip()

    if not model_id:
        return jsonify({"status": "error", "message": "model_id is required."}), 400

    if not endpoint_url:
        db.execute("DELETE FROM phonos_model_endpoint_overrides WHERE model_id = %s", (model_id,))
        db.commit()
        return jsonify({"status": "success", "cleared": True})

    try:
        clean_url = validate_endpoint_url(endpoint_url, allow_private=True)
    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400

    existing = db.execute("SELECT 1 FROM phonos_model_endpoint_overrides WHERE model_id = %s", (model_id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE phonos_model_endpoint_overrides SET endpoint_url = %s, provider_id = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE model_id = %s",
            (clean_url, provider_id, uid, model_id),
        )
    else:
        db.execute(
            "INSERT INTO phonos_model_endpoint_overrides (model_id, endpoint_url, provider_id, updated_by) VALUES (%s, %s, %s, %s)",
            (model_id, clean_url, provider_id, uid),
        )
    db.commit()
    return jsonify({"status": "success", "model_id": model_id, "endpoint_url": clean_url})
