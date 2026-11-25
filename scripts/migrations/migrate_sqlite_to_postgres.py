#!/usr/bin/env python3
"""Migrate data from SQLite to PostgreSQL database."""

import sqlite3
import psycopg2
import psycopg2.extras
import sys

# Database configuration
SQLITE_DB = '/opt/phonos/phonos.db'
PG_CONFIG = {
    'dbname': 'phonos',
    'user': 'phonos_user',
    'password': os.environ.get('POSTGRES_PASSWORD', ''),
    'host': 'localhost',
}

# Tables to migrate in order (respecting foreign keys)
TABLES_TO_MIGRATE = [
    'user_credentials',
    'users',
    'workspaces',
    'projects',
    'files',
    'jobs',
    'job_results',
    'invitations',
    'workspace_members',
    'project_members',
    'login_history',
    'sessions',
    'api_keys',
    'notifications',
    'audit_logs',
    'storage_usage',
]

def migrate_table(sqlite_conn, pg_conn, table_name):
    """Migrate a single table from SQLite to PostgreSQL."""
    print(f"Migrating table: {table_name}")

    sqlite_cur = sqlite_conn.cursor()
    pg_cur = pg_conn.cursor()

    # Get all data from SQLite
    try:
        sqlite_cur.execute(f"SELECT * FROM {table_name}")
        rows = sqlite_cur.fetchall()

        if not rows:
            print(f"  No data in {table_name}")
            return

        # Get column names
        column_names = [description[0] for description in sqlite_cur.description]

        # Check if table exists in PostgreSQL
        pg_cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name = %s
            )
        """, (table_name,))

        if not pg_cur.fetchone()[0]:
            print(f"  Table {table_name} does not exist in PostgreSQL, skipping")
            return

        # Insert data into PostgreSQL
        placeholders = ','.join(['%s'] * len(column_names))
        columns = ','.join(column_names)
        insert_query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"

        successful = 0
        failed = 0

        for row in rows:
            try:
                # Convert memoryview to bytes for bytea columns
                converted_row = []
                for val in row:
                    if isinstance(val, memoryview):
                        converted_row.append(bytes(val))
                    else:
                        converted_row.append(val)

                pg_cur.execute(insert_query, converted_row)
                successful += 1
            except Exception as e:
                failed += 1
                print(f"  Error inserting row: {e}")
                # Try to continue with other rows

        pg_conn.commit()
        print(f"  Migrated {successful} rows ({failed} failed)")

        # Update sequence for tables with auto-increment IDs
        if 'id' in column_names:
            try:
                pg_cur.execute(f"SELECT MAX(id) FROM {table_name}")
                max_id = pg_cur.fetchone()[0]
                if max_id:
                    pg_cur.execute(f"SELECT setval('{table_name}_id_seq', {max_id}, true)")
                    pg_conn.commit()
                    print(f"  Updated sequence for {table_name} to {max_id}")
            except Exception as e:
                print(f"  Could not update sequence: {e}")

    except sqlite3.Error as e:
        print(f"  SQLite error: {e}")
    except psycopg2.Error as e:
        print(f"  PostgreSQL error: {e}")
        pg_conn.rollback()


def main():
    """Main migration function."""
    print("Starting migration from SQLite to PostgreSQL...")

    # Connect to databases
    try:
        sqlite_conn = sqlite3.connect(SQLITE_DB)
        print(f"Connected to SQLite database: {SQLITE_DB}")
    except sqlite3.Error as e:
        print(f"Error connecting to SQLite: {e}")
        sys.exit(1)

    try:
        pg_conn = psycopg2.connect(**PG_CONFIG)
        print(f"Connected to PostgreSQL database: {PG_CONFIG['dbname']}")
    except psycopg2.Error as e:
        print(f"Error connecting to PostgreSQL: {e}")
        sys.exit(1)

    # Migrate each table
    for table_name in TABLES_TO_MIGRATE:
        migrate_table(sqlite_conn, pg_conn, table_name)

    # Close connections
    sqlite_conn.close()
    pg_conn.close()

    print("\nMigration complete!")


if __name__ == '__main__':
    main()
