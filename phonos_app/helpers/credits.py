"""Credit and plan state helpers."""
from __future__ import annotations

from datetime import datetime, timedelta

from .organizations import ORGANIZATION_ROLE_BILLING


def _get_plan_service():
    """Lazy import for plan_service to support different runtime layouts (tests vs prod)."""
    try:
        from production.plan_service import (
            get_plan_credit_quota,
            is_enterprise_plan,
            normalize_plan,
            plan_supports_organizations,
        )
        return get_plan_credit_quota, is_enterprise_plan, normalize_plan, plan_supports_organizations
    except ImportError:
        from plan_service import (
            get_plan_credit_quota,
            is_enterprise_plan,
            normalize_plan,
            plan_supports_organizations,
        )
        return get_plan_credit_quota, is_enterprise_plan, normalize_plan, plan_supports_organizations


def parse_sqlite_timestamp(value):
    """
    Parse timestamp value from database.
    PostgreSQL returns datetime objects directly, so this mainly handles None values.
    Kept for backwards compatibility but functionality is now database-agnostic.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    # Fallback for string timestamps (for backwards compatibility)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def user_has_enterprise_plan(db, user_id: int) -> bool:
    _, is_enterprise_plan, _, _ = _get_plan_service()
    row = db.execute("SELECT plan FROM users WHERE id = %s", (user_id,)).fetchone()
    if not row:
        return False
    return is_enterprise_plan(row["plan"])


def user_has_organization_plan(db, user_id: int) -> bool:
    """Return ``True`` if the user's plan unlocks organization governance."""
    _, _, _, plan_supports_organizations = _get_plan_service()
    row = db.execute("SELECT plan FROM users WHERE id = %s", (user_id,)).fetchone()
    if not row:
        return False
    return plan_supports_organizations(row["plan"])


def _organization_plan_check(db, organization_id: int, predicate) -> bool:
    if not organization_id:
        return False

    billing_user = db.execute(
        """
        SELECT u.plan FROM users u
        JOIN organization_members om ON u.id = om.user_id
        WHERE om.organization_id = %s AND om.role = %s
        """,
        (organization_id, ORGANIZATION_ROLE_BILLING),
    ).fetchone()

    if billing_user and predicate(billing_user["plan"]):
        return True

    owner = db.execute(
        """
        SELECT u.plan FROM users u
        JOIN organizations o ON u.id = o.owner_id
        WHERE o.id = %s
        """,
        (organization_id,),
    ).fetchone()

    if owner and predicate(owner["plan"]):
        return True

    return False


def organization_is_enterprise(db, organization_id: int) -> bool:
    _, is_enterprise_plan, _, _ = _get_plan_service()
    return _organization_plan_check(db, organization_id, is_enterprise_plan)


def organization_has_organization_plan(db, organization_id: int) -> bool:
    """Return ``True`` when the organization is backed by an eligible plan."""
    _, _, _, plan_supports_organizations = _get_plan_service()
    return _organization_plan_check(db, organization_id, plan_supports_organizations)


def ensure_credits_reset_column(db):
    # Check if column exists using PostgreSQL syntax
    column_exists = db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'credits_last_reset'"
    ).fetchone()

    if not column_exists:
        db.execute("ALTER TABLE users ADD COLUMN credits_last_reset TIMESTAMP")
        db.commit()


def ensure_monthly_credit_reset(db, user_id):
    ensure_credits_reset_column(db)
    user_row = db.execute(
        "SELECT plan, credits_last_reset, is_admin FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    if not user_row:
        return None

    get_plan_credit_quota, _, normalize_plan, _ = _get_plan_service()
    plan = normalize_plan(user_row["plan"])
    if plan != user_row["plan"]:
        db.execute("UPDATE users SET plan = %s WHERE id = %s", (plan, user_id))
        db.commit()
    last_reset_dt = parse_sqlite_timestamp(user_row["credits_last_reset"])
    now = datetime.utcnow()

    if user_row["is_admin"]:
        if not last_reset_dt:
            db.execute(
                "UPDATE users SET credits_last_reset = %s WHERE id = %s",
                (now.strftime("%Y-%m-%d %H:%M:%S"), user_id),
            )
            db.commit()
            return now
        return last_reset_dt

    needs_reset = False
    if not last_reset_dt:
        needs_reset = True
    else:
        needs_reset = (now - last_reset_dt >= timedelta(days=30)) or (
            (now.year, now.month) != (last_reset_dt.year, last_reset_dt.month)
        )

    if needs_reset:
        db.execute(
            "UPDATE users SET credits = %s, credits_last_reset = %s WHERE id = %s",
            (get_plan_credit_quota(plan), now.strftime("%Y-%m-%d %H:%M:%S"), user_id),
        )
        db.commit()
        return now

    return last_reset_dt
