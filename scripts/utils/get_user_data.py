import os
import sys
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

# Add the project root to the Python path to import Config
project_root = "/opt/phonos"
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Temporarily mock the Flask app context for Config to load correctly
# This is a common pattern when trying to access Flask app config
# outside of a running Flask application.
class MockApp:
    def __init__(self):
        self.config = {}

mock_app = MockApp()

# Load Config
try:
    from production.config import Config
    # Populate mock_app.config with relevant database settings
    mock_app.config['DATABASE_URL'] = Config.DATABASE_URL
except Exception as e:
    print(f"Error loading Config: {e}", file=sys.stderr)
    sys.exit(1)

def get_db_connection():
    db_url = mock_app.config.get('DATABASE_URL')
    if not db_url:
        raise ValueError("DATABASE_URL not found in config.")

    # Handle SQLite for local development fallback
    if db_url.startswith('sqlite:///'):
        print("Detected SQLite database. This script is designed for PostgreSQL.", file=sys.stderr)
        print("Please ensure DATABASE_URL environment variable is set for PostgreSQL.", file=sys.stderr)
        sys.exit(1)

    # Parse PostgreSQL URL
    url = urlparse(db_url)
    conn_args = {
        'database': url.path[1:],
        'user': url.username,
        'password': url.password,
        'host': url.hostname,
        'port': url.port
    }
    
    # Establish connection
    print(f"Connecting to PostgreSQL database: {url.hostname}:{url.port}/{url.path[1:]}", file=sys.stderr)
    return psycopg2.connect(**conn_args)

def get_all_users_and_emails():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        query = """
            SELECT u.username, uc.email
            FROM users u
            JOIN user_credentials uc ON u.user_credential_id = uc.id
            ORDER BY u.id
        """
        cursor.execute(query)
        users = cursor.fetchall()
        return users
    except Exception as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    user_data = get_all_users_and_emails()
    if user_data:
        print("--- All Usernames and Emails ---")
        for user in user_data:
            print(f"Username: {user['username']}, Email: {user['email']}")
    else:
        print("No users found or an error occurred.")
