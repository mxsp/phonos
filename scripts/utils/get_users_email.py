import os
import psycopg2
from psycopg2.extras import RealDictCursor
from production.database import DatabaseConfig, _resolve_database_url

def get_users_with_email():
    try:
        db_url = _resolve_database_url()
        db_config = DatabaseConfig(db_url)
        conn = psycopg2.connect(**db_config.get_connection_params(), cursor_factory=RealDictCursor)
        cursor = conn.cursor()

        query = """
            SELECT
                u.username,
                uc.email
            FROM
                users u
            JOIN
                user_credentials uc ON u.id = uc.user_id;
        """
        cursor.execute(query)
        users = cursor.fetchall()

        cursor.close()
        conn.close()
        return users
    except Exception as e:
        print(f"Error accessing database: {e}")
        return None

if __name__ == "__main__":
    users_data = get_users_with_email()
    if users_data:
        for user in users_data:
            print(f"Username: {user['username']}, Email: {user['email']}")
    else:
        print("Could not retrieve user data.")