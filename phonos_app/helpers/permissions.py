"""Project-specific permissions management helpers."""
from __future__ import annotations

from .database import get_db
from .projects import get_project_assignment


def grant_permission(
    project_id: int,
    user_id: int,
    resource_type: str,
    permission_level: str,
    granted_by: int,
    resource_id: int = None,
) -> dict:
    """Grant a permission to a user for a resource."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        INSERT INTO project_permissions (project_id, user_id, resource_type, resource_id, permission_level, granted_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (project_id, user_id, resource_type, resource_id, permission_level, granted_by),
    )

    permission_id = result.lastrowid
    db.commit()

    return {
        "id": permission_id,
        "project_id": project_id,
        "user_id": user_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "permission_level": permission_level,
        "granted_by": granted_by,
    }


def revoke_permission(permission_id: int) -> bool:
    """Revoke a specific permission."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        DELETE FROM project_permissions
        WHERE id = %s
        """,
        (permission_id,),
    )

    db.commit()
    return result.rowcount > 0


def check_permission(
    user_id: int,
    project_id: int,
    resource_type: str,
    required_level: str,
    resource_id: int = None,
) -> bool:
    """
    Check if a user has a specific permission level for a resource.

    Permission hierarchy: owner > admin > write > read
    """
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    permission_hierarchy = {
        "read": 1,
        "collaborator": 1.5,  # Allows viewing but not modifying
        "write": 2,
        "admin": 3,
        "owner": 4,
    }

    required_level_value = permission_hierarchy.get(required_level, 0)

    # Check for resource-specific permission
    if resource_id:
        result = db.execute(
            """
            SELECT permission_level
            FROM project_permissions
            WHERE user_id = %s AND project_id = %s AND resource_type = %s AND resource_id = %s
            """,
            (user_id, project_id, resource_type, resource_id),
        )
    else:
        result = db.execute(
            """
            SELECT permission_level
            FROM project_permissions
            WHERE user_id = %s AND project_id = %s AND resource_type = %s AND resource_id IS NULL
            """,
            (user_id, project_id, resource_type),
        )

    row = result.fetchone()

    if not row:
        return False

    user_level = row["permission_level"]
    user_level_value = permission_hierarchy.get(user_level, 0)

    return user_level_value >= required_level_value


def get_user_permissions(user_id: int, project_id: int) -> list[dict]:
    """Get all permissions for a user in a project."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT pp.id, pp.project_id, pp.user_id, pp.resource_type, pp.resource_id,
               pp.permission_level, pp.granted_by, pp.created_at, u.username as granted_by_username
        FROM project_permissions pp
        JOIN users u ON pp.granted_by = u.id
        WHERE pp.user_id = %s AND pp.project_id = %s
        ORDER BY pp.created_at DESC
        """,
        (user_id, project_id),
    )

    permissions = []
    for row in result.fetchall():
        permissions.append(dict(row))

    return permissions


def get_resource_permissions(project_id: int, resource_type: str, resource_id: int = None) -> list[dict]:
    """Get all permissions for a specific resource."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    if resource_id:
        result = db.execute(
            """
            SELECT pp.id, pp.project_id, pp.user_id, pp.resource_type, pp.resource_id,
                   pp.permission_level, pp.granted_by, pp.created_at,
                   u.username, gb.username as granted_by_username
            FROM project_permissions pp
            JOIN users u ON pp.user_id = u.id
            JOIN users gb ON pp.granted_by = gb.id
            WHERE pp.project_id = %s AND pp.resource_type = %s AND pp.resource_id = %s
            ORDER BY pp.created_at DESC
            """,
            (project_id, resource_type, resource_id),
        )
    else:
        result = db.execute(
            """
            SELECT pp.id, pp.project_id, pp.user_id, pp.resource_type, pp.resource_id,
                   pp.permission_level, pp.granted_by, pp.created_at,
                   u.username, gb.username as granted_by_username
            FROM project_permissions pp
            JOIN users u ON pp.user_id = u.id
            JOIN users gb ON pp.granted_by = gb.id
            WHERE pp.project_id = %s AND pp.resource_type = %s AND pp.resource_id IS NULL
            ORDER BY pp.created_at DESC
            """,
            (project_id, resource_type),
        )

    permissions = []
    for row in result.fetchall():
        permissions.append(dict(row))

    return permissions


def update_permission_level(permission_id: int, new_level: str) -> bool:
    """Update the permission level of an existing permission."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        UPDATE project_permissions
        SET permission_level = %s
        WHERE id = %s
        """,
        (new_level, permission_id),
    )

    db.commit()
    return result.rowcount > 0


def bulk_grant_permissions(
    project_id: int,
    user_ids: list[int],
    resource_type: str,
    permission_level: str,
    granted_by: int,
    resource_id: int = None,
) -> int:
    """Grant the same permission to multiple users."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    count = 0
    for user_id in user_ids:
        db.execute(
            """
            INSERT INTO project_permissions (project_id, user_id, resource_type, resource_id, permission_level, granted_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (project_id, user_id, resource_type, resource_id, permission_level, granted_by),
        )
        count += 1

    db.commit()
    return count


def check_project_access(project_id: int) -> bool:
    """Check if current user has any access to the project."""
    from flask import session
    
    if "user_id" not in session:
        return False
    
    db = get_db()
    user_id = session["user_id"]
    role = get_project_assignment(db, user_id, project_id)
    
    return role is not None


def check_project_manager(project_id: int) -> bool:
    """Check if current user has manager or owner access to the project."""
    from flask import session
    from config import Config
    
    if "user_id" not in session:
        return False
    
    db = get_db()
    user_id = session["user_id"]
    role = get_project_assignment(db, user_id, project_id)
    
    manager_roles = {Config.PROJECT_ROLE_MANAGER, Config.PROJECT_ROLE_OWNER}
    return role in manager_roles


def require_project_access(f):
    """Decorator to require any project access."""
    from functools import wraps
    from flask import jsonify, session
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        project_id = kwargs.get("project_id")
        if not project_id:
            return jsonify({
                "status": "error",
                "message": "Project ID missing from request URL.",
            }), 400

        if "user_id" not in session:
            return jsonify({"status": "error", "message": "Authentication required."}), 401

        if not check_project_access(project_id):
            return jsonify({
                "status": "error", 
                "message": "You do not have access to this project.",
            }), 403

        return f(*args, **kwargs)

    return decorated_function
