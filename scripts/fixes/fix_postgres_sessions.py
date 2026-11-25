import psycopg2
import os
from datetime import datetime

# Get URL from environment or hardcoded fallback matching the .env found
DATABASE_URL = "sqlite:///phonos.db"

def fix_sessions():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        print("Checking active sessions in PostgreSQL...")
        cursor.execute("SELECT id, session_token, user_id, kernel_status FROM notebook_sessions WHERE kernel_status IN ('idle', 'busy', 'starting')")
        sessions = cursor.fetchall()
        
        if not sessions:
            print("No active sessions found in DB.")
        else:
            print(f"Found {len(sessions)} active sessions. Clearing them...")
            cursor.execute("UPDATE notebook_sessions SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP, error_message = 'Manual cleanup' WHERE kernel_status IN ('idle', 'busy', 'starting')")
            conn.commit()
            print("Sessions cleared.")
            
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    fix_sessions()
