"""Aggregated helper exports for backwards compatibility."""
from __future__ import annotations

# Use lazy import to avoid circular dependency during app initialization
try:
    from ..app import SUPPORTED_MODEL_IDS as APP_SUPPORTED_MODEL_IDS
except ImportError:
    # During initialization, this may fail due to circular imports
    APP_SUPPORTED_MODEL_IDS = None
from config import Config

TEAM_ROLE_OWNER = Config.TEAM_ROLE_OWNER
TEAM_ROLE_ADMIN = Config.TEAM_ROLE_ADMIN
TEAM_ROLE_MEMBER = Config.TEAM_ROLE_MEMBER
TEAM_ROLE_BILLING = Config.TEAM_ROLE_BILLING
TEAM_MUTABLE_ROLES = Config.TEAM_MUTABLE_ROLES

PROJECT_ROLE_MANAGER = Config.PROJECT_ROLE_MANAGER
PROJECT_ROLE_CONTRIBUTOR = Config.PROJECT_ROLE_CONTRIBUTOR
PROJECT_ROLE_VIEWER = Config.PROJECT_ROLE_VIEWER
PROJECT_MUTABLE_ROLES = Config.PROJECT_MUTABLE_ROLES


from .auth import admin_required, login_required, organization_role_required, project_role_required
from .credits import (
    ensure_monthly_credit_reset,
    organization_has_organization_plan,
    organization_is_enterprise,
    parse_sqlite_timestamp,
    user_has_enterprise_plan,
    user_has_organization_plan,
)
from .database import close_db, get_db, get_job_status_counts
from .emails import send_email
# Lazy import for listeners to avoid circular imports during app initialization
# (listeners.py needs redis_pool which is defined later in app.py)
def listen_for_job_updates(*args, **kwargs):
    from .listeners import listen_for_job_updates as _listen
    return _listen(*args, **kwargs)
from .models import get_supported_model_name
from .notifications import (
    create_user_notification,
    ensure_notifications_table,
    ensure_user_layout_columns,
    record_job_notification,
)

from .paths import (
    DEFAULT_JOB_BRANCH,
    INPUTS_JOB_BRANCH,
    get_project_storage_roots,
    get_safe_project_path,
    get_all_job_output_roots,
    get_allowed_job_output_prefixes,
    get_job_branch_path,
    get_job_branch_path_from_root,
    get_job_branch_relative_path,
    get_job_root_path,
    infer_job_root_from_output_path,
    is_path_within,
    is_path_within_any,
    friendly_artifact_download_name,
    list_job_artifact_dirs,
    normalize_job_branch_name,
    resolve_job_artifact_path,
    resolve_job_branch_path,
    resolve_file_path,
)
from .disk_manager import get_disk_manager, DiskManager

from .storage import (
    apply_plan_to_user,
    build_plan_catalog,
    delete_job_file_and_update_storage,
    format_storage_bytes,
    get_user_storage_state,
)
from .projects import externalize_project_role, get_project_assignment, normalize_project_role
from .uploads import allowed_file
from . import tags
from .organizations import (
    determine_job_target_queue,
    ensure_personal_organization,
    generate_unique_org_slug,
    get_organization_membership,
    get_organization_node_rows,
    is_valid_public_key,
    normalize_worker_types,
    parse_worker_types,
    serialize_datetime,
    serialize_organization,
    serialize_organization_member,
    serialize_organization_node,
)

# Team aliases (new terminology) ---------------------------------------------------------

team_role_required = organization_role_required

ensure_personal_team = ensure_personal_organization
generate_unique_team_slug = generate_unique_org_slug
get_team_membership = get_organization_membership
get_team_node_rows = get_organization_node_rows
serialize_team = serialize_organization
serialize_team_member = serialize_organization_member
serialize_team_node = serialize_organization_node

team_is_enterprise = organization_is_enterprise
team_has_team_plan = organization_has_organization_plan
user_has_team_plan = user_has_organization_plan

SUPPORTED_MODEL_IDS = APP_SUPPORTED_MODEL_IDS

__all__ = [
    "SUPPORTED_MODEL_IDS",
    "admin_required",
    "login_required",
    "organization_role_required",
    "team_role_required",
    "project_role_required",
    "ensure_monthly_credit_reset",
    "organization_has_organization_plan",
    "organization_is_enterprise",
    "team_has_team_plan",
    "team_is_enterprise",
    "parse_sqlite_timestamp",
    "user_has_enterprise_plan",
    "user_has_organization_plan",
    "user_has_team_plan",
    "close_db",
    "get_db",
    "get_job_status_counts",
    "send_email",
    "listen_for_job_updates",
    "get_supported_model_name",
    "create_user_notification",
    "ensure_notifications_table",
    "ensure_user_layout_columns",
    "record_job_notification",
    "TEAM_MUTABLE_ROLES",
    "TEAM_ROLE_ADMIN",
    "TEAM_ROLE_BILLING",
    "TEAM_ROLE_MEMBER",
    "TEAM_ROLE_OWNER",
    "PROJECT_MUTABLE_ROLES",
    "PROJECT_ROLE_CONTRIBUTOR",
    "PROJECT_ROLE_MANAGER",
    "PROJECT_ROLE_VIEWER",
    "determine_job_target_queue",
    "ensure_personal_organization",
    "ensure_personal_team",
    "generate_unique_org_slug",
    "generate_unique_team_slug",
    "get_organization_membership",
    "get_team_membership",
    "get_organization_node_rows",
    "get_team_node_rows",
    "is_valid_public_key",
    "normalize_worker_types",
    "parse_worker_types",
    "serialize_datetime",
    "serialize_organization",
    "serialize_organization_member",
    "serialize_organization_node",
    "serialize_team",
    "serialize_team_member",
    "serialize_team_node",
    "get_project_storage_roots",
    "get_safe_project_path",
    "get_all_job_output_roots",
    "get_allowed_job_output_prefixes",
    "DEFAULT_JOB_BRANCH",
    "INPUTS_JOB_BRANCH",
    "get_job_branch_path",
    "get_job_branch_path_from_root",
    "get_job_branch_relative_path",
    "get_job_root_path",
    "infer_job_root_from_output_path",
    "is_path_within",
    "is_path_within_any",
    "friendly_artifact_download_name",
    "list_job_artifact_dirs",
    "normalize_job_branch_name",
    "resolve_job_artifact_path",
    "resolve_job_branch_path",
    "resolve_file_path",
    "get_disk_manager",
    "DiskManager",
    "externalize_project_role",
    "get_project_assignment",
    "normalize_project_role",
    "apply_plan_to_user",
    "build_plan_catalog",
    "delete_job_file_and_update_storage",
    "format_storage_bytes",
    "get_user_storage_state",
    "allowed_file",
    "tags",
]
