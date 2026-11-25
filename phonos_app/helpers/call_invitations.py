"""
Helper functions for managing call invitations
"""
import logging
from datetime import datetime

from ..helpers.database import get_db, transaction

logger = logging.getLogger(__name__)


def create_call_invitation(call_id, inviter_id, invitee_id):
    """Create a call invitation"""
    with transaction() as db:
        cursor = db.execute('''
            INSERT INTO call_invitations (call_id, inviter_id, invitee_id, status)
            VALUES (%s, %s, %s, 'pending')
        ''', (call_id, inviter_id, invitee_id))

        result = cursor.fetchone()


        invitation_id = result["id"] if result else None
        return invitation_id


def get_pending_invitations(user_id):
    """Get all pending call invitations for a user"""
    db = get_db()

    invitations = db.execute('''
        SELECT
            ci.*,
            u.username as inviter_username,
            cs.call_type,
            cs.status as call_status
        FROM call_invitations ci
        JOIN users u ON ci.inviter_id = u.id
        JOIN call_sessions cs ON ci.call_id = cs.id
        WHERE ci.invitee_id = %s
          AND ci.status = 'pending'
          AND cs.status = 'active'
        ORDER BY ci.created_at DESC
    ''', (user_id,)).fetchall()

    return invitations


def respond_to_invitation(invitation_id, status):
    """Respond to a call invitation (accept/decline)"""
    with transaction() as db:
        db.execute('''
            UPDATE call_invitations
            SET status = %s, responded_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (status, invitation_id))
        return True


def get_invitation_by_id(invitation_id):
    """Get invitation details"""
    db = get_db()

    invitation = db.execute('''
        SELECT * FROM call_invitations WHERE id = %s
    ''', (invitation_id,)).fetchone()

    return invitation if invitation else None


def cancel_pending_invitations(call_id):
    """Cancel all pending invitations for a call"""
    with transaction() as db:
        db.execute('''
            UPDATE call_invitations
            SET status = 'cancelled'
            WHERE call_id = %s AND status = 'pending'
        ''', (call_id,))
        return True
