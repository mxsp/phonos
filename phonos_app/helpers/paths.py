"""Filesystem path helpers with multi-disk support."""
from __future__ import annotations

import os
import re
import logging
import psycopg2
from datetime import datetime, timedelta
from typing import Any, List, Optional

from ..app import app
from .database import get_db

logger = logging.getLogger(__name__)

JOB_BRANCHES_DIRNAME = "branches"
DEFAULT_JOB_BRANCH = "main"
INPUTS_JOB_BRANCH = "inputs"
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Lazy import to avoid circular dependencies
_disk_manager = None

# Path resolution cache with TTL (time-to-live)
# Cache key: stored_path -> (resolved_path, timestamp)
_path_cache = {}
_cache_ttl = timedelta(minutes=5)
_cache_max_size = 10000  # Prevent unbounded memory growth


def _get_disk_manager():
    """Get the disk manager instance (lazy import)."""
    global _disk_manager
    if _disk_manager is None:
        try:
            from .disk_manager import get_disk_manager
            _disk_manager = get_disk_manager()
        except ImportError:
            logger.warning("disk_manager module not available, using fallback paths")
            return None
    return _disk_manager


def get_all_job_output_roots() -> List[str]:
    """Get all configured job output root directories.

    Returns paths from:
    1. Disk manager configuration (if available)
    2. Primary JOB_OUTPUT_DIR from app config
    3. Fallback JOB_OUTPUT_DIR_FALLBACK if configured
    """
    if app.config.get("TESTING"):
        test_root = os.path.abspath(app.config.get("JOB_OUTPUT_DIR", "/tmp/phonos-test-job-outputs"))
        fallback_root = os.path.abspath(app.config.get("JOB_OUTPUT_DIR_FALLBACK", test_root))
        roots = [test_root]
        if fallback_root not in roots:
            roots.append(fallback_root)
        return roots

    roots = []

    # Try to get from disk manager first
    dm = _get_disk_manager()
    if dm:
        for disk in dm.get_all_disks():
            if disk.path and disk.path not in roots:
                roots.append(os.path.abspath(disk.path))

    # Add primary from config
    primary_cfg = app.config.get("JOB_OUTPUT_DIR") or os.path.join(os.getcwd(), "job_outputs")
    primary = os.path.abspath(primary_cfg)
    if primary not in roots:
        roots.insert(0, primary)  # Primary goes first

    # Add fallback path if configured
    fallback = app.config.get("JOB_OUTPUT_DIR_FALLBACK")
    if fallback:
        fallback = os.path.abspath(fallback)
        if fallback not in roots:
            roots.append(fallback)

    return roots


def get_allowed_job_output_prefixes(project_paths: Optional[List[str]] = None) -> List[str]:
    """Return all filesystem prefixes trusted for job output access."""
    allowed_prefixes: List[str] = []

    for project_path in project_paths or []:
        if project_path:
            allowed_prefixes.append(os.path.abspath(project_path))
            allowed_prefixes.append(os.path.realpath(project_path))

    local_job_outputs = os.path.abspath(os.path.join(os.getcwd(), "job_outputs"))
    allowed_prefixes.append(local_job_outputs)

    config_job_outputs = os.path.abspath(app.config.get("JOB_OUTPUT_DIR", ""))
    if config_job_outputs:
        allowed_prefixes.append(config_job_outputs)
        allowed_prefixes.append(os.path.realpath(config_job_outputs))

    fallback_dir = app.config.get("JOB_OUTPUT_DIR_FALLBACK", "")
    if fallback_dir:
        allowed_prefixes.append(os.path.abspath(fallback_dir))
        allowed_prefixes.append(os.path.realpath(fallback_dir))

    try:
        for root in get_all_job_output_roots():
            if root:
                allowed_prefixes.append(os.path.abspath(root))
                allowed_prefixes.append(os.path.realpath(root))
    except Exception as e:
        logger.warning("Could not get job output roots from disk manager: %s", e)

    # Container default plus any deployment-specific legacy prefixes from config
    # (JOB_OUTPUT_LEGACY_PREFIXES) so paths stored by other deployments resolve.
    allowed_prefixes.append("/app/job_outputs")
    allowed_prefixes.extend(app.config.get("JOB_OUTPUT_LEGACY_PREFIXES", []))

    return list({path for path in allowed_prefixes if path})


def is_path_within(candidate_path: str, root_path: str) -> bool:
    """Return True only when candidate_path is inside root_path."""
    if not candidate_path or not root_path:
        return False

    try:
        candidate_abs = os.path.abspath(candidate_path)
        root_abs = os.path.abspath(root_path)
        candidate_real = os.path.realpath(candidate_path)
        root_real = os.path.realpath(root_path)
        return (
            os.path.commonpath([candidate_abs, root_abs]) == root_abs
            and os.path.commonpath([candidate_real, root_real]) == root_real
        )
    except (OSError, ValueError):
        return False


def is_path_within_any(candidate_path: str, root_paths: List[str]) -> bool:
    """Return True when candidate_path is inside one of root_paths."""
    return any(is_path_within(candidate_path, root) for root in root_paths if root)


def _normalise_candidate(path: str, allowed_roots: List[str]) -> str:
    """Return an absolute, normalised path if it is within any allowed root."""
    absolute = os.path.abspath(path)
    if is_path_within_any(absolute, allowed_roots):
        return absolute
    return None


def get_project_storage_roots(project_id: int, organization_id: int, creator_id: int = None) -> list[str]:
    """Return ordered candidate directories for a project's storage.

    The first entry represents the canonical modern layout using the disk
    with most available space. Subsequent entries cover legacy structures.

    Args:
        project_id: The project ID
        organization_id: Optional organization/team ID
        creator_id: Optional project creator user ID (for user-personal paths)
    """
    all_roots = get_all_job_output_roots()

    # Select best disk for new storage (space-based selection)
    dm = _get_disk_manager()
    if dm:
        best_disk = dm.select_disk_for_output()
        if best_disk:
            # Put best disk first
            all_roots = [best_disk] + [r for r in all_roots if r != best_disk]

    candidates: List[str] = []

    # If we don't have creator_id, try to get it from the database
    if not creator_id:
        try:
            db = get_db()
            project_row = db.execute(
                "SELECT creator_id FROM projects WHERE id = %s", (project_id,)
            ).fetchone()
            if project_row:
                creator_id = project_row.get('creator_id')
        except Exception:
            pass

    # Build candidates for each root
    for base_dir in all_roots:
        base_dir = os.path.abspath(base_dir)

        if organization_id:
            db = get_db()
            slug_row = None
            try:
                slug_row = db.execute(
                    "SELECT slug FROM organizations WHERE id = %s", (organization_id,)
                ).fetchone()
            except (psycopg2.OperationalError, psycopg2.ProgrammingError):
                try:
                    slug_row = db.execute(
                        "SELECT slug FROM teams WHERE id = %s", (organization_id,)
                    ).fetchone()
                except Exception:
                    pass

            if slug_row and slug_row["slug"]:
                org_slug = slug_row["slug"]
                org_base_path = os.path.join(base_dir, org_slug)
                candidates.append(os.path.join(org_base_path, "projects", str(project_id)))

        # Check user-personal project folder if we have creator_id (most common case)
        if creator_id:
            user_personal_path = os.path.join(base_dir, f'user-{creator_id}-personal', 'projects', str(project_id))
            if os.path.isdir(user_personal_path):
                candidates.insert(0, user_personal_path)  # Prioritize this path

        # Fallback: scan for any user-personal folders containing this project
        try:
            for entry in os.listdir(base_dir):
                if entry.startswith('user-') and entry.endswith('-personal'):
                    user_project_path = os.path.join(base_dir, entry, 'projects', str(project_id))
                    if os.path.isdir(user_project_path) and user_project_path not in candidates:
                        candidates.append(user_project_path)
        except (OSError, PermissionError):
            pass

        # Legacy layouts
        candidates.append(os.path.join(base_dir, str(project_id)))
        candidates.append(os.path.join(base_dir, f"project_{project_id}"))

    # Normalize and deduplicate
    normalised: list[str] = []
    for candidate in candidates:
        normalised_path = _normalise_candidate(candidate, all_roots)
        if normalised_path and normalised_path not in normalised:
            normalised.append(normalised_path)

    return normalised


def get_safe_project_path(project_id: int, organization_id: int) -> str:
    """Return the preferred project directory for storing artefacts."""
    if app.config.get("TESTING"):
        base_root = os.path.abspath(app.config.get("JOB_OUTPUT_DIR_FALLBACK") or app.config.get("JOB_OUTPUT_DIR") or "/tmp/phonos-test-job-outputs")
        team_segment = f"user-{organization_id}-personal" if organization_id else "shared"
        return os.path.join(base_root, team_segment, "projects", str(project_id))

    candidates = get_project_storage_roots(project_id, organization_id)
    if not candidates:
        return None

    # Prefer an existing directory to avoid unnecessary migrations
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate

    # For new projects, use the first candidate (best disk)
    return candidates[0]


def get_job_output_path(db, project_id: int, organization_id: int, job_id: str) -> str:
    """Construct a safe, absolute path for a job's output directory."""
    project_path = get_safe_project_path(project_id, organization_id)
    if not project_path:
        return None

    job_output_dir = os.path.abspath(os.path.join(project_path, "jobs", job_id))

    # Final security check - ensure path is within an allowed root
    all_roots = get_all_job_output_roots()
    if not is_path_within_any(job_output_dir, all_roots):
        logger.error(f"Path traversal attempt detected: {job_output_dir}")
        return None

    try:
        os.makedirs(job_output_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Failed to create job output directory {job_output_dir}: {e}")
        return None

    return job_output_dir


def normalize_job_branch_name(branch: str) -> str:
    """Return a safe branch name for job artifact storage."""
    candidate = (branch or DEFAULT_JOB_BRANCH).strip()
    if not _BRANCH_NAME_RE.match(candidate):
        return DEFAULT_JOB_BRANCH
    return candidate


def get_job_root_path(project_path: str, job_id: str) -> str:
    """Return the canonical root directory for a job."""
    return os.path.abspath(os.path.join(project_path, "jobs", job_id))


def get_job_branch_relative_path(branch: str = DEFAULT_JOB_BRANCH) -> str:
    """Return a branch path relative to a job root."""
    return os.path.join(JOB_BRANCHES_DIRNAME, normalize_job_branch_name(branch))


def get_job_branch_path_from_root(job_root_dir: str, branch: str = DEFAULT_JOB_BRANCH, create: bool = False) -> str:
    """Return a branch directory path from an existing job root path."""
    branch_dir = os.path.abspath(
        os.path.join(job_root_dir, get_job_branch_relative_path(branch))
    )
    if create:
        os.makedirs(branch_dir, exist_ok=True)
    return branch_dir


def get_job_branch_path(
    project_path: str,
    job_id: str,
    branch: str = DEFAULT_JOB_BRANCH,
    create: bool = False,
) -> str:
    """Return a branch directory path for a job under a project."""
    return get_job_branch_path_from_root(
        get_job_root_path(project_path, job_id), branch=branch, create=create
    )


def infer_job_root_from_output_path(job_id: str, output_path: str) -> str:
    """Infer a job root directory from an absolute output file path."""
    if not output_path:
        return None
    abs_output = os.path.abspath(output_path)
    parts = abs_output.split(os.sep)
    for idx, part in enumerate(parts):
        if part == job_id:
            candidate_root = os.sep.join(parts[: idx + 1]) or os.sep
            return os.path.abspath(candidate_root)
    return None


def _is_within(candidate_path: str, root_path: str) -> bool:
    return is_path_within(candidate_path, root_path)


def _policy_branch_entry(policy: dict[str, Any] | None, branch: str) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {}
    branches = policy.get("artifact_branches")
    if not isinstance(branches, dict):
        return {}
    entry = branches.get(branch, {})
    return entry if isinstance(entry, dict) else {}


def resolve_job_branch_path(
    project_path: str,
    job_id: str,
    output_file: str = None,
    policy: dict[str, Any] | None = None,
    branch: str = DEFAULT_JOB_BRANCH,
    create: bool = False,
) -> str:
    """Resolve the best branch directory for a job, including legacy fallbacks."""
    branch_name = normalize_job_branch_name(branch)
    candidates: list[str] = []

    project_job_root: str = None
    if project_path:
        project_job_root = get_job_root_path(project_path, job_id)
        candidates.append(get_job_branch_path_from_root(project_job_root, branch=branch_name))
        if branch_name == DEFAULT_JOB_BRANCH:
            candidates.append(project_job_root)

    inferred_job_root = infer_job_root_from_output_path(job_id, output_file)
    if inferred_job_root:
        if branch_name == DEFAULT_JOB_BRANCH and output_file:
            candidates.insert(0, os.path.dirname(os.path.abspath(output_file)))
        candidates.append(get_job_branch_path_from_root(inferred_job_root, branch=branch_name))
        if branch_name == DEFAULT_JOB_BRANCH:
            candidates.append(inferred_job_root)

    branch_policy = _policy_branch_entry(policy, branch_name)
    if branch_policy:
        absolute_path = branch_policy.get("path")
        if isinstance(absolute_path, str) and absolute_path:
            abs_policy = os.path.abspath(absolute_path)
            if not project_job_root or _is_within(abs_policy, project_job_root):
                candidates.insert(0, abs_policy)
        relative_path = branch_policy.get("relative_path")
        if isinstance(relative_path, str) and relative_path:
            if project_job_root:
                rel_candidate = os.path.abspath(os.path.join(project_job_root, relative_path))
                if _is_within(rel_candidate, project_job_root):
                    candidates.insert(0, rel_candidate)
            elif inferred_job_root:
                rel_candidate = os.path.abspath(os.path.join(inferred_job_root, relative_path))
                if _is_within(rel_candidate, inferred_job_root):
                    candidates.insert(0, rel_candidate)

    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)

    if not deduped:
        return None

    selected = deduped[0]
    for candidate in deduped:
        if os.path.isdir(candidate):
            selected = candidate
            break

    if create:
        try:
            os.makedirs(selected, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create job branch directory %s: %s", selected, exc)
            return None

    return selected


def _iter_policy_artifacts(policy: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(policy, dict):
        return []
    entries: list[dict[str, Any]] = []
    artifacts_info = policy.get("artifacts_info")
    if isinstance(artifacts_info, list):
        entries.extend(item for item in artifacts_info if isinstance(item, dict))
    artifacts = policy.get("artifacts")
    if isinstance(artifacts, list):
        entries.extend(item for item in artifacts if isinstance(item, dict))
    return entries


def resolve_job_artifact_path(
    project_path: str,
    job_id: str,
    artifact_name: str,
    output_file: str = None,
    policy: dict[str, Any] | None = None,
    preferred_branch: str = DEFAULT_JOB_BRANCH,
) -> str:
    """Resolve a concrete artifact file path for a job."""
    if not artifact_name or "/" in artifact_name or "\\" in artifact_name or ".." in artifact_name:
        return None

    project_job_root = get_job_root_path(project_path, job_id) if project_path else None
    inferred_job_root = infer_job_root_from_output_path(job_id, output_file)
    job_root = project_job_root or inferred_job_root

    candidates: list[str] = []

    for entry in _iter_policy_artifacts(policy):
        entry_filename = entry.get("filename") or entry.get("name")
        if entry_filename != artifact_name:
            continue

        entry_path = entry.get("path")
        if isinstance(entry_path, str) and entry_path:
            abs_entry_path = os.path.abspath(entry_path)
            if job_root and _is_within(abs_entry_path, job_root):
                candidates.append(abs_entry_path)

        relative_path = entry.get("relative_path")
        if isinstance(relative_path, str) and relative_path and job_root:
            rel_candidate = os.path.abspath(os.path.join(job_root, relative_path))
            if _is_within(rel_candidate, job_root):
                candidates.append(rel_candidate)

        entry_branch = normalize_job_branch_name(entry.get("branch") or preferred_branch)
        branch_dir = resolve_job_branch_path(
            project_path=project_path,
            job_id=job_id,
            output_file=output_file,
            policy=policy,
            branch=entry_branch,
            create=False,
        )
        if branch_dir:
            candidates.append(os.path.abspath(os.path.join(branch_dir, artifact_name)))

    default_branch_dir = resolve_job_branch_path(
        project_path=project_path,
        job_id=job_id,
        output_file=output_file,
        policy=policy,
        branch=preferred_branch,
        create=False,
    )
    if default_branch_dir:
        candidates.append(os.path.abspath(os.path.join(default_branch_dir, artifact_name)))

    if job_root:
        candidates.append(os.path.abspath(os.path.join(job_root, artifact_name)))

    if output_file:
        candidates.append(
            os.path.abspath(
                os.path.join(os.path.dirname(os.path.abspath(output_file)), artifact_name)
            )
        )

    deduped: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in deduped:
            continue
        if job_root and not _is_within(candidate, job_root):
            continue
        deduped.append(candidate)

    for candidate in deduped:
        if os.path.exists(candidate) and os.path.isfile(candidate):
            return candidate

    return None


def list_job_artifact_dirs(
    project_path: str,
    job_id: str,
    output_file: str = None,
    policy: dict[str, Any] | None = None,
) -> list[str]:
    """Return known artifact directories for a job in priority order."""
    dirs: list[str] = []

    main_dir = resolve_job_branch_path(
        project_path=project_path,
        job_id=job_id,
        output_file=output_file,
        policy=policy,
        branch=DEFAULT_JOB_BRANCH,
        create=False,
    )
    if main_dir:
        dirs.append(main_dir)

    for entry in _iter_policy_artifacts(policy):
        branch = normalize_job_branch_name(entry.get("branch"))
        branch_dir = resolve_job_branch_path(
            project_path=project_path,
            job_id=job_id,
            output_file=output_file,
            policy=policy,
            branch=branch,
            create=False,
        )
        if branch_dir:
            dirs.append(branch_dir)

    root_path = get_job_root_path(project_path, job_id) if project_path else infer_job_root_from_output_path(job_id, output_file)
    if root_path:
        dirs.append(root_path)

    deduped: list[str] = []
    for directory in dirs:
        if directory and directory not in deduped and os.path.isdir(directory):
            deduped.append(directory)
    return deduped


def _slug_for_filename(value: str, fallback: str = "") -> str:
    """Collapse arbitrary text into a filesystem-safe, readable token."""
    if not value:
        return fallback
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if len(slug) > 60:
        slug = slug[:60].rstrip("_")
    return slug or fallback


def friendly_artifact_download_name(
    job_name: str,
    job_id: str,
    filename: str,
    record: dict[str, Any] | None = None,
) -> str:
    """Build an individual, descriptive download filename for a job artifact.

    Across multiple jobs the raw artifact names collide: every DiffDock job
    emits ``rank1.sdf`` / ``<job_id>_pose_0.sdf``, AlphaFold emits
    ``ranked_0.pdb``, etc. This prefixes a sanitized job-name slug and a human
    descriptor (pose/model number) so each downloaded file is uniquely and
    meaningfully named, e.g. ``MyDockRun_pose_1.sdf``.

    Falls back to the original filename when it has no extension.
    """
    if not filename:
        return filename or ""

    base, ext = os.path.splitext(filename)
    if not ext:
        return filename
    ext = ext.lower()
    lower = filename.lower()
    record = record or {}

    job_slug = (
        _slug_for_filename(job_name)
        or _slug_for_filename((job_id or "")[:8])
        or "job"
    )

    descriptor: str = None
    pose_match = re.search(r"_pose_(\d+)", lower)
    rank_match = re.match(r"rank[_-]?(\d+)", lower)
    ranked_match = re.search(r"ranked_(\d+)", lower)
    pose_index = record.get("pose_index")

    if "_docked_ligand" in lower or lower.endswith("docked_ligand.sdf"):
        descriptor = "docked_ligand"
    elif pose_match:
        descriptor = "pose_%d" % (int(pose_match.group(1)) + 1)  # 0-indexed -> 1-indexed
    elif rank_match:
        descriptor = "pose_%d" % int(rank_match.group(1))
    elif isinstance(pose_index, int):
        descriptor = "pose_%d" % (pose_index + 1)
    elif ranked_match:
        descriptor = "model_%d" % (int(ranked_match.group(1)) + 1)

    if descriptor is None:
        clean = base
        # DiffDock-RE names files "<job_id>_..." — drop the raw id prefix.
        if job_id and clean.lower().startswith(job_id.lower()):
            clean = clean[len(job_id):].lstrip("_-")
        descriptor = _slug_for_filename(clean, fallback="output")

    # Avoid doubling up when the descriptor already starts with the job slug.
    if descriptor.lower().startswith(job_slug.lower()):
        return descriptor + ext
    return "%s_%s%s" % (job_slug, descriptor, ext)


def resolve_file_path(stored_path: str) -> Optional[str]:
    """
    Resolve a stored file path, checking all configured disks.

    Handles migration from single-disk to multi-disk setup.
    Files that were stored with old paths will still be found.

    Args:
        stored_path: The path as stored in the database

    Returns:
        The actual path to the file, or None if not found
    """
    # Check if path already exists as-is
    if os.path.exists(stored_path):
        return stored_path

    # Try disk manager's resolver if available
    dm = _get_disk_manager()
    if dm:
        resolved = dm.resolve_file_path(stored_path)
        if resolved:
            return resolved

    # Manual fallback: extract relative path and search
    all_roots = get_all_job_output_roots()
    relative = stored_path

    # Remove any known disk prefix to get relative path
    for root in all_roots:
        if stored_path.startswith(root):
            relative = stored_path[len(root):].lstrip('/')
            break

    # Also handle legacy paths: deployment-specific prefixes from config plus
    # the relative 'job_outputs/' form.
    legacy_prefixes = [
        p.rstrip('/') + '/' for p in app.config.get("JOB_OUTPUT_LEGACY_PREFIXES", [])
    ]
    legacy_prefixes.append('job_outputs/')
    for prefix in legacy_prefixes:
        if stored_path.startswith(prefix):
            relative = stored_path[len(prefix):]
            break

    # Try to find the file on any configured disk
    for root in all_roots:
        candidate = os.path.join(root, relative)
        if os.path.exists(candidate):
            logger.debug(f"Resolved {stored_path} to {candidate}")
            return candidate

    logger.warning(f"Could not resolve path: {stored_path}")
    return None


def resolve_file_path_cached(stored_path: str) -> Optional[str]:
    """
    Cached version of resolve_file_path with TTL.

    Cache entries expire after 5 minutes to handle file moves/deletions.
    This provides significant performance improvement for repeated path lookups.

    Args:
        stored_path: The path as stored in the database

    Returns:
        The actual path to the file, or None if not found
    """
    global _path_cache

    cache_key = stored_path
    current_time = datetime.now()

    # Check cache
    if cache_key in _path_cache:
        cached_path, cache_time = _path_cache[cache_key]

        # Check if cache entry is still valid
        if current_time - cache_time < _cache_ttl:
            # Verify file still exists (quick check)
            if os.path.exists(cached_path):
                return cached_path
            else:
                # File was deleted, remove from cache
                del _path_cache[cache_key]
        else:
            # Cache entry expired, remove it
            del _path_cache[cache_key]

    # Cache miss - resolve path using original function
    resolved = resolve_file_path(stored_path)

    # Cache result if found
    if resolved:
        # Check cache size limit
        if len(_path_cache) >= _cache_max_size:
            # Simple eviction: remove oldest 20% of entries
            _evict_old_cache_entries()

        _path_cache[cache_key] = (resolved, current_time)

    return resolved


def _evict_old_cache_entries():
    """Evict oldest 20% of cache entries when cache is full."""
    global _path_cache

    if not _path_cache:
        return

    # Sort by timestamp (oldest first)
    sorted_entries = sorted(_path_cache.items(), key=lambda x: x[1][1])

    # Remove oldest 20%
    num_to_remove = max(1, len(sorted_entries) // 5)
    for key, _ in sorted_entries[:num_to_remove]:
        del _path_cache[key]

    logger.debug(f"Evicted {num_to_remove} old cache entries, {len(_path_cache)} remaining")


def clear_path_cache():
    """Clear the path resolution cache.

    Should be called after:
    - File migrations (team changes)
    - Bulk file operations
    - Manual file system modifications
    """
    global _path_cache
    old_size = len(_path_cache)
    _path_cache = {}
    logger.info(f"Cleared path cache ({old_size} entries)")


def get_path_cache_stats() -> dict:
    """Get path cache statistics for monitoring.

    Returns:
        Dict with keys: size, max_size, ttl_seconds
    """
    return {
        'size': len(_path_cache),
        'max_size': _cache_max_size,
        'ttl_seconds': int(_cache_ttl.total_seconds())
    }
