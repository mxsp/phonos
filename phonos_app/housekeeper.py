"""Housekeeping tasks for Phonos."""
from __future__ import annotations

import os
import threading
import time
import logging
from datetime import datetime, timedelta

from .app import app, socketio
from .helpers.database import get_db
from .helpers.notifications import record_job_notification

logger = logging.getLogger(__name__)

def check_job_timeouts():
    """
    Background task to check for jobs that have exceeded the timeout limit.
    Marks them as failed and refunds credits.
    """
    # Timeout threshold (8 hours)
    TIMEOUT_HOURS = 8

    while True:
        try:
            # Sleep first to let app startup finish
            time.sleep(300)  # Check every 5 minutes

            with app.app_context():
                db = get_db()

                # Calculate the cutoff time
                cutoff_time = datetime.utcnow() - timedelta(hours=TIMEOUT_HOURS)

                # Find jobs that are still running/processing and older than cutoff
                # Note: We use SQLite/Postgres compatible syntax where possible
                # For Postgres: created_at < NOW() - INTERVAL '8 hours'
                # But we'll compute the datetime in Python to be safe across DBs

                timed_out_jobs = db.execute(
                    """
                    SELECT job_id, creator_id, project_id, cost, model, created_at
                    FROM jobs
                    WHERE status IN ('processing', 'running', 'submitted', 'queued')
                    AND created_at < %s
                    """,
                    (cutoff_time,)
                ).fetchall()

                if not timed_out_jobs:
                    continue

                logger.info(f"Found {len(timed_out_jobs)} timed out jobs.")

                for job in timed_out_jobs:
                    job_id = job['job_id']
                    creator_id = job['creator_id']
                    cost = job['cost'] or 1 # Default to 1 if cost is missing (legacy jobs)

                    logger.info(f"Timing out job {job_id} (created {job['created_at']})")

                    # Mark as failed
                    error_message = f"Job timed out (exceeded {TIMEOUT_HOURS} hours)."

                    db.execute(
                        "UPDATE jobs SET status = %s, message = %s WHERE job_id = %s",
                        ("failed", error_message, job_id)
                    )

                    # Log the failure
                    db.execute(
                        "INSERT INTO job_logs (job_id, status, message) VALUES (%s, %s, %s)",
                        (job_id, "failed", error_message)
                    )

                    # Refund credits
                    db.execute(
                        "UPDATE users SET credits = credits + %s WHERE id = %s",
                        (cost, creator_id)
                    )

                    # Commit the changes for this job
                    db.commit()

                    # Send notifications
                    # Fetch fresh job data for notification helper
                    job_data = db.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
                    if job_data:
                        recipients = record_job_notification(
                            db, job_data, "failed", error_message
                        )

                        # Emit socket update
                        # Convert datetimes for JSON
                        job_dict = dict(job_data)
                        for key, value in job_dict.items():
                            if isinstance(value, datetime):
                                job_dict[key] = value.isoformat()

                        for recipient_id in recipients:
                            socketio.emit(
                                "job_update", {"job": job_dict}, room=f"user_{recipient_id}"
                            )
                            socketio.emit("user_info_update", room=f"user_{recipient_id}")
                            socketio.emit("notification_update", room=f"user_{recipient_id}")

        except Exception as e:
            logger.error(f"Error in job timeout checker: {e}", exc_info=True)
            # Wait a bit before retrying loop to avoid rapid failure loops
            time.sleep(60)

def start_timeout_monitor():
    """Start the timeout monitor thread."""
    if os.environ.get("FLASK_ENV") == "development" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    thread = threading.Thread(target=check_job_timeouts, daemon=True, name="JobTimeoutMonitor")
    thread.start()
    logger.info("Job timeout monitor started.")
