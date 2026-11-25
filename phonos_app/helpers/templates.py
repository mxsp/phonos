"""Job template management helpers."""
from __future__ import annotations

import json
import secrets
from typing import Any

from .database import get_db


def generate_template_id() -> str:
    """Generate a unique template ID."""
    return f"template_{secrets.token_urlsafe(16)}"


def create_template(
    creator_id: int,
    name: str,
    model: str,
    config: dict[str, Any],
    description: str = None,
    project_id: int = None,
    team_id: int = None,
    is_public: bool = False,
) -> dict:
    """Create a new job template."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    template_id = generate_template_id()
    config_json = json.dumps(config)

    db.execute(
        """
        INSERT INTO job_templates (
            template_id, creator_id, project_id, team_id, name, description,
            model, config, is_public, use_count
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
        """,
        (
            template_id,
            creator_id,
            project_id,
            team_id,
            name,
            description,
            model,
            config_json,
            int(is_public),
        ),
    )

    db.commit()

    return {
        "template_id": template_id,
        "creator_id": creator_id,
        "project_id": project_id,
        "team_id": team_id,
        "name": name,
        "description": description,
        "model": model,
        "config": config,
        "is_public": is_public,
        "use_count": 0,
    }


def get_template(template_id: str) -> dict:
    """Get template details by template_id."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, template_id, creator_id, project_id, team_id, name, description,
               model, config, is_public, use_count, created_at, updated_at
        FROM job_templates
        WHERE template_id = %s
        """,
        (template_id,),
    )

    row = result.fetchone()
    if not row:
        return None

    template = dict(row)
    template["config"] = json.loads(template["config"])
    template["is_public"] = bool(template["is_public"])

    return template


def get_user_templates(user_id: int, include_public: bool = True) -> list[dict]:
    """Get all templates created by a user, optionally including public templates."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    if include_public:
        result = db.execute(
            """
            SELECT id, template_id, creator_id, project_id, team_id, name, description,
                   model, config, is_public, use_count, created_at, updated_at
            FROM job_templates
            WHERE creator_id = %s OR is_public = 1
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
    else:
        result = db.execute(
            """
            SELECT id, template_id, creator_id, project_id, team_id, name, description,
                   model, config, is_public, use_count, created_at, updated_at
            FROM job_templates
            WHERE creator_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,),
        )

    templates = []
    for row in result.fetchall():
        template = dict(row)
        template["config"] = json.loads(template["config"])
        template["is_public"] = bool(template["is_public"])
        templates.append(template)

    return templates


def get_project_templates(project_id: int) -> list[dict]:
    """Get all templates for a specific project."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, template_id, creator_id, project_id, team_id, name, description,
               model, config, is_public, use_count, created_at, updated_at
        FROM job_templates
        WHERE project_id = %s OR is_public = 1
        ORDER BY created_at DESC
        """,
        (project_id,),
    )

    templates = []
    for row in result.fetchall():
        template = dict(row)
        template["config"] = json.loads(template["config"])
        template["is_public"] = bool(template["is_public"])
        templates.append(template)

    return templates


def get_team_templates(team_id: int) -> list[dict]:
    """Get all templates for a specific team."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        SELECT id, template_id, creator_id, project_id, team_id, name, description,
               model, config, is_public, use_count, created_at, updated_at
        FROM job_templates
        WHERE team_id = %s OR is_public = 1
        ORDER BY created_at DESC
        """,
        (team_id,),
    )

    templates = []
    for row in result.fetchall():
        template = dict(row)
        template["config"] = json.loads(template["config"])
        template["is_public"] = bool(template["is_public"])
        templates.append(template)

    return templates


def increment_template_usage(template_id: str) -> None:
    """Increment the usage count for a template."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    db.execute(
        """
        UPDATE job_templates
        SET use_count = use_count + 1, updated_at = CURRENT_TIMESTAMP
        WHERE template_id = %s
        """,
        (template_id,),
    )

    db.commit()


def update_template(
    template_id: str,
    name: str = None,
    description: str = None,
    config: dict = None,
    is_public: bool = None,
) -> bool:
    """Update template details."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    updates = []
    params = []

    if name is not None:
        updates.append("name = %s")
        params.append(name)

    if description is not None:
        updates.append("description = %s")
        params.append(description)

    if config is not None:
        updates.append("config = %s")
        params.append(json.dumps(config))

    if is_public is not None:
        updates.append("is_public = %s")
        params.append(int(is_public))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(template_id)

    query = f"""
        UPDATE job_templates
        SET {', '.join(updates)}
        WHERE template_id = %s
    """

    result = db.execute(query, params)
    db.commit()

    return result.rowcount > 0


def delete_template(template_id: str) -> bool:
    """Delete a template."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute(
        """
        DELETE FROM job_templates
        WHERE template_id = %s
        """,
        (template_id,),
    )

    db.commit()
    return result.rowcount > 0
