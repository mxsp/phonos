"""Budget alerts and monitoring helpers."""
from __future__ import annotations

from .database import get_db
from .notifications import create_user_notification as create_notification


def create_budget_alert(
    alert_type: str,
    threshold_value: int,
    user_id: int = None,
    team_id: int = None,
) -> dict:
    """Create a new budget alert."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        INSERT INTO budget_alerts (user_id, team_id, alert_type, threshold_value, is_active)
        VALUES (%s, %s, %s, %s, 1)
        """,
        (user_id, team_id, alert_type, threshold_value),
    )

    alert_id = result.lastrowid
    db.commit()

    return {
        "id": alert_id,
        "user_id": user_id,
        "team_id": team_id,
        "alert_type": alert_type,
        "threshold_value": threshold_value,
        "current_value": 0,
        "is_triggered": False,
        "is_active": True,
    }


def get_budget_alert(alert_id: int) -> dict:
    """Get a budget alert by ID."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, user_id, team_id, alert_type, threshold_value, current_value,
               is_triggered, last_triggered_at, is_active, created_at
        FROM budget_alerts
        WHERE id = %s
        """,
        (alert_id,),
    )

    row = result.fetchone()
    if not row:
        return None

    alert = dict(row)
    alert["is_triggered"] = bool(alert["is_triggered"])
    alert["is_active"] = bool(alert["is_active"])

    return alert


def get_user_budget_alerts(user_id: int, active_only: bool = True) -> list[dict]:
    """Get all budget alerts for a user."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    if active_only:
        result = db.execute(
            """
            SELECT id, user_id, team_id, alert_type, threshold_value, current_value,
                   is_triggered, last_triggered_at, is_active, created_at
            FROM budget_alerts
            WHERE user_id = %s AND is_active = 1
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
    else:
        result = db.execute(
            """
            SELECT id, user_id, team_id, alert_type, threshold_value, current_value,
                   is_triggered, last_triggered_at, is_active, created_at
            FROM budget_alerts
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,),
        )

    alerts = []
    for row in result.fetchall():
        alert = dict(row)
        alert["is_triggered"] = bool(alert["is_triggered"])
        alert["is_active"] = bool(alert["is_active"])
        alerts.append(alert)

    return alerts


def get_team_budget_alerts(team_id: int, active_only: bool = True) -> list[dict]:
    """Get all budget alerts for a team."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    if active_only:
        result = db.execute(
            """
            SELECT id, user_id, team_id, alert_type, threshold_value, current_value,
                   is_triggered, last_triggered_at, is_active, created_at
            FROM budget_alerts
            WHERE team_id = %s AND is_active = 1
            ORDER BY created_at DESC
            """,
            (team_id,),
        )
    else:
        result = db.execute(
            """
            SELECT id, user_id, team_id, alert_type, threshold_value, current_value,
                   is_triggered, last_triggered_at, is_active, created_at
            FROM budget_alerts
            WHERE team_id = %s
            ORDER BY created_at DESC
            """,
            (team_id,),
        )

    alerts = []
    for row in result.fetchall():
        alert = dict(row)
        alert["is_triggered"] = bool(alert["is_triggered"])
        alert["is_active"] = bool(alert["is_active"])
        alerts.append(alert)

    return alerts


def update_budget_alert_value(alert_id: int, current_value: int) -> bool:
    """Update the current value for a budget alert and check if threshold is exceeded."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    # Get alert details
    result = db.execute(
        """
        SELECT user_id, team_id, alert_type, threshold_value, is_triggered
        FROM budget_alerts
        WHERE id = %s
        """,
        (alert_id,),
    )

    row = result.fetchone()
    if not row:
        return False

    alert = dict(row)
    threshold_exceeded = current_value >= alert["threshold_value"]

    # Update the alert
    if threshold_exceeded and not alert["is_triggered"]:
        db.execute(
            """
            UPDATE budget_alerts
            SET current_value = %s, is_triggered = 1, last_triggered_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (current_value, alert_id),
        )

        # Send notification
        if alert["user_id"]:
            create_notification(
                user_id=alert["user_id"],
                title="Budget Alert Triggered",
                message=f"Your {alert['alert_type']} budget alert has been triggered. Current value: {current_value}, Threshold: {alert['threshold_value']}",
                category="budget",
            )
    else:
        db.execute(
            """
            UPDATE budget_alerts
            SET current_value = %s
            WHERE id = %s
            """,
            (current_value, alert_id),
        )

    db.commit()
    return True


def check_and_update_user_budget_alerts(user_id: int, credits_used: int) -> None:
    """Check and update all budget alerts for a user based on credit usage."""
    alerts = get_user_budget_alerts(user_id, active_only=True)

    for alert in alerts:
        if alert["alert_type"] == "credits":
            update_budget_alert_value(alert["id"], credits_used)


def deactivate_budget_alert(alert_id: int) -> bool:
    """Deactivate a budget alert."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        UPDATE budget_alerts
        SET is_active = 0
        WHERE id = %s
        """,
        (alert_id,),
    )

    db.commit()
    return result.rowcount > 0


def reset_budget_alert(alert_id: int) -> bool:
    """Reset a triggered budget alert."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        UPDATE budget_alerts
        SET current_value = 0, is_triggered = 0, last_triggered_at = NULL
        WHERE id = %s
        """,
        (alert_id,),
    )

    db.commit()
    return result.rowcount > 0


def delete_budget_alert(alert_id: int) -> bool:
    """Permanently delete a budget alert."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        DELETE FROM budget_alerts
        WHERE id = %s
        """,
        (alert_id,),
    )

    db.commit()
    return result.rowcount > 0
