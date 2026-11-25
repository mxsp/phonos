"""Global search functionality helpers."""
from __future__ import annotations

import re
from typing import Any

from .database import get_db


def normalize_search_query(query: str) -> str:
    """Normalize search query for better matching."""
    # Remove extra whitespace
    query = re.sub(r'\s+', ' ', query.strip())
    # Convert to lowercase
    return query.lower()


def search_projects(user_id: int, query: str, limit: int = 50) -> list[dict]:
    """Search user's projects by name or description."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    search_pattern = f"%{query}%"

    result = db.execute(
        """
        SELECT DISTINCT p.id, p.name, p.description, p.created_at, p.updated_at
        FROM projects p
        LEFT JOIN project_assignments pa ON p.id = pa.project_id
        WHERE (pa.user_id = %s OR p.organization_id IN (
            SELECT team_id FROM team_members WHERE user_id = %s
        ))
        AND (p.name LIKE %s OR p.description LIKE %s)
        ORDER BY p.updated_at DESC
        LIMIT %s
        """,
        (user_id, user_id, search_pattern, search_pattern, limit),
    )

    results = []
    for row in result.fetchall():
        project = dict(row)
        project["result_type"] = "project"
        results.append(project)

    return results


def search_jobs(user_id: int, query: str, limit: int = 50) -> list[dict]:
    """Search user's jobs by job_id, model, or sequence."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    search_pattern = f"%{query}%"

    result = db.execute(
        """
        SELECT j.id, j.job_id, j.model, j.status, j.sequence, j.created_at, p.name as project_name
        FROM jobs j
        JOIN projects p ON j.project_id = p.id
        LEFT JOIN project_assignments pa ON p.id = pa.project_id
        WHERE (pa.user_id = %s OR j.creator_id = %s)
        AND (j.job_id LIKE %s OR j.model LIKE %s OR j.sequence LIKE %s)
        ORDER BY j.created_at DESC
        LIMIT %s
        """,
        (user_id, user_id, search_pattern, search_pattern, search_pattern, limit),
    )

    results = []
    for row in result.fetchall():
        job = dict(row)
        job["result_type"] = "job"
        results.append(job)

    return results


def search_files(user_id: int, query: str, limit: int = 50) -> list[dict]:
    """Search user's files by name or notes."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    search_pattern = f"%{query}%"

    result = db.execute(
        """
        SELECT f.id, f.name, f.size, f.notes, f.created_at, p.name as project_name
        FROM files f
        JOIN projects p ON f.project_id = p.id
        LEFT JOIN project_assignments pa ON p.id = pa.project_id
        WHERE (pa.user_id = %s OR f.creator_id = %s)
        AND (f.name LIKE %s OR f.notes LIKE %s)
        ORDER BY f.created_at DESC
        LIMIT %s
        """,
        (user_id, user_id, search_pattern, search_pattern, limit),
    )

    results = []
    for row in result.fetchall():
        file = dict(row)
        file["result_type"] = "file"
        results.append(file)

    return results


def search_users(query: str, limit: int = 20) -> list[dict]:
    """Search users by username or legal name."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    search_pattern = f"%{query}%"

    result = db.execute(
        """
        SELECT id, username, legal_name, profile_picture_url, created_at
        FROM users
        WHERE username LIKE %s OR legal_name LIKE %s
        ORDER BY username
        LIMIT %s
        """,
        (search_pattern, search_pattern, limit),
    )

    results = []
    for row in result.fetchall():
        user = dict(row)
        user["result_type"] = "user"
        results.append(user)

    return results


def search_teams(user_id: int, query: str, limit: int = 20) -> list[dict]:
    """Search teams that user is a member of."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    search_pattern = f"%{query}%"

    result = db.execute(
        """
        SELECT DISTINCT t.id, t.name, t.slug, t.description, t.created_at
        FROM teams t
        JOIN team_members tm ON t.id = tm.team_id
        WHERE tm.user_id = %s
        AND (t.name LIKE %s OR t.description LIKE %s)
        ORDER BY t.name
        LIMIT %s
        """,
        (user_id, search_pattern, search_pattern, limit),
    )

    results = []
    for row in result.fetchall():
        team = dict(row)
        team["result_type"] = "team"
        results.append(team)

    return results



def search_templates(user_id: int, query: str, limit: int = 50) -> list[dict]:
    """Search job templates by name or description."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    search_pattern = f"%{query}%"

    result = db.execute(
        """
        SELECT id, template_id, name, description, model, is_public, use_count, created_at
        FROM job_templates
        WHERE (creator_id = %s OR is_public = 1)
        AND (name LIKE %s OR description LIKE %s)
        ORDER BY use_count DESC, created_at DESC
        LIMIT %s
        """,
        (user_id, search_pattern, search_pattern, limit),
    )

    results = []
    for row in result.fetchall():
        template = dict(row)
        template["result_type"] = "template"
        template["is_public"] = bool(template["is_public"])
        results.append(template)

    return results


def global_search(user_id: int, query: str, categories: list[str] | None = None) -> dict[str, Any]:
    """
    Perform a global search across all resource types.

    Args:
        user_id: The user performing the search
        query: Search query string
        categories: Optional list of categories to search (projects, jobs, files, users, teams, templates)

    Returns:
        Dictionary with search results grouped by category
    """
    normalized_query = normalize_search_query(query)

    results = {
        "query": query,
        "projects": [],
        "jobs": [],
        "files": [],
        "users": [],
        "teams": [],
        "templates": [],
        "total_results": 0,
    }

    # If no categories specified, search all
    if not categories:
        categories = ["projects", "jobs", "files", "users", "teams", "templates"]

    if "projects" in categories:
        results["projects"] = search_projects(user_id, normalized_query, limit=10)

    if "jobs" in categories:
        results["jobs"] = search_jobs(user_id, normalized_query, limit=10)

    if "files" in categories:
        results["files"] = search_files(user_id, normalized_query, limit=10)

    if "users" in categories:
        results["users"] = search_users(normalized_query, limit=10)

    if "teams" in categories:
        results["teams"] = search_teams(user_id, normalized_query, limit=10)

    if "templates" in categories:
        results["templates"] = search_templates(user_id, normalized_query, limit=10)

    # Calculate total results
    results["total_results"] = sum([
        len(results["projects"]),
        len(results["jobs"]),
        len(results["files"]),
        len(results["users"]),
        len(results["teams"]),
        len(results["templates"]),
    ])

    return results


def search_documentation(query: str, limit: int = 20) -> list[dict]:
    """Search documentation pages."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    search_pattern = f"%{query}%"

    result = db.execute(
        """
        SELECT id, slug, title, category, view_count, created_at, updated_at
        FROM documentation_pages
        WHERE title LIKE %s OR content LIKE %s OR search_vector LIKE %s
        ORDER BY view_count DESC, updated_at DESC
        LIMIT %s
        """,
        (search_pattern, search_pattern, search_pattern, limit),
    )

    results = []
    for row in result.fetchall():
        doc = dict(row)
        doc["result_type"] = "documentation"
        results.append(doc)

    return results
