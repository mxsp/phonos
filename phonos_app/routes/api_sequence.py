"""API routes for sequence analysis operations."""
from __future__ import annotations

import json
import os
from datetime import datetime

from flask import jsonify, request, session
from werkzeug.utils import secure_filename

from config import Config
from ..app import app, logger
from ..helpers import get_db, login_required, get_project_assignment, get_safe_project_path
from ..helpers.security import csrf_protect, sanitize_input
from ..helpers.sequence_utils import (
    detect_sequence_type,
    validate_sequence,
    transcribe,
    back_transcribe,
    translate,
    reverse_complement,
    calculate_gc_content,
    calculate_nucleotide_composition,
    calculate_amino_acid_composition,
    calculate_molecular_weight,
    calculate_isoelectric_point,
    calculate_hydropathy_profile,
    analyze_codon_usage,
    find_orfs,
    calculate_protein_properties,
    get_sequence_statistics,
    clean_sequence,
    # Primer design functions
    design_primers,
    analyze_primer,
    check_primer_dimer,
    calculate_tm_nearest_neighbor,
    # Motif and restriction site functions
    find_restriction_sites,
    find_regulatory_elements,
    search_motif,
    list_enzymes,
    RESTRICTION_ENZYMES,
    REGULATORY_ELEMENTS,
)


@app.route('/api/sequence/detect', methods=['POST'])
@login_required
@csrf_protect
def sequence_detect():
    """
    Auto-detect sequence type (DNA, RNA, or Protein).

    Request body:
        sequence: Input sequence string

    Returns:
        JSON with detected type and validation status
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        seq_type = detect_sequence_type(sequence)
        is_valid, error_msg = validate_sequence(sequence, seq_type)

        return jsonify({
            'success': True,
            'type': seq_type,
            'valid': is_valid,
            'error': error_msg if not is_valid else None,
            'length': len(clean_sequence(sequence))
        })

    except Exception as e:
        logger.error(f"Sequence detect error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/validate', methods=['POST'])
@login_required
@csrf_protect
def sequence_validate():
    """
    Validate sequence against specified type rules.

    Request body:
        sequence: Input sequence string
        type: 'dna', 'rna', or 'protein'

    Returns:
        JSON with validation result
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '')
        seq_type = data.get('type', 'dna')

        is_valid, error_msg = validate_sequence(sequence, seq_type)

        return jsonify({
            'success': True,
            'valid': is_valid,
            'error': error_msg if not is_valid else None
        })

    except Exception as e:
        logger.error(f"Sequence validate error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/transcribe', methods=['POST'])
@login_required
@csrf_protect
def sequence_transcribe():
    """
    Transcribe DNA to RNA (T -> U).

    Request body:
        sequence: Input DNA sequence

    Returns:
        JSON with RNA sequence
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        is_valid, error_msg = validate_sequence(sequence, 'dna')

        if not is_valid:
            return jsonify({'error': True, 'message': error_msg}), 400

        rna = transcribe(sequence)

        return jsonify({
            'success': True,
            'input': clean_sequence(sequence),
            'output': rna,
            'input_type': 'DNA',
            'output_type': 'RNA',
            'length': len(rna)
        })

    except Exception as e:
        logger.error(f"Transcribe error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/back-transcribe', methods=['POST'])
@login_required
@csrf_protect
def sequence_back_transcribe():
    """
    Back-transcribe RNA to DNA (U -> T).

    Request body:
        sequence: Input RNA sequence

    Returns:
        JSON with DNA sequence
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        is_valid, error_msg = validate_sequence(sequence, 'rna')

        if not is_valid:
            return jsonify({'error': True, 'message': error_msg}), 400

        dna = back_transcribe(sequence)

        return jsonify({
            'success': True,
            'input': clean_sequence(sequence),
            'output': dna,
            'input_type': 'RNA',
            'output_type': 'DNA',
            'length': len(dna)
        })

    except Exception as e:
        logger.error(f"Back-transcribe error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/translate', methods=['POST'])
@login_required
@csrf_protect
def sequence_translate():
    """
    Translate DNA or RNA to protein.

    Request body:
        sequence: Input nucleotide sequence
        reading_frame: 1, 2, or 3 (default: 1)
        type: 'dna' or 'rna' (auto-detected if not provided)

    Returns:
        JSON with protein sequence
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        reading_frame = min(max(1, int(data.get('reading_frame', 1))), 3)
        seq_type = data.get('type') or detect_sequence_type(sequence)

        if seq_type not in ('dna', 'rna'):
            return jsonify({'error': True, 'message': 'Sequence must be DNA or RNA for translation'}), 400

        is_valid, error_msg = validate_sequence(sequence, seq_type)
        if not is_valid:
            return jsonify({'error': True, 'message': error_msg}), 400

        protein = translate(sequence, reading_frame, seq_type)
        clean_seq = clean_sequence(sequence)

        return jsonify({
            'success': True,
            'input': clean_seq,
            'output': protein,
            'input_type': seq_type.upper(),
            'output_type': 'Protein',
            'reading_frame': reading_frame,
            'input_length': len(clean_seq),
            'output_length': len(protein),
            'stop_codons': protein.count('*')
        })

    except Exception as e:
        logger.error(f"Translate error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/reverse-complement', methods=['POST'])
@login_required
@csrf_protect
def sequence_reverse_complement():
    """
    Get reverse complement of DNA or RNA sequence.

    Request body:
        sequence: Input nucleotide sequence
        type: 'dna' or 'rna' (auto-detected if not provided)

    Returns:
        JSON with reverse complement sequence
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        seq_type = data.get('type') or detect_sequence_type(sequence)

        if seq_type not in ('dna', 'rna'):
            return jsonify({'error': True, 'message': 'Sequence must be DNA or RNA for reverse complement'}), 400

        is_valid, error_msg = validate_sequence(sequence, seq_type)
        if not is_valid:
            return jsonify({'error': True, 'message': error_msg}), 400

        rev_comp = reverse_complement(sequence, seq_type)
        clean_seq = clean_sequence(sequence)

        return jsonify({
            'success': True,
            'input': clean_seq,
            'output': rev_comp,
            'type': seq_type.upper(),
            'length': len(rev_comp)
        })

    except Exception as e:
        logger.error(f"Reverse complement error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/statistics', methods=['POST'])
@login_required
@csrf_protect
def sequence_statistics():
    """
    Calculate sequence statistics.

    Request body:
        sequence: Input sequence
        type: 'dna', 'rna', or 'protein' (auto-detected if not provided)

    Returns:
        JSON with comprehensive statistics
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        seq_type = data.get('type') or detect_sequence_type(sequence)

        is_valid, error_msg = validate_sequence(sequence, seq_type)
        if not is_valid:
            return jsonify({'error': True, 'message': error_msg}), 400

        stats = get_sequence_statistics(sequence, seq_type)

        return jsonify({
            'success': True,
            **stats
        })

    except Exception as e:
        logger.error(f"Statistics error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/codon-usage', methods=['POST'])
@login_required
@csrf_protect
def sequence_codon_usage():
    """
    Analyze codon usage frequencies.

    Request body:
        sequence: Input DNA or RNA sequence

    Returns:
        JSON with codon frequencies and RSCU values
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        seq_type = detect_sequence_type(sequence)

        if seq_type not in ('dna', 'rna'):
            return jsonify({'error': True, 'message': 'Sequence must be DNA or RNA for codon analysis'}), 400

        usage = analyze_codon_usage(sequence)

        return jsonify({
            'success': True,
            **usage
        })

    except Exception as e:
        logger.error(f"Codon usage error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/find-orfs', methods=['POST'])
@login_required
@csrf_protect
def sequence_find_orfs():
    """
    Find Open Reading Frames (ORFs) in a sequence.

    Request body:
        sequence: Input DNA or RNA sequence
        min_length: Minimum ORF length in bp (default: 100)
        include_nested: Include nested ORFs (default: false)

    Returns:
        JSON with list of ORFs
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        min_length = max(30, int(data.get('min_length', 100)))
        include_nested = bool(data.get('include_nested', False))

        seq_type = detect_sequence_type(sequence)
        if seq_type not in ('dna', 'rna'):
            return jsonify({'error': True, 'message': 'Sequence must be DNA or RNA for ORF detection'}), 400

        orfs = find_orfs(sequence, min_length, include_nested)

        return jsonify({
            'success': True,
            'orfs': orfs,
            'count': len(orfs),
            'sequence_length': len(clean_sequence(sequence))
        })

    except Exception as e:
        logger.error(f"Find ORFs error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/protein-properties', methods=['POST'])
@login_required
@csrf_protect
def sequence_protein_properties():
    """
    Calculate protein properties.

    Request body:
        sequence: Input protein sequence

    Returns:
        JSON with MW, pI, composition, hydropathy, etc.
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')

        # Handle both protein sequences and nucleotide sequences (translate first)
        seq_type = detect_sequence_type(sequence)
        if seq_type in ('dna', 'rna'):
            # Translate to protein first
            protein = translate(sequence, 1, seq_type)
            protein = protein.replace('*', '')  # Remove stop codons
        else:
            protein = clean_sequence(sequence).replace('*', '')

        if not protein:
            return jsonify({'error': True, 'message': 'Empty protein sequence'}), 400

        props = calculate_protein_properties(protein)

        return jsonify({
            'success': True,
            **props
        })

    except Exception as e:
        logger.error(f"Protein properties error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/hydropathy', methods=['POST'])
@login_required
@csrf_protect
def sequence_hydropathy():
    """
    Calculate Kyte-Doolittle hydropathy profile.

    Request body:
        sequence: Input protein sequence
        window_size: Sliding window size (default: 9)

    Returns:
        JSON with hydropathy profile data
    """
    try:
        data = request.get_json()
        if not data or 'sequence' not in data:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = data.get('sequence', '')
        window_size = max(3, min(21, int(data.get('window_size', 9))))

        # Handle both protein and nucleotide sequences
        seq_type = detect_sequence_type(sequence)
        if seq_type in ('dna', 'rna'):
            protein = translate(sequence, 1, seq_type).replace('*', '')
        else:
            protein = clean_sequence(sequence).replace('*', '')

        if not protein:
            return jsonify({'error': True, 'message': 'Empty protein sequence'}), 400

        profile = calculate_hydropathy_profile(protein, window_size)

        return jsonify({
            'success': True,
            'profile': profile,
            'window_size': window_size,
            'sequence_length': len(protein)
        })

    except Exception as e:
        logger.error(f"Hydropathy error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


# ============================================================================
# PROJECT FILE OPERATIONS
# ============================================================================

SEQUENCE_FILE_EXTENSIONS = {'.fasta', '.fa', '.fna', '.ffn', '.faa', '.frn', '.seq', '.txt', '.gb', '.gbk', '.genbank'}


@app.route('/api/sequence/projects', methods=['GET'])
@login_required
def sequence_list_projects():
    """
    List user's projects that can contain sequence files.

    Returns:
        JSON with list of accessible projects
    """
    try:
        db = get_db()
        user_id = session['user_id']

        # Get all projects user has access to
        projects = db.execute('''
            SELECT DISTINCT p.id, p.name, p.description, p.created_at,
                   pa.role, t.name as team_name
            FROM projects p
            JOIN project_assignments pa ON p.id = pa.project_id
            LEFT JOIN teams t ON p.team_id = t.id
            WHERE pa.user_id = %s
            ORDER BY p.name
        ''', (user_id,)).fetchall()

        project_list = []
        for p in projects:
            project_dict = dict(p)
            # Count sequence files in project
            file_count = db.execute('''
                SELECT COUNT(*) as count FROM files
                WHERE project_id = %s AND (
                    LOWER(name) LIKE '%%.fasta' OR
                    LOWER(name) LIKE '%%.fa' OR
                    LOWER(name) LIKE '%%.seq' OR
                    LOWER(name) LIKE '%%.txt' OR
                    LOWER(name) LIKE '%%.gb' OR
                    LOWER(name) LIKE '%%.gbk'
                )
            ''', (p['id'],)).fetchone()
            project_dict['sequence_file_count'] = file_count['count'] if file_count else 0
            project_list.append(project_dict)

        return jsonify({
            'success': True,
            'projects': project_list
        })

    except Exception as e:
        logger.error(f"List projects error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/project/<int:project_id>/files', methods=['GET'])
@login_required
def sequence_list_files(project_id):
    """
    List sequence files in a project.

    Returns:
        JSON with list of sequence files
    """
    try:
        db = get_db()
        user_id = session['user_id']

        # Check project access
        role = get_project_assignment(db, user_id, project_id)
        if not role:
            return jsonify({'error': True, 'message': 'Access denied'}), 403

        # Get sequence files (FASTA, GenBank, plain text)
        files = db.execute('''
            SELECT id, name, size, created_at, notes
            FROM files
            WHERE project_id = %s AND (
                LOWER(name) LIKE '%%.fasta' OR
                LOWER(name) LIKE '%%.fa' OR
                LOWER(name) LIKE '%%.fna' OR
                LOWER(name) LIKE '%%.ffn' OR
                LOWER(name) LIKE '%%.faa' OR
                LOWER(name) LIKE '%%.seq' OR
                LOWER(name) LIKE '%%.txt' OR
                LOWER(name) LIKE '%%.gb' OR
                LOWER(name) LIKE '%%.gbk'
            )
            ORDER BY created_at DESC
        ''', (project_id,)).fetchall()

        file_list = []
        for f in files:
            file_dict = dict(f)
            if file_dict.get('created_at') and hasattr(file_dict['created_at'], 'isoformat'):
                file_dict['created_at'] = file_dict['created_at'].isoformat()
            # Determine file type from extension
            name = f['name'].lower()
            if name.endswith(('.gb', '.gbk', '.genbank')):
                file_dict['format'] = 'genbank'
            elif name.endswith(('.fasta', '.fa', '.fna', '.ffn', '.faa', '.frn')):
                file_dict['format'] = 'fasta'
            else:
                file_dict['format'] = 'text'
            file_list.append(file_dict)

        return jsonify({
            'success': True,
            'files': file_list,
            'project_id': project_id
        })

    except Exception as e:
        logger.error(f"List files error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/project/<int:project_id>/save', methods=['POST'])
@login_required
@csrf_protect
def sequence_save_to_project(project_id):
    """
    Save sequence to a project as a FASTA file.

    Request body:
        sequence: The sequence string
        name: File name (without extension)
        description: Optional description/header
        type: Sequence type (dna, rna, protein)

    Returns:
        JSON with saved file info
    """
    try:
        db = get_db()
        user_id = session['user_id']

        # Check project write access
        role = get_project_assignment(db, user_id, project_id)
        if not role or role not in ['Project Manager', 'Project Contributor']:
            return jsonify({'error': True, 'message': 'You do not have permission to save files to this project'}), 403

        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '').strip()
        name = sanitize_input(data.get('name', 'sequence').strip(), max_length=100)
        description = sanitize_input(data.get('description', '').strip(), max_length=500)
        seq_type = data.get('type', 'dna')

        if not sequence:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        # Clean sequence
        cleaned = clean_sequence(sequence)
        if not cleaned:
            return jsonify({'error': True, 'message': 'Invalid sequence'}), 400

        # Get project path
        project = db.execute('SELECT team_id FROM projects WHERE id = %s', (project_id,)).fetchone()
        if not project:
            return jsonify({'error': True, 'message': 'Project not found'}), 404

        project_path = get_safe_project_path(project_id, project['team_id'])
        if not project_path:
            return jsonify({'error': True, 'message': 'Could not determine project path'}), 500

        # Create sequences subdirectory
        sequences_dir = os.path.join(project_path, 'sequences')
        os.makedirs(sequences_dir, exist_ok=True)

        # Generate filename
        safe_name = secure_filename(name) or 'sequence'
        if not safe_name.endswith('.fasta'):
            safe_name += '.fasta'

        # Ensure unique filename
        file_path = os.path.join(sequences_dir, safe_name)
        base_name = safe_name[:-6]  # Remove .fasta
        counter = 1
        while os.path.exists(file_path):
            safe_name = f"{base_name}_{counter}.fasta"
            file_path = os.path.join(sequences_dir, safe_name)
            counter += 1

        # Create FASTA content
        header = f">{description or name} | Type: {seq_type.upper()} | Length: {len(cleaned)}"
        fasta_content = f"{header}\n"
        # Wrap sequence at 80 characters
        for i in range(0, len(cleaned), 80):
            fasta_content += cleaned[i:i+80] + "\n"

        # Write file
        with open(file_path, 'w') as f:
            f.write(fasta_content)

        file_size = os.path.getsize(file_path)

        # Record in database
        db.execute('BEGIN')
        try:
            result = db.execute('''
                INSERT INTO files (project_id, creator_id, name, path, size, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
            ''', (project_id, user_id, safe_name, file_path, file_size, description or None))
            row = result.fetchone()
            file_id = row['id']
            created_at = row['created_at']

            # Update storage used
            db.execute('UPDATE users SET storage_used = storage_used + %s WHERE id = %s', (file_size, user_id))
            db.commit()

            return jsonify({
                'success': True,
                'file': {
                    'id': file_id,
                    'name': safe_name,
                    'size': file_size,
                    'created_at': created_at.isoformat() if hasattr(created_at, 'isoformat') else str(created_at),
                    'format': 'fasta',
                    'sequence_type': seq_type,
                    'sequence_length': len(cleaned)
                }
            })

        except Exception as e:
            db.rollback()
            if os.path.exists(file_path):
                os.remove(file_path)
            raise e

    except Exception as e:
        logger.error(f"Save sequence error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/project/<int:project_id>/load/<int:file_id>', methods=['GET'])
@login_required
def sequence_load_from_project(project_id, file_id):
    """
    Load sequence from a project file.

    Returns:
        JSON with sequence content and metadata
    """
    try:
        db = get_db()
        user_id = session['user_id']

        # Check project access
        role = get_project_assignment(db, user_id, project_id)
        if not role:
            return jsonify({'error': True, 'message': 'Access denied'}), 403

        # Get file
        file_row = db.execute('''
            SELECT id, name, path, size, notes
            FROM files
            WHERE id = %s AND project_id = %s
        ''', (file_id, project_id)).fetchone()

        if not file_row:
            return jsonify({'error': True, 'message': 'File not found'}), 404

        file_path = file_row['path']
        if not os.path.exists(file_path):
            return jsonify({'error': True, 'message': 'File not found on disk'}), 404

        # Read file content
        with open(file_path, 'r') as f:
            content = f.read()

        # Parse FASTA or plain text
        sequences = []
        current_header = None
        current_seq = []

        lines = content.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('>'):
                if current_header is not None:
                    seq_str = ''.join(current_seq)
                    seq_type = detect_sequence_type(seq_str)
                    sequences.append({
                        'header': current_header,
                        'sequence': seq_str,
                        'type': seq_type,
                        'length': len(seq_str)
                    })
                current_header = line[1:]
                current_seq = []
            elif line and not line.startswith(';'):  # Skip comment lines
                current_seq.append(line.upper().replace(' ', ''))

        # Handle last sequence or non-FASTA content
        if current_header is not None:
            seq_str = ''.join(current_seq)
            seq_type = detect_sequence_type(seq_str)
            sequences.append({
                'header': current_header,
                'sequence': seq_str,
                'type': seq_type,
                'length': len(seq_str)
            })
        elif current_seq or (lines and not any(l.startswith('>') for l in lines)):
            # Plain text sequence
            seq_str = ''.join(l.upper().replace(' ', '') for l in lines if l.strip() and not l.startswith(';'))
            seq_type = detect_sequence_type(seq_str)
            sequences.append({
                'header': file_row['name'],
                'sequence': seq_str,
                'type': seq_type,
                'length': len(seq_str)
            })

        return jsonify({
            'success': True,
            'file': {
                'id': file_row['id'],
                'name': file_row['name'],
                'size': file_row['size'],
                'notes': file_row['notes']
            },
            'sequences': sequences,
            'count': len(sequences)
        })

    except Exception as e:
        logger.error(f"Load sequence error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/project/<int:project_id>/delete/<int:file_id>', methods=['DELETE'])
@login_required
@csrf_protect
def sequence_delete_file(project_id, file_id):
    """
    Delete a sequence file from a project.

    Returns:
        JSON with success status
    """
    try:
        db = get_db()
        user_id = session['user_id']

        # Check project write access
        role = get_project_assignment(db, user_id, project_id)
        if not role or role not in ['Project Manager', 'Project Contributor']:
            return jsonify({'error': True, 'message': 'Permission denied'}), 403

        # Get file
        file_row = db.execute('''
            SELECT id, path, size, creator_id
            FROM files
            WHERE id = %s AND project_id = %s
        ''', (file_id, project_id)).fetchone()

        if not file_row:
            return jsonify({'error': True, 'message': 'File not found'}), 404

        # Delete file from disk
        if os.path.exists(file_row['path']):
            os.remove(file_row['path'])

        # Delete from database and update storage
        db.execute('BEGIN')
        try:
            db.execute('DELETE FROM files WHERE id = %s', (file_id,))
            db.execute('UPDATE users SET storage_used = storage_used - %s WHERE id = %s',
                      (file_row['size'], file_row['creator_id']))
            db.commit()

            return jsonify({'success': True, 'message': 'File deleted'})

        except Exception as e:
            db.rollback()
            raise e

    except Exception as e:
        logger.error(f"Delete file error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


# =============================================================================
# PRIMER DESIGN ENDPOINTS
# =============================================================================

@app.route('/api/sequence/primer/design', methods=['POST'])
@login_required
@csrf_protect
def primer_design():
    """
    Design primer pairs for a DNA sequence.

    Request body:
        sequence: DNA sequence to design primers for
        target_start: Start of target region (optional)
        target_end: End of target region (optional)
        min_length: Minimum primer length (default 18)
        max_length: Maximum primer length (default 25)
        min_tm: Minimum melting temperature (default 55)
        max_tm: Maximum melting temperature (default 65)
        min_gc: Minimum GC content % (default 40)
        max_gc: Maximum GC content % (default 60)
        max_results: Maximum number of pairs to return (default 5)

    Returns:
        JSON with list of primer pairs and their properties
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '')
        if not sequence:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        # Clean and validate sequence
        sequence = clean_sequence(sequence)
        seq_type = detect_sequence_type(sequence)

        if seq_type not in ('dna', 'rna'):
            return jsonify({
                'error': True,
                'message': 'Primer design requires a DNA or RNA sequence'
            }), 400

        # Get optional parameters
        params = {
            'target_start': data.get('target_start'),
            'target_end': data.get('target_end'),
            'min_length': int(data.get('min_length', 18)),
            'max_length': int(data.get('max_length', 25)),
            'optimal_length': int(data.get('optimal_length', 20)),
            'min_tm': float(data.get('min_tm', 55.0)),
            'max_tm': float(data.get('max_tm', 65.0)),
            'optimal_tm': float(data.get('optimal_tm', 60.0)),
            'min_gc': float(data.get('min_gc', 40.0)),
            'max_gc': float(data.get('max_gc', 60.0)),
            'max_results': int(data.get('max_results', 5)),
        }

        # Design primers
        primer_pairs = design_primers(sequence, **params)

        return jsonify({
            'success': True,
            'sequence_length': len(sequence),
            'primer_pairs': primer_pairs,
            'count': len(primer_pairs),
            'parameters': params
        })

    except Exception as e:
        logger.error(f"Primer design error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/primer/analyze', methods=['POST'])
@login_required
@csrf_protect
def primer_analyze():
    """
    Analyze a single primer sequence.

    Request body:
        primer: Primer sequence to analyze

    Returns:
        JSON with primer properties (Tm, GC%, self-complementarity, etc.)
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        primer = data.get('primer', '')
        if not primer:
            return jsonify({'error': True, 'message': 'No primer sequence provided'}), 400

        # Clean and analyze primer
        primer = clean_sequence(primer)
        analysis = analyze_primer(primer)

        return jsonify({
            'success': True,
            'analysis': analysis
        })

    except Exception as e:
        logger.error(f"Primer analysis error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/primer/check-dimer', methods=['POST'])
@login_required
@csrf_protect
def primer_check_dimer():
    """
    Check for primer dimer formation between two primers.

    Request body:
        forward: Forward primer sequence
        reverse: Reverse primer sequence

    Returns:
        JSON with dimer analysis results
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        forward = data.get('forward', '')
        reverse = data.get('reverse', '')

        if not forward or not reverse:
            return jsonify({
                'error': True,
                'message': 'Both forward and reverse primers are required'
            }), 400

        # Clean sequences
        forward = clean_sequence(forward)
        reverse = clean_sequence(reverse)

        # Check for dimer
        dimer_result = check_primer_dimer(forward, reverse)

        # Also get individual analyses
        fwd_analysis = analyze_primer(forward)
        rev_analysis = analyze_primer(reverse)

        return jsonify({
            'success': True,
            'dimer_check': dimer_result,
            'forward_analysis': fwd_analysis,
            'reverse_analysis': rev_analysis,
            'tm_difference': round(abs(fwd_analysis['tm_nearest_neighbor'] - rev_analysis['tm_nearest_neighbor']), 1)
        })

    except Exception as e:
        logger.error(f"Primer dimer check error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/primer/tm', methods=['POST'])
@login_required
@csrf_protect
def primer_tm():
    """
    Calculate melting temperature for a primer.

    Request body:
        primer: Primer sequence
        na_conc: Sodium concentration in mM (optional, default 50)
        mg_conc: Magnesium concentration in mM (optional, default 1.5)

    Returns:
        JSON with Tm calculation results
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        primer = data.get('primer', '')
        if not primer:
            return jsonify({'error': True, 'message': 'No primer sequence provided'}), 400

        na_conc = float(data.get('na_conc', 50.0))
        mg_conc = float(data.get('mg_conc', 1.5))

        primer = clean_sequence(primer)
        tm = calculate_tm_nearest_neighbor(primer, na_conc, mg_conc)

        # Also calculate GC content
        gc = 100 * sum(1 for b in primer.upper() if b in 'GC') / len(primer) if primer else 0

        return jsonify({
            'success': True,
            'primer': primer,
            'tm': tm,
            'gc_content': round(gc, 1),
            'length': len(primer),
            'conditions': {
                'na_conc_mm': na_conc,
                'mg_conc_mm': mg_conc
            }
        })

    except Exception as e:
        logger.error(f"Tm calculation error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


# =============================================================================
# RESTRICTION SITES & MOTIF SEARCH ENDPOINTS
# =============================================================================

@app.route('/api/sequence/restriction-sites', methods=['POST'])
@login_required
@csrf_protect
def find_restriction():
    """
    Find restriction enzyme cut sites in a DNA sequence.

    Request body:
        sequence: DNA sequence to search
        enzymes: List of enzyme names (optional, default: common 6-cutters)

    Returns:
        JSON with list of restriction sites
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '')
        if not sequence:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        # Clean sequence
        sequence = clean_sequence(sequence)

        # Get enzyme list (default to common 6-cutters if not specified)
        enzymes = data.get('enzymes')
        if enzymes is None:
            # Default to common 6-cutters
            enzymes = ['EcoRI', 'BamHI', 'HindIII', 'XhoI', 'SalI', 'PstI', 'XbaI', 'NcoI', 'NdeI', 'NheI']

        sites = find_restriction_sites(sequence, enzymes)

        # Group by enzyme for summary
        by_enzyme = {}
        for site in sites:
            enzyme = site['enzyme']
            if enzyme not in by_enzyme:
                by_enzyme[enzyme] = []
            by_enzyme[enzyme].append(site)

        return jsonify({
            'success': True,
            'sequence_length': len(sequence),
            'sites': sites,
            'count': len(sites),
            'by_enzyme': by_enzyme,
            'enzymes_searched': enzymes
        })

    except Exception as e:
        logger.error(f"Restriction sites error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/enzymes', methods=['GET'])
@login_required
def get_enzymes():
    """
    Get list of available restriction enzymes.

    Query params:
        filter: Optional filter by overhang type ('5prime', '3prime', 'blunt')

    Returns:
        JSON with list of enzymes
    """
    try:
        filter_type = request.args.get('filter')
        enzymes = list_enzymes(filter_type)

        return jsonify({
            'success': True,
            'enzymes': enzymes,
            'count': len(enzymes)
        })

    except Exception as e:
        logger.error(f"List enzymes error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/regulatory-elements', methods=['POST'])
@login_required
@csrf_protect
def find_regulatory():
    """
    Find regulatory elements in a DNA sequence.

    Request body:
        sequence: DNA sequence to search
        elements: List of element names (optional, default: all)

    Returns:
        JSON with list of regulatory elements found
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '')
        if not sequence:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400

        sequence = clean_sequence(sequence)
        elements = data.get('elements')

        results = find_regulatory_elements(sequence, elements)

        # Group by type
        by_type = {}
        for elem in results:
            elem_type = elem['type']
            if elem_type not in by_type:
                by_type[elem_type] = []
            by_type[elem_type].append(elem)

        return jsonify({
            'success': True,
            'sequence_length': len(sequence),
            'elements': results,
            'count': len(results),
            'by_type': by_type,
            'available_elements': list(REGULATORY_ELEMENTS.keys())
        })

    except Exception as e:
        logger.error(f"Regulatory elements error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/motif/search', methods=['POST'])
@login_required
@csrf_protect
def motif_search():
    """
    Search for a custom motif or pattern in a sequence.

    Request body:
        sequence: Sequence to search
        pattern: Pattern to search for (IUPAC codes or regex)
        is_regex: If True, treat pattern as regex (default: False)

    Returns:
        JSON with list of matches
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        sequence = data.get('sequence', '')
        pattern = data.get('pattern', '')

        if not sequence:
            return jsonify({'error': True, 'message': 'No sequence provided'}), 400
        if not pattern:
            return jsonify({'error': True, 'message': 'No pattern provided'}), 400

        sequence = clean_sequence(sequence)
        is_regex = data.get('is_regex', False)

        results = search_motif(sequence, pattern, is_regex)

        # Check for error in results
        if results and 'error' in results[0]:
            return jsonify({
                'success': False,
                'error': True,
                'message': results[0]['error']
            }), 400

        return jsonify({
            'success': True,
            'sequence_length': len(sequence),
            'pattern': pattern,
            'matches': results,
            'count': len(results)
        })

    except Exception as e:
        logger.error(f"Motif search error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


# =============================================================================
# SEQUENCE COMPARISON ENDPOINTS
# =============================================================================

@app.route('/api/sequence/compare', methods=['POST'])
@login_required
def compare_sequences_endpoint():
    """
    Compare two sequences using global, local, or quick alignment.

    Request body:
        seq1: First sequence
        seq2: Second sequence
        method: Alignment method ('global', 'local', 'quick')
        is_protein: Whether sequences are protein (default: False)

    Returns:
        Alignment results with statistics and visualization data
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        seq1 = data.get('seq1', '')
        seq2 = data.get('seq2', '')
        method = data.get('method', 'global')
        is_protein = data.get('is_protein', False)

        if not seq1 or not seq2:
            return jsonify({
                'success': False,
                'error': True,
                'message': 'Two sequences are required for comparison'
            }), 400

        if method not in ['global', 'local', 'quick']:
            return jsonify({
                'success': False,
                'error': True,
                'message': 'Invalid method. Use: global, local, or quick'
            }), 400

        from phonos_app.helpers.sequence_utils import compare_sequences
        result = compare_sequences(seq1, seq2, method, is_protein)

        if not result.get('success', True):
            return jsonify({
                'success': False,
                'error': True,
                'message': result.get('error', 'Comparison failed')
            }), 400

        return jsonify(result)

    except Exception as e:
        logger.error(f"Sequence comparison error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/dotplot', methods=['POST'])
@login_required
def generate_dotplot():
    """
    Generate dot plot data for sequence comparison visualization.

    Request body:
        seq1: First sequence
        seq2: Second sequence
        window_size: Size of comparison window (default: 10)
        threshold: Minimum match ratio (default: 0.7)

    Returns:
        Dot plot data with coordinates and intensities
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        seq1 = data.get('seq1', '')
        seq2 = data.get('seq2', '')
        window_size = data.get('window_size', 10)
        threshold = data.get('threshold', 0.7)

        if not seq1 or not seq2:
            return jsonify({
                'success': False,
                'error': True,
                'message': 'Two sequences are required'
            }), 400

        # Validate parameters
        window_size = max(5, min(50, int(window_size)))
        threshold = max(0.5, min(1.0, float(threshold)))

        from phonos_app.helpers.sequence_utils import generate_dotplot_data
        result = generate_dotplot_data(seq1, seq2, window_size, threshold)

        return jsonify({
            'success': True,
            **result
        })

    except Exception as e:
        logger.error(f"Dotplot generation error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/align', methods=['POST'])
@login_required
def align_sequences():
    """
    Perform sequence alignment with detailed results.

    Request body:
        seq1: First sequence
        seq2: Second sequence
        method: 'global' (Needleman-Wunsch) or 'local' (Smith-Waterman)
        is_protein: Whether sequences are protein

    Returns:
        Detailed alignment results
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        seq1 = data.get('seq1', '')
        seq2 = data.get('seq2', '')
        method = data.get('method', 'global')
        is_protein = data.get('is_protein', False)

        if not seq1 or not seq2:
            return jsonify({
                'success': False,
                'error': True,
                'message': 'Two sequences are required'
            }), 400

        from phonos_app.helpers.sequence_utils import (
            needleman_wunsch, smith_waterman,
            calculate_alignment_stats, generate_alignment_display
        )

        seq1_clean = seq1.upper().replace(' ', '').replace('\n', '')
        seq2_clean = seq2.upper().replace(' ', '').replace('\n', '')

        # Length limit for performance
        max_len = 10000
        if len(seq1_clean) > max_len or len(seq2_clean) > max_len:
            return jsonify({
                'success': False,
                'error': True,
                'message': f'Sequences too long. Maximum length is {max_len} characters.'
            }), 400

        if method == 'local':
            aligned1, aligned2, score, start1, start2 = smith_waterman(seq1_clean, seq2_clean, is_protein)
            result = {
                'success': True,
                'method': 'local',
                'aligned_seq1': aligned1,
                'aligned_seq2': aligned2,
                'score': score,
                'start1': start1,
                'start2': start2,
                'stats': calculate_alignment_stats(aligned1, aligned2),
                'display': generate_alignment_display(aligned1, aligned2)
            }
        else:
            aligned1, aligned2, score = needleman_wunsch(seq1_clean, seq2_clean, is_protein)
            result = {
                'success': True,
                'method': 'global',
                'aligned_seq1': aligned1,
                'aligned_seq2': aligned2,
                'score': score,
                'stats': calculate_alignment_stats(aligned1, aligned2),
                'display': generate_alignment_display(aligned1, aligned2)
            }

        return jsonify(result)

    except Exception as e:
        logger.error(f"Sequence alignment error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500


@app.route('/api/sequence/identity', methods=['POST'])
@login_required
def calculate_identity():
    """
    Calculate quick sequence identity without full alignment.

    Request body:
        seq1: First sequence
        seq2: Second sequence

    Returns:
        Quick identity metrics
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        seq1 = data.get('seq1', '')
        seq2 = data.get('seq2', '')

        if not seq1 or not seq2:
            return jsonify({
                'success': False,
                'error': True,
                'message': 'Two sequences are required'
            }), 400

        seq1_clean = seq1.upper().replace(' ', '').replace('\n', '')
        seq2_clean = seq2.upper().replace(' ', '').replace('\n', '')

        # Direct comparison (no alignment)
        min_len = min(len(seq1_clean), len(seq2_clean))
        max_len = max(len(seq1_clean), len(seq2_clean))

        if min_len == 0:
            return jsonify({
                'success': True,
                'identity': 0,
                'seq1_length': len(seq1_clean),
                'seq2_length': len(seq2_clean)
            })

        # Count matches at each position
        matches = sum(1 for i in range(min_len) if seq1_clean[i] == seq2_clean[i])
        identity = round(matches / max_len * 100, 2)

        # Length difference
        length_diff = abs(len(seq1_clean) - len(seq2_clean))
        length_ratio = round(min_len / max_len * 100, 2)

        return jsonify({
            'success': True,
            'identity': identity,
            'matches': matches,
            'compared_positions': min_len,
            'seq1_length': len(seq1_clean),
            'seq2_length': len(seq2_clean),
            'length_difference': length_diff,
            'length_ratio': length_ratio
        })

    except Exception as e:
        logger.error(f"Identity calculation error: {e}", exc_info=True)
        return jsonify({'error': True, 'message': str(e)}), 500
