"""API routes for activity feed operations."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import jsonify, request, session

from ..app import app, logger
from ..helpers import get_db, login_required, serialize_datetime
from ..helpers.security import csrf_protect, sanitize_input


def ensure_activity_log_table(db):
    """Ensure activity_log table exists.

    Uses the schema production/database.py creates and helpers/activity_log.py
    writes (no project_id / team_id columns). A project or team is expressed as
    resource_type 'project' / 'team' + resource_id.
    """
    db.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            activity_type TEXT NOT NULL,
            activity_description TEXT NOT NULL,
            resource_type TEXT,
            resource_id INTEGER,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()


def log_activity(db, user_id, activity_type, description, resource_type=None, resource_id=None, project_id=None, team_id=None, metadata=None):
    """Log an activity to the activity feed.

    project_id / team_id are stored as the resource when no explicit resource
    is given; otherwise they are kept in the metadata JSON.
    """
    ensure_activity_log_table(db)

    if project_id and not resource_type:
        resource_type, resource_id = 'project', project_id
    elif team_id and not resource_type:
        resource_type, resource_id = 'team', team_id
    else:
        extra = {k: v for k, v in (('project_id', project_id), ('team_id', team_id)) if v}
        if extra:
            metadata = {**(metadata or {}), **extra}

    meta_json = json.dumps(metadata) if metadata else None

    db.execute("""
        INSERT INTO activity_log
        (user_id, activity_type, activity_description, resource_type, resource_id, metadata)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (user_id, activity_type, description, resource_type, resource_id, meta_json))
    db.commit()


# Activity type constants
ACTIVITY_TYPES = {
    'job_submitted': 'submitted a job',
    'job_completed': 'completed a job',
    'job_failed': 'job failed',
    'file_uploaded': 'uploaded a file',
    'file_deleted': 'deleted a file',
    'project_created': 'created a project',
    'project_updated': 'updated a project',
    'project_deleted': 'deleted a project',
    'member_joined': 'joined the team',
    'member_left': 'left the team',
    'member_invited': 'invited a member',
    'comment_added': 'added a comment',
    'settings_updated': 'updated settings',
    'login': 'logged in',
}


@app.route('/api/activity', methods=['GET'])
@login_required
@csrf_protect
def get_activity_feed():
    """Get activity feed for the current user."""
    try:
        db = get_db()
        user_id = session['user_id']
        ensure_activity_log_table(db)

        # Query parameters
        limit = min(100, max(1, int(request.args.get('limit', 50))))
        offset = max(0, int(request.args.get('offset', 0)))
        activity_type = request.args.get('type')
        project_id = request.args.get('project_id')
        team_id = request.args.get('team_id')
        days = request.args.get('days')

        # Build query
        query = """
            SELECT
                a.id,
                a.activity_type,
                a.activity_description,
                a.resource_type,
                a.resource_id,
                p.id as project_id,
                t.id as team_id,
                a.metadata,
                a.created_at,
                u.username,
                u.profile_picture_url,
                p.name as project_name,
                t.name as team_name
            FROM activity_log a
            JOIN users u ON a.user_id = u.id
            LEFT JOIN projects p ON a.resource_type = 'project' AND a.resource_id = p.id
            LEFT JOIN teams t ON a.resource_type = 'team' AND a.resource_id = t.id
            WHERE 1=1
        """
        params = []

        # Filter by projects/teams the user has access to
        # Get user's project IDs
        user_projects = db.execute("""
            SELECT project_id FROM project_assignments WHERE user_id = %s
        """, (user_id,)).fetchall()
        project_ids = [p['project_id'] for p in user_projects]

        # Get user's team IDs
        user_teams = db.execute("""
            SELECT team_id FROM team_members WHERE user_id = %s
        """, (user_id,)).fetchall()
        team_ids = [t['team_id'] for t in user_teams]

        if project_ids or team_ids:
            conditions = []
            if project_ids:
                placeholders = ','.join(['%s' for _ in project_ids])
                conditions.append(f"(a.resource_type = 'project' AND a.resource_id IN ({placeholders}))")
                params.extend(project_ids)
            if team_ids:
                placeholders = ','.join(['%s' for _ in team_ids])
                conditions.append(f"(a.resource_type = 'team' AND a.resource_id IN ({placeholders}))")
                params.extend(team_ids)
            # Also include user's own activities
            conditions.append('a.user_id = %s')
            params.append(user_id)

            query += f' AND ({" OR ".join(conditions)})'
        else:
            # Only show user's own activities
            query += ' AND a.user_id = %s'
            params.append(user_id)

        # Apply filters
        if activity_type:
            query += ' AND a.activity_type = %s'
            params.append(sanitize_input(activity_type))

        if project_id:
            query += " AND a.resource_type = 'project' AND a.resource_id = %s"
            params.append(int(project_id))

        if team_id:
            query += " AND a.resource_type = 'team' AND a.resource_id = %s"
            params.append(int(team_id))

        if days:
            cutoff = datetime.utcnow() - timedelta(days=int(days))
            query += ' AND a.created_at >= %s'
            params.append(cutoff)

        query += ' ORDER BY a.created_at DESC LIMIT %s OFFSET %s'
        params.extend([limit, offset])

        rows = db.execute(query, tuple(params)).fetchall()

        activities = []
        for row in rows:
            try:
                metadata = json.loads(row['metadata']) if row['metadata'] else {}
            except (TypeError, ValueError):
                metadata = {}

            activities.append({
                'id': row['id'],
                'type': row['activity_type'],
                'description': row['activity_description'],
                'resource_type': row['resource_type'],
                'resource_id': row['resource_id'],
                'project_id': row['project_id'],
                'project_name': row['project_name'],
                'team_id': row['team_id'],
                'team_name': row['team_name'],
                'user': {
                    'username': row['username'],
                    'profile_picture_url': row['profile_picture_url']
                },
                'metadata': metadata,
                'created_at': serialize_datetime(row['created_at'])
            })

        return jsonify({
            'activities': activities,
            'has_more': len(activities) == limit
        })

    except Exception as e:
        logger.error(f"Error fetching activity feed: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch activity feed.'}), 500


@app.route('/api/activity/project/<int:project_id>', methods=['GET'])
@login_required
@csrf_protect
def get_project_activity(project_id):
    """Get activity feed for a specific project."""
    try:
        db = get_db()
        user_id = session['user_id']
        ensure_activity_log_table(db)

        # Verify user has access to project
        assignment = db.execute("""
            SELECT role FROM project_assignments WHERE project_id = %s AND user_id = %s
        """, (project_id, user_id)).fetchone()

        if not assignment:
            return jsonify({'status': 'error', 'message': 'Access denied.'}), 403

        limit = min(100, max(1, int(request.args.get('limit', 30))))

        rows = db.execute("""
            SELECT
                a.id,
                a.activity_type,
                a.activity_description,
                a.resource_type,
                a.resource_id,
                a.metadata,
                a.created_at,
                u.username,
                u.profile_picture_url
            FROM activity_log a
            JOIN users u ON a.user_id = u.id
            WHERE a.resource_type = 'project' AND a.resource_id = %s
            ORDER BY a.created_at DESC
            LIMIT %s
        """, (project_id, limit)).fetchall()

        activities = []
        for row in rows:
            try:
                metadata = json.loads(row['metadata']) if row['metadata'] else {}
            except (TypeError, ValueError):
                metadata = {}

            activities.append({
                'id': row['id'],
                'type': row['activity_type'],
                'description': row['activity_description'],
                'resource_type': row['resource_type'],
                'resource_id': row['resource_id'],
                'user': {
                    'username': row['username'],
                    'profile_picture_url': row['profile_picture_url']
                },
                'metadata': metadata,
                'created_at': serialize_datetime(row['created_at'])
            })

        return jsonify({'activities': activities})

    except Exception as e:
        logger.error(f"Error fetching project activity: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to fetch activity.'}), 500


@app.route('/api/activity/log', methods=['POST'])
@login_required
@csrf_protect
def log_user_activity():
    """Log a user activity (for frontend-triggered events)."""
    try:
        db = get_db()
        user_id = session['user_id']
        data = request.json or {}

        activity_type = sanitize_input(data.get('type', 'unknown'))
        description = sanitize_input(data.get('description', ''))
        resource_type = sanitize_input(data.get('resource_type')) if data.get('resource_type') else None
        resource_id = int(data['resource_id']) if data.get('resource_id') else None
        project_id = int(data['project_id']) if data.get('project_id') else None
        team_id = int(data['team_id']) if data.get('team_id') else None
        metadata = data.get('metadata')

        if not description:
            description = ACTIVITY_TYPES.get(activity_type, 'performed an action')

        log_activity(
            db, user_id, activity_type, description,
            resource_type=resource_type,
            resource_id=resource_id,
            project_id=project_id,
            team_id=team_id,
            metadata=metadata
        )

        return jsonify({'status': 'success'})

    except Exception as e:
        logger.error(f"Error logging activity: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to log activity.'}), 500


# Export helper for use in other modules
__all__ = ['log_activity', 'ACTIVITY_TYPES']
