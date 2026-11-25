import sqlite3
import os

DB_PATH = 'phonos.db'

def fix_sessions():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        print("Checking active sessions...")
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
