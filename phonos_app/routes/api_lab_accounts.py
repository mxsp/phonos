"""Gimel-style Account Manager, First-Run Setup Wizard & GitHub Release Updater routes."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict

import bcrypt
from flask import jsonify, redirect, render_template, request, session, url_for

from config import Config
from ..app import app
from ..helpers import admin_required, get_db, login_required
from ..helpers.lab_accounts import (
    CANONICAL_SYSTEM_ROLES,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_SCIENTIST,
    ROLE_VIEWER,
    assert_not_last_admin,
    check_password_strength,
    create_local_account,
    ensure_default_lab_team,
    ensure_lab_accounts_schema,
    get_default_lab_team_id,
    get_lab_setting,
    get_user_auth_state,
    local_lan_ips,
    normalize_system_role,
    normalize_username,
    record_account_audit,
    set_lab_setting,
    set_user_lab_team_role,
)
from ..helpers.persistent_auth import revoke_all_remember_tokens, revoke_all_trust_devices
from ..helpers.projects import PROJECT_ROLE_MANAGER
from ..helpers.security import AccountLockout, csrf_protect, generate_csrf_token
from ..helpers import updater
from ..version import APP_VERSION

logger = logging.getLogger(__name__)


def _rdict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return {}


def _serialize_dt(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


@app.route("/api/health", methods=["GET"])
def api_healthcheck():
    db_ok = False
    db_backend = "sqlite" if str(getattr(Config, "DATABASE_URL", "")).startswith("sqlite") else "postgresql"
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({
        "ok": db_ok,
        "version": APP_VERSION,
        "mode": os.environ.get("PHONOS_MODE", "production"),
        "db_backend": db_backend,
        "local_lab_mode": bool(getattr(Config, "LOCAL_LAB_MODE", True)),
    }), (200 if db_ok else 503)


# ---------------------------------------------------------------------------
# First-Run Setup Wizard (open ONLY when 0 accounts exist in DB)
# ---------------------------------------------------------------------------
@app.route("/setup", methods=["GET"])
def first_run_setup_page():
    db = get_db()
    ensure_lab_accounts_schema(db)
    cnt_row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    cnt = int((_rdict(cnt_row).get("cnt") if cnt_row else 0) or 0)
    if cnt > 0:
        return redirect(url_for("oversight") if "user_id" in session else url_for("login"))
    return render_template("setup_wizard.html", version=APP_VERSION, csrf_token=generate_csrf_token())


@app.route("/api/accounts/setup-status", methods=["GET"])
def api_accounts_setup_status():
    db = get_db()
    ensure_lab_accounts_schema(db)
    cnt_row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    cnt = int((_rdict(cnt_row).get("cnt") if cnt_row else 0) or 0)
    port = request.host.split(":")[-1] if ":" in request.host else "5000"
    db_backend = "sqlite" if str(getattr(Config, "DATABASE_URL", "")).startswith("sqlite") else "postgresql"
    return jsonify({
        "needs_setup": cnt == 0,
        "localnet_only": bool(getattr(Config, "LOCALNET_ONLY", False)),
        "local_lab_mode": bool(getattr(Config, "LOCAL_LAB_MODE", True)),
        "mode": os.environ.get("PHONOS_MODE", "production"),
        "db_backend": db_backend,
        "version": APP_VERSION,
        "instance_name": get_lab_setting(db, "instance_name", os.environ.get("PHONOS_LAB_NAME", "Phonos Lab")),
        "lan_urls": [f"http://{ip}:{port}" for ip in local_lan_ips()],
    })


@app.route("/api/accounts/setup", methods=["POST"])
def api_accounts_setup_first_admin():
    db = get_db()
    ensure_lab_accounts_schema(db)
    cnt_row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    cnt = int((_rdict(cnt_row).get("cnt") if cnt_row else 0) or 0)
    if cnt > 0:
        return jsonify({"status": "error", "message": "Initial setup is already complete."}), 409

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    lab_name = (data.get("lab_name") or data.get("plant_name") or "Phonos Lab").strip()[:160]
    allow_signup = bool(data.get("allow_public_signup", False))

    try:
        acct = create_local_account(
            db,
            username=username,
            password=password,
            system_role=ROLE_ADMIN,
            email=data.get("email"),
            legal_name=data.get("legal_name") or username,
            actor=username or "setup",
        )
        team_id = ensure_default_lab_team(db, acct["id"], lab_name=lab_name)
        set_lab_setting(db, "allow_public_signup", "1" if allow_signup else "0")
        Config.ALLOW_PUBLIC_SIGNUP = allow_signup
        record_account_audit(
            db,
            actor=acct["username"],
            action="account_setup_admin",
            target_user_id=acct["id"],
            detail={"lab_name": lab_name, "default_team_id": team_id},
        )
        db.commit()
    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400
    except Exception as exc:
        logger.error("First-run admin setup failed: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": "Setup failed."}), 500

    session.permanent = True
    session["user_id"] = acct["id"]
    session["username"] = acct["username"]
    session["is_admin"] = True
    session["system_role"] = ROLE_ADMIN
    return jsonify({
        "status": "success",
        "account": acct,
        "lab_name": lab_name,
        "redirect": url_for("oversight", auto=1),
    }), 201


# ---------------------------------------------------------------------------
# Admin Account Manager UI & CRUD API
# ---------------------------------------------------------------------------
@app.route("/admin/accounts", methods=["GET"])
@admin_required
def admin_accounts_page():
    return render_template(
        "admin_accounts.html",
        version=APP_VERSION,
        csrf_token=generate_csrf_token(),
    )


def _format_account_row(db, row: Any, default_team_id: int | None) -> Dict[str, Any]:
    d = _rdict(row)
    uid = int(d["id"])
    uname = d.get("username") or ""
    role = d.get("system_role") or (ROLE_ADMIN if d.get("is_admin") else ROLE_SCIENTIST)
    active = bool(d.get("is_active", 1) if d.get("is_active") is not None else 1)

    # Check active remember tokens as session indicator
    sess_cnt = 0
    try:
        sr = db.execute("SELECT COUNT(*) as cnt FROM remember_tokens WHERE user_id = %s AND revoked = 0", (uid,)).fetchone()
        sess_cnt = int((_rdict(sr).get("cnt") if sr else 0) or 0)
    except Exception:
        pass

    proj_cnt = 0
    try:
        pr = db.execute("SELECT COUNT(*) as cnt FROM project_assignments WHERE user_id = %s", (uid,)).fetchone()
        proj_cnt = int((_rdict(pr).get("cnt") if pr else 0) or 0)
    except Exception:
        pass

    lab_team_role = "none"
    if default_team_id:
        tm = db.execute(
            "SELECT role FROM team_members WHERE team_id = %s AND user_id = %s",
            (default_team_id, uid),
        ).fetchone()
        if tm:
            lab_team_role = _rdict(tm).get("role") or "member"

    locked = False
    try:
        lk = db.execute(
            "SELECT locked_until FROM account_lockouts WHERE identifier = %s ORDER BY locked_until DESC LIMIT 1",
            (uname,),
        ).fetchone()
        if lk and _rdict(lk).get("locked_until"):
            lu = _rdict(lk)["locked_until"]
            if hasattr(lu, "timestamp") and lu > datetime.utcnow():
                locked = True
    except Exception:
        pass

    return {
        "id": uid,
        "username": uname,
        "email": d.get("email") or "",
        "legal_name": d.get("legal_name") or "",
        "role": role,
        "is_admin": bool(d.get("is_admin")) or role == ROLE_ADMIN,
        "active": active,
        "credits": int(d.get("credits") or 0),
        "project_count": proj_cnt,
        "lab_team_role": lab_team_role,
        "locked": locked,
        "active_sessions": sess_cnt,
        "created_at": _serialize_dt(d.get("created_at")),
        "last_login_at": _serialize_dt(d.get("last_login_at")),
    }


@app.route("/api/accounts/summary", methods=["GET"])
@admin_required
def api_accounts_summary():
    db = get_db()
    ensure_lab_accounts_schema(db)
    default_tid = get_default_lab_team_id(db)
    rows = db.execute(
        """
        SELECT u.id, u.username, u.legal_name, u.is_admin, u.system_role, u.is_active, u.credits,
               u.created_at, u.last_login_at, uc.email
        FROM users u
        LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id
        ORDER BY u.id ASC
        """
    ).fetchall()
    accounts = [_format_account_row(db, r, default_tid) for r in rows]
    return jsonify({
        "total": len(accounts),
        "admins": sum(1 for a in accounts if a["role"] == ROLE_ADMIN and a["active"]),
        "scientists": sum(1 for a in accounts if a["role"] == ROLE_SCIENTIST and a["active"]),
        "operators": sum(1 for a in accounts if a["role"] == ROLE_OPERATOR and a["active"]),
        "viewers": sum(1 for a in accounts if a["role"] == ROLE_VIEWER and a["active"]),
        "active": sum(1 for a in accounts if a["active"]),
        "inactive": sum(1 for a in accounts if not a["active"]),
        "locked": sum(1 for a in accounts if a["locked"]),
        "instance_name": get_lab_setting(db, "instance_name", "Phonos Lab"),
        "default_team_id": default_tid,
        "allow_public_signup": bool(getattr(Config, "ALLOW_PUBLIC_SIGNUP", True)),
    })


@app.route("/api/accounts", methods=["GET", "POST"])
@admin_required
@csrf_protect
def api_accounts_collection():
    db = get_db()
    ensure_lab_accounts_schema(db)
    default_tid = get_default_lab_team_id(db)

    if request.method == "GET":
        q = (request.args.get("q") or "").strip().lower()
        role_filter = (request.args.get("role") or "").strip().lower()
        rows = db.execute(
            """
            SELECT u.id, u.username, u.legal_name, u.is_admin, u.system_role, u.is_active, u.credits,
                   u.created_at, u.last_login_at, uc.email
            FROM users u
            LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id
            ORDER BY LOWER(u.username) ASC, u.id ASC
            """
        ).fetchall()
        items = [_format_account_row(db, r, default_tid) for r in rows]
        if q:
            items = [a for a in items if q in a["username"].lower() or q in a["email"].lower() or q in a["legal_name"].lower()]
        if role_filter and role_filter in CANONICAL_SYSTEM_ROLES:
            items = [a for a in items if a["role"] == role_filter]
        return jsonify({"accounts": items, "default_team_id": default_tid})

    # POST: Create local account
    data = request.get_json(silent=True) or {}
    actor = session.get("username") or "admin"
    if default_tid is None:
        default_tid = ensure_default_lab_team(db, int(session["user_id"]), get_lab_setting(db, "instance_name", "Phonos Lab"))
        db.commit()

    try:
        acct = create_local_account(
            db,
            username=data.get("username") or "",
            password=data.get("password") or "",
            system_role=data.get("role") or ROLE_SCIENTIST,
            email=data.get("email"),
            legal_name=data.get("legal_name"),
            credits=int(data["credits"]) if data.get("credits") is not None and str(data["credits"]).strip() != "" else None,
            lab_team_role=data.get("lab_team_role"),
            actor=actor,
        )
        row = db.execute(
            """
            SELECT u.id, u.username, u.legal_name, u.is_admin, u.system_role, u.is_active, u.credits,
                   u.created_at, u.last_login_at, uc.email
            FROM users u LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE u.id = %s
            """,
            (acct["id"],),
        ).fetchone()
        return jsonify(_format_account_row(db, row, default_tid)), 201
    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400
    except Exception as exc:
        logger.error("Error creating account: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": "Failed to create account."}), 500


@app.route("/api/accounts/<int:target_id>", methods=["PATCH", "DELETE"])
@admin_required
@csrf_protect
def api_accounts_item(target_id: int):
    db = get_db()
    ensure_lab_accounts_schema(db)
    actor_id = int(session["user_id"])
    actor_name = session.get("username") or "admin"
    default_tid = get_default_lab_team_id(db)

    existing = db.execute(
        """
        SELECT u.id, u.username, u.legal_name, u.is_admin, u.system_role, u.is_active, u.credits,
               u.created_at, u.last_login_at, uc.email
        FROM users u LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE u.id = %s
        """,
        (target_id,),
    ).fetchone()
    if not existing:
        return jsonify({"status": "error", "message": "Account not found."}), 404
    cur = _rdict(existing)

    if request.method == "DELETE":
        if target_id == actor_id:
            return jsonify({"status": "error", "message": "You cannot delete your own account."}), 409
        try:
            assert_not_last_admin(db, target_id)
        except ValueError as ve:
            return jsonify({"status": "error", "message": str(ve)}), 409

        data = request.get_json(silent=True) or {}
        reassign_to = data.get("reassign_to")
        if reassign_to:
            try:
                new_owner_id = int(reassign_to)
                owned_projs = db.execute(
                    "SELECT project_id FROM project_assignments WHERE user_id = %s AND role = %s",
                    (target_id, PROJECT_ROLE_MANAGER),
                ).fetchall()
                for pr in owned_projs:
                    pid = _rdict(pr).get("project_id") or pr[0]
                    has_new = db.execute(
                        "SELECT 1 FROM project_assignments WHERE project_id = %s AND user_id = %s",
                        (pid, new_owner_id),
                    ).fetchone()
                    if has_new:
                        db.execute(
                            "UPDATE project_assignments SET role = %s WHERE project_id = %s AND user_id = %s",
                            (PROJECT_ROLE_MANAGER, pid, new_owner_id),
                        )
                    else:
                        db.execute(
                            "INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, %s)",
                            (pid, new_owner_id, PROJECT_ROLE_MANAGER),
                        )
            except Exception as exc:
                logger.warning("Project reassignment warning: %s", exc)

        revoke_all_remember_tokens(db, target_id)
        revoke_all_trust_devices(db, target_id)

        has_jobs = db.execute("SELECT 1 FROM jobs WHERE creator_id = %s LIMIT 1", (target_id,)).fetchone()
        has_projs = db.execute("SELECT 1 FROM project_assignments WHERE user_id = %s LIMIT 1", (target_id,)).fetchone()
        if has_jobs or has_projs:
            db.execute("UPDATE users SET is_active = 0 WHERE id = %s", (target_id,))
            record_account_audit(
                db,
                actor=actor_name,
                action="account_deactivate",
                target_user_id=target_id,
                detail={"reason": "has_projects_or_jobs", "reassign_to": reassign_to},
            )
            db.commit()
            return jsonify({"action": "deactivated", "detail": "Account deactivated (scientific jobs and project records preserved)."})

        db.execute("DELETE FROM users WHERE id = %s", (target_id,))
        record_account_audit(db, actor=actor_name, action="account_delete", target_user_id=target_id, detail={"username": cur.get("username")})
        db.commit()
        return jsonify({"action": "deleted", "detail": "Account permanently deleted."})

    # PATCH
    data = request.get_json(silent=True) or {}
    changed: Dict[str, Any] = {}
    revoke = False

    if "username" in data and data["username"] is not None:
        try:
            new_uname = normalize_username(data["username"])
        except ValueError as ve:
            return jsonify({"status": "error", "message": str(ve)}), 400
        if new_uname != cur.get("username"):
            clash = db.execute("SELECT 1 FROM users WHERE LOWER(username) = %s AND id != %s", (new_uname, target_id)).fetchone()
            if clash:
                return jsonify({"status": "error", "message": "Username is already taken."}), 409
            db.execute("UPDATE users SET username = %s WHERE id = %s", (new_uname, target_id))
            changed["username"] = [cur.get("username"), new_uname]

    if "role" in data and data["role"] is not None:
        try:
            new_role = normalize_system_role(data["role"])
        except ValueError as ve:
            return jsonify({"status": "error", "message": str(ve)}), 400
        old_role = cur.get("system_role") or (ROLE_ADMIN if cur.get("is_admin") else ROLE_SCIENTIST)
        if new_role != old_role:
            if target_id == actor_id:
                return jsonify({"status": "error", "message": "Another administrator must change your own global role."}), 409
            if old_role == ROLE_ADMIN and new_role != ROLE_ADMIN:
                try:
                    assert_not_last_admin(db, target_id)
                except ValueError as ve:
                    return jsonify({"status": "error", "message": str(ve)}), 409
            is_adm = 1 if new_role == ROLE_ADMIN else 0
            db.execute("UPDATE users SET system_role = %s, is_admin = %s WHERE id = %s", (new_role, is_adm, target_id))
            changed["role"] = [old_role, new_role]
            revoke = True

    if "active" in data and data["active"] is not None:
        new_active = bool(data["active"])
        old_active = bool(cur.get("is_active", 1) if cur.get("is_active") is not None else 1)
        if new_active != old_active:
            if not new_active:
                if target_id == actor_id:
                    return jsonify({"status": "error", "message": "You cannot deactivate your own account."}), 409
                try:
                    assert_not_last_admin(db, target_id)
                except ValueError as ve:
                    return jsonify({"status": "error", "message": str(ve)}), 409
                revoke = True
            db.execute("UPDATE users SET is_active = %s WHERE id = %s", (1 if new_active else 0, target_id))
            changed["active"] = [old_active, new_active]

    if "credits" in data and data["credits"] is not None:
        try:
            new_credits = max(0, int(data["credits"]))
            db.execute("UPDATE users SET credits = %s WHERE id = %s", (new_credits, target_id))
            changed["credits"] = [int(cur.get("credits") or 0), new_credits]
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Credits must be an integer."}), 400

    if "lab_team_role" in data:
        if default_tid is None:
            default_tid = ensure_default_lab_team(db, actor_id, get_lab_setting(db, "instance_name", "Phonos Lab"))
        mapped = set_user_lab_team_role(db, target_id, data.get("lab_team_role"))
        changed["lab_team_role"] = mapped or "none"

    if revoke:
        revoke_all_remember_tokens(db, target_id)
        revoke_all_trust_devices(db, target_id)

    if changed:
        record_account_audit(db, actor=actor_name, action="account_update", target_user_id=target_id, detail=changed)
        db.commit()

    refreshed = db.execute(
        """
        SELECT u.id, u.username, u.legal_name, u.is_admin, u.system_role, u.is_active, u.credits,
               u.created_at, u.last_login_at, uc.email
        FROM users u LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE u.id = %s
        """,
        (target_id,),
    ).fetchone()
    return jsonify(_format_account_row(db, refreshed, default_tid))


@app.route("/api/accounts/<int:target_id>/password", methods=["POST"])
@admin_required
@csrf_protect
def api_accounts_set_password(target_id: int):
    db = get_db()
    ensure_lab_accounts_schema(db)
    data = request.get_json(silent=True) or {}
    new_pw = data.get("password") or ""
    row = db.execute(
        "SELECT u.id, u.username, uc.email FROM users u LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE u.id = %s",
        (target_id,),
    ).fetchone()
    if not row:
        return jsonify({"status": "error", "message": "Account not found."}), 404
    d = _rdict(row)
    try:
        check_password_strength(new_pw, username=d.get("username") or "", email=d.get("email") or "")
    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400

    hashed = bcrypt.hashpw(new_pw.encode("utf-8"), bcrypt.gensalt())
    db.execute("UPDATE user_credentials SET password = %s WHERE user_id = %s", (hashed, target_id))
    revoke_all_remember_tokens(db, target_id)
    revoke_all_trust_devices(db, target_id)
    try:
        db.execute("DELETE FROM account_lockouts WHERE identifier = %s", (d.get("username"),))
    except Exception:
        pass
    record_account_audit(db, actor=session.get("username") or "admin", action="account_password_set", target_user_id=target_id)
    db.commit()
    return jsonify({"status": "success"})


@app.route("/api/accounts/<int:target_id>/unlock", methods=["POST"])
@admin_required
@csrf_protect
def api_accounts_unlock(target_id: int):
    db = get_db()
    row = db.execute(
        "SELECT u.id, u.username, uc.email FROM users u LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id WHERE u.id = %s",
        (target_id,),
    ).fetchone()
    if not row:
        return jsonify({"status": "error", "message": "Account not found."}), 404
    d = _rdict(row)
    for ident in filter(None, [d.get("username"), d.get("email")]):
        try:
            db.execute("DELETE FROM account_lockouts WHERE identifier = %s", (ident,))
        except Exception:
            pass
    record_account_audit(db, actor=session.get("username") or "admin", action="account_unlock", target_user_id=target_id)
    db.commit()
    return jsonify({"status": "success"})


@app.route("/api/accounts/<int:target_id>/logout-all", methods=["POST"])
@admin_required
@csrf_protect
def api_accounts_logout_all(target_id: int):
    db = get_db()
    revoke_all_remember_tokens(db, target_id)
    revoke_all_trust_devices(db, target_id)
    record_account_audit(db, actor=session.get("username") or "admin", action="account_logout_all", target_user_id=target_id)
    db.commit()
    return jsonify({"status": "success"})


@app.route("/api/accounts/<int:target_id>/audit", methods=["GET"])
@admin_required
def api_accounts_audit(target_id: int):
    db = get_db()
    ensure_lab_accounts_schema(db)
    rows = db.execute(
        "SELECT id, at, actor, action, detail FROM account_audit_log WHERE target_user_id = %s ORDER BY id DESC LIMIT 50",
        (target_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = _rdict(r)
        try:
            det = json.loads(d.get("detail") or "{}")
        except Exception:
            det = {}
        out.append({
            "id": d.get("id"),
            "at": _serialize_dt(d.get("at")),
            "actor": d.get("actor"),
            "action": d.get("action"),
            "detail": det,
        })
    return jsonify({"audit": out})


@app.route("/api/accounts/lab-settings", methods=["POST"])
@admin_required
@csrf_protect
def api_accounts_lab_settings():
    db = get_db()
    ensure_lab_accounts_schema(db)
    data = request.get_json(silent=True) or {}
    if "instance_name" in data:
        name = str(data["instance_name"]).strip()[:120] or "Phonos Lab"
        set_lab_setting(db, "instance_name", name)
        tid = get_default_lab_team_id(db)
        if tid:
            db.execute("UPDATE teams SET name = %s WHERE id = %s", (name, tid))
    if "allow_public_signup" in data:
        allow = bool(data["allow_public_signup"])
        set_lab_setting(db, "allow_public_signup", "1" if allow else "0")
        Config.ALLOW_PUBLIC_SIGNUP = allow
    db.commit()
    return jsonify({"status": "success"})


# ---------------------------------------------------------------------------
# GitHub Release Updater Routes (/api/updates/*)
# ---------------------------------------------------------------------------
@app.route("/api/updates/status", methods=["GET"])
@login_required
def api_updates_status():
    force = request.args.get("force", "false").lower() in ("1", "true", "yes")
    state = get_user_auth_state(get_db(), session.get("user_id"))
    allow_force = bool(force and state.get("is_admin"))
    return jsonify(updater.check_for_update(force=allow_force).to_dict())


@app.route("/api/updates/apply", methods=["POST"])
@admin_required
@csrf_protect
def api_updates_apply():
    data = request.get_json(silent=True) or {}
    target_tag = (data.get("tag") or "").strip() or None
    auto_restart = bool(data.get("auto_restart", True))
    try:
        res = updater.apply_update(target_tag=target_tag, auto_restart=auto_restart)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Update failed: {exc}"}), 500

    db = get_db()
    record_account_audit(
        db,
        actor=session.get("username") or "admin",
        action="system_update",
        target_user_id=None,
        detail=res.to_dict(),
    )
    db.commit()
    return jsonify(res.to_dict())


@app.route("/api/updates/restart", methods=["POST"])
@admin_required
@csrf_protect
def api_updates_restart():
    ok = updater.schedule_restart()
    db = get_db()
    record_account_audit(
        db,
        actor=session.get("username") or "admin",
        action="system_restart",
        target_user_id=None,
        detail={"scheduled": ok},
    )
    db.commit()
    return jsonify({
        "ok": ok,
        "restarting": ok,
        "detail": "Phonos is restarting…" if ok else "Container restarts are managed via Docker on the host.",
    })
