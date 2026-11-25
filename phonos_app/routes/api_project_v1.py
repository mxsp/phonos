"""
Project API v1 - project-scoped REST API (``/api/project/v1``).

A single project API key (``pj_`` prefix) authenticates the caller AND is
locked to one project: every endpoint here operates only on that project's
jobs and files. The same key is *dual use* - it also derives the AES-256 data
key (see :mod:`phonos_app.helpers.project_api_crypto`) used to encrypt the job
inputs/params and results we persist, so a database dump alone cannot read
them. Decryption requires the caller to present the key on each request.

Rate limit: 10 requests / minute / key.

Authentication:
    Authorization: Bearer pj_xxxxxxxx...

Endpoints (all relative to /api/project/v1):
    GET  /info                       - API + encryption info (no auth)
    GET  /project                    - the key's project details
    GET  /members                    - project members
    GET  /models                     - available models + their parameters
    POST /jobs                       - run a job in the project (input encrypted)
    GET  /jobs                        - list jobs in the project
    GET  /jobs/<job_id>              - job detail (decrypts submitted input)
    GET  /jobs/<job_id>/status       - job status only
    GET  /jobs/<job_id>/result       - decrypted result (encrypts at rest)
    GET  /jobs/<job_id>/output       - download the output artifact
    POST /jobs/<job_id>/cancel       - cancel a queued/running job
    DELETE /jobs/<job_id>            - delete a job
    GET  /files                       - list project files
    POST /files/upload               - upload a file to the project
    GET  /files/<file_id>            - file info
    GET  /files/<file_id>/download   - download a file
    DELETE /files/<file_id>          - delete a file
"""

import os
import json
import logging
from functools import wraps

from flask import jsonify, request, send_file
from werkzeug.utils import secure_filename

from ..app import app
from ..helpers.database import get_db
from ..helpers.paths import get_safe_project_path, is_path_within
from ..helpers.jobs import submit_job_helper
from ..helpers import project_api_crypto as crypto
from ..helpers.project_api_keys import (
    verify_project_api_key,
    check_rate_limit,
    log_usage,
    store_job_input,
    get_payload,
    set_enc_result,
    RATE_LIMIT_PER_MINUTE,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/project/v1"
API_VERSION = "1.0.0"

# Extensions whose content we inline (as text) in the result payload.
_TEXT_OUTPUT_EXTS = {".pdb", ".cif", ".sdf", ".mol2", ".fasta", ".fa", ".txt",
                     ".json", ".csv", ".seq", ".a3m"}
_MAX_INLINE_BYTES = 5 * 1024 * 1024  # 5 MB cap for inlined output content

# The files table stores no type column; we derive a category from the name.
_EXT_TYPE = {".pdb": "structure", ".cif": "structure", ".fasta": "sequence",
             ".fa": "sequence", ".sdf": "ligand", ".mol2": "ligand",
             ".csv": "data", ".json": "data",
             ".wl": "wolfram", ".wls": "wolfram", ".nb": "wolfram",
             ".m": "wolfram"}


def _file_type(name):
    return _EXT_TYPE.get(os.path.splitext(name or "")[1].lower(), "other")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _err(error, status, message=None):
    body = {"status": "error", "error": error}
    if message:
        body["message"] = message
    return jsonify(body), status


def _resolve_model(model_str):
    """Resolve a friendly name / alias / canonical id to a SUPPORTED_MODELS id."""
    from config import Config
    from ..helpers.models import canonical_model_id

    if not model_str:
        return None
    canonical = canonical_model_id(str(model_str).strip())
    if Config.get_model_definition(canonical):
        return canonical
    s = str(model_str).strip().lower()
    for m in Config.SUPPORTED_MODELS:
        if (m.get("id", "").lower() == s
                or (m.get("short_name") or "").lower() == s
                or (m.get("name") or "").lower() == s):
            return m["id"]
    return None


def _clean_sequence(sequence):
    """Strip FASTA headers/whitespace from a sequence string."""
    if not sequence:
        return sequence
    if ">" in sequence:
        sequence = "".join(
            line.strip() for line in sequence.splitlines()
            if not line.strip().startswith(">")
        )
    return "".join(sequence.split())


def require_project_api_key(f):
    """Authenticate a pj_ key, enforce the 10/min limit, and lock to its project."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _err("missing_authorization", 401,
                        "Provide your key as: Authorization: Bearer pj_...")
        raw_key = auth[len("Bearer "):].strip()
        if not raw_key:
            return _err("empty_api_key", 401, "API key cannot be empty")

        ctx = verify_project_api_key(raw_key)
        if not ctx:
            return _err("invalid_api_key", 401,
                        "Invalid, inactive, or non-project API key")

        if not check_rate_limit(ctx["key_id"], ctx["rate_limit"]):
            return _err("rate_limit_exceeded", 429,
                        f"Rate limit of {ctx['rate_limit']} requests/minute exceeded")

        # Count this accepted request toward the sliding window.
        log_usage(ctx["key_id"], request.path, 0)
        request.proj_ctx = ctx
        return f(*args, **kwargs)

    return wrapper


def _load_job(db, job_id, project_id):
    """Fetch a job by uuid, scoped to the key's project. None if not found."""
    return db.execute(
        """
        SELECT j.*, p.team_id
        FROM jobs j
        LEFT JOIN projects p ON p.id = j.project_id
        WHERE j.job_id = %s AND j.project_id = %s
        """,
        (job_id, project_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------
@app.route(f"{API_PREFIX}/info", methods=["GET"])
def project_api_info():
    """Public API description (no auth)."""
    return jsonify({
        "status": "success",
        "api": "Phonos Project API",
        "version": API_VERSION,
        "prefix": API_PREFIX,
        "authentication": "Bearer pj_<key> (project-scoped)",
        "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
        "encryption": {
            "scheme": "AES-256-GCM",
            "key_derivation": "HKDF-SHA256 from the API key (dual use)",
            "at_rest": "Job inputs/params and results are stored encrypted; "
                       "only the API key can decrypt them.",
        },
    })


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
@app.route(f"{API_PREFIX}/project", methods=["GET"])
@require_project_api_key
def project_api_project():
    """Return details of the project this key is scoped to."""
    ctx = request.proj_ctx
    db = get_db()
    project = db.execute(
        """
        SELECT p.id, p.name, p.description, p.is_private, p.is_archived,
               p.created_at, p.updated_at,
               (SELECT COUNT(*) FROM jobs WHERE project_id = p.id) AS job_count,
               (SELECT COUNT(*) FROM files WHERE project_id = p.id) AS file_count,
               (SELECT COUNT(*) FROM project_assignments WHERE project_id = p.id) AS member_count
        FROM projects p
        WHERE p.id = %s
        """,
        (ctx["project_id"],),
    ).fetchone()

    if not project:
        return _err("project_not_found", 404)

    return jsonify({
        "status": "success",
        "project": {
            "id": project["id"],
            "name": project["name"],
            "description": project["description"],
            "is_private": bool(project["is_private"]),
            "is_archived": bool(project["is_archived"]),
            "job_count": project["job_count"],
            "file_count": project["file_count"],
            "member_count": project["member_count"],
            "created_at": project["created_at"].isoformat() if hasattr(project["created_at"], "isoformat") else project["created_at"],
            "updated_at": project["updated_at"].isoformat() if hasattr(project["updated_at"], "isoformat") else project["updated_at"],
        },
    })


@app.route(f"{API_PREFIX}/members", methods=["GET"])
@require_project_api_key
def project_api_members():
    """List members of the key's project."""
    ctx = request.proj_ctx
    db = get_db()
    rows = db.execute(
        """
        SELECT u.id, u.username, uc.email, pa.role, pa.created_at AS joined_at
        FROM project_assignments pa
        JOIN users u ON pa.user_id = u.id
        LEFT JOIN user_credentials uc ON uc.user_id = u.id
        WHERE pa.project_id = %s
        ORDER BY pa.created_at
        """,
        (ctx["project_id"],),
    ).fetchall()

    members = [{
        "user_id": r["id"],
        "username": r["username"],
        "email": r["email"],
        "role": r["role"],
        "joined_at": r["joined_at"].isoformat() if hasattr(r["joined_at"], "isoformat") else r["joined_at"],
    } for r in rows]

    return jsonify({"status": "success", "project_id": ctx["project_id"], "members": members})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@app.route(f"{API_PREFIX}/models", methods=["GET"])
@require_project_api_key
def project_api_models():
    """List available models and the parameters (custom_fields) each accepts."""
    from config import Config
    models = []
    for m in Config.get_available_models():
        models.append({
            "id": m.get("id"),
            "name": m.get("name"),
            "short_name": m.get("short_name"),
            "description": m.get("description"),
            "task": m.get("task"),
            "input_modes": m.get("input_modes"),
            "output_extension": m.get("output_extension"),
            "credit_cost": m.get("credit_cost", m.get("cost", 20)),
            "parameters": m.get("custom_fields", []),
        })
    return jsonify({"status": "success", "models": models})


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.route(f"{API_PREFIX}/jobs", methods=["POST"])
@require_project_api_key
def project_api_submit_job():
    """
    Run a job inside the key's project.

    Body:
        {
            "model": "esmfold" | "type-0" | ...,   (required)
            "sequence": "MKFLIL...",                (required)
            "name": "My Job",                        (optional)
            "params": { ... model-specific ... }     (optional)
        }

    The input (sequence + params) is encrypted at rest with the key-derived
    data key before the job runs.
    """
    ctx = request.proj_ctx
    data = request.get_json(silent=True) or {}

    model_in = data.get("model")
    sequence = data.get("sequence")
    if not model_in:
        return _err("missing_model", 400, "Field 'model' is required")
    if not sequence:
        return _err("missing_sequence", 400, "Field 'sequence' is required")

    model_type = _resolve_model(model_in)
    if not model_type:
        return _err("invalid_model", 400, f"Unknown model '{model_in}'. See GET {API_PREFIX}/models")

    params = data.get("params") or {}
    if not isinstance(params, dict):
        return _err("invalid_params", 400, "'params' must be an object")

    name = data.get("name")
    clean_seq = _clean_sequence(sequence)

    # Encrypt the input BEFORE submitting (proves at-rest protection of input).
    enc_input = crypto.encrypt_json(ctx["data_key"], {
        "model": model_type,
        "sequence": clean_seq,
        "params": params,
    })

    # Submit through the same helper the UI/internal routes use (real pipeline,
    # credit accounting, MRS queue).
    result = submit_job_helper(
        user_id=ctx["user_id"],
        project_id=ctx["project_id"],
        model_type=model_type,
        sequence=clean_seq,
        name=name,
        custom_params=params,
    )

    if result.get("status") != "success":
        msg = result.get("message", "Job submission failed")
        status = 402 if "credit" in msg.lower() else 400
        return _err("submit_failed", status, msg)

    job_uuid = result["job_id"]

    # Persist the encrypted input keyed to the new job.
    try:
        db = get_db()
        row = db.execute("SELECT id FROM jobs WHERE job_id = %s", (job_uuid,)).fetchone()
        job_db_id = row["id"] if row else 0
        store_job_input(job_db_id, job_uuid, ctx["key_id"], ctx["project_id"], enc_input)
    except Exception as e:
        logger.warning("project_api: failed to store encrypted input for %s: %s", job_uuid, e)

    return jsonify({
        "status": "success",
        "job": {
            "job_id": job_uuid,
            "model": model_type,
            "name": name,
            "status": "queued",
            "project_id": ctx["project_id"],
            "credits_used": result.get("credits_used"),
            "input_encrypted": True,
        },
    }), 201


@app.route(f"{API_PREFIX}/jobs", methods=["GET"])
@require_project_api_key
def project_api_list_jobs():
    """List jobs in the key's project."""
    ctx = request.proj_ctx
    db = get_db()
    limit = min(request.args.get("limit", 50, type=int), 200)
    offset = request.args.get("offset", 0, type=int)
    status_filter = request.args.get("status")

    where = "project_id = %s"
    params = [ctx["project_id"]]
    if status_filter:
        where += " AND status = %s"
        params.append(status_filter)

    total = db.execute(f"SELECT COUNT(*) AS c FROM jobs WHERE {where}", tuple(params)).fetchone()["c"]
    params.extend([limit, offset])
    rows = db.execute(
        f"""
        SELECT job_id, name, model, status, execution_time, created_at, updated_at
        FROM jobs WHERE {where}
        ORDER BY created_at DESC LIMIT %s OFFSET %s
        """,
        tuple(params),
    ).fetchall()

    from ..helpers.models import get_supported_model_name
    jobs = [{
        "job_id": r["job_id"],
        "name": r["name"],
        "model": r["model"],
        "model_name": get_supported_model_name(r["model"], short=True),
        "status": r["status"],
        "execution_time": r["execution_time"],
        "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
        "updated_at": r["updated_at"].isoformat() if hasattr(r["updated_at"], "isoformat") else r["updated_at"],
    } for r in rows]

    return jsonify({
        "status": "success",
        "jobs": jobs,
        "pagination": {"total": total, "limit": limit, "offset": offset,
                       "has_more": (offset + limit) < total},
    })


@app.route(f"{API_PREFIX}/jobs/<job_id>", methods=["GET"])
@require_project_api_key
def project_api_get_job(job_id):
    """Job detail, including the decrypted input the caller submitted."""
    ctx = request.proj_ctx
    db = get_db()
    job = _load_job(db, job_id, ctx["project_id"])
    if not job:
        return _err("job_not_found", 404)

    from ..helpers.models import get_supported_model_name

    # Decrypt the stored input with the key-derived data key.
    decrypted_input = None
    payload = get_payload(job_id)
    if payload and payload.get("enc_input"):
        decrypted_input = crypto.decrypt_json(ctx["data_key"], payload["enc_input"])

    return jsonify({
        "status": "success",
        "job": {
            "job_id": job["job_id"],
            "name": job["name"],
            "model": job["model"],
            "model_name": get_supported_model_name(job["model"], short=True),
            "status": job["status"],
            "project_id": job["project_id"],
            "execution_time": job["execution_time"],
            "has_output": bool(job["output_file"]),
            "output_file_name": job["output_display_name"],
            "input": decrypted_input,  # {model, sequence, params} or None
            "created_at": job["created_at"].isoformat() if hasattr(job["created_at"], "isoformat") else job["created_at"],
            "updated_at": job["updated_at"].isoformat() if hasattr(job["updated_at"], "isoformat") else job["updated_at"],
        },
    })


@app.route(f"{API_PREFIX}/jobs/<job_id>/status", methods=["GET"])
@require_project_api_key
def project_api_job_status(job_id):
    """Lightweight status check."""
    ctx = request.proj_ctx
    db = get_db()
    job = db.execute(
        "SELECT job_id, status, execution_time FROM jobs WHERE job_id = %s AND project_id = %s",
        (job_id, ctx["project_id"]),
    ).fetchone()
    if not job:
        return _err("job_not_found", 404)
    return jsonify({
        "status": "success",
        "job_id": job["job_id"],
        "job_status": job["status"],
        "execution_time": job["execution_time"],
    })


@app.route(f"{API_PREFIX}/jobs/<job_id>/result", methods=["GET"])
@require_project_api_key
def project_api_job_result(job_id):
    """
    Return the decrypted result for a completed job.

    The first time a completed job's result is fetched, it is encrypted with
    the key-derived data key and cached (at-rest result encryption); subsequent
    reads decrypt from that cache.
    """
    ctx = request.proj_ctx
    db = get_db()
    job = _load_job(db, job_id, ctx["project_id"])
    if not job:
        return _err("job_not_found", 404)

    if job["status"] != "completed":
        return _err("job_not_complete", 400,
                    f"Job status is '{job['status']}'. Results are only available when completed.")

    payload = get_payload(job_id)

    # Serve from the encrypted cache if present.
    if payload and payload.get("enc_result"):
        cached = crypto.decrypt_json(ctx["data_key"], payload["enc_result"])
        if cached is not None:
            return jsonify({"status": "success", "job_id": job_id,
                            "result": cached, "encrypted_at_rest": True})

    # Otherwise build the result from the job row + output file.
    result_meta = None
    if job["message"]:
        try:
            result_meta = json.loads(job["message"])
        except (ValueError, TypeError):
            result_meta = {"message": job["message"]}

    output_content = None
    output_name = job["output_display_name"]
    out_path = job["output_file"]
    if out_path and os.path.exists(out_path):
        project_path = get_safe_project_path(job["project_id"], job["team_id"]) if job["team_id"] is not None else None
        if not project_path or not is_path_within(out_path, project_path):
            logger.warning("project_api: blocked output outside project root: job=%s", job_id)
            return _err("forbidden_path", 403, "Output file path is not allowed")
        ext = os.path.splitext(out_path)[1].lower()
        if ext in _TEXT_OUTPUT_EXTS and os.path.getsize(out_path) <= _MAX_INLINE_BYTES:
            try:
                with open(out_path, "r", errors="replace") as fh:
                    output_content = fh.read()
            except OSError as e:
                logger.warning("project_api: could not read output for %s: %s", job_id, e)

    result = {
        "metadata": result_meta,
        "output_file_name": output_name,
        "output_content": output_content,
    }

    # Encrypt + cache for at-rest protection (best effort).
    if payload:
        try:
            set_enc_result(job_id, crypto.encrypt_json(ctx["data_key"], result))
        except Exception as e:
            logger.warning("project_api: failed to cache encrypted result for %s: %s", job_id, e)

    return jsonify({"status": "success", "job_id": job_id,
                    "result": result, "encrypted_at_rest": bool(payload)})


@app.route(f"{API_PREFIX}/jobs/<job_id>/output", methods=["GET"])
@require_project_api_key
def project_api_job_output(job_id):
    """Download the raw output artifact for a completed job."""
    ctx = request.proj_ctx
    db = get_db()
    job = _load_job(db, job_id, ctx["project_id"])
    if not job:
        return _err("job_not_found", 404)
    out_path = job["output_file"]
    if not out_path or not os.path.exists(out_path):
        return _err("no_output", 404, "No output file available for this job")

    project_path = get_safe_project_path(job["project_id"], job["team_id"]) if job["team_id"] is not None else None
    if not project_path or not is_path_within(out_path, project_path):
        logger.warning("project_api: blocked output download outside project root: job=%s", job_id)
        return _err("forbidden_path", 403, "Output file path is not allowed")

    return send_file(out_path, as_attachment=True,
                     download_name=job["output_display_name"] or f"{job_id}.out")


@app.route(f"{API_PREFIX}/jobs/<job_id>/cancel", methods=["POST"])
@require_project_api_key
def project_api_cancel_job(job_id):
    """Cancel a queued or running job."""
    ctx = request.proj_ctx
    db = get_db()
    job = db.execute(
        "SELECT status FROM jobs WHERE job_id = %s AND project_id = %s",
        (job_id, ctx["project_id"]),
    ).fetchone()
    if not job:
        return _err("job_not_found", 404)
    if job["status"] in ("completed", "failed", "cancelled"):
        return _err("cannot_cancel", 400, f"Cannot cancel a job with status '{job['status']}'")
    db.execute(
        "UPDATE jobs SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
        "WHERE job_id = %s AND project_id = %s",
        (job_id, ctx["project_id"]),
    )
    db.commit()
    return jsonify({"status": "success", "message": "Job cancelled", "job_id": job_id})


@app.route(f"{API_PREFIX}/jobs/<job_id>", methods=["DELETE"])
@require_project_api_key
def project_api_delete_job(job_id):
    """Delete a job (and its encrypted payload record + output file)."""
    ctx = request.proj_ctx
    db = get_db()
    job = db.execute(
        "SELECT output_file FROM jobs WHERE job_id = %s AND project_id = %s",
        (job_id, ctx["project_id"]),
    ).fetchone()
    if not job:
        return _err("job_not_found", 404)

    if job["output_file"] and os.path.exists(job["output_file"]):
        try:
            os.remove(job["output_file"])
        except OSError as e:
            logger.warning("project_api: failed to delete output for %s: %s", job_id, e)

    db.execute("DELETE FROM jobs WHERE job_id = %s AND project_id = %s", (job_id, ctx["project_id"]))
    db.execute("DELETE FROM project_api_job_payloads WHERE job_uuid = %s", (job_id,))
    db.commit()
    return jsonify({"status": "success", "message": "Job deleted", "job_id": job_id})


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@app.route(f"{API_PREFIX}/files", methods=["GET"])
@require_project_api_key
def project_api_list_files():
    """List files in the key's project."""
    ctx = request.proj_ctx
    db = get_db()
    rows = db.execute(
        "SELECT id, name, size, created_at FROM files WHERE project_id = %s ORDER BY created_at DESC",
        (ctx["project_id"],),
    ).fetchall()
    files = [{
        "id": r["id"], "name": r["name"], "type": _file_type(r["name"]), "size": r["size"],
        "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
    } for r in rows]
    return jsonify({"status": "success", "project_id": ctx["project_id"], "files": files})


@app.route(f"{API_PREFIX}/files/upload", methods=["POST"])
@require_project_api_key
def project_api_upload_file():
    """Upload a file into the key's project."""
    ctx = request.proj_ctx
    if "file" not in request.files:
        return _err("no_file", 400, "No file provided (multipart field 'file')")
    upload = request.files["file"]
    if upload.filename == "":
        return _err("no_filename", 400, "Empty filename")

    from config import Config
    filename = secure_filename(upload.filename)
    custom_name = request.form.get("name", filename)

    upload_dir = os.path.join(Config.UPLOAD_FOLDER, str(ctx["user_id"]), str(ctx["project_id"]))
    os.makedirs(upload_dir, exist_ok=True)
    import uuid as _uuid
    unique = f"{_uuid.uuid4()}_{filename}"
    file_path = os.path.join(upload_dir, unique)
    upload.save(file_path)
    size = os.path.getsize(file_path)

    file_type = _file_type(filename)

    db = get_db()
    row = db.execute(
        """
        INSERT INTO files (project_id, creator_id, name, path, size)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (ctx["project_id"], ctx["user_id"], custom_name, file_path, size),
    ).fetchone()
    db.commit()

    return jsonify({
        "status": "success",
        "file": {
            "id": row["id"], "name": custom_name, "original_name": filename,
            "type": file_type, "size": size, "project_id": ctx["project_id"],
            "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else row["created_at"],
        },
    }), 201


@app.route(f"{API_PREFIX}/files/<int:file_id>", methods=["GET"])
@require_project_api_key
def project_api_get_file(file_id):
    """File metadata (scoped to the key's project)."""
    ctx = request.proj_ctx
    db = get_db()
    f = db.execute(
        "SELECT id, name, size, created_at FROM files WHERE id = %s AND project_id = %s",
        (file_id, ctx["project_id"]),
    ).fetchone()
    if not f:
        return _err("file_not_found", 404)
    return jsonify({
        "status": "success",
        "file": {
            "id": f["id"], "name": f["name"], "type": _file_type(f["name"]), "size": f["size"],
            "project_id": ctx["project_id"],
            "created_at": f["created_at"].isoformat() if hasattr(f["created_at"], "isoformat") else f["created_at"],
        },
    })


@app.route(f"{API_PREFIX}/files/<int:file_id>/download", methods=["GET"])
@require_project_api_key
def project_api_download_file(file_id):
    """Download a project file."""
    ctx = request.proj_ctx
    db = get_db()
    f = db.execute(
        """
        SELECT f.name, f.path, f.project_id, p.team_id
        FROM files f JOIN projects p ON p.id = f.project_id
        WHERE f.id = %s AND f.project_id = %s
        """,
        (file_id, ctx["project_id"]),
    ).fetchone()
    if not f:
        return _err("file_not_found", 404)
    if not f["path"] or not os.path.exists(f["path"]):
        return _err("file_missing", 404, "File no longer exists on disk")
    project_path = get_safe_project_path(f["project_id"], f["team_id"])
    if not project_path or not is_path_within(f["path"], project_path):
        logger.warning("project_api: blocked file download outside project root: file=%s", file_id)
        return _err("forbidden_path", 403, "File path is not allowed")
    return send_file(f["path"], as_attachment=True, download_name=f["name"])


@app.route(f"{API_PREFIX}/files/<int:file_id>", methods=["DELETE"])
@require_project_api_key
def project_api_delete_file(file_id):
    """Delete a project file."""
    ctx = request.proj_ctx
    db = get_db()
    f = db.execute(
        "SELECT id, path FROM files WHERE id = %s AND project_id = %s",
        (file_id, ctx["project_id"]),
    ).fetchone()
    if not f:
        return _err("file_not_found", 404)
    if f["path"] and os.path.exists(f["path"]):
        try:
            os.remove(f["path"])
        except OSError:
            pass
    db.execute("DELETE FROM files WHERE id = %s", (file_id,))
    db.commit()
    return jsonify({"status": "success", "message": "File deleted"})
