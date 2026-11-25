#!/usr/bin/env python3
"""Create an admin user for Phonos using PostgreSQL."""

import os
import sys
import bcrypt
import uuid
import argparse
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get database URL from environment
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///phonos.db')

def create_admin_user():
    """
    Creates a new administrative user from command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Create a new administrative user.")
    parser.add_argument("username", help="Admin username")
    parser.add_argument("email", help="Admin email")
    parser.add_argument("password", help="Admin password")
    args = parser.parse_args()

    try:
        username = args.username
        email = args.email
        password = args.password

        # Hash password
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        user_uuid = str(uuid.uuid4())

        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Check if user or email already exists
        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            print(f"Error: User '{username}' already exists.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        cursor.execute("SELECT id FROM user_credentials WHERE email = %s", (email,))
        if cursor.fetchone():
            print(f"Error: Email '{email}' already exists.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        # Insert new user into users table
        cursor.execute(
            """INSERT INTO users (user_id, username, is_admin, plan, credits, storage_quota, storage_used, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id""",
            (user_uuid, username, 1, 'Premium', 999999, 10737418240, 0)  # 10GB for admin
        )
        user_id = cursor.fetchone()['id']

        # Insert into user_credentials table
        cursor.execute(
            """INSERT INTO user_credentials (user_id, email, password, email_verified, plan, created_at)
               VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id""",
            (user_id, email, hashed_password, 1, 'Premium')
        )
        credential_id = cursor.fetchone()['id']

        # Update users table with credential reference
        cursor.execute("UPDATE users SET user_credential_id = %s WHERE id = %s", (credential_id, user_id))

        conn.commit()
        conn.close()

        print(f"✓ Admin user created successfully!")
        print(f"  Username: {username}")
        print(f"  Email: {email}")
        print(f"  Admin privileges: Yes")
        print(f"  Plan: Premium")
        print(f"  Credits: 999999")
        print(f"  Storage: 10GB")

    except psycopg2.Error as e:
        print(f"Error: Database error - {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    create_admin_user()
