"""Helper functions for collaboration features."""
from __future__ import annotations
import secrets
from datetime import datetime
from .database import get_db

def get_pending_invitations(user_id: int) -> list[dict]:
    """Fetches all pending project invitations for a given user."""
    db = get_db()
    user_email_query = db.execute('SELECT email FROM user_credentials WHERE user_id = %s', (user_id,)).fetchone()
    if not user_email_query:
        return []

    user_email = user_email_query['email']

    invitations = db.execute(
        """
        SELECT
            inv.id,
            inv.token,
            inv.project_id,
            inv.role,
            inv.created_at,
            p.name as project_name,
            u.username as inviter_name
        FROM invitations inv
        JOIN projects p ON inv.project_id = p.id
        JOIN users u ON inv.inviter_id = u.id
        WHERE lower(inv.invitee_email) = lower(%s) AND inv.status = 'pending'
        ORDER BY inv.created_at DESC
        """,
        (user_email,)
    ).fetchall()

    result = []
    for row in invitations:
        d = dict(row)
        if d.get('created_at') and hasattr(d['created_at'], 'isoformat'):
            d['created_at'] = d['created_at'].isoformat()
        result.append(d)
    return result


def get_collaboration_session(session_id: str) -> dict:
    """Get collaboration session by session_id."""
    db = get_db()
    session = db.execute(
        """
        SELECT * FROM collaboration_sessions
        WHERE session_id = %s
        """,
        (session_id,)
    ).fetchone()

    return dict(session) if session else None


def add_participant(session_id: str, user_id: int) -> bool:
    """Add a participant to a collaboration session."""
    db = get_db()

    # Check if participant already exists and is active
    existing = db.execute(
        """
        SELECT id FROM collaboration_participants
        WHERE session_id = %s AND user_id = %s AND left_at IS NULL
        """,
        (session_id, user_id)
    ).fetchone()

    if existing:
        return False  # Already a participant

    # Add new participant
    db.execute(
        """
        INSERT INTO collaboration_participants (session_id, user_id, joined_at)
        VALUES (%s, %s, %s)
        """,
        (session_id, user_id, datetime.utcnow())
    )
    db.commit()

    return True


def remove_participant(session_id: str, user_id: int) -> None:
    """Remove a participant from a collaboration session."""
    db = get_db()

    db.execute(
        """
        UPDATE collaboration_participants
        SET left_at = %s
        WHERE session_id = %s AND user_id = %s AND left_at IS NULL
        """,
        (datetime.utcnow(), session_id, user_id)
    )
    db.commit()


def get_active_participants(session_id: str) -> list[dict]:
    """Get all active participants in a collaboration session."""
    db = get_db()

    participants = db.execute(
        """
        SELECT
            cp.user_id,
            u.username,
            cp.joined_at
        FROM collaboration_participants cp
        JOIN users u ON cp.user_id = u.id
        WHERE cp.session_id = %s AND cp.left_at IS NULL
        ORDER BY cp.joined_at ASC
        """,
        (session_id,)
    ).fetchall()

    return [dict(row) for row in participants]


def create_collaboration_session(resource_type: str, resource_id: int, creator_id: int) -> dict:
    """Create a new collaboration session for a resource."""
    db = get_db()
    session_id = secrets.token_urlsafe(16)

    db.execute(
        """
        INSERT INTO collaboration_sessions (session_id, resource_type, resource_id, creator_id, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (session_id, resource_type, resource_id, creator_id, datetime.utcnow())
    )
    db.commit()

    # Add creator as first participant
    add_participant(session_id, creator_id)

    return {
        'session_id': session_id,
        'resource_type': resource_type,
        'resource_id': resource_id,
        'creator_id': creator_id
    }


def get_active_sessions_for_resource(resource_type: str, resource_id: int) -> list[dict]:
    """Get all active collaboration sessions for a specific resource."""
    db = get_db()

    sessions = db.execute(
        """
        SELECT
            cs.session_id,
            cs.resource_type,
            cs.resource_id,
            cs.creator_id,
            cs.created_at,
            u.username as creator_name
        FROM collaboration_sessions cs
        JOIN users u ON cs.creator_id = u.id
        WHERE cs.resource_type = %s AND cs.resource_id = %s AND cs.ended_at IS NULL
        ORDER BY cs.created_at DESC
        """,
        (resource_type, resource_id)
    ).fetchall()

    return [dict(row) for row in sessions]


def end_collaboration_session(session_id: str) -> bool:
    """End a collaboration session."""
    db = get_db()

    # Check if session exists and is not already ended
    session = db.execute(
        "SELECT id FROM collaboration_sessions WHERE session_id = %s AND ended_at IS NULL",
        (session_id,)
    ).fetchone()

    if not session:
        return False

    # Mark all participants as left
    db.execute(
        """
        UPDATE collaboration_participants
        SET left_at = %s
        WHERE session_id = %s AND left_at IS NULL
        """,
        (datetime.utcnow(), session_id)
    )

    # End the session
    db.execute(
        """
        UPDATE collaboration_sessions
        SET ended_at = %s
        WHERE session_id = %s
        """,
        (datetime.utcnow(), session_id)
    )
    db.commit()

    return True


def get_user_active_sessions(user_id: int) -> list[dict]:
    """Get all active collaboration sessions for a user."""
    db = get_db()

    sessions = db.execute(
        """
        SELECT
            cs.session_id,
            cs.resource_type,
            cs.resource_id,
            cs.creator_id,
            cs.created_at,
            u.username as creator_name
        FROM collaboration_sessions cs
        JOIN collaboration_participants cp ON cs.session_id = cp.session_id
        JOIN users u ON cs.creator_id = u.id
        WHERE cp.user_id = %s AND cp.left_at IS NULL AND cs.ended_at IS NULL
        ORDER BY cs.created_at DESC
        """,
        (user_id,)
    ).fetchall()

    return [dict(row) for row in sessions]