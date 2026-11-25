"""Notification helpers."""
from __future__ import annotations

import html

from flask import url_for

from ..app import socketio
from .emails import send_email
from .models import get_supported_model_name


def ensure_user_layout_columns(db):
    # Check columns using PostgreSQL syntax
    columns = {
        row["column_name"]
        for row in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
        ).fetchall()
    }
    altered = False
    if "layout_density" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN layout_density TEXT DEFAULT 'cozy'")
        altered = True
    if "table_style" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN table_style TEXT DEFAULT 'zebra'")
        altered = True
    if altered:
        db.commit()


def ensure_notifications_table(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS notifications (
               id SERIAL PRIMARY KEY,
               user_id INTEGER NOT NULL,
               title TEXT NOT NULL,
               message TEXT NOT NULL,
               category TEXT DEFAULT 'system',
               related_job_id TEXT,
               metadata TEXT,
               is_read INTEGER NOT NULL DEFAULT 0,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
           )"""
    )
    db.commit()


def record_job_notification(db, job_row, status, message=None):
    if not job_row:
        return set()

    ensure_notifications_table(db)
    ensure_user_layout_columns(db)

    # Deduplication: skip if a job notification already exists for this job_id AND status
    # Also check by status to allow different notifications (e.g., completed vs failed)
    notification_key = f"{job_row['job_id']}:{status}"
    redis_key = f"job_notification_sent:{notification_key}"
    try:
        import redis
        from ..app import redis_pool
        r = redis.Redis(connection_pool=redis_pool)
        if r.get(redis_key):
            import logging
            logging.getLogger(__name__).info(
                "Notification already sent for job %s (status: %s), skipping duplicate.",
                job_row["job_id"], status
            )
            member_rows = db.execute(
                "SELECT user_id FROM project_assignments WHERE project_id = %s",
                (job_row["project_id"],),
            ).fetchall()
            recipients = {row["user_id"] for row in member_rows}
            recipients.add(job_row["creator_id"])
            return recipients
        r.set(redis_key, "1", ex=300, nx=True)
    except Exception:
        pass

    existing = db.execute(
        "SELECT 1 FROM notifications WHERE related_job_id = %s AND category = 'job' AND title LIKE %s LIMIT 1",
        (job_row["job_id"], f"%{status.capitalize()}%"),
    ).fetchone()
    if existing:
        import logging
        logging.getLogger(__name__).info(
            "Notification already exists for job %s (status: %s), skipping duplicate.",
            job_row["job_id"], status
        )
        member_rows = db.execute(
            "SELECT user_id FROM project_assignments WHERE project_id = %s",
            (job_row["project_id"],),
        ).fetchall()
        recipients = {row["user_id"] for row in member_rows}
        recipients.add(job_row["creator_id"])
        return recipients

    project = db.execute(
        "SELECT name FROM projects WHERE id = %s", (job_row["project_id"],)
    ).fetchone()
    project_name = project["name"] if project else "Project"
    model_name = get_supported_model_name(job_row.get("model"))
    default_message = (
        "Job completed successfully." if status == "completed" else "Job encountered an error."
    )
    summary = (message or "").strip() or default_message
    formatted_message = f"{project_name} • {model_name} — {summary}"

    member_rows = db.execute(
        "SELECT user_id FROM project_assignments WHERE project_id = %s",
        (job_row["project_id"],),
    ).fetchall()
    recipients = {row["user_id"] for row in member_rows}
    recipients.add(job_row["creator_id"])

    for user_id in recipients:
        db.execute(
            "INSERT INTO notifications (user_id, title, message, category, related_job_id) VALUES (%s, %s, %s, %s, %s)",
            (
                user_id,
                f"Job {'Completed' if status == 'completed' else 'Failed'}",
                formatted_message,
                "job",
                job_row["job_id"],
            ),
        )

        user_email_prefs = db.execute(
            """
            SELECT uc.email, u.username, uc.email_notifications_enabled
            FROM users u
            JOIN user_credentials uc ON u.user_credential_id = uc.id
            WHERE u.id = %s
            """,
            (user_id,),
        ).fetchone()
        if (
            user_email_prefs
            and user_email_prefs["email"]
            and user_email_prefs["email_notifications_enabled"]
        ):
            try:
                dashboard_url = url_for("dashboard", _external=True)
            except RuntimeError:
                base_url = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5041").rstrip("/")
                dashboard_url = f"{base_url}/dashboard"
            email_subject = f"[Phonos] Job {'Completed' if status == 'completed' else 'Failed'}"
            job_identifier = html.escape(str(job_row.get("job_id", ""))[:8]) or "—"
            safe_username = html.escape(user_email_prefs["username"])
            safe_project = html.escape(project_name)
            safe_summary = html.escape(summary)
            email_body = (
                f"<p>Hi {safe_username},</p>"
                f"<p>The job <strong>{job_identifier}</strong> in the project "
                f"<strong>{safe_project}</strong> has {html.escape(status)}.</p>"
                f"<p>{safe_summary}</p>"
                f"<p><a href='{dashboard_url}'>Open your dashboard</a> to review the details.</p>"
            )
            send_email(user_email_prefs["email"], email_subject, email_body)

    db.commit()
    return recipients


def create_user_notification(db, user_id, title, message, category="system", metadata=None):
    ensure_notifications_table(db)
    ensure_user_layout_columns(db)

    import json
    metadata_json = json.dumps(metadata) if metadata else None

    db.execute(
        "INSERT INTO notifications (user_id, title, message, category, metadata) VALUES (%s, %s, %s, %s, %s)",
        (user_id, title, message, category, metadata_json),
    )

    socketio.emit("notification_update", room=f"user_{user_id}")
