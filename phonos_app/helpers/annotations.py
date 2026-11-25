"""Resource annotations management helpers."""
from __future__ import annotations

import json

from .database import get_db


def create_annotation(
    resource_type: str,
    resource_id: int,
    user_id: int,
    content: str,
    position_data: dict = None,
) -> dict:
    """Create an annotation on a resource."""
    db = get_db()
    cursor = db.cursor()

    position_json = json.dumps(position_data) if position_data else None

    result = db.execute(
        """
        INSERT INTO resource_annotations (resource_type, resource_id, user_id, content, position_data)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (resource_type, resource_id, user_id, content, position_json),
    )

    annotation_id = result.lastrowid
    db.commit()

    return {
        "id": annotation_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "user_id": user_id,
        "content": content,
        "position_data": position_data,
    }


def get_annotation(annotation_id: int) -> dict:
    """Get annotation by ID."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, resource_type, resource_id, user_id, content, position_data, created_at, updated_at
        FROM resource_annotations
        WHERE id = %s
        """,
        (annotation_id,),
    )

    row = result.fetchone()
    if not row:
        return None

    annotation = dict(row)
    if annotation["position_data"]:
        annotation["position_data"] = json.loads(annotation["position_data"])

    return annotation


def get_resource_annotations(resource_type: str, resource_id: int) -> list[dict]:
    """Get all annotations for a specific resource."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT a.id, a.resource_type, a.resource_id, a.user_id, a.content,
               a.position_data, a.created_at, a.updated_at, u.username
        FROM resource_annotations a
        JOIN users u ON a.user_id = u.id
        WHERE a.resource_type = %s AND a.resource_id = %s
        ORDER BY a.created_at DESC
        """,
        (resource_type, resource_id),
    )

    annotations = []
    for row in result.fetchall():
        annotation = dict(row)
        if annotation["position_data"]:
            annotation["position_data"] = json.loads(annotation["position_data"])
        annotations.append(annotation)

    return annotations


def update_annotation(annotation_id: int, content: str) -> bool:
    """Update annotation content."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        UPDATE resource_annotations
        SET content = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (content, annotation_id),
    )

    db.commit()
    return result.rowcount > 0


def delete_annotation(annotation_id: int) -> bool:
    """Delete an annotation."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        DELETE FROM resource_annotations
        WHERE id = %s
        """,
        (annotation_id,),
    )

    db.commit()
    return result.rowcount > 0


def get_user_annotations(user_id: int, limit: int = 50) -> list[dict]:
    """Get recent annotations created by a user."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, resource_type, resource_id, user_id, content, position_data, created_at, updated_at
        FROM resource_annotations
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (user_id, limit),
    )

    annotations = []
    for row in result.fetchall():
        annotation = dict(row)
        if annotation["position_data"]:
            annotation["position_data"] = json.loads(annotation["position_data"])
        annotations.append(annotation)

    return annotations
