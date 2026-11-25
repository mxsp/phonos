"""Extended API endpoints for new features: templates, sharing, analytics, etc."""
from __future__ import annotations

from datetime import datetime
import os

from flask import jsonify, request, session

from ..app import app, logger
from ..helpers import login_required, admin_required, get_db, project_role_required
from ..helpers import (
    PROJECT_ROLE_MANAGER,
    PROJECT_ROLE_CONTRIBUTOR,
    PROJECT_ROLE_VIEWER,
)
from ..helpers.templates import (
    create_template,
    get_template,
    get_user_templates,
    get_project_templates,
    increment_template_usage,
    update_template,
    delete_template,
)
from ..helpers.sharing import (
    create_shared_link,
    get_shared_link,
    get_user_shared_links,
    revoke_shared_link,
    delete_shared_link,
    validate_shared_link,
)
from ..helpers.annotations import (
    create_annotation,
    get_annotation,
    get_resource_annotations,
    update_annotation,
    delete_annotation,
)
from ..helpers.versioning import (
    create_file_version,
    get_file_versions,
    revert_to_version,
    delete_file_version,
)
from ..helpers.api_auth import (
    create_api_key,
    validate_api_key,
    get_user_api_keys,
    revoke_api_key,
    delete_api_key,
    check_rate_limit,
)
from ..helpers.analytics import (
    get_user_analytics,
    get_project_analytics,
    get_usage_summary,
    get_team_analytics,
    get_daily_usage,
)
from ..helpers.permissions import (
    grant_permission,
    revoke_permission,
    check_permission,
    get_user_permissions,
    get_resource_permissions,
    update_permission_level,
)
from ..helpers.budget import (
    create_budget_alert,
    get_budget_alert,
    get_user_budget_alerts,
    get_team_budget_alerts,
    deactivate_budget_alert,
    reset_budget_alert,
    delete_budget_alert,
)
from ..helpers.collaboration import (
    create_collaboration_session,
    get_collaboration_session,
    get_active_sessions_for_resource,
    add_participant,
    remove_participant,
    end_collaboration_session,
    get_user_active_sessions,
)
from ..helpers.search import global_search, search_documentation
from ..helpers.documentation import (
    create_documentation_page,
    get_documentation_page,
    get_documentation_pages_by_category,
    get_all_categories,
    update_documentation_page,
    get_popular_pages,
    get_recent_pages,
)
from ..helpers.security import csrf_protect
from ..helpers.projects import get_project_assignment
from ..helpers.teams import get_team_membership
from ..helpers.databases import (
    import_from_pdb,
    import_from_uniprot,
    import_from_genbank,
    import_from_pubchem,
    DatabaseIntegrationError,
    PDBIntegration,
    UniProtIntegration,
    GenBankIntegration,
    PubChemIntegration,
)


# ============================================================================
# ACCESS HELPERS
# ============================================================================

_WRITE_ROLES = {PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR}

# URL / payload resource type -> table holding (project_id, creator_id)
_PROJECT_SCOPED_TABLES = {
    'file': 'files', 'files': 'files',
    'job': 'jobs', 'jobs': 'jobs',
}


def _forbidden(message='You do not have access to this resource'):
    return jsonify({'status': 'error', 'message': message}), 403


def _project_role(project_id):
    return get_project_assignment(get_db(), session['user_id'], project_id)


def _resource_access(resource_type, resource_id, user_id, write=False):
    """True when ``user_id`` may read (or write) the given resource.

    Unknown resource types are refused (fail closed).
    """
    try:
        resource_id = int(resource_id)
    except (TypeError, ValueError):
        return False
    db = get_db()
    rtype = str(resource_type or '').lower()
    if rtype in ('project', 'projects'):
        role = get_project_assignment(db, user_id, resource_id)
        return bool(role) and (not write or role in _WRITE_ROLES)
    if rtype in _PROJECT_SCOPED_TABLES:
        table = _PROJECT_SCOPED_TABLES[rtype]
        row = db.execute(
            f'SELECT project_id, creator_id FROM {table} WHERE id = %s', (resource_id,)
        ).fetchone()
        if not row:
            return False
        if row['creator_id'] == user_id:
            return True
        role = get_project_assignment(db, user_id, row['project_id'])
        return bool(role) and (not write or role in _WRITE_ROLES)
    if rtype in ('notebook', 'notebooks'):
        from .api_notebooks import check_notebook_access
        nb, _err = check_notebook_access(resource_id, user_id, require_write=write)
        return nb is not None
    return False


def _file_access(file_id, write=False):
    """Return (file_row, error_response). Same rule as download_file, plus a
    write role requirement for mutations."""
    db = get_db()
    user_id = session['user_id']
    row = db.execute(
        'SELECT id, path, project_id, creator_id FROM files WHERE id = %s', (file_id,)
    ).fetchone()
    if not row:
        return None, (jsonify({'status': 'error', 'message': 'File not found'}), 404)
    role = get_project_assignment(db, user_id, row['project_id'])
    is_creator = row['creator_id'] == user_id
    if not role and not is_creator:
        return None, _forbidden('You do not have permission to access this file')
    if write and not is_creator and role not in _WRITE_ROLES:
        return None, _forbidden('You do not have permission to modify this file')
    return row, None


def _same_file(row, supplied):
    """The client may only name the file's registered path, never another one."""
    if not row['path'] or not supplied:
        return False
    return os.path.realpath(supplied) == os.path.realpath(row['path'])


# ============================================================================
# JOB TEMPLATES API
# ============================================================================

@app.route('/api/templates', methods=['GET', 'POST'])
@login_required
@csrf_protect
def api_templates():
    """Get all templates or create a new template."""
    user_id = session['user_id']

    if request.method == 'GET':
        templates = get_user_templates(user_id, include_public=True)
        return jsonify({'status': 'success', 'templates': templates})

    # POST - Create new template
    data = request.get_json()
    name = data.get('name')
    model = data.get('model')
    config = data.get('config')
    description = data.get('description')
    project_id = data.get('project_id')
    team_id = data.get('team_id')
    is_public = data.get('is_public', False)

    if not all([name, model, config]):
        return jsonify({'status': 'error', 'message': 'Name, model, and config are required'}), 400
    if project_id and not _project_role(project_id):
        return _forbidden('You are not a member of this project')
    if team_id and not get_team_membership(get_db(), user_id, team_id):
        return _forbidden('You are not a member of this team')

    template = create_template(
        user_id, name, model, config, description, project_id, team_id, is_public
    )

    return jsonify({'status': 'success', 'template': template}), 201


@app.route('/api/templates/<template_id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
@csrf_protect
def api_template_detail(template_id):
    """Get, update, or delete a specific template."""
    user_id = session['user_id']
    template = get_template(template_id)

    if not template:
        return jsonify({'status': 'error', 'message': 'Template not found'}), 404

    if request.method == 'GET':
        db = get_db()
        visible = (
            template.get('creator_id') == user_id
            or template.get('is_public')
            or (template.get('project_id') and get_project_assignment(db, user_id, template['project_id']))
            or (template.get('team_id') and get_team_membership(db, user_id, template['team_id']))
        )
        if not visible:
            return jsonify({'status': 'error', 'message': 'Template not found'}), 404
        increment_template_usage(template_id)
        return jsonify({'status': 'success', 'template': template})

    # Mutations require ownership
    if template.get('creator_id') != user_id:
        return jsonify({'status': 'error', 'message': 'You do not have permission to modify this template'}), 403

    if request.method == 'PUT':
        data = request.get_json()
        success = update_template(
            template_id,
            name=data.get('name'),
            description=data.get('description'),
            config=data.get('config'),
            is_public=data.get('is_public'),
        )

        if success:
            return jsonify({'status': 'success', 'message': 'Template updated'})
        return jsonify({'status': 'error', 'message': 'Failed to update template'}), 500

    if request.method == 'DELETE':
        success = delete_template(template_id)
        if success:
            return jsonify({'status': 'success', 'message': 'Template deleted'})
        return jsonify({'status': 'error', 'message': 'Failed to delete template'}), 500


@app.route('/api/projects/<int:project_id>/templates', methods=['GET'])
@login_required
def api_project_templates(project_id):
    """Get all templates for a project."""
    if not _project_role(project_id):
        return _forbidden('You do not have access to this project')
    templates = get_project_templates(project_id)
    return jsonify({'status': 'success', 'templates': templates})


# ============================================================================
# SHARED LINKS API
# ============================================================================

@app.route('/api/share', methods=['POST'])
@login_required
@csrf_protect
def api_create_share_link():
    """Create a shared link for a resource."""
    data = request.get_json()
    resource_type = data.get('resource_type')
    resource_id = data.get('resource_id')
    permissions = data.get('permissions', 'read')
    expires_in_days = data.get('expires_in_days')

    if not all([resource_type, resource_id]):
        return jsonify({'status': 'error', 'message': 'Resource type and ID are required'}), 400

    user_id = session['user_id']
    if not _resource_access(resource_type, resource_id, user_id):
        return _forbidden()
    link = create_shared_link(resource_type, resource_id, user_id, permissions, expires_in_days)

    return jsonify({'status': 'success', 'link': link}), 201


@app.route('/api/share/<token>', methods=['GET', 'DELETE'])
@csrf_protect
def api_share_link_access(token):
    """Access or revoke a shared link."""
    if request.method == 'GET':
        is_valid, error_message = validate_shared_link(token)

        if not is_valid:
            return jsonify({'status': 'error', 'message': error_message}), 403

        link = get_shared_link(token)
        return jsonify({'status': 'success', 'link': link})

    if request.method == 'DELETE':
        # Requires authentication
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

        link = get_shared_link(token)
        if not link:
            return jsonify({'status': 'error', 'message': 'Link not found'}), 404
        if link.get('creator_id') != session['user_id']:
            return _forbidden('Only the creator can revoke this link')

        success = revoke_shared_link(token)
        if success:
            return jsonify({'status': 'success', 'message': 'Link revoked'})
        return jsonify({'status': 'error', 'message': 'Failed to revoke link'}), 500


@app.route('/api/share/my-links', methods=['GET'])
@login_required
def api_user_shared_links():
    """Get all shared links created by the current user."""
    user_id = session['user_id']
    links = get_user_shared_links(user_id)
    return jsonify({'status': 'success', 'links': links})


# ============================================================================
# ANNOTATIONS API
# ============================================================================

@app.route('/api/annotations', methods=['POST'])
@login_required
@csrf_protect
def api_create_annotation():
    """Create an annotation on a resource."""
    data = request.get_json()
    resource_type = data.get('resource_type')
    resource_id = data.get('resource_id')
    content = data.get('content')
    position_data = data.get('position_data')

    if not all([resource_type, resource_id, content]):
        return jsonify({'status': 'error', 'message': 'Resource type, ID, and content are required'}), 400

    user_id = session['user_id']
    if not _resource_access(resource_type, resource_id, user_id):
        return _forbidden()
    annotation = create_annotation(resource_type, resource_id, user_id, content, position_data)

    return jsonify({'status': 'success', 'annotation': annotation}), 201


@app.route('/api/annotations/<int:annotation_id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
@csrf_protect
def api_annotation_detail(annotation_id):
    """Get, update, or delete an annotation."""
    user_id = session['user_id']
    annotation = get_annotation(annotation_id)

    if not annotation:
        return jsonify({'status': 'error', 'message': 'Annotation not found'}), 404

    if not _resource_access(annotation.get('resource_type'), annotation.get('resource_id'), user_id):
        return jsonify({'status': 'error', 'message': 'Annotation not found'}), 404

    if request.method == 'GET':
        return jsonify({'status': 'success', 'annotation': annotation})

    # Mutations require ownership
    if annotation.get('user_id') != user_id:
        return jsonify({'status': 'error', 'message': 'You do not have permission to modify this annotation'}), 403

    if request.method == 'PUT':
        data = request.get_json()
        content = data.get('content')

        if not content:
            return jsonify({'status': 'error', 'message': 'Content is required'}), 400

        success = update_annotation(annotation_id, content)
        if success:
            return jsonify({'status': 'success', 'message': 'Annotation updated'})
        return jsonify({'status': 'error', 'message': 'Failed to update annotation'}), 500

    if request.method == 'DELETE':
        success = delete_annotation(annotation_id)
        if success:
            return jsonify({'status': 'success', 'message': 'Annotation deleted'})
        return jsonify({'status': 'error', 'message': 'Failed to delete annotation'}), 500


@app.route('/api/<resource_type>/<int:resource_id>/annotations', methods=['GET'])
@login_required
def api_resource_annotations(resource_type, resource_id):
    """Get all annotations for a resource."""
    if not _resource_access(resource_type, resource_id, session['user_id']):
        return _forbidden()
    annotations = get_resource_annotations(resource_type, resource_id)
    return jsonify({'status': 'success', 'annotations': annotations})


# ============================================================================
# FILE VERSIONING API
# ============================================================================

@app.route('/api/files/<int:file_id>/versions', methods=['GET', 'POST'])
@login_required
@csrf_protect
def api_file_versions(file_id):
    """Get all versions or create a new version of a file."""
    file_row, err = _file_access(file_id, write=request.method != 'GET')
    if err:
        return err

    if request.method == 'GET':
        versions = get_file_versions(file_id)
        return jsonify({'status': 'success', 'versions': versions})

    # POST - Create new version
    data = request.get_json() or {}
    file_path = data.get('file_path')

    if not file_path:
        return jsonify({'status': 'error', 'message': 'File path is required'}), 400
    if not _same_file(file_row, file_path):
        return jsonify({'status': 'error', 'message': 'file_path does not match this file'}), 400

    user_id = session['user_id']

    try:
        version = create_file_version(file_id, file_path, user_id)
        return jsonify({'status': 'success', 'version': version}), 201
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/files/<int:file_id>/versions/<int:version_number>', methods=['GET', 'DELETE'])
@login_required
@csrf_protect
def api_file_version_detail(file_id, version_number):
    """Get or delete a specific file version."""
    from ..helpers.versioning import get_file_version

    _row, err = _file_access(file_id, write=request.method == 'DELETE')
    if err:
        return err

    version = get_file_version(file_id, version_number)

    if not version:
        return jsonify({'status': 'error', 'message': 'Version not found'}), 404

    if request.method == 'GET':
        return jsonify({'status': 'success', 'version': version})

    if request.method == 'DELETE':
        success = delete_file_version(file_id, version_number)
        if success:
            return jsonify({'status': 'success', 'message': 'Version deleted'})
        return jsonify({'status': 'error', 'message': 'Failed to delete version'}), 500


@app.route('/api/files/<int:file_id>/revert', methods=['POST'])
@login_required
@csrf_protect
def api_file_revert(file_id):
    """Revert a file to a previous version."""
    file_row, err = _file_access(file_id, write=True)
    if err:
        return err

    data = request.get_json() or {}
    version_number = data.get('version_number')
    current_file_path = data.get('current_file_path')

    if not all([version_number, current_file_path]):
        return jsonify({'status': 'error', 'message': 'Version number and current file path are required'}), 400
    if not _same_file(file_row, current_file_path):
        return jsonify({'status': 'error', 'message': 'current_file_path does not match this file'}), 400

    user_id = session['user_id']

    try:
        success = revert_to_version(file_id, version_number, current_file_path, user_id)
        if success:
            return jsonify({'status': 'success', 'message': 'File reverted successfully'})
        return jsonify({'status': 'error', 'message': 'Failed to revert file'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ============================================================================
# API KEYS MANAGEMENT
# ============================================================================

@app.route('/api/api-keys', methods=['GET', 'POST'])
@login_required
@csrf_protect
def api_keys_management():
    """Get all API keys or create a new one."""
    user_id = session['user_id']

    if request.method == 'GET':
        keys = get_user_api_keys(user_id)
        return jsonify({'status': 'success', 'keys': keys})

    # POST - Create new API key
    data = request.get_json()
    name = data.get('name')
    scopes = data.get('scopes', [])
    expires_in_days = data.get('expires_in_days')

    if not name:
        return jsonify({'status': 'error', 'message': 'Name is required'}), 400

    plain_key, key_info = create_api_key(user_id, name, scopes, expires_in_days)

    return jsonify({
        'status': 'success',
        'api_key': plain_key,
        'key_info': key_info,
        'message': 'API key created. Save it securely - it will not be shown again.'
    }), 201


@app.route('/api/api-keys/<key_id>', methods=['DELETE'])
@login_required
@csrf_protect
def api_key_revoke(key_id):
    """Revoke an API key."""
    user_id = session['user_id']
    db = get_db()
    # Verify the key belongs to the authenticated user before revoking
    row = db.execute(
        'SELECT id FROM api_keys WHERE key_id = %s AND user_id = %s',
        (key_id, user_id),
    ).fetchone()
    if not row:
        return jsonify({'status': 'error', 'message': 'API key not found'}), 404
    success = revoke_api_key(key_id)
    if success:
        return jsonify({'status': 'success', 'message': 'API key revoked'})
    return jsonify({'status': 'error', 'message': 'Failed to revoke API key'}), 500


# Analytics logic moved to api_analytics.py


@app.route('/api/analytics/summary', methods=['GET'])
@login_required
def api_usage_summary():
    """Get usage summary for the current user."""
    user_id = session['user_id']
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if start_date:
        start_date = datetime.fromisoformat(start_date)
    if end_date:
        end_date = datetime.fromisoformat(end_date)

    summary = get_usage_summary(user_id, start_date, end_date)
    return jsonify({'status': 'success', 'summary': summary})


@app.route('/api/analytics/daily', methods=['GET'])
@login_required
def api_daily_usage():
    """Get daily usage statistics."""
    user_id = session['user_id']
    try:
        days = int(request.args.get('days', 30))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'days must be a number'}), 400
    days = max(1, min(days, 365))

    daily_stats = get_daily_usage(user_id, days)
    return jsonify({'status': 'success', 'daily_usage': daily_stats})


@app.route('/api/projects/<int:project_id>/analytics', methods=['GET'])
@login_required
def api_project_analytics(project_id):
    """Get analytics for a specific project."""
    if not _project_role(project_id):
        return _forbidden('You do not have access to this project')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if start_date:
        start_date = datetime.fromisoformat(start_date)
    if end_date:
        end_date = datetime.fromisoformat(end_date)

    analytics = get_project_analytics(project_id, start_date, end_date)
    return jsonify({'status': 'success', 'analytics': analytics})


@app.route('/api/teams/<int:team_id>/analytics', methods=['GET'])
@login_required
def api_team_analytics(team_id):
    """Get analytics for a team."""
    if not get_team_membership(get_db(), session['user_id'], team_id):
        return _forbidden('You are not a member of this team')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if start_date:
        start_date = datetime.fromisoformat(start_date)
    if end_date:
        end_date = datetime.fromisoformat(end_date)

    analytics = get_team_analytics(team_id, start_date, end_date)
    return jsonify({'status': 'success', 'analytics': analytics})


# ============================================================================
# PERMISSIONS API
# ============================================================================

@app.route('/api/projects/<int:project_id>/permissions', methods=['GET', 'POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER})
@csrf_protect
def api_project_permissions(project_id):
    """Get or grant project permissions."""
    if request.method == 'GET':
        user_id = request.args.get('user_id')
        resource_type = request.args.get('resource_type')
        resource_id = request.args.get('resource_id')

        if user_id:
            permissions = get_user_permissions(int(user_id), project_id)
        elif resource_type:
            permissions = get_resource_permissions(project_id, resource_type, resource_id)
        else:
            return jsonify({'status': 'error', 'message': 'user_id or resource_type is required'}), 400

        return jsonify({'status': 'success', 'permissions': permissions})

    # POST - Grant permission
    data = request.get_json()
    user_id_to_grant = data.get('user_id')
    resource_type = data.get('resource_type')
    resource_id = data.get('resource_id')
    permission_level = data.get('permission_level')

    if not all([user_id_to_grant, resource_type, permission_level]):
        return jsonify({'status': 'error', 'message': 'user_id, resource_type, and permission_level are required'}), 400

    granted_by = session['user_id']
    permission = grant_permission(
        project_id, user_id_to_grant, resource_type, permission_level, granted_by, resource_id
    )

    return jsonify({'status': 'success', 'permission': permission}), 201


@app.route('/api/permissions/<int:permission_id>', methods=['PUT', 'DELETE'])
@login_required
@csrf_protect
def api_permission_detail(permission_id):
    """Update or revoke a permission (project managers only)."""
    row = get_db().execute(
        'SELECT project_id FROM project_permissions WHERE id = %s', (permission_id,)
    ).fetchone()
    if not row:
        return jsonify({'status': 'error', 'message': 'Permission not found'}), 404
    if _project_role(row['project_id']) != PROJECT_ROLE_MANAGER:
        return _forbidden('You do not have the required permissions for this project')

    if request.method == 'PUT':
        data = request.get_json()
        new_level = data.get('permission_level')

        if not new_level:
            return jsonify({'status': 'error', 'message': 'permission_level is required'}), 400

        success = update_permission_level(permission_id, new_level)
        if success:
            return jsonify({'status': 'success', 'message': 'Permission updated'})
        return jsonify({'status': 'error', 'message': 'Failed to update permission'}), 500

    if request.method == 'DELETE':
        success = revoke_permission(permission_id)
        if success:
            return jsonify({'status': 'success', 'message': 'Permission revoked'})
        return jsonify({'status': 'error', 'message': 'Failed to revoke permission'}), 500


# ============================================================================
# BUDGET ALERTS API
# ============================================================================

@app.route('/api/budget-alerts', methods=['GET', 'POST'])
@login_required
@csrf_protect
def api_budget_alerts():
    """Get all budget alerts or create a new one."""
    user_id = session['user_id']

    if request.method == 'GET':
        alerts = get_user_budget_alerts(user_id)
        return jsonify({'status': 'success', 'alerts': alerts})

    # POST - Create budget alert
    data = request.get_json()
    alert_type = data.get('alert_type')
    threshold_value = data.get('threshold_value')
    team_id = data.get('team_id')

    if not all([alert_type, threshold_value]):
        return jsonify({'status': 'error', 'message': 'alert_type and threshold_value are required'}), 400

    if team_id and not get_team_membership(get_db(), user_id, team_id):
        return _forbidden('You are not a member of this team')

    alert = create_budget_alert(alert_type, threshold_value, user_id if not team_id else None, team_id)

    return jsonify({'status': 'success', 'alert': alert}), 201


@app.route('/api/budget-alerts/<int:alert_id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
@csrf_protect
def api_budget_alert_detail(alert_id):
    """Get, update, or delete a budget alert."""
    user_id = session['user_id']
    alert = get_budget_alert(alert_id)

    if not alert:
        return jsonify({'status': 'error', 'message': 'Alert not found'}), 404

    # Verify ownership — alert must belong to this user
    if alert.get('user_id') != user_id:
        return jsonify({'status': 'error', 'message': 'Alert not found'}), 404

    if request.method == 'GET':
        return jsonify({'status': 'success', 'alert': alert})

    if request.method == 'PUT':
        data = request.get_json()
        action = data.get('action')

        if action == 'deactivate':
            success = deactivate_budget_alert(alert_id)
        elif action == 'reset':
            success = reset_budget_alert(alert_id)
        else:
            return jsonify({'status': 'error', 'message': 'Invalid action'}), 400

        if success:
            return jsonify({'status': 'success', 'message': f'Alert {action}d'})
        return jsonify({'status': 'error', 'message': f'Failed to {action} alert'}), 500

    if request.method == 'DELETE':
        success = delete_budget_alert(alert_id)
        if success:
            return jsonify({'status': 'success', 'message': 'Alert deleted'})
        return jsonify({'status': 'error', 'message': 'Failed to delete alert'}), 500


# ============================================================================
# COLLABORATION SESSIONS API
# ============================================================================

@app.route('/api/collaboration/sessions', methods=['POST'])
@login_required
@csrf_protect
def api_create_collaboration_session():
    """Create a new collaboration session."""
    from ..helpers.projects import get_project_assignment

    data = request.get_json()
    resource_type = data.get('resource_type')
    resource_id = data.get('resource_id')

    if not all([resource_type, resource_id]):
        return jsonify({'status': 'error', 'message': 'resource_type and resource_id are required'}), 400

    user_id = session['user_id']
    db = get_db()

    # Verify user has access to the resource
    if not _resource_access(resource_type, resource_id, user_id):
        return _forbidden('You do not have access to this resource')

    collab_session = create_collaboration_session(resource_type, resource_id, user_id)

    return jsonify({'status': 'success', 'session': collab_session}), 201


@app.route('/api/collaboration/sessions/<session_id>', methods=['GET', 'DELETE'])
@login_required
@csrf_protect
def api_collaboration_session_detail(session_id):
    """Get or end a collaboration session."""
    user_id = session['user_id']
    collab_session = get_collaboration_session(session_id)

    if not collab_session:
        return jsonify({'status': 'error', 'message': 'Session not found'}), 404

    if request.method == 'GET':
        if not _resource_access(collab_session.get('resource_type'), collab_session.get('resource_id'), user_id):
            return jsonify({'status': 'error', 'message': 'Session not found'}), 404
        return jsonify({'status': 'success', 'session': collab_session})

    if request.method == 'DELETE':
        # Only the session creator can end it
        if collab_session.get('host_user_id', collab_session.get('created_by')) != user_id:
            return jsonify({'status': 'error', 'message': 'You do not have permission to end this session'}), 403
        success = end_collaboration_session(session_id)
        if success:
            return jsonify({'status': 'success', 'message': 'Session ended'})
        return jsonify({'status': 'error', 'message': 'Failed to end session'}), 500


@app.route('/api/collaboration/sessions/<session_id>/join', methods=['POST'])
@login_required
@csrf_protect
def api_join_collaboration_session(session_id):
    """Join a collaboration session."""
    from ..helpers.projects import get_project_assignment

    user_id = session['user_id']
    db = get_db()

    # Get the session to check resource access
    collab_session = get_collaboration_session(session_id)
    if not collab_session:
        return jsonify({'status': 'error', 'message': 'Session not found'}), 404

    # Verify user has access to the resource
    resource_type = collab_session.get('resource_type')
    resource_id = collab_session.get('resource_id')

    if not _resource_access(resource_type, resource_id, user_id):
        return _forbidden('You do not have access to this resource')

    success = add_participant(session_id, user_id)

    if success:
        return jsonify({'status': 'success', 'message': 'Joined session'})
    return jsonify({'status': 'error', 'message': 'Failed to join session or already a participant'}), 400


@app.route('/api/collaboration/sessions/<session_id>/leave', methods=['POST'])
@login_required
@csrf_protect
def api_leave_collaboration_session(session_id):
    """Leave a collaboration session."""
    user_id = session['user_id']
    success = remove_participant(session_id, user_id)

    if success:
        return jsonify({'status': 'success', 'message': 'Left session'})
    return jsonify({'status': 'error', 'message': 'Failed to leave session'}), 500


@app.route('/api/collaboration/my-sessions', methods=['GET'])
@login_required
def api_user_collaboration_sessions():
    """Get all active collaboration sessions for the current user."""
    user_id = session['user_id']
    sessions = get_user_active_sessions(user_id)
    return jsonify({'status': 'success', 'sessions': sessions})


# ============================================================================
# GLOBAL SEARCH API
# ============================================================================

@app.route('/api/search', methods=['GET'])
@login_required
def api_global_search():
    """Perform a global search across all resources."""
    query = request.args.get('q', '')
    categories = request.args.getlist('categories')

    if not query:
        return jsonify({'status': 'error', 'message': 'Query parameter "q" is required'}), 400

    user_id = session['user_id']
    results = global_search(user_id, query, categories if categories else None)

    return jsonify({'status': 'success', 'results': results})


@app.route('/api/search/documentation', methods=['GET'])
@login_required
def api_search_documentation():
    """Search documentation pages."""
    query = request.args.get('q', '')
    limit = int(request.args.get('limit', 20))

    if not query:
        return jsonify({'status': 'error', 'message': 'Query parameter "q" is required'}), 400

    results = search_documentation(query, limit)
    return jsonify({'status': 'success', 'results': results})


# ============================================================================
# DOCUMENTATION API
# ============================================================================

@app.route('/api/documentation', methods=['GET', 'POST'])
@login_required
@csrf_protect
def api_documentation():
    """Get all documentation pages or create a new one."""
    if request.method == 'GET':
        category = request.args.get('category')

        if category:
            pages = get_documentation_pages_by_category(category)
        else:
            # Return popular and recent pages
            popular = get_popular_pages(limit=5)
            recent = get_recent_pages(limit=5)
            categories = get_all_categories()

            return jsonify({
                'status': 'success',
                'popular': popular,
                'recent': recent,
                'categories': categories
            })

        return jsonify({'status': 'success', 'pages': pages})

    # POST - Create new documentation page (admin only)
    return _create_documentation_page()


@admin_required
def _create_documentation_page():
    """Create new documentation page (admin only)."""
    data = request.get_json()
    title = data.get('title')
    content = data.get('content')
    category = data.get('category')
    slug = data.get('slug')

    if not all([title, content, category]):
        return jsonify({'status': 'error', 'message': 'title, content, and category are required'}), 400

    page = create_documentation_page(title, content, category, slug)
    return jsonify({'status': 'success', 'page': page}), 201


@app.route('/api/documentation/<slug>', methods=['GET', 'PUT', 'DELETE'])
@login_required
@csrf_protect
def api_documentation_page(slug):
    """Get, update, or delete a documentation page."""
    if request.method != 'GET':
        # PUT and DELETE are admin-only; check before looking at the page.
        return _documentation_mutation(slug)

    page = get_documentation_page(slug, increment_view=True)

    if not page:
        return jsonify({'status': 'error', 'message': 'Page not found'}), 404

    return jsonify({'status': 'success', 'page': page})


@admin_required
def _documentation_mutation(slug):
    if not get_documentation_page(slug, increment_view=False):
        return jsonify({'status': 'error', 'message': 'Page not found'}), 404
    if request.method == 'PUT':
        return _update_documentation_page(slug)
    return _delete_documentation_page(slug)


@admin_required
def _update_documentation_page(slug):
    """Update documentation page (admin only)."""
    data = request.get_json()
    success = update_documentation_page(
        slug,
        title=data.get('title'),
        content=data.get('content'),
        category=data.get('category'),
    )

    if success:
        return jsonify({'status': 'success', 'message': 'Page updated'})
    return jsonify({'status': 'error', 'message': 'Failed to update page'}), 500


@admin_required
def _delete_documentation_page(slug):
    """Delete documentation page (admin only)."""
    from ..helpers.documentation import delete_documentation_page
    success = delete_documentation_page(slug)
    if success:
        return jsonify({'status': 'success', 'message': 'Page deleted'})
    return jsonify({'status': 'error', 'message': 'Failed to delete page'}), 500


# ============================================================================
# DATABASE INTEGRATION API
# ============================================================================

@app.route('/api/import/pdb/<pdb_id>', methods=['GET'])
@login_required
def api_import_pdb(pdb_id):
    """Import structure from PDB."""
    format_type = request.args.get('format', 'pdb')

    try:
        data = import_from_pdb(pdb_id, format_type)
        return jsonify({'status': 'success', 'data': data})
    except DatabaseIntegrationError as e:
        error_msg = str(e)
        # Return 404 for not found errors
        if '404' in error_msg or 'not found' in error_msg.lower():
            return jsonify({'status': 'error', 'message': f'PDB structure {pdb_id} not found', 'code': 'NOT_FOUND'}), 404
        return jsonify({'status': 'error', 'message': error_msg}), 500


@app.route('/api/import/uniprot/<uniprot_id>', methods=['GET'])
@login_required
def api_import_uniprot(uniprot_id):
    """Import protein data from UniProt."""
    try:
        data = import_from_uniprot(uniprot_id)
        return jsonify({'status': 'success', 'data': data})
    except DatabaseIntegrationError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/import/genbank/<genbank_id>', methods=['GET'])
@login_required
def api_import_genbank(genbank_id):
    """Import sequence from GenBank."""
    db_type = request.args.get('db', 'nucleotide')

    try:
        data = import_from_genbank(genbank_id, db_type)
        return jsonify({'status': 'success', 'data': data})
    except DatabaseIntegrationError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/import/pubchem/<cid>', methods=['GET'])
@login_required
def api_import_pubchem(cid):
    """Import compound data from PubChem."""
    try:
        data = import_from_pubchem(cid)
        return jsonify({'status': 'success', 'data': data})
    except DatabaseIntegrationError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/database/search', methods=['GET'])
@login_required
def api_database_search():
    """Search scientific databases."""
    database = request.args.get('database')
    query = request.args.get('query')

    if not database or not query:
        return jsonify({'status': 'error', 'message': 'database and query parameters required'}), 400

    try:
        results = []

        if database == 'pdb':
            # Real PDB search
            search_results = PDBIntegration.search(query, limit=20)
            for result in search_results:
                pdb_id = result.get('identifier', '')
                results.append({
                    'id': pdb_id,
                    'title': result.get('title', pdb_id),
                    'description': f"PDB Entry: {pdb_id}"
                })

        elif database == 'uniprot':
            # Real UniProt search
            search_results = UniProtIntegration.search(query, limit=20)
            for result in search_results:
                uniprot_id = result.get('primaryAccession', '')
                protein_name = result.get('proteinDescription', {}).get('recommendedName', {}).get('fullName', {}).get('value', '')
                organism = result.get('organism', {}).get('scientificName', '')
                results.append({
                    'id': uniprot_id,
                    'title': protein_name or uniprot_id,
                    'description': f"{organism}" if organism else f"UniProt Entry: {uniprot_id}"
                })

        elif database == 'genbank':
            # Real GenBank search
            search_results = GenBankIntegration.search(query, limit=20)
            for result in search_results:
                gb_id = result.get('accessionversion', result.get('uid', ''))
                title = result.get('title', gb_id)
                results.append({
                    'id': gb_id,
                    'title': title,
                    'description': f"GenBank Entry: {gb_id}"
                })

        elif database == 'pubchem':
            # Real PubChem search
            search_results = PubChemIntegration.search_by_name(query, limit=20)
            for result in search_results:
                compound_data = result.get('PC_Compounds', [{}])[0] if 'PC_Compounds' in result else {}
                cid = compound_data.get('id', {}).get('id', {}).get('cid', '')
                props = compound_data.get('props', [])
                title = query
                # Try to get compound name from properties
                for prop in props:
                    if prop.get('urn', {}).get('label', '') == 'IUPAC Name':
                        title = prop.get('value', {}).get('sval', query)
                        break
                results.append({
                    'id': str(cid),
                    'title': title,
                    'description': f"PubChem CID: {cid}"
                })

        return jsonify({'status': 'success', 'results': results})

    except DatabaseIntegrationError as e:
        logger.error(f'Database search error: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500
    except Exception as e:
        logger.error(f'Unexpected database search error: {e}')
        return jsonify({'status': 'error', 'message': 'Search failed'}), 500


@app.route('/api/database/import', methods=['POST'])
@login_required
@project_role_required({PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR})
@csrf_protect
def api_database_import():
    """Import entries from scientific databases to a project."""
    data = request.get_json()
    database = data.get('database')
    entries = data.get('entries', [])
    project_id = data.get('project_id')

    if not database or not entries or not project_id:
        return jsonify({'status': 'error', 'message': 'database, entries, and project_id required'}), 400

    imported_count = 0
    failed_count = 0
    imported_files = []

    from ..helpers import get_safe_project_path
    import os

    try:
        project_path = get_safe_project_path(project_id, session['user_id'])
        if not project_path:
            return jsonify({'status': 'error', 'message': 'Invalid project'}), 404

        # Create import directory
        import_dir = os.path.join(project_path, 'imports')
        os.makedirs(import_dir, exist_ok=True)

        for entry_id in entries:
            try:
                file_content = None
                filename = None
                file_type = None
                metadata = {}

                # Import based on database type
                if database == 'pdb':
                    result = import_from_pdb(entry_id, 'pdb')
                    file_content = result['structure_data']  # Extract the actual PDB file content
                    filename = f"{entry_id}.pdb"
                    file_type = 'pdb'
                    metadata = {'source': 'pdb', 'pdb_id': entry_id}

                elif database == 'uniprot':
                    result = import_from_uniprot(entry_id)
                    file_content = result['sequence']  # Extract the FASTA content
                    filename = f"{entry_id}.fasta"
                    file_type = 'fasta'
                    metadata = {'source': 'uniprot', 'uniprot_id': entry_id}

                elif database == 'genbank':
                    result = import_from_genbank(entry_id, 'nucleotide')
                    file_content = result['sequence']  # Extract the FASTA sequence
                    filename = f"{entry_id}.fasta"
                    file_type = 'fasta'
                    metadata = {'source': 'genbank', 'genbank_id': entry_id}

                elif database == 'pubchem':
                    result = import_from_pubchem(entry_id)
                    # For PubChem, we'll save the SMILES string
                    file_content = result.get('smiles', '')
                    if not file_content:
                        failed_count += 1
                        continue
                    filename = f"{entry_id}.smi"
                    file_type = 'smi'
                    metadata = {'source': 'pubchem', 'cid': entry_id}

                else:
                    failed_count += 1
                    continue

                if not file_content:
                    failed_count += 1
                    continue

                # Save to project
                file_path = os.path.join(import_dir, filename)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(file_content)

                # Get file size
                file_size = os.path.getsize(file_path)

                # Register file in database
                db = get_db()
                cursor = db.execute(
                    '''INSERT INTO input_files (name, type, size, created_at, project_id, notes)
                       VALUES (%s, %s, %s, NOW(), %s, %s)''',
                    (filename, file_type, file_size, project_id, f"Imported from {database.upper()}")
                )
                db.commit()

                imported_files.append({
                    'id': cursor.fetchone()['id'],
                    'name': filename,
                    'type': file_type,
                    'size': file_size,
                    'source': database
                })

                imported_count += 1

            except DatabaseIntegrationError as e:
                logger.error(f'Failed to import {entry_id} from {database}: {e}')
                failed_count += 1
            except Exception as e:
                logger.error(f'Unexpected error importing {entry_id} from {database}: {e}')
                failed_count += 1

        return jsonify({
            'status': 'success',
            'imported_count': imported_count,
            'failed_count': failed_count,
            'files': imported_files
        })

    except Exception as e:
        logger.error(f'Database import error: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500
