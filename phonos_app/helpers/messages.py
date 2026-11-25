"""Helper functions for project messages and chat."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .database import get_db
from .security import sanitize_html


def serialize_datetime(value):
    """Convert datetime to ISO format string for JSON serialization."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def create_project_message(
    project_id: int,
    sender_id: int,
    content: str,
    message_type: str = "text",
    file_id: Optional[int] = None,
    parent_message_id: Optional[int] = None,
) -> dict:
    """Create a new message in a project."""
    db = get_db()

    # Sanitize content for XSS protection
    sanitized_content = sanitize_html(content)

    result = db.execute(
        """
        INSERT INTO project_messages (
            project_id, sender_id, content, message_type,
            file_id, parent_message_id
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            project_id,
            sender_id,
            sanitized_content,
            message_type,
            file_id,
            parent_message_id,
        ),
    )

    row = result.fetchone()
    message_id = row['id'] if row else None
    db.commit()

    return get_project_message(message_id) if message_id else None


def get_project_message(message_id: int) -> dict:
    """Get a single project message by ID."""
    db = get_db()

    result = db.execute(
        """
        SELECT
            pm.id, pm.project_id, pm.sender_id, pm.content,
            pm.message_type, pm.file_id, pm.gemini_response,
            pm.parent_message_id, pm.created_at,
            u.username as sender_username,
            u.profile_picture_url as sender_picture,
            f.name as file_name
        FROM project_messages pm
        JOIN users u ON pm.sender_id = u.id
        LEFT JOIN files f ON pm.file_id = f.id
        WHERE pm.id = %s
        """,
        (message_id,),
    )

    row = result.fetchone()
    if not row:
        return None

    message = dict(row)

    # Serialize datetime fields for JSON compatibility
    message['created_at'] = serialize_datetime(message.get('created_at'))

    # Get attachments
    result = db.execute(
        """
        SELECT id, file_name, file_path, file_type, file_size, thumbnail_path, uploaded_at
        FROM message_attachments
        WHERE message_id = %s
        ORDER BY uploaded_at ASC
        """,
        (message_id,),
    )

    attachments = []
    for att in result.fetchall():
        att_dict = dict(att)
        att_dict['uploaded_at'] = serialize_datetime(att_dict.get('uploaded_at'))
        attachments.append(att_dict)
    message['attachments'] = attachments

    # Get reactions
    result = db.execute(
        """
        SELECT mr.reaction, COUNT(*) as count,
               STRING_AGG(u.username, ',') as users
        FROM message_reactions mr
        JOIN users u ON mr.user_id = u.id
        WHERE mr.message_id = %s
        GROUP BY mr.reaction
        """,
        (message_id,),
    )

    message['reactions'] = [dict(r) for r in result.fetchall()]

    return message


def get_project_messages(
    project_id: int, limit: int = 50, before_id: Optional[int] = None
) -> list[dict]:
    """Get messages for a project, paginated."""
    db = get_db()

    if before_id:
        result = db.execute(
            """
            SELECT
                pm.id, pm.project_id, pm.sender_id, pm.content,
                pm.message_type, pm.file_id, pm.gemini_response,
                pm.parent_message_id, pm.created_at,
                u.username as sender_username,
                u.profile_picture_url as sender_picture,
                f.name as file_name
            FROM project_messages pm
            JOIN users u ON pm.sender_id = u.id
            LEFT JOIN files f ON pm.file_id = f.id
            WHERE pm.project_id = %s AND pm.id < %s
            ORDER BY pm.created_at DESC
            LIMIT %s
            """,
            (project_id, before_id, limit),
        )
    else:
        result = db.execute(
            """
            SELECT
                pm.id, pm.project_id, pm.sender_id, pm.content,
                pm.message_type, pm.file_id, pm.gemini_response,
                pm.parent_message_id, pm.created_at,
                u.username as sender_username,
                u.profile_picture_url as sender_picture,
                f.name as file_name
            FROM project_messages pm
            JOIN users u ON pm.sender_id = u.id
            LEFT JOIN files f ON pm.file_id = f.id
            WHERE pm.project_id = %s
            ORDER BY pm.created_at DESC
            LIMIT %s
            """,
            (project_id, limit),
        )

    messages = []
    for row in result.fetchall():
        message = dict(row)
        message_id = message['id']

        # Serialize datetime fields for JSON compatibility
        message['created_at'] = serialize_datetime(message.get('created_at'))

        # Get attachments for this message
        att_result = db.execute(
            """
            SELECT id, file_name, file_path, file_type, file_size, thumbnail_path, uploaded_at
            FROM message_attachments
            WHERE message_id = %s
            ORDER BY uploaded_at ASC
            """,
            (message_id,),
        )
        attachments = []
        for att in att_result.fetchall():
            att_dict = dict(att)
            att_dict['uploaded_at'] = serialize_datetime(att_dict.get('uploaded_at'))
            attachments.append(att_dict)
        message['attachments'] = attachments

        # Get reactions for this message
        react_result = db.execute(
            """
            SELECT mr.reaction, COUNT(*) as count,
                   STRING_AGG(u.username, ',') as users
            FROM message_reactions mr
            JOIN users u ON mr.user_id = u.id
            WHERE mr.message_id = %s
            GROUP BY mr.reaction
            """,
            (message_id,),
        )
        message['reactions'] = [dict(r) for r in react_result.fetchall()]

        messages.append(message)

    # Reverse to get chronological order
    return list(reversed(messages))


def delete_project_message(message_id: int, user_id: int) -> bool:
    """Delete a project message (only sender can delete)."""
    db = get_db()

    result = db.execute(
        """
        DELETE FROM project_messages
        WHERE id = %s AND sender_id = %s
        RETURNING id
        """,
        (message_id, user_id),
    )

    deleted = result.fetchone() is not None
    db.commit()
    return deleted


def get_message_count(project_id: int) -> int:
    """Get total message count for a project."""
    db = get_db()

    result = db.execute(
        """
        SELECT COUNT(*) as count
        FROM project_messages
        WHERE project_id = %s
        """,
        (project_id,),
    )

    row = result.fetchone()
    return row["count"] if row else 0


def add_message_reaction(message_id: int, user_id: int, reaction: str) -> dict:
    """Add a reaction to a message."""
    db = get_db()

    try:
        db.execute(
            """
            INSERT INTO message_reactions (message_id, user_id, reaction)
            VALUES (%s, %s, %s)
            ON CONFLICT (message_id, user_id, reaction) DO NOTHING
            """,
            (message_id, user_id, reaction),
        )
        db.commit()
    except Exception:
        pass

    return get_message_reactions(message_id)


def remove_message_reaction(message_id: int, user_id: int, reaction: str) -> dict:
    """Remove a reaction from a message."""
    db = get_db()

    db.execute(
        """
        DELETE FROM message_reactions
        WHERE message_id = %s AND user_id = %s AND reaction = %s
        """,
        (message_id, user_id, reaction),
    )

    db.commit()
    return get_message_reactions(message_id)


def get_message_reactions(message_id: int) -> dict:
    """Get all reactions for a message grouped by reaction type."""
    db = get_db()

    result = db.execute(
        """
        SELECT mr.reaction, COUNT(*) as count,
               STRING_AGG(u.username, ',') as users,
               STRING_AGG(mr.user_id::text, ',') as user_ids
        FROM message_reactions mr
        JOIN users u ON mr.user_id = u.id
        WHERE mr.message_id = %s
        GROUP BY mr.reaction
        """,
        (message_id,),
    )

    reactions = [dict(r) for r in result.fetchall()]
    return {'message_id': message_id, 'reactions': reactions}


def edit_project_message(message_id: int, user_id: int, new_content: str) -> dict:
    """Edit a message (only sender can edit)."""
    db = get_db()

    # Sanitize edited content for XSS protection
    sanitized_content = sanitize_html(new_content)

    result = db.execute(
        """
        UPDATE project_messages
        SET content = %s
        WHERE id = %s AND sender_id = %s
        RETURNING id
        """,
        (sanitized_content, message_id, user_id),
    )

    updated = result.fetchone() is not None
    db.commit()

    if updated:
        return get_project_message(message_id)
    return None


def pin_project_message(message_id: int, project_id: int) -> bool:
    """Pin a message in a project."""
    db = get_db()

    # First, check if message belongs to project
    result = db.execute(
        """
        SELECT id FROM project_messages
        WHERE id = %s AND project_id = %s
        """,
        (message_id, project_id),
    )

    if not result.fetchone():
        return False

    # Update or create pinned message marker
    db.execute(
        """
        INSERT INTO pinned_messages (project_id, message_id, pinned_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (project_id) DO UPDATE SET message_id = EXCLUDED.message_id, pinned_at = CURRENT_TIMESTAMP
        """,
        (project_id, message_id),
    )

    db.commit()
    return True


def unpin_project_message(message_id: int, project_id: int) -> bool:
    """Unpin a message in a project."""
    db = get_db()

    result = db.execute(
        """
        DELETE FROM pinned_messages
        WHERE message_id = %s AND project_id = %s
        RETURNING id
        """,
        (message_id, project_id),
    )

    deleted = result.fetchone() is not None
    db.commit()
    return deleted


def get_pinned_messages(project_id: int) -> list[dict]:
    """Get all pinned messages for a project."""
    db = get_db()

    result = db.execute(
        """
        SELECT pm.id, pm.project_id, pm.sender_id, pm.content,
               pm.message_type, pm.file_id, pm.gemini_response,
               pm.parent_message_id, pm.created_at,
               u.username as sender_username,
               u.profile_picture_url as sender_picture,
               f.name as file_name,
               pn.pinned_at
        FROM pinned_messages pn
        JOIN project_messages pm ON pn.message_id = pm.id
        JOIN users u ON pm.sender_id = u.id
        LEFT JOIN files f ON pm.file_id = f.id
        WHERE pn.project_id = %s
        ORDER BY pn.pinned_at DESC
        """,
        (project_id,),
    )

    messages = []
    for row in result.fetchall():
        message = dict(row)
        message_id = message['id']

        # Serialize datetime fields for JSON compatibility
        message['created_at'] = serialize_datetime(message.get('created_at'))
        message['pinned_at'] = serialize_datetime(message.get('pinned_at'))

        # Get attachments
        att_result = db.execute(
            """
            SELECT id, file_name, file_path, file_type, file_size, thumbnail_path, uploaded_at
            FROM message_attachments
            WHERE message_id = %s
            """,
            (message_id,),
        )
        attachments = []
        for att in att_result.fetchall():
            att_dict = dict(att)
            att_dict['uploaded_at'] = serialize_datetime(att_dict.get('uploaded_at'))
            attachments.append(att_dict)
        message['attachments'] = attachments

        # Get reactions
        react_result = db.execute(
            """
            SELECT mr.reaction, COUNT(*) as count,
                   STRING_AGG(u.username, ',') as users
            FROM message_reactions mr
            JOIN users u ON mr.user_id = u.id
            WHERE mr.message_id = %s
            GROUP BY mr.reaction
            """,
            (message_id,),
        )
        message['reactions'] = [dict(r) for r in react_result.fetchall()]

        messages.append(message)

    return messages
