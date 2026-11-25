"""Database migration helpers."""
from __future__ import annotations

import logging
from .database import get_db

logger = logging.getLogger(__name__)

def ensure_jobs_cost_column():
    """Ensure the 'cost' column exists in the 'jobs' table."""
    db = get_db()
    try:
        # Check if column exists
        cursor = db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'jobs' AND column_name = 'cost'"
        )
        if cursor.fetchone():
            return

        logger.info("Adding 'cost' column to 'jobs' table...")
        db.execute("ALTER TABLE jobs ADD COLUMN cost INTEGER DEFAULT 0")
        db.commit()
        logger.info("Successfully added 'cost' column.")
    except Exception as e:
        logger.error(f"Error checking/adding 'cost' column: {e}")
        # Don't re-raise to avoid blocking app startup if something minor fails,
        # though this is critical for the refund feature.
