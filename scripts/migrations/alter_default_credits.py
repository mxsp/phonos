import os
import sys
import logging
# Add the directory containing 'production' to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), "production"))

from production.database import db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def alter_table_default_credits():
    """Alters the default value of the credits column in the users table."""
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            alter_sql = "ALTER TABLE users ALTER COLUMN credits SET DEFAULT 1000;"
            cursor.execute(alter_sql)
            conn.commit()
            logger.info("Successfully altered 'users' table: 'credits' column default set to 1000.")
    except Exception as e:
        logger.error(f"Error altering 'users' table default credits: {e}")

if __name__ == "__main__":
    alter_table_default_credits()