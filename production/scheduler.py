
from apscheduler.schedulers.blocking import BlockingScheduler
import sqlite3
import time
from ..app import get_db

def run_bulk_schedules():
    """Runs the bulk job schedules."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, project_id, creator_id, tag_id, cron_schedule FROM bulk_job_schedules WHERE is_active = 1")
    schedules = cursor.fetchall()

    for schedule in schedules:
        schedule_id, project_id, creator_id, tag_id, cron_schedule = schedule

        # This is a simplified logic. In a real-world scenario, 
        # you would parse the cron_schedule and check if it's time to run.
        # For this example, we'll just run it every time.

        cursor.execute(
            "SELECT j.id FROM jobs j JOIN job_tags jt ON jt.job_id = j.id WHERE jt.tag_id = ? AND j.project_id = ?",
            (tag_id, project_id),
        )
        job_ids = [row[0] for row in cursor.fetchall()]

        for job_id in job_ids:
            # Here you would add the logic to check user credits and submit the job.
            # For now, we'll just print the job id.
            print(f"Submitting job {job_id} for schedule {schedule_id}")

if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(run_bulk_schedules, 'interval', hours=6)
    print("Starting scheduler...")
    scheduler.start()
