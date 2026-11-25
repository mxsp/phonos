"""Usage analytics and tracking helpers."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .database import get_db


def track_event(
    user_id: int,
    event_type: str,
    credits_used: int = 0,
    storage_delta: int = 0,
    compute_time: float = None,
    project_id: int = None,
    job_id: int = None,
    metadata: dict = None,
) -> int:
    """Track a usage event."""
    db = get_db()

    metadata_json = json.dumps(metadata) if metadata else None

    result = db.execute(
        """
        INSERT INTO usage_analytics (
            user_id, project_id, job_id, event_type, credits_used,
            storage_delta, compute_time, metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user_id,
            project_id,
            job_id,
            event_type,
            credits_used,
            storage_delta,
            compute_time,
            metadata_json,
        ),
    )

    row = result.fetchone()
    event_id = row["id"] if row else None
    db.commit()

    return event_id


def get_user_analytics(
    user_id: int,
    start_date: datetime = None,
    end_date: datetime = None,
) -> list[dict]:
    """Get analytics data for a user within a date range."""
    db = get_db()

    if not start_date:
        start_date = datetime.now() - timedelta(days=30)

    if not end_date:
        end_date = datetime.now()

    result = db.execute(
        """
        SELECT id, user_id, project_id, job_id, event_type, credits_used,
               storage_delta, compute_time, metadata, created_at
        FROM usage_analytics
        WHERE user_id = %s AND created_at BETWEEN %s AND %s
        ORDER BY created_at DESC
        """,
        (user_id, start_date, end_date),
    )

    events = []
    for row in result.fetchall():
        event = dict(row)
        if event["metadata"]:
            event["metadata"] = json.loads(event["metadata"])
        events.append(event)

    return events


def get_project_analytics(
    project_id: int,
    start_date: datetime = None,
    end_date: datetime = None,
) -> list[dict]:
    """Get analytics data for a project within a date range."""
    db = get_db()

    if not start_date:
        start_date = datetime.now() - timedelta(days=30)

    if not end_date:
        end_date = datetime.now()

    result = db.execute(
        """
        SELECT id, user_id, project_id, job_id, event_type, credits_used,
               storage_delta, compute_time, metadata, created_at
        FROM usage_analytics
        WHERE project_id = %s AND created_at BETWEEN %s AND %s
        ORDER BY created_at DESC
        """,
        (project_id, start_date, end_date),
    )

    events = []
    for row in result.fetchall():
        event = dict(row)
        if event["metadata"]:
            event["metadata"] = json.loads(event["metadata"])
        events.append(event)

    return events


def get_usage_summary(
    user_id: int,
    start_date: datetime = None,
    end_date: datetime = None,
) -> dict:
    """Get usage summary statistics for a user."""
    db = get_db()

    if not start_date:
        start_date = datetime.now() - timedelta(days=30)

    if not end_date:
        end_date = datetime.now()

    result = db.execute(
        """
        SELECT
            COUNT(*) as total_events,
            SUM(credits_used) as total_credits,
            SUM(storage_delta) as storage_change,
            SUM(compute_time) as total_compute_time,
            COUNT(DISTINCT project_id) as projects_used,
            COUNT(DISTINCT DATE(created_at)) as active_days
        FROM usage_analytics
        WHERE user_id = %s AND created_at BETWEEN %s AND %s
        """,
        (user_id, start_date, end_date),
    )

    summary = dict(result.fetchone())

    # Get breakdown by job status (completed, failed, other)
    # Note: Only show 'processing' for jobs created in the last hour that are not completed/failed
    result = db.execute(
        """
        SELECT
            CASE
                WHEN status = 'completed' THEN 'completed'
                WHEN status = 'failed' THEN 'failed'
                WHEN status IN ('submitted', 'queued', 'pending', 'running')
                     AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour' THEN 'processing'
                WHEN status IN ('submitted', 'queued', 'pending', 'running') THEN 'other'
                ELSE 'other'
            END as job_status,
            COUNT(*) as count,
            COALESCE(SUM(cost), 0) as credits
        FROM jobs
        WHERE creator_id = %s AND created_at BETWEEN %s AND %s
        GROUP BY (CASE
                    WHEN status = 'completed' THEN 'completed'
                    WHEN status = 'failed' THEN 'failed'
                    WHEN status IN ('submitted', 'queued', 'pending', 'running')
                         AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour' THEN 'processing'
                    WHEN status IN ('submitted', 'queued', 'pending', 'running') THEN 'other'
                    ELSE 'other'
                  END)
        ORDER BY credits DESC
        """,
        (user_id, start_date, end_date),
    )

    summary["by_job_status"] = []
    for row in result.fetchall():
        summary["by_job_status"].append(dict(row))

    # Get breakdown by event type
    result = db.execute(
        """
        SELECT event_type, COUNT(*) as count, SUM(credits_used) as credits
        FROM usage_analytics
        WHERE user_id = %s AND created_at BETWEEN %s AND %s
        GROUP BY event_type
        ORDER BY credits DESC
        """,
        (user_id, start_date, end_date),
    )

    summary["by_event_type"] = []
    for row in result.fetchall():
        summary["by_event_type"].append(dict(row))

    # Get breakdown by project
    result = db.execute(
        """
        SELECT ua.project_id, p.name as project_name, COUNT(*) as count,
               SUM(ua.credits_used) as credits
        FROM usage_analytics ua
        LEFT JOIN projects p ON ua.project_id = p.id
        WHERE ua.user_id = %s AND ua.created_at BETWEEN %s AND %s
        GROUP BY ua.project_id, p.name
        ORDER BY credits DESC
        """,
        (user_id, start_date, end_date),
    )

    summary["by_project"] = []
    for row in result.fetchall():
        summary["by_project"].append(dict(row))

    # Get breakdown by model (from job metadata)
    result = db.execute(
        """
        SELECT
            ua.metadata::jsonb->>'model' as model,
            COUNT(*) as count,
            SUM(ua.credits_used) as credits,
            SUM(ua.compute_time) as compute_time
        FROM usage_analytics ua
        WHERE ua.user_id = %s
        AND ua.created_at BETWEEN %s AND %s
        AND ua.event_type = 'job_completed'
        AND ua.metadata IS NOT NULL
        AND ua.metadata::jsonb->>'model' IS NOT NULL
        GROUP BY ua.metadata::jsonb->>'model'
        ORDER BY credits DESC
        """,
        (user_id, start_date, end_date),
    )

    summary["by_model"] = []
    for row in result.fetchall():
        summary["by_model"].append(dict(row))

    return summary


def get_team_analytics(
    team_id: int,
    start_date: datetime = None,
    end_date: datetime = None,
) -> dict:
    """Get analytics summary for a team."""
    db = get_db()

    if not start_date:
        start_date = datetime.now() - timedelta(days=30)

    if not end_date:
        end_date = datetime.now()

    # Get team member IDs
    result = db.execute(
        """
        SELECT user_id FROM team_members WHERE team_id = %s
        """,
        (team_id,),
    )

    member_ids = [row["user_id"] for row in result.fetchall()]

    if not member_ids:
        return {"total_events": 0, "total_credits": 0, "by_user": []}

    placeholders = ",".join(["%s"] * len(member_ids))

    result = db.execute(
        f"""
        SELECT
            COUNT(*) as total_events,
            SUM(credits_used) as total_credits,
            SUM(storage_delta) as storage_change,
            SUM(compute_time) as total_compute_time
        FROM usage_analytics
        WHERE user_id IN ({placeholders}) AND created_at BETWEEN %s AND %s
        """,
        (*member_ids, start_date, end_date),
    )

    summary = dict(result.fetchone())

    # Get breakdown by user
    result = db.execute(
        f"""
        SELECT ua.user_id, u.username, COUNT(*) as count, SUM(ua.credits_used) as credits
        FROM usage_analytics ua
        JOIN users u ON ua.user_id = u.id
        WHERE ua.user_id IN ({placeholders}) AND ua.created_at BETWEEN %s AND %s
        GROUP BY ua.user_id, u.username
        ORDER BY credits DESC
        """,
        (*member_ids, start_date, end_date),
    )

    summary["by_user"] = []
    for row in result.fetchall():
        summary["by_user"].append(dict(row))

    return summary


def get_daily_usage(
    user_id: int,
    days: int = 30,
) -> list[dict]:
    """Get daily usage statistics for a user."""
    db = get_db()

    start_date = datetime.now() - timedelta(days=days)

    result = db.execute(
        """
        SELECT
            DATE(created_at) as date,
            COUNT(*) as events,
            SUM(credits_used) as credits,
            SUM(compute_time) as compute_time
        FROM usage_analytics
        WHERE user_id = %s AND created_at >= %s
        GROUP BY DATE(created_at)
        ORDER BY date DESC
        """,
        (user_id, start_date),
    )

    daily_stats = []
    for row in result.fetchall():
        daily_stats.append(dict(row))

    return daily_stats
