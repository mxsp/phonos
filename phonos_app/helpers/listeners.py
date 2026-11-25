"""Background listeners for realtime updates."""
from __future__ import annotations

import base64
import json
import os
import shutil

import redis

from config import Config

from ..app import app, logger, redis_pool, socketio
from .database import get_db
from .notifications import record_job_notification
from .paths import (
    DEFAULT_JOB_BRANCH,
    INPUTS_JOB_BRANCH,
    get_job_branch_path_from_root,
    get_job_branch_relative_path,
    get_job_output_path,
    normalize_job_branch_name,
)
from .storage import format_storage_bytes, get_user_storage_state
from .analytics import track_event


def listen_for_job_updates():
    """Subscribe to Redis job status events and broadcast updates."""
    r = redis.Redis(connection_pool=redis_pool)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.psubscribe(f"{Config.REDIS_JOB_STATUS_CHANNEL}:*")
    pubsub.subscribe("pubsub:flow_update")
    logger.info("[REDIS-LISTENER] Subscribed to job status and flow update channels.")

    for message in pubsub.listen():
        try:
            channel = message['channel'].decode('utf-8') if isinstance(message['channel'], bytes) else message['channel']
            
            if channel == "pubsub:flow_update":
                try:
                    data = json.loads(message["data"])
                    project_id = data.get("project_id")
                    
                    if project_id:
                        # Broadcast to project room
                        socketio.emit('flow_update', data, room=f'project_{project_id}')
                        logger.debug(f"[SOCKETIO] Relayed flow update for project {project_id}")
                except Exception as e:
                    logger.error(f"[REDIS-LISTENER] Error processing flow update: {e}")
                continue

            # Existing job status logic
            data = json.loads(message["data"])
            job_id = data.get("job_id")
            status = data.get("status")

            if not all([job_id, status]):
                logger.warning("[REDIS-LISTENER] Invalid message received: %s", data)
                continue

            logger.info("[REDIS-LISTENER] Received status '%s' for job %s", status, job_id)

            with app.app_context():
                db = get_db()

                project_columns = {
                    row["column_name"]
                    for row in db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'").fetchall()
                }
                org_column = (
                    "organization_id"
                    if "organization_id" in project_columns
                    else "team_id"
                )

                job_info = db.execute(
                    f"SELECT j.creator_id, j.project_id, j.policy, j.model, j.status AS current_status, p.{org_column} AS organization_id "
                    "FROM jobs j JOIN projects p ON j.project_id = p.id "
                    "WHERE j.job_id = %s",
                    (job_id,),
                ).fetchone()
                if not job_info:
                    logger.warning(
                        "[REDIS-LISTENER] Job %s not found in database.", job_id
                    )
                    continue

                creator_id = job_info["creator_id"]

                # Guard against duplicate messages: skip if job already in terminal state
                if status in ("completed", "failed") and job_info["current_status"] in ("completed", "failed"):
                    logger.info(
                        "[REDIS-LISTENER] Job %s already in terminal state '%s', skipping duplicate '%s' message.",
                        job_id, job_info["current_status"], status,
                    )
                    continue

                # Distributed lock: prevent multiple listener processes from handling
                # the same terminal status (Redis pubsub fans out to all subscribers).
                if status in ("completed", "failed"):
                    lock_key = f"job_terminal_lock:{job_id}"
                    if not r.set(lock_key, "1", nx=True, ex=300):
                        logger.info(
                            "[REDIS-LISTENER] Job %s terminal status '%s' already claimed by another listener, skipping.",
                            job_id, status,
                        )
                        continue

                if status == "processing":
                    db.execute(
                        "UPDATE jobs SET status = %s, message = %s WHERE job_id = %s",
                        ("processing", data.get("message"), job_id),
                    )
                    db.execute(
                        "INSERT INTO job_logs (job_id, status, message) VALUES (%s, %s, %s)",
                        (job_id, "processing", data.get("message")),
                    )

                elif status == "completed":
                    result_data = data.get("data", {})
                    execution_time = result_data.get("execution_time", 0)

                    job_output_root = get_job_output_path(
                        db, job_info["project_id"], job_info["organization_id"], job_id
                    )

                    if not job_output_root:
                        db.execute(
                            "UPDATE jobs SET status = 'failed', message = 'Failed to create secure output directory.' WHERE job_id = %s",
                            (job_id,),
                        )
                        db.commit()
                        logger.error(
                            "Could not create job output directory for job %s.", job_id
                        )
                        continue

                    main_branch_dir = get_job_branch_path_from_root(
                        job_output_root, branch=DEFAULT_JOB_BRANCH, create=True
                    )

                    total_size = 0
                    main_output_path = None
                    final_artifact_metadata = []

                    def _relative_for_branch(branch_name: str, filename: str) -> str:
                        return os.path.join(
                            get_job_branch_relative_path(branch_name),
                            filename,
                        )

                    # Handle main output file
                    output_content_b64 = result_data.get("output_file_content")
                    output_filename = result_data.get("output_filename")
                    if output_content_b64 and not output_filename:
                        cached_output_path = result_data.get("output_path")
                        output_filename = (
                            os.path.basename(cached_output_path)
                            if cached_output_path
                            else f"{job_id}.out"
                        )
                        logger.warning(
                            "Job %s had output_file_content without output_filename; using fallback '%s'.",
                            job_id,
                            output_filename,
                        )
                    if output_content_b64 and output_filename:
                        try:
                            decoded_content = base64.b64decode(output_content_b64)
                            if "/" in output_filename or "\\" in output_filename or ".." in output_filename:
                                raise ValueError("Unsafe output filename")
                            main_output_path = os.path.join(main_branch_dir, output_filename)
                            with open(main_output_path, "wb") as f:
                                f.write(decoded_content)
                            total_size += len(decoded_content)
                        except (IOError, base64.binascii.Error) as e:
                            logger.error(
                                "Failed to write main output file for job %s: %s", job_id, e
                            )
                            # Job will be marked as failed later if this was the only output

                    # Handle artifacts
                    artifact_entries = result_data.get("artifacts", [])
                    for entry in artifact_entries:
                        content_b64 = entry.get("content_b64")
                        filename = entry.get("filename")
                        normalized_entry = dict(entry) if isinstance(entry, dict) else {}
                        branch_name = normalize_job_branch_name(
                            normalized_entry.get("branch") or DEFAULT_JOB_BRANCH
                        )
                        artifact_branch_dir = get_job_branch_path_from_root(
                            job_output_root, branch=branch_name, create=True
                        )

                        if content_b64 and filename:
                            # Reject filenames with path separators or traversal
                            if '/' in filename or '\\' in filename or '..' in filename:
                                logger.error(
                                    "Rejecting artifact with unsafe filename %r for job %s",
                                    filename, job_id,
                                )
                                continue
                            try:
                                decoded_content = base64.b64decode(content_b64)
                                artifact_path = os.path.join(artifact_branch_dir, filename)
                                with open(artifact_path, "wb") as f:
                                    f.write(decoded_content)
                                normalized_entry["size"] = len(decoded_content)
                                normalized_entry["branch"] = branch_name
                                normalized_entry["relative_path"] = _relative_for_branch(
                                    branch_name, filename
                                )
                                total_size += normalized_entry["size"]
                                final_artifact_metadata.append(normalized_entry)
                            except (IOError, base64.binascii.Error) as e:
                                logger.error(
                                    "Failed to write artifact %s for job %s: %s",
                                    filename,
                                    job_id,
                                    e,
                                )
                        else:
                            if filename:
                                normalized_entry["branch"] = branch_name
                                normalized_entry["relative_path"] = _relative_for_branch(
                                    branch_name, filename
                                )
                            final_artifact_metadata.append(normalized_entry)

                    # Fallback for cached/deduplicated jobs that arrived without
                    # file content but with an output_path pointing to an existing file.
                    if not main_output_path and not final_artifact_metadata and result_data.get("cached"):
                        src_path = result_data.get("output_path")
                        if src_path and os.path.isfile(src_path):
                            try:
                                dest_filename = os.path.basename(src_path)
                                dest_path = os.path.join(main_branch_dir, dest_filename)
                                shutil.copy2(src_path, dest_path)
                                main_output_path = dest_path
                                total_size += os.path.getsize(dest_path)
                                logger.info("Job %s: copied cached output from %s", job_id, src_path)
                            except OSError as e:
                                logger.error("Job %s: failed to copy cached output from %s: %s", job_id, src_path, e)
                        elif src_path and os.path.isdir(src_path):
                            try:
                                for fname in os.listdir(src_path):
                                    s = os.path.join(src_path, fname)
                                    d = os.path.join(main_branch_dir, fname)
                                    shutil.copy2(s, d)
                                    fsize = os.path.getsize(d)
                                    total_size += fsize
                                    if main_output_path is None:
                                        main_output_path = d
                                logger.info("Job %s: copied cached output dir from %s", job_id, src_path)
                            except OSError as e:
                                logger.error("Job %s: failed to copy cached output dir from %s: %s", job_id, src_path, e)

                    # Ensure we actually have some output
                    if not main_output_path and not final_artifact_metadata:
                        logger.error("Job %s completed but no output files could be saved.", job_id)
                        
                        # Debug logging for diagnosis
                        logger.error("Debug Job %s: output_content_b64 present? %s (len=%d)", 
                                    job_id, bool(output_content_b64), len(output_content_b64) if output_content_b64 else 0)
                        logger.error("Debug Job %s: output_filename: %s", job_id, output_filename)
                        logger.error("Debug Job %s: artifacts count: %d", job_id, len(artifact_entries))
                        logger.error("Debug Job %s: result keys: %s", job_id, list(result_data.keys()))
                        
                        db.execute(
                            "UPDATE jobs SET status = %s, message = %s, execution_time = %s WHERE job_id = %s",
                            ("failed", "Job finished but produced no valid output files.", execution_time, job_id),
                        )
                        db.execute(
                            "INSERT INTO job_logs (job_id, status, message) VALUES (%s, %s, %s)",
                            (job_id, "failed", "Job finished but produced no valid output files."),
                        )
                        db.commit()
                        
                        # Notify user of failure
                        try:
                            job_data = dict(db.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone())
                            # Convert datetime
                            from datetime import datetime as dt
                            for key, value in job_data.items():
                                if isinstance(value, dt):
                                    job_data[key] = value.isoformat()
                            
                            recipients = {creator_id}
                            # Add team members if applicable (simplified logic here as we just want to notify creator primarily)
                            
                            for recipient_id in recipients:
                                socketio.emit("job_update", {"job": job_data}, room=f"user_{recipient_id}")
                        except Exception as e:
                            logger.error(f"Failed to emit failure update for job {job_id}: {e}")
                            
                        continue

                    # Check storage quota
                    storage_state = get_user_storage_state(db, creator_id)
                    if storage_state["available"] < total_size:
                        if os.path.exists(job_output_root):
                            shutil.rmtree(job_output_root)
                        quota_text = format_storage_bytes(storage_state["quota"])
                        used_text = format_storage_bytes(storage_state["used"])
                        needed_text = format_storage_bytes(total_size)
                        message_text = (
                            f"Storage quota exceeded. Needed {needed_text}, "
                            f"but {used_text} is already used of {quota_text}. "
                            "Output file deleted."
                        )
                        db.execute(
                            "UPDATE jobs SET status = %s, message = %s, output_file = %s, output_file_size = %s, execution_time = %s WHERE job_id = %s",
                            ("failed", message_text, None, 0, execution_time, job_id),
                        )
                        db.execute(
                            "INSERT INTO job_logs (job_id, status, message) VALUES (%s, %s, %s)",
                            (
                                job_id,
                                "failed",
                                "Storage quota exceeded while saving output.",
                            ),
                        )
                    else:
                        job_metadata = {}
                        policy_raw = job_info["policy"]
                        if policy_raw:
                            try:
                                job_metadata = json.loads(policy_raw)
                            except (TypeError, json.JSONDecodeError):
                                job_metadata = {}

                        branch_layout = job_metadata.get("artifact_branches")
                        if not isinstance(branch_layout, dict):
                            branch_layout = {}
                        branch_layout[DEFAULT_JOB_BRANCH] = {
                            "relative_path": get_job_branch_relative_path(DEFAULT_JOB_BRANCH)
                        }
                        branch_layout[INPUTS_JOB_BRANCH] = {
                            "relative_path": get_job_branch_relative_path(INPUTS_JOB_BRANCH)
                        }
                        job_metadata["artifact_branches"] = branch_layout

                        if main_output_path:
                            output_filename = os.path.basename(main_output_path)
                            job_metadata["main_output"] = {
                                "filename": output_filename,
                                "branch": DEFAULT_JOB_BRANCH,
                                "relative_path": os.path.join(
                                    get_job_branch_relative_path(DEFAULT_JOB_BRANCH),
                                    output_filename,
                                ),
                            }

                        if final_artifact_metadata:
                            job_metadata["artifacts"] = [
                                {
                                    "name": item.get("name"),
                                    "filename": item.get("filename"),
                                    "display_name": item.get("display_name"),
                                    "branch": item.get("branch"),
                                    "relative_path": item.get("relative_path"),
                                }
                                for item in final_artifact_metadata
                            ]
                            job_metadata["artifacts_info"] = final_artifact_metadata
                        metrics = result_data.get("metrics")
                        if metrics:
                            job_metadata["result_summary"] = metrics

                        # Preserve all worker result metadata (scores, metrics, params, etc.)
                        WORKER_METADATA_KEYS = (
                            # Boltz2 fields
                            'confidence_scores', 'ptm_scores', 'iptm_scores',
                            'complex_plddt_scores', 'plddt_scores',
                            'plddt_avg', 'plddt_min', 'plddt_max',
                            'confidence_score', 'ptm_score', 'iptm_score', 'complex_plddt',
                            'num_models', 'num_ligand_sdf', 'num_protein_only',
                            'has_ligand', 'docking_style', 'ligand_input_type',
                            'ligand_smiles', 'ligand_ccd',
                            'diffusion_samples_requested', 'affinities',
                            'runtime_metrics',
                            'recycling_steps', 'sampling_steps', 'step_scale',
                            'predict_affinity', 'write_full_pae', 'without_potentials',
                            'concatenate_msas', 'has_pae_matrix', 'pae_matrix_size',
                            # OpenFold3 fields
                            'num_samples', 'input_id', 'molecule_type',
                            'is_multimer', 'output_format',
                            # DiffDock-RE fields
                            'pose_confidence', 'best_confidence', 'best_pose_index',
                            'num_poses', 'ligand_file_type', 'input_ligand_smiles',
                            # General fields
                            'structure_format', 'model_used',
                        )
                        for meta_key in WORKER_METADATA_KEYS:
                            val = result_data.get(meta_key)
                            if val is not None:
                                job_metadata[meta_key] = val

                        db.execute(
                            "UPDATE jobs SET status = %s, output_file = %s, output_file_size = %s, execution_time = %s, message = %s, policy = %s WHERE job_id = %s",
                            (
                                "completed",
                                main_output_path,
                                total_size,
                                execution_time,
                                None,
                                json.dumps(job_metadata),
                                job_id,
                            ),
                        )
                        db.execute(
                            "INSERT INTO job_logs (job_id, status) VALUES (%s, %s)",
                            (job_id, "completed"),
                        )
                        db.execute(
                            "UPDATE users SET storage_used = storage_used + %s WHERE id = %s",
                            (total_size, creator_id),
                        )

                        # Track job completion event
                        try:
                            job_row = db.execute("SELECT id, project_id, model, cost FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
                            if job_row:
                                # Use the actual cost from the job record for analytics display
                                job_cost = job_row.get('cost', 0) or 0
                                track_event(
                                    user_id=creator_id,
                                    event_type='job_completed',
                                    credits_used=job_cost,  # Show actual cost in analytics
                                    compute_time=execution_time,
                                    project_id=job_row['project_id'],
                                    job_id=job_row['id'],
                                    metadata={
                                        'model': job_row['model'],
                                        'output_size': total_size,
                                        'job_uuid': job_id
                                    }
                                )
                        except Exception as e:
                            logger.warning(f"Failed to track job completion event: {e}")

                elif status == "failed":
                    error_message = data.get(
                        "message", "Unknown error during job processing."
                    )
                    result_data = data.get("data", {})
                    execution_time = result_data.get("execution_time", 0)

                    # Get the cost of the failed job
                    job_cost_row = db.execute("SELECT cost FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
                    refund_amount = job_cost_row["cost"] if job_cost_row and job_cost_row["cost"] is not None else 1

                    db.execute(
                        "UPDATE jobs SET status = %s, message = %s, execution_time = %s WHERE job_id = %s",
                        ("failed", error_message, execution_time, job_id),
                    )
                    db.execute(
                        "INSERT INTO job_logs (job_id, status, message) VALUES (%s, %s, %s)",
                        (job_id, "failed", error_message),
                    )
                    # Refund the actual cost
                    db.execute(
                        "UPDATE users SET credits = credits + %s WHERE id = %s",
                        (refund_amount, creator_id),
                    )

                db.commit()

                job_row = db.execute(
                    "SELECT * FROM jobs WHERE job_id = %s", (job_id,)
                ).fetchone()
                if not job_row:
                    continue

                job_data = dict(job_row)
                # Convert datetime fields to ISO format strings for JSON serialization
                from datetime import datetime as dt
                for key, value in job_data.items():
                    if isinstance(value, dt):
                        job_data[key] = value.isoformat()

                is_pipeline_job = bool(job_data.get("pipeline_id"))

                if is_pipeline_job:
                    # Internal pipeline jobs are reported through the pipeline view,
                    # not the Jobs tab, so suppress their per-job notifications/emits.
                    recipients = set()
                elif status in {"completed", "failed"}:
                    notification_message = (
                        data.get("message")
                        if status == "failed"
                        else data.get("message")
                    )
                    recipients = record_job_notification(
                        db, job_data, status, notification_message
                    )
                else:
                    member_rows = db.execute(
                        "SELECT user_id FROM project_assignments WHERE project_id = %s",
                        (job_info["project_id"],),
                    ).fetchall()
                    recipients = {row["user_id"] for row in member_rows}
                    recipients.add(creator_id)

                for recipient_id in recipients:
                    socketio.emit(
                        "job_update", {"job": job_data}, room=f"user_{recipient_id}"
                    )
                    socketio.emit("user_info_update", room=f"user_{recipient_id}")
                    socketio.emit("notification_update", room=f"user_{recipient_id}")
                logger.info(
                    "[SOCKETIO] Emitted updates for job %s to %s recipients.",
                    job_id,
                    len(recipients),
                )

                # Advance any pipeline this job belongs to: when a whole stage is
                # done, matrix-expand its outputs and submit the next stage.
                if status in {"completed", "failed"}:
                    try:
                        from .pipelines import advance_pipeline_after_job
                        advance_pipeline_after_job(db, r, job_id, status)
                    except Exception:
                        logger.error(
                            "[PIPELINE] Failed to advance pipeline for job %s", job_id,
                            exc_info=True,
                        )

        except json.JSONDecodeError:
            logger.error(
                "[REDIS-LISTENER] Could not decode JSON from message: %s",
                message.get("data"),
            )
        except Exception:
            job_id_for_error = "unknown"
            try:
                msg_data = message.get("data", b"{}")
                job_id_for_error = json.loads(msg_data.decode("utf-8", "ignore")).get(
                    "job_id", "unknown"
                )
            except Exception:  # pragma: no cover - defensive logging
                pass
            logger.error(
                "[REDIS-LISTENER] Unexpected error processing message for job %s.",
                job_id_for_error,
                exc_info=True,
            )
