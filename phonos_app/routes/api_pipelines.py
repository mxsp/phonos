"""API routes for multi-stage model pipelines.

A pipeline chains models from different functional categories (Fold -> Dock,
DNAGen -> Fold -> Dock, Fold + MolGen -> Dock, ...).  It charges a single flat
fee (Config.PIPELINE_FLAT_CREDITS) up front; the internal jobs run for free and
are orchestrated by helpers/pipelines.py from the result listener.
"""
from __future__ import annotations

import json
import time
import uuid

from flask import jsonify, make_response, render_template, request, session

from config import Config
from model_overrides import apply_overrides_to_models, fetch_model_overrides

from ..app import app, logger
from ..helpers import (
    ensure_monthly_credit_reset,
    get_db,
    get_project_assignment,
    login_required,
    serialize_datetime,
)
from ..helpers.analytics import track_event
from ..helpers.models import get_supported_model_name
from ..helpers.security import csrf_protect
from ..helpers.worker_runtime import apply_runtime_status_to_models

PROJECT_ROLE_MANAGER = Config.PROJECT_ROLE_MANAGER
PROJECT_ROLE_CONTRIBUTOR = Config.PROJECT_ROLE_CONTRIBUTOR
SUBMIT_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _online_models(db) -> dict:
    """Return {model_id: model} for models that are currently online."""
    models = apply_overrides_to_models(Config.SUPPORTED_MODELS, fetch_model_overrides(db))
    models = apply_runtime_status_to_models(models)
    return {m.get("id"): m for m in models}


def _resolve_billing_user(db, project_id, user_id):
    project = db.execute(
        "SELECT p.id, p.collaboration_billing_model, t.creator_id "
        "FROM projects p LEFT JOIN teams t ON p.team_id = t.id WHERE p.id = %s",
        (project_id,),
    ).fetchone()
    if not project:
        return None, None
    model = project["collaboration_billing_model"] or "collaborator_credits"
    if model == "admin_credits" and project["creator_id"]:
        return project["creator_id"], project
    return user_id, project


# What each category needs upstream, and what it produces. Generators
# (molecule_gen / dna_gen) need nothing, so they can sit anywhere in a chain;
# this models "Fold + MolGen -> Dock" correctly (both feed docking).
_STAGE_REQUIRES = {
    "folding": {"sequence"},
    "docking": {"structure"},      # ligand is optional (seed param or upstream)
    "protein_design": {"structure"},
    "molecule_gen": set(),
    "dna_gen": set(),
    "integrated": set(),
    "analysis": {"sequence"},
}
_STAGE_PRODUCES = {
    "folding": {"structure"},
    "docking": {"poses"},
    "protein_design": {"structure", "sequence"},
    "molecule_gen": {"ligand"},
    "dna_gen": {"sequence"},
    "integrated": {"structure"},
    "analysis": set(),
}


def _seed_kinds(seed_input):
    """What input kinds the user's seed can satisfy."""
    t = (seed_input or {}).get("type")
    if t == "smiles":
        return {"ligand"}
    if t in ("file", "pdb", "pdb_content"):
        return {"structure", "sequence"}
    # default: a pasted/selected sequence
    return {"sequence"}


def _validate_stages(db, stages, seed_input=None):
    """Validate the chain: online models + each stage's inputs are satisfiable.

    Returns (normalized_stages, error_message). Categories are derived
    server-side from each model's task, not trusted from the client.
    """
    if not isinstance(stages, list) or not stages:
        return None, "A pipeline needs at least one stage."
    if len(stages) > Config.PIPELINE_MAX_STAGES:
        return None, f"A pipeline may have at most {Config.PIPELINE_MAX_STAGES} stages."

    online = _online_models(db)
    normalized = []
    available = set(_seed_kinds(seed_input))
    for idx, stage in enumerate(stages):
        if not isinstance(stage, dict):
            return None, f"Stage {idx + 1} is malformed."
        model_id = stage.get("model")
        model = online.get(model_id)
        if not model:
            return None, f"Stage {idx + 1}: unknown model '{model_id}'."
        if not (model.get("runtime_available") and model.get("is_visible", True)
                and model.get("available", True)):
            return None, f"Stage {idx + 1}: model '{model_id}' is offline."
        category = Config.pipeline_category_for_model(model_id)
        if not category:
            return None, f"Stage {idx + 1}: model '{model_id}' can't be used in a pipeline."

        requires = _STAGE_REQUIRES.get(category, set())
        missing = {req for req in requires if req not in available}
        # The seed always satisfies stage 0's needs.
        if missing and idx > 0:
            return None, (f"Stage {idx + 1} ({category}) needs "
                          f"{', '.join(sorted(missing))} that no earlier stage produces.")

        normalized.append({
            "index": idx,
            "category": category,
            "model": model_id,
            "custom_params": stage.get("custom_params") or {},
        })
        available |= _STAGE_PRODUCES.get(category, set())
    return normalized, None


def _estimate_jobs(stages):
    """Rough upper bound on internal jobs for the live builder display."""
    topn = Config.PIPELINE_STAGE_TOPN
    structures = 1
    ligands = 0
    sequences = 0
    total = 0
    for st in stages:
        cat = st["category"]
        if cat == "dna_gen":
            sequences = topn
            total += 1
        elif cat == "molecule_gen":
            ligands = topn
            total += 1
        elif cat == "folding":
            n = max(1, sequences) if sequences else 1
            structures = min(topn, n)
            total += structures
        elif cat == "docking":
            n = max(1, structures) * max(1, ligands or 1)
            total += min(Config.PIPELINE_MAX_TOTAL_JOBS, n)
        elif cat == "protein_design":
            total += max(1, structures)
        else:
            total += 1
    capped = total > Config.PIPELINE_MAX_TOTAL_JOBS
    return min(total, Config.PIPELINE_MAX_TOTAL_JOBS), capped


def _pipeline_dict(row):
    data = {k: serialize_datetime(v) for k, v in dict(row).items()}
    for key in ("definition", "seed_input", "metadata"):
        if isinstance(data.get(key), str):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, ValueError):
                data[key] = {}
    return data


def _can_access_pipeline(db, pipeline, user_id):
    if pipeline["creator_id"] == user_id or pipeline.get("billing_user_id") == user_id:
        return True
    return bool(get_project_assignment(db, user_id, pipeline["project_id"]))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------
def register_pipeline_routes(app):
    @app.route("/api/projects/<int:project_id>/pipelines/estimate", methods=["POST"])
    @login_required
    @csrf_protect
    def estimate_pipeline(project_id):
        db = get_db()
        body = request.get_json(silent=True) or {}
        stages, err = _validate_stages(db, body.get("stages", []), body.get("seed_input"))
        if err:
            return jsonify({"status": "error", "message": err}), 400
        est, capped = _estimate_jobs(stages)
        return jsonify({
            "status": "ok",
            "cost": Config.PIPELINE_FLAT_CREDITS,
            "estimated_max_jobs": est,
            "cap": Config.PIPELINE_MAX_TOTAL_JOBS,
            "capped": capped,
        })

    @app.route("/api/projects/<int:project_id>/pipelines", methods=["POST"])
    @login_required
    @csrf_protect
    def create_pipeline(project_id):
        db = get_db()
        user_id = session["user_id"]

        role = get_project_assignment(db, user_id, project_id)
        if not role or role not in SUBMIT_ROLES:
            return jsonify({"status": "error",
                            "message": "You can't submit pipelines to this project."}), 403

        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip() or f"Pipeline-{uuid.uuid4().hex[:8]}"
        seed_input = body.get("seed_input") or {}
        stages, err = _validate_stages(db, body.get("stages", []), seed_input)
        if err:
            return jsonify({"status": "error", "message": err}), 400

        billing_user_id, project = _resolve_billing_user(db, project_id, user_id)
        if not project:
            return jsonify({"status": "error", "message": "Project not found."}), 404

        ensure_monthly_credit_reset(db, billing_user_id)
        credits_row = db.execute(
            "SELECT credits FROM users WHERE id = %s", (billing_user_id,)
        ).fetchone()
        cost = Config.PIPELINE_FLAT_CREDITS
        if not credits_row or (credits_row["credits"] or 0) < cost:
            have = credits_row["credits"] if credits_row else 0
            return jsonify({
                "status": "error",
                "message": f"Insufficient credits. A pipeline costs {cost} credits, "
                           f"but you have {have}.",
            }), 402

        pipeline_id = f"pipeline_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        definition = {"stages": stages}

        # Charge the flat fee and create the pipeline atomically.
        db.execute("BEGIN")
        try:
            db.execute(
                """
                INSERT INTO job_pipelines
                    (pipeline_id, project_id, creator_id, name, status,
                     definition, seed_input, cost, billing_user_id,
                     total_stages, current_stage, safety_cap)
                VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s, %s, 0, %s)
                """,
                (pipeline_id, project_id, user_id, name,
                 json.dumps(definition), json.dumps(seed_input), cost,
                 billing_user_id, len(stages), Config.PIPELINE_MAX_TOTAL_JOBS),
            )
            db.execute("UPDATE users SET credits = credits - %s WHERE id = %s",
                       (cost, billing_user_id))
            db.commit()
        except Exception:
            db.rollback()
            logger.error("[PIPELINE] failed creating pipeline", exc_info=True)
            return jsonify({"status": "error",
                            "message": "Failed to create pipeline."}), 500

        pipeline = db.execute(
            "SELECT * FROM job_pipelines WHERE pipeline_id = %s", (pipeline_id,)
        ).fetchone()

        # Submit stage 0 (reuses the orchestrator's stage builder).
        from ..helpers.pipelines import _build_and_submit_stage, emit_pipeline_update
        try:
            submitted = _build_and_submit_stage(db, pipeline, stages, 0)
        except Exception:
            logger.error("[PIPELINE] stage-0 submission raised", exc_info=True)
            submitted = 0

        if submitted == 0:
            # Refund and mark failed: nothing actually launched.
            db.execute("UPDATE users SET credits = credits + %s WHERE id = %s",
                       (cost, billing_user_id))
            db.execute("UPDATE job_pipelines SET status = 'failed' WHERE id = %s",
                       (pipeline["id"],))
            db.commit()
            return jsonify({"status": "error",
                            "message": "Could not start the pipeline's first stage."}), 500

        db.execute(
            "UPDATE job_pipelines SET total_jobs = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s", (submitted, pipeline["id"]),
        )
        db.commit()

        try:
            track_event(user_id=user_id, event_type="pipeline_submitted",
                        credits_used=cost, project_id=project_id,
                        metadata={"pipeline_id": pipeline_id, "stages": len(stages)})
        except Exception:
            pass

        emit_pipeline_update(db, pipeline["id"])
        fresh = db.execute("SELECT * FROM job_pipelines WHERE id = %s",
                           (pipeline["id"],)).fetchone()
        return jsonify(_pipeline_dict(fresh)), 202

    @app.route("/api/pipelines/all", methods=["GET"])
    @login_required
    def list_all_pipelines():
        db = get_db()
        user_id = session["user_id"]
        rows = db.execute(
            """
            SELECT DISTINCT jp.*, p.name AS project_name, u.username AS creator_username
            FROM job_pipelines jp
            JOIN projects p ON jp.project_id = p.id
            LEFT JOIN users u ON u.id = jp.creator_id
            LEFT JOIN project_assignments pa ON pa.project_id = jp.project_id
            WHERE jp.creator_id = %s OR jp.billing_user_id = %s OR pa.user_id = %s
            ORDER BY jp.created_at DESC
            """,
            (user_id, user_id, user_id),
        ).fetchall()
        return jsonify([_pipeline_dict(r) for r in rows])

    @app.route("/api/projects/<int:project_id>/pipelines", methods=["GET"])
    @login_required
    def list_project_pipelines(project_id):
        db = get_db()
        user_id = session["user_id"]
        if not get_project_assignment(db, user_id, project_id):
            return jsonify({"status": "error", "message": "Access denied."}), 403
        rows = db.execute(
            "SELECT * FROM job_pipelines WHERE project_id = %s ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return jsonify([_pipeline_dict(r) for r in rows])

    @app.route("/api/pipelines/<pipeline_id>", methods=["GET"])
    @login_required
    def get_pipeline_detail(pipeline_id):
        db = get_db()
        user_id = session["user_id"]
        pipeline = db.execute(
            "SELECT * FROM job_pipelines WHERE pipeline_id = %s", (pipeline_id,)
        ).fetchone()
        if not pipeline:
            return jsonify({"status": "error", "message": "Pipeline not found."}), 404
        if not _can_access_pipeline(db, pipeline, user_id):
            return jsonify({"status": "error", "message": "Access denied."}), 403
        return jsonify(_build_detail(db, pipeline))

    @app.route("/api/pipelines/<pipeline_id>", methods=["DELETE"])
    @login_required
    @csrf_protect
    def delete_pipeline(pipeline_id):
        db = get_db()
        user_id = session["user_id"]
        pipeline = db.execute(
            "SELECT * FROM job_pipelines WHERE pipeline_id = %s", (pipeline_id,)
        ).fetchone()
        if not pipeline:
            return jsonify({"status": "error", "message": "Pipeline not found."}), 404
        if pipeline["creator_id"] != user_id and pipeline.get("billing_user_id") != user_id:
            return jsonify({"status": "error", "message": "Access denied."}), 403
        # Cancel any still-running internal jobs, then delete (cascade removes lineage).
        db.execute(
            "UPDATE jobs SET status = 'cancelled' WHERE pipeline_id = %s "
            "AND status IN ('submitted', 'queued', 'processing')",
            (pipeline_id,),
        )
        db.execute("DELETE FROM job_pipelines WHERE id = %s", (pipeline["id"],))
        db.commit()
        return jsonify({"status": "ok"})

    @app.route("/view_pipeline/<pipeline_id>")
    @login_required
    def view_pipeline(pipeline_id):
        db = get_db()
        user_id = session["user_id"]
        pipeline = db.execute(
            "SELECT * FROM job_pipelines WHERE pipeline_id = %s", (pipeline_id,)
        ).fetchone()
        if not pipeline:
            return "Pipeline not found.", 404
        if not _can_access_pipeline(db, pipeline, user_id):
            return "You do not have permission to view this pipeline.", 403
        project = db.execute("SELECT name FROM projects WHERE id = %s",
                             (pipeline["project_id"],)).fetchone()
        detail = _build_detail(db, pipeline)
        resp = make_response(render_template(
            "view_pipeline.html",
            pipeline=detail["pipeline"],
            stages=detail["stages"],
            project_name=project["name"] if project else "",
        ))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp


def _build_detail(db, pipeline):
    """Assemble pipeline + per-stage jobs (joined to job status/outputs)."""
    pdata = _pipeline_dict(pipeline)
    definition = pdata.get("definition") or {}
    stage_defs = definition.get("stages", []) or []

    rows = db.execute(
        """
        SELECT pj.id, pj.stage_index, pj.category, pj.model, pj.job_id, pj.status,
               pj.parent_pipeline_job_ids, pj.combo_label, pj.rank_score,
               j.status AS job_status, j.name AS job_name, j.output_file
        FROM pipeline_jobs pj
        LEFT JOIN jobs j ON pj.job_id = j.job_id
        WHERE pj.pipeline_pk = %s
        ORDER BY pj.stage_index ASC, pj.id ASC
        """,
        (pipeline["id"],),
    ).fetchall()

    by_stage: dict[int, list] = {}
    for r in rows:
        by_stage.setdefault(r["stage_index"], []).append({
            "pj_id": r["id"],
            "category": r["category"],
            "model": r["model"],
            "model_name": get_supported_model_name(r["model"], short=True),
            "job_id": r["job_id"],
            "status": r["job_status"] or r["status"],
            "job_name": r["job_name"],
            "combo_label": r["combo_label"],
            "rank_score": r["rank_score"],
            "parents": list(r["parent_pipeline_job_ids"] or []),
            "has_output": bool(r["output_file"]),
        })

    stages = []
    for sd in stage_defs:
        idx = sd.get("index")
        stages.append({
            "index": idx,
            "category": sd.get("category"),
            "model": sd.get("model"),
            "model_name": get_supported_model_name(sd.get("model"), short=True),
            "jobs": by_stage.get(idx, []),
        })
    return {"pipeline": pdata, "stages": stages}
