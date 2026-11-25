"""HTTP surface of Phonos Copilot: page, JSON API and the SSE chat stream."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    session,
    stream_with_context,
)

from ..helpers import get_db, login_required
from ..helpers.security import csrf_protect
from . import jobtokens, locks, page_context
from . import settings as agent_settings
from . import store, viewer_context
from .db import ensure_schema, independent_db
from .runner import run_stream
from .tools import (
    ToolContext,
    ToolError,
    _job_artifacts,
    _require_job,
    _require_project,
    execute_tool,
    public_catalog,
    tool_summary,
)

agent_bp = Blueprint("agent", __name__, url_prefix="")

_ACTIVE: dict[int, threading.Event] = {}
_ACTIVE_LOCK = threading.Lock()


def _sse(event: dict) -> str:
    name = event.get("type") or "message"
    data = json.dumps(event, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {data}\n\n"


def _user(db) -> dict:
    uid = session.get("user_id")
    row = db.execute(
        "SELECT u.id, u.username, u.plan, u.credits, u.is_admin, u.storage_quota, "
        "u.storage_used, uc.email "
        "FROM users u LEFT JOIN user_credentials uc ON uc.id = u.user_credential_id "
        "WHERE u.id = %s",
        (uid,),
    ).fetchone()
    if not row:
        return {"id": uid, "username": "unknown", "is_admin": False, "plan": "unknown"}
    item = dict(row)
    item["is_admin"] = bool(item.get("is_admin"))
    return item


def _json(payload, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message: str, status: int = 400):
    return _json({"status": "error", "message": message}, status)


def _require_admin(user: dict):
    if not user.get("is_admin"):
        return _error("Administrator privileges are required.", 403)
    return None


# ---------------------------------------------------------------------------
# page + health
# ---------------------------------------------------------------------------
@agent_bp.route("/agent", methods=["GET"])
@login_required
def agent_page():
    db = get_db()
    user = _user(db)
    response = Response(
        render_template(
            "agent.html",
            current_user=user,
            agent_mode="page",
        ),
        mimetype="text/html",
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Permissions-Policy"] = "microphone=(self)"
    return response


@agent_bp.route("/api/agent/health", methods=["GET"])
@login_required
def health():
    db = get_db()
    ensure_schema(db)
    settings = agent_settings.read_settings(db)
    return _json({
        "status": "ok",
        "tools": tool_summary(),
        "model": settings.get("model"),
        "provider": settings.get("provider_label"),
        "api_key_set": bool(settings.get("api_key_set")),
        "web_enabled": bool(settings.get("web_enabled")),
    })


# ---------------------------------------------------------------------------
# settings and provider
# ---------------------------------------------------------------------------
@agent_bp.route("/api/agent/config", methods=["GET"])
@login_required
def config():
    db = get_db()
    user = _user(db)
    ensure_schema(db)
    settings = agent_settings.public_settings(db)
    return _json({
        "status": "success",
        "user": {
            "id": user.get("id"),
            "username": user.get("username"),
            "email": user.get("email"),
            "plan": user.get("plan"),
            "is_admin": bool(user.get("is_admin")),
            "credits": user.get("credits"),
            "storage_quota": user.get("storage_quota"),
            "storage_used": user.get("storage_used"),
        },
        "settings": settings,
        "presets": agent_settings.preset_list(),
        "tools": tool_summary(),
        "limits": {
            "max_steps": 20,
            "max_history": 100,
            "max_text_chars": 24000,
        },
    })


@agent_bp.route("/api/agent/settings", methods=["GET"])
@login_required
def read_settings():
    db = get_db()
    ensure_schema(db)
    return _json({"status": "success", "settings": agent_settings.public_settings(db)})


@agent_bp.route("/api/agent/settings", methods=["PUT"])
@login_required
@csrf_protect
def write_settings():
    db = get_db()
    user = _user(db)
    denied = _require_admin(user)
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    try:
        saved = agent_settings.save_settings(db, user["id"], payload)
    except agent_settings.SettingsError as exc:
        return _error(str(exc), 400)
    agent_settings.model_cache_clear()
    return _json({"status": "success", "settings": saved})


@agent_bp.route("/api/agent/models", methods=["GET"])
@login_required
def models():
    db = get_db()
    try:
        rows = agent_settings.fetch_models(
            db, refresh=request.args.get("refresh") in ("1", "true", "yes")
        )
    except agent_settings.SettingsError as exc:
        return _error(str(exc), 502)
    return _json({"status": "success", "models": rows})


@agent_bp.route("/api/agent/provider/test", methods=["POST"])
@login_required
@csrf_protect
def test_provider():
    db = get_db()
    user = _user(db)
    denied = _require_admin(user)
    if denied:
        return denied
    try:
        result = agent_settings.test_provider(db)
    except agent_settings.SettingsError as exc:
        return _error(str(exc), 502)
    return _json({"status": "success", **result})


# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------
@agent_bp.route("/api/agent/chats", methods=["GET"])
@login_required
def chats():
    db = get_db()
    user = _user(db)
    ensure_schema(db)
    return _json({"status": "success", "chats": store.list_chats(db, user["id"])})


@agent_bp.route("/api/agent/chats", methods=["POST"])
@login_required
@csrf_protect
def create_chat():
    db = get_db()
    user = _user(db)
    payload = request.get_json(silent=True) or {}
    settings = agent_settings.read_settings(db)
    chat = store.create_chat(
        db,
        user["id"],
        title=payload.get("title") or "",
        model=payload.get("model") or settings.get("model") or "",
        provider=payload.get("provider") or settings.get("provider_label") or "",
    )
    return _json({"status": "success", "chat": chat}, 201)


@agent_bp.route("/api/agent/chats/<int:chat_id>", methods=["GET"])
@login_required
def get_chat(chat_id: int):
    db = get_db()
    user = _user(db)
    chat = store.get_chat(db, user["id"], chat_id)
    if not chat:
        return _error("Chat not found.", 404)
    return _json({
        "status": "success",
        "chat": chat,
        "messages": store.messages_for(db, chat_id),
    })


@agent_bp.route("/api/agent/chats/<int:chat_id>", methods=["PATCH"])
@login_required
@csrf_protect
def patch_chat(chat_id: int):
    db = get_db()
    user = _user(db)
    payload = request.get_json(silent=True) or {}
    chat = store.update_chat(
        db, user["id"], chat_id,
        title=payload.get("title"),
        model=payload.get("model"),
        provider=payload.get("provider"),
        write_mode=payload.get("write_mode"),
    )
    if not chat:
        return _error("Chat not found.", 404)
    return _json({"status": "success", "chat": chat})


@agent_bp.route("/api/agent/chats/<int:chat_id>", methods=["DELETE"])
@login_required
@csrf_protect
def delete_chat(chat_id: int):
    db = get_db()
    user = _user(db)
    if not store.soft_delete(db, user["id"], chat_id):
        return _error("Chat not found.", 404)
    return _json({"status": "success", "deleted": True})


@agent_bp.route("/api/agent/chats/<int:chat_id>/messages", methods=["POST"])
@login_required
@csrf_protect
def post_message(chat_id: int):
    db = get_db()
    user = _user(db)
    chat = store.get_chat(db, user["id"], chat_id)
    if not chat:
        return _error("Chat not found.", 404)
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("message") or payload.get("content") or "").strip()
    if not prompt:
        return _error("The message is empty.", 422)
    if len(prompt) > 32000:
        return _error("The message is longer than 32,000 characters.", 413)
    context = page_context.sanitize(payload.get("context"))
    if not locks.acquire(chat_id):
        return _error("An answer is already running in this chat.", 409)
    with _ACTIVE_LOCK:
        if chat_id in _ACTIVE:
            locks.release(chat_id)
            return _error("An answer is already running in this chat.", 409)
        cancel = threading.Event()
        _ACTIVE[chat_id] = cancel
    token = locks.CancelToken(chat_id, cancel)

    def events() -> Iterator[str]:
        try:
            yield ": ping\n\n"
            with independent_db() as stream_db:
                for event in run_stream(
                    db=stream_db,
                    user=user,
                    chat=chat,
                    prompt=prompt,
                    context=context,
                    cancel_event=token,
                ):
                    yield _sse(event)
        except GeneratorExit:  # client disconnected
            cancel.set()
            raise
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "detail": f"The assistant run failed: {exc}"})
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.pop(chat_id, None)
            locks.release(chat_id)
            locks.clear_cancel(chat_id)

    response = Response(
        stream_with_context(events()),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    response.headers["Content-Security-Policy"] = "default-src 'none'"
    return response


@agent_bp.route("/api/agent/chats/<int:chat_id>/cancel", methods=["POST"])
@login_required
@csrf_protect
def cancel_chat(chat_id: int):
    db = get_db()
    user = _user(db)
    if not store.get_chat(db, user["id"], chat_id):
        return _error("Chat not found.", 404)
    with _ACTIVE_LOCK:
        event = _ACTIVE.get(chat_id)
    if event is not None:
        event.set()
        locks.cancel(chat_id)
        return _json({"status": "success", "cancelled": True})
    locks.cancel(chat_id)
    return _json({"status": "success", "cancelled": True})


@agent_bp.route("/api/agent/chats/<int:chat_id>/rewind", methods=["POST"])
@login_required
@csrf_protect
def rewind(chat_id: int):
    db = get_db()
    user = _user(db)
    payload = request.get_json(silent=True) or {}
    try:
        message_id = int(payload.get("message_id"))
    except (TypeError, ValueError):
        return _error("message_id is required.", 422)
    result = store.rewind(db, user["id"], chat_id, message_id)
    if not result.get("ok"):
        return _error(result.get("error") or "Rewind failed.", 404)
    return _json({"status": "success", **result})


# ---------------------------------------------------------------------------
# tools and stats
# ---------------------------------------------------------------------------
@agent_bp.route("/api/agent/tools", methods=["GET"])
@login_required
def tools():
    db = get_db()
    settings = agent_settings.read_settings(db)
    catalog = public_catalog(web_enabled=bool(settings.get("web_enabled")))
    return _json({
        "status": "success",
        "tools": catalog,
        "summary": tool_summary(),
    })


@agent_bp.route("/api/agent/tools/<name>/run", methods=["POST"])
@login_required
@csrf_protect
def run_tool(name: str):
    db = get_db()
    user = _user(db)
    payload = request.get_json(silent=True) or {}
    context = page_context.sanitize(payload.get("context"))
    settings = agent_settings.read_settings(db)
    ctx = ToolContext(
        db=db,
        user_id=user["id"],
        is_admin=bool(user.get("is_admin")),
        context=context,
    )
    try:
        result = execute_tool(
            ctx, name, payload.get("arguments") or {},
            web_enabled=bool(settings.get("web_enabled")),
        )
    except ToolError as exc:
        return _json({"status": "error", "message": str(exc), "result": {"ok": False}}, 400)
    return _json({"status": "success", "result": result})


@agent_bp.route("/api/agent/stats", methods=["GET"])
@login_required
def stats():
    db = get_db()
    user = _user(db)
    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    return _json({"status": "success", **store.stats(db, user["id"], days)})


@agent_bp.route("/api/agent/context", methods=["POST"])
@login_required
@csrf_protect
def echo_context():
    """Debug/diagnostic endpoint: show exactly what the server would send."""
    payload = request.get_json(silent=True) or {}
    context = page_context.sanitize(payload.get("context"))
    return _json({
        "status": "success",
        "context": context,
        "rendered": page_context.render(context).strip(),
    })


@agent_bp.route("/api/agent/jobs", methods=["POST"])
@login_required
@csrf_protect
def submit_agent_job():
    """Run a bio-model job after the user confirmed the draft in the chat.

    The heavy lifting stays in the existing submission path
    (``routes.api_jobs._handle_job_submission``): role check, model state,
    credit debit, validation, output path and queueing are not duplicated here.
    """
    db = get_db()
    user = _user(db)
    payload = request.get_json(silent=True) or {}
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else payload
    try:
        project_id = int(draft.get("project_id") or payload.get("project_id"))
    except (TypeError, ValueError):
        return _error("project_id is required.", 422)
    ctx = ToolContext(db=db, user_id=user["id"], is_admin=bool(user.get("is_admin")))
    try:
        _require_project(ctx, project_id)
    except ToolError as exc:
        return _error(str(exc), 403)

    model = str(draft.get("model") or draft.get("model_id") or "").strip()
    if not model:
        return _error("model is required.", 422)

    # Auto-run is a per-chat permission. A token is issued only when that
    # permission is on; without a valid token the job still needs the user's
    # click in the confirmation modal.
    auto_token = str(payload.get("auto_token") or "")
    if auto_token:
        chat_id = payload.get("chat_id")
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return _error("chat_id is required for an automatic run.", 422)
        chat = store.get_chat(db, user["id"], chat_id)
        if not chat or not chat.get("write_mode"):
            return _error("Auto-run is not enabled for this chat.", 403)
        if not jobtokens.verify(auto_token, user["id"], chat_id, draft):
            return _error("The auto-run permission expired or does not match this job.", 403)
    # Support BYOM custom model jobs (user-model-<id>) directly from Copilot
    if model.startswith("user-model-"):
        try:
            custom_db_id = int(model.split("user-model-", 1)[1])
        except ValueError:
            return _error("Invalid custom model identifier.", 400)
        import uuid as _uuid
        from ..helpers.custom_endpoint_runner import ensure_byom_schema, execute_custom_model_job_async
        ensure_byom_schema(db)
        mrow = db.execute("SELECT * FROM user_models WHERE id = %s", (custom_db_id,)).fetchone()
        if not mrow:
            return _error("Custom model not found.", 404)
        mdict = dict(mrow)
        job_id = str(_uuid.uuid4())
        jname = str(draft.get("name") or f"{mdict.get('model_name')} Run")[:120]
        inputs = draft.get("custom_params") if isinstance(draft.get("custom_params"), dict) else {}
        if draft.get("sequence") and "sequence" not in inputs:
            inputs["sequence"] = draft["sequence"]
        cur = db.execute(
            "INSERT INTO jobs (job_id, project_id, creator_id, name, model, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'queued', CURRENT_TIMESTAMP) RETURNING id",
            (job_id, project_id, user["id"], jname, model),
        )
        jf = cur.fetchone()
        job_db_id = int((jf["id"] if isinstance(jf, dict) or hasattr(jf, "keys") else jf[0]) if jf else cur.lastrowid)
        db.execute(
            "INSERT INTO external_job_queue (job_id, user_model_id, status, created_at) VALUES (%s, %s, 'pending', CURRENT_TIMESTAMP)",
            (job_db_id, custom_db_id),
        )
        db.commit()
        if mdict.get("worker_endpoint_url"):
            from ..app import app as _flask_app, socketio as _sio
            execute_custom_model_job_async(
                app=_flask_app,
                socketio=_sio,
                job_id=job_id,
                job_db_id=job_db_id,
                project_id=project_id,
                user_id=user["id"],
                model_row=mdict,
                input_data=inputs,
            )
        return _json({
            "status": "success",
            "job": {
                "job_id": job_id,
                "name": jname,
                "model": model,
                "status": "running" if mdict.get("worker_endpoint_url") else "queued",
                "cost": 0,
                "project_id": project_id,
            },
        }, 202)

    submit_payload = {
        "model": model,
        "name": str(draft.get("name") or "")[:120],
        "sequence": str(draft.get("sequence") or "")[:100000],
    }
    if draft.get("file_id") not in (None, ""):
        submit_payload["file_id"] = draft.get("file_id")
    if isinstance(draft.get("custom_params"), dict):
        submit_payload["custom_params"] = draft["custom_params"]

    try:
        from ..routes.api_jobs import _handle_job_submission

        response = _handle_job_submission(project_id, submit_payload, user_id=user["id"])
    except ImportError:
        return _error("Job submission is unavailable on this server.", 503)
    if isinstance(response, tuple):
        flask_response, status = response[0], response[1]
    else:
        flask_response, status = response, getattr(response, "status_code", 200)
    data = flask_response.get_json(silent=True) or {}
    if status >= 400 or data.get("status") == "error":
        return _error(data.get("message") or "The job could not be submitted.", status if status >= 400 else 400)
    job_id = data.get("job_id")
    if not job_id:
        return _error("The job was accepted but no job id was returned.", 502)
    return _json({
        "status": "success",
        "job": {
            "job_id": job_id,
            "name": data.get("name"),
            "model": data.get("model"),
            "status": data.get("status"),
            "cost": data.get("cost"),
            "project_id": data.get("project_id"),
        },
    }, 202)


@agent_bp.route("/api/agent/jobs/<job_id>", methods=["GET"])
@login_required
def agent_job_status(job_id: str):
    """Poll one job for the run card: status, artifacts and input file."""
    db = get_db()
    user = _user(db)
    ctx = ToolContext(db=db, user_id=user["id"], is_admin=bool(user.get("is_admin")))
    try:
        job = _require_job(ctx, job_id)
        project = _require_project(ctx, job.get("project_id"))
    except ToolError as exc:
        return _error(str(exc), 404)
    artifacts = _job_artifacts(project, job)
    input_file = None
    if job.get("input_file_id"):
        row = db.execute("SELECT id, name, size, project_id FROM files WHERE id = %s", (job["input_file_id"],)).fetchone()
        input_file = dict(row) if row else None
    return _json({
        "status": "success",
        "job": {
            "job_id": job.get("job_id"),
            "name": job.get("name"),
            "model": job.get("model"),
            "status": job.get("status"),
            "message": job.get("message"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "execution_time": job.get("execution_time"),
            "cost": job.get("cost"),
            "project_id": job.get("project_id"),
            "input_file_id": job.get("input_file_id"),
            "output_display_name": job.get("output_display_name"),
            "input_file": input_file,
        },
        "artifacts": [
            {"name": item["name"], "size": item["size"]}
            for item in artifacts[:100]
        ],
    })


@agent_bp.route("/api/agent/model-catalog", methods=["GET"])
@login_required
def model_catalog():
    """The bio-model catalog for the UI, with the payload kinds each model takes."""
    from ..helpers import model_io
    from .tools import _model_brief, _model_defs

    models = []
    for item in _model_defs():
        brief = _model_brief(item)
        brief["accepts"] = list(model_io.MODEL_INPUT_KINDS.get(item.get("id"), ()))
        brief["accepts_labels"] = [
            model_io.KIND_LABELS.get(kind, kind) for kind in brief["accepts"]
        ]
        brief["active"] = bool(item.get("is_active")) and not bool(item.get("retired"))
        models.append(brief)
    return _json({
        "status": "success",
        "models": models,
        "kinds": [
            {"id": kind, "label": model_io.KIND_LABELS.get(kind, kind)}
            for kind in model_io.ALL_KINDS
        ],
        "delivery": dict(model_io.KIND_DELIVERY),
    })


@agent_bp.route("/api/agent/routines", methods=["GET"])
@login_required
def discovery_routines():
    """Gimel-style multi-step Molecular Discovery Playbooks / Routines."""
    routines = [
        {
            "id": "target-triage",
            "title": "Target-to-Structure Rapid Triage",
            "category": "Structure",
            "prompt": "Inspect my current project, analyse the target sequence composition/MW/pI, check active structure models (Boltz-2, OpenFold3, and any custom BYOM endpoints), and propose the optimal structure prediction run.",
        },
        {
            "id": "plddt-loop-redesign",
            "title": "3D Confidence Audit & Loop Redesign",
            "category": "Design",
            "prompt": "Run structure_confidence_plddt on the live 3D viewer structure (or latest completed structure job), identify all low-confidence segments (<70 pLDDT), and propose a stabilizing mutation scan or ProteinMPNN redesign.",
        },
        {
            "id": "lead-admet-screen",
            "title": "Lead Optimization & Lipinski/QED Screen",
            "category": "Chemistry",
            "prompt": "Profile our candidate SMILES ligands with admet_lipinski_profile, rank them by QED and Lipinski/Veber compliance, and recommend the top lead for DiffDock molecular docking.",
        },
        {
            "id": "docking-workflow",
            "title": "Receptor + Ligand Blind Docking Plan",
            "category": "Docking",
            "prompt": "Check our receptor PDB/mmCIF structure for embedded co-factors/ligands, validate the candidate ligand SMILES, and draft a DiffDock docking job.",
        },
        {
            "id": "denovo-binder",
            "title": "De Novo Protein Binder Pipeline",
            "category": "Pipeline",
            "prompt": "Plan an end-to-end binder discovery pipeline: RFdiffusion backbone generation -> ProteinMPNN sequence design -> Boltz-2 complex validation, including input/output handoffs.",
        },
        {
            "id": "custom-endpoint-bench",
            "title": "Custom BYOM Endpoint Discovery",
            "category": "Models",
            "prompt": "Call list_custom_models to inspect our lab's registered custom endpoints, summarise their input schemas and protocols, and help me prepare a test run.",
        },
    ]
    return _json({"status": "success", "routines": routines})


@agent_bp.route("/api/agent/viewer-context", methods=["GET"])
@login_required
def read_viewer_context():
    user = _user(get_db())
    return _json({"status": "success", "viewer": viewer_context.summary(user["id"])})


@agent_bp.route("/api/agent/viewer-context", methods=["PUT"])
@login_required
@csrf_protect
def write_viewer_context():
    user = _user(get_db())
    payload = request.get_json(silent=True) or {}
    summary = viewer_context.put(user["id"], payload)
    return _json({"status": "success", "viewer": summary})


@agent_bp.route("/api/agent/viewer-context", methods=["DELETE"])
@login_required
@csrf_protect
def clear_viewer_context():
    user = _user(get_db())
    viewer_context.clear(user["id"])
    return _json({"status": "success", "viewer": {"attached": False}})
