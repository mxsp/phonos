"""Orchestration for multi-stage model pipelines.

A pipeline chains models from different functional categories (e.g. Fold -> Dock,
DNAGen -> Fold -> Dock, Fold + MolGen -> Dock).  The user pays a single flat fee
when the pipeline is created; the internal jobs run for free.  When every job in a
stage reaches a terminal state, this module collects that stage's outputs,
matrix-expands them against the next stage, and submits the next stage's jobs.

The advance function is invoked from the Redis result listener
(`listeners.advance_pipeline_after_job`) inside an application context while
holding the per-job terminal lock.
"""
from __future__ import annotations

import json
import os
from typing import Any

from config import Config

from ..app import logger, socketio


# ---------------------------------------------------------------------------
# Small JSON helpers
# ---------------------------------------------------------------------------
def _as_dict(value: Any) -> dict:
    """Return a dict from a JSONB column that may arrive as dict or str."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _serialize_row(row) -> dict:
    out = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Output extraction per category
# ---------------------------------------------------------------------------
def _read_smiles_from_job(db, job_uuid: str) -> list[dict]:
    """Best-effort parse of a molecule-generation job's output into SMILES."""
    job = db.execute(
        "SELECT output_file, policy FROM jobs WHERE job_id = %s", (job_uuid,)
    ).fetchone()
    if not job:
        return []
    results: list[dict] = []
    path = job["output_file"]
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return []

    stripped = text.lstrip()
    if path.endswith(".json") or stripped[:1] in ("{", "["):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = None
        candidates: list[Any] = []
        if isinstance(data, dict):
            for key in ("molecules", "generated", "generated_molecules",
                        "samples", "smiles", "results"):
                if key in data and isinstance(data[key], list):
                    candidates = data[key]
                    break
        elif isinstance(data, list):
            candidates = data
        for item in candidates:
            smi, score = None, None
            if isinstance(item, str):
                smi = item
            elif isinstance(item, dict):
                smi = item.get("smiles") or item.get("smi") or item.get("SMILES")
                score = item.get("score") or item.get("qed") or item.get("value")
            if smi:
                results.append({"value": str(smi).strip(), "score": _safe_float(score)})
    else:
        # .smi / plain text: first whitespace token per line is the SMILES
        for line in text.splitlines():
            tok = line.strip().split()
            if tok and not tok[0].startswith("#"):
                results.append({"value": tok[0], "score": None})
    return results


def _read_sequences_from_job(db, job_uuid: str) -> list[dict]:
    """Best-effort parse of a DNA-generation job's output into sequences."""
    job = db.execute(
        "SELECT output_file FROM jobs WHERE job_id = %s", (job_uuid,)
    ).fetchone()
    if not job or not job["output_file"] or not os.path.exists(job["output_file"]):
        return []
    try:
        with open(job["output_file"], "r", encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
    except (OSError, TypeError, ValueError):
        return []
    seqs: list[Any] = []
    if isinstance(data, dict):
        for key in ("sequences", "generated_sequences", "output_sequences",
                    "samples", "outputs"):
            if key in data and isinstance(data[key], list):
                seqs = data[key]
                break
    elif isinstance(data, list):
        seqs = data
    results = []
    for item in seqs:
        if isinstance(item, str):
            results.append({"value": item.strip(), "score": None})
        elif isinstance(item, dict):
            val = item.get("sequence") or item.get("seq") or item.get("text")
            if val:
                results.append({"value": str(val).strip(),
                                "score": _safe_float(item.get("score"))})
    return results


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _collect_outputs(db, pipeline_pk: int, before_stage: int,
                     categories: tuple[str, ...]) -> list[dict]:
    """Collect usable outputs from completed jobs in upstream stages.

    Returns a list of items: {kind, value, score, pj_id, label}.  Structures
    reference the producing job UUID; ligands/sequences are parsed from disk.
    Results are sorted best-first by score and capped to PIPELINE_STAGE_TOPN.
    """
    rows = db.execute(
        """
        SELECT pj.id AS pj_id, pj.stage_index, pj.category, pj.combo_label,
               pj.rank_score, pj.job_id
        FROM pipeline_jobs pj
        JOIN jobs j ON pj.job_id = j.job_id
        WHERE pj.pipeline_pk = %s AND pj.stage_index < %s
          AND pj.category = ANY(%s) AND j.status = 'completed'
        ORDER BY pj.stage_index DESC
        """,
        (pipeline_pk, before_stage, list(categories)),
    ).fetchall()
    if not rows:
        return []

    # Only consider the most recent producing stage for this category group.
    latest_stage = rows[0]["stage_index"]
    rows = [r for r in rows if r["stage_index"] == latest_stage]

    items: list[dict] = []
    for r in rows:
        cat = r["category"]
        base_label = r["combo_label"] or f"s{r['stage_index']}"
        if cat in ("folding", "integrated", "protein_design"):
            # One structure per job, referenced by job UUID.
            items.append({
                "kind": "structure", "value": r["job_id"],
                "score": r["rank_score"], "pj_id": r["pj_id"], "label": base_label,
            })
        elif cat == "molecule_gen":
            for idx, mol in enumerate(_read_smiles_from_job(db, r["job_id"])):
                items.append({
                    "kind": "ligand", "value": mol["value"],
                    "score": mol["score"], "pj_id": r["pj_id"],
                    "label": f"{base_label}#mol{idx + 1}",
                })
        elif cat == "dna_gen":
            for idx, seq in enumerate(_read_sequences_from_job(db, r["job_id"])):
                items.append({
                    "kind": "sequence", "value": seq["value"],
                    "score": seq["score"], "pj_id": r["pj_id"],
                    "label": f"{base_label}#seq{idx + 1}",
                })

    items.sort(key=lambda it: (it["score"] is None, -(it["score"] or 0.0)))
    return items[: Config.PIPELINE_STAGE_TOPN]


# ---------------------------------------------------------------------------
# Internal job submission
# ---------------------------------------------------------------------------
def _submit_one(db, pipeline, stage_index: int, category: str, model: str,
                payload: dict, parent_pj_ids: list[int], label: str) -> bool:
    """Submit a single internal job for a stage and record its lineage."""
    from ..routes.api_jobs import _handle_job_submission

    name = f"{pipeline['name']}-s{stage_index}-{label}"
    payload = dict(payload)
    payload["name"] = name[:120]
    payload["model"] = model

    try:
        resp = _handle_job_submission(
            pipeline["project_id"], payload,
            user_id=pipeline["creator_id"],
            skip_credit=True, cost_override=0,
            pipeline_id=pipeline["pipeline_id"], pipeline_stage=stage_index,
            skip_role_check=True, skip_rate_limit=True,
        )
    except Exception:
        logger.error("[PIPELINE] internal job submission raised", exc_info=True)
        return False

    body, code = resp if isinstance(resp, tuple) else (resp, 200)
    data = body.get_json() if hasattr(body, "get_json") else {}
    new_job_id = (data or {}).get("job_id")
    if code != 202 or not new_job_id:
        logger.error("[PIPELINE] internal job rejected (code=%s): %s", code, data)
        return False

    db.execute(
        """
        INSERT INTO pipeline_jobs
            (pipeline_pk, stage_index, category, model, job_id, status,
             parent_pipeline_job_ids, combo_label)
        VALUES (%s, %s, %s, %s, %s, 'submitted', %s, %s)
        """,
        (pipeline["id"], stage_index, category, model, new_job_id,
         parent_pj_ids or [], label),
    )
    db.commit()
    return True


def _build_and_submit_stage(db, pipeline, stages: list[dict],
                            stage_index: int) -> int:
    """Build the job set for a stage (matrix-expanded) and submit it."""
    stage = stages[stage_index]
    category = stage.get("category")
    model = stage.get("model")
    cparams = dict(stage.get("custom_params") or {})
    seed = _as_dict(pipeline["seed_input"])

    structures = _collect_outputs(db, pipeline["id"], stage_index,
                                  ("folding", "integrated", "protein_design"))
    ligands = _collect_outputs(db, pipeline["id"], stage_index, ("molecule_gen",))
    sequences = _collect_outputs(db, pipeline["id"], stage_index, ("dna_gen",))

    jobs: list[tuple[dict, list[int], str]] = []

    if category == "folding":
        srcs = sequences if sequences else [None]
        for s in srcs:
            payload = {"custom_params": dict(cparams), "input_type": "sequence",
                       "sequence": s["value"] if s else seed.get("sequence", "")}
            jobs.append((payload, [s["pj_id"]] if s else [], s["label"] if s else "seed"))

    elif category == "docking":
        prots = structures if structures else [None]
        ligs = ligands if ligands else [None]
        for p in prots:
            for lg in ligs:
                cp = dict(cparams)
                if p:
                    cp["protein_input_type"] = "file"
                    cp["protein_file"] = p["value"]  # upstream fold job UUID
                if lg:
                    cp["ligand_input_type"] = "smiles"
                    cp["ligand"] = lg["value"]
                parents = [x["pj_id"] for x in (p, lg) if x]
                label = " x ".join(x["label"] for x in (p, lg) if x) or "seed"
                jobs.append(({"custom_params": cp, "input_type": "file"}, parents, label))

    elif category == "protein_design":
        srcs = structures if structures else [None]
        for p in srcs:
            cp = dict(cparams)
            if p:
                cp["input_pdb"] = p["value"]
            jobs.append(({"custom_params": cp, "input_type": "file"},
                         [p["pj_id"]] if p else [], p["label"] if p else "seed"))

    elif category in ("molecule_gen", "dna_gen", "integrated"):
        # Generators / single-shot stages run once, seeded from their own params.
        payload = {"custom_params": dict(cparams), "input_type": "sequence",
                   "sequence": seed.get("sequence", "") if stage_index == 0 else ""}
        jobs.append((payload, [], "gen"))

    else:
        logger.warning("[PIPELINE] unknown stage category '%s'", category)
        return 0

    # Enforce the total-jobs safety cap.
    cap = pipeline["safety_cap"] or Config.PIPELINE_MAX_TOTAL_JOBS
    used = pipeline["total_jobs"] or 0
    remaining = max(0, cap - used)
    if len(jobs) > remaining:
        jobs = jobs[:remaining]
        db.execute(
            "UPDATE job_pipelines SET metadata = "
            "COALESCE(metadata, '{}'::jsonb) || '{\"capped\": true}'::jsonb WHERE id = %s",
            (pipeline["id"],),
        )

    submitted = 0
    for payload, parents, label in jobs:
        if _submit_one(db, pipeline, stage_index, category, model,
                       payload, parents, label):
            submitted += 1
    return submitted


# ---------------------------------------------------------------------------
# Finalization + events
# ---------------------------------------------------------------------------
def _finalize_pipeline(db, pipeline_pk: int) -> None:
    """Finalize a pipeline that ran its full chain (status from job counts)."""
    row = db.execute(
        "SELECT completed_jobs, failed_jobs FROM job_pipelines WHERE id = %s",
        (pipeline_pk,),
    ).fetchone()
    completed = (row["completed_jobs"] if row else 0) or 0
    failed = (row["failed_jobs"] if row else 0) or 0
    if failed and completed:
        status = "partial"
    elif failed and not completed:
        status = "failed"
    else:
        status = "completed"
    db.execute(
        "UPDATE job_pipelines SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (status, pipeline_pk),
    )
    db.commit()


def _stall_pipeline(db, pipeline_pk: int, reason: str) -> None:
    """Mark a pipeline that could not continue mid-chain.

    This is the key fix for "shows finished at 1/3": a pipeline that stalls
    before the last stage must NOT be reported as completed.
    """
    row = db.execute(
        "SELECT completed_jobs FROM job_pipelines WHERE id = %s", (pipeline_pk,)
    ).fetchone()
    completed = (row["completed_jobs"] if row else 0) or 0
    status = "partial" if completed else "failed"
    db.execute(
        "UPDATE job_pipelines SET status = %s, "
        "metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('stall_reason', %s::text), "
        "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (status, reason, pipeline_pk),
    )
    db.commit()
    logger.warning("[PIPELINE] pipeline %s stalled (%s): %s", pipeline_pk, status, reason)


def emit_pipeline_update(db, pipeline_pk: int) -> None:
    p = db.execute("SELECT * FROM job_pipelines WHERE id = %s", (pipeline_pk,)).fetchone()
    if not p:
        return
    data = _serialize_row(p)
    recipients = {p["creator_id"]}
    if p["billing_user_id"]:
        recipients.add(p["billing_user_id"])
    try:
        for row in db.execute(
            "SELECT user_id FROM project_assignments WHERE project_id = %s",
            (p["project_id"],),
        ).fetchall():
            recipients.add(row["user_id"])
    except Exception:
        pass
    for uid in recipients:
        try:
            socketio.emit("pipeline_update", {"pipeline": data}, room=f"user_{uid}")
        except Exception:
            logger.debug("[PIPELINE] socket emit failed for user %s", uid)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def advance_pipeline_after_job(db, r, job_id: str, status: str) -> None:
    """Advance the pipeline a job belongs to, if any.

    Called for every terminal job; no-op when the job is not part of a pipeline.
    """
    job = db.execute(
        "SELECT pipeline_id, pipeline_stage FROM jobs WHERE job_id = %s", (job_id,)
    ).fetchone()
    if not job or not job["pipeline_id"]:
        return

    pipeline_id = job["pipeline_id"]
    stage_index = job["pipeline_stage"] if job["pipeline_stage"] is not None else 0
    pipeline = db.execute(
        "SELECT * FROM job_pipelines WHERE pipeline_id = %s", (pipeline_id,)
    ).fetchone()
    if not pipeline:
        return
    pipeline_pk = pipeline["id"]

    # Record this internal job's terminal state.
    pj_status = "completed" if status == "completed" else "failed"
    db.execute(
        "UPDATE pipeline_jobs SET status = %s WHERE pipeline_pk = %s AND job_id = %s",
        (pj_status, pipeline_pk, job_id),
    )
    if status == "completed":
        db.execute(
            "UPDATE job_pipelines SET completed_jobs = completed_jobs + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = %s", (pipeline_pk,),
        )
    else:
        db.execute(
            "UPDATE job_pipelines SET failed_jobs = failed_jobs + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = %s", (pipeline_pk,),
        )
    db.commit()

    # Stage barrier: wait until every job in this stage is terminal.
    pending = db.execute(
        "SELECT COUNT(*) AS c FROM pipeline_jobs WHERE pipeline_pk = %s AND "
        "stage_index = %s AND status NOT IN ('completed', 'failed', 'skipped', 'cancelled')",
        (pipeline_pk, stage_index),
    ).fetchone()["c"]
    if pending and pending > 0:
        return

    # Exactly-once: only one listener fans out the next stage.
    lock_key = f"pipeline_stage_advance:{pipeline_id}:{stage_index}"
    try:
        if not r.set(lock_key, "1", nx=True, ex=600):
            return
    except Exception:
        pass  # if Redis lock fails, fall through (single-listener deployments)

    definition = _as_dict(pipeline["definition"])
    stages = definition.get("stages", []) or []
    total_stages = len(stages)
    next_stage_index = stage_index + 1

    completed_in_stage = db.execute(
        "SELECT COUNT(*) AS c FROM pipeline_jobs WHERE pipeline_pk = %s AND "
        "stage_index = %s AND status = 'completed'",
        (pipeline_pk, stage_index),
    ).fetchone()["c"]

    # Reached the end of the chain -> finalize by job counts (the only path
    # that may mark the pipeline "completed").
    if next_stage_index >= total_stages:
        _finalize_pipeline(db, pipeline_pk)
        emit_pipeline_update(db, pipeline_pk)
        return

    # More stages remain but this one produced nothing usable -> stalled.
    if not completed_in_stage:
        _stall_pipeline(db, pipeline_pk,
                        f"Stage {stage_index + 1} produced no completed jobs.")
        emit_pipeline_update(db, pipeline_pk)
        return

    submitted = 0
    try:
        submitted = _build_and_submit_stage(db, pipeline, stages, next_stage_index)
    except Exception:
        logger.error("[PIPELINE] failed building stage %s of %s",
                     next_stage_index, pipeline_id, exc_info=True)

    if submitted == 0:
        # Couldn't start the next stage (e.g. previous outputs weren't usable as
        # inputs) — this is a stall, NOT a successful completion.
        _stall_pipeline(db, pipeline_pk,
                        f"Stage {next_stage_index + 1} could not be started from "
                        f"stage {stage_index + 1}'s outputs.")
    else:
        db.execute(
            "UPDATE job_pipelines SET current_stage = %s, status = 'running', "
            "total_jobs = total_jobs + %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (next_stage_index, submitted, pipeline_pk),
        )
        db.commit()
    emit_pipeline_update(db, pipeline_pk)
