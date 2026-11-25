"""
API Search Routes for Webapp Rework
Provides database lookup and search functionality for:
- Local file search
- PubChem compound search
- PDB structure search
"""

import os
import json
import psycopg2
import urllib.request
import urllib.parse
import re
from flask import jsonify, request, session
from ..app import app, logger
from ..helpers import login_required, get_db, get_safe_project_path
from ..helpers.public_databases import PDBConnector, AlphaFoldConnector, UniProtConnector, PubChemConnector, PublicDatabaseManager
from config import Config


@app.route('/api/search/files', methods=['GET'])
@login_required
def api_search_files():
    """Search through user's project files for matches with pagination and filters"""
    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) < 2:
            return jsonify({'status': 'error', 'message': 'Query too short'}), 400

        # Pagination parameters
        page = max(1, int(request.args.get('page', 1)))
        limit = min(100, max(1, int(request.args.get('limit', 20))))
        offset = (page - 1) * limit

        # Filter parameters
        file_type = request.args.get('file_type', '').strip()
        project_id = request.args.get('project_id', '').strip()
        date_from = request.args.get('date_from', '').strip()
        date_to = request.args.get('date_to', '').strip()

        user_id = session['user_id']
        db = get_db()

        results = []

        # Build dynamic WHERE clause for filters
        where_conditions = ['pa.user_id = %s', 'f.filename LIKE %s']
        params = [user_id, f'%{query}%']

        if file_type:
            where_conditions.append('f.file_type = %s')
            params.append(file_type)

        if project_id:
            where_conditions.append('f.project_id = %s')
            params.append(int(project_id))

        if date_from:
            where_conditions.append('f.uploaded_at >= %s')
            params.append(date_from)

        if date_to:
            where_conditions.append('f.uploaded_at <= %s')
            params.append(date_to)

        where_clause = ' AND '.join(where_conditions)

        # Get total count for pagination
        count_query = f'''SELECT COUNT(*) as count
               FROM project_files f
               JOIN projects p ON f.project_id = p.id
               JOIN project_assignments pa ON p.id = pa.project_id
               WHERE {where_clause}'''
        total_files = db.execute(count_query, tuple(params)).fetchone()['count']

        # Search in file names with pagination and filters
        search_query = f'''SELECT f.id, f.filename, f.file_type, f.file_size, f.uploaded_at,
                      f.project_id, p.name as project_name
               FROM project_files f
               JOIN projects p ON f.project_id = p.id
               JOIN project_assignments pa ON p.id = pa.project_id
               WHERE {where_clause}
               ORDER BY f.uploaded_at DESC
               LIMIT %s OFFSET %s'''
        file_rows = db.execute(search_query, tuple(params + [limit, offset])).fetchall()

        for row in file_rows:
            file_dict = dict(row)
            file_path = get_safe_project_path(file_dict['project_id'], file_dict['filename'])

            # Try to get a preview for text files
            preview = ''
            if file_dict.get('file_type') in ['fasta', 'txt', 'csv', 'json', 'yaml', 'smi', 'pdb']:
                try:
                    if os.path.exists(file_path):
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            preview = f.read(200)  # First 200 chars
                except Exception:
                    pass

            # Format file size for display
            size_bytes = file_dict['file_size']
            if size_bytes < 1024:
                size_display = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_display = f"{size_bytes / 1024:.1f} KB"
            elif size_bytes < 1024 * 1024 * 1024:
                size_display = f"{size_bytes / (1024 * 1024):.1f} MB"
            else:
                size_display = f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

            # Determine file icon/category
            file_ext = file_dict['filename'].rsplit('.', 1)[-1].lower() if '.' in file_dict['filename'] else ''
            file_category = 'document'
            if file_ext in ['pdb', 'mol', 'sdf', 'mol2']:
                file_category = 'molecule'
            elif file_ext in ['fasta', 'fa', 'faa', 'seq']:
                file_category = 'sequence'
            elif file_ext in ['csv', 'tsv', 'json', 'xml']:
                file_category = 'data'
            elif file_ext in ['png', 'jpg', 'jpeg', 'gif', 'svg']:
                file_category = 'image'

            results.append({
                'type': 'file',
                'file_id': file_dict['file_id'],
                'filename': file_dict['filename'],
                'project_name': file_dict['project_name'],
                'project_id': file_dict['project_id'],
                'file_type': file_dict['file_type'],
                'file_category': file_category,
                'file_extension': file_ext,
                'file_size': size_bytes,
                'file_size_display': size_display,
                'uploaded_at': file_dict['uploaded_at'],
                'preview': preview,
                'download_url': f"/api/files/{file_dict['file_id']}/download",
                'view_url': f"/projects/{file_dict['project_id']}"
            })

        # Get total count for jobs
        total_jobs = db.execute(
            '''SELECT COUNT(*) as count
               FROM jobs j
               JOIN projects p ON j.project_id = p.id
               JOIN project_assignments pa ON p.id = pa.project_id
               WHERE pa.user_id = %s AND j.sequence LIKE %s''',
            (user_id, f'%{query}%')
        ).fetchone()['count']

        # Search in job sequences with pagination
        job_rows = db.execute(
            '''SELECT j.id, j.sequence, j.model_name, j.submitted_at,
                      p.name as project_name
               FROM jobs j
               JOIN projects p ON j.project_id = p.id
               JOIN project_assignments pa ON p.id = pa.project_id
               WHERE pa.user_id = %s AND j.sequence LIKE %s
               ORDER BY j.submitted_at DESC
               LIMIT %s OFFSET %s''',
            (user_id, f'%{query}%', limit, offset)
        ).fetchall()

        for row in job_rows:
            job_dict = dict(row)
            sequence_length = len(job_dict['sequence'] or '')

            # Format sequence size for display
            if sequence_length < 1024:
                size_display = f"{sequence_length} chars"
            else:
                size_display = f"{sequence_length / 1024:.1f} KB"

            results.append({
                'type': 'job',
                'job_id': job_dict['id'],
                'filename': f"Job #{job_dict['id']} - {job_dict['model_name']}",
                'project_name': job_dict['project_name'],
                'file_type': 'sequence',
                'file_category': 'sequence',
                'model_name': job_dict['model_name'],
                'file_size': sequence_length,
                'file_size_display': size_display,
                'uploaded_at': job_dict['submitted_at'],
                'preview': (job_dict['sequence'] or '')[:200],
                'view_url': f"/jobs/{job_dict['id']}"
            })

        total_count = total_files + total_jobs
        total_pages = (total_count + limit - 1) // limit  # Ceiling division

        # Categorize results for better display
        files_only = [r for r in results if r.get('type') == 'file']
        jobs_only = [r for r in results if r.get('type') == 'job']

        # Category counts
        category_counts = {}
        for result in results:
            cat = result.get('file_category', 'other')
            category_counts[cat] = category_counts.get(cat, 0) + 1

        return jsonify({
            'status': 'success',
            'results': results,
            'count': len(results),
            'summary': {
                'total_files': total_files,
                'total_jobs': total_jobs,
                'files_shown': len(files_only),
                'jobs_shown': len(jobs_only),
                'categories': category_counts
            },
            'pagination': {
                'page': page,
                'limit': limit,
                'total': total_count,
                'total_pages': total_pages,
                'has_next': page < total_pages,
                'has_prev': page > 1
            },
            'filters': {
                'file_type': file_type,
                'project_id': project_id,
                'date_from': date_from,
                'date_to': date_to
            }
        })

    except Exception as e:
        logger.error(f"File search error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search/pubchem', methods=['GET'])
@login_required
def api_search_pubchem():
    """Search PubChem database for compounds"""
    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) < 2:
            return jsonify({'status': 'error', 'message': 'Query too short'}), 400

        results = []

        # Try to search PubChem
        try:
            # Search by name first
            search_url = f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{urllib.parse.quote(query)}/property/MolecularFormula,MolecularWeight,CanonicalSMILES/JSON'

            with urllib.request.urlopen(search_url, timeout=5) as response:
                data = json.loads(response.read())

                if 'PropertyTable' in data and 'Properties' in data['PropertyTable']:
                    for prop in data['PropertyTable']['Properties'][:10]:  # Limit to 10 results
                        results.append({
                            'cid': prop.get('CID'),
                            'title': f"CID {prop.get('CID')}",
                            'description': f"{prop.get('MolecularFormula', 'N/A')} - MW: {prop.get('MolecularWeight', 'N/A')}",
                            'smiles': prop.get('CanonicalSMILES'),
                            'formula': prop.get('MolecularFormula'),
                            'weight': prop.get('MolecularWeight')
                        })
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Try as SMILES instead
                try:
                    smiles_url = f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/{urllib.parse.quote(query)}/property/MolecularFormula,MolecularWeight,CanonicalSMILES/JSON'

                    with urllib.request.urlopen(smiles_url, timeout=5) as response:
                        data = json.loads(response.read())

                        if 'PropertyTable' in data and 'Properties' in data['PropertyTable']:
                            for prop in data['PropertyTable']['Properties'][:10]:
                                results.append({
                                    'cid': prop.get('CID'),
                                    'title': f"CID {prop.get('CID')}",
                                    'description': f"{prop.get('MolecularFormula', 'N/A')} - MW: {prop.get('MolecularWeight', 'N/A')}",
                                    'smiles': prop.get('CanonicalSMILES'),
                                    'formula': prop.get('MolecularFormula'),
                                    'weight': prop.get('MolecularWeight')
                                })
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"PubChem search error: {e}")

        return jsonify({
            'status': 'success',
            'results': results,
            'count': len(results)
        })

    except Exception as e:
        logger.error(f"PubChem search error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search/pdb', methods=['GET'])
@login_required
def api_search_pdb():
    """Search PDB (Protein Data Bank) for structures"""
    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) < 2:
            return jsonify({'status': 'error', 'message': 'Query too short'}), 400

        results = []

        # Try to search PDB
        try:
            # Check if it's a direct PDB ID (4 characters)
            if len(query) == 4 and query.isalnum():
                # Direct PDB ID lookup
                pdb_url = f'https://data.rcsb.org/rest/v1/core/entry/{query.upper()}'

                with urllib.request.urlopen(pdb_url, timeout=5) as response:
                    data = json.loads(response.read())

                    if 'struct' in data:
                        results.append({
                            'pdb_id': query.upper(),
                            'title': data['struct'].get('title', query.upper()),
                            'description': data['struct'].get('pdbx_descriptor', 'No description available'),
                            'method': data.get('exptl', [{}])[0].get('method', 'Unknown') if 'exptl' in data else 'Unknown',
                            'resolution': data.get('rcsb_entry_info', {}).get('resolution_combined', [None])[0]
                        })
            else:
                # Text search using RCSB search API with full_text service
                search_data = {
                    "query": {
                        "type": "terminal",
                        "service": "full_text",
                        "parameters": {
                            "value": query
                        }
                    },
                    "return_type": "entry",
                    "request_options": {
                        "return_all_hits": False,
                        "results_content_type": ["experimental"],
                        "sort": [{"sort_by": "score", "direction": "desc"}]
                    }
                }

                req = urllib.request.Request(
                    'https://search.rcsb.org/rcsbsearch/v2/query',
                    data=json.dumps(search_data).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )

                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read())

                    if 'result_set' in data:
                        for result in data['result_set'][:10]:  # Limit to 10
                            pdb_id = result['identifier']

                            # Fetch details for each PDB ID
                            try:
                                detail_url = f'https://data.rcsb.org/rest/v1/core/entry/{pdb_id}'
                                with urllib.request.urlopen(detail_url, timeout=3) as detail_response:
                                    detail_data = json.loads(detail_response.read())

                                    results.append({
                                        'pdb_id': pdb_id,
                                        'title': detail_data.get('struct', {}).get('title', pdb_id),
                                        'description': detail_data.get('struct', {}).get('pdbx_descriptor', 'No description available'),
                                        'method': detail_data.get('exptl', [{}])[0].get('method', 'Unknown') if 'exptl' in detail_data else 'Unknown',
                                        'resolution': detail_data.get('rcsb_entry_info', {}).get('resolution_combined', [None])[0]
                                    })
                            except Exception:
                                # If detail fetch fails, use basic info
                                results.append({
                                    'pdb_id': pdb_id,
                                    'title': pdb_id,
                                    'description': 'Structure from PDB',
                                    'method': 'Unknown',
                                    'resolution': None
                                })
        except urllib.error.HTTPError as e:
            logger.warning(f"PDB search HTTP error: {e.code}")
        except Exception as e:
            logger.warning(f"PDB search error: {e}")

        return jsonify({
            'status': 'success',
            'results': results,
            'count': len(results)
        })

    except Exception as e:
        logger.error(f"PDB search error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search/unified', methods=['GET'])
@login_required
def api_search_unified():
    """
    Unified search endpoint that searches across multiple sources
    and returns combined results
    """
    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) < 2:
            return jsonify({'status': 'error', 'message': 'Query too short'}), 400

        all_results = {
            'local_files': [],
            'pubchem': [],
            'pdb': [],
            'query': query
        }

        # Search local files (always)
        try:
            user_id = session['user_id']
            db = get_db()

            file_rows = db.execute(
                '''SELECT f.id, f.filename, f.file_type, f.project_id, p.name as project_name
                   FROM project_files f
                   JOIN projects p ON f.project_id = p.id
                   JOIN project_assignments pa ON p.id = pa.project_id
                   WHERE pa.user_id = %s AND f.filename LIKE %s
                   ORDER BY f.uploaded_at DESC
                   LIMIT 5''',
                (user_id, f'%{query}%')
            ).fetchall()

            all_results['local_files'] = [dict(row) for row in file_rows]
        except Exception as e:
            logger.error(f"Local file search error: {e}")

        # Search PubChem if query looks like a compound (short and alphanumeric)
        if len(query) < 50 and any(c.isalpha() for c in query):
            try:
                # Quick PubChem search
                search_url = f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{urllib.parse.quote(query)}/cids/JSON?list_return=listkey'
                with urllib.request.urlopen(search_url, timeout=3) as response:
                    data = json.loads(response.read())
                    if 'IdentifierList' in data:
                        all_results['pubchem'] = data['IdentifierList'].get('CID', [])[:5]
            except Exception:
                pass

        # Search PDB if it looks like a PDB ID or protein-related
        if len(query) == 4 or 'protein' in query.lower():
            try:
                if len(query) == 4:
                    pdb_url = f'https://data.rcsb.org/rest/v1/core/entry/{query.upper()}'
                    with urllib.request.urlopen(pdb_url, timeout=3) as response:
                        data = json.loads(response.read())
                        if 'struct' in data:
                            all_results['pdb'].append({
                                'pdb_id': query.upper(),
                                'title': data['struct'].get('title', query.upper())
                            })
            except Exception:
                pass

        return jsonify({
            'status': 'success',
            'results': all_results,
            'total_count': len(all_results['local_files']) + len(all_results['pubchem']) + len(all_results['pdb'])
        })

    except Exception as e:
        logger.error(f"Unified search error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search/enhanced', methods=['GET'])
@login_required
def api_search_enhanced():
    """
    Enhanced unified search that includes both local files and public databases.
    Returns results from:
    - Local project files (including PDB files)
    - PDB (Protein Data Bank)
    - AlphaFold Database
    - UniProt
    """
    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) < 2:
            return jsonify({'status': 'error', 'message': 'Query too short'}), 400

        page = max(1, int(request.args.get('page', 1)))
        limit = min(50, max(5, int(request.args.get('limit', 20))))
        include_databases = request.args.get('databases', 'true').lower() == 'true'

        all_results = {
            'local_files': [],
            'database_results': [],
            'query': query
        }

        # Search local files
        try:
            user_id = session['user_id']
            db = get_db()

            # Search both filenames and content for PDB/structure files
            file_rows = db.execute(
                '''SELECT f.id, f.file_id, f.filename, f.file_type, f.file_size, f.uploaded_at,
                          f.project_id, p.name as project_name
                   FROM project_files f
                   JOIN projects p ON f.project_id = p.id
                   JOIN project_assignments pa ON p.id = pa.project_id
                   WHERE pa.user_id = %s AND f.filename LIKE %s
                   ORDER BY f.uploaded_at DESC
                   LIMIT %s''',
                (user_id, f'%{query}%', limit)
            ).fetchall()

            for row in file_rows:
                file_dict = dict(row)
                file_ext = file_dict['filename'].rsplit('.', 1)[-1].lower() if '.' in file_dict['filename'] else ''

                # Determine if it's a structure file
                is_structure = file_ext in ['pdb', 'cif', 'mol', 'sdf', 'mol2']

                # Format file size
                size_bytes = file_dict['file_size']
                if size_bytes < 1024:
                    size_display = f"{size_bytes} B"
                elif size_bytes < 1024 * 1024:
                    size_display = f"{size_bytes / 1024:.1f} KB"
                else:
                    size_display = f"{size_bytes / (1024 * 1024):.1f} MB"

                all_results['local_files'].append({
                    'type': 'local_file',
                    'file_id': file_dict['file_id'],
                    'filename': file_dict['filename'],
                    'project_name': file_dict['project_name'],
                    'project_id': file_dict['project_id'],
                    'file_type': file_dict['file_type'],
                    'file_extension': file_ext,
                    'file_size': size_bytes,
                    'file_size_display': size_display,
                    'uploaded_at': str(file_dict['uploaded_at']),
                    'is_structure': is_structure,
                    'download_url': f"/api/files/{file_dict['file_id']}/download",
                    'view_url': f"/projects/{file_dict['project_id']}"
                })

        except Exception as e:
            logger.error(f"Local file search error: {e}", exc_info=True)

        # Search public databases if enabled
        if include_databases:
            try:
                # Initialize database connectors
                pdb_connector = PDBConnector()

                # Search PDB - limit to 10 results
                pdb_results = pdb_connector.search(query, limit=10)

                for result in pdb_results:
                    all_results['database_results'].append({
                        'type': 'pdb',
                        'database': 'PDB',
                        'id': result['id'],
                        'title': result['title'],
                        'description': result['description'],
                        'organism': result['organism'],
                        'method': result['method'],
                        'resolution': result['resolution'],
                        'deposition_date': result['deposition_date'],
                        'url': result['url'],
                        'download_formats': ['pdb', 'cif', 'xml'],
                        'download_url': f"/api/databases/pdb/{result['id']}/download"
                    })

                # Search AlphaFold if query looks like a UniProt ID
                if len(query) <= 10 and query.isalnum():
                    try:
                        alphafold_connector = AlphaFoldConnector()
                        af_results = alphafold_connector.search(query, limit=5)

                        for result in af_results:
                            all_results['database_results'].append({
                                'type': 'alphafold',
                                'database': 'AlphaFold',
                                'id': result['id'],
                                'title': result['title'],
                                'description': result.get('description', ''),
                                'organism': result.get('organism', 'N/A'),
                                'url': result['url'],
                                'download_formats': ['pdb', 'cif'],
                                'download_url': f"/api/databases/alphafold/{result['id']}/download"
                            })
                    except Exception as af_error:
                        logger.warning(f"AlphaFold search error: {af_error}")

            except Exception as db_error:
                logger.error(f"Database search error: {db_error}", exc_info=True)

        # Calculate pagination info
        total_count = len(all_results['local_files']) + len(all_results['database_results'])

        return jsonify({
            'status': 'success',
            'results': all_results,
            'summary': {
                'total_count': total_count,
                'local_files': len(all_results['local_files']),
                'database_results': len(all_results['database_results'])
            },
            'pagination': {
                'page': page,
                'limit': limit
            }
        })

    except Exception as e:
        logger.error(f"Enhanced search error: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/databases/<database>/<identifier>/download', methods=['GET'])
@login_required
def api_database_download(database, identifier):
    """
    Download a file from a public database (PDB, AlphaFold, etc.)

    Query parameters:
        - format: File format (pdb, cif, xml, etc.) - default: pdb
    """
    try:
        file_format = request.args.get('format', 'pdb').lower()

        # Validate database
        valid_databases = ['pdb', 'alphafold', 'uniprot']
        if database.lower() not in valid_databases:
            return jsonify({'error': 'Invalid database'}), 400

        # Initialize appropriate connector
        connector = None
        if database.lower() == 'pdb':
            connector = PDBConnector()
        elif database.lower() == 'alphafold':
            connector = AlphaFoldConnector()
        elif database.lower() == 'uniprot':
            connector = UniProtConnector()

        if not connector:
            return jsonify({'error': 'Database connector not available'}), 500

        # Fetch the file
        file_content, filename = connector.fetch_file(identifier, file_format)

        # Log the download
        db = get_db()
        user_id = session.get('user_id')
        try:
            db.execute(
                '''INSERT INTO activity_log
                   (user_id, activity_type, activity_description, resource_type)
                   VALUES (%s, %s, %s, %s)''',
                (user_id, 'database_download',
                 f'Downloaded {identifier} from {database} as {file_format}',
                 'database_file')
            )
            db.commit()
        except Exception as log_error:
            logger.warning(f"Failed to log download: {log_error}")

        # Return file with appropriate content type
        content_types = {
            'pdb': 'chemical/x-pdb',
            'cif': 'chemical/x-cif',
            'xml': 'application/xml',
            'fasta': 'text/plain'
        }

        from flask import Response
        response = Response(
            file_content,
            mimetype=content_types.get(file_format, 'application/octet-stream'),
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': len(file_content)
            }
        )

        return response

    except Exception as e:
        logger.error(f"Database download error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/databases/<database>/<identifier>/save-to-project', methods=['POST'])
@login_required
def api_database_save_to_project(database, identifier):
    """
    Download a file from a public database and save it to a project

    Request body:
        - project_id: int - The project ID to save to
        - format: str - File format (pdb, cif, xml, fasta, etc.)
    """
    try:
        data = request.get_json()
        project_id = data.get('project_id')
        file_format = data.get('format', 'pdb').lower()

        if not project_id:
            return jsonify({'error': 'project_id is required'}), 400

        user_id = session['user_id']
        db = get_db()

        # Verify user has access to the project (via assignments)
        result = db.execute("""
            SELECT p.id, p.name, p.organization_id
            FROM projects p
            LEFT JOIN project_assignments pa ON p.id = pa.project_id AND pa.user_id = %s
            WHERE p.id = %s AND pa.user_id IS NOT NULL
        """, (user_id, project_id))

        project = result.fetchone()
        if not project:
            return jsonify({'error': 'Project not found or access denied'}), 403

        # Validate database
        valid_databases = ['pdb', 'alphafold', 'uniprot']
        if database.lower() not in valid_databases:
            return jsonify({'error': 'Invalid database'}), 400

        # Initialize appropriate connector
        connector = None
        if database.lower() == 'pdb':
            connector = PDBConnector()
        elif database.lower() == 'alphafold':
            connector = AlphaFoldConnector()
        elif database.lower() == 'uniprot':
            connector = UniProtConnector()

        if not connector:
            return jsonify({'error': 'Database connector not available'}), 500

        # Fetch the file
        file_content, filename = connector.fetch_file(identifier, file_format)

        # Save to project directory
        from ..helpers import get_safe_project_path

        # Create safe file path
        project_dir = get_safe_project_path(project_id, project['organization_id'])
        if not os.path.exists(project_dir):
            os.makedirs(project_dir, exist_ok=True)

        # Full file path (relative path for database)
        relative_path = f"project_{project_id}/{filename}"
        absolute_path = os.path.join(project_dir, filename)

        # Write file
        with open(absolute_path, 'wb') as f:
            f.write(file_content)

        # Get file size
        file_size = len(file_content)

        # Determine file type from extension
        file_extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

        # Add to database (use ON CONFLICT to handle duplicate files)
        result = db.execute("""
            INSERT INTO files
            (project_id, creator_id, name, path, size, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (path) DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP,
                name = EXCLUDED.name,
                size = EXCLUDED.size
            RETURNING id
        """, (project_id, user_id, filename, absolute_path, file_size))

        file_record = result.fetchone()
        file_id = file_record['id']

        db.commit()

        # Log the action
        try:
            db.execute("""
                INSERT INTO activity_log
                (user_id, activity_type, activity_description, resource_type, resource_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                user_id,
                'file_saved_from_database',
                f'Saved {identifier} from {database} to project {project["name"]}',
                'file',
                file_id
            ))
            db.commit()
        except Exception as log_error:
            logger.warning(f"Failed to log save action: {log_error}")

        return jsonify({
            'status': 'success',
            'message': f'Successfully saved {filename} to {project["name"]}',
            'file': {
                'file_id': file_id,
                'filename': filename,
                'file_size': file_size,
                'project_id': project_id,
                'project_name': project['name']
            }
        })

    except Exception as e:
        logger.error(f"Error saving database file to project: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ============================================================================
# PUBLIC DATABASES TAB API ENDPOINTS
# ============================================================================

@app.route('/api/databases/list', methods=['GET'])
@login_required
def api_list_databases():
    """
    List all available public databases with metadata.
    Used by the Databases tab to show database options.
    """
    try:
        databases = [
            {
                'id': 'pdb',
                'name': 'Protein Data Bank',
                'shortName': 'PDB',
                'description': 'Crystal structures of proteins, nucleic acids, and complex assemblies',
                'color': '#10b981',
                'formats': ['pdb', 'cif', 'xml'],
                'defaultFormat': 'pdb',
                'searchPlaceholder': 'Search by PDB ID, protein name, organism...',
                'exampleQueries': ['hemoglobin', '4HHB', 'insulin', 'kinase']
            },
            {
                'id': 'uniprot',
                'name': 'UniProt',
                'shortName': 'UniProt',
                'description': 'Comprehensive protein sequence and functional annotation database',
                'color': '#6366f1',
                'formats': ['fasta', 'txt', 'xml'],
                'defaultFormat': 'fasta',
                'searchPlaceholder': 'Search by UniProt ID, protein name, gene...',
                'exampleQueries': ['P12345', 'BRCA1', 'insulin', 'hemoglobin']
            },
            {
                'id': 'alphafold',
                'name': 'AlphaFold Database',
                'shortName': 'AlphaFold',
                'description': 'AI-predicted protein structures from DeepMind',
                'color': '#8b5cf6',
                'formats': ['pdb', 'cif'],
                'defaultFormat': 'pdb',
                'searchPlaceholder': 'Search by UniProt ID or gene name...',
                'exampleQueries': ['P53_HUMAN', 'Q9Y6K9', 'P04637']
            },
            {
                'id': 'pubchem',
                'name': 'PubChem',
                'shortName': 'PubChem',
                'description': 'Chemical compounds and their biological activities',
                'color': '#f59e0b',
                'formats': ['sdf', 'pdb', 'mol'],
                'defaultFormat': 'sdf',
                'searchPlaceholder': 'Search by compound name, CID, formula...',
                'exampleQueries': ['aspirin', 'caffeine', 'glucose', 'C9H8O4']
            }
        ]

        return jsonify({
            'status': 'success',
            'databases': databases
        })

    except Exception as e:
        logger.error(f"Error listing databases: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/databases/<database>/<identifier>/details', methods=['GET'])
@login_required
def api_database_details(database, identifier):
    """
    Get detailed metadata for a database entry.
    """
    try:
        # Validate database
        valid_databases = ['pdb', 'uniprot', 'alphafold', 'pubchem']
        db_lower = database.lower()
        if db_lower not in valid_databases:
            return jsonify({'status': 'error', 'message': f'Invalid database: {database}'}), 400

        # Initialize appropriate connector
        connector = None
        if db_lower == 'pdb':
            connector = PDBConnector()
        elif db_lower == 'uniprot':
            connector = UniProtConnector()
        elif db_lower == 'alphafold':
            connector = AlphaFoldConnector()
        elif db_lower == 'pubchem':
            connector = PubChemConnector()

        if not connector:
            return jsonify({'status': 'error', 'message': 'Database connector not available'}), 500

        # Get metadata
        if hasattr(connector, 'get_metadata'):
            metadata = connector.get_metadata(identifier)
        else:
            # Fallback: search for the ID and return first result
            results = connector.search(identifier, limit=1)
            metadata = results[0] if results else {}

        return jsonify({
            'status': 'success',
            'database': db_lower,
            'identifier': identifier,
            'metadata': metadata
        })

    except Exception as e:
        logger.error(f"Database details error for {database}/{identifier}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500
