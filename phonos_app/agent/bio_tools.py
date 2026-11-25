"""Bio-model tools: choose, validate, chain and run the platform's AI models.

These tools sit on top of the same tables and helpers the web UI uses:
``Config.SUPPORTED_MODELS`` for the catalog, ``helpers.model_io`` for payload
classification and chaining, ``helpers.sequence_utils`` for sequence checks and
the submission endpoint in ``routes/api_jobs.py`` for the actual job.

A tool never submits a job on its own. ``propose_job`` returns a draft as a
``confirm_job`` client action; the chat UI shows the cost and the input, and
only the user's click calls ``POST /api/agent/jobs``.
"""

from __future__ import annotations

import os
from typing import Any

from production.config import Config

from ..helpers import model_io
from ..helpers import sequence_utils
from ..helpers.paths import resolve_file_path
from . import viewer_context
from .tools import (
    TEXT_EXTENSIONS,
    ToolContext,
    ToolError,
    ToolSpec,
    _file_ref,
    _int,
    _job_artifacts,
    _job_ref,
    _model_def,
    _ok,
    _project_id,
    _read_text,
    _require_file,
    _require_job,
    _require_project,
    _schema,
    _text,
)

MAX_TEXT = 24000
MAX_SEQUENCE = 100_000


# ---------------------------------------------------------------------------
# input helpers
# ---------------------------------------------------------------------------
def _input_text(ctx: ToolContext, args: dict) -> tuple[str, str]:
    """Return (content, filename) from ``text`` or a project file."""
    text = str(args.get("text") or "")
    filename = _text(args.get("filename"), 200)
    file_id = _file_ref(ctx, args, required=False)
    if file_id is not None:
        row = _require_file(ctx, file_id)
        path = resolve_file_path(row.get("path") or "")
        if not path:
            raise ToolError("The selected file is not available on disk.")
        content, _ = _read_text(path, MAX_TEXT)
        return content, row.get("name") or filename
    if not text.strip():
        raise ToolError("Pass text, or select a project file with file_id.")
    return text[:MAX_TEXT], filename


def _sequence_arg(args: dict) -> str:
    raw = str(args.get("sequence") or "").strip()
    if not raw:
        raise ToolError("Pass a sequence.")
    if len(raw) > MAX_SEQUENCE:
        raise ToolError("The sequence is longer than 100,000 characters.")
    if raw.startswith(">"):
        records = model_io.parse_fasta(raw)
        if not records:
            raise ToolError("The FASTA input contains no sequence.")
        raw = records[0].get("sequence") or ""
    raw = "".join(raw.split())
    return raw


def _sequence_kind(args: dict, model: dict | None = None) -> str:
    explicit = str(args.get("seq_type") or "").strip().lower()
    if explicit in ("dna", "rna", "protein"):
        return explicit
    task = str((model or {}).get("task") or "").lower()
    if "dna" in task:
        return "dna"
    return sequence_utils.detect_sequence_type(_sequence_arg(args))


def _validate_sequence(sequence: str, kind: str) -> tuple[bool, str]:
    ok, error = sequence_utils.validate_sequence(sequence, kind)
    return ok, error


def _structure_text(ctx: ToolContext, args: dict) -> tuple[str, str]:
    inline = str(args.get("structure") or args.get("pdb_text") or "")
    if inline.strip():
        return inline[:MAX_TEXT * 6], _text(args.get("filename"), 200)
    job_ref = _job_ref(ctx, args, required=False)
    if job_ref:
        job = _require_job(ctx, job_ref)
        project = _require_project(ctx, job.get("project_id"))
        artifacts = _job_artifacts(project, job)
        candidates = [
            item for item in artifacts
            if os.path.splitext(item["name"])[1].lower() in (".pdb", ".cif", ".mmcif", ".ent")
        ]
        if not candidates:
            raise ToolError("This job has no PDB/mmCIF output.")
        content, _ = _read_text(candidates[0]["path"], MAX_TEXT * 6)
        return content, candidates[0]["name"]
    file_id = _file_ref(ctx, args, required=False)
    if file_id is not None:
        row = _require_file(ctx, file_id)
        extension = os.path.splitext(row.get("name") or "")[1].lower()
        if extension not in (".pdb", ".cif", ".mmcif", ".ent"):
            raise ToolError("The selected file is not a PDB/mmCIF structure.")
        path = resolve_file_path(row.get("path") or "")
        if not path:
            raise ToolError("The selected file is not available on disk.")
        content, _ = _read_text(path, MAX_TEXT * 6)
        return content, row.get("name") or ""
    raise ToolError("Pass structure text, a structure file_id, or a job_id with a PDB output.")


# ---------------------------------------------------------------------------
# model catalog and readiness
# ---------------------------------------------------------------------------
def model_input_schema(ctx: ToolContext, args: dict) -> dict:
    model = _model_def(str(args.get("model_id") or ""))
    if not model:
        raise ToolError("Unknown model.")
    accepted = list(model_io.MODEL_INPUT_KINDS.get(model.get("id"), ()))
    fields = []
    for field in model.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        fields.append({
            "name": field.get("name"),
            "label": field.get("label"),
            "type": field.get("type"),
            "required": bool(field.get("required")),
            "default": field.get("default"),
            "options": [
                option.get("value") for option in (field.get("options") or [])
                if isinstance(option, dict)
            ][:30],
            "help": field.get("help"),
        })
    return _ok(
        f"Input schema for {model.get('short_name') or model.get('name')}.",
        model={
            "id": model.get("id"),
            "name": model.get("name"),
            "task": model.get("task"),
            "input_modes": model.get("input_modes"),
            "output_extension": model.get("output_extension"),
            "credit_cost": model.get("credit_cost"),
            "accepts": accepted,
            "accepts_labels": [model_io.KIND_LABELS.get(kind, kind) for kind in accepted],
            "active": bool(model.get("is_active")) and not bool(model.get("retired")),
            "offline_reason": model.get("offline_reason"),
        },
        fields=fields,
    )


def match_models_to_input(ctx: ToolContext, args: dict) -> dict:
    content, filename = _input_text(ctx, args)
    classified = model_io.classify_payload(filename or "", content)
    models = model_io.compatible_models(
        classified.get("kinds") or [],
        Config.SUPPORTED_MODELS,
        include_unavailable=True,
    )
    sequences = [
        {
            "name": record.get("name"),
            "kind": record.get("kind"),
            "length": len(record.get("sequence") or ""),
            "preview": (record.get("sequence") or "")[:60],
        }
        for record in (classified.get("sequences") or [])[:12]
    ]
    return _ok(
        "Input classified as " + ", ".join(classified.get("kinds") or ["unknown"]) + ".",
        filename=filename,
        kinds=classified.get("kinds") or [],
        needs_file=bool(classified.get("needs_file")),
        sequences=sequences,
        smiles=classified.get("smiles") or [],
        model_count=len(models),
        models=models,
    )


def model_compatibility_matrix(ctx: ToolContext, args: dict) -> dict:
    matrix = model_io.describe_matrix(Config.SUPPORTED_MODELS)
    return _ok("Model I/O compatibility matrix.", **matrix)


def check_model_ready(ctx: ToolContext, args: dict) -> dict:
    model = _model_def(str(args.get("model_id") or ""))
    if not model:
        raise ToolError("Unknown model.")
    from ..helpers.worker_runtime import apply_runtime_status_to_models

    enriched = apply_runtime_status_to_models([model])[0]
    ready = bool(enriched.get("is_active")) and not bool(enriched.get("retired")) and bool(enriched.get("available", True))
    if enriched.get("runtime_managed"):
        ready = ready and bool(enriched.get("runtime_available"))
    return _ok(
        f"{enriched.get('short_name') or enriched.get('name')} is "
        + ("ready." if ready else "not ready."),
        model={
            "id": enriched.get("id"),
            "name": enriched.get("name"),
            "short_name": enriched.get("short_name"),
            "task": enriched.get("task"),
            "ready": ready,
            "is_active": bool(enriched.get("is_active")),
            "available": bool(enriched.get("available", True)),
            "retired": bool(enriched.get("retired")),
            "runtime_status": enriched.get("runtime_status"),
            "worker_count": enriched.get("worker_count"),
            "active_jobs": enriched.get("active_jobs"),
            "queue_depth": enriched.get("queue_depth"),
            "nim_available": enriched.get("nim_available"),
            "offline_reason": enriched.get("offline_reason"),
            "replacement": enriched.get("replacement"),
        },
    )


def estimate_job_cost(ctx: ToolContext, args: dict) -> dict:
    model = _model_def(str(args.get("model_id") or ""))
    if not model:
        raise ToolError("Unknown model.")
    sequence = str(args.get("sequence") or "")
    custom = args.get("custom_params") if isinstance(args.get("custom_params"), dict) else {}
    cost = Config.calculate_adaptive_credit_cost(model.get("id"), sequence, custom)
    user = ctx.db.execute("SELECT credits FROM users WHERE id = %s", (ctx.user_id,)).fetchone()
    credits = int((dict(user) if user else {}).get("credits") or 0)
    return _ok(
        f"{cost} credits for one {model.get('short_name') or model.get('name')} job "
        f"({credits} available).",
        model={"id": model.get("id"), "name": model.get("name"),
               "credit_cost": model.get("credit_cost")},
        cost=cost,
        credits_available=credits,
        affordable=credits >= cost,
    )


# ---------------------------------------------------------------------------
# sequence and structure helpers
# ---------------------------------------------------------------------------
def sequence_analysis(ctx: ToolContext, args: dict) -> dict:
    sequence = _sequence_arg(args)
    kind = _sequence_kind(args)
    valid, error = _validate_sequence(sequence, kind)
    if not valid:
        return _ok(f"Invalid {kind} sequence: {error}", valid=False, error=error, kind=kind)
    stats = sequence_utils.get_sequence_statistics(sequence, kind)
    extra: dict[str, Any] = {}
    if kind in ("dna", "rna"):
        extra["orfs"] = sequence_utils.find_orfs(sequence, _int(args.get("min_orf"), default=100) or 100)[:10]
    else:
        extra["protein_properties"] = sequence_utils.calculate_protein_properties(sequence)
    return _ok(
        f"Valid {kind} sequence, {len(sequence)} residues.",
        valid=True,
        kind=kind,
        length=len(sequence),
        statistics=stats,
        **extra,
    )


def translate_sequence(ctx: ToolContext, args: dict) -> dict:
    sequence = _sequence_arg(args)
    kind = _sequence_kind(args)
    if kind not in ("dna", "rna"):
        raise ToolError("Translation expects a DNA or RNA sequence.")
    frame = max(1, min(_int(args.get("reading_frame"), default=1) or 1, 3))
    protein = sequence_utils.translate(sequence, frame, kind)
    return _ok(
        f"Translated {kind} frame {frame}: {len(protein)} amino acids.",
        kind=kind,
        reading_frame=frame,
        protein=protein[:MAX_TEXT],
        length=len(protein),
    )


def reverse_complement(ctx: ToolContext, args: dict) -> dict:
    sequence = _sequence_arg(args)
    kind = _sequence_kind(args)
    if kind not in ("dna", "rna"):
        raise ToolError("Reverse complement expects a DNA or RNA sequence.")
    result = sequence_utils.reverse_complement(sequence, kind)
    return _ok(f"Reverse complement of {len(sequence)} bases.", sequence=result, kind=kind)


def find_orfs(ctx: ToolContext, args: dict) -> dict:
    sequence = _sequence_arg(args)
    kind = _sequence_kind(args)
    if kind not in ("dna", "rna"):
        raise ToolError("ORF search expects a DNA or RNA sequence.")
    minimum = max(30, min(_int(args.get("min_length"), default=100) or 100, 10000))
    orfs = sequence_utils.find_orfs(sequence, minimum, bool(args.get("include_nested")))
    return _ok(f"{len(orfs)} ORF(s) at least {minimum} bp.", orfs=orfs[:50], count=len(orfs))


def structure_summary(ctx: ToolContext, args: dict) -> dict:
    content, filename = _structure_text(ctx, args)
    upper = content[:2000].upper()
    fmt = "cif" if ("DATA_" in upper or "_ATOM_SITE." in upper) else "pdb"
    chains = model_io.parse_structure_sequences(content, fmt)
    ligands = model_io.extract_structure_ligands(content)
    atom_lines = sum(1 for line in content.splitlines() if line.startswith(("ATOM", "HETATM")))
    residues = set()
    models = 0
    for line in content.splitlines():
        if line.startswith("MODEL"):
            models += 1
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 27:
            residues.add((line[21:22], line[22:27].strip()))
    resolution = None
    for line in content.splitlines()[:200]:
        if line.startswith("REMARK   2 RESOLUTION"):
            parts = line.split()
            for token in parts:
                try:
                    value = float(token)
                except ValueError:
                    continue
                if 0 < value < 20:
                    resolution = value
                    break
    return _ok(
        f"{fmt.upper()} structure: {len(chains)} chain(s), {len(residues)} residue(s), {len(ligands)} ligand(s).",
        format=fmt,
        filename=filename,
        chains=[
            {"id": chain.get("chain") or chain.get("id"), "length": len(chain.get("sequence") or ""),
             "sequence": (chain.get("sequence") or "")[:5000]}
            for chain in chains[:20]
        ],
        ligands=ligands[:50],
        atoms=atom_lines,
        residues=len(residues),
        models=models or 1,
        resolution=resolution,
    )


def extract_structure_sequence(ctx: ToolContext, args: dict) -> dict:
    summary = structure_summary(ctx, args)
    chains = summary.get("chains") or []
    if not chains:
        raise ToolError("No protein chain could be read from this structure.")
    return _ok(
        f"Extracted {len(chains)} chain sequence(s).",
        chains=chains,
        format=summary.get("format"),
    )


def structure_ligands(ctx: ToolContext, args: dict) -> dict:
    content, filename = _structure_text(ctx, args)
    ligands = model_io.extract_structure_ligands(content)
    return _ok(f"{len(ligands)} ligand residue(s).", ligands=ligands, filename=filename)


def validate_ligand(ctx: ToolContext, args: dict) -> dict:
    smiles = _text(args.get("smiles"), 2000)
    if not smiles:
        raise ToolError("Pass a SMILES string.")
    result: dict[str, Any] = {"smiles": smiles}
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return _ok("RDKit could not parse this SMILES.", valid=False, error="Invalid SMILES")
        result.update({
            "valid": True,
            "formula": rdMolDescriptors.CalcMolFormula(mol),
            "molecular_weight": round(float(Descriptors.MolWt(mol)), 2),
            "logp": round(float(Descriptors.MolLogP(mol)), 2),
            "h_donors": int(rdMolDescriptors.CalcNumHBD(mol)),
            "h_acceptors": int(rdMolDescriptors.CalcNumHBA(mol)),
            "rotatable_bonds": int(rdMolDescriptors.CalcNumRotatableBonds(mol)),
            "rings": int(rdMolDescriptors.CalcNumRings(mol)),
            "atoms": int(mol.GetNumAtoms()),
        })
        return _ok(f"Valid molecule, {result['formula']}, {result['molecular_weight']} Da.", **result)
    except ImportError:
        looks = model_io.looks_like_smiles(smiles, trusted=True)
        return _ok(
            "RDKit is not installed; only a basic SMILES check was possible.",
            valid=bool(looks),
            error=None if looks else "The string does not look like SMILES.",
            **result,
        )


# ---------------------------------------------------------------------------
# running a model
# ---------------------------------------------------------------------------
def _first_ligand_field(model: dict) -> str | None:
    for field in model.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "")
        if name in ("ligand", "ligand_smiles", "smiles", "ligand_smiles_string"):
            return name
    return None


def propose_job(ctx: ToolContext, args: dict) -> dict:
    model = _model_def(str(args.get("model_id") or ""))
    if not model:
        raise ToolError("Unknown model.")
    if model.get("retired") or model.get("is_active") is False or not model.get("available", True):
        raise ToolError(
            f"{model.get('short_name') or model.get('name')} is not active. "
            f"{model.get('offline_reason') or ''}".strip()
        )
    project = _require_project(ctx, _project_id(ctx, args))
    sequence = str(args.get("sequence") or "").strip()
    if sequence.startswith(">"):
        records = model_io.parse_fasta(sequence)
        if records:
            sequence = records[0].get("sequence") or ""
    sequence = "".join(sequence.split())[:MAX_SEQUENCE]
    file_id = _file_ref(ctx, args, required=False)
    custom = args.get("custom_params") if isinstance(args.get("custom_params"), dict) else {}
    custom = {str(key): value for key, value in list(custom.items())[:40]}

    modes = set(model.get("input_modes") or [])
    needs_sequence = "sequence" in modes
    needs_file = bool({"file", "pdb"} & modes)
    if needs_sequence and not sequence and file_id is None:
        raise ToolError("This model needs a sequence. Pass sequence, or a FASTA file_id.")
    if needs_file and file_id is None and not sequence:
        raise ToolError(
            "This model needs a structure/ligand file. Pass file_id from the project."
        )
    if sequence:
        kind = _sequence_kind(args, model)
        valid, error = _validate_sequence(sequence, kind)
        if not valid:
            raise ToolError(f"The {kind} sequence is invalid: {error}")
    if file_id is not None:
        row = _require_file(ctx, file_id)
        extension = os.path.splitext(row.get("name") or "")[1].lower()
        if extension and extension not in TEXT_EXTENSIONS | {".pdb", ".cif", ".mmcif"}:
            raise ToolError(f"{extension} is not a supported input format for this model.")

    ligand = _text(args.get("ligand_smiles"), 2000)
    if ligand:
        field = _first_ligand_field(model)
        if not field:
            raise ToolError("This model has no ligand field. Check its input schema.")
        custom[field] = ligand

    if not custom:
        defaults = {
            field.get("name"): field.get("default")
            for field in model.get("custom_fields") or []
            if isinstance(field, dict) and field.get("default") is not None
        }
        custom = defaults

    required = [
        field.get("name")
        for field in model.get("custom_fields") or []
        if isinstance(field, dict) and field.get("required")
        and field.get("name") not in ("sequence", "input_file", "file")
        and field.get("default") is None
    ]
    missing = [name for name in required if name and name not in custom]
    if missing:
        raise ToolError("The model needs these fields first: " + ", ".join(missing))

    name = _text(args.get("name"), 120) or f"{model.get('short_name') or model.get('name')} run"
    duplicates = []
    try:
        rows = ctx.db.execute(
            "SELECT job_id, name, status, sequence, created_at FROM jobs "
            "WHERE project_id = %s AND model = %s AND creator_id = %s "
            "ORDER BY created_at DESC LIMIT 20",
            (project["id"], model.get("id"), ctx.user_id),
        ).fetchall()
        for row in rows:
            item = dict(row)
            if str(item.get("status") or "").lower() in ("failed", "cancelled", "canceled"):
                continue
            if sequence and str(item.get("sequence") or "") == sequence:
                duplicates.append({
                    "job_id": item.get("job_id"),
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "created_at": item.get("created_at"),
                })
            if len(duplicates) >= 3:
                break
    except Exception:
        duplicates = []
    cost = Config.calculate_adaptive_credit_cost(model.get("id"), sequence, custom)
    user = ctx.db.execute("SELECT credits FROM users WHERE id = %s", (ctx.user_id,)).fetchone()
    credits = int((dict(user) if user else {}).get("credits") or 0)
    if credits < cost:
        raise ToolError(f"Not enough credits: the job costs {cost}, you have {credits}.")

    draft = {
        "model": model.get("id"),
        "project_id": project["id"],
        "name": name,
        "sequence": sequence,
        "custom_params": custom,
    }
    files = []
    if file_id is not None:
        draft["file_id"] = file_id
        row = _require_file(ctx, file_id)
        files.append({"id": row.get("id"), "name": row.get("name"), "size": row.get("size"), "role": "input"})
    input_summary = (
        f"{len(sequence)} residues" if sequence
        else f"file #{file_id}"
    )
    action = {
        "type": "confirm_job",
        "draft": draft,
        "model_name": model.get("short_name") or model.get("name"),
        "project_name": project["name"],
        "cost": cost,
        "credits": credits,
        "input_summary": input_summary,
        "files": files,
        "duplicates": duplicates,
    }
    if duplicates:
        action["warning"] = "A matching job already exists. Auto-run is paused; confirm manually if you really want another copy."
    if ctx.auto_run and ctx.chat_id and not duplicates:
        from . import jobtokens
        action["auto"] = True
        action["auto_token"] = jobtokens.issue(ctx.user_id, ctx.chat_id, draft)
    summary = (
        f"Ready to run {model.get('short_name') or model.get('name')} on {input_summary} "
        f"in \"{project['name']}\" for {cost} credits. "
        + ("Auto-run is enabled; the job will be submitted now." if action.get("auto") else "The user must confirm.")
        + (" A matching job already exists; auto-run is paused." if duplicates else "")
    )
    return _ok(
        summary,
        client_action=action,
        draft=draft,
        cost=cost,
        credits=credits,
        files=files,
        duplicates=duplicates,
    )


def propose_pipeline(ctx: ToolContext, args: dict) -> dict:
    goal = _text(args.get("goal"), 200).lower()
    if not goal:
        raise ToolError("Describe the goal, for example 'fold then dock' or 'design sequences'.")
    active = [
        model for model in Config.SUPPORTED_MODELS
        if model.get("is_active") and not model.get("retired") and model.get("available", True)
    ]
    by_task: dict[str, list[dict]] = {}
    for model in active:
        by_task.setdefault(str(model.get("task") or ""), []).append(model)

    stages: list[dict] = []
    if any(word in goal for word in ("dock", "docking", "ligand", "binding")):
        candidates = by_task.get("molecular_docking") or by_task.get("fold_and_dock") or []
        if "fold" in goal or "structure" in goal or not candidates:
            folding = (by_task.get("protein_folding") or by_task.get("structure_prediction")
                       or by_task.get("protein_complex_folding") or [])
            if folding:
                stages.append(folding[0])
        stages.extend(candidates[:1])
    elif any(word in goal for word in ("design", "backbone", "sequence design")):
        if "backbone" in goal:
            rfd = [m for m in by_task.get("protein_design", []) if "rfdiffusion" in str(m.get("id"))]
            stages.extend(rfd[:1])
        mpnn = [m for m in by_task.get("protein_design", []) if "proteinmpnn" in str(m.get("id"))]
        stages.extend(mpnn[:1] or by_task.get("protein_design", [])[:1])
    elif any(word in goal for word in ("dna", "gene", "genome")):
        stages.extend((by_task.get("dna_generation") or by_task.get("dna_sequence_generation") or [])[:1])
    elif any(word in goal for word in ("molecule", "smiles", "compound", "ligand generation")):
        stages.extend((by_task.get("molecule_generation") or [])[:2])
    else:
        stages.extend((by_task.get("protein_folding") or by_task.get("structure_prediction") or [])[:1])

    if not stages:
        raise ToolError("No active model matches that goal.")
    out = []
    for index, model in enumerate(stages, 1):
        out.append({
            "stage": index,
            "model_id": model.get("id"),
            "name": model.get("short_name") or model.get("name"),
            "task": model.get("task"),
            "accepts": list(model_io.MODEL_INPUT_KINDS.get(model.get("id"), ())),
            "output_extension": model.get("output_extension"),
            "credit_cost": model.get("credit_cost"),
        })
    return _ok(
        f"Suggested {len(out)}-stage pipeline. Submit each stage from the wizard; nothing is queued yet.",
        goal=goal,
        stages=out,
        note="Pipelines are user-confirmed in the UI. This tool only plans.",
    )


def summarize_job_result(ctx: ToolContext, args: dict) -> dict:
    job = _require_job(ctx, _job_ref(ctx, args))
    project = _require_project(ctx, job.get("project_id"))
    artifacts = _job_artifacts(project, job)
    summary: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "name": job.get("name"),
        "model": job.get("model"),
        "status": job.get("status"),
        "execution_time": job.get("execution_time"),
        "artifacts": [{"name": item["name"], "size": item["size"]} for item in artifacts],
    }
    for artifact in artifacts:
        extension = os.path.splitext(artifact["name"])[1].lower()
        if extension in (".pdb", ".cif", ".mmcif"):
            try:
                content, _ = _read_text(artifact["path"], MAX_TEXT * 6)
                chains = model_io.parse_structure_sequences(content, "cif" if extension != ".pdb" else "pdb")
                summary["structure"] = {
                    "file": artifact["name"],
                    "chains": [{"id": chain.get("chain") or chain.get("id"),
                                "length": len(chain.get("sequence") or "")} for chain in chains],
                    "ligands": model_io.extract_structure_ligands(content)[:20],
                }
            except Exception:
                pass
            break
        if extension in (".fasta", ".fa", ".faa"):
            try:
                content, _ = _read_text(artifact["path"], MAX_TEXT)
                records = model_io.parse_fasta(content)
                summary["sequences"] = [
                    {"name": record.get("name"), "length": len(record.get("sequence") or "")}
                    for record in records[:20]
                ]
            except Exception:
                pass
            break
    return _ok(f"Result summary for {job.get('job_id')}.", **summary)



# ---------------------------------------------------------------------------
# live 3D / sequence context
# ---------------------------------------------------------------------------
def _viewer_or_error(ctx: ToolContext) -> dict:
    item = viewer_context.get(ctx.user_id)
    if not item:
        raise ToolError(
            "No live 3D/sequence snapshot is attached. Open a structure in the viewer or the project "
            "visualizer, then ask again."
        )
    return item


def get_viewer_context(ctx: ToolContext, args: dict) -> dict:
    item = _viewer_or_error(ctx)
    summary = viewer_context.summary(ctx.user_id)
    return _ok(
        "Live viewer snapshot: "
        + (summary.get("name") or "structure")
        + (f" · {len(summary.get('selection') or [])} selected residue(s)" if summary.get("selection") else ""),
        viewer=summary,
        analysis=item.get("analysis") or {},
        sequence_chains=[
            {"chain": chain.get("chain"), "length": len(chain.get("sequence") or "")}
            for chain in item.get("chains") or []
        ],
    )


def read_viewer_structure(ctx: ToolContext, args: dict) -> dict:
    item = _viewer_or_error(ctx)
    text = item.get("structure_text") or ""
    if not text:
        raise ToolError("The viewer snapshot has no structure text.")
    max_chars = max(1000, min(_int(args.get("max_chars"), default=24000) or 24000, 200000))
    return _ok(
        f"Read the live structure ({len(text)} characters).",
        name=item.get("name") or "",
        format=item.get("format") or "pdb",
        content=text[:max_chars],
        truncated=len(text) > max_chars,
    )


def viewer_sequence(ctx: ToolContext, args: dict) -> dict:
    item = _viewer_or_error(ctx)
    chains = item.get("chains") or []
    if not chains:
        raise ToolError("The snapshot has no extracted sequence.")
    return _ok(
        f"{len(chains)} chain sequence(s) from the live viewer.",
        chains=[
            {"chain": chain.get("chain"), "length": len(chain.get("sequence") or ""),
             "sequence": chain.get("sequence") or ""}
            for chain in chains
        ],
    )


def viewer_selection(ctx: ToolContext, args: dict) -> dict:
    item = _viewer_or_error(ctx)
    selection = item.get("selection") or []
    if not selection and not item.get("ligand"):
        raise ToolError("Nothing is selected in the viewer. Select residues or a ligand first.")
    return _ok(
        f"{len(selection)} selected residue(s)"
        + (f", ligand {item.get('ligand')}" if item.get("ligand") else "") + ".",
        selection=selection,
        ligand=item.get("ligand") or "",
        name=item.get("name") or "",
    )


def find_in_viewer_structure(ctx: ToolContext, args: dict) -> dict:
    item = _viewer_or_error(ctx)
    motif = _text(args.get("motif") or args.get("query"), 120).upper()
    if not motif:
        raise ToolError("Pass a motif or sequence pattern.")
    hits = []
    for chain in item.get("chains") or []:
        sequence = (chain.get("sequence") or "").upper()
        start = 0
        while True:
            index = sequence.find(motif, start)
            if index < 0:
                break
            hits.append({"chain": chain.get("chain"), "start": index + 1, "end": index + len(motif)})
            start = index + 1
            if len(hits) >= 100:
                break
        if len(hits) >= 100:
            break
    return _ok(
        f"{len(hits)} match(es) for {motif} in the live structure.",
        motif=motif,
        hits=hits,
        chains=[{"chain": chain.get("chain"), "length": len(chain.get("sequence") or "")}
                for chain in item.get("chains") or []],
    )


# ---------------------------------------------------------------------------
# BYOM custom endpoints, medicinal chemistry ADMET & structure pLDDT tools
# ---------------------------------------------------------------------------
def list_custom_models(ctx: ToolContext, args: dict) -> dict:
    """List user-registered and shared Bring-Your-Own-Model (BYOM) custom endpoints."""
    from ..helpers.user_models import get_user_models

    project_id = _project_id(ctx, args, required=False)
    models = get_user_models(ctx.user_id, project_id=project_id, include_public=True)
    items = []
    for m in models[:50]:
        items.append({
            "id": m.get("id"),
            "agent_model_id": f"user-model-{m.get('id')}",
            "name": m.get("model_name"),
            "description": m.get("model_description"),
            "endpoint_mode": m.get("endpoint_mode") or "direct_http",
            "output_format": m.get("output_format") or "json",
            "input_schema": m.get("input_schema") or [],
            "active": bool(m.get("is_active")),
            "total_jobs_run": m.get("total_jobs_run") or 0,
        })
    return _ok(f"{len(items)} custom model endpoint(s) available.", custom_models=items)


def propose_custom_model_job(ctx: ToolContext, args: dict) -> dict:
    """Prepare a user-confirmed run against a custom BYOM endpoint."""
    from ..helpers.user_models import get_user_model
    from . import jobtokens

    custom_id = _int(args.get("custom_model_id"), default=0) or 0
    if custom_id < 1:
        raise ToolError("Pass a valid positive custom_model_id.")
    model = get_user_model(custom_id, user_id=ctx.user_id)
    if not model:
        raise ToolError(f"Custom model #{custom_id} not found or access denied.")
    project_id = _project_id(ctx, args, required=True)
    _require_project(ctx, project_id)
    inputs = args.get("inputs") if isinstance(args.get("inputs"), dict) else {}
    for field in (model.get("input_schema") or []):
        if isinstance(field, dict) and field.get("required") and field.get("name") not in inputs:
            if field.get("default") is not None:
                inputs[field["name"]] = field["default"]
            else:
                raise ToolError(f"Missing required custom model field: {field.get('name')}")
    name = _text(args.get("name"), 120) or f"{model.get('model_name')} Run"
    draft = {
        "model": f"user-model-{custom_id}",
        "model_name": model.get("model_name"),
        "project_id": project_id,
        "name": name,
        "sequence": str(inputs.get("sequence") or inputs.get("prompt") or ""),
        "custom_params": inputs,
        "cost": int(model.get("credit_cost") or 0),
    }
    auto_token = ""
    if getattr(ctx, "write_mode", False) and getattr(ctx, "chat_id", None):
        auto_token = jobtokens.issue(ctx.user_id, ctx.chat_id, draft)
    return _ok(
        f"Prepared custom endpoint run '{name}' on {model.get('model_name')}.",
        action={"type": "confirm_job", "draft": draft, "auto_token": auto_token},
        draft=draft,
    )


def admet_lipinski_profile(ctx: ToolContext, args: dict) -> dict:
    """Profile one or more SMILES molecules with RDKit (Lipinski, Veber, TPSA, QED, structural alerts)."""
    raw_list = args.get("smiles_list")
    if isinstance(raw_list, list):
        candidates = [str(s).strip() for s in raw_list if str(s).strip()]
    else:
        single = str(args.get("smiles") or "").strip()
        candidates = [s.strip() for s in single.splitlines() if s.strip()]
    if not candidates:
        raise ToolError("Provide 'smiles' or 'smiles_list' with at least one SMILES string.")

    try:
        from rdkit import Chem
        from rdkit.Chem import Crippen, Descriptors, Lipinski, QED, rdMolDescriptors
    except ImportError as exc:
        raise ToolError(f"RDKit is not available: {exc}") from exc

    results = []
    for smi in candidates[:50]:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            results.append({"smiles": smi, "valid": False, "error": "Invalid SMILES"})
            continue
        mw = round(Descriptors.MolWt(mol), 2)
        logp = round(Crippen.MolLogP(mol), 2)
        hbd = int(Lipinski.NumHDonors(mol))
        hba = int(Lipinski.NumHAcceptors(mol))
        tpsa = round(rdMolDescriptors.CalcTPSA(mol), 2)
        rot = int(Lipinski.NumRotatableBonds(mol))
        rings = int(rdMolDescriptors.CalcNumRings(mol))
        qed_val = round(float(QED.qed(mol)), 3)
        chiral = len(Chem.FindMolChiralCenters(mol, includeUnassigned=True))
        violations = sum([mw > 500, logp > 5, hbd > 5, hba > 10])
        veber_pass = bool(rot <= 10 and tpsa <= 140)
        results.append({
            "smiles": Chem.MolToSmiles(mol, canonical=True),
            "valid": True,
            "formula": rdMolDescriptors.CalcMolFormula(mol),
            "mw": mw,
            "clogp": logp,
            "hbd": hbd,
            "hba": hba,
            "tpsa": tpsa,
            "rotatable_bonds": rot,
            "rings": rings,
            "chiral_centers": chiral,
            "qed": qed_val,
            "lipinski_violations": violations,
            "lipinski_pass": violations <= 1,
            "veber_pass": veber_pass,
        })
    valid_cnt = sum(1 for r in results if r.get("valid"))
    return _ok(f"Profiled {valid_cnt}/{len(results)} molecule(s) for ADMET/drug-likeness.", molecules=results)


def structure_confidence_plddt(ctx: ToolContext, args: dict) -> dict:
    """Compute per-chain and regional pLDDT / B-factor confidence breakdown for a 3D structure."""
    content = ""
    source_name = ""
    if args.get("use_viewer"):
        item = viewer_context.get(ctx.user_id)
        if not item or not item.get("structure_text"):
            raise ToolError("No structure is currently attached in the 3D viewer.")
        content = str(item.get("structure_text") or "")
        source_name = str(item.get("name") or "viewer_structure.pdb")
    else:
        content, source_name = _structure_text(ctx, args)

    chain_scores: dict[str, list[tuple[int, str, float]]] = {}
    seen_res: set[tuple[str, int]] = set()

    for line in content.splitlines():
        if line.startswith("ATOM  ") and len(line) >= 66:
            atom_name = line[12:16].strip()
            if atom_name != "CA":
                continue
            chain_id = (line[21:22].strip() or "A")
            try:
                res_seq = int(line[22:26].strip())
                bfac = float(line[60:66].strip())
            except ValueError:
                continue
            res_name = line[17:20].strip()
            if (chain_id, res_seq) in seen_res:
                continue
            seen_res.add((chain_id, res_seq))
            # Scale 0..1 confidence to 0..100 if needed
            score = bfac * 100.0 if 0.0 < bfac <= 1.0 else bfac
            chain_scores.setdefault(chain_id, []).append((res_seq, res_name, score))

    # Minimal mmCIF _atom_site CA fallback when no PDB ATOM lines matched
    if not chain_scores and "_atom_site." in content:
        for line in content.splitlines():
            if not line.startswith("ATOM "):
                continue
            parts = line.split()
            if len(parts) >= 15 and parts[3] == "CA":
                try:
                    chain_id = parts[6]
                    res_name = parts[5]
                    res_seq = int(parts[8]) if parts[8].lstrip("-").isdigit() else len(seen_res) + 1
                    bfac = float(parts[14])
                    score = bfac * 100.0 if 0.0 < bfac <= 1.0 else bfac
                    if (chain_id, res_seq) not in seen_res:
                        seen_res.add((chain_id, res_seq))
                        chain_scores.setdefault(chain_id, []).append((res_seq, res_name, score))
                except (ValueError, IndexError):
                    continue

    if not chain_scores:
        raise ToolError("Could not extract C-alpha B-factor / pLDDT values from structure.")

    summary_chains = []
    all_vals: list[float] = []
    low_segments = []
    for cid, residues in sorted(chain_scores.items()):
        vals = [r[2] for r in residues]
        all_vals.extend(vals)
        mean_v = round(sum(vals) / len(vals), 2)
        bands = {
            "very_high_gt90": sum(1 for v in vals if v >= 90),
            "confident_70_90": sum(1 for v in vals if 70 <= v < 90),
            "low_50_70": sum(1 for v in vals if 50 <= v < 70),
            "disordered_lt50": sum(1 for v in vals if v < 50),
        }
        summary_chains.append({"chain": cid, "residues": len(vals), "mean_plddt": mean_v, "bands": bands})
        # Collapse contiguous <70 stretches for ProteinMPNN/RFdiffusion redesign hints
        seg_start = None
        seg_end = None
        for res_seq, _, sc in residues:
            if sc < 70.0:
                if seg_start is None:
                    seg_start = res_seq
                seg_end = res_seq
            elif seg_start is not None:
                if seg_end - seg_start >= 2:
                    low_segments.append({"chain": cid, "start": seg_start, "end": seg_end, "length": seg_end - seg_start + 1})
                seg_start = None
        if seg_start is not None and seg_end is not None and seg_end - seg_start >= 2:
            low_segments.append({"chain": cid, "start": seg_start, "end": seg_end, "length": seg_end - seg_start + 1})

    global_mean = round(sum(all_vals) / len(all_vals), 2)
    return _ok(
        f"Confidence analysis for {source_name}: global mean pLDDT = {global_mean} across {len(all_vals)} C-alpha residues.",
        source=source_name,
        global_mean_plddt=global_mean,
        total_residues=len(all_vals),
        chains=summary_chains,
        low_confidence_segments=low_segments[:30],
    )


def design_mutation_scan(ctx: ToolContext, args: dict) -> dict:
    """Generate a targeted point-mutation screening panel (alanine, stability, or solubility scan)."""
    seq = _sequence_arg(args).upper()
    strategy = str(args.get("strategy") or "alanine").strip().lower()
    start_raw = _int(args.get("start_pos"), default=1) or 1
    start_pos = max(1, min(start_raw, len(seq)))
    end_raw = _int(args.get("end_pos"), default=min(len(seq), start_pos + 19)) or min(len(seq), start_pos + 19)
    end_pos = max(start_pos, min(end_raw, len(seq)))

    palette = {
        "alanine": ["A"],
        "solubility": ["K", "R", "E", "D", "Q"],
        "stability": ["V", "I", "L", "P", "Y"],
        "disulfide": ["C"],
    }.get(strategy, ["A"])

    variants = []
    for pos in range(start_pos, min(end_pos + 1, start_pos + 40)):
        wt = seq[pos - 1]
        for mut in palette:
            if mut == wt:
                continue
            mut_seq = seq[:pos - 1] + mut + seq[pos:]
            label = f"{wt}{pos}{mut}"
            variants.append({"mutation": label, "position": pos, "wild_type": wt, "mutant": mut, "sequence": mut_seq})
            if len(variants) >= 60:
                break
        if len(variants) >= 60:
            break

    fasta = "\n".join(f">{v['mutation']}\n{v['sequence']}" for v in variants)
    return _ok(
        f"Generated {len(variants)} {strategy} variant(s) across residues {start_pos}-{end_pos}.",
        strategy=strategy,
        count=len(variants),
        variants=variants,
        fasta=fasta,
    )

# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------
_MODEL_ID = {"model_id": {"type": "string", "description": "Model id from list_models, e.g. openfold3 or diffdock-re."}}

BIO_SPECS = [
    ToolSpec(
        "model_input_schema",
        "Read exactly what a bio model needs: accepted payload kinds, input fields, formats, output and credit cost.",
        _schema(_MODEL_ID, ["model_id"]), model_input_schema, "Models",
    ),
    ToolSpec(
        "match_models_to_input",
        "Classify a sequence, structure, ligand or SMILES input and list every model that can consume it.",
        _schema({"text": {"type": "string"}, "filename": {"type": "string"},
                 "file_id": {"type": "integer"}}, []),
        match_models_to_input, "Models",
    ),
    ToolSpec(
        "model_compatibility_matrix",
        "Return the full model I/O matrix: which models accept which payload kinds and what they emit.",
        _schema(), model_compatibility_matrix, "Models",
    ),
    ToolSpec(
        "check_model_ready",
        "Check whether one model is active and has workers/queue capacity right now.",
        _schema(_MODEL_ID, ["model_id"]), check_model_ready, "Models",
    ),
    ToolSpec(
        "estimate_job_cost",
        "Estimate the credits for one model job and compare them with the user's balance.",
        _schema({**_MODEL_ID, "sequence": {"type": "string"},
                 "custom_params": {"type": "object"}}, ["model_id"]),
        estimate_job_cost, "Models",
    ),
    ToolSpec(
        "sequence_analysis",
        "Validate a DNA/RNA/protein sequence and return length, composition, GC, MW, pI and ORFs.",
        _schema({"sequence": {"type": "string"}, "seq_type": {"type": "string"},
                 "min_orf": {"type": "integer"}}, ["sequence"]),
        sequence_analysis, "Bio",
    ),
    ToolSpec(
        "translate_sequence",
        "Translate a DNA/RNA sequence in reading frame 1-3.",
        _schema({"sequence": {"type": "string"}, "seq_type": {"type": "string"},
                 "reading_frame": {"type": "integer"}}, ["sequence"]),
        translate_sequence, "Bio",
    ),
    ToolSpec(
        "reverse_complement",
        "Return the reverse complement of a DNA/RNA sequence.",
        _schema({"sequence": {"type": "string"}, "seq_type": {"type": "string"}}, ["sequence"]),
        reverse_complement, "Bio",
    ),
    ToolSpec(
        "find_orfs",
        "Find open reading frames in a DNA/RNA sequence.",
        _schema({"sequence": {"type": "string"}, "min_length": {"type": "integer"},
                 "include_nested": {"type": "boolean"}}, ["sequence"]),
        find_orfs, "Bio",
    ),
    ToolSpec(
        "structure_summary",
        "Summarise a PDB/mmCIF structure: chains, residue count, ligands, models and resolution.",
        _schema({"structure": {"type": "string"}, "filename": {"type": "string"},
                 "file_id": {"type": "integer"}, "job_id": {"type": "string"}}),
        structure_summary, "Bio",
    ),
    ToolSpec(
        "extract_structure_sequence",
        "Extract one-letter sequences per chain from a PDB/mmCIF structure.",
        _schema({"structure": {"type": "string"}, "file_id": {"type": "integer"},
                 "job_id": {"type": "string"}}),
        extract_structure_sequence, "Bio",
    ),
    ToolSpec(
        "structure_ligands",
        "List the non-solvent ligand residue names embedded in a structure.",
        _schema({"structure": {"type": "string"}, "file_id": {"type": "integer"},
                 "job_id": {"type": "string"}}),
        structure_ligands, "Bio",
    ),
    ToolSpec(
        "validate_ligand",
        "Validate a SMILES string with RDKit: formula, molecular weight, logP, donors, acceptors and rings.",
        _schema({"smiles": {"type": "string"}}, ["smiles"]), validate_ligand, "Bio",
    ),
    ToolSpec(
        "propose_job",
        "Prepare a real bio-model run and ask the user to confirm it. Nothing is queued until the user clicks Run.",
        _schema({**_MODEL_ID, "project_id": {"type": "integer"},
                 "sequence": {"type": "string"}, "file_id": {"type": "integer"},
                 "ligand_smiles": {"type": "string"}, "name": {"type": "string"},
                 "custom_params": {"type": "object"}}, ["model_id"]),
        propose_job, "Run", action=True,
    ),
    ToolSpec(
        "propose_pipeline",
        "Plan a multi-stage bio-model pipeline (fold, dock, design, generate) from the available models.",
        _schema({"goal": {"type": "string"}}, ["goal"]), propose_pipeline, "Run",
    ),
    ToolSpec(
        "summarize_job_result",
        "Summarise a finished job: artifacts, structure chains and ligands, or generated sequences.",
        _schema({"job_id": {"type": "string"}}), summarize_job_result, "Run",
    ),
    ToolSpec(
        "get_viewer_context",
        "Read the live 3D/sequence snapshot: structure name, chains, selection, ligand and analysis.",
        _schema(), get_viewer_context, "3D",
    ),
    ToolSpec(
        "read_viewer_structure",
        "Read the full PDB/mmCIF currently open in the 3D viewer (optionally truncated).",
        _schema({"max_chars": {"type": "integer"}}), read_viewer_structure, "3D",
    ),
    ToolSpec(
        "viewer_sequence",
        "Read the per-chain sequences extracted from the live 3D viewer.",
        _schema(), viewer_sequence, "3D",
    ),
    ToolSpec(
        "viewer_selection",
        "Read the residues and ligand the user selected in the 3D viewer or sequence panel.",
        _schema(), viewer_selection, "3D",
    ),
    ToolSpec(
        "find_in_viewer_structure",
        "Find a sequence motif in the live structure (1-based residue positions).",
        _schema({"motif": {"type": "string"}}, ["motif"]), find_in_viewer_structure, "3D",
    ),
    ToolSpec(
        "list_custom_models",
        "List user-registered and shared Bring-Your-Own-Model (BYOM) custom endpoints, their protocols and schemas.",
        _schema({"project_id": {"type": "integer"}}), list_custom_models, "Models",
    ),
    ToolSpec(
        "propose_custom_model_job",
        "Prepare a user-confirmed run against a custom BYOM endpoint (nothing runs until confirmed).",
        _schema({
            "custom_model_id": {"type": "integer"},
            "project_id": {"type": "integer"},
            "name": {"type": "string"},
            "inputs": {"type": "object"},
        }, ["custom_model_id", "project_id"]),
        propose_custom_model_job, "Run", action=True,
    ),
    ToolSpec(
        "admet_lipinski_profile",
        "Compute RDKit drug-likeness & ADMET descriptors (Lipinski, Veber, MW, cLogP, TPSA, QED, chiral centers) for SMILES.",
        _schema({
            "smiles": {"type": "string"},
            "smiles_list": {"type": "array", "items": {"type": "string"}},
        }),
        admet_lipinski_profile, "Bio",
    ),
    ToolSpec(
        "structure_confidence_plddt",
        "Analyse per-chain and regional pLDDT / B-factor confidence and flag low-confidence loops (<70) for redesign.",
        _schema({
            "structure": {"type": "string"},
            "file_id": {"type": "integer"},
            "job_id": {"type": "string"},
            "use_viewer": {"type": "boolean"},
        }),
        structure_confidence_plddt, "3D",
    ),
    ToolSpec(
        "design_mutation_scan",
        "Generate a targeted point-mutation variant library (alanine, solubility, stability, or disulfide scan) in FASTA.",
        _schema({
            "sequence": {"type": "string"},
            "strategy": {"type": "string", "description": "alanine | solubility | stability | disulfide"},
            "start_pos": {"type": "integer"},
            "end_pos": {"type": "integer"},
        }, ["sequence"]),
        design_mutation_scan, "Bio",
    ),

]
