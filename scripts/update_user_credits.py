import os
import psycopg2
from dotenv import load_dotenv
import uuid # Import the uuid module

load_dotenv()

def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        database_url = "sqlite:///phonos.db"
        print(f"WARNING: DATABASE_URL not set, using default: {database_url}")
    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = False
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        raise

def update_admin_credits(username="admin", credits=10000):
    """
    Updates the credits for a given user. If the user does not exist,
    it creates a new user with default values and sets their credits.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if user exists
        cursor.execute("SELECT id FROM users WHERE username = %s;", (username,))
        user_record = cursor.fetchone()

        if user_record:
            user_db_id = user_record[0]
            # User exists, update credits
            cursor.execute("UPDATE users SET credits = %s, updated_at = NOW() WHERE id = %s;",
                           (credits, user_db_id))
            print(f"Successfully updated credits for user '{username}' (ID: {user_db_id}) to {credits}.")
        else:
            # User does not exist, create new user with default values
            print(f"User '{username}' not found. Creating new user...")
            
            new_user_uuid = str(uuid.uuid4()) # Generate a UUID for user_id

            # 1. Insert into users table
            insert_user_sql = """
            INSERT INTO users (user_id, username, credits, is_admin, plan, storage_quota, storage_used, layout_density, table_style)
            VALUES (%s, %s, %s, %s, 'Standard', 1073741824, 0, 'cozy', 'zebra')
            RETURNING id;
            """
            cursor.execute(insert_user_sql, (new_user_uuid, username, credits, 1 if username == "admin" else 0))
            new_user_db_id = cursor.fetchone()[0]

            # 2. Insert into user_credentials table
            dummy_email = f"{username}_{new_user_db_id}@example.com" # Use user_db_id to ensure unique email
            dummy_password_hash = b'dummy_password_hash' # This should be a properly hashed password

            insert_credentials_sql = """
            INSERT INTO user_credentials (user_id, email, password, email_verified, plan, email_notifications_enabled, tos_accepted)
            VALUES (%s, %s, %s, 1, 'Standard', 1, 1)
            RETURNING id;
            """
            cursor.execute(insert_credentials_sql, (new_user_db_id, dummy_email, dummy_password_hash))
            new_credential_db_id = cursor.fetchone()[0]

            # 3. Update the users table with the user_credential_id
            update_user_credential_id_sql = """
            UPDATE users SET user_credential_id = %s WHERE id = %s;
            """
            cursor.execute(update_user_credential_id_sql, (new_credential_db_id, new_user_db_id))

            print(f"User '{username}' created with DB ID {new_user_db_id}, User_ID '{new_user_uuid}', and credits set to {credits}.")
            print(f"Associated user_credentials entry created with ID {new_credential_db_id}.")

        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"An error occurred: {e}")
        raise
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    update_admin_credits()
