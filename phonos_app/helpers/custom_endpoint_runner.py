"""Bring-Your-Own-Model (BYOM) & Custom Endpoint Execution Engine.

Supports 5 execution protocols for user-registered models in ``user_models``:
  1. ``direct_http``    — POST JSON directly to any REST/FastAPI/Flask model server
  2. ``nvidia_nim``     — POST to NVIDIA NIM / BioNeMo (cloud or local), including 202 async NVCF polling
  3. ``openai_compat``  — POST to OpenAI-compatible ``/chat/completions`` (Ollama, vLLM, BioMistral, OpenAI)
  4. ``huggingface``    — POST to Hugging Face Inference / Dedicated Endpoints
  5. ``webhook_bridge`` — Queue in ``external_job_queue`` for pull/callback workers

Outputs (PDB, mmCIF, SDF, MOL2, FASTA, SMILES, JSON) are auto-detected, persisted
into ``Config.JOB_OUTPUT_DIR``, recorded on ``jobs``, and broadcast live via Socket.IO.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from config import Config

from . import data_crypto
from .model_providers import resolve_nvidia_api_key, validate_endpoint_url

logger = logging.getLogger(__name__)

CUSTOM_KEY_LABEL = "phonos.custom_model_endpoint_key"
ALLOWED_ENDPOINT_MODES = ("direct_http", "nvidia_nim", "openai_compat", "huggingface", "webhook_bridge")
ALLOWED_AUTH_TYPES = ("none", "bearer", "x-api-key", "custom_header")
FORBIDDEN_CUSTOM_HEADERS = {"host", "cookie", "content-length", "transfer-encoding", "connection"}

_BYOM_SCHEMA_READY = False


def ensure_byom_schema(db) -> None:
    """Idempotently add BYOM endpoint execution columns to ``user_models``."""
    try:
        db.execute("SELECT endpoint_mode FROM user_models LIMIT 1")
        return
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    alter_stmts = (
        "ALTER TABLE user_models ADD COLUMN endpoint_mode TEXT DEFAULT 'direct_http'",
        "ALTER TABLE user_models ADD COLUMN auth_type TEXT DEFAULT 'none'",
        "ALTER TABLE user_models ADD COLUMN auth_header_name TEXT DEFAULT 'Authorization'",
        "ALTER TABLE user_models ADD COLUMN upstream_api_key_enc TEXT DEFAULT ''",
        "ALTER TABLE user_models ADD COLUMN custom_headers_json TEXT DEFAULT '{}'",
        "ALTER TABLE user_models ADD COLUMN request_template_json TEXT DEFAULT ''",
        "ALTER TABLE user_models ADD COLUMN response_output_path TEXT DEFAULT ''",
        "ALTER TABLE user_models ADD COLUMN upstream_model_name TEXT DEFAULT ''",
    )
    for sql in alter_stmts:
        try:
            db.execute(sql)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    _BYOM_SCHEMA_READY = True


def seal_endpoint_key(raw_key: Optional[str]) -> str:
    val = (raw_key or "").strip()
    return data_crypto.seal(CUSTOM_KEY_LABEL, val) if val else ""


def open_endpoint_key(enc_key: Optional[str]) -> str:
    if not enc_key:
        return ""
    try:
        return data_crypto.unseal(CUSTOM_KEY_LABEL, enc_key) or ""
    except Exception:
        return ""


def sanitize_custom_headers(raw_headers: Any) -> Dict[str, str]:
    if isinstance(raw_headers, str):
        raw_headers = raw_headers.strip()
        if not raw_headers:
            return {}
        try:
            raw_headers = json.loads(raw_headers)
        except Exception as exc:
            raise ValueError(f"custom_headers must be valid JSON object: {exc}") from exc
    if not isinstance(raw_headers, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for k, v in raw_headers.items():
        ks = str(k).strip()
        if not ks or ks.lower() in FORBIDDEN_CUSTOM_HEADERS or "\r" in ks or "\n" in ks:
            continue
        vs = str(v).strip()
        if "\r" in vs or "\n" in vs:
            continue
        cleaned[ks[:80]] = vs[:500]
    return cleaned


def _extract_dot_path(data: Any, path_expr: str) -> Any:
    if not path_expr or data is None:
        return data
    cur = data
    for part in path_expr.strip().split("."):
        if cur is None:
            return None
        if isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _detect_bio_payload(raw_value: Any, preferred_ext: str = "json") -> Tuple[str, str]:
    """Return ``(content_text, file_extension)`` from an endpoint response."""
    pref = (preferred_ext or "json").strip().lstrip(".").lower()

    if isinstance(raw_value, dict):
        # Auto-inspect well-known structure/sequence keys in response dicts
        for candidate_key, ext in (
            ("pdb", "pdb"),
            ("pdb_string", "pdb"),
            ("structure_pdb", "pdb"),
            ("cif", "cif"),
            ("mmcif", "cif"),
            ("structure_cif", "cif"),
            ("sdf", "sdf"),
            ("mol", "sdf"),
            ("fasta", "fasta"),
            ("sequence", "fasta"),
            ("dna", "fasta"),
        ):
            val = raw_value.get(candidate_key)
            if isinstance(val, str) and len(val.strip()) > 10:
                return val, ext
        # Boltz-2 / OpenFold style structures array
        structures = raw_value.get("structures")
        if isinstance(structures, list) and structures and isinstance(structures[0], dict):
            s0 = structures[0]
            if isinstance(s0.get("structure"), str):
                fmt = str(s0.get("format") or "cif").lower()
                return s0["structure"], ("pdb" if "pdb" in fmt else "cif")
        return json.dumps(raw_value, indent=2), "json"

    if isinstance(raw_value, str):
        stripped = raw_value.lstrip()
        if stripped.startswith(("HEADER", "ATOM  ", "HETATM", "MODEL ")):
            return raw_value, "pdb"
        if stripped.startswith("data_") or "_atom_site." in stripped[:1000]:
            return raw_value, "cif"
        if "V2000" in stripped[:500] or "V3000" in stripped[:500] or stripped.endswith("$$$$"):
            return raw_value, "sdf"
        if stripped.startswith(">"):
            return raw_value, "fasta"
        if pref in ("pdb", "cif", "mmcif", "sdf", "mol2", "fasta", "txt", "csv"):
            return raw_value, ("cif" if pref == "mmcif" else pref)
        try:
            parsed = json.loads(raw_value)
            return json.dumps(parsed, indent=2), "json"
        except Exception:
            return raw_value, (pref if pref != "json" else "txt")

    return json.dumps(raw_value, indent=2), "json"


def _render_request_template(template_str: str, input_data: Dict[str, Any], model_row: Dict[str, Any], job_id: str) -> Any:
    """Render ``{{field}}`` placeholders inside a JSON template safely."""
    if not template_str or not template_str.strip():
        return None
    rendered = template_str
    context = dict(input_data or {})
    context.setdefault("job_id", job_id)
    context.setdefault("model_name", model_row.get("upstream_model_name") or model_row.get("model_name") or "")

    def _repl(match: re.Match) -> str:
        key = match.group(1).strip()
        val = context.get(key, "")
        if isinstance(val, (dict, list, int, float, bool)):
            # If placeholder is unquoted in JSON, emit JSON literal; if inside quotes, escape inner string
            return json.dumps(val)[1:-1] if isinstance(val, str) else json.dumps(val)
        # Escape string safely for JSON string interpolation
        return json.dumps(str(val))[1:-1]

    rendered = re.sub(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}", _repl, rendered)
    return json.loads(rendered)


def build_upstream_request(
    model_row: Dict[str, Any],
    input_data: Dict[str, Any],
    *,
    job_id: str = "probe",
    user_id: Optional[int] = None,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Construct ``(url, headers, json_payload)`` for the configured ``endpoint_mode``."""
    raw_url = (model_row.get("worker_endpoint_url") or "").strip()
    url = validate_endpoint_url(raw_url, allow_private=True)
    mode = (model_row.get("endpoint_mode") or "direct_http").strip().lower()
    auth_type = (model_row.get("auth_type") or "none").strip().lower()
    auth_header = (model_row.get("auth_header_name") or "Authorization").strip() or "Authorization"

    api_key = open_endpoint_key(model_row.get("upstream_api_key_enc"))
    if not api_key and mode == "nvidia_nim":
        api_key = resolve_nvidia_api_key(user_id=user_id)

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Phonos-BYOM/1.0 ({mode})",
    }
    headers.update(sanitize_custom_headers(model_row.get("custom_headers_json") or "{}"))

    if api_key and auth_type != "none":
        if auth_type == "x-api-key":
            headers["X-API-Key"] = api_key
        elif auth_type == "custom_header" and auth_header.lower() not in FORBIDDEN_CUSTOM_HEADERS:
            headers[auth_header] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    elif api_key and mode in ("nvidia_nim", "openai_compat", "huggingface"):
        headers["Authorization"] = f"Bearer {api_key}"

    custom_tmpl = (model_row.get("request_template_json") or "").strip()
    if custom_tmpl:
        payload = _render_request_template(custom_tmpl, input_data, model_row, job_id)
    elif mode == "openai_compat":
        if not url.endswith("/chat/completions"):
            url = url.rstrip("/") + "/chat/completions"
        upstream_model = (model_row.get("upstream_model_name") or "").strip() or "default"
        prompt_text = input_data.get("prompt") or json.dumps(input_data, indent=2)
        payload = {
            "model": upstream_model,
            "messages": [
                {"role": "system", "content": f"You are executing scientific model '{model_row.get('model_name')}'. Return structured scientific output."},
                {"role": "user", "content": str(prompt_text)},
            ],
            "temperature": float(input_data.get("temperature", 0.2)),
        }
    elif mode == "huggingface":
        inputs_val = input_data.get("inputs") or input_data.get("sequence") or input_data.get("smiles") or input_data
        payload = {"inputs": inputs_val}
        if "parameters" in input_data and isinstance(input_data["parameters"], dict):
            payload["parameters"] = input_data["parameters"]
    elif mode == "nvidia_nim":
        payload = dict(input_data)
    else:
        # direct_http standard contract (also passes top-level input fields for simple FastAPI servers)
        payload = {
            "job_id": job_id,
            "model_id": model_row.get("model_id") or model_row.get("id"),
            "model_name": model_row.get("model_name"),
            "input_data": input_data,
            **input_data,
        }

    return url, headers, payload


def invoke_upstream_sync(
    model_row: Dict[str, Any],
    input_data: Dict[str, Any],
    *,
    job_id: str = "probe",
    user_id: Optional[int] = None,
    timeout_s: int = 180,
) -> Dict[str, Any]:
    """Invoke the custom endpoint synchronously (including NVCF 202 polling for ``nvidia_nim``)."""
    url, headers, payload = build_upstream_request(model_row, input_data, job_id=job_id, user_id=user_id)
    mode = (model_row.get("endpoint_mode") or "direct_http").strip().lower()
    t0 = time.monotonic()

    resp = requests.post(url, json=payload, headers=headers, timeout=(15, max(20, int(timeout_s))))

    # Handle NVIDIA NIM / NVCF 202 Accepted async polling automatically
    if mode == "nvidia_nim" and resp.status_code == 202:
        req_id = resp.headers.get("nvcf-reqid") or resp.headers.get("NVCF-REQID")
        status_base = os.environ.get("NVIDIA_STATUS_URL", "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status").rstrip("/")
        deadline = time.monotonic() + max(60, int(timeout_s))
        poll_headers = {k: v for k, v in headers.items() if k.lower() in ("authorization", "accept")}
        while req_id and time.monotonic() < deadline:
            time.sleep(3.0)
            poll_resp = requests.get(f"{status_base}/{req_id}", headers=poll_headers, timeout=30)
            if poll_resp.status_code == 202:
                continue
            resp = poll_resp
            break

    latency_ms = int((time.monotonic() - t0) * 1000)
    if not resp.ok:
        snippet = (resp.text or "")[:600]
        raise RuntimeError(f"Endpoint returned HTTP {resp.status_code}: {snippet}")

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "json" in ctype:
        body: Any = resp.json()
    else:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    out_path = (model_row.get("response_output_path") or "").strip()
    if not out_path and mode == "openai_compat" and isinstance(body, dict):
        out_path = "choices.0.message.content"
    extracted = _extract_dot_path(body, out_path) if out_path else body
    content_text, ext = _detect_bio_payload(extracted, preferred_ext=model_row.get("output_format") or "json")

    return {
        "ok": True,
        "status_code": resp.status_code,
        "latency_ms": latency_ms,
        "extension": ext,
        "content": content_text,
        "raw_preview": (content_text or "")[:1200],
    }


def probe_custom_endpoint(model_row: Dict[str, Any], sample_input: Optional[Dict[str, Any]] = None, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Test reachability & schema execution of a custom model endpoint."""
    raw_url = (model_row.get("worker_endpoint_url") or "").strip()
    if not raw_url:
        return {"ok": False, "error": "No worker_endpoint_url configured."}
    try:
        url = validate_endpoint_url(raw_url, allow_private=True)
    except ValueError as ve:
        return {"ok": False, "error": str(ve)}

    if sample_input:
        try:
            res = invoke_upstream_sync(model_row, sample_input, job_id="test-probe", user_id=user_id, timeout_s=45)
            return {
                "ok": True,
                "mode": model_row.get("endpoint_mode") or "direct_http",
                "status_code": res["status_code"],
                "latency_ms": res["latency_ms"],
                "detected_format": res["extension"],
                "preview": res["raw_preview"],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # Lightweight connectivity probe (HEAD/GET health check)
    t0 = time.monotonic()
    try:
        _, headers, _ = build_upstream_request(model_row, {}, job_id="health-probe", user_id=user_id)
        resp = requests.get(url, headers=headers, timeout=10)
        ms = int((time.monotonic() - t0) * 1000)
        # 200, 405 Method Not Allowed, or 422 Unprocessable Entity all prove the HTTP endpoint is live!
        reachable = resp.status_code in (200, 201, 204, 400, 405, 415, 422)
        return {
            "ok": reachable,
            "status_code": resp.status_code,
            "latency_ms": ms,
            "detail": f"Endpoint responded with HTTP {resp.status_code} ({ms} ms)." if reachable else f"HTTP {resp.status_code}: {(resp.text or '')[:200]}",
        }
    except Exception as exc:
        return {"ok": False, "error": f"Connection failed: {exc}"}


def execute_custom_model_job_async(
    *,
    app,
    socketio,
    job_id: str,
    job_db_id: int,
    project_id: int,
    user_id: int,
    model_row: Dict[str, Any],
    input_data: Dict[str, Any],
) -> None:
    """Spawn a real OS background thread to execute a direct custom model job and persist outputs."""
    try:
        import eventlet.patcher
        real_threading = eventlet.patcher.original("threading")
    except Exception:
        import threading as real_threading

    def _runner() -> None:
        from .database import get_db

        try:
            with app.app_context():
                db = get_db()
                db.execute("UPDATE jobs SET status = 'running', updated_at = CURRENT_TIMESTAMP WHERE job_id = %s", (job_id,))
                db.execute("UPDATE external_job_queue SET status = 'processing', updated_at = CURRENT_TIMESTAMP WHERE job_id = %s", (job_db_id,))
                db.commit()
        except Exception as exc:
            logger.warning("Could not mark custom job %s running: %s", job_id, exc)

        timeout_s = max(30, min(1800, int(model_row.get("estimated_runtime_seconds") or 180) * 3))
        try:
            res = invoke_upstream_sync(
                model_row,
                input_data,
                job_id=job_id,
                user_id=user_id,
                timeout_s=timeout_s,
            )
            ext = res["extension"]
            out_dir = Path(app.config.get("JOB_OUTPUT_DIR") or getattr(Config, "JOB_OUTPUT_DIR", "job_outputs"))
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{job_id}.{ext}"
            out_path = out_dir / filename
            out_path.write_text(res["content"], encoding="utf-8")
            fsize = out_path.stat().st_size
            exec_sec = round((res.get("latency_ms") or 0) / 1000.0, 3)
            msg_payload = json.dumps({"format": ext, "output_file": filename, "latency_ms": res.get("latency_ms") or 0})

            with app.app_context():
                db = get_db()
                db.execute(
                    """
                    UPDATE jobs
                    SET status = 'completed',
                        output_file = %s,
                        output_file_size = %s,
                        execution_time = %s,
                        message = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = %s
                    """,
                    (filename, fsize, exec_sec, msg_payload, job_id),
                )
                db.execute(
                    "UPDATE external_job_queue SET status = 'completed', completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE job_id = %s",
                    (job_db_id,),
                )
                db.execute(
                    "UPDATE user_models SET total_jobs_run = COALESCE(total_jobs_run, 0) + 1, last_used_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (model_row["id"],),
                )
                db.commit()

            if socketio is not None:
                try:
                    socketio.emit(
                        "job_update",
                        {
                            "job_id": job_id,
                            "status": "completed",
                            "output_file": filename,
                            "project_id": project_id,
                        },
                        room=f"user_{user_id}",
                    )
                except Exception:
                    pass
            logger.info("Custom endpoint job %s completed (%s, %d ms)", job_id, filename, res["latency_ms"])
        except Exception as exc:
            logger.error("Custom endpoint job %s failed: %s", job_id, exc)
            try:
                with app.app_context():
                    db = get_db()
                    db.execute(
                        "UPDATE jobs SET status = 'failed', message = %s, updated_at = CURRENT_TIMESTAMP WHERE job_id = %s",
                        (str(exc)[:1000], job_id),
                    )
                    db.execute(
                        "UPDATE external_job_queue SET status = 'failed', completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE job_id = %s",
                        (job_db_id,),
                    )
                    db.commit()
            except Exception:
                pass
            if socketio is not None:
                try:
                    socketio.emit(
                        "job_update",
                        {"job_id": job_id, "status": "failed", "error": str(exc)[:500], "project_id": project_id},
                        room=f"user_{user_id}",
                    )
                except Exception:
                    pass

    t = real_threading.Thread(target=_runner, daemon=True, name=f"byom-{job_id[:8]}")
    t.start()
