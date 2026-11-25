"""
One-time migration: encrypt plaintext api_webhooks.secret_key values at rest.

Rewrites any row whose secret_key is not already an enc:v1: token to the
encrypted form used by phonos_app.helpers.field_crypto. Idempotent - rows
already encrypted are skipped, so it is safe to run multiple times.

Run manually (not wired into app boot):
    python3.11 scripts/encrypt_webhook_secrets.py
"""

import os
import sys

# Make project root + production importable so phonos_app resolves
# (mirrors the sys.path setup in wsgi.py).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "production"))
sys.path.insert(0, PROJECT_ROOT)

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from phonos_app.helpers.field_crypto import encrypt_field, is_encrypted


def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        database_url = "sqlite:///phonos.db"
        print(f"WARNING: DATABASE_URL not set, using default: {database_url}")
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    return conn


def migrate():
    conn = get_db_connection()
    encrypted_count = 0
    skipped_count = 0
    failed_count = 0
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT id, secret_key FROM api_webhooks")
        rows = cursor.fetchall()

        for row in rows:
            secret = row["secret_key"]
            if secret is None or is_encrypted(secret):
                skipped_count += 1
                continue
            try:
                cursor.execute(
                    "UPDATE api_webhooks SET secret_key = %s WHERE id = %s",
                    (encrypt_field(secret), row["id"]),
                )
                conn.commit()
                encrypted_count += 1
            except Exception as e:
                conn.rollback()
                failed_count += 1
                print(f"ERROR: failed to encrypt webhook {row['id']}: {e}")

        print(
            f"Done: {encrypted_count} encrypted, {skipped_count} skipped "
            f"(already encrypted or NULL), {failed_count} failed "
            f"of {len(rows)} total."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
