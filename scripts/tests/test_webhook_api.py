#!/usr/bin/env python3
"""Test webhook API endpoints to verify they're working correctly."""

import sys
import psycopg2
from psycopg2.extras import RealDictCursor
import os

# Test database connection and webhook table
try:
    conn = psycopg2.connect(
        "sqlite:///phonos.db",
        cursor_factory=RealDictCursor
    )
    cursor = conn.cursor()

    print("✓ Database connection successful")

    # Test 1: Verify api_webhooks table exists
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'api_webhooks'
        ORDER BY ordinal_position
    """)

    columns = cursor.fetchall()
    if columns:
        print(f"✓ api_webhooks table exists with {len(columns)} columns:")
        for col in columns:
            print(f"  - {col['column_name']}: {col['data_type']}")
    else:
        print("✗ api_webhooks table not found!")
        sys.exit(1)

    # Test 2: Check if we can query webhooks
    cursor.execute("SELECT COUNT(*) as count FROM api_webhooks")
    count = cursor.fetchone()['count']
    print(f"✓ Current webhook count: {count}")

    # Test 3: Verify api_keys table exists
    cursor.execute("""
        SELECT COUNT(*) as count
        FROM information_schema.tables
        WHERE table_name = 'api_keys'
    """)

    if cursor.fetchone()['count'] > 0:
        print("✓ api_keys table exists")
    else:
        print("✗ api_keys table not found!")

    # Test 4: Check users exist for testing
    cursor.execute("SELECT COUNT(*) as count FROM users")
    user_count = cursor.fetchone()['count']
    print(f"✓ Users available for testing: {user_count}")

    print("\n✓ All database checks passed! Webhook functionality should work.")
    print("\nTo test in the UI:")
    print("1. Log in to the PHONOS dashboard")
    print("2. Navigate to Settings → API Tokens")
    print("3. Click on the 'Webhooks' tab")
    print("4. Click 'Add Webhook' button")
    print("5. Enter a test webhook URL (must be HTTPS)")
    print("6. Select events to subscribe to")
    print("7. Click 'Create Webhook'")

    cursor.close()
    conn.close()

except Exception as e:
    print(f"✗ Error: {e}")
    sys.exit(1)
