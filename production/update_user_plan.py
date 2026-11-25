import os
import sqlite3

DATABASE_FILE = os.environ.get('DATABASE_FILE', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.data', 'production', 'phonos.db'))
USERNAME = 'admin'
NEW_PLAN = 'Enterprise'

print(f"Connecting to database: {DATABASE_FILE}")

try:
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print(f"Tables in database: {tables}")

    cursor.execute("UPDATE users SET plan = %s WHERE username = %s", (NEW_PLAN, USERNAME))

    conn.commit()

    if cursor.rowcount > 0:
        print(f"Successfully updated user '{USERNAME}' to the '{NEW_PLAN}' plan.")
    else:
        print(f"User '{USERNAME}' not found.")

    conn.close()

except sqlite3.Error as e:
    print(f"Database error: {e}")