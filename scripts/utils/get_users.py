
import sys
import os

# Add the current directory to the Python path
sys.path.append(os.getcwd())

from production.database import get_db_connection, release_db_connection

def list_users():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = "SELECT user_id, username FROM users"
        cursor.execute(query)
        
        rows = cursor.fetchall()
        
        print(f"{'User ID':<20} | {'Username':<20}")
        print("-" * 43)
        for row in rows:
            print(f"{str(row['user_id']):<20} | {row['username']:<20}")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if conn:
            release_db_connection(conn)

if __name__ == "__main__":
    list_users()
