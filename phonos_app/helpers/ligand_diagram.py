"""Ligand interaction diagram (LID) payload - the data behind a Maestro-style
2D protein-ligand interaction map.

The interaction typing itself lives in ``interaction_analysis.py``; this module
adds everything else a 2D diagram needs and that the client cannot compute on
its own:

    * a 2D depiction of the ligand (RDKit ``Compute2DCoords``): atom positions,
      bonds, bond orders, formal charges - so the diagram draws the real
      chemical structure instead of a featureless blob;
    * per-ligand-atom solvent exposure (Shrake-Rupley numerical SASA, ligand
      atoms only, computed both inside the complex and for the free ligand), so
      the diagram can shade solvent-exposed parts of the ligand the way Maestro
      does;
    * a residue-level roll-up with Maestro-style residue categories, which is
      what the diagram actually places around the ligand;
    * ligand physicochemical descriptors for the analytics panel.

Atom indices in this payload refer to the ligand molecule *as parsed*, with
explicit hydrogens preserved (``removeHs=False``, matching
``interaction_analysis._parse_ligand``). This keeps ``ligand_atom_index`` on
every interaction directly usable as an index into ``ligand.atoms``. Hydrogens
are flagged ``is_hydrogen`` so the renderer can hide them without disturbing
that mapping.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Maestro groups binding-site residues into a handful of chemically meaningful
# classes and colours the diagram by them. Anything unlisted falls back to
# 'other' (non-standard residues, modified amino acids, cofactors).
RESIDUE_CATEGORIES = {
    'ARG': 'charged_positive', 'LYS': 'charged_positive', 'HIS': 'charged_positive',
    'ASP': 'charged_negative', 'GLU': 'charged_negative',
    'SER': 'polar', 'THR': 'polar', 'ASN': 'polar', 'GLN': 'polar',
    'CYS': 'polar', 'TYR': 'polar', 'TRP': 'polar',
    'ALA': 'hydrophobic', 'VAL': 'hydrophobic', 'LEU': 'hydrophobic',
    'ILE': 'hydrophobic', 'PHE': 'hydrophobic', 'MET': 'hydrophobic',
    'PRO': 'hydrophobic',
    'GLY': 'glycine',
}

WATER_RESNAMES = {'HOH', 'WAT', 'H2O', 'DOD'}

# Shrake-Rupley parameters. 92 points is the classic Shrake-Rupley sampling
# density and is plenty for a per-atom exposure fraction driving a 2D shading.
SASA_PROBE_RADIUS = 1.4
SASA_SAMPLE_POINTS = 92

# Fallback van der Waals radii (Angstrom) for elements RDKit's periodic table
# may not resolve from a bare HETATM-derived SDF.
_FALLBACK_VDW = {
    'H': 1.20, 'C': 1.70, 'N': 1.55, 'O': 1.52, 'F': 1.47, 'P': 1.80,
    'S': 1.80, 'CL': 1.75, 'BR': 1.85, 'I': 1.98, 'SE': 1.90, 'B': 1.92,
}
_DEFAULT_VDW = 1.70


def _sphere_points(n: int):
    """`n` roughly-uniform points on the unit sphere (Fibonacci lattice)."""
    points = []
    offset = 2.0 / n
    increment = math.pi * (3.0 - math.sqrt(5.0))  # golden angle
    for i in range(n):
        y = i * offset - 1.0 + offset / 2.0
        r = math.sqrt(max(0.0, 1.0 - y * y))
        phi = i * increment
        points.append((math.cos(phi) * r, y, math.sin(phi) * r))
    return points


def _vdw_radius(element: str, periodic_table=None, atomic_num: int = 0) -> float:
    if periodic_table is not None and atomic_num:
        try:
            radius = periodic_table.GetRvdw(atomic_num)
            if radius and radius > 0:
                return float(radius)
        except Exception:
            pass
    return _FALLBACK_VDW.get((element or '').upper(), _DEFAULT_VDW)


def _compute_solvent_exposure(lig_coords, lig_radii, protein_coords, protein_radii):
    """Per-ligand-atom SASA, free and inside the complex.

    Numerical Shrake-Rupley: sample the solvent-accessible sphere of each ligand
    atom and count the fraction of sample points not buried inside any other
    atom's solvent-accessible sphere. Returns a list of dicts with the free and
    complexed accessible fractions and the resulting buried fraction.

    Implemented with numpy (already a hard dependency) rather than Biopython's
    ShrakeRupley, because the ligand here is an RDKit molecule that is not part
    of the parsed Biopython structure.
    """
    import numpy as np

    lig_xyz = np.asarray(lig_coords, dtype=float)
    lig_r = np.asarray(lig_radii, dtype=float) + SASA_PROBE_RADIUS
    n_lig = len(lig_xyz)
    if n_lig == 0:
        return []

    if len(protein_coords):
        prot_xyz = np.asarray(protein_coords, dtype=float)
        prot_r = np.asarray(protein_radii, dtype=float) + SASA_PROBE_RADIUS
    else:
        prot_xyz = np.zeros((0, 3))
        prot_r = np.zeros(0)

    unit_sphere = np.asarray(_sphere_points(SASA_SAMPLE_POINTS), dtype=float)
    results = []

    for i in range(n_lig):
        # Sample points on this atom's solvent-accessible surface.
        samples = lig_xyz[i] + unit_sphere * lig_r[i]

        # Occlusion by the rest of the ligand (this is the "free ligand" surface).
        free_mask = np.ones(len(samples), dtype=bool)
        if n_lig > 1:
            others = np.delete(np.arange(n_lig), i)
            d2 = ((samples[:, None, :] - lig_xyz[None, others, :]) ** 2).sum(axis=2)
            free_mask &= ~(d2 < (lig_r[others] ** 2)[None, :]).any(axis=1)

        # Additional occlusion by the protein (the "complexed" surface).
        complex_mask = free_mask.copy()
        if len(prot_xyz):
            # Only protein atoms that can possibly reach this sample shell matter.
            reach = lig_r[i] + prot_r.max() if len(prot_r) else 0.0
            near = np.where(((prot_xyz - lig_xyz[i]) ** 2).sum(axis=1) < reach ** 2)[0]
            if len(near):
                d2 = ((samples[:, None, :] - prot_xyz[None, near, :]) ** 2).sum(axis=2)
                complex_mask &= ~(d2 < (prot_r[near] ** 2)[None, :]).any(axis=1)

        n_points = float(len(samples))
        free_frac = float(free_mask.sum()) / n_points
        complex_frac = float(complex_mask.sum()) / n_points
        # Buried = how much of the atom's *available* surface the protein covers.
        buried = 0.0 if free_frac <= 0 else 1.0 - (complex_frac / free_frac)
        sphere_area = 4.0 * math.pi * (lig_r[i] ** 2)
        results.append({
            'free_fraction': round(free_frac, 3),
            'complex_fraction': round(complex_frac, 3),
            'buried_fraction': round(max(0.0, min(1.0, buried)), 3),
            'sasa': round(complex_frac * sphere_area, 2),
        })
    return results


def _build_2d_depiction(mol) -> dict:
    """2D coordinates, bonds and per-atom drawing metadata for the ligand."""
    from rdkit import Chem
    from rdkit.Chem import AllChem, rdDepictor

    # Depict on a copy: computing 2D coordinates overwrites the conformer, and
    # the 3D conformer is still needed for the SASA pass.
    flat = Chem.Mol(mol)
    flat.RemoveAllConformers()
    try:
        rdDepictor.SetPreferCoordGen(True)
    except Exception:
        pass
    try:
        AllChem.Compute2DCoords(flat)
    except Exception as exc:
        logger.warning('2D coordinate generation failed: %s', exc, exc_info=True)
        return {'atoms': [], 'bonds': [], 'bounds': None}

    conf = flat.GetConformer()
    periodic_table = Chem.GetPeriodicTable()

    atoms = []
    xs, ys = [], []
    for atom in flat.GetAtoms():
        idx = atom.GetIdx()
        pos = conf.GetAtomPosition(idx)
        element = atom.GetSymbol()
        is_h = atom.GetAtomicNum() == 1
        if not is_h:
            xs.append(pos.x)
            ys.append(pos.y)
        atoms.append({
            'idx': idx,
            'element': element,
            'x': round(pos.x, 4),
            'y': round(pos.y, 4),
            'charge': atom.GetFormalCharge(),
            'aromatic': bool(atom.GetIsAromatic()),
            'in_ring': bool(atom.IsInRing()),
            'is_hydrogen': is_h,
            # Implicit+explicit H count, so the renderer can label "NH", "OH",
            # "NH2" the way a 2D sketch does.
            'h_count': atom.GetTotalNumHs(includeNeighbors=True),
            'vdw': round(_vdw_radius(element, periodic_table, atom.GetAtomicNum()), 3),
        })

    bonds = []
    for bond in flat.GetBonds():
        bonds.append({
            'begin': bond.GetBeginAtomIdx(),
            'end': bond.GetEndAtomIdx(),
            'order': int(round(bond.GetBondTypeAsDouble())),
            'aromatic': bool(bond.GetIsAromatic()),
        })

    rings = [list(r) for r in flat.GetRingInfo().AtomRings()]

    bounds = None
    if xs and ys:
        bounds = {
            'min_x': round(min(xs), 4), 'max_x': round(max(xs), 4),
            'min_y': round(min(ys), 4), 'max_y': round(max(ys), 4),
        }

    return {'atoms': atoms, 'bonds': bonds, 'rings': rings, 'bounds': bounds}


def _ligand_properties(mol) -> dict:
    """Physicochemical descriptors for the analytics panel."""
    from rdkit import Chem
    from rdkit.Chem import Descriptors, Crippen, rdMolDescriptors

    props = {}
    try:
        heavy = Chem.RemoveHs(Chem.Mol(mol))
    except Exception:
        heavy = mol

    def safe(name, fn, digits=2):
        try:
            value = fn()
            props[name] = round(float(value), digits) if isinstance(value, float) else value
        except Exception:
            props[name] = None

    safe('molecular_weight', lambda: Descriptors.MolWt(heavy))
    safe('heavy_atoms', lambda: heavy.GetNumHeavyAtoms())
    safe('clogp', lambda: Crippen.MolLogP(heavy))
    safe('tpsa', lambda: rdMolDescriptors.CalcTPSA(heavy))
    safe('hbd', lambda: rdMolDescriptors.CalcNumHBD(heavy))
    safe('hba', lambda: rdMolDescriptors.CalcNumHBA(heavy))
    safe('rotatable_bonds', lambda: rdMolDescriptors.CalcNumRotatableBonds(heavy))
    safe('rings', lambda: rdMolDescriptors.CalcNumRings(heavy))
    safe('aromatic_rings', lambda: rdMolDescriptors.CalcNumAromaticRings(heavy))
    safe('fraction_csp3', lambda: rdMolDescriptors.CalcFractionCSP3(heavy), 3)
    safe('formal_charge', lambda: Chem.GetFormalCharge(heavy))
    safe('formula', lambda: rdMolDescriptors.CalcMolFormula(heavy))
    safe('smiles', lambda: Chem.MolToSmiles(heavy))

    try:
        from rdkit.Chem import QED
        props['qed'] = round(float(QED.qed(heavy)), 3)
    except Exception:
        props['qed'] = None

    # Lipinski rule-of-five and Veber, reported as explicit failures rather than
    # a bare pass/fail so the panel can say which criterion is violated.
    violations = []
    if props.get('molecular_weight') is not None and props['molecular_weight'] > 500:
        violations.append('MW > 500')
    if props.get('clogp') is not None and props['clogp'] > 5:
        violations.append('cLogP > 5')
    if props.get('hbd') is not None and props['hbd'] > 5:
        violations.append('HBD > 5')
    if props.get('hba') is not None and props['hba'] > 10:
        violations.append('HBA > 10')
    props['lipinski_violations'] = violations
    props['lipinski_pass'] = len(violations) <= 1

    veber = []
    if props.get('rotatable_bonds') is not None and props['rotatable_bonds'] > 10:
        veber.append('Rotatable bonds > 10')
    if props.get('tpsa') is not None and props['tpsa'] > 140:
        veber.append('TPSA > 140')
    props['veber_violations'] = veber
    props['veber_pass'] = not veber

    return props


# Geometric aromaticity perception. An aromatic ring is flat and its bonds sit
# between a single and a double bond in length; both are unambiguous in a
# well-resolved pose and neither depends on knowing bond orders.
AROMATIC_RING_SIZES = (5, 6)
AROMATIC_MAX_PLANE_DEVIATION = 0.15   # Angstrom, atom to best-fit ring plane
AROMATIC_MIN_BOND_LENGTH = 1.28
AROMATIC_MAX_BOND_LENGTH = 1.48


def _ring_is_planar_aromatic(mol, ring) -> bool:
    """True when a ring's geometry is that of an aromatic ring."""
    import numpy as np

    conf = mol.GetConformer()
    coords = np.array([[conf.GetAtomPosition(i).x,
                        conf.GetAtomPosition(i).y,
                        conf.GetAtomPosition(i).z] for i in ring])

    # Only C/N/O/S rings are candidates; a ring containing anything else is not
    # an aromatic system we should be guessing at.
    for idx in ring:
        if mol.GetAtomWithIdx(idx).GetSymbol().upper() not in ('C', 'N', 'O', 'S'):
            return False

    # Best-fit plane through the ring: the smallest singular vector is its normal.
    centred = coords - coords.mean(axis=0)
    try:
        normal = np.linalg.svd(centred)[2][2]
    except np.linalg.LinAlgError:
        return False
    if np.abs(centred @ normal).max() > AROMATIC_MAX_PLANE_DEVIATION:
        return False

    # Ring bond lengths must all lie in the aromatic range.
    for position, idx in enumerate(ring):
        neighbour = ring[(position + 1) % len(ring)]
        length = float(np.linalg.norm(coords[position] - coords[(position + 1) % len(ring)]))
        if not (AROMATIC_MIN_BOND_LENGTH <= length <= AROMATIC_MAX_BOND_LENGTH):
            return False
        if mol.GetBondBetweenAtoms(idx, neighbour) is None:
            return False
    return True


def perceive_aromatic_rings(sdf_text: str) -> tuple:
    """Flag geometrically aromatic rings on a connectivity-only ligand SDF.

    ``ligand_split.extract_ligand_sdf`` rebuilds a ligand from HETATM records,
    which carry coordinates but no bonds, so every bond comes back single and
    nothing is aromatic. That silently disables pi-stacking and cation-pi
    detection for any ligand taken straight out of a complex.

    Full bond-order inference (``rdDetermineBonds``) is deliberately NOT used
    here: it assumes explicit hydrogens, which HETATM ligands do not have, and
    without them it compensates by inventing formal charges - producing a
    molecule that is wrong in ways that also break donor/acceptor typing.
    Perceiving *only* aromaticity, from ring planarity and bond lengths, adds
    the missing signal without asserting anything about protonation.

    Aromatic bonds are written back as mol-block bond type 4, which RDKit reads
    back as aromatic, so the flag survives the round-trip to the analysis pass.

    Returns ``(sdf_text, ring_count)``.
    """
    try:
        from rdkit import Chem
    except ImportError:
        return sdf_text, 0

    try:
        mol = Chem.MolFromMolBlock(sdf_text, sanitize=False, removeHs=False)
        if mol is None or mol.GetNumConformers() == 0:
            return sdf_text, 0
        Chem.GetSSSR(mol)
    except Exception:
        return sdf_text, 0

    try:
        aromatic_rings = 0
        for ring in mol.GetRingInfo().AtomRings():
            if len(ring) not in AROMATIC_RING_SIZES:
                continue
            if not _ring_is_planar_aromatic(mol, ring):
                continue
            aromatic_rings += 1
            for position, idx in enumerate(ring):
                mol.GetAtomWithIdx(idx).SetIsAromatic(True)
                bond = mol.GetBondBetweenAtoms(idx, ring[(position + 1) % len(ring)])
                if bond is not None:
                    bond.SetIsAromatic(True)
                    bond.SetBondType(Chem.BondType.AROMATIC)

        if not aromatic_rings:
            return sdf_text, 0

        # kekulize=False keeps the aromatic bonds as type 4 in the output block.
        block = Chem.MolToMolBlock(mol, kekulize=False)
        if not block or not block.strip():
            return sdf_text, 0
        return block + '$$$$\n', aromatic_rings
    except Exception as exc:
        logger.warning('Aromaticity perception failed: %s', exc, exc_info=True)
        return sdf_text, 0


def _residue_key(entry: dict) -> str:
    return f"{entry.get('chain', '')}:{entry.get('resnum', '')}:{entry.get('resname', '')}"


def _aggregate_residues(interactions: list) -> list:
    """Roll interactions up to one entry per binding-site residue.

    The diagram places one disc per residue, not one per interaction, so each
    residue carries the set of interaction types it makes, its closest approach,
    and the ligand atoms it touches (used to position the disc near the right
    part of the ligand).
    """
    residues = {}
    for item in interactions:
        protein = item.get('protein') or {}
        resname = (protein.get('resname') or '').upper()
        key = _residue_key(protein)
        entry = residues.get(key)
        if entry is None:
            if resname in WATER_RESNAMES:
                category = 'water'
            elif item.get('type') == 'metal_coordination':
                category = 'metal'
            else:
                category = RESIDUE_CATEGORIES.get(resname, 'other')
            entry = residues[key] = {
                'key': key,
                'chain': protein.get('chain'),
                'resname': resname,
                'resnum': protein.get('resnum'),
                'category': category,
                'types': [],
                'interaction_count': 0,
                'min_distance': None,
                'ligand_atoms': [],
            }

        entry['interaction_count'] += 1
        if item['type'] not in entry['types']:
            entry['types'].append(item['type'])

        distance = item.get('distance')
        if distance is not None and (entry['min_distance'] is None or distance < entry['min_distance']):
            entry['min_distance'] = distance

        atom_ids = item.get('ligand_atom_indices')
        if not atom_ids:
            single = item.get('ligand_atom_index')
            atom_ids = [single] if single is not None else []
        for atom_id in atom_ids:
            if atom_id not in entry['ligand_atoms']:
                entry['ligand_atoms'].append(atom_id)

    ordered = sorted(
        residues.values(),
        key=lambda r: (r['min_distance'] if r['min_distance'] is not None else 999.0),
    )
    return ordered


def build_ligand_diagram(protein_text: str, protein_format: str,
                         ligand_text: str, ligand_format: str,
                         distance_cutoff: float = 5.0,
                         max_residues: int = 40) -> dict:
    """Full ligand-interaction-diagram payload for a protein-ligand complex.

    ``protein_text`` may be the full complex (the ligand's own HETATM records
    are ignored by the polymer parse); ``ligand_text`` must carry real bond
    orders, since donor/acceptor/aromatic/charge typing depends on them.
    """
    from .interaction_analysis import (
        analyze_interactions, _parse_ligand, _ligand_atoms, _parse_protein_atoms,
    )
    from rdkit import Chem

    result = analyze_interactions(
        protein_text=protein_text, protein_format=protein_format,
        ligand_text=ligand_text, ligand_format=ligand_format,
        distance_cutoff=distance_cutoff,
    )

    mol, _features = _parse_ligand(ligand_text, ligand_format)
    lig_atoms = _ligand_atoms(mol)

    depiction = _build_2d_depiction(mol)
    properties = _ligand_properties(mol)

    # Solvent exposure needs the protein heavy atoms as occluders.
    exposure = []
    try:
        protein_atoms = _parse_protein_atoms(protein_text, protein_format)
        exposure = _compute_solvent_exposure(
            lig_coords=[a['coord'] for a in lig_atoms],
            lig_radii=[a.get('vdw') or _vdw_radius(a['element']) for a in lig_atoms],
            protein_coords=[a['coord'] for a in protein_atoms],
            # Protein atoms are effectively all C/N/O/S, which the fallback table
            # covers exactly; no need to round-trip element -> atomic number.
            protein_radii=[_vdw_radius(a['element']) for a in protein_atoms],
        )
    except Exception as exc:
        logger.warning('Solvent exposure calculation failed: %s', exc, exc_info=True)
        exposure = []

    residues = _aggregate_residues(result['interactions'])
    truncated = len(residues) > max_residues
    if truncated:
        residues = residues[:max_residues]

    ligand_name = 'LIG'
    try:
        if mol.HasProp('_Name') and mol.GetProp('_Name').strip():
            ligand_name = mol.GetProp('_Name').strip()[:16]
    except Exception:
        pass

    return {
        'success': True,
        'ligand': {
            'name': ligand_name,
            'atoms': depiction['atoms'],
            'bonds': depiction['bonds'],
            'rings': depiction.get('rings', []),
            'bounds': depiction['bounds'],
            'solvent_exposure': exposure,
        },
        'properties': properties,
        'interactions': result['interactions'],
        'residues': residues,
        'residues_truncated': truncated,
        'counts': result['counts'],
        'criteria_note': result['criteria_note'],
    }
