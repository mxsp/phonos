"""
API routes for public database integration.
Allows searching and importing files from PDB, AlphaFold, UniProt, PubChem, etc.
"""

import os
import logging
from flask import jsonify, request, session

logger = logging.getLogger(__name__)

# Lazy load database manager
_db_manager = None

def get_db_manager():
    """Lazy initialization of database manager to avoid circular imports."""
    global _db_manager
    if _db_manager is None:
        from phonos_app.helpers.public_databases import PublicDatabaseManager
        _db_manager = PublicDatabaseManager()
    return _db_manager


def register_database_routes(app):
    """Register all public database integration routes."""

    @app.route('/api/databases/available', methods=['GET'])
    def get_available_databases():
        """Get list of available public databases."""
        from phonos_app.helpers.auth import login_required

        @login_required
        def _handler():
            try:
                try:
                    db_manager = get_db_manager()
                except Exception as e:
                    logger.error(f"Failed to initialize database manager: {e}", exc_info=True)
                    return jsonify({'status': 'error', 'message': 'Database integration is not available.'}), 503

                databases = db_manager.get_available_databases()
                return jsonify(databases), 200
            except Exception as e:
                logger.error(f"Error fetching available databases: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': 'Failed to fetch databases.'}), 500

        return _handler()

    @app.route('/api/databases/<database>/search', methods=['GET'])
    def search_database(database):
        """Search a public database."""
        from phonos_app.helpers.auth import login_required

        @login_required
        def _handler():
            try:
                query = request.args.get('query', '')
                page = max(1, int(request.args.get('page', 1)))
                limit = min(100, max(1, int(request.args.get('limit', 20))))

                if not query:
                    return jsonify({'status': 'error', 'message': 'Query parameter is required.'}), 400

                db_manager = get_db_manager()

                # Fetch more results to enable pagination
                # Most external APIs don't support offset, so we fetch extra and slice
                fetch_limit = min(limit * page, 100)
                all_results = db_manager.search(database, query, fetch_limit)

                # Calculate pagination
                offset = (page - 1) * limit
                paginated_results = all_results[offset:offset + limit]
                total_count = len(all_results)
                total_pages = (total_count + limit - 1) // limit

                return jsonify({
                    'status': 'success',
                    'database': database,
                    'query': query,
                    'count': len(paginated_results),
                    'results': paginated_results,
                    'pagination': {
                        'page': page,
                        'limit': limit,
                        'total': total_count,
                        'total_pages': total_pages,
                        'has_next': page < total_pages,
                        'has_prev': page > 1
                    }
                }), 200

            except ValueError as e:
                return jsonify({'status': 'error', 'message': str(e)}), 400
            except Exception as e:
                logger.error(f"Error searching {database}: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': f'Search failed: {str(e)}'}), 500

        return _handler()

    @app.route('/api/databases/<database>/import', methods=['POST'])
    def import_from_database(database):
        """Import a file from a public database directly into a project."""
        from phonos_app.helpers.auth import login_required
        from phonos_app.helpers.security import csrf_protect, sanitize_html
        from phonos_app.helpers.database import get_db
        from phonos_app.helpers.projects import (
            get_project_assignment,
            PROJECT_ROLE_CONTRIBUTOR,
            PROJECT_ROLE_MANAGER,
        )
        from phonos_app.helpers.paths import get_safe_project_path
        from phonos_app.helpers.file_scanner import scan_upload
        from phonos_app.helpers.storage import format_storage_bytes, get_user_storage_state
        from werkzeug.utils import secure_filename
        from datetime import datetime

        @login_required
        @csrf_protect
        def _handler():
            try:
                db = get_db()
                user_id = session['user_id']

                data = request.get_json()
                if not data:
                    return jsonify({'status': 'error', 'message': 'No data provided.'}), 400

                project_id = data.get('project_id')
                identifier = data.get('identifier')
                file_format = data.get('file_format')
                notes = data.get('notes')

                if not project_id or not identifier:
                    return jsonify({'status': 'error', 'message': 'project_id and identifier are required.'}), 400

                # Check project permissions
                role = get_project_assignment(db, user_id, project_id)
                if not role or role not in [PROJECT_ROLE_MANAGER, PROJECT_ROLE_CONTRIBUTOR]:
                    return jsonify({'status': 'error', 'message': 'You do not have permission to import files to this project.'}), 403

                # Get project info
                project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
                if not project:
                    return jsonify({'status': 'error', 'message': 'Project not found.'}), 404

                # Get project path
                project_path = get_safe_project_path(project_id, project['team_id'])
                if not project_path:
                    return jsonify({'status': 'error', 'message': 'Invalid project path.'}), 404

                # Create database-imports directory
                target_dir = os.path.join(project_path, 'database-imports')
                os.makedirs(target_dir, exist_ok=True)

                # Fetch file from database
                logger.info(f"Fetching {identifier} from {database} for user {user_id}")
                db_manager = get_db_manager()
                file_content, original_filename = db_manager.fetch_file(database, identifier, file_format)

                # Generate safe filename
                filename = secure_filename(original_filename)
                file_path = os.path.join(target_dir, filename)

                # Check if file already exists
                if os.path.exists(file_path):
                    # Add timestamp to make unique
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    base, ext = os.path.splitext(filename)
                    filename = f"{base}_{timestamp}{ext}"
                    file_path = os.path.join(target_dir, filename)

                # Check storage quota
                file_size = len(file_content)
                storage_state = get_user_storage_state(db, user_id)

                if storage_state['available'] <= 0:
                    return jsonify({
                        'status': 'error',
                        'message': 'You have no storage remaining.',
                        'storage': storage_state
                    }), 507

                if file_size > storage_state['available']:
                    return jsonify({
                        'status': 'error',
                        'message': (
                            f"This file requires {format_storage_bytes(file_size)}, "
                            f"but only {format_storage_bytes(storage_state['available'])} is available."
                        ),
                        'storage': storage_state
                    }), 507

                # Save to temporary location first
                temp_path = file_path + '.tmp'
                with open(temp_path, 'wb') as f:
                    f.write(file_content)

                # Scan the file for security
                is_safe, message = scan_upload(temp_path, filename, user_id, request.remote_addr)
                if not is_safe:
                    return jsonify({'status': 'error', 'message': message}), 400

                # Move to final location
                os.rename(temp_path, file_path)

                # Add metadata notes
                metadata_notes = f"Imported from {database.upper()}: {identifier}"
                if notes:
                    sanitized_notes = sanitize_html(notes)
                    metadata_notes = f"{metadata_notes}\n\n{sanitized_notes}"
                else:
                    metadata_notes = sanitize_html(metadata_notes)

                # Insert into database
                db.execute('BEGIN')
                try:
                    cursor = db.execute(
                        'INSERT INTO files (project_id, creator_id, name, path, size, notes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
                        (project_id, user_id, filename, file_path, file_size, metadata_notes)
                    )
                    file_id = cursor.fetchone()['id']
                    db.execute('UPDATE users SET storage_used = storage_used + %s WHERE id = %s', (file_size, user_id))
                    db.commit()

                    new_file = db.execute(
                        "SELECT id, name as filename, size, created_at, notes, project_id, 'upload' as type FROM files WHERE id = %s",
                        (file_id,)
                    ).fetchone()

                    file_dict = dict(new_file)
                    if file_dict.get('created_at') and hasattr(file_dict['created_at'], 'isoformat'):
                        file_dict['created_at'] = file_dict['created_at'].isoformat()

                    logger.info(f"Successfully imported {identifier} from {database} to project {project_id}")

                    return jsonify({
                        'status': 'success',
                        'message': f'Successfully imported {filename} from {database.upper()}',
                        'file': file_dict,
                        'source': {
                            'database': database,
                            'identifier': identifier,
                            'format': file_format or 'default'
                        }
                    }), 201

                except Exception as e:
                    db.rollback()
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    logger.error(f"Failed to save imported file to database: {e}", exc_info=True)
                    return jsonify({'status': 'error', 'message': 'Failed to save imported file.'}), 500

            except ValueError as e:
                return jsonify({'status': 'error', 'message': str(e)}), 400
            except Exception as e:
                logger.error(f"Error importing from {database}: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': f'Import failed: {str(e)}'}), 500

        return _handler()

    @app.route('/api/databases/<database>/preview/<identifier>', methods=['GET'])
    def preview_database_entry(database, identifier):
        """Get preview/metadata for a database entry without downloading."""
        from phonos_app.helpers.auth import login_required

        @login_required
        def _handler():
            try:
                db_manager = get_db_manager()
                connector = db_manager.get_connector(database)
                if not connector:
                    return jsonify({'status': 'error', 'message': f'Unknown database: {database}'}), 400

                metadata = connector.get_metadata(identifier)

                return jsonify({
                    'status': 'success',
                    'database': database,
                    'identifier': identifier,
                    'metadata': metadata
                }), 200

            except Exception as e:
                logger.error(f"Error previewing {identifier} from {database}: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': f'Preview failed: {str(e)}'}), 500

        return _handler()

    @app.route('/api/databases/<database>/content/<identifier>', methods=['GET'])
    def fetch_database_content(database, identifier):
        """Fetch file content from a public database for preview (returns JSON)."""
        from phonos_app.helpers.auth import login_required

        @login_required
        def _handler():
            try:
                file_format = request.args.get('format', 'pdb')
                db_manager = get_db_manager()

                # Fetch content directly
                content, filename = db_manager.fetch_file(database, identifier, file_format)

                # Decode bytes to string for JSON response
                try:
                    content_str = content.decode('utf-8')
                except (UnicodeDecodeError, AttributeError):
                    content_str = content.decode('latin-1') if isinstance(content, bytes) else str(content)

                return jsonify({
                    'status': 'success',
                    'content': content_str,
                    'filename': filename,
                    'format': file_format
                }), 200

            except Exception as e:
                logger.error(f"Error fetching content for {identifier} from {database}: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': f'Fetch failed: {str(e)}'}), 500

        return _handler()

    @app.route('/api/databases/<database>/<identifier>/download', methods=['GET'])
    def download_database_entry(database, identifier):
        """Download a file from a public database."""
        from phonos_app.helpers.auth import login_required
        from flask import Response

        @login_required
        def _handler():
            try:
                file_format = request.args.get('format')
                db_manager = get_db_manager()
                
                content, filename = db_manager.fetch_file(database, identifier, file_format)
                
                return Response(content, mimetype='application/octet-stream', headers={
                    'Content-Disposition': f'attachment; filename="{filename}"'
                })

            except Exception as e:
                logger.error(f"Error downloading {identifier} from {database}: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': f'Download failed: {str(e)}'}), 500

        return _handler()

    @app.route('/api/databases/uniprot/<uniprot_id>/fasta', methods=['GET'])
    def proxy_uniprot_fasta(uniprot_id):
        """Proxy UniProt FASTA requests to avoid CORS issues."""
        from phonos_app.helpers.auth import login_required
        from flask import Response
        import requests
        
        @login_required
        def _handler():
            try:
                db_manager = get_db_manager()
                # Use UniProtConnector directly or via manager
                content, filename = db_manager.fetch_file('uniprot', uniprot_id, 'fasta')
                
                return Response(content, mimetype='text/plain', headers={
                    'Content-Disposition': f'inline; filename="{filename}"'
                })
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return jsonify({'status': 'error', 'message': 'UniProt ID not found'}), 404
                logger.error(f"UniProt upstream error for {uniprot_id}: {e}")
                return jsonify({'status': 'error', 'message': f'UniProt error: {e.response.status_code}'}), e.response.status_code
            except Exception as e:
                logger.error(f"Error proxying UniProt FASTA for {uniprot_id}: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500
        
        return _handler()

    @app.route('/api/databases/stats', methods=['GET'])
    def get_database_import_stats():
        """Get statistics on imported database files for the current user."""
        from phonos_app.helpers.auth import login_required
        from phonos_app.helpers.database import get_db

        @login_required
        def _handler():
            try:
                db = get_db()
                user_id = session['user_id']

                # Query files with database import metadata
                stats = db.execute("""
                    SELECT
                        COUNT(*) as total_imports,
                        SUM(size) as total_size,
                        COUNT(DISTINCT project_id) as projects_with_imports
                    FROM files
                    WHERE creator_id = %s
                    AND notes LIKE 'Imported from%%'
                """, (user_id,)).fetchone()

                # Get breakdown by database
                breakdown = db.execute("""
                    SELECT
                        CASE
                            WHEN notes LIKE 'Imported from PDB:%%' THEN 'PDB'
                            WHEN notes LIKE 'Imported from ALPHAFOLD:%%' THEN 'AlphaFold'
                            WHEN notes LIKE 'Imported from UNIPROT:%%' THEN 'UniProt'
                            WHEN notes LIKE 'Imported from PUBCHEM:%%' THEN 'PubChem'
                            ELSE 'Other'
                        END as database,
                        COUNT(*) as count,
                        SUM(size) as total_size
                    FROM files
                    WHERE creator_id = %s
                    AND notes LIKE 'Imported from%%'
                    GROUP BY database
                    ORDER BY count DESC
                """, (user_id,)).fetchall()

                return jsonify({
                    'status': 'success',
                    'stats': dict(stats) if stats else {},
                    'breakdown': [dict(row) for row in breakdown]
                }), 200

            except Exception as e:
                logger.error(f"Error fetching database import stats: {e}", exc_info=True)
                return jsonify({'status': 'error', 'message': 'Failed to fetch stats.'}), 500

        return _handler()
