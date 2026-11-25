"""API routes for protein structure analysis."""
from functools import lru_cache
import logging
import os
import importlib.util
import shutil
import tempfile
from flask import Flask, jsonify, request, session

from production.database import db_connection
from phonos_app.helpers.security import csrf_protect

logger = logging.getLogger(__name__)


def _require_authenticated_user():
    """Return (user_id, None) if authenticated, or (None, error_response)."""
    user_id = session.get('user_id')
    if not user_id:
        return None, (jsonify({'error': 'Authentication required'}), 401)
    return user_id, None


def _check_project_membership(project_id, user_id):
    """Return True if user_id is a member of project_id."""
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM project_assignments WHERE project_id = %s AND user_id = %s LIMIT 1",
                (project_id, user_id),
            )
            return cursor.fetchone() is not None
    except Exception:
        return False


@lru_cache(maxsize=1)
def _load_analysis_tools():
    """Lazily import the heavy structure analysis helpers.

    Keeping this import out of module scope ensures the API routes are still
    registered even when an optional scientific dependency is missing at app
    startup. In that case the endpoint should return a JSON error, not a 404.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    structure_analysis_path = os.path.join(base_dir, 'helpers', 'structure_analysis.py')
    spec = importlib.util.spec_from_file_location('structure_analysis', structure_analysis_path)
    if spec is None or spec.loader is None:
        raise ImportError(f'Unable to load structure analysis module from {structure_analysis_path}')

    structure_analysis = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(structure_analysis)
    return (
        structure_analysis.StructureAnalyzer,
        getattr(structure_analysis, 'NormalModeAnalyzer', None),
        getattr(structure_analysis, 'MDPrep', None),
    )


def _get_analysis_tools():
    """Return analysis helper classes or a Flask error response."""
    try:
        return _load_analysis_tools(), None
    except Exception as exc:
        logger.warning("Structure analysis dependencies unavailable: %s", exc, exc_info=True)
        return None, (
            jsonify({
                'error': 'Structure analysis is currently unavailable on this server',
                'detail': str(exc),
            }),
            503,
        )


def register_analysis_routes(app: Flask):
    """Register analysis API routes."""

    @app.route('/api/analysis/confidence/<job_id>', methods=['GET'])
    def get_confidence_analysis(job_id):
        """
        Get pLDDT confidence analysis for a structure prediction job.

        Returns pLDDT scores, confidence regions, and statistics.
        """
        try:
            user_id, auth_error = _require_authenticated_user()
            if auth_error:
                return auth_error

            analysis_tools, error_response = _get_analysis_tools()
            if error_response:
                return error_response
            StructureAnalyzer = analysis_tools[0]

            # Get job info
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT j.output_file, j.sequence, j.project_id, j.model
                    FROM jobs j
                    WHERE j.job_id = %s
                """, (job_id,))
                job = cursor.fetchone()

            if not job:
                return jsonify({'error': 'Job not found'}), 404

            # Check project membership
            if not _check_project_membership(job['project_id'], user_id):
                return jsonify({'error': 'Access denied'}), 403

            # Check if this is a structure prediction job
            if job['model'] not in ['type-0', 'type-1']:  # ESMFold variants
                return jsonify({'error': 'Not a structure prediction job'}), 400

            output_file = job['output_file']
            if not output_file or not os.path.exists(output_file):
                return jsonify({'error': 'Structure file not found'}), 404

            # Extract pLDDT data
            analyzer = StructureAnalyzer()
            plddt_data = analyzer.extract_plddt_from_pdb(output_file)

            if not plddt_data:
                return jsonify({'error': 'Could not extract confidence scores'}), 500

            # Classify regions
            regions = analyzer.classify_confidence_regions(plddt_data)

            # Calculate statistics
            scores = [r['plddt'] for r in plddt_data]
            stats = {
                'mean_plddt': round(sum(scores) / len(scores), 2),
                'min_plddt': round(min(scores), 2),
                'max_plddt': round(max(scores), 2),
                'num_residues': len(plddt_data)
            }

            # Region counts
            region_counts = {
                'very_high': len(regions['very_high']),
                'high': len(regions['high']),
                'low': len(regions['low']),
                'very_low': len(regions['very_low'])
            }

            return jsonify({
                'success': True,
                'plddt_scores': plddt_data,
                'confidence_regions': regions,
                'statistics': stats,
                'region_counts': region_counts
            })

        except Exception as e:
            logger.error(f"Error in confidence analysis: {e}", exc_info=True)
            return jsonify({'error': 'Analysis failed'}), 500

    @app.route('/api/analysis/disorder/<job_id>', methods=['GET'])
    def get_disorder_analysis(job_id):
        """
        Get disorder prediction and comparison with pLDDT.

        Identifies truly disordered regions vs. low-confidence predictions.
        """
        try:
            user_id, auth_error = _require_authenticated_user()
            if auth_error:
                return auth_error

            analysis_tools, error_response = _get_analysis_tools()
            if error_response:
                return error_response
            StructureAnalyzer = analysis_tools[0]

            # Get job info
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT j.output_file, j.sequence, j.project_id, j.model
                    FROM jobs j
                    WHERE j.job_id = %s
                """, (job_id,))
                job = cursor.fetchone()

            if not job:
                return jsonify({'error': 'Job not found'}), 404

            # Check project membership
            if not _check_project_membership(job['project_id'], user_id):
                return jsonify({'error': 'Access denied'}), 403

            sequence = job['sequence']
            if not sequence:
                return jsonify({'error': 'No sequence found for job'}), 400

            output_file = job['output_file']
            if not output_file or not os.path.exists(output_file):
                return jsonify({'error': 'Structure file not found'}), 404

            # Get pLDDT data
            analyzer = StructureAnalyzer()
            plddt_data = analyzer.extract_plddt_from_pdb(output_file)

            # Predict disorder
            disorder_scores = analyzer.predict_disorder_iupred(sequence)

            # Compare predictions
            comparison = analyzer.compare_plddt_disorder(plddt_data, disorder_scores)

            return jsonify({
                'success': True,
                'disorder_scores': disorder_scores,
                'comparison': comparison,
                'interpretation': {
                    'consensus_disordered': len(comparison['consensus_disordered']),
                    'low_confidence_only': len(comparison['low_confidence_only']),
                    'predicted_disorder_only': len(comparison['predicted_disorder_only'])
                }
            })

        except Exception as e:
            logger.error(f"Error in disorder analysis: {e}", exc_info=True)
            return jsonify({'error': 'Analysis failed'}), 500

    @app.route('/api/analysis/compare', methods=['POST'])
    def compare_structures():
        """
        Compare two structures and calculate RMSD.

        Expects:
        - model_job_id: Job ID of model structure
        - reference_file: Uploaded PDB file (or reference_job_id)
        """
        try:
            user_id, auth_error = _require_authenticated_user()
            if auth_error:
                return auth_error

            analysis_tools, error_response = _get_analysis_tools()
            if error_response:
                return error_response
            StructureAnalyzer = analysis_tools[0]

            model_job_id = request.form.get('model_job_id')
            reference_job_id = request.form.get('reference_job_id')

            if not model_job_id:
                return jsonify({'error': 'model_job_id required'}), 400

            # Get model structure
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT j.output_file, j.project_id
                    FROM jobs j
                    WHERE j.job_id = %s
                """, (model_job_id,))
                model_job = cursor.fetchone()

            if not model_job:
                return jsonify({'error': 'Model job not found'}), 404

            if not _check_project_membership(model_job['project_id'], user_id):
                return jsonify({'error': 'Access denied'}), 403

            model_path = model_job['output_file']
            if not model_path or not os.path.exists(model_path):
                return jsonify({'error': 'Model structure not found'}), 404

            # Get reference structure
            temp_dir = None
            try:
                if reference_job_id:
                    with db_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT j.output_file, j.project_id
                            FROM jobs j
                            WHERE j.job_id = %s
                        """, (reference_job_id,))
                        ref_job = cursor.fetchone()

                    if not ref_job:
                        return jsonify({'error': 'Reference job not found'}), 404

                    if not _check_project_membership(ref_job['project_id'], user_id):
                        return jsonify({'error': 'Access denied to reference'}), 403

                    reference_path = ref_job['output_file']
                else:
                    # Handle uploaded file
                    if 'reference_file' not in request.files:
                        return jsonify({'error': 'No reference file provided'}), 400

                    file = request.files['reference_file']
                    # Validate extension on the secured filename to avoid client bypass
                    safe_name = os.path.basename(file.filename or '')
                    if not safe_name.endswith(('.pdb', '.cif')):
                        return jsonify({'error': 'Invalid file format (PDB or CIF only)'}), 400

                    temp_dir = tempfile.mkdtemp()
                    reference_path = os.path.join(temp_dir, 'reference.pdb')
                    file.save(reference_path)

                # Calculate RMSD
                analyzer = StructureAnalyzer()
                result = analyzer.calculate_rmsd(model_path, reference_path)
            finally:
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)

            if not result.get('success'):
                return jsonify({'error': result.get('error', 'RMSD calculation failed')}), 500

            return jsonify({
                'success': True,
                'global_rmsd': result['global_rmsd'],
                'aligned_residues': result['aligned_residues'],
                'per_residue_rmsd': result['per_residue_rmsd']
            })

        except Exception as e:
            logger.error(f"Error comparing structures: {e}", exc_info=True)
            return jsonify({'error': 'Comparison failed'}), 500

    @app.route('/api/analysis/summary/<job_id>', methods=['GET'])
    def get_structure_summary(job_id):
        """Get structure summary statistics."""
        try:
            user_id, auth_error = _require_authenticated_user()
            if auth_error:
                return auth_error

            analysis_tools, error_response = _get_analysis_tools()
            if error_response:
                return error_response
            StructureAnalyzer = analysis_tools[0]

            # Get job info
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT j.output_file, j.project_id
                    FROM jobs j
                    WHERE j.job_id = %s
                """, (job_id,))
                job = cursor.fetchone()

            if not job:
                return jsonify({'error': 'Job not found'}), 404

            # Check project membership
            if not _check_project_membership(job['project_id'], user_id):
                return jsonify({'error': 'Access denied'}), 403

            output_file = job['output_file']
            if not output_file or not os.path.exists(output_file):
                return jsonify({'error': 'Structure file not found'}), 404

            # Get summary
            analyzer = StructureAnalyzer()
            summary = analyzer.get_structure_summary(output_file)

            return jsonify({
                'success': True,
                **summary
            })

        except Exception as e:
            logger.error(f"Error getting structure summary: {e}", exc_info=True)
            return jsonify({'error': 'Analysis failed'}), 500

    @app.route('/api/analysis/deep', methods=['POST'])
    def deep_structure_analysis():
        """Run comprehensive structural analysis on PDB text.

        Expects JSON body: { "pdb_text": "<PDB file contents>" }

        Returns binding pockets, surface chemistry, structural domains,
        flexibility/dynamics (GNM), and quaternary structure analysis.
        """
        try:
            _, auth_error = _require_authenticated_user()
            if auth_error:
                return auth_error

            analysis_tools, error_response = _get_analysis_tools()
            if error_response:
                return error_response
            StructureAnalyzer = analysis_tools[0]

            data = request.get_json(silent=True)
            if not data or 'pdb_text' not in data:
                return jsonify({'error': 'pdb_text required in JSON body'}), 400

            pdb_text = data['pdb_text']
            if not pdb_text or len(pdb_text) < 50:
                return jsonify({'error': 'PDB text too short'}), 400

            # Limit input size (20 MB)
            if len(pdb_text) > 20 * 1024 * 1024:
                return jsonify({'error': 'PDB text exceeds 20 MB limit'}), 400

            analyzer = StructureAnalyzer()
            result = analyzer.deep_analysis(pdb_text)

            if not result.get('success'):
                return jsonify({'error': result.get('error', 'Analysis failed')}), 500

            return jsonify(result)

        except Exception as e:
            logger.error(f"Error in deep structure analysis: {e}", exc_info=True)
            return jsonify({'error': 'Analysis failed'}), 500

    @app.route('/api/analysis/interactions', methods=['POST'])
    @csrf_protect
    def analyze_protein_ligand_interactions():
        """Real protein-ligand interaction typing (hydrogen bonds, hydrophobic
        contacts, salt bridges, pi-stacking) - replaces the old client-side
        "any N/O within 3.5A" heuristic with RDKit/Biopython-based geometry
        and chemistry (see phonos_app/helpers/interaction_analysis.py for the
        exact criteria).

        Expects JSON body:
        {
          "protein_text": "<PDB or mmCIF text>",
          "protein_format": "pdb" | "cif",
          "ligand_text": "<SDF/MOL/MOL2 text with 3D coordinates>",
          "ligand_format": "sdf" | "mol" | "mol2",
          "distance_cutoff": 5.0   (optional, Angstrom, default 5.0)
        }

        The ligand must be supplied separately with real bond orders (as
        produced by RDKit-based SMILES->3D generation, a docking pose SDF,
        etc.) - this endpoint does not attempt bond-order perception on a
        combined complex's raw HETATM records, since that is unreliable for
        chemistry-sensitive typing (donor/acceptor/aromatic/charge
        assignment needs real bonds, not just atom connectivity).
        """
        try:
            _, auth_error = _require_authenticated_user()
            if auth_error:
                return auth_error

            data = request.get_json(silent=True)
            if not data:
                return jsonify({'error': 'JSON body required'}), 400

            protein_text = data.get('protein_text')
            ligand_text = data.get('ligand_text')
            if not protein_text or not ligand_text:
                return jsonify({'error': 'protein_text and ligand_text are both required'}), 400
            if len(protein_text) > 20 * 1024 * 1024 or len(ligand_text) > 5 * 1024 * 1024:
                return jsonify({'error': 'Input exceeds size limit'}), 400

            try:
                distance_cutoff = float(data.get('distance_cutoff', 5.0))
            except (TypeError, ValueError):
                distance_cutoff = 5.0
            distance_cutoff = max(3.0, min(distance_cutoff, 8.0))

            try:
                from phonos_app.helpers.interaction_analysis import analyze_interactions
                result = analyze_interactions(
                    protein_text=protein_text,
                    protein_format=data.get('protein_format', 'pdb'),
                    ligand_text=ligand_text,
                    ligand_format=data.get('ligand_format', 'sdf'),
                    distance_cutoff=distance_cutoff,
                )
            except ImportError as exc:
                logger.warning("Interaction analysis dependencies unavailable: %s", exc, exc_info=True)
                return jsonify({
                    'error': 'Interaction analysis is currently unavailable on this server',
                    'detail': str(exc),
                }), 503
            return jsonify(result)

        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error(f"Error in protein-ligand interaction analysis: {e}", exc_info=True)
            return jsonify({'error': 'Interaction analysis failed'}), 500

    @app.route('/api/analysis/ligand-diagram', methods=['POST'])
    @csrf_protect
    def ligand_interaction_diagram():
        """Full 2D ligand-interaction-diagram payload for a protein-ligand complex.

        Expects JSON body:
        {
          "structure_text": "<PDB or mmCIF text of the complex>",
          "structure_format": "pdb" | "cif",
          "ligand_text": "<optional SDF/MOL/MOL2 with real bond orders>",
          "ligand_format": "sdf" | "mol" | "mol2",
          "ligand_selector": {"chain": "A", "resnum": "401", "resname": "ATP"},
          "distance_cutoff": 5.0
        }

        Unlike /api/analysis/interactions - which requires the caller to supply a
        separately-uploaded ligand file - this endpoint falls back to extracting
        the selected ligand straight out of the complex's HETATM records
        (helpers/ligand_split.py, RDKit connectivity perception). That extraction
        recovers connectivity but not bond orders, so aromaticity- and
        charge-sensitive typing is weaker than with a real ligand file; the
        response flags which path was taken via "ligand_source" so the UI can say
        so. Supplying "ligand_text" always takes precedence.

        Returns the 2D depiction, typed interactions, per-residue roll-up,
        per-atom solvent exposure and ligand descriptors.
        """
        try:
            _, auth_error = _require_authenticated_user()
            if auth_error:
                return auth_error

            data = request.get_json(silent=True)
            if not data:
                return jsonify({'error': 'JSON body required'}), 400

            structure_text = data.get('structure_text')
            if not structure_text:
                return jsonify({'error': 'structure_text is required'}), 400
            if len(structure_text) > 20 * 1024 * 1024:
                return jsonify({'error': 'Input exceeds size limit'}), 400

            structure_format = (data.get('structure_format') or 'pdb').lower()
            ligand_text = data.get('ligand_text')
            ligand_format = (data.get('ligand_format') or 'sdf').lower()
            ligand_source = 'uploaded'

            if ligand_text and len(ligand_text) > 5 * 1024 * 1024:
                return jsonify({'error': 'Input exceeds size limit'}), 400

            try:
                distance_cutoff = float(data.get('distance_cutoff', 5.0))
            except (TypeError, ValueError):
                distance_cutoff = 5.0
            distance_cutoff = max(3.0, min(distance_cutoff, 8.0))

            try:
                from phonos_app.helpers import ligand_split
                from phonos_app.helpers.ligand_diagram import (
                    build_ligand_diagram, perceive_aromatic_rings,
                )

                available = ligand_split.list_ligand_residues(structure_text, structure_format)

                if not ligand_text:
                    selector = data.get('ligand_selector') or None
                    if selector is not None and not isinstance(selector, dict):
                        return jsonify({'error': 'ligand_selector must be an object'}), 400
                    ligand_text = ligand_split.extract_ligand_sdf(
                        structure_text, structure_format, selector)
                    ligand_format = 'sdf'
                    ligand_source = 'extracted'
                    if not ligand_text:
                        return jsonify({
                            'error': 'No ligand found in the structure',
                            'detail': ('The structure has no non-water, non-ion HETATM records'
                                       + (' matching the requested selector.' if selector
                                          else '.')),
                            'available_ligands': available,
                        }), 400
                    # HETATM records carry no bond orders, so nothing comes back
                    # aromatic; recovering that from ring geometry is what makes
                    # pi-stacking and cation-pi detectable on an extracted ligand.
                    ligand_text, aromatic_rings = perceive_aromatic_rings(ligand_text)
                    if aromatic_rings:
                        ligand_source = 'extracted_aromaticity_perceived'

                result = build_ligand_diagram(
                    protein_text=structure_text,
                    protein_format=structure_format,
                    ligand_text=ligand_text,
                    ligand_format=ligand_format,
                    distance_cutoff=distance_cutoff,
                )
            except ImportError as exc:
                logger.warning("Ligand diagram dependencies unavailable: %s", exc, exc_info=True)
                return jsonify({
                    'error': 'Ligand interaction diagrams are currently unavailable on this server',
                    'detail': str(exc),
                }), 503

            result['ligand_source'] = ligand_source
            result['available_ligands'] = available
            return jsonify(result)

        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error(f"Error building ligand interaction diagram: {e}", exc_info=True)
            return jsonify({'error': 'Ligand interaction diagram failed'}), 500
