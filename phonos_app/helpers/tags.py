"""Helpers for managing project and job tags."""

from __future__ import annotations

import psycopg2
import sqlite3
from typing import Iterable

from .database import get_db


def get_tag(tag_id: int):
    """Return the tag record for an identifier or ``None`` if missing."""

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    result = db.execute(
        "SELECT id, team_id, name FROM tags WHERE id = %s",
        (tag_id,),
    )
    return result.fetchone()


def create_tag(team_id: int, name: str):
    """Create a new tag for the given team.

    ``None`` is returned when the tag already exists for the requested team.
    """

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    try:
        result = db.execute(
            "INSERT INTO tags (team_id, name) VALUES (%s, %s)",
            (team_id, name),
        )
        db.commit()
        return result.lastrowid
    except psycopg2.IntegrityError:
        return None


def get_tags_for_team(team_id: int) -> Iterable[dict]:
    """Return all tags defined for ``team_id`` ordered alphabetically."""

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    result = db.execute(
        """
        SELECT id, name
        FROM tags
        WHERE team_id = %s
        ORDER BY LOWER(name), name
        """,
        (team_id,),
    )
    return result.fetchall()


# Backwards compatibility: legacy imports used the organization-oriented name.
get_tags_for_organization = get_tags_for_team


def get_tags_for_project(project_id: int) -> Iterable[sqlite3.Row]:
    """Return the tags currently associated with ``project_id``."""

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    result = db.execute(
        """
        SELECT t.id, t.name
        FROM project_tags pt
        JOIN tags t ON t.id = pt.tag_id
        WHERE pt.project_id = %s
        ORDER BY LOWER(t.name), t.name
        """,
        (project_id,),
    )
    return result.fetchall()


def add_tag_to_job(job_id: int, tag_id: int):
    """Associate ``tag_id`` with ``job_id`` if the mapping does not exist."""

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    try:
        db.execute(
            "INSERT INTO job_tags (job_id, tag_id) VALUES (%s, %s)",
            (job_id, tag_id),
        )
        db.commit()
    except psycopg2.IntegrityError:
        pass


def remove_tag_from_job(job_id: int, tag_id: int):
    """Remove the association between ``job_id`` and ``tag_id``."""

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    db.execute(
        "DELETE FROM job_tags WHERE job_id = %s AND tag_id = %s",
        (job_id, tag_id),
    )
    db.commit()


def add_tag_to_project(project_id: int, tag_id: int):
    """Associate ``tag_id`` with ``project_id`` if the mapping does not exist."""

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    try:
        db.execute(
            "INSERT INTO project_tags (project_id, tag_id) VALUES (%s, %s)",
            (project_id, tag_id),
        )
        db.commit()
    except psycopg2.IntegrityError:
        pass


def remove_tag_from_project(project_id: int, tag_id: int):
    """Remove the association between ``tag_id`` and ``project_id``."""

    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper
    db.execute(
        "DELETE FROM project_tags WHERE project_id = %s AND tag_id = %s",
        (project_id, tag_id),
    )
    db.commit()
