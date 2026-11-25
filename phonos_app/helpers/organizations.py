"""Legacy organization helper shims.

This module bridges older imports that still refer to
``phonos_app.helpers.organizations`` with the newer team-oriented
implementations.  All functionality is delegated to the
``phonos_app.helpers.teams`` module so existing callers continue to work
without modification.
"""
from __future__ import annotations

from .teams import (
    TEAM_MUTABLE_ROLES as ORGANIZATION_MUTABLE_ROLES,
    TEAM_ROLE_ADMIN as ORGANIZATION_ROLE_ADMIN,
    TEAM_ROLE_BILLING as ORGANIZATION_ROLE_BILLING,
    TEAM_ROLE_MEMBER as ORGANIZATION_ROLE_MEMBER,
    TEAM_ROLE_OWNER as ORGANIZATION_ROLE_OWNER,
    determine_job_target_queue as _determine_team_job_target_queue,
    ensure_personal_team as _ensure_personal_team,
    generate_unique_team_slug as _generate_unique_team_slug,
    get_team_membership as _get_team_membership,
    get_team_node_rows as _get_team_node_rows,
    is_valid_public_key as _is_valid_public_key,
    normalize_worker_types as _normalize_worker_types,
    parse_worker_types as _parse_worker_types,
    serialize_datetime as _serialize_datetime,
    serialize_team as _serialize_team,
    serialize_team_member as _serialize_team_member,
    serialize_team_node as _serialize_team_node,
)


def determine_job_target_queue(db, organization_id, model_type):
    """Return the Redis queue for an organization's preferred worker."""

    return _determine_team_job_target_queue(db, organization_id, model_type)


def ensure_personal_organization(db, user_id):
    """Ensure the user has a personal organization (team) workspace."""

    return _ensure_personal_team(db, user_id)


def generate_unique_org_slug(db, name: str) -> str:
    """Generate a unique slug for an organization name."""

    return _generate_unique_team_slug(db, name)


def get_organization_membership(db, user_id: int, organization_id: int):
    """Return the role a user holds within the organization."""

    return _get_team_membership(db, user_id, organization_id)


def get_organization_node_rows(db, organization_id: int):
    """Fetch the node rows registered to an organization."""

    return _get_team_node_rows(db, organization_id)


def is_valid_public_key(pem: str) -> bool:
    """Validate a PEM-formatted public key."""

    return _is_valid_public_key(pem)


def normalize_worker_types(worker_types):
    """Normalise worker types against the supported model registry."""

    return _normalize_worker_types(worker_types)


def parse_worker_types(value):
    """Parse a worker type string into a list of individual identifiers."""

    return _parse_worker_types(value)


def serialize_datetime(value):
    """Serialise datetime objects consistently."""

    return _serialize_datetime(value)


def serialize_organization(row):
    """Serialise an organization row for JSON responses."""

    return _serialize_team(row)


def serialize_organization_member(row):
    """Serialise an organization member row for JSON responses."""

    return _serialize_team_member(row)


def serialize_organization_node(row):
    """Serialise an organization node row for JSON responses."""

    return _serialize_team_node(row)


__all__ = [
    "ORGANIZATION_MUTABLE_ROLES",
    "ORGANIZATION_ROLE_ADMIN",
    "ORGANIZATION_ROLE_BILLING",
    "ORGANIZATION_ROLE_MEMBER",
    "ORGANIZATION_ROLE_OWNER",
    "determine_job_target_queue",
    "ensure_personal_organization",
    "generate_unique_org_slug",
    "get_organization_membership",
    "get_organization_node_rows",
    "is_valid_public_key",
    "normalize_worker_types",
    "parse_worker_types",
    "serialize_datetime",
    "serialize_organization",
    "serialize_organization_member",
    "serialize_organization_node",
]
