"""
Migration script to add attachment support to direct_messages table
"""
import sqlite3
import os
from pathlib import Path


def migrate_direct_messages():
    """Add attachment columns to direct_messages table"""
    # Get database path
    db_path = Path(__file__).parent / 'phonos.db'

    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(direct_messages)")
        columns = [col[1] for col in cursor.fetchall()]

        migrations_needed = []
        if 'message_type' not in columns:
            migrations_needed.append(("message_type", "ALTER TABLE direct_messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'"))
        if 'attachment_url' not in columns:
            migrations_needed.append(("attachment_url", "ALTER TABLE direct_messages ADD COLUMN attachment_url TEXT"))
        if 'attachment_name' not in columns:
            migrations_needed.append(("attachment_name", "ALTER TABLE direct_messages ADD COLUMN attachment_name TEXT"))
        if 'attachment_size' not in columns:
            migrations_needed.append(("attachment_size", "ALTER TABLE direct_messages ADD COLUMN attachment_size INTEGER"))

        if not migrations_needed:
            print("Database is already up to date!")
            return True

        # Apply migrations
        for col_name, sql in migrations_needed:
            print(f"Adding column: {col_name}")
            cursor.execute(sql)

        conn.commit()
        print(f"Successfully added {len(migrations_needed)} columns to direct_messages table")
        return True

    except Exception as e:
        print(f"Migration failed: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    migrate_direct_messages()
