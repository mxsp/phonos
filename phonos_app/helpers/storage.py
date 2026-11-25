"""Storage and plan management helpers."""
from __future__ import annotations

import os
import shutil
from datetime import datetime

from plan_service import (
    DEFAULT_PLAN,
    get_plan_definition,
    serialize_plan_catalog,
)

from ..app import app, logger


def format_storage_bytes(num_bytes: int) -> str:
    """Return a human friendly representation for a byte value."""
    if num_bytes is None:
        return "0 B"
    num_bytes = max(0, int(num_bytes))
    units = ["B", "KB", "MB", "GB", "TB"]
    if num_bytes == 0:
        return "0 B"
    power = min(len(units) - 1, int((num_bytes).bit_length() / 10))
    value = num_bytes / (1024**power)
    if value >= 1000 and power < len(units) - 1:
        power += 1
        value = num_bytes / (1024**power)
    return f"{value:.2f} {units[power]}"


def get_user_storage_state(db, user_id: int) -> dict:
    row = db.execute(
        "SELECT storage_used, storage_quota FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if not row:
        return {"used": 0, "quota": 0, "available": 0, "overage": 0}
    quota = max(0, int(row["storage_quota"] or 0))
    used = max(0, int(row["storage_used"] or 0))
    available = max(0, quota - used)
    overage = max(0, used - quota)
    return {
        "used": used,
        "quota": quota,
        "available": available,
        "overage": overage,
    }


def apply_plan_to_user(db, user_id: int, target_plan: str):
    plan_def = get_plan_definition(target_plan)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    db.execute(
        "UPDATE users SET plan = %s, credits = %s, storage_quota = %s, credits_last_reset = %s WHERE id = %s",
        (
            plan_def.name,
            plan_def.monthly_credits,
            plan_def.storage_quota_bytes,
            timestamp,
            user_id,
        ),
    )

    # Keep credential metadata in sync so UI plan gating reflects the change.
    credential_row = db.execute(
        "SELECT user_credential_id FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()

    credential_id = credential_row["user_credential_id"] if credential_row else None
    if credential_id:
        db.execute(
            "UPDATE user_credentials SET plan = %s WHERE id = %s",
            (plan_def.name, credential_id),
        )
    else:
        # Legacy databases may only have the reverse foreign key populated.
        db.execute(
            "UPDATE user_credentials SET plan = %s WHERE user_id = %s",
            (plan_def.name, user_id),
        )

    db.commit()

    storage_state = get_user_storage_state(db, user_id)
    warning = None
    if plan_def.name == DEFAULT_PLAN and storage_state["overage"] > 0:
        warning = (
            f"You are {format_storage_bytes(storage_state['overage'])} over the Standard storage limit. "
            "Delete job outputs or files to restore functionality."
        )

    return plan_def, storage_state, warning


def build_plan_catalog() -> list[dict]:
    catalog: list[dict] = []
    for entry in serialize_plan_catalog():
        enriched = dict(entry)
        enriched["storage_quota_human"] = format_storage_bytes(
            entry["storage_quota_bytes"]
        )
        catalog.append(enriched)
    return catalog


def delete_job_file_and_update_storage(
    db, user_id, job_id, output_file, output_file_size
):
    """Delete the physical output folder and update storage counters."""
    size_to_deduct = 0
    normalized_output_size = 0

    if output_file_size not in (None, ""):
        try:
            normalized_output_size = max(0, int(float(output_file_size)))
        except (TypeError, ValueError):
            logger.warning(
                "Invalid output file size %r for job %s. Defaulting to 0 bytes.",
                output_file_size,
                job_id,
            )

    if output_file and os.path.exists(output_file):
        try:
            from ..helpers.paths import get_all_job_output_roots, infer_job_root_from_output_path, is_path_within_any

            job_output_folder = infer_job_root_from_output_path(job_id, output_file)
            if not job_output_folder:
                job_output_folder = os.path.abspath(os.path.dirname(output_file))

            # Verify: folder basename must match the job_id
            if os.path.basename(job_output_folder) != job_id:
                logger.error(
                    "[SECURITY] Invalid path deletion attempt for job %s by user %s: "
                    "folder basename %r != job_id.",
                    job_id, user_id, os.path.basename(job_output_folder),
                )
                return

            # Verify: folder must be under one of the allowed output roots
            all_roots = get_all_job_output_roots()
            is_under_allowed_root = is_path_within_any(job_output_folder, all_roots)
            if not is_under_allowed_root:
                logger.error(
                    "[SECURITY] Invalid path deletion attempt for job %s by user %s: "
                    "path %r not under any allowed root.",
                    job_id, user_id, job_output_folder,
                )
                return

            shutil.rmtree(job_output_folder)
            size_to_deduct = normalized_output_size
            logger.info(
                "Deleted job output folder for job %s. Reclaiming %s bytes.",
                job_id,
                size_to_deduct,
            )
        except (IOError, OSError) as exc:
            logger.error(
                "[WEB-ERROR] Error deleting directory for job %s: %s", job_id, exc, exc_info=True
            )
            size_to_deduct = normalized_output_size

    elif normalized_output_size > 0:
        size_to_deduct = normalized_output_size
        logger.warning(
            "Output file for job %s not found, but reclaiming %s bytes from user storage.",
            job_id,
            size_to_deduct,
        )

    if size_to_deduct > 0:
        db.execute(
            "UPDATE users SET storage_used = GREATEST(0, storage_used - %s) WHERE id = %s",
            (size_to_deduct, user_id),
        )
