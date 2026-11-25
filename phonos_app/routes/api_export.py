"""API routes for project export operations."""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime

from flask import jsonify, request, send_file, session

from ..app import app, logger
from ..helpers import get_db, login_required, serialize_datetime
from ..helpers.security import csrf_protect


def get_project_export_data(db, project_id, user_id):
    """Gather all project data for export."""

    # Verify access
    assignment = db.execute("""
        SELECT role FROM project_assignments WHERE project_id = %s AND user_id = %s
    """, (project_id, user_id)).fetchone()

    if not assignment:
        return None, "Access denied"

    # Get project info
    project = db.execute("""
        SELECT
            p.*,
            t.name as team_name
        FROM projects p
        LEFT JOIN teams t ON p.team_id = t.id
        WHERE p.id = %s
    """, (project_id,)).fetchone()

    if not project:
        return None, "Project not found"

    # Get files
    files = db.execute("""
        SELECT f.*, u.username as creator_username
        FROM files f
        JOIN users u ON f.creator_id = u.id
        WHERE f.project_id = %s
        ORDER BY f.created_at
    """, (project_id,)).fetchall()

    # Get jobs
    jobs = db.execute("""
        SELECT j.*, u.username as creator_username
        FROM jobs j
        JOIN users u ON j.creator_id = u.id
        WHERE j.project_id = %s
        ORDER BY j.created_at DESC
    """, (project_id,)).fetchall()

    # Get members
    members = db.execute("""
        SELECT pa.role, u.username, u.profile_picture_url, pa.created_at
        FROM project_assignments pa
        JOIN users u ON pa.user_id = u.id
        WHERE pa.project_id = %s
    """, (project_id,)).fetchall()

    # Get messages (last 100)
    messages = db.execute("""
        SELECT pm.*, u.username as sender_username
        FROM project_messages pm
        JOIN users u ON pm.sender_id = u.id
        WHERE pm.project_id = %s
        ORDER BY pm.created_at DESC
        LIMIT 100
    """, (project_id,)).fetchall()

    return {
        'project': dict(project),
        'files': [dict(f) for f in files],
        'jobs': [dict(j) for j in jobs],
        'members': [dict(m) for m in members],
        'messages': [dict(m) for m in messages]
    }, None


def serialize_export_data(data):
    """Serialize export data to JSON-compatible format."""

    def serialize_row(row):
        result = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, bytes):
                result[key] = None  # Skip binary data
            else:
                result[key] = value
        return result

    return {
        'export_version': '1.0',
        'exported_at': datetime.utcnow().isoformat(),
        'project': serialize_row(data['project']),
        'files': [serialize_row(f) for f in data['files']],
        'jobs': [serialize_row(j) for j in data['jobs']],
        'members': [serialize_row(m) for m in data['members']],
        'messages': [serialize_row(m) for m in data['messages']]
    }


@app.route('/api/projects/<int:project_id>/export', methods=['GET'])
@login_required
@csrf_protect
def export_project(project_id):
    """Export project metadata as JSON."""
    try:
        db = get_db()
        user_id = session['user_id']

        data, error = get_project_export_data(db, project_id, user_id)
        if error:
            return jsonify({'status': 'error', 'message': error}), 403 if 'Access' in error else 404

        export_data = serialize_export_data(data)

        return jsonify({
            'status': 'success',
            'data': export_data
        })

    except Exception as e:
        logger.error(f"Error exporting project {project_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to export project.'}), 500


@app.route('/api/projects/<int:project_id>/export/zip', methods=['GET'])
@login_required
@csrf_protect
def export_project_zip(project_id):
    """Export project with all files as a ZIP archive."""
    try:
        db = get_db()
        user_id = session['user_id']

        data, error = get_project_export_data(db, project_id, user_id)
        if error:
            return jsonify({'status': 'error', 'message': error}), 403 if 'Access' in error else 404

        project_name = data['project'].get('name', 'project')
        safe_name = ''.join(c for c in project_name if c.isalnum() or c in ' -_').strip()

        # Create ZIP in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Add metadata JSON
            export_data = serialize_export_data(data)
            zf.writestr('metadata.json', json.dumps(export_data, indent=2))

            # Add files
            for file_info in data['files']:
                file_path = file_info.get('path')
                if file_path and os.path.exists(file_path):
                    file_name = file_info.get('name', os.path.basename(file_path))
                    arcname = f"files/{file_name}"
                    try:
                        zf.write(file_path, arcname)
                    except Exception as e:
                        logger.warning(f"Could not add file to ZIP: {file_path}: {e}")

            # Add job outputs
            for job in data['jobs']:
                output_file = job.get('output_file')
                if output_file and os.path.exists(output_file):
                    job_id = job.get('job_id', 'unknown')
                    output_name = job.get('output_display_name', os.path.basename(output_file))
                    arcname = f"job_outputs/{job_id}_{output_name}"
                    try:
                        zf.write(output_file, arcname)
                    except Exception as e:
                        logger.warning(f"Could not add job output to ZIP: {output_file}: {e}")

            # Add README
            readme = f"""# {project_name}

Exported from Phonos on {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC

## Contents

- metadata.json: Full project metadata including files, jobs, members, and messages
- files/: Uploaded project files
- job_outputs/: Job output files

## Project Info

- Created: {data['project'].get('created_at', 'Unknown')}
- Team: {data['project'].get('team_name', 'None')}
- Members: {len(data['members'])}
- Files: {len(data['files'])}
- Jobs: {len(data['jobs'])}
"""
            zf.writestr('README.md', readme)

        zip_buffer.seek(0)

        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{safe_name}_export.zip'
        )

    except Exception as e:
        logger.error(f"Error exporting project ZIP {project_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to export project.'}), 500


@app.route('/api/analytics/export', methods=['GET'])
@login_required
@csrf_protect
def export_analytics():
    """Export user analytics as CSV."""
    try:
        db = get_db()
        user_id = session['user_id']

        # Get date range
        days = int(request.args.get('days', 30))
        format_type = request.args.get('format', 'csv')

        # Fetch analytics data
        rows = db.execute("""
            SELECT
                ua.event_type,
                ua.credits_used,
                ua.storage_delta,
                ua.compute_time,
                ua.created_at,
                p.name as project_name,
                j.name as job_name,
                j.model
            FROM usage_analytics ua
            LEFT JOIN projects p ON ua.project_id = p.id
            LEFT JOIN jobs j ON ua.job_id = j.id
            WHERE ua.user_id = %s
                AND ua.created_at >= CURRENT_TIMESTAMP - INTERVAL %s DAY
            ORDER BY ua.created_at DESC
        """, (user_id, days)).fetchall()

        if format_type == 'json':
            data = [
                {
                    'event_type': r['event_type'],
                    'credits_used': r['credits_used'],
                    'storage_delta': r['storage_delta'],
                    'compute_time': r['compute_time'],
                    'project_name': r['project_name'],
                    'job_name': r['job_name'],
                    'model': r['model'],
                    'created_at': serialize_datetime(r['created_at'])
                }
                for r in rows
            ]
            return jsonify({
                'status': 'success',
                'period_days': days,
                'data': data
            })

        # CSV format
        csv_lines = ['Event Type,Credits Used,Storage Delta,Compute Time,Project,Job,Model,Date']
        for r in rows:
            csv_lines.append(','.join([
                str(r['event_type'] or ''),
                str(r['credits_used'] or 0),
                str(r['storage_delta'] or 0),
                str(r['compute_time'] or ''),
                f'"{r["project_name"] or ""}"',
                f'"{r["job_name"] or ""}"',
                str(r['model'] or ''),
                serialize_datetime(r['created_at']) or ''
            ]))

        csv_content = '\n'.join(csv_lines)

        return send_file(
            io.BytesIO(csv_content.encode()),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'phonos_analytics_{days}days.csv'
        )

    except Exception as e:
        logger.error(f"Error exporting analytics: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': 'Failed to export analytics.'}), 500
