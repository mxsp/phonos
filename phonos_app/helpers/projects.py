"""Project helper utilities."""
from __future__ import annotations

from typing import Optional

PROJECT_ROLE_MANAGER = "Project Manager"
PROJECT_ROLE_CONTRIBUTOR = "Project Contributor"
PROJECT_ROLE_VIEWER = "Project Viewer"
PROJECT_MUTABLE_ROLES = {PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER}

PROJECT_ROLE_LABELS = {
    "owner": PROJECT_ROLE_MANAGER,
    "collaborator": PROJECT_ROLE_CONTRIBUTOR,
    "spectator": PROJECT_ROLE_VIEWER,
}
PROJECT_ROLE_REVERSE_LABELS = {value: key for key, value in PROJECT_ROLE_LABELS.items()}


def normalize_project_role(value: str) -> str:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned in PROJECT_ROLE_REVERSE_LABELS:
        return cleaned
    mapped = PROJECT_ROLE_LABELS.get(cleaned.lower())
    if mapped:
        return mapped
    return cleaned


def externalize_project_role(value: str) -> str:
    if not value:
        return value
    return PROJECT_ROLE_REVERSE_LABELS.get(value, value)


def get_project_assignment(db, user_id: Optional[int], project_id: Optional[int]):
    """Return the canonical role for ``user_id`` within ``project_id``.

    Legacy databases stored project roles using the external labels (``owner``,
    ``collaborator``, ``spectator``). Newer migrations persist the human-facing
    strings (``Project Manager`` etc.).  Downstream permission checks expect the
    canonical form, so normalise anything we fetch before returning it.

    If no direct assignment exists, checks if the user has access via team membership.
    """

    if not user_id or not project_id:
        return None

    try:
        from .lab_accounts import get_user_auth_state, ROLE_VIEWER
        auth_state = get_user_auth_state(db, user_id)
        if not auth_state.get("exists") or not auth_state.get("active"):
            return None
    except Exception:
        auth_state = {"exists": True, "active": True, "is_admin": False, "system_role": "scientist"}
        ROLE_VIEWER = "viewer"

    resolved_role: Optional[str] = None

    # Check for direct project assignment first
    assignment = db.execute(
        "SELECT role FROM project_assignments WHERE user_id = %s AND project_id = %s",
        (user_id, project_id),
    ).fetchone()

    if assignment:
        role = assignment["role"]
        normalized_role = normalize_project_role(role)
        resolved_role = normalized_role or role

    # If no direct assignment, check if user has access via team membership
    project = db.execute(
        "SELECT id, team_id, organization_id FROM projects WHERE id = %s",
        (project_id,),
    ).fetchone()

    if not project:
        return None

    if not resolved_role:
        team_id = project["team_id"] or project["organization_id"]
        if team_id:
            team_member = db.execute(
                "SELECT role FROM team_members WHERE team_id = %s AND user_id = %s",
                (team_id, user_id),
            ).fetchone()

            if team_member:
                team_role = team_member["role"]
                if team_role in ("owner", "admin"):
                    resolved_role = PROJECT_ROLE_MANAGER
                elif team_role == "member":
                    resolved_role = PROJECT_ROLE_CONTRIBUTOR
                elif team_role == "billing":
                    resolved_role = PROJECT_ROLE_VIEWER

    # Instance Admin override: active admins can inspect, manage, or reassign any lab project
    if auth_state.get("is_admin"):
        return PROJECT_ROLE_MANAGER

    # Global viewer ceiling: viewer/guest accounts are strictly read-only across all projects
    if resolved_role and auth_state.get("system_role") == ROLE_VIEWER:
        return PROJECT_ROLE_VIEWER

    return resolved_role


def validate_file_creator_access(db, user_id: int, project_id: int) -> tuple[bool, str]:
    """Validate that a user has permission to upload files to a project.

    Args:
        db: Database connection
        user_id: User ID attempting to upload
        project_id: Target project ID

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.

    Security:
        - User must have Project Manager or Project Contributor role
        - Project Viewer role is read-only and cannot upload
        - Archived projects cannot accept uploads
    """
    if not user_id or not project_id:
        return False, "Invalid user or project ID"

    # Check if project is archived
    project = db.execute(
        "SELECT is_archived, name FROM projects WHERE id = %s",
        (project_id,)
    ).fetchone()

    if not project:
        return False, "Project not found"

    if project["is_archived"]:
        return False, f"Cannot upload files to archived project '{project['name']}'"

    # Get user's role in the project
    role = get_project_assignment(db, user_id, project_id)

    if not role:
        return False, "You do not have access to this project"

    # Only Project Manager and Project Contributor can upload
    if role not in (PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR):
        return False, f"Your role ({role}) does not have permission to upload files. You need Contributor or Manager role."

    return True, ""


def check_project_archived(db, project_id: int) -> bool:
    """Check if a project is archived.

    Args:
        db: Database connection
        project_id: Project ID to check

    Returns:
        True if project is archived, False otherwise
    """
    project = db.execute(
        "SELECT is_archived FROM projects WHERE id = %s",
        (project_id,)
    ).fetchone()

    return bool(project and project["is_archived"])
