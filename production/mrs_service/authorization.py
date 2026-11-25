"""Authorization helpers for validating jobs and worker access."""
from __future__ import annotations

import sys
import os
from typing import Optional

# Add parent directory to path to import database module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db_connection

from .runtime import Config, QUEUE_NAME_PATTERN, logger

# Import subdomain verification (best-effort)
try:
    from phonos_app.helpers.subdomain_verify import (
        SUBDOMAIN_PATTERN,
        TRUSTED_DOMAIN,
    )
    SUBDOMAIN_AVAILABLE = True
except ImportError:
    SUBDOMAIN_AVAILABLE = False


def is_valid_worker_queue(name: str) -> bool:
    """Return ``True`` when ``name`` matches the worker queue naming convention."""
    return isinstance(name, str) and bool(QUEUE_NAME_PATTERN.match(name))


def is_external_worker_subdomain(worker_id: str) -> bool:
    """Return ``True`` when *worker_id* follows the external worker naming scheme.

    External workers use their subdomain label as worker_id.
    This checks if the worker_id matches the pattern of a DNS label
    and is not a known internal node.
    """
    if worker_id in Config.PUBLISHER_NODES:
        return False
    if not SUBDOMAIN_AVAILABLE:
        return False
    # External workers have IDs that are valid DNS labels (alphanumeric + hyphens)
    # This is a heuristic — internal nodes typically use UUIDs (node-<uuid>).
    import re
    return bool(re.match(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$", worker_id))


def get_job_organization(job_id: str) -> Optional[int]:
    """Look up the organization ID associated with ``job_id``."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.organization_id
            FROM jobs j
            JOIN projects p ON j.project_id = p.id
            WHERE j.job_id = %s
            """,
            (job_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row["organization_id"]
    except Exception as exc:
        logger.error("Failed to resolve organization for job %s: %s", job_id, exc)
    return None


def worker_authorized_for_job(worker_id: str, organization_id: Optional[int]) -> bool:
    """Check whether ``worker_id`` may handle jobs for ``organization_id``."""
    if worker_id in Config.PUBLISHER_NODES:
        return True

    # External workers always need an organization
    if is_external_worker_subdomain(worker_id) and organization_id is None:
        logger.warning(
            "External worker %s attempted to claim a public (org-less) job — denied",
            worker_id,
        )
        return False

    if organization_id is None:
        return False

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM organization_nodes WHERE organization_id = %s AND node_id = %s",
            (organization_id, worker_id),
        )
        allowed = cursor.fetchone() is not None
        conn.close()
        return allowed
    except Exception as exc:
        logger.error(
            "Failed to validate worker %s for organization %s: %s",
            worker_id,
            organization_id,
            exc,
        )
        return False


__all__ = [
    "get_job_organization",
    "is_external_worker_subdomain",
    "is_valid_worker_queue",
    "worker_authorized_for_job",
]
