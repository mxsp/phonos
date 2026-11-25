import sqlite3
import redis
import json
import argparse
import sys
from config import Config

# Redis channel for sending confirmation back to the node
CONFIRMATION_CHANNEL_PREFIX = "pubsub:node_registration_confirmation:"

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    try:
        conn = sqlite3.connect(Config.DATABASE_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        sys.exit(1)

def get_redis_connection():
    """Establishes a connection to Redis."""
    try:
        r = redis.Redis(
            host=Config.REDIS_HOST,
            port=Config.REDIS_PORT,
            db=Config.REDIS_DB,
            decode_responses=True
        )
        r.ping()
        return r
    except redis.exceptions.RedisError as e:
        print(f"Error connecting to Redis: {e}", file=sys.stderr)
        sys.exit(1)

def list_pending_nodes():
    """Lists all nodes that are pending approval."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT node_id, created_at FROM customer_nodes WHERE approved = 0 ORDER BY created_at ASC")
    nodes = cursor.fetchall()
    conn.close()

    if not nodes:
        print("No nodes are currently pending approval.")
        return

    print("--- Pending Node Approvals ---")
    for node in nodes:
        print(f"  ID: {node['node_id']} (Registered on: {node['created_at']})")
    print("------------------------------")

def approve_node(node_id: str):
    """Approves a specific node and notifies it."""
    db_conn = get_db_connection()
    redis_conn = get_redis_connection()

    # Check if the node exists and is pending
    cursor = db_conn.cursor()
    cursor.execute("SELECT id, approved FROM customer_nodes WHERE node_id = %s", (node_id,))
    node = cursor.fetchone()

    if not node:
        print(f"Error: Node with ID '{node_id}' not found.", file=sys.stderr)
        db_conn.close()
        sys.exit(1)

    if node['approved'] == 1:
        print(f"Node '{node_id}' is already approved.")
        db_conn.close()
        return

    # Update the database to approve the node
    try:
        cursor.execute("UPDATE customer_nodes SET approved = 1 WHERE node_id = %s", (node_id,))
        db_conn.commit()
        if cursor.rowcount == 0:
            print(f"Error: Could not update node '{node_id}'. It may have been deleted.", file=sys.stderr)
            db_conn.close()
            sys.exit(1)
        print(f"Successfully approved node '{node_id}' in the database.")
    except sqlite3.Error as e:
        print(f"Database error while approving node: {e}", file=sys.stderr)
        db_conn.rollback()
        db_conn.close()
        sys.exit(1)
    finally:
        db_conn.close()

    # Publish a confirmation message to the specific node's channel
    try:
        confirmation_channel = f"{CONFIRMATION_CHANNEL_PREFIX}{node_id}"
        payload = json.dumps({'status': 'approved'})
        redis_conn.publish(confirmation_channel, payload)
        print(f"Sent approval confirmation to node on channel '{confirmation_channel}'.")
    except redis.exceptions.RedisError as e:
        print(f"Redis error while sending confirmation: {e}", file=sys.stderr)
        # The node is approved in the DB, but might time out waiting for confirmation.
        # An admin can re-run the approval to re-send the notification.
        sys.exit(1)

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Administrator tool to manage and approve new worker nodes."
    )
    parser.add_argument(
        '--list',
        action='store_true',
        help="List all nodes that are currently pending approval."
    )
    parser.add_argument(
        '--approve',
        metavar='NODE_ID',
        type=str,
        help="Approve a specific node by its full ID."
    )

    args = parser.parse_args()

    if args.list:
        list_pending_nodes()
    elif args.approve:
        approve_node(args.approve)
    else:
        parser.print_help()
        print("\nPlease provide an action, e.g., --list or --approve <NODE_ID>", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()