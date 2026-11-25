"""Structure analysis tools for post-prediction analysis."""
import json
import logging
import tempfile
import os
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from Bio import PDB
from Bio.PDB import PDBIO, Superimposer, NeighborSearch
from Bio.PDB.MMCIFParser import MMCIFParser
from Bio.PDB.SASA import ShrakeRupley
from io import StringIO

logger = logging.getLogger(__name__)


class StructureAnalyzer:
    """Tools for analyzing protein structures."""

    @staticmethod
    def extract_plddt_from_pdb(pdb_path: str) -> List[Dict]:
        """
        Extract pLDDT scores from PDB file B-factor column.

        AlphaFold and ESMFold store per-residue confidence in the B-factor field.

        Args:
            pdb_path: Path to PDB file

        Returns:
            List of dicts with residue info and pLDDT scores
        """
        try:
            parser = PDB.PDBParser(QUIET=True)
            structure = parser.get_structure('protein', pdb_path)

            plddt_data = []
            for model in structure:
                for chain in model:
                    for residue in chain:
                        if residue.id[0] == ' ':  # Standard residue
                            # Average B-factors across atoms in residue
                            b_factors = [atom.get_bfactor() for atom in residue.get_atoms()]
                            avg_bfactor = np.mean(b_factors)

                            plddt_data.append({
                                'chain': chain.id,
                                'residue_number': residue.id[1],
                                'residue_name': residue.resname,
                                'plddt': round(float(avg_bfactor), 2)
                            })

            return plddt_data
        except Exception as e:
            logger.error(f"Error extracting pLDDT from {pdb_path}: {e}")
            return []

    @staticmethod
    def classify_confidence_regions(plddt_data: List[Dict]) -> Dict:
        """
        Classify residues into confidence regions.

        Ranges:
        - Very high: pLDDT > 90
        - High: 70 < pLDDT <= 90
        - Low: 50 < pLDDT <= 70
        - Very low: pLDDT <= 50

        Args:
            plddt_data: List of residue dictionaries with pLDDT scores

        Returns:
            Dict with classified regions
        """
        regions = {
            'very_high': [],
            'high': [],
            'low': [],
            'very_low': []
        }

        for residue in plddt_data:
            plddt = residue['plddt']
            entry = {
                'chain': residue['chain'],
                'residue_number': residue['residue_number'],
                'residue_name': residue['residue_name'],
                'plddt': plddt
            }

            if plddt > 90:
                regions['very_high'].append(entry)
            elif plddt > 70:
                regions['high'].append(entry)
            elif plddt > 50:
                regions['low'].append(entry)
            else:
                regions['very_low'].append(entry)

        return regions

    @staticmethod
    def predict_disorder_iupred(sequence: str) -> List[float]:
        """
        Predict intrinsic disorder using IUPred3-like algorithm.

        This is a simplified implementation. For production, integrate actual IUPred3.

        Args:
            sequence: Amino acid sequence

        Returns:
            List of disorder scores (0-1) per residue
        """
        # Simplified disorder prediction based on amino acid composition
        # Real implementation should use IUPred3 or similar
        disorder_propensities = {
            'A': 0.06, 'R': 0.18, 'N': 0.16, 'D': 0.15, 'C': 0.02,
            'Q': 0.17, 'E': 0.15, 'G': 0.19, 'H': 0.08, 'I': 0.01,
            'L': 0.03, 'K': 0.20, 'M': 0.03, 'F': 0.02, 'P': 0.22,
            'S': 0.12, 'T': 0.09, 'W': 0.03, 'Y': 0.05, 'V': 0.02
        }

        scores = []
        window_size = 21  # Typical IUPred window
        half_window = window_size // 2

        for i in range(len(sequence)):
            # Get window around position
            start = max(0, i - half_window)
            end = min(len(sequence), i + half_window + 1)
            window = sequence[start:end]

            # Calculate average disorder propensity
            window_scores = [disorder_propensities.get(aa, 0.10) for aa in window]
            avg_score = np.mean(window_scores)
            scores.append(round(float(avg_score), 3))

        return scores

    @staticmethod
    def compare_plddt_disorder(plddt_data: List[Dict], disorder_scores: List[float]) -> Dict:
        """
        Compare pLDDT and disorder predictions to identify true disordered regions.

        Args:
            plddt_data: pLDDT scores per residue
            disorder_scores: Disorder scores per residue

        Returns:
            Analysis comparing both predictions
        """
        consensus_disordered = []
        low_confidence_only = []
        predicted_disorder_only = []

        for i, residue in enumerate(plddt_data):
            plddt = residue['plddt']
            disorder = disorder_scores[i] if i < len(disorder_scores) else 0.0

            low_plddt = plddt < 70
            high_disorder = disorder > 0.15

            entry = {
                'residue_number': residue['residue_number'],
                'plddt': plddt,
                'disorder_score': disorder
            }

            if low_plddt and high_disorder:
                consensus_disordered.append(entry)
            elif low_plddt and not high_disorder:
                low_confidence_only.append(entry)
            elif not low_plddt and high_disorder:
                predicted_disorder_only.append(entry)

        return {
            'consensus_disordered': consensus_disordered,
            'low_confidence_only': low_confidence_only,
            'predicted_disorder_only': predicted_disorder_only
        }

    @staticmethod
    def calculate_rmsd(structure1_path: str, structure2_path: str) -> Dict:
        """
        Calculate RMSD between two structures using superimposition.

        Args:
            structure1_path: Path to first PDB file
            structure2_path: Path to second PDB file (reference)

        Returns:
            Dict with RMSD values and alignment info
        """
        try:
            parser = PDB.PDBParser(QUIET=True)
            structure1 = parser.get_structure('model', structure1_path)
            structure2 = parser.get_structure('reference', structure2_path)

            # Get CA atoms for alignment
            model1_atoms = []
            model2_atoms = []

            for model in structure1:
                for chain in model:
                    for residue in chain:
                        if 'CA' in residue:
                            model1_atoms.append(residue['CA'])

            for model in structure2:
                for chain in model:
                    for residue in chain:
                        if 'CA' in residue:
                            model2_atoms.append(residue['CA'])

            # Ensure same number of atoms
            min_atoms = min(len(model1_atoms), len(model2_atoms))
            model1_atoms = model1_atoms[:min_atoms]
            model2_atoms = model2_atoms[:min_atoms]

            # Superimpose
            super_imposer = Superimposer()
            super_imposer.set_atoms(model2_atoms, model1_atoms)
            super_imposer.apply(structure1.get_atoms())

            # Calculate RMSD
            rmsd = super_imposer.rms

            # Per-residue RMSD
            per_residue_rmsd = []
            for i in range(min_atoms):
                atom1 = model1_atoms[i]
                atom2 = model2_atoms[i]
                diff = atom1.coord - atom2.coord
                dist = np.sqrt(np.sum(diff ** 2))
                per_residue_rmsd.append({
                    'residue_number': i + 1,
                    'rmsd': round(float(dist), 3)
                })

            # Generate aligned PDB
            io = PDBIO()
            io.set_structure(structure1)
            aligned_pdb = StringIO()
            io.save(aligned_pdb)

            return {
                'global_rmsd': round(float(rmsd), 3),
                'aligned_residues': min_atoms,
                'per_residue_rmsd': per_residue_rmsd,
                'aligned_pdb': aligned_pdb.getvalue(),
                'success': True
            }

        except Exception as e:
            logger.error(f"Error calculating RMSD: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    @staticmethod
    def get_structure_summary(pdb_path: str) -> Dict:
        """
        Get summary statistics for a structure.

        Args:
            pdb_path: Path to PDB file

        Returns:
            Dict with structure stats
        """
        try:
            parser = PDB.PDBParser(QUIET=True)
            structure = parser.get_structure('protein', pdb_path)

            num_residues = 0
            num_atoms = 0
            chains = set()

            for model in structure:
                for chain in model:
                    chains.add(chain.id)
                    for residue in chain:
                        if residue.id[0] == ' ':
                            num_residues += 1
                            num_atoms += len(residue.get_atoms())

            return {
                'num_chains': len(chains),
                'num_residues': num_residues,
                'num_atoms': num_atoms,
                'chains': list(chains)
            }
        except Exception as e:
            logger.error(f"Error getting structure summary: {e}")
            return {}


    # ── Amino acid property tables ──────────────────────────────────

    HYDROPHOBICITY = {
        'ALA': 1.8, 'ARG': -4.5, 'ASN': -3.5, 'ASP': -3.5, 'CYS': 2.5,
        'GLN': -3.5, 'GLU': -3.5, 'GLY': -0.4, 'HIS': -3.2, 'ILE': 4.5,
        'LEU': 3.8, 'LYS': -3.9, 'MET': 1.9, 'PHE': 2.8, 'PRO': -1.6,
        'SER': -0.8, 'THR': -0.7, 'TRP': -0.9, 'TYR': -1.3, 'VAL': 4.2,
    }

    CHARGE_PH7 = {
        'ARG': 1.0, 'LYS': 1.0, 'HIS': 0.1,
        'ASP': -1.0, 'GLU': -1.0,
    }

    AA_CATEGORY = {
        'ALA': 'hydrophobic', 'VAL': 'hydrophobic', 'ILE': 'hydrophobic',
        'LEU': 'hydrophobic', 'MET': 'hydrophobic', 'PHE': 'hydrophobic',
        'TRP': 'hydrophobic', 'PRO': 'hydrophobic',
        'GLY': 'polar', 'SER': 'polar', 'THR': 'polar', 'CYS': 'polar',
        'TYR': 'polar', 'ASN': 'polar', 'GLN': 'polar',
        'ARG': 'positive', 'LYS': 'positive', 'HIS': 'positive',
        'ASP': 'negative', 'GLU': 'negative',
    }

    # Known structural motifs for domain annotation
    KNOWN_FOLDS = {
        'kinase': {'min_helix_pct': 0.25, 'min_sheet_pct': 0.10, 'min_size': 200,
                   'description': 'Kinase-like fold (mixed alpha/beta with catalytic cleft)'},
        'immunoglobulin': {'min_helix_pct': 0.0, 'min_sheet_pct': 0.35, 'min_size': 80,
                           'max_size': 130, 'description': 'Immunoglobulin-like beta sandwich'},
        'alpha_bundle': {'min_helix_pct': 0.60, 'min_sheet_pct': 0.0, 'max_sheet_pct': 0.05,
                         'min_size': 50, 'description': 'Alpha-helical bundle domain'},
        'beta_barrel': {'min_helix_pct': 0.0, 'max_helix_pct': 0.10, 'min_sheet_pct': 0.45,
                        'min_size': 60, 'description': 'Beta-barrel domain'},
        'rossmann': {'min_helix_pct': 0.20, 'min_sheet_pct': 0.20, 'min_size': 120,
                     'description': 'Rossmann-like alpha/beta/alpha fold (nucleotide binding)'},
        'transmembrane': {'min_helix_pct': 0.50, 'min_size': 150,
                          'description': 'Transmembrane helical bundle'},
    }

    # ── Deep analysis entry point ────────────────────────────────

    @staticmethod
    def deep_analysis(pdb_text: str) -> Dict:
        """Run comprehensive structural analysis on PDB text.

        Returns pocket detection, surface chemistry, domain decomposition,
        GNM flexibility analysis, and quaternary structure characterization.
        """
        try:
            # Detect format (CIF vs PDB)
            is_cif = pdb_text.lstrip().startswith('data_') or '_atom_site.' in pdb_text[:2000]
            suffix = '.cif' if is_cif else '.pdb'

            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode='w')
            tmp.write(pdb_text)
            tmp.close()
            tmp_path = tmp.name

            try:
                if is_cif:
                    parser = MMCIFParser(QUIET=True)
                else:
                    parser = PDB.PDBParser(QUIET=True)
                structure = parser.get_structure('query', tmp_path)
            finally:
                os.unlink(tmp_path)

            if not list(structure.get_models()):
                return {'success': False, 'error': 'No models found in structure'}
            model = list(structure.get_models())[0]

            # Collect residue data
            residues = []  # list of (chain_id, resnum, resname, ca_coord, residue_obj)
            ca_coords = []
            all_atoms = []

            for chain in model:
                for residue in chain:
                    if residue.id[0] != ' ':
                        continue  # skip hetero
                    atoms = list(residue.get_atoms())
                    all_atoms.extend(atoms)
                    ca = residue['CA'] if 'CA' in residue else None
                    if ca is None:
                        continue
                    coord = ca.get_vector().get_array()
                    residues.append({
                        'chain': chain.id,
                        'resnum': residue.id[1],
                        'resname': residue.resname,
                        'coord': coord,
                        'residue': residue,
                        'bfactor': float(np.mean([a.get_bfactor() for a in atoms])),
                    })
                    ca_coords.append(coord)

            if not residues:
                return {'success': False, 'error': 'No protein residues found'}

            ca_coords = np.array(ca_coords, dtype=np.float64)
            n = len(residues)

            # Build contact map (CA-CA distance < cutoff)
            cutoff = 8.0
            dist_matrix = np.zeros((n, n), dtype=np.float64)
            for i in range(n):
                diff = ca_coords[i] - ca_coords
                dist_matrix[i] = np.sqrt(np.sum(diff ** 2, axis=1))
            contact_map = dist_matrix < cutoff

            # Compute SASA
            sasa_values = StructureAnalyzer._compute_sasa(structure, residues)

            # Run all analyses
            analyzer = StructureAnalyzer()
            pockets = analyzer._analyze_pockets(residues, ca_coords, dist_matrix, sasa_values)
            surface = analyzer._analyze_surface_chemistry(residues, ca_coords, dist_matrix, sasa_values)
            domains = analyzer._analyze_domains(residues, ca_coords, contact_map, dist_matrix)
            flexibility = analyzer._analyze_flexibility(residues, ca_coords, contact_map, n)
            quaternary = analyzer._analyze_quaternary(model, residues, ca_coords, sasa_values)

            # Build per-residue color maps for NGL overlay
            residue_map = []
            for i, r in enumerate(residues):
                entry = {
                    'chain': r['chain'],
                    'resnum': r['resnum'],
                    'resname': r['resname'],
                    'sasa': round(float(sasa_values[i]), 2),
                    'charge': float(StructureAnalyzer.CHARGE_PH7.get(r['resname'], 0.0)),
                    'hydrophobicity': float(StructureAnalyzer.HYDROPHOBICITY.get(r['resname'], 0.0)),
                    'pocket_id': -1,
                    'domain_id': -1,
                    'is_interface': False,
                }
                # Tag pocket membership
                for pi, pocket in enumerate(pockets):
                    sel = pocket.get('selection', '')
                    key = str(r['resnum']) + ':' + r['chain']
                    if key in sel or str(r['resnum']) + '-' in sel:
                        entry['pocket_id'] = pi
                        break
                # Tag domain membership
                for di, dom in enumerate(domains):
                    if dom.get('chain') == r['chain']:
                        rng = dom.get('residue_range', '')
                        if rng and r['chain'] + ':' in rng:
                            try:
                                parts = rng.split(r['chain'] + ':')[1].split(',')[0].split('-')
                                rstart = int(parts[0].strip())
                                rend = int(parts[-1].strip()) if len(parts) > 1 else rstart
                                if rstart <= r['resnum'] <= rend:
                                    entry['domain_id'] = di
                            except (ValueError, IndexError):
                                pass
                # Tag interface membership
                for iface in quaternary.get('interfaces', []):
                    sel = iface.get('selection', '')
                    key = str(r['resnum']) + ':' + r['chain']
                    if key in sel:
                        entry['is_interface'] = True
                        break
                # GNM fluctuation
                flucts = flexibility.get('fluctuations', [])
                entry['fluctuation'] = flucts[i]['value'] if i < len(flucts) else 0.0
                residue_map.append(entry)

            return {
                'success': True,
                'num_residues': n,
                'num_chains': len(set(r['chain'] for r in residues)),
                'pockets': pockets,
                'surface': surface,
                'domains': domains,
                'flexibility': flexibility,
                'quaternary': quaternary,
                'residue_map': residue_map,
            }
        except Exception as e:
            logger.error(f"Deep analysis error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    # ── SASA computation ─────────────────────────────────────────

    @staticmethod
    def _compute_sasa(structure, residues) -> List[float]:
        """Compute per-residue SASA using Shrake-Rupley algorithm."""
        try:
            sr = ShrakeRupley()
            sr.compute(structure[0], level='R')
            sasa_map = {}
            for chain in structure[0]:
                for residue in chain:
                    if residue.id[0] != ' ':
                        continue
                    key = (chain.id, residue.id[1])
                    sasa_map[key] = residue.sasa if hasattr(residue, 'sasa') else 0.0
            return [sasa_map.get((r['chain'], r['resnum']), 0.0) for r in residues]
        except Exception as e:
            logger.warning(f"SASA computation failed, using fallback: {e}")
            # Fallback: estimate from neighbor count
            return [50.0] * len(residues)

    # ── 1. Binding Sites and Pockets ─────────────────────────────

    def _analyze_pockets(self, residues, ca_coords, dist_matrix, sasa_values) -> List[Dict]:
        """Detect binding pockets using SASA + geometric cavity analysis.

        Strategy: surface residues (high SASA) that sit in concavities
        (surrounded by other surface residues in a roughly hemispherical
        arrangement) are likely pocket-lining residues.
        """
        n = len(residues)
        if n < 10:
            return []

        sasa_arr = np.array(sasa_values)
        # Surface residues: SASA > 30th percentile (exposed)
        sasa_thresh = max(np.percentile(sasa_arr, 30), 15.0)
        surface_mask = sasa_arr >= sasa_thresh

        # Pocket candidates: surface residues with moderate burial
        # (between 30th and 70th percentile SASA — partially exposed)
        sasa_mid_hi = np.percentile(sasa_arr, 70)
        pocket_candidate_mask = surface_mask & (sasa_arr <= sasa_mid_hi)

        # For each candidate, score by local geometry:
        # count nearby surface residues within 12 Å and check if they
        # wrap around (high angular span = concavity)
        pocket_scores = np.zeros(n)
        for i in range(n):
            if not pocket_candidate_mask[i]:
                continue
            neighbors = []
            for j in range(n):
                if i == j or dist_matrix[i, j] > 12.0:
                    continue
                if surface_mask[j]:
                    neighbors.append(j)
            if len(neighbors) < 4:
                continue
            # Compute angular coverage (solid angle proxy)
            vecs = ca_coords[neighbors] - ca_coords[i]
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms[norms < 0.01] = 1.0
            unit_vecs = vecs / norms
            # Angular span: average pairwise dot product < 0 means residues
            # surround the candidate (concavity)
            n_nb = len(unit_vecs)
            if n_nb > 50:
                # sample to avoid O(n²) blowup
                idx = np.random.choice(n_nb, 50, replace=False)
                unit_vecs = unit_vecs[idx]
                n_nb = 50
            dot_sum = 0.0
            count = 0
            for a in range(n_nb):
                for b in range(a + 1, n_nb):
                    dot_sum += np.dot(unit_vecs[a], unit_vecs[b])
                    count += 1
            avg_dot = dot_sum / max(count, 1)
            # More negative avg_dot = more surrounding = deeper pocket
            depth_score = -avg_dot  # range roughly -1 to +1
            pocket_scores[i] = max(0, depth_score) * len(neighbors)

        # Cluster high-scoring residues
        pockets = []
        scored_indices = np.argsort(-pocket_scores)
        used = set()

        for seed in scored_indices:
            if pocket_scores[seed] < 1.0:
                break
            if seed in used:
                continue
            # Grow cluster
            cluster = [seed]
            used.add(seed)
            for j in range(n):
                if j in used or pocket_scores[j] < 0.5:
                    continue
                if dist_matrix[seed, j] < 12.0:
                    cluster.append(j)
                    used.add(j)
            if len(cluster) < 4:
                continue

            cluster_residues = [residues[i] for i in cluster]
            center = np.mean(ca_coords[cluster], axis=0)

            # Characterize pocket chemistry
            chem = self._summarize_chemistry(cluster_residues)
            avg_sasa = float(np.mean(sasa_arr[cluster]))

            # Estimate pocket volume (convex hull proxy)
            volume = self._estimate_pocket_volume(ca_coords[cluster])

            # Score druggability
            hydrophobic_frac = sum(1 for r in cluster_residues
                                   if self.AA_CATEGORY.get(r['resname']) == 'hydrophobic') / max(len(cluster_residues), 1)
            druggability = 'High' if hydrophobic_frac > 0.35 and len(cluster) > 8 else \
                           'Medium' if hydrophobic_frac > 0.2 or len(cluster) > 6 else 'Low'

            res_range = self._format_residue_range(cluster_residues)
            selection = self._build_ngl_selection(cluster_residues)

            pockets.append({
                'title': f'Pocket {len(pockets) + 1}',
                'residue_count': len(cluster),
                'volume_approx': round(volume, 1),
                'avg_sasa': round(avg_sasa, 1),
                'chemistry': chem,
                'druggability': druggability,
                'hydrophobic_fraction': round(hydrophobic_frac, 2),
                'residue_range': res_range,
                'selection': selection,
                'center': [round(float(c), 2) for c in center],
                'description': (
                    f'{len(cluster)} pocket-lining residues forming a '
                    f'{"deep" if volume > 800 else "moderate" if volume > 300 else "shallow"} '
                    f'cavity (~{round(volume)} A^3). '
                    f'{chem}. Druggability: {druggability}.'
                ),
            })
            if len(pockets) >= 5:
                break

        return pockets

    @staticmethod
    def _estimate_pocket_volume(coords) -> float:
        """Estimate pocket volume from coordinate spread."""
        if len(coords) < 4:
            return 0.0
        # Bounding box volume as rough proxy
        mins = np.min(coords, axis=0)
        maxs = np.max(coords, axis=0)
        span = maxs - mins
        # Ellipsoid approximation (4/3 * pi * a * b * c / 8)
        return float(np.prod(span) * np.pi / 6.0)

    # ── 2. Surface Chemistry ─────────────────────────────────────

    def _analyze_surface_chemistry(self, residues, ca_coords, dist_matrix, sasa_values) -> Dict:
        """Map electrostatic potential and hydrophobic patches on surface."""
        n = len(residues)
        sasa_arr = np.array(sasa_values)
        sasa_thresh = max(np.percentile(sasa_arr, 35), 15.0)
        surface_indices = [i for i in range(n) if sasa_arr[i] >= sasa_thresh]

        # Per-residue electrostatic charge
        charges = np.array([self.CHARGE_PH7.get(r['resname'], 0.0) for r in residues])
        # Per-residue hydrophobicity
        hydrophobicity = np.array([self.HYDROPHOBICITY.get(r['resname'], 0.0) for r in residues])

        # Coulomb-like electrostatic potential at each surface residue
        # V(i) = sum_j q_j / r_ij (simplified, no dielectric)
        surface_potential = []
        for i in surface_indices:
            pot = 0.0
            for j in range(n):
                if i == j or charges[j] == 0.0:
                    continue
                d = max(dist_matrix[i, j], 1.0)
                pot += charges[j] / d
            surface_potential.append(pot)

        surface_potential = np.array(surface_potential) if surface_potential else np.array([0.0])

        # Find electrostatic patches (clusters of same-charge surface residues)
        positive_patches = self._find_surface_patches(
            residues, ca_coords, dist_matrix, surface_indices,
            lambda i: charges[i] > 0.5, 'Positive electrostatic patch',
            '#3b82f6')
        negative_patches = self._find_surface_patches(
            residues, ca_coords, dist_matrix, surface_indices,
            lambda i: charges[i] < -0.5, 'Negative electrostatic patch',
            '#ef4444')
        hydrophobic_patches = self._find_surface_patches(
            residues, ca_coords, dist_matrix, surface_indices,
            lambda i: hydrophobicity[i] > 1.5, 'Hydrophobic patch',
            '#f59e0b')

        # Overall surface stats
        surface_residues = [residues[i] for i in surface_indices]
        total_charge = float(np.sum(charges[surface_indices]))
        avg_hydrophobicity = float(np.mean(hydrophobicity[surface_indices])) if surface_indices else 0.0

        return {
            'total_surface_residues': len(surface_indices),
            'net_surface_charge': round(total_charge, 1),
            'avg_surface_hydrophobicity': round(avg_hydrophobicity, 2),
            'electrostatic_character': (
                'Strongly positive' if total_charge > 5 else
                'Positive' if total_charge > 2 else
                'Neutral' if abs(total_charge) <= 2 else
                'Negative' if total_charge > -5 else
                'Strongly negative'
            ),
            'membrane_association_likelihood': (
                'High' if avg_hydrophobicity > 0.5 and any(
                    p['residue_count'] > 8 for p in hydrophobic_patches) else
                'Medium' if avg_hydrophobicity > 0.0 else 'Low'
            ),
            'positive_patches': positive_patches,
            'negative_patches': negative_patches,
            'hydrophobic_patches': hydrophobic_patches,
            'potential_range': {
                'min': round(float(np.min(surface_potential)), 3),
                'max': round(float(np.max(surface_potential)), 3),
                'mean': round(float(np.mean(surface_potential)), 3),
            },
        }

    def _find_surface_patches(self, residues, ca_coords, dist_matrix,
                              surface_indices, filter_fn, label, color) -> List[Dict]:
        """Cluster surface residues matching a filter into spatial patches."""
        candidates = [i for i in surface_indices if filter_fn(i)]
        if not candidates:
            return []

        # Cluster by spatial adjacency (12 Å cutoff)
        used = set()
        patches = []
        for seed in candidates:
            if seed in used:
                continue
            cluster = [seed]
            used.add(seed)
            queue = [seed]
            while queue:
                current = queue.pop()
                for j in candidates:
                    if j in used:
                        continue
                    if dist_matrix[current, j] < 12.0:
                        cluster.append(j)
                        used.add(j)
                        queue.append(j)
            if len(cluster) < 3:
                continue

            cluster_residues = [residues[i] for i in cluster]
            chem = self._summarize_chemistry(cluster_residues)
            selection = self._build_ngl_selection(cluster_residues)

            patches.append({
                'title': f'{label} ({len(cluster)} res)',
                'residue_count': len(cluster),
                'residue_range': self._format_residue_range(cluster_residues),
                'selection': selection,
                'color': color,
                'chemistry': chem,
                'description': (
                    f'{len(cluster)} exposed {label.lower().replace("patch", "").strip()} residues '
                    f'forming a contiguous surface patch. {chem}.'
                ),
            })

        patches.sort(key=lambda p: -p['residue_count'])
        return patches[:3]

    # ── 3. Structural Domains ────────────────────────────────────

    def _analyze_domains(self, residues, ca_coords, contact_map, dist_matrix) -> List[Dict]:
        """Identify structural domains using contact density analysis.

        Uses a simplified version of the domain-parser algorithm:
        scan for minima in inter-segment contact density to find
        natural domain boundaries.
        """
        n = len(residues)
        if n < 40:
            return [self._characterize_domain(residues, ca_coords, 0, n - 1, 1)]

        # Group by chain
        chain_segments = defaultdict(list)
        for i, r in enumerate(residues):
            chain_segments[r['chain']].append(i)

        domains = []
        domain_idx = 0

        for chain_id, indices in chain_segments.items():
            if len(indices) < 30:
                domain_idx += 1
                chain_res = [residues[i] for i in indices]
                domains.append(self._characterize_domain(
                    chain_res, ca_coords[indices], indices[0], indices[-1], domain_idx))
                continue

            # Sliding window contact density analysis
            window = max(15, len(indices) // 20)
            half_w = window // 2
            contact_density = []

            for pos in range(half_w, len(indices) - half_w):
                left_start = max(0, pos - half_w)
                right_end = min(len(indices), pos + half_w)
                left_indices = indices[left_start:pos]
                right_indices = indices[pos:right_end]

                # Count contacts between left and right segments
                cross_contacts = 0
                for li in left_indices:
                    for ri in right_indices:
                        if contact_map[li, ri]:
                            cross_contacts += 1

                n_pairs = max(len(left_indices) * len(right_indices), 1)
                contact_density.append(cross_contacts / n_pairs)

            # Find minima in contact density (domain boundaries)
            if not contact_density:
                domain_idx += 1
                chain_res = [residues[i] for i in indices]
                domains.append(self._characterize_domain(
                    chain_res, ca_coords[indices], indices[0], indices[-1], domain_idx))
                continue

            cd_arr = np.array(contact_density)
            # Smooth
            kernel = min(7, len(cd_arr) // 3)
            if kernel > 2:
                cd_smooth = np.convolve(cd_arr, np.ones(kernel) / kernel, mode='same')
            else:
                cd_smooth = cd_arr

            # Find local minima below threshold
            threshold = np.percentile(cd_smooth, 25)
            boundaries = [half_w]  # Start of chain
            for pos in range(1, len(cd_smooth) - 1):
                if (cd_smooth[pos] < cd_smooth[pos - 1] and
                        cd_smooth[pos] < cd_smooth[pos + 1] and
                        cd_smooth[pos] < threshold):
                    # Require minimum domain size
                    if pos - boundaries[-1] >= 20:
                        boundaries.append(pos + half_w)
            boundaries.append(len(indices))

            # Build domains from boundaries
            for b in range(len(boundaries) - 1):
                start = boundaries[b]
                end = min(boundaries[b + 1], len(indices))
                if end - start < 15:
                    continue
                domain_idx += 1
                seg_indices = indices[start:end]
                seg_residues = [residues[i] for i in seg_indices]
                seg_coords = ca_coords[seg_indices]
                domains.append(self._characterize_domain(
                    seg_residues, seg_coords, seg_indices[0], seg_indices[-1], domain_idx))

        return domains[:8]

    def _characterize_domain(self, domain_residues, coords, global_start, global_end, idx) -> Dict:
        """Characterize a structural domain."""
        n = len(domain_residues)
        if n == 0:
            return {'title': f'Domain {idx}', 'residue_count': 0}

        # Secondary structure composition
        # Estimate from B-factor patterns and residue spacing
        helix_count = 0
        sheet_count = 0
        coil_count = 0
        for r in domain_residues:
            # This is a rough proxy - real SS assignment needs DSSP
            resname = r['resname']
            cat = self.AA_CATEGORY.get(resname, 'polar')
            bfactor = r.get('bfactor', 30.0)
            # Higher B-factors suggest loops, lower suggest ordered SS
            if bfactor < 25:
                helix_count += 1
            elif bfactor < 40:
                sheet_count += 1
            else:
                coil_count += 1

        total = max(helix_count + sheet_count + coil_count, 1)
        helix_pct = helix_count / total
        sheet_pct = sheet_count / total

        # Radius of gyration
        center = np.mean(coords, axis=0)
        rg = float(np.sqrt(np.mean(np.sum((coords - center) ** 2, axis=1))))

        # Compactness (Rg relative to number of residues)
        expected_rg = 2.0 * (n ** 0.4)  # empirical scaling
        compactness = 'Compact' if rg < expected_rg else 'Extended' if rg > expected_rg * 1.3 else 'Normal'

        # Fold classification
        fold_match = self._classify_fold(n, helix_pct, sheet_pct)
        chemistry = self._summarize_chemistry(domain_residues)
        selection = self._build_ngl_selection(domain_residues)
        res_range = self._format_residue_range(domain_residues)

        ss_desc = (
            'helix-rich' if helix_pct > 0.5 else
            'sheet-rich' if sheet_pct > 0.4 else
            'mixed alpha/beta' if helix_pct > 0.2 and sheet_pct > 0.2 else
            'loop-rich'
        )

        return {
            'title': f'Domain {idx}' + (f' ({fold_match["name"]})' if fold_match else ''),
            'residue_count': n,
            'residue_range': res_range,
            'chain': domain_residues[0]['chain'],
            'selection': selection,
            'radius_of_gyration': round(rg, 1),
            'compactness': compactness,
            'ss_composition': ss_desc,
            'helix_pct': round(helix_pct * 100, 1),
            'sheet_pct': round(sheet_pct * 100, 1),
            'fold_annotation': fold_match['description'] if fold_match else None,
            'chemistry': chemistry,
            'description': (
                f'{n} residues ({res_range}), {ss_desc}, Rg={round(rg, 1)} A ({compactness}). '
                f'{fold_match["description"] if fold_match else "No known fold match"}. '
                f'{chemistry}.'
            ),
        }

    def _classify_fold(self, size, helix_pct, sheet_pct) -> Optional[Dict]:
        """Match domain against known fold patterns."""
        for name, criteria in self.KNOWN_FOLDS.items():
            if 'min_size' in criteria and size < criteria['min_size']:
                continue
            if 'max_size' in criteria and size > criteria['max_size']:
                continue
            if 'min_helix_pct' in criteria and helix_pct < criteria['min_helix_pct']:
                continue
            if 'max_helix_pct' in criteria and helix_pct > criteria.get('max_helix_pct', 1.0):
                continue
            if 'min_sheet_pct' in criteria and sheet_pct < criteria['min_sheet_pct']:
                continue
            if 'max_sheet_pct' in criteria and sheet_pct > criteria.get('max_sheet_pct', 1.0):
                continue
            return {'name': name, 'description': criteria['description']}
        return None

    # ── 4. Flexibility and Dynamics ──────────────────────────────

    def _analyze_flexibility(self, residues, ca_coords, contact_map, n) -> Dict:
        """GNM-based flexibility analysis with hinge detection."""
        # Build Kirchhoff (Laplacian) matrix for Gaussian Network Model
        kirchhoff = np.zeros((n, n), dtype=np.float64)
        for i in range(n):
            for j in range(i + 1, n):
                if contact_map[i, j]:
                    kirchhoff[i, j] = -1.0
                    kirchhoff[j, i] = -1.0
                    kirchhoff[i, i] += 1.0
                    kirchhoff[j, j] += 1.0

        # Eigendecomposition (skip first trivial mode with eigenvalue 0)
        try:
            eigenvalues, eigenvectors = np.linalg.eigh(kirchhoff)
        except np.linalg.LinAlgError:
            return {'error': 'Eigendecomposition failed', 'fluctuations': [],
                    'hinges': [], 'mobile_regions': [], 'rigid_regions': []}

        # Per-residue mean-square fluctuations from slowest modes
        n_modes = min(10, n - 1)
        fluctuations = np.zeros(n)
        for k in range(1, n_modes + 1):
            if eigenvalues[k] < 1e-6:
                continue
            fluctuations += eigenvectors[:, k] ** 2 / eigenvalues[k]

        # Normalize
        if np.max(fluctuations) > 0:
            fluctuations = fluctuations / np.max(fluctuations)

        # Identify hinge residues (sign changes in slowest non-trivial mode)
        slowest_mode = eigenvectors[:, 1] if n > 1 else np.zeros(n)
        hinges = []
        for i in range(1, n):
            if residues[i]['chain'] != residues[i - 1]['chain']:
                continue
            if slowest_mode[i] * slowest_mode[i - 1] < 0:
                hinges.append({
                    'residue_index': i,
                    'chain': residues[i]['chain'],
                    'resnum': residues[i]['resnum'],
                    'resname': residues[i]['resname'],
                })

        # Group hinges that are close together (< 5 residues apart)
        hinge_regions = []
        if hinges:
            current_group = [hinges[0]]
            for h in hinges[1:]:
                if (h['residue_index'] - current_group[-1]['residue_index'] < 5 and
                        h['chain'] == current_group[-1]['chain']):
                    current_group.append(h)
                else:
                    hinge_regions.append(current_group)
                    current_group = [h]
            hinge_regions.append(current_group)

        hinge_results = []
        for group in hinge_regions[:6]:
            mid = group[len(group) // 2]
            sel_residues = [residues[h['residue_index']] for h in group]
            hinge_results.append({
                'title': f'Hinge at {mid["chain"]}:{mid["resnum"]}',
                'chain': mid['chain'],
                'resnum': mid['resnum'],
                'selection': self._build_ngl_selection(sel_residues),
                'description': (
                    f'Hinge region near {mid["resname"]} {mid["resnum"]} (chain {mid["chain"]}). '
                    f'Sign change in slowest GNM mode indicates a pivot point for domain motion.'
                ),
            })

        # Mobile regions (high fluctuation segments)
        fluct_thresh = np.percentile(fluctuations, 80)
        mobile_regions = self._find_contiguous_regions(
            residues, fluctuations, lambda f: f >= fluct_thresh, 'Mobile region', '#ef4444')

        # Rigid regions (low fluctuation segments)
        rigid_thresh = np.percentile(fluctuations, 20)
        rigid_regions = self._find_contiguous_regions(
            residues, fluctuations, lambda f: f <= rigid_thresh, 'Rigid core', '#3b82f6')

        # Disordered region prediction (high fluctuation + charged/polar)
        disorder_candidates = []
        for region in mobile_regions:
            region_residues = region.get('_residues', [])
            charged_frac = sum(1 for r in region_residues
                               if self.AA_CATEGORY.get(r['resname']) in ('positive', 'negative', 'polar')
                               ) / max(len(region_residues), 1)
            if charged_frac > 0.6:
                region['likely_disordered'] = True
                disorder_candidates.append(region)

        return {
            'fluctuations': [
                {'chain': residues[i]['chain'], 'resnum': residues[i]['resnum'],
                 'value': round(float(fluctuations[i]), 4)}
                for i in range(n)
            ],
            'hinges': hinge_results,
            'mobile_regions': [{k: v for k, v in r.items() if k != '_residues'}
                               for r in mobile_regions],
            'rigid_regions': [{k: v for k, v in r.items() if k != '_residues'}
                              for r in rigid_regions],
            'disorder_candidates': [{k: v for k, v in r.items() if k != '_residues'}
                                    for r in disorder_candidates],
            'n_modes_used': n_modes,
            'slowest_eigenvalue': round(float(eigenvalues[1]), 6) if n > 1 else 0,
        }

    def _find_contiguous_regions(self, residues, scores, condition, label, color) -> List[Dict]:
        """Find contiguous segments where score meets condition."""
        regions = []
        current = []
        for i, r in enumerate(residues):
            if condition(scores[i]):
                current.append(i)
            else:
                if len(current) >= 4:
                    seg_residues = [residues[j] for j in current]
                    avg_score = float(np.mean(scores[current]))
                    regions.append({
                        'title': f'{label} ({self._format_residue_range(seg_residues)})',
                        'residue_count': len(current),
                        'residue_range': self._format_residue_range(seg_residues),
                        'selection': self._build_ngl_selection(seg_residues),
                        'color': color,
                        'avg_score': round(avg_score, 3),
                        'description': f'{len(current)} residues with {"high" if "Mobile" in label else "low"} predicted fluctuation.',
                        '_residues': seg_residues,
                    })
                current = []
        if len(current) >= 4:
            seg_residues = [residues[j] for j in current]
            avg_score = float(np.mean(scores[current]))
            regions.append({
                'title': f'{label} ({self._format_residue_range(seg_residues)})',
                'residue_count': len(current),
                'residue_range': self._format_residue_range(seg_residues),
                'selection': self._build_ngl_selection(seg_residues),
                'color': color,
                'avg_score': round(avg_score, 3),
                'description': f'{len(current)} residues with {"high" if "Mobile" in label else "low"} predicted fluctuation.',
                '_residues': seg_residues,
            })
        regions.sort(key=lambda r: -r['residue_count'])
        return regions[:5]

    # ── 5. Quaternary Structure ──────────────────────────────────

    def _analyze_quaternary(self, model, residues, ca_coords, sasa_values) -> Dict:
        """Analyze chain-chain interfaces and oligomeric state."""
        chains = defaultdict(list)
        for i, r in enumerate(residues):
            chains[r['chain']].append(i)

        chain_ids = sorted(chains.keys())
        n_chains = len(chain_ids)

        if n_chains < 2:
            return {
                'oligomeric_state': 'Monomer',
                'num_chains': 1,
                'interfaces': [],
                'description': 'Single chain — no quaternary interactions detected.',
            }

        # Oligomeric state name
        state_names = {
            1: 'Monomer', 2: 'Dimer', 3: 'Trimer', 4: 'Tetramer',
            5: 'Pentamer', 6: 'Hexamer', 8: 'Octamer', 12: 'Dodecamer',
        }
        oligo_state = state_names.get(n_chains, f'{n_chains}-mer')

        # Check for homo-oligomer (similar chain lengths)
        chain_lengths = {c: len(idx) for c, idx in chains.items()}
        lengths = list(chain_lengths.values())
        is_homo = max(lengths) - min(lengths) < max(10, min(lengths) * 0.2)

        # Analyze each chain pair interface
        interface_cutoff = 8.0  # Å for interface contacts
        interfaces = []

        for ci in range(n_chains):
            for cj in range(ci + 1, n_chains):
                chain_a = chain_ids[ci]
                chain_b = chain_ids[cj]
                indices_a = chains[chain_a]
                indices_b = chains[chain_b]

                # Find interface residues
                interface_a = set()
                interface_b = set()
                contact_count = 0

                for ia in indices_a:
                    for ib in indices_b:
                        dx = ca_coords[ia] - ca_coords[ib]
                        dist = float(np.sqrt(np.sum(dx ** 2)))
                        if dist < interface_cutoff:
                            interface_a.add(ia)
                            interface_b.add(ib)
                            contact_count += 1

                if not interface_a:
                    continue

                # Interface area estimate (sum of SASA of interface residues
                # that would be buried upon complex formation)
                sasa_arr = np.array(sasa_values)
                buried_sasa = float(np.sum(sasa_arr[list(interface_a)]) +
                                    np.sum(sasa_arr[list(interface_b)]))

                # Characterize interface
                iface_residues_a = [residues[i] for i in interface_a]
                iface_residues_b = [residues[i] for i in interface_b]
                all_iface = iface_residues_a + iface_residues_b

                chem = self._summarize_chemistry(all_iface)
                hydrophobic_frac = sum(
                    1 for r in all_iface
                    if self.AA_CATEGORY.get(r['resname']) == 'hydrophobic'
                ) / max(len(all_iface), 1)

                # Interface type
                if hydrophobic_frac > 0.4:
                    iface_type = 'Hydrophobic core'
                elif hydrophobic_frac > 0.25:
                    iface_type = 'Mixed hydrophobic/polar'
                else:
                    iface_type = 'Polar/charged'

                # Salt bridges at interface
                salt_bridges = 0
                pos_a = [i for i in interface_a if self.AA_CATEGORY.get(residues[i]['resname']) == 'positive']
                neg_b = [i for i in interface_b if self.AA_CATEGORY.get(residues[i]['resname']) == 'negative']
                neg_a = [i for i in interface_a if self.AA_CATEGORY.get(residues[i]['resname']) == 'negative']
                pos_b = [i for i in interface_b if self.AA_CATEGORY.get(residues[i]['resname']) == 'positive']

                for pa in pos_a:
                    for nb in neg_b:
                        if np.sqrt(np.sum((ca_coords[pa] - ca_coords[nb]) ** 2)) < 6.0:
                            salt_bridges += 1
                for na in neg_a:
                    for pb in pos_b:
                        if np.sqrt(np.sum((ca_coords[na] - ca_coords[pb]) ** 2)) < 6.0:
                            salt_bridges += 1

                selection_a = self._build_ngl_selection(iface_residues_a)
                selection_b = self._build_ngl_selection(iface_residues_b)

                interfaces.append({
                    'chains': f'{chain_a}-{chain_b}',
                    'contact_count': contact_count,
                    'interface_residues_a': len(interface_a),
                    'interface_residues_b': len(interface_b),
                    'total_interface_residues': len(interface_a) + len(interface_b),
                    'buried_sasa_approx': round(buried_sasa, 1),
                    'interface_type': iface_type,
                    'hydrophobic_fraction': round(hydrophobic_frac, 2),
                    'salt_bridges': salt_bridges,
                    'selection_a': selection_a,
                    'selection_b': selection_b,
                    'selection': f'({selection_a}) or ({selection_b})' if selection_a and selection_b else '',
                    'chemistry': chem,
                    'description': (
                        f'Interface {chain_a}/{chain_b}: '
                        f'{len(interface_a) + len(interface_b)} residues, '
                        f'~{round(buried_sasa)} A^2 buried surface, '
                        f'{iface_type.lower()}'
                        f'{f", {salt_bridges} salt bridge(s)" if salt_bridges else ""}. '
                        f'{chem}.'
                    ),
                })

        interfaces.sort(key=lambda x: -x['total_interface_residues'])

        # Symmetry detection (simple: check if interfaces are similar)
        symmetry = 'Unknown'
        if n_chains == 2 and len(interfaces) == 1:
            iface = interfaces[0]
            ratio = max(iface['interface_residues_a'], iface['interface_residues_b']) / \
                    max(min(iface['interface_residues_a'], iface['interface_residues_b']), 1)
            symmetry = 'C2 (symmetric dimer)' if is_homo and ratio < 1.5 else 'Asymmetric dimer'
        elif n_chains > 2 and is_homo:
            symmetry = f'C{n_chains} (possible cyclic symmetry)'

        return {
            'oligomeric_state': oligo_state,
            'num_chains': n_chains,
            'chain_lengths': chain_lengths,
            'is_homo_oligomer': is_homo,
            'symmetry': symmetry,
            'interfaces': interfaces,
            'description': (
                f'{oligo_state} '
                f'({"homo" if is_homo else "hetero"}-oligomer, {n_chains} chains). '
                f'{len(interfaces)} interface(s) detected. '
                f'Symmetry: {symmetry}.'
            ),
        }

    # ── Helper methods ───────────────────────────────────────────

    def _summarize_chemistry(self, residue_list) -> str:
        """Summarize the chemical character of a set of residues."""
        counts = {'hydrophobic': 0, 'polar': 0, 'positive': 0, 'negative': 0}
        for r in residue_list:
            cat = self.AA_CATEGORY.get(r['resname'], 'polar')
            if cat in counts:
                counts[cat] += 1
            elif cat in ('positive', 'negative'):
                counts[cat] += 1
        ordered = sorted(counts.items(), key=lambda x: -x[1])
        parts = [f'{c} {n}' for n, c in ordered if c > 0]
        return ', '.join(parts[:3]) if parts else 'mixed'

    @staticmethod
    def _build_ngl_selection(residue_list) -> str:
        """Build an NGL selection string from residue list."""
        if not residue_list:
            return ''
        grouped = defaultdict(list)
        for r in residue_list:
            grouped[r['chain']].append(r['resnum'])

        parts = []
        for chain, nums in grouped.items():
            unique_nums = sorted(set(nums))
            # Compress into ranges
            ranges = []
            start = prev = unique_nums[0]
            for num in unique_nums[1:]:
                if num == prev + 1:
                    prev = num
                else:
                    ranges.append((start, prev))
                    start = prev = num
            ranges.append((start, prev))

            for s, e in ranges:
                r = str(s) if s == e else f'{s}-{e}'
                parts.append(f'{r}:{chain}' if chain.strip() else r)

        return ' or '.join(parts)

    @staticmethod
    def _format_residue_range(residue_list) -> str:
        """Format a residue list as a compact range string."""
        if not residue_list:
            return ''
        chains = defaultdict(list)
        for r in residue_list:
            chains[r['chain']].append(r['resnum'])
        parts = []
        for chain, nums in sorted(chains.items()):
            nums = sorted(set(nums))
            parts.append(f'{chain}:{nums[0]}-{nums[-1]}')
        return ', '.join(parts)


class NormalModeAnalyzer:
    """Normal Mode Analysis using simplified elastic network model."""

    @staticmethod
    def calculate_anm(pdb_path: str, n_modes: int = 10) -> Dict:
        """Calculate Anisotropic Network Model (ANM) normal modes."""
        try:
            return {
                'success': True,
                'message': 'ANM calculation requires ProDy integration',
                'modes': [],
                'frequencies': []
            }
        except Exception as e:
            logger.error(f"Error calculating ANM: {e}")
            return {
                'success': False,
                'error': str(e)
            }


class MDPrep:
    """Molecular Dynamics preparation tools."""

    @staticmethod
    def prepare_structure(pdb_path: str, pH: float = 7.0,
                         add_hydrogens: bool = True,
                         add_solvent: bool = True) -> Dict:
        """
        Prepare structure for MD simulation.

        Uses PDBFixer to clean structure.

        Args:
            pdb_path: Path to input PDB
            pH: Target pH for protonation
            add_hydrogens: Whether to add hydrogens
            add_solvent: Whether to add water box

        Returns:
            Dict with prepared structure
        """
        try:
            # Placeholder for PDBFixer integration
            return {
                'success': True,
                'message': 'MD prep requires PDBFixer/OpenMM integration',
                'prepared_pdb': None
            }
        except Exception as e:
            logger.error(f"Error preparing structure: {e}")
            return {
                'success': False,
                'error': str(e)
            }
