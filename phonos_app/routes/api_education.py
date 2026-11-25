"""
Phonos Education API Routes

Surfaces for the Education project type (a third project kind that turns the
Notebook IDE into a WebWork-style assignment platform).

Phase 1 (this file):
  - Roster management (list/remove students, list pending invitations)
  - Education-project metadata (is this an education project? am I teacher or student?)
  - Assignment list stub (returns published assignments; full CRUD in Phase 2)

Role model:
  - Teacher = ``Project Manager`` (owner, blue, full administrative power)
  - Student = ``Student`` (red, solves assignments)

All endpoints are scoped to a single ``project_id`` and reuse the existing
``project_role_required`` decorator so access control stays consistent with
the rest of the platform.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request, session

from phonos_app.helpers.auth import login_required, project_role_required
from phonos_app.helpers.database import get_db
from phonos_app.helpers.education import (
    auto_grade,
    encrypt_answer,
    sanitize_problem_config_for_student,
)
from phonos_app.helpers.projects import (
    PROJECT_ROLE_MANAGER,
    PROJECT_ROLE_STUDENT,
    externalize_project_role,
    get_project_assignment,
)

try:
    from phonos_app.helpers.security import csrf_protect
except ImportError:  # pragma: no cover - security helpers always present in app
    def csrf_protect(f):
        return f

logger = logging.getLogger(__name__)

education_bp = Blueprint("education", __name__, url_prefix="/api/education")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_id() -> int:
    uid = session.get("user_id")
    if not uid:
        raise RuntimeError("Not authenticated")
    return uid


def _iso(value) -> str | None:
    """Serialise a timestamp that may be a datetime OR a SQLite string."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _get_project_type(db, project_id: int) -> str:
    row = db.execute(
        "SELECT project_type FROM projects WHERE id = %s", (project_id,)
    ).fetchone()
    if not row:
        return ""
    return row["project_type"] if "project_type" in row.keys() else "legacy"


def _resolve_assignment_project_id(db, assignment_id: int):
    """Return the project_id for an assignment, or None if not found."""
    row = db.execute(
        "SELECT project_id FROM education_assignments WHERE id = %s",
        (assignment_id,),
    ).fetchone()
    return row["project_id"] if row else None


def _check_assignment_role(assignment_id, allowed_roles):
    """Resolve the project from an assignment and check the user's role.

    Returns (db, project_id, role) on success, or (response, None, None) on
    failure (response is a Flask jsonify tuple ready to return).
    """
    db = get_db()
    _ensure_education_tables(db)
    project_id = _resolve_assignment_project_id(db, assignment_id)
    if not project_id:
        return (jsonify({"status": "error", "message": "Assignment not found."}), 404), None, None
    user_id = _user_id()
    role = get_project_assignment(db, user_id, project_id)
    if not role or role not in allowed_roles:
        return (jsonify({"status": "error", "message": "You do not have the required permissions for this assignment."}), 403), None, None
    return db, project_id, role


def _is_teacher(role: str) -> bool:
    return role == PROJECT_ROLE_MANAGER


def _is_student(role: str) -> bool:
    return role == PROJECT_ROLE_STUDENT


# ---------------------------------------------------------------------------
# Education-specific user settings (separate from the general /api/settings
# flag — this endpoint holds the teacher defaults used when creating new
# assignments: default late policy, default time limit).
# ---------------------------------------------------------------------------

EDU_ALLOWED_LATE_POLICIES = {'none', 'penalty', 'reject'}


def _ensure_edu_settings_table(db):
    db.execute(
        '''CREATE TABLE IF NOT EXISTS user_education_settings (
               id SERIAL PRIMARY KEY,
               user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
               education_enabled INTEGER NOT NULL DEFAULT 0,
               default_late_policy TEXT NOT NULL DEFAULT 'none',
               default_time_limit_minutes INTEGER,
               default_max_submissions_per_problem INTEGER NOT NULL DEFAULT 1,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )'''
    )
    # Ensure columns exist on pre-existing rows (older installs).
    cols = {c['column_name'] for c in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_education_settings'"
    ).fetchall()} if not isinstance(db.execute("SELECT 1").__class__.__name__, str) else set()
    try:
        existing = db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_education_settings'"
        ).fetchall()
        cols = {r['column_name'] for r in existing}
    except Exception:
        cols = set()
    if 'default_late_policy' not in cols:
        try:
            db.execute("ALTER TABLE user_education_settings ADD COLUMN default_late_policy TEXT NOT NULL DEFAULT 'none'")
        except Exception:
            pass
    if 'default_time_limit_minutes' not in cols:
        try:
            db.execute("ALTER TABLE user_education_settings ADD COLUMN default_time_limit_minutes INTEGER")
        except Exception:
            pass
    if 'default_max_submissions_per_problem' not in cols:
        try:
            db.execute("ALTER TABLE user_education_settings ADD COLUMN default_max_submissions_per_problem INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
    db.commit()


@education_bp.route("/settings", methods=["GET"])
@login_required
def get_edu_settings():
    """Return the current user's education-specific defaults."""
    db = get_db()
    _ensure_edu_settings_table(db)
    row = db.execute(
        "SELECT default_late_policy, default_time_limit_minutes, default_max_submissions_per_problem "
        "FROM user_education_settings WHERE user_id = %s",
        (_user_id(),),
    ).fetchone()
    if not row:
        return jsonify({"default_late_policy": "none", "default_time_limit_minutes": None, "default_max_submissions_per_problem": 1})
    return jsonify({
        "default_late_policy": row["default_late_policy"] or "none",
        "default_time_limit_minutes": row["default_time_limit_minutes"],
        "default_max_submissions_per_problem": row["default_max_submissions_per_problem"] or 1,
    })


@education_bp.route("/settings", methods=["PUT"])
@login_required
@csrf_protect
def put_edu_settings():
    """Save the user's education-specific defaults (late policy, time limit)."""
    db = get_db()
    _ensure_edu_settings_table(db)
    data = request.get_json(silent=True) or {}
    late_policy = (data.get("default_late_policy") or "none").lower()
    if late_policy not in EDU_ALLOWED_LATE_POLICIES:
        late_policy = "none"
    raw_tlm = data.get("default_time_limit_minutes")
    try:
        tlm = int(raw_tlm) if raw_tlm is not None else None
    except (TypeError, ValueError):
        tlm = None
    try:
        max_subs = max(1, int(data.get("default_max_submissions_per_problem", 1)))
    except (TypeError, ValueError):
        max_subs = 1
    uid = _user_id()
    db.execute(
        '''INSERT INTO user_education_settings (user_id, default_late_policy, default_time_limit_minutes, default_max_submissions_per_problem)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT(user_id) DO UPDATE SET
               default_late_policy = excluded.default_late_policy,
               default_time_limit_minutes = excluded.default_time_limit_minutes,
               default_max_submissions_per_problem = excluded.default_max_submissions_per_problem''',
        (uid, late_policy, tlm, max_subs),
    )
    db.commit()
    return jsonify({
        "status": "success",
        "default_late_policy": late_policy,
        "default_time_limit_minutes": tlm,
        "default_max_submissions_per_problem": max_subs,
    })


# ---------------------------------------------------------------------------
# Project-scoped metadata
# ---------------------------------------------------------------------------

@education_bp.route("/projects/<int:project_id>/info", methods=["GET"])
@login_required
def education_project_info(project_id):
    """Return education-specific metadata for a project.

    Tells the frontend whether this is an education project and what the
    current user's role is (teacher=blue / student=red), so the UI can apply
    the colour scheme and lock down student cells.
    """
    db = get_db()
    user_id = _user_id()
    project_type = _get_project_type(db, project_id)
    role = get_project_assignment(db, user_id, project_id)

    if not role:
        return jsonify({"status": "error", "message": "Access denied."}), 403

    is_education = project_type == "education"
    return jsonify({
        "status": "success",
        "is_education": is_education,
        "project_type": project_type,
        "role": externalize_project_role(role),
        "role_canonical": role,
        "is_teacher": is_education and _is_teacher(role),
        "is_student": is_education and _is_student(role),
        # Colour hints for the frontend (matched to the design system)
        "color": "#137CBD" if (is_education and _is_teacher(role)) else
                 "#C53030" if (is_education and _is_student(role)) else None,
    })


# ---------------------------------------------------------------------------
# Cross-project dashboards ("My Classes" for teachers, "My Assignments" for
# students) -- aggregate across every education project the user belongs to,
# rather than the single-project views above.
# ---------------------------------------------------------------------------

@education_bp.route("/my-classes", methods=["GET"])
@login_required
def my_classes():
    """Every education project this user teaches, with at-a-glance counts."""
    db = get_db()
    user_id = _user_id()
    projects = db.execute(
        '''SELECT p.id, p.name
           FROM project_assignments pa
           JOIN projects p ON p.id = pa.project_id
           WHERE pa.user_id = %s AND pa.role = %s AND p.project_type = 'education'
           ORDER BY lower(p.name)''',
        (user_id, PROJECT_ROLE_MANAGER),
    ).fetchall()

    out = []
    for p in projects:
        pid = p["id"]
        counts = db.execute(
            '''SELECT COUNT(*) AS total,
                      SUM(CASE WHEN published = 1 THEN 1 ELSE 0 END) AS published
               FROM education_assignments WHERE project_id = %s''',
            (pid,),
        ).fetchone()
        student_count = db.execute(
            "SELECT COUNT(*) AS c FROM project_assignments WHERE project_id = %s AND role = %s",
            (pid, PROJECT_ROLE_STUDENT),
        ).fetchone()
        ungraded = db.execute(
            '''SELECT COUNT(*) AS c FROM assignment_submissions s
               JOIN education_assignments a ON s.assignment_id = a.id
               WHERE a.project_id = %s AND s.status = 'pending' ''',
            (pid,),
        ).fetchone()
        out.append({
            "project_id": pid,
            "project_name": p["name"],
            "assignment_count": int(counts["total"] or 0) if counts else 0,
            "published_count": int(counts["published"] or 0) if counts else 0,
            "student_count": int(student_count["c"] or 0) if student_count else 0,
            "ungraded_count": int(ungraded["c"] or 0) if ungraded else 0,
        })

    return jsonify({"status": "success", "classes": out})


@education_bp.route("/my-assignments", methods=["GET"])
@login_required
def my_assignments():
    """Every published assignment across every education project this user
    is enrolled in as a student, with this student's own progress/grade."""
    db = get_db()
    user_id = _user_id()
    rows = db.execute(
        '''SELECT a.id, a.title, a.open_at, a.close_at, a.time_limit_minutes,
                  a.late_policy, a.total_points, a.notebook_id,
                  p.id AS project_id, p.name AS project_name
           FROM project_assignments pa
           JOIN projects p ON p.id = pa.project_id
           JOIN education_assignments a ON a.project_id = p.id
           WHERE pa.user_id = %s AND pa.role = %s AND p.project_type = 'education'
                 AND a.published = 1
           ORDER BY (a.close_at IS NULL), a.close_at, a.created_at''',
        (user_id, PROJECT_ROLE_STUDENT),
    ).fetchall()

    out = []
    now = datetime.utcnow()
    for a in rows:
        aid = a["id"]
        grade = db.execute(
            "SELECT total_score, max_score, status FROM assignment_grades WHERE assignment_id = %s AND student_id = %s",
            (aid, user_id),
        ).fetchone()
        problem_count = db.execute(
            "SELECT COUNT(*) AS c FROM assignment_problems WHERE assignment_id = %s", (aid,)
        ).fetchone()
        submitted_problems = db.execute(
            '''SELECT COUNT(DISTINCT problem_id) AS c FROM assignment_submissions
               WHERE assignment_id = %s AND student_id = %s AND status != 'extension' ''',
            (aid, user_id),
        ).fetchone()

        close_at = _parse_ts(a["close_at"])
        is_closed = bool(close_at and now > close_at and (a["late_policy"] or "none") == "reject")

        out.append({
            "assignment_id": aid,
            "title": a["title"],
            "project_id": a["project_id"],
            "project_name": a["project_name"],
            "notebook_id": a["notebook_id"],
            "open_at": _iso(a["open_at"]),
            "close_at": _iso(a["close_at"]),
            "time_limit_minutes": a["time_limit_minutes"],
            "late_policy": a["late_policy"],
            "total_points": float(a["total_points"] or 0),
            "problem_count": int(problem_count["c"] or 0) if problem_count else 0,
            "submitted_problem_count": int(submitted_problems["c"] or 0) if submitted_problems else 0,
            "status": grade["status"] if grade else "not_started",
            "score": float(grade["total_score"]) if grade and grade["total_score"] is not None else None,
            "max_score": float(grade["max_score"]) if grade and grade["max_score"] is not None else None,
            "is_closed": is_closed,
        })

    return jsonify({"status": "success", "assignments": out})


# ---------------------------------------------------------------------------
# Roster (teacher only)
# ---------------------------------------------------------------------------

@education_bp.route("/projects/<int:project_id>/roster", methods=["GET"])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
def get_roster(project_id):
    """List all students in an education project (teacher only)."""
    db = get_db()
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400

    members = db.execute(
        '''SELECT u.id, u.username, u.legal_name, u.profile_picture_url, pa.role, pa.created_at
           FROM project_assignments pa
           JOIN users u ON u.id = pa.user_id
           WHERE pa.project_id = %s
           ORDER BY CASE pa.role
                      WHEN %s THEN 0
                      WHEN %s THEN 1
                      ELSE 2
                    END,
                    lower(u.username)''',
        (project_id, PROJECT_ROLE_MANAGER, PROJECT_ROLE_STUDENT),
    ).fetchall()

    roster = []
    for row in members:
        roster.append({
            "user_id": row["id"],
            "username": row["username"],
            "display_name": row["legal_name"] or row["username"],
            "profile_picture_url": row["profile_picture_url"],
            "role": externalize_project_role(row["role"]),
            "is_teacher": row["role"] == PROJECT_ROLE_MANAGER,
            "is_student": row["role"] == PROJECT_ROLE_STUDENT,
            "joined_at": _iso(row["created_at"]),
        })

    # Pending invitations
    pending = db.execute(
        '''SELECT id, invitee_email, role, status, created_at
           FROM invitations
           WHERE project_id = %s AND status = 'pending'
           ORDER BY created_at DESC''',
        (project_id,),
    ).fetchall()
    pending_list = [
        {
            "id": row["id"],
            "email": row["invitee_email"],
            "role": externalize_project_role(row["role"]),
            "status": row["status"],
            "created_at": _iso(row["created_at"]),
        }
        for row in pending
    ]

    return jsonify({
        "status": "success",
        "members": roster,
        "teacher_count": sum(1 for m in roster if m["is_teacher"]),
        "student_count": sum(1 for m in roster if m["is_student"]),
        "pending_invitations": pending_list,
    })


@education_bp.route("/projects/<int:project_id>/roster/<int:user_id>/remove", methods=["POST"])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def remove_student(project_id, user_id):
    """Remove a student from an education project (teacher only).

    The teacher (owner) cannot remove themselves. Only Student-role members
    can be removed here; other managers must use the standard project flow.
    """
    db = get_db()
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400

    if user_id == _user_id():
        return jsonify({"status": "error", "message": "You cannot remove yourself as the teacher."}), 400

    assignment = db.execute(
        "SELECT role FROM project_assignments WHERE project_id = %s AND user_id = %s",
        (project_id, user_id),
    ).fetchone()
    if not assignment:
        return jsonify({"status": "error", "message": "User is not a member of this project."}), 404

    if assignment["role"] != PROJECT_ROLE_STUDENT:
        return jsonify({"status": "error", "message": "Only students can be removed via this endpoint."}), 400

    db.execute(
        "DELETE FROM project_assignments WHERE project_id = %s AND user_id = %s",
        (project_id, user_id),
    )
    db.commit()
    return jsonify({"status": "success", "message": "Student removed."})


# ---------------------------------------------------------------------------
# Assignments (Phase 2 surface — read-only stub for now)
# ---------------------------------------------------------------------------

@education_bp.route("/projects/<int:project_id>/assignments", methods=["GET"])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_STUDENT})
def list_assignments(project_id):
    """List assignments in an education project.

    Teachers see all assignments (drafts + published). Students only see
    published ones. Full assignment CRUD + submission/grading lands in Phase 2.
    """
    db = get_db()
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400

    role = g.current_project_role
    user_id = _user_id()

    if _is_teacher(role):
        rows = db.execute(
            '''SELECT a.id, a.notebook_id, a.title, a.description, a.open_at, a.close_at,
                      a.time_limit_minutes, a.late_policy, a.max_submissions_per_problem,
                      a.total_points, a.published, a.created_at, a.updated_at,
                      n.name as notebook_name
               FROM education_assignments a
               LEFT JOIN notebooks n ON a.notebook_id = n.id
               WHERE a.project_id = %s
               ORDER BY a.created_at DESC''',
            (project_id,),
        ).fetchall()
    else:
        # Students: only published assignments within/around the open window
        rows = db.execute(
            '''SELECT a.id, a.notebook_id, a.title, a.description, a.open_at, a.close_at,
                      a.time_limit_minutes, a.late_policy, a.max_submissions_per_problem,
                      a.total_points, a.published, a.created_at, a.updated_at,
                      n.name as notebook_name
               FROM education_assignments a
               LEFT JOIN notebooks n ON a.notebook_id = n.id
               WHERE a.project_id = %s AND a.published = 1
               ORDER BY a.open_at DESC NULLS LAST, a.created_at DESC''',
            (project_id,),
        ).fetchall()

    assignments = []
    for row in rows:
        assignments.append({
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "notebook_name": row["notebook_name"],
            "title": row["title"],
            "description": row["description"],
            "open_at": _iso(row["open_at"]),
            "close_at": _iso(row["close_at"]),
            "time_limit_minutes": row["time_limit_minutes"],
            "late_policy": row["late_policy"],
            "max_submissions_per_problem": row["max_submissions_per_problem"],
            "total_points": float(row["total_points"]) if row["total_points"] is not None else 0,
            "published": bool(row["published"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        })

    return jsonify({"status": "success", "assignments": assignments, "role": externalize_project_role(role)})


# ---------------------------------------------------------------------------
# Assignment CRUD (Phase 2)
# ---------------------------------------------------------------------------

ALLOWED_PROBLEM_TYPES = {'code', 'numeric', 'formula', 'mc', 'text', 'file'}
ALLOWED_LATE_POLICIES = {'none', 'penalty', 'reject'}


def _ensure_education_tables(db):
    """Idempotently ensure all education tables exist (defence for fresh installs)."""
    for stmt in (
        '''CREATE TABLE IF NOT EXISTS education_assignments (
               id SERIAL PRIMARY KEY, notebook_id INTEGER NOT NULL UNIQUE,
               project_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT,
               open_at TIMESTAMP, close_at TIMESTAMP, time_limit_minutes INTEGER,
               late_policy TEXT NOT NULL DEFAULT 'none',
               max_submissions_per_problem INTEGER NOT NULL DEFAULT 1,
               total_points REAL NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0,
               created_by INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )''',
        '''CREATE TABLE IF NOT EXISTS assignment_problems (
               id SERIAL PRIMARY KEY, assignment_id INTEGER NOT NULL,
               cell_id TEXT NOT NULL, problem_type TEXT NOT NULL,
               config TEXT DEFAULT '{}', points REAL NOT NULL DEFAULT 1,
               sort_order INTEGER NOT NULL DEFAULT 0,
               time_limit_minutes INTEGER,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               UNIQUE(assignment_id, cell_id)
           )''',
        '''CREATE TABLE IF NOT EXISTS assignment_submissions (
               id SERIAL PRIMARY KEY, assignment_id INTEGER NOT NULL,
               problem_id INTEGER NOT NULL, student_id INTEGER NOT NULL,
               attempt_number INTEGER NOT NULL DEFAULT 1, answer TEXT,
               auto_score REAL, final_score REAL, status TEXT NOT NULL DEFAULT 'pending',
               submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, graded_by INTEGER,
               feedback TEXT, time_started TIMESTAMP, time_remaining_sec INTEGER,
               UNIQUE(assignment_id, problem_id, student_id, attempt_number)
           )''',
        '''CREATE TABLE IF NOT EXISTS assignment_grades (
               id SERIAL PRIMARY KEY, assignment_id INTEGER NOT NULL,
               student_id INTEGER NOT NULL, total_score REAL NOT NULL DEFAULT 0,
               max_score REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'in_progress',
               time_started TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               UNIQUE(assignment_id, student_id)
           )''',
    ):
        db.execute(stmt)
    db.commit()


def _config_to_json(config) -> str:
    if isinstance(config, str):
        # Validate it's JSON
        try:
            json.loads(config)
            return config
        except (ValueError, TypeError):
            return '{}'
    if isinstance(config, dict):
        return json.dumps(config)
    return '{}'


def _config_to_dict(config) -> dict:
    if isinstance(config, dict):
        return config
    if isinstance(config, str):
        try:
            return json.loads(config) or {}
        except (ValueError, TypeError):
            return {}
    return {}


@education_bp.route("/projects/<int:project_id>/assignments", methods=["POST"])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def create_assignment(project_id):
    """Teacher creates an assignment from an existing notebook."""
    db = get_db()
    _ensure_education_tables(db)
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400

    data = request.get_json(silent=True) or {}
    notebook_id = data.get("notebook_id")
    title = (data.get("title") or "").strip()
    if not notebook_id or not title:
        return jsonify({"status": "error", "message": "notebook_id and title are required."}), 400

    # Notebook must belong to this project
    nb = db.execute(
        "SELECT id FROM notebooks WHERE id = %s AND project_id = %s",
        (notebook_id, project_id),
    ).fetchone()
    if not nb:
        return jsonify({"status": "error", "message": "Notebook not found in this project."}), 404

    # No duplicate assignment per notebook
    existing = db.execute(
        "SELECT id FROM education_assignments WHERE notebook_id = %s", (notebook_id,)
    ).fetchone()
    if existing:
        return jsonify({"status": "error", "message": "This notebook is already an assignment."}), 409

    # Fields the caller omits entirely (e.g. the notebook IDE's one-click
    # "Make Assignment", which only sends notebook_id + title) fall back to
    # the teacher's saved defaults from user_education_settings instead of a
    # hardcoded value, so those defaults actually mean something.
    _ensure_edu_settings_table(db)
    teacher_defaults = None
    if "late_policy" not in data or "max_submissions_per_problem" not in data or "time_limit_minutes" not in data:
        teacher_defaults = db.execute(
            "SELECT default_late_policy, default_time_limit_minutes, default_max_submissions_per_problem "
            "FROM user_education_settings WHERE user_id = %s",
            (_user_id(),),
        ).fetchone()

    if "late_policy" in data:
        late_policy = (data.get("late_policy") or "none").lower()
    else:
        late_policy = (teacher_defaults["default_late_policy"] if teacher_defaults else None) or "none"
    if late_policy not in ALLOWED_LATE_POLICIES:
        late_policy = "none"

    if "time_limit_minutes" in data:
        try:
            time_limit = int(data.get("time_limit_minutes")) if data.get("time_limit_minutes") else None
        except (TypeError, ValueError):
            time_limit = None
    else:
        time_limit = teacher_defaults["default_time_limit_minutes"] if teacher_defaults else None

    if "max_submissions_per_problem" in data:
        try:
            max_subs = max(1, int(data.get("max_submissions_per_problem", 1)))
        except (TypeError, ValueError):
            max_subs = 1
    else:
        try:
            max_subs = max(1, int(teacher_defaults["default_max_submissions_per_problem"])) if teacher_defaults else 1
        except (TypeError, ValueError):
            max_subs = 1

    result = db.execute(
        '''INSERT INTO education_assignments
           (notebook_id, project_id, title, description, open_at, close_at,
            time_limit_minutes, late_policy, max_submissions_per_problem,
            total_points, published, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s)''',
        (
            notebook_id, project_id, title, (data.get("description") or "").strip() or None,
            data.get("open_at"), data.get("close_at"), time_limit, late_policy, max_subs,
            float(data.get("total_points", 0)), _user_id(),
        ),
    )
    aid = result.lastrowid
    db.commit()
    return jsonify({"status": "success", "assignment_id": aid}), 201


@education_bp.route("/assignments/<int:assignment_id>", methods=["GET"])
@login_required
def get_assignment(assignment_id):
    """Get a single assignment with its problems.

    Teachers get the full config (including encrypted answers + hidden tests).
    Students get a sanitized config (no answers, no hidden test code).
    """
    db = get_db()
    _ensure_education_tables(db)
    a = db.execute(
        '''SELECT a.*, n.name as notebook_name, p.id as project_id
           FROM education_assignments a
           JOIN notebooks n ON a.notebook_id = n.id
           JOIN projects p ON a.project_id = p.id
           WHERE a.id = %s''',
        (assignment_id,),
    ).fetchone()
    if not a:
        return jsonify({"status": "error", "message": "Assignment not found."}), 404

    role = get_project_assignment(db, _user_id(), a["project_id"])
    if not role:
        return jsonify({"status": "error", "message": "Access denied."}), 403

    is_student = _is_student(role)
    if is_student and not int(a["published"]):
        return jsonify({"status": "error", "message": "Assignment not published."}), 404

    problems = db.execute(
        "SELECT * FROM assignment_problems WHERE assignment_id = %s ORDER BY sort_order, id",
        (assignment_id,),
    ).fetchall()

    problems_out = []
    for p in problems:
        cfg = _config_to_dict(p["config"])
        if is_student:
            cfg = sanitize_problem_config_for_student(p["problem_type"], cfg)
        problems_out.append({
            "id": p["id"],
            "cell_id": p["cell_id"],
            "problem_type": p["problem_type"],
            "config": cfg,
            "points": float(p["points"]),
            "sort_order": p["sort_order"],
            "time_limit_minutes": p["time_limit_minutes"] if "time_limit_minutes" in p.keys() else None,
        })

    return jsonify({
        "status": "success",
        "assignment": {
            "id": a["id"],
            "notebook_id": a["notebook_id"],
            "notebook_name": a["notebook_name"],
            "project_id": a["project_id"],
            "title": a["title"],
            "description": a["description"],
            "open_at": _iso(a["open_at"]),
            "close_at": _iso(a["close_at"]),
            "time_limit_minutes": a["time_limit_minutes"],
            "late_policy": a["late_policy"],
            "max_submissions_per_problem": a["max_submissions_per_problem"],
            "total_points": float(a["total_points"]),
            "published": bool(a["published"]),
            "created_at": _iso(a["created_at"]),
            "updated_at": _iso(a["updated_at"]),
        },
        "problems": problems_out,
        "role": externalize_project_role(role),
        "is_teacher": _is_teacher(role),
        "is_student": is_student,
    })


@education_bp.route("/assignments/<int:assignment_id>", methods=["PATCH"])
@login_required
@csrf_protect
def update_assignment(assignment_id):
    """Teacher updates assignment settings."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400

    data = request.get_json(silent=True) or {}
    fields, params = [], []
    for key, caster in (
        ("title", lambda v: (str(v).strip() or None)),
        ("description", lambda v: (str(v).strip() or None)),
        ("open_at", lambda v: v),
        ("close_at", lambda v: v),
        ("time_limit_minutes", lambda v: int(v) if v else None),
        ("max_submissions_per_problem", lambda v: max(1, int(v))),
        ("total_points", lambda v: float(v)),
    ):
        if key in data:
            fields.append(f"{key} = %s")
            params.append(caster(data[key]))
    if "late_policy" in data and data["late_policy"] in ALLOWED_LATE_POLICIES:
        fields.append("late_policy = %s")
        params.append(data["late_policy"])

    if fields:
        params.append(assignment_id)
        db.execute(f"UPDATE education_assignments SET {', '.join(fields)} WHERE id = %s", tuple(params))
        db.commit()
    return jsonify({"status": "success"})


@education_bp.route("/assignments/<int:assignment_id>/publish", methods=["POST"])
@login_required
@csrf_protect
def publish_assignment(assignment_id):
    """Publish an assignment so students can see it."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400
    db.execute("UPDATE education_assignments SET published = 1 WHERE id = %s", (assignment_id,))
    db.commit()
    return jsonify({"status": "success", "message": "Assignment published."})


@education_bp.route("/assignments/<int:assignment_id>", methods=["DELETE"])
@login_required
@csrf_protect
def delete_assignment(assignment_id):
    """Delete an assignment (and its problems/submissions via cascade)."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400
    db.execute("DELETE FROM education_assignments WHERE id = %s", (assignment_id,))
    db.commit()
    return jsonify({"status": "success"})


@education_bp.route("/notebooks/<int:notebook_id>/assignment", methods=["GET"])
@login_required
def get_assignment_for_notebook(notebook_id):
    """Return the assignment that uses this notebook, if any.

    Because ``notebook_id`` is UNIQUE in ``education_assignments``, there
    can be at most one.  The caller still needs to check the user's role
    against the project — this just resolves the id.
    """
    db = get_db()
    _ensure_education_tables(db)
    a = db.execute(
        "SELECT id, project_id, title, published FROM education_assignments WHERE notebook_id = %s",
        (notebook_id,),
    ).fetchone()
    if not a:
        return jsonify({"status": "success", "assignment": None})
    return jsonify({
        "status": "success",
        "assignment": {
            "id": a["id"],
            "project_id": a["project_id"],
            "title": a["title"],
            "published": bool(a["published"]),
        },
    })


# ---------------------------------------------------------------------------
# Problem authoring (Phase 2)
# ---------------------------------------------------------------------------

@education_bp.route("/assignments/<int:assignment_id>/problems/<cell_id>", methods=["PUT"])
@login_required
@csrf_protect
def upsert_problem(assignment_id, cell_id):
    """Teacher defines (or updates) a problem for a notebook cell.

    The ``config`` payload is type-specific. Secret fields (correct answers,
    hidden test code) are AES-encrypted here before storage so they never
    leave the server in plaintext after this point.
    """
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result

    data = request.get_json(silent=True) or {}
    problem_type = (data.get("problem_type") or "").lower()
    if problem_type not in ALLOWED_PROBLEM_TYPES:
        return jsonify({"status": "error", "message": f"problem_type must be one of {sorted(ALLOWED_PROBLEM_TYPES)}"}), 400

    config = dict(data.get("config") or {})
    # Encrypt secret fields at the boundary so plaintext never touches disk.
    if "correct_answer" in config and config["correct_answer"] is not None:
        config["correct_answer_enc"] = encrypt_answer(str(config.pop("correct_answer")))
    if "correct_index" in config and config["correct_index"] is not None:
        config["correct_index_enc"] = encrypt_answer(str(config.pop("correct_index")))
    # test_cases code stays in plaintext (the grader needs it), but it is
    # stripped from student-facing configs by sanitize_problem_config_for_student.

    try:
        points = float(data.get("points", config.get("points", 1)))
    except (TypeError, ValueError):
        points = 1.0
    try:
        sort_order = int(data.get("sort_order", 0))
    except (TypeError, ValueError):
        sort_order = 0
    try:
        task_time_limit = int(data.get("time_limit_minutes")) if data.get("time_limit_minutes") else None
    except (TypeError, ValueError):
        task_time_limit = None

    # Ensure the time_limit_minutes column exists (fresh installs / older DBs)
    try:
        db.execute("ALTER TABLE assignment_problems ADD COLUMN IF NOT EXISTS time_limit_minutes INTEGER")
    except Exception:
        pass

    db.execute(
        '''INSERT INTO assignment_problems (assignment_id, cell_id, problem_type, config, points, sort_order, time_limit_minutes)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (assignment_id, cell_id) DO UPDATE SET
               problem_type = excluded.problem_type,
               config = excluded.config,
               points = excluded.points,
               sort_order = excluded.sort_order,
               time_limit_minutes = excluded.time_limit_minutes''',
        (assignment_id, cell_id, problem_type, _config_to_json(config), points, sort_order, task_time_limit),
    )
    db.commit()
    return jsonify({"status": "success"})


@education_bp.route("/assignments/<int:assignment_id>/problems/<cell_id>", methods=["DELETE"])
@login_required
@csrf_protect
def delete_problem(assignment_id, cell_id):
    """Remove a problem definition from a cell."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    db.execute(
        "DELETE FROM assignment_problems WHERE assignment_id = %s AND cell_id = %s",
        (assignment_id, cell_id),
    )
    db.commit()
    return jsonify({"status": "success"})


@education_bp.route("/assignments/<int:assignment_id>/tasks", methods=["GET"])
@login_required
def list_tasks(assignment_id):
    """List all tasks (problems) in an assignment with full details.

    Teachers see the complete config; students see sanitized configs.
    """
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_STUDENT, PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    is_student = _is_student(role)

    a = db.execute("SELECT published FROM education_assignments WHERE id = %s", (assignment_id,)).fetchone()
    if not a:
        return jsonify({"status": "error", "message": "Assignment not found."}), 404
    if is_student and not int(a["published"]):
        return jsonify({"status": "error", "message": "Assignment not published."}), 404

    problems = db.execute(
        "SELECT * FROM assignment_problems WHERE assignment_id = %s ORDER BY sort_order, id",
        (assignment_id,),
    ).fetchall()
    tasks = []
    for p in problems:
        cfg = _config_to_dict(p["config"])
        if is_student:
            cfg = sanitize_problem_config_for_student(p["problem_type"], cfg)
        tasks.append({
            "id": p["id"],
            "cell_id": p["cell_id"],
            "problem_type": p["problem_type"],
            "config": cfg,
            "points": float(p["points"]),
            "sort_order": p["sort_order"],
            "time_limit_minutes": p["time_limit_minutes"] if "time_limit_minutes" in p.keys() else None,
        })
    return jsonify({"status": "success", "tasks": tasks, "is_teacher": _is_teacher(role)})


@education_bp.route("/assignments/<int:assignment_id>/tasks", methods=["POST"])
@login_required
@csrf_protect
def create_task(assignment_id):
    """Teacher creates a new task (problem) in one call.

    This is a convenience wrapper around ``upsert_problem`` that also generates
    a fresh ``cell_id`` if one isn't provided, so the teacher doesn't need to
    pre-create a notebook cell. The notebook's cells array is updated to
    include a placeholder problem cell + answer cell so the notebook IDE
    renders them.

    Request body:
      - problem_type: code|numeric|formula|mc|text|file
      - title: display title for the task (stored in config)
      - prompt: the problem statement (markdown, stored in config)
      - config: type-specific grading config (correct_answer, tolerance, etc.)
      - points: float (default 1)
      - time_limit_minutes: optional per-task countdown
      - cell_id: optional (auto-generated if omitted)
    """
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result

    data = request.get_json(silent=True) or {}
    problem_type = (data.get("problem_type") or "").lower()
    if problem_type not in ALLOWED_PROBLEM_TYPES:
        return jsonify({"status": "error", "message": f"problem_type must be one of {sorted(ALLOWED_PROBLEM_TYPES)}"}), 400

    import uuid as _uuid
    cell_id = (data.get("cell_id") or "").strip() or f"task-{_uuid.uuid4().hex[:12]}"

    # Build the config from the request, merging title + prompt into it.
    config = dict(data.get("config") or {})
    if data.get("title"):
        config["title"] = str(data["title"]).strip()
    if data.get("prompt"):
        config["prompt"] = str(data["prompt"]).strip()

    # Encrypt secret fields at the boundary
    if "correct_answer" in config and config["correct_answer"] is not None:
        config["correct_answer_enc"] = encrypt_answer(str(config.pop("correct_answer")))
    if "correct_index" in config and config["correct_index"] is not None:
        config["correct_index_enc"] = encrypt_answer(str(config.pop("correct_index")))

    try:
        points = float(data.get("points", config.get("points", 1)))
    except (TypeError, ValueError):
        points = 1.0
    try:
        sort_order = int(data.get("sort_order", 0))
    except (TypeError, ValueError):
        sort_order = 0
    try:
        task_time_limit = int(data.get("time_limit_minutes")) if data.get("time_limit_minutes") else None
    except (TypeError, ValueError):
        task_time_limit = None

    try:
        db.execute("ALTER TABLE assignment_problems ADD COLUMN IF NOT EXISTS time_limit_minutes INTEGER")
    except Exception:
        pass

    db.execute(
        '''INSERT INTO assignment_problems (assignment_id, cell_id, problem_type, config, points, sort_order, time_limit_minutes)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (assignment_id, cell_id) DO UPDATE SET
               problem_type = excluded.problem_type,
               config = excluded.config,
               points = excluded.points,
               sort_order = excluded.sort_order,
               time_limit_minutes = excluded.time_limit_minutes''',
        (assignment_id, cell_id, problem_type, _config_to_json(config), points, sort_order, task_time_limit),
    )

    # Also append a problem cell + answer cell to the notebook so the IDE
    # renders the task. This keeps the notebook and the problem table in sync.
    a = db.execute("SELECT notebook_id FROM education_assignments WHERE id = %s", (assignment_id,)).fetchone()
    if a:
        nb = db.execute("SELECT cells FROM notebooks WHERE id = %s", (a["notebook_id"],)).fetchone()
        if nb:
            cells = nb["cells"]
            if isinstance(cells, str):
                try:
                    cells = json.loads(cells)
                except (ValueError, TypeError):
                    cells = []
            elif not isinstance(cells, list):
                cells = []
            existing_ids = {c.get("id") for c in cells if isinstance(c, dict)}
            if cell_id not in existing_ids:
                cells.append({
                    "id": cell_id,
                    "type": "markdown",
                    "source": config.get("prompt", config.get("title", "New Task")),
                    "metadata": {"education_role": "problem", "problem_type": problem_type},
                })
                answer_cell_id = f"{cell_id}-answer"
                answer_type = "code" if problem_type == "code" else "markdown"
                cells.append({
                    "id": answer_cell_id,
                    "type": answer_type,
                    "source": "",
                    "metadata": {"education_role": "answer", "problem_cell_id": cell_id},
                })
                db.execute("UPDATE notebooks SET cells = %s WHERE id = %s", (json.dumps(cells), a["notebook_id"]))

    db.commit()
    return jsonify({"status": "success", "cell_id": cell_id, "problem_type": problem_type, "points": points, "time_limit_minutes": task_time_limit}), 201


@education_bp.route("/assignments/<int:assignment_id>/tasks/<int:problem_id>", methods=["PATCH"])
@login_required
@csrf_protect
def update_task(assignment_id, problem_id):
    """Teacher updates a single task's settings (points, time limit, config).

    Lighter than re-upserting the whole problem — only the fields present in
    the request body are updated.
    """
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result

    prob = db.execute(
        "SELECT * FROM assignment_problems WHERE id = %s AND assignment_id = %s",
        (problem_id, assignment_id),
    ).fetchone()
    if not prob:
        return jsonify({"status": "error", "message": "Task not found."}), 404

    data = request.get_json(silent=True) or {}
    fields, params = [], []

    if "points" in data:
        try:
            fields.append("points = %s")
            params.append(float(data["points"]))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "points must be a number."}), 400

    if "time_limit_minutes" in data:
        try:
            tl = int(data["time_limit_minutes"]) if data["time_limit_minutes"] else None
            fields.append("time_limit_minutes = %s")
            params.append(tl)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "time_limit_minutes must be an integer."}), 400

    if "sort_order" in data:
        try:
            fields.append("sort_order = %s")
            params.append(int(data["sort_order"]))
        except (TypeError, ValueError):
            pass

    if "problem_type" in data:
        pt = str(data["problem_type"]).lower()
        if pt in ALLOWED_PROBLEM_TYPES:
            fields.append("problem_type = %s")
            params.append(pt)

    new_cfg = None
    if ("config" in data and isinstance(data["config"], dict)) or "title" in data or "prompt" in data:
        new_cfg = _config_to_dict(prob["config"])
        if isinstance(data.get("config"), dict):
            new_cfg.update(data["config"])
        # The Problem Editor sends title/prompt as sibling top-level fields
        # (matching create_task's convention), not nested inside config --
        # merge them in the same way so edits to an existing task actually
        # persist instead of being silently dropped.
        if data.get("title"):
            new_cfg["title"] = str(data["title"]).strip()
        if data.get("prompt"):
            new_cfg["prompt"] = str(data["prompt"]).strip()
        if "correct_answer" in new_cfg and new_cfg["correct_answer"] is not None:
            new_cfg["correct_answer_enc"] = encrypt_answer(str(new_cfg.pop("correct_answer")))
        if "correct_index" in new_cfg and new_cfg["correct_index"] is not None:
            new_cfg["correct_index_enc"] = encrypt_answer(str(new_cfg.pop("correct_index")))
        fields.append("config = %s")
        params.append(_config_to_json(new_cfg))

    if not fields:
        return jsonify({"status": "success", "message": "No changes."})

    params.append(problem_id)
    db.execute(f"UPDATE assignment_problems SET {', '.join(fields)} WHERE id = %s", tuple(params))

    # Keep the notebook cell's rendered text in sync with the problem prompt/
    # title -- create_task seeds this cell on first creation, but nothing
    # previously kept it in sync on later edits, so the notebook IDE would
    # keep showing the stale prompt after a teacher edited it here.
    if new_cfg is not None and (data.get("title") or data.get("prompt")):
        a = db.execute("SELECT notebook_id FROM education_assignments WHERE id = %s", (assignment_id,)).fetchone()
        if a:
            nb = db.execute("SELECT cells FROM notebooks WHERE id = %s", (a["notebook_id"],)).fetchone()
            if nb:
                cells = nb["cells"]
                if isinstance(cells, str):
                    try:
                        cells = json.loads(cells)
                    except (ValueError, TypeError):
                        cells = []
                elif not isinstance(cells, list):
                    cells = []
                changed = False
                for c in cells:
                    if isinstance(c, dict) and c.get("id") == prob["cell_id"]:
                        c["source"] = new_cfg.get("prompt", new_cfg.get("title", c.get("source", "")))
                        changed = True
                        break
                if changed:
                    db.execute("UPDATE notebooks SET cells = %s WHERE id = %s", (json.dumps(cells), a["notebook_id"]))

    db.commit()
    return jsonify({"status": "success"})


# ---------------------------------------------------------------------------
# Student open / submit (Phase 2 + enforcement)
# ---------------------------------------------------------------------------

def _parse_ts(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value
    s = str(value)
    # SQLite may return "YYYY-MM-DD HH:MM:SS" — normalise for fromisoformat
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None


@education_bp.route("/assignments/<int:assignment_id>/open", methods=["POST"])
@login_required
@csrf_protect
def open_assignment(assignment_id):
    """Student opens an assignment — server checks the window and starts the countdown."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_STUDENT, PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    a = db.execute("SELECT * FROM education_assignments WHERE id = %s", (assignment_id,)).fetchone()
    if not a:
        return jsonify({"status": "error", "message": "Assignment not found."}), 404
    if not int(a["published"]):
        return jsonify({"status": "error", "message": "Assignment not published yet."}), 404

    now = datetime.utcnow()
    open_at = _parse_ts(a["open_at"])
    close_at = _parse_ts(a["close_at"])
    if open_at and now < open_at:
        return jsonify({"status": "error", "message": "This assignment is not open yet."}), 403
    if close_at and now > close_at and (a["late_policy"] or "none") == "reject":
        return jsonify({"status": "error", "message": "This assignment is closed."}), 403

    user_id = _user_id()
    # Start (or resume) the per-student countdown.
    grade = db.execute(
        "SELECT time_started FROM assignment_grades WHERE assignment_id = %s AND student_id = %s",
        (assignment_id, user_id),
    ).fetchone()
    time_remaining_sec = None
    if a["time_limit_minutes"]:
        if not grade or not grade["time_started"]:
            db.execute(
                '''INSERT INTO assignment_grades (assignment_id, student_id, time_started, status)
                   VALUES (%s, %s, %s, 'in_progress')
                   ON CONFLICT (assignment_id, student_id) DO UPDATE SET time_started = excluded.time_started''',
                (assignment_id, user_id, now),
            )
            db.commit()
            time_remaining_sec = int(a["time_limit_minutes"]) * 60
        else:
            elapsed = now - _parse_ts(grade["time_started"])
            time_remaining_sec = max(0, int(a["time_limit_minutes"]) * 60 - int(elapsed.total_seconds()))

    # Fetch per-problem time limits so the frontend can show per-task countdowns.
    problems = db.execute(
        "SELECT id, cell_id, time_limit_minutes FROM assignment_problems WHERE assignment_id = %s ORDER BY sort_order, id",
        (assignment_id,),
    ).fetchall()
    problem_time_limits = {}
    for p in problems:
        tl = p["time_limit_minutes"] if "time_limit_minutes" in p.keys() else None
        if tl:
            problem_time_limits[p["id"]] = int(tl) * 60  # seconds

    return jsonify({
        "status": "success",
        "time_remaining_sec": time_remaining_sec,
        "open_at": _iso(a["open_at"]),
        "close_at": _iso(a["close_at"]),
        "time_limit_minutes": a["time_limit_minutes"],
        "late_policy": a["late_policy"],
        "max_submissions_per_problem": a["max_submissions_per_problem"],
        "problem_time_limits_sec": problem_time_limits,
    })


@education_bp.route("/assignments/<int:assignment_id>/problems/<int:problem_id>/submit", methods=["POST"])
@login_required
@csrf_protect
def submit_answer(assignment_id, problem_id):
    """Student submits an answer for a problem.

    Enforces: publish state, open/close window, per-student countdown, and
    max-attempts. Auto-grades when possible; leaves manual types for the teacher.
    """
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_STUDENT, PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    a = db.execute("SELECT * FROM education_assignments WHERE id = %s", (assignment_id,)).fetchone()
    if not a:
        return jsonify({"status": "error", "message": "Assignment not found."}), 404
    if not int(a["published"]):
        return jsonify({"status": "error", "message": "Assignment not published yet."}), 404

    prob = db.execute(
        "SELECT * FROM assignment_problems WHERE id = %s AND assignment_id = %s",
        (problem_id, assignment_id),
    ).fetchone()
    if not prob:
        return jsonify({"status": "error", "message": "Problem not found."}), 404

    user_id = _user_id()
    now = datetime.utcnow()
    open_at = _parse_ts(a["open_at"])
    close_at = _parse_ts(a["close_at"])
    late_policy = a["late_policy"] or "none"

    # Window enforcement
    if open_at and now < open_at:
        return jsonify({"status": "error", "message": "This assignment is not open yet."}), 403
    past_close = bool(close_at and now > close_at)
    if past_close and late_policy == "reject":
        return jsonify({"status": "error", "message": "Submissions are closed for this assignment."}), 403

    # Countdown enforcement — per-problem time limit takes precedence over the
    # assignment-level one when set. Both use the same time_started anchor in
    # assignment_grades so a single "open" starts all timers.
    time_remaining_sec = None
    prob_time_limit = prob["time_limit_minutes"] if "time_limit_minutes" in prob.keys() else None
    effective_time_limit = prob_time_limit or a["time_limit_minutes"]
    if effective_time_limit:
        grade = db.execute(
            "SELECT time_started FROM assignment_grades WHERE assignment_id = %s AND student_id = %s",
            (assignment_id, user_id),
        ).fetchone()
        if not grade or not grade["time_started"]:
            return jsonify({"status": "error", "message": "Open the assignment before submitting."}), 400
        elapsed = now - _parse_ts(grade["time_started"])
        time_remaining_sec = max(0, int(effective_time_limit) * 60 - int(elapsed.total_seconds()))
        if time_remaining_sec <= 0 and late_policy == "reject":
            return jsonify({"status": "error", "message": "Time has expired for this task."}), 403

    # Max-attempts enforcement (extension rows are excluded from the count).
    max_subs = int(a["max_submissions_per_problem"])
    last = db.execute(
        "SELECT MAX(attempt_number) AS n FROM assignment_submissions WHERE assignment_id = %s AND problem_id = %s AND student_id = %s AND status != 'extension'",
        (assignment_id, problem_id, user_id),
    ).fetchone()
    current_attempts = int(last["n"]) if (last and last["n"] is not None) else 0
    # Count granted extensions for this student on this assignment
    ext = db.execute(
        "SELECT COUNT(*) AS c FROM assignment_submissions WHERE assignment_id = %s AND student_id = %s AND status = 'extension'",
        (assignment_id, user_id),
    ).fetchone()
    extra_allowed = int(ext["c"]) if ext else 0
    effective_max = max_subs + extra_allowed
    if current_attempts >= effective_max:
        return jsonify({
            "status": "error",
            "message": f"You have used all {effective_max} allowed attempt(s) for this problem.",
            "attempts_used": current_attempts,
            "attempts_remaining": 0,
        }), 403

    data = request.get_json(silent=True) or {}
    answer = data.get("answer")
    if answer is None:
        return jsonify({"status": "error", "message": "answer is required."}), 400
    if not isinstance(answer, str):
        answer = json.dumps(answer)

    attempt_number = current_attempts + 1
    config = _config_to_dict(prob["config"])

    # Auto-grade (returns None score for manual types)
    auto_score, feedback, is_correct = auto_grade(
        prob["problem_type"], answer, config, user_id, db
    )

    status = "graded" if auto_score is not None else "pending"
    if past_close and late_policy == "penalty" and auto_score is not None:
        # Simple 20% late penalty
        auto_score = round(auto_score * 0.8, 4)
        feedback = (feedback or "") + " (20% late penalty applied.)"

    try:
        db.execute("ALTER TABLE assignment_submissions ADD COLUMN IF NOT EXISTS is_correct BOOLEAN")
    except Exception:
        pass

    result = db.execute(
        '''INSERT INTO assignment_submissions
           (assignment_id, problem_id, student_id, attempt_number, answer,
            auto_score, final_score, status, feedback, time_remaining_sec, is_correct)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (
            assignment_id, problem_id, user_id, attempt_number, answer,
            auto_score, auto_score, status, feedback, time_remaining_sec, is_correct,
        ),
    )
    submission_id = result.lastrowid
    db.commit()

    # Refresh the aggregate gradebook row
    _recompute_grade(db, assignment_id, user_id, a)

    return jsonify({
        "status": "success",
        "submission_id": submission_id,
        "attempt_number": attempt_number,
        "attempts_remaining": max(0, effective_max - attempt_number),
        "auto_score": auto_score,
        "feedback": feedback,
        "is_correct": is_correct,
        "graded": auto_score is not None,
        "time_remaining_sec": time_remaining_sec,
    })


@education_bp.route("/assignments/<int:assignment_id>/submissions", methods=["GET"])
@login_required
def list_submissions(assignment_id):
    """List submissions. Students see only their own; teachers see all."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_STUDENT, PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    user_id = _user_id()

    if _is_teacher(role):
        rows = db.execute(
            '''SELECT s.*, u.username
               FROM assignment_submissions s
               JOIN users u ON s.student_id = u.id
               WHERE s.assignment_id = %s
               ORDER BY s.submitted_at DESC''',
            (assignment_id,),
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT * FROM assignment_submissions
               WHERE assignment_id = %s AND student_id = %s
               ORDER BY attempt_number DESC''',
            (assignment_id, user_id),
        ).fetchall()

    out = [{
        "id": r["id"],
        "problem_id": r["problem_id"],
        "student_id": r["student_id"],
        "student": r["username"] if "username" in r.keys() else None,
        "attempt_number": r["attempt_number"],
        "answer": r["answer"],
        "auto_score": r["auto_score"],
        "final_score": r["final_score"],
        "status": r["status"],
        "feedback": r["feedback"],
        "submitted_at": _iso(r["submitted_at"]),
        "time_remaining_sec": r["time_remaining_sec"],
        # Authoritative correctness from grading time -- independent of any
        # later late-penalty score adjustment, so the frontend no longer needs
        # to (mis)infer it by comparing final_score against the task's points.
        "is_correct": r["is_correct"] if "is_correct" in r.keys() else None,
    } for r in rows if r["status"] != "extension"]
    return jsonify({"status": "success", "submissions": out, "is_teacher": _is_teacher(role)})


# ---------------------------------------------------------------------------
# Manual grading + gradebook + analytics (Phase 3)
# ---------------------------------------------------------------------------

def _recompute_grade(db, assignment_id, student_id, assignment_row=None):
    """Recompute the aggregate gradebook row for one student/assignment."""
    if assignment_row is None:
        assignment_row = db.execute(
            "SELECT * FROM education_assignments WHERE id = %s", (assignment_id,)
        ).fetchone()
    if not assignment_row:
        return

    # Sum the best final_score per problem for this student
    rows = db.execute(
        '''SELECT problem_id, MAX(final_score) AS best
           FROM assignment_submissions
           WHERE assignment_id = %s AND student_id = %s AND final_score IS NOT NULL
           GROUP BY problem_id''',
        (assignment_id, student_id),
    ).fetchall()
    total = sum(float(r["best"] or 0) for r in rows)

    # Max possible = sum of problem points
    max_row = db.execute(
        "SELECT COALESCE(SUM(points), 0) AS s FROM assignment_problems WHERE assignment_id = %s",
        (assignment_id,),
    ).fetchone()
    max_score = float(max_row["s"]) if max_row else 0.0

    # Count pending (ungraded) submissions to decide status
    pending = db.execute(
        "SELECT COUNT(*) AS c FROM assignment_submissions WHERE assignment_id = %s AND student_id = %s AND status = 'pending'",
        (assignment_id, student_id),
    ).fetchone()
    has_any = db.execute(
        "SELECT COUNT(*) AS c FROM assignment_submissions WHERE assignment_id = %s AND student_id = %s",
        (assignment_id, student_id),
    ).fetchone()
    status = "in_progress"
    if int(has_any["c"]) > 0 and int(pending["c"]) == 0:
        status = "graded"
    elif int(has_any["c"]) == 0:
        status = "in_progress"

    db.execute(
        '''INSERT INTO assignment_grades (assignment_id, student_id, total_score, max_score, status)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (assignment_id, student_id) DO UPDATE SET
               total_score = excluded.total_score,
               max_score = excluded.max_score,
               status = excluded.status''',
        (assignment_id, student_id, total, max_score, status),
    )
    db.commit()


@education_bp.route("/assignments/<int:assignment_id>/submissions/<int:submission_id>/grade", methods=["POST"])
@login_required
@csrf_protect
def manual_grade(assignment_id, submission_id):
    """Teacher manually grades or overrides a submission."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    sub = db.execute(
        "SELECT * FROM assignment_submissions WHERE id = %s AND assignment_id = %s",
        (submission_id, assignment_id),
    ).fetchone()
    if not sub:
        return jsonify({"status": "error", "message": "Submission not found."}), 404

    data = request.get_json(silent=True) or {}
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "score (number) is required."}), 400
    feedback = (data.get("feedback") or "").strip() or None

    db.execute(
        "UPDATE assignment_submissions SET final_score = %s, feedback = %s, status = 'graded', graded_by = %s WHERE id = %s",
        (score, feedback, _user_id(), submission_id),
    )
    db.commit()
    _recompute_grade(db, assignment_id, sub["student_id"])
    return jsonify({"status": "success", "final_score": score})


@education_bp.route("/projects/<int:project_id>/gradebook", methods=["GET"])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
def get_gradebook(project_id):
    """Full gradebook for an education project (teacher only)."""
    db = get_db()
    _ensure_education_tables(db)
    if _get_project_type(db, project_id) != "education":
        return jsonify({"status": "error", "message": "Not an education project."}), 400

    assignments = db.execute(
        "SELECT id, title, total_points FROM education_assignments WHERE project_id = %s ORDER BY created_at",
        (project_id,),
    ).fetchall()
    students = db.execute(
        '''SELECT u.id, u.username, u.legal_name
           FROM project_assignments pa JOIN users u ON pa.user_id = u.id
           WHERE pa.project_id = %s AND pa.role = %s ORDER BY lower(u.username)''',
        (project_id, PROJECT_ROLE_STUDENT),
    ).fetchall()

    gradebook = []
    for s in students:
        grades = db.execute(
            '''SELECT assignment_id, total_score, max_score, status
               FROM assignment_grades WHERE student_id = %s AND assignment_id IN
               (SELECT id FROM education_assignments WHERE project_id = %s)''',
            (s["id"], project_id),
        ).fetchall()
        grade_map = {g["assignment_id"]: g for g in grades}
        row = {
            "student_id": s["id"],
            "username": s["username"],
            "display_name": s["legal_name"] or s["username"],
            "grades": [],
        }
        total_earned = 0.0
        total_max = 0.0
        for a in assignments:
            g = grade_map.get(a["id"])
            earned = float(g["total_score"]) if g and g["total_score"] is not None else 0.0
            mx = float(g["max_score"]) if g and g["max_score"] is not None else float(a["total_points"] or 0)
            status = g["status"] if g else "in_progress"
            row["grades"].append({
                "assignment_id": a["id"],
                "title": a["title"],
                "score": earned,
                "max_score": mx,
                "status": status,
            })
            total_earned += earned
            total_max += mx
        row["total_earned"] = round(total_earned, 4)
        row["total_max"] = round(total_max, 4)
        row["overall_pct"] = round(total_earned / total_max * 100, 2) if total_max else 0.0
        gradebook.append(row)

    return jsonify({
        "status": "success",
        "assignments": [{"id": a["id"], "title": a["title"], "max_points": float(a["total_points"] or 0)} for a in assignments],
        "students": gradebook,
    })


@education_bp.route("/assignments/<int:assignment_id>/analytics", methods=["GET"])
@login_required
def assignment_analytics(assignment_id):
    """Per-problem success-rate stats for the teacher."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    problems = db.execute(
        "SELECT id, cell_id, problem_type, points FROM assignment_problems WHERE assignment_id = %s ORDER BY sort_order, id",
        (assignment_id,),
    ).fetchall()
    out = []
    for p in problems:
        stats = db.execute(
            '''SELECT COUNT(*) AS attempts,
                      SUM(CASE WHEN final_score IS NOT NULL THEN 1 ELSE 0 END) AS graded,
                      AVG(final_score) AS avg_score,
                      MAX(final_score) AS max_score,
                      SUM(CASE WHEN final_score IS NOT NULL AND final_score >= %s THEN 1 ELSE 0 END) AS perfect
               FROM assignment_submissions WHERE problem_id = %s AND status != 'extension' ''',
            (p["points"], p["id"]),
        ).fetchone()
        out.append({
            "problem_id": p["id"],
            "cell_id": p["cell_id"],
            "problem_type": p["problem_type"],
            "points": float(p["points"]),
            "attempts": int(stats["attempts"] or 0),
            "graded": int(stats["graded"] or 0),
            "avg_score": round(float(stats["avg_score"]), 4) if stats["avg_score"] is not None else None,
            "max_score": round(float(stats["max_score"]), 4) if stats["max_score"] is not None else None,
            "perfect_count": int(stats["perfect"] or 0),
        })
    return jsonify({"status": "success", "problems": out})


@education_bp.route("/assignments/<int:assignment_id>/students/<int:student_id>/extend", methods=["POST"])
@login_required
@csrf_protect
def grant_extension(assignment_id, student_id):
    """Teacher grants extra attempts or a time extension to a student."""
    result, project_id, role = _check_assignment_role(assignment_id, {PROJECT_ROLE_MANAGER})
    if project_id is None:
        return result
    db = result
    data = request.get_json(silent=True) or {}
    extra_attempts = data.get("extra_attempts")
    extend_minutes = data.get("extend_minutes")

    if extra_attempts:
        try:
            extra = int(extra_attempts)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "extra_attempts must be an integer."}), 400
        if extra > 0:
            # Extra attempts are recorded as status='extension' marker rows;
            # submit_answer()'s max-attempts check counts them (scoped to
            # assignment_id + student_id, ignoring problem_id) and adds them
            # on top of the assignment's normal per-problem cap. problem_id is
            # NOT NULL and part of a UNIQUE(assignment_id, problem_id,
            # student_id, attempt_number) constraint, so each marker needs a
            # real problem_id (any problem in the assignment works, since the
            # count that reads these back doesn't filter by problem_id) and a
            # distinct attempt_number -- we count down from -1 so markers
            # never collide with real attempts (which start at 1).
            anchor = db.execute(
                "SELECT id FROM assignment_problems WHERE assignment_id = %s ORDER BY id LIMIT 1",
                (assignment_id,),
            ).fetchone()
            if not anchor:
                return jsonify({"status": "error", "message": "This assignment has no problems yet."}), 400
            anchor_problem_id = anchor["id"]
            floor = db.execute(
                '''SELECT MIN(attempt_number) AS n FROM assignment_submissions
                   WHERE assignment_id = %s AND problem_id = %s AND student_id = %s AND status = 'extension' ''',
                (assignment_id, anchor_problem_id, student_id),
            ).fetchone()
            next_attempt = (int(floor["n"]) - 1) if (floor and floor["n"] is not None) else -1
            for _ in range(extra):
                db.execute(
                    '''INSERT INTO assignment_submissions
                       (assignment_id, problem_id, student_id, attempt_number, answer, status)
                       VALUES (%s, %s, %s, %s, '__extension__', 'extension')''',
                    (assignment_id, anchor_problem_id, student_id, next_attempt),
                )
                next_attempt -= 1
            db.commit()

    if extend_minutes:
        try:
            mins = int(extend_minutes)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "extend_minutes must be an integer."}), 400
        if mins > 0:
            g = db.execute(
                "SELECT time_started FROM assignment_grades WHERE assignment_id = %s AND student_id = %s",
                (assignment_id, student_id),
            ).fetchone()
            if g and g["time_started"]:
                started = _parse_ts(g["time_started"])
                if started:
                    new_start = started + timedelta(minutes=mins)
                    db.execute(
                        "UPDATE assignment_grades SET time_started = %s WHERE assignment_id = %s AND student_id = %s",
                        (new_start, assignment_id, student_id),
                    )
                    db.commit()

    return jsonify({"status": "success", "message": "Extension applied."})


def register_education_routes(app):
    """Register the education blueprint on the Flask app."""
    app.register_blueprint(education_bp)