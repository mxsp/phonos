"""
Helper functions for managing user connections/contacts for direct calling
"""
from datetime import datetime
import logging

from .database import get_db

logger = logging.getLogger(__name__)


def get_user_contacts(db_path, user_id):
    """Get all contacts for a user (bidirectional connections)"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    # Get bidirectional connections
    cursor = conn.execute('''
        SELECT
            u.id,
            u.username,
            u.profile_picture_url,
            CASE
                WHEN u.last_login_at > NOW() - INTERVAL '5 minutes' THEN 'online'
                WHEN u.last_login_at > NOW() - INTERVAL '1 hour' THEN 'away'
                ELSE 'offline'
            END as status,
            u.last_login_at
        FROM users u
        WHERE u.id IN (
            SELECT connected_user_id FROM user_connections
            WHERE user_id = %s AND status = 'accepted'
            UNION
            SELECT user_id FROM user_connections
            WHERE connected_user_id = %s AND status = 'accepted'
        )
        ORDER BY
            CASE
                WHEN u.last_login_at > NOW() - INTERVAL '5 minutes' THEN 1
                WHEN u.last_login_at > NOW() - INTERVAL '1 hour' THEN 2
                ELSE 3
            END,
            u.username ASC
    ''', (user_id, user_id))

    contacts = [dict(row) for row in cursor.fetchall()]

    return contacts


def get_all_users_for_calling(db_path, user_id):
    """Get all users that can be called (excluding self)"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    cursor = conn.execute('''
        SELECT
            u.id,
            u.username,
            u.profile_picture_url,
            u.company,
            u.university,
            CASE
                WHEN u.last_login_at > NOW() - INTERVAL '5 minutes' THEN 'online'
                WHEN u.last_login_at > NOW() - INTERVAL '1 hour' THEN 'away'
                ELSE 'offline'
            END as status,
            u.last_login_at,
            CASE WHEN uc.id IS NOT NULL THEN 1 ELSE 0 END as is_contact
        FROM users u
        LEFT JOIN user_connections uc ON
            ((uc.user_id = %s AND uc.connected_user_id = u.id) OR
             (uc.connected_user_id = %s AND uc.user_id = u.id))
            AND uc.status = 'accepted'
        WHERE u.id != %s
        ORDER BY
            is_contact DESC,
            CASE
                WHEN u.last_login_at > NOW() - INTERVAL '5 minutes' THEN 1
                WHEN u.last_login_at > NOW() - INTERVAL '1 hour' THEN 2
                ELSE 3
            END,
            u.username ASC
    ''', (user_id, user_id, user_id))

    users = [dict(row) for row in cursor.fetchall()]

    return users


def add_user_connection(db_path, user_id, connected_user_id):
    """Add a user connection (contact)"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    try:
        cursor = conn.execute('''
            INSERT INTO user_connections (user_id, connected_user_id, status)
            VALUES (%s, %s, 'accepted')
            ON CONFLICT DO NOTHING
        ''', (user_id, connected_user_id))

        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error adding connection: {e}")
        conn.rollback()
        return False


def remove_user_connection(db_path, user_id, connected_user_id):
    """Remove a user connection"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    try:
        # Remove bidirectional
        cursor = conn.execute('''
            DELETE FROM user_connections
            WHERE (user_id = %s AND connected_user_id = %s)
               OR (user_id = %s AND connected_user_id = %s)
        ''', (user_id, connected_user_id, connected_user_id, user_id))

        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error removing connection: {e}")
        conn.rollback()
        return False


def is_user_contact(db_path, user_id, other_user_id):
    """Check if users are connected"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    cursor = conn.execute('''
        SELECT COUNT(*) AS n FROM user_connections
        WHERE ((user_id = %s AND connected_user_id = %s) OR
               (user_id = %s AND connected_user_id = %s))
          AND status = 'accepted'
    ''', (user_id, other_user_id, other_user_id, user_id))

    count = cursor.fetchone()['n']

    return count > 0
