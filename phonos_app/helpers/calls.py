"""
Helper functions for managing voice/video calls
"""
import logging
from datetime import datetime

from ..helpers.database import get_db, transaction

logger = logging.getLogger(__name__)


def create_call_session(call_type, initiator_id, project_id=None):
    """
    Create a new call session
    call_type: 'direct', 'group', 'project'
    """
    with transaction() as db:
        cursor = db.execute('''
            INSERT INTO call_sessions (call_type, project_id, initiator_id, status)
            VALUES (%s, %s, %s, 'active')
        ''', (call_type, project_id, initiator_id))

        result = cursor.fetchone()


        call_id = result["id"] if result else None
    return call_id


def get_call_session(call_id):
    """Get call session details"""
    db = get_db()

    call = db.execute('''
        SELECT cs.*, u.username as initiator_username
        FROM call_sessions cs
        JOIN users u ON cs.initiator_id = u.id
        WHERE cs.id = %s
    ''', (call_id,)).fetchone()

    return call if call else None


def add_call_participant(call_id, user_id):
    """
    Add participant to call (idempotent - won't fail if already in call)
    Returns: (participant_id, was_already_in_call)
    """
    with transaction() as db:
        # Check if user is already in this call
        existing = db.execute('''
            SELECT id FROM call_participants
            WHERE call_id = %s AND user_id = %s AND left_at IS NULL
        ''', (call_id, user_id)).fetchone()

        if existing:
            return existing['id'], True  # Already in call

        # Add new participant
        cursor = db.execute('''
            INSERT INTO call_participants (call_id, user_id)
            VALUES (%s, %s)
        ''', (call_id, user_id))

        result = cursor.fetchone()


        participant_id = result["id"] if result else None
        return participant_id, False  # Newly added


def remove_call_participant(call_id, user_id):
    """Remove participant from call"""
    with transaction() as db:
        db.execute('''
            UPDATE call_participants
            SET left_at = CURRENT_TIMESTAMP
            WHERE call_id = %s AND user_id = %s AND left_at IS NULL
        ''', (call_id, user_id))


def get_call_participants(call_id):
    """Get all active participants in a call"""
    db = get_db()

    participants = db.execute('''
        SELECT cp.*, u.username
        FROM call_participants cp
        JOIN users u ON cp.user_id = u.id
        WHERE cp.call_id = %s AND cp.left_at IS NULL
        ORDER BY cp.joined_at ASC
    ''', (call_id,)).fetchall()

    return participants


def update_participant_state(call_id, user_id, is_muted=None, is_video_on=None):
    """Update participant's media state"""
    with transaction() as db:
        updates = []
        params = []

        if is_muted is not None:
            updates.append('is_muted = %s')
            params.append(int(is_muted))

        if is_video_on is not None:
            updates.append('is_video_on = %s')
            params.append(int(is_video_on))

        if updates:
            params.extend([call_id, user_id])
            db.execute(f'''
                UPDATE call_participants
                SET {', '.join(updates)}
                WHERE call_id = %s AND user_id = %s AND left_at IS NULL
            ''', params)


def end_call_session(call_id):
    """End call session"""
    with transaction() as db:
        # Update call session
        db.execute('''
            UPDATE call_sessions
            SET status = 'ended', ended_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (call_id,))

        # Update all participants still in call
        db.execute('''
            UPDATE call_participants
            SET left_at = CURRENT_TIMESTAMP
            WHERE call_id = %s AND left_at IS NULL
        ''', (call_id,))


def get_active_calls_for_project(project_id):
    """Get all active calls for a project"""
    db = get_db()

    calls = db.execute('''
        SELECT cs.*, u.username as initiator_username,
               COUNT(cp.id) as participant_count
        FROM call_sessions cs
        JOIN users u ON cs.initiator_id = u.id
        LEFT JOIN call_participants cp ON cs.id = cp.call_id AND cp.left_at IS NULL
        WHERE cs.project_id = %s AND cs.status = 'active'
        GROUP BY cs.id
        ORDER BY cs.started_at DESC
    ''', (project_id,)).fetchall()

    return calls


def get_user_active_calls(user_id):
    """Get all active calls that the user can join (across all their projects)"""
    db = get_db()

    calls = []
    # Fetch call sessions
    call_sessions = db.execute('''
        SELECT DISTINCT cs.*, u.username as initiator_username,
               p.name as project_name
        FROM call_sessions cs
        JOIN users u ON cs.initiator_id = u.id
        LEFT JOIN projects p ON cs.project_id = p.id
        LEFT JOIN project_members pm ON p.id = pm.project_id
        WHERE cs.status = 'active'
        AND (cs.project_id IS NULL OR pm.user_id = %s)
        ORDER BY cs.started_at DESC
    ''', (user_id,)).fetchall()

    for call_dict in call_sessions:
        # Get participants for this call
        participants = db.execute('''
            SELECT u.id, u.username
            FROM call_participants cp
            JOIN users u ON cp.user_id = u.id
            WHERE cp.call_id = %s AND cp.left_at IS NULL
        ''', (call_dict['id'],)).fetchall()

        call_dict['participant_count'] = len(participants)
        call_dict['participants'] = participants
        calls.append(call_dict)

    return calls


def get_active_calls_for_user(user_id):
    """Get all active calls user is participating in"""
    db = get_db()

    calls = db.execute('''
        SELECT DISTINCT cs.*, u.username as initiator_username
        FROM call_sessions cs
        JOIN users u ON cs.initiator_id = u.id
        JOIN call_participants cp ON cs.id = cp.call_id
        WHERE cp.user_id = %s AND cp.left_at IS NULL AND cs.status = 'active'
        ORDER BY cs.started_at DESC
    ''', (user_id,)).fetchall()

    return calls


def is_user_in_call(call_id, user_id):
    """Check if user is in a specific call"""
    db = get_db()

    count = db.execute('''
        SELECT COUNT(*) FROM call_participants
        WHERE call_id = %s AND user_id = %s AND left_at IS NULL
    ''', (call_id, user_id)).fetchone()[0]

    return count > 0


def clear_stale_calls_for_user(user_id):
    """
    Marks any active call participations for a given user as 'left'
    if the corresponding call session has ended or appears stale.

    This function is more aggressive to handle cases where users
    close their browser without properly leaving a call.
    """
    with transaction() as db:
        # More aggressive clearing: clear ALL stale participations
        cursor = db.execute('''
            UPDATE call_participants
            SET left_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
              AND left_at IS NULL
              AND (
                call_id IN (SELECT id FROM call_sessions WHERE status = 'ended')
                OR
                call_id IN (
                    SELECT cs.id
                    FROM call_sessions cs
                    LEFT JOIN call_participants cp2 ON cs.id = cp2.call_id AND cp2.left_at IS NULL
                    WHERE cs.status = 'active'
                    GROUP BY cs.id
                    HAVING COUNT(cp2.id) = 0
                )
                OR
                -- Clear participations older than 2 minutes (reduced from 3)
                joined_at < (CURRENT_TIMESTAMP - INTERVAL '2 minutes')
              )
        ''', (user_id,))

        return cursor.rowcount if cursor else 0


def force_leave_all_calls(user_id, exclude_call_id=None):
    """
    Force user to leave ALL active calls (optionally excluding one specific call).
    This is used before joining a new call to ensure clean state.

    Returns: number of calls left
    """
    with transaction() as db:
        if exclude_call_id:
            cursor = db.execute('''
                UPDATE call_participants
                SET left_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
                  AND left_at IS NULL
                  AND call_id != %s
            ''', (user_id, exclude_call_id))
        else:
            cursor = db.execute('''
                UPDATE call_participants
                SET left_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
                  AND left_at IS NULL
            ''', (user_id,))

        return cursor.rowcount if cursor else 0


def cleanup_empty_calls():
    """
    End any active call sessions that have no active participants.
    Returns: number of calls ended
    """
    with transaction() as db:
        # Find and end empty active calls
        cursor = db.execute('''
            UPDATE call_sessions
            SET status = 'ended', ended_at = CURRENT_TIMESTAMP
            WHERE status = 'active'
              AND id IN (
                  SELECT cs.id
                  FROM call_sessions cs
                  LEFT JOIN call_participants cp ON cs.id = cp.call_id AND cp.left_at IS NULL
                  WHERE cs.status = 'active'
                  GROUP BY cs.id
                  HAVING COUNT(cp.id) = 0
              )
        ''')

        return cursor.rowcount if cursor else 0


def cleanup_all_stale_calls():
    """
    Comprehensive cleanup of all stale call data.
    Should be run periodically (e.g., every 1-2 minutes) as a background job.

    Returns: dict with cleanup statistics
    """
    with transaction() as db:
        stats = {
            'stale_participants_removed': 0,
            'empty_calls_ended': 0,
            'old_calls_archived': 0
        }

        # 1. Clear stale participations (older than 2 minutes or in ended calls)
        cursor = db.execute('''
            UPDATE call_participants
            SET left_at = CURRENT_TIMESTAMP
            WHERE left_at IS NULL
              AND (
                call_id IN (SELECT id FROM call_sessions WHERE status = 'ended')
                OR joined_at < (CURRENT_TIMESTAMP - INTERVAL '2 minutes')
              )
        ''')
        stats['stale_participants_removed'] = cursor.rowcount if cursor else 0

        # 2. End empty active calls
        cursor = db.execute('''
            UPDATE call_sessions
            SET status = 'ended', ended_at = CURRENT_TIMESTAMP
            WHERE status = 'active'
              AND id NOT IN (
                  SELECT DISTINCT call_id
                  FROM call_participants
                  WHERE left_at IS NULL
              )
        ''')
        stats['empty_calls_ended'] = cursor.rowcount if cursor else 0

        # 3. Archive very old active calls (older than 12 hours - safety measure)
        cursor = db.execute('''
            UPDATE call_sessions
            SET status = 'ended', ended_at = CURRENT_TIMESTAMP
            WHERE status = 'active'
              AND started_at < (CURRENT_TIMESTAMP - INTERVAL '12 hours')
        ''')
        stats['old_calls_archived'] = cursor.rowcount if cursor else 0

        logger.info(f"Cleanup completed: {stats}")
        return stats
