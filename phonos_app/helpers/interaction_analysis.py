"""
Protein-ligand interaction typing - real geometry/chemistry, not the old
client-side "any N/O within 3.5A" heuristic (static/js/app.js
computeMolVisLigandContacts, being retired by a sibling frontend change).

Criteria are modeled loosely on PLIP's publicly-documented interaction
definitions (https://plip-tool.biotec.tu-dresden.de/), implemented from
scratch against RDKit + Biopython (both already installed/used elsewhere
in this codebase) rather than adding a PLIP dependency, which is heavy
and not actively maintained.

Explicit hydrogens are NOT placed on the protein (neither X-ray nor
predicted structures in this product carry them), so hydrogen bonds are
scored on heavy-atom donor...acceptor distance plus a directionality
angle at the donor (parent-heavy-atom -> donor -> acceptor) as a proxy
for "the donor's H plausibly points toward the acceptor", rather than a
true donor-H...acceptor angle. This is a documented simplification, not
full PLIP-grade geometry - it is nonetheless a real, meaningful upgrade
over the previous implementation, which did no directionality or
donor/acceptor typing at all.

Interaction types implemented: hydrogen bonds, hydrophobic contacts
(RDKit van der Waals radii, not a fixed cutoff), salt bridges (charged
group detection via RDKit's NegIonizable/PosIonizable feature families
on the ligand side, hardcoded charged side-chain atoms on the protein
side), pi-stacking (aromatic ring centroid/normal geometry), halogen
bonds (sigma-hole directionality on C-X...acceptor), cation-pi (in both
directions), metal coordination (monatomic HETATM ions, reported with
the protein residues completing the shell), and water-mediated bridges
(a single water reaching both partners).

Waters and metal ions are read from the HETATM records in a second
parse pass (_parse_het_atoms); the polymer pass deliberately skips
every HETATM record so solvent never leaks into the protein-side
donor/acceptor tables.
"""
from __future__ import annotations

import io
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Protein-side chemistry tables (standard amino acid donor/acceptor/charged/
# aromatic atoms - PDB atom naming)
# ============================================================================

# Backbone: every residue's carbonyl O is an acceptor; every residue's amide
# N is a donor except proline (secondary amine, no N-H).
BACKBONE_ACCEPTOR_ATOMS = {'O'}
BACKBONE_DONOR_ATOMS = {'N'}
PROLINE_NO_BACKBONE_DONOR = {'PRO'}

# Side-chain donor atoms per residue (atom name -> the parent heavy atom
# it's bonded to, used for the H-bond directionality proxy angle).
SIDECHAIN_DONORS = {
    'SER': {'OG': 'CB'},
    'THR': {'OG1': 'CB'},
    'TYR': {'OH': 'CZ'},
    'ASN': {'ND2': 'CG'},
    'GLN': {'NE2': 'CD'},
    'HIS': {'ND1': 'CG', 'NE2': 'CE1'},
    'LYS': {'NZ': 'CE'},
    'ARG': {'NE': 'CD', 'NH1': 'CZ', 'NH2': 'CZ'},
    'TRP': {'NE1': 'CD1'},
    'CYS': {'SG': 'CB'},
}

# Side-chain acceptor atoms per residue (atom name -> parent heavy atom).
SIDECHAIN_ACCEPTORS = {
    'SER': {'OG': 'CB'},
    'THR': {'OG1': 'CB'},
    'TYR': {'OH': 'CZ'},
    'ASN': {'OD1': 'CG'},
    'GLN': {'OE1': 'CD'},
    'HIS': {'ND1': 'CG', 'NE2': 'CE1'},
    'ASP': {'OD1': 'CG', 'OD2': 'CG'},
    'GLU': {'OE1': 'CD', 'OE2': 'CD'},
}

# Backbone donor's parent heavy atom is the previous residue's carbonyl
# carbon in a real structure; approximate with the residue's own CA, which
# is close enough for a coarse directionality check.
BACKBONE_DONOR_PARENT = 'CA'
BACKBONE_ACCEPTOR_PARENT = 'C'

NEGATIVE_RESIDUE_ATOMS = {
    'ASP': ['OD1', 'OD2'],
    'GLU': ['OE1', 'OE2'],
}
POSITIVE_RESIDUE_ATOMS = {
    'LYS': ['NZ'],
    'ARG': ['NE', 'NH1', 'NH2'],
}

AROMATIC_RING_ATOMS = {
    'PHE': ['CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ'],
    'TYR': ['CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ'],
    'TRP': ['CD2', 'CE2', 'CE3', 'CZ2', 'CZ3', 'CH2'],  # six-membered ring of the indole
    'HIS': ['CG', 'ND1', 'CD2', 'CE1', 'NE2'],
}

# Cation-pi: the protein-side cationic groups whose charge centre can sit over a
# ligand aromatic ring face. Lys ammonium is a point charge (NZ); Arg guanidinium
# is delocalised, so its centroid is used.
CATION_GROUP_ATOMS = {
    'LYS': ['NZ'],
    'ARG': ['NE', 'NH1', 'NH2'],
}

# Residue names treated as structural water (water-mediated bridges) and as
# coordinating metal ions (metal coordination). Both arrive as HETATM records,
# which _parse_protein_atoms drops, so they are collected in a separate pass.
WATER_RESNAMES = {'HOH', 'WAT', 'H2O', 'DOD'}
METAL_ELEMENTS = {
    'ZN', 'MG', 'CA', 'FE', 'MN', 'CU', 'NI', 'CO', 'CD', 'NA', 'K', 'HG', 'MO',
}
# Ligand-side atoms that can donate a lone pair to a metal centre.
METAL_COORDINATING_ELEMENTS = {'N', 'O', 'S'}
# Protein-side side-chain atoms that commonly complete a metal coordination shell.
METAL_PROTEIN_ATOMS = {
    'HIS': ['ND1', 'NE2'], 'CYS': ['SG'], 'ASP': ['OD1', 'OD2'],
    'GLU': ['OE1', 'OE2'], 'MET': ['SD'], 'SER': ['OG'], 'THR': ['OG1'],
    'TYR': ['OH'], 'ASN': ['OD1'], 'GLN': ['OE1'],
}

HBOND_DISTANCE_CUTOFF = 3.5     # heavy-atom donor...acceptor, Angstrom (standard)
HBOND_MIN_DIRECTIONALITY_ANGLE = 90.0  # degrees, parent-donor-acceptor
HYDROPHOBIC_VDW_TOLERANCE = 0.5  # Angstrom added to the sum of vdW radii
SALT_BRIDGE_DISTANCE_CUTOFF = 4.0
PISTACK_CENTROID_DISTANCE_CUTOFF = 6.0
PISTACK_PARALLEL_MAX_ANGLE = 30.0     # face-to-face
PISTACK_PERPENDICULAR_TOLERANCE = 30.0  # around 90 degrees, edge-to-face

# Halogen bonds: the sigma-hole on a polarised C-X bond points at an acceptor
# lone pair, so the C-X...A angle at the halogen must be close to linear. Values
# follow PLIP's published defaults.
HALOGENS = {'CL', 'BR', 'I'}
HALOGEN_BOND_DISTANCE_CUTOFF = 4.0
HALOGEN_BOND_MIN_DONOR_ANGLE = 140.0   # C-X...acceptor, at the halogen

# Cation-pi: cation to aromatic ring centroid, plus an offset angle measuring how
# far the cation sits from the ring axis (0 deg = directly over the ring face).
CATION_PI_DISTANCE_CUTOFF = 6.0
CATION_PI_MAX_OFFSET_ANGLE = 30.0

# Metal coordination: first-shell donor distance to the metal centre.
METAL_COORDINATION_CUTOFF = 2.8

# Water bridges: one water simultaneously within H-bond range of both a ligand
# polar atom and a protein polar atom.
WATER_BRIDGE_MAX_DISTANCE = 3.5
WATER_BRIDGE_MIN_DISTANCE = 2.4


def _vec(a, b):
    return (b[0] - a[0], b[1] - a[1], b[2] - a[2])


def _dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _angle_deg(p1, p2, p3):
    """Angle at p2, formed by p1-p2-p3, in degrees."""
    v1 = _vec(p2, p1)
    v2 = _vec(p2, p3)
    n1 = math.sqrt(sum(c * c for c in v1))
    n2 = math.sqrt(sum(c * c for c in v2))
    if n1 == 0 or n2 == 0:
        return 180.0
    dot = sum(v1[i] * v2[i] for i in range(3))
    cos_a = max(-1.0, min(1.0, dot / (n1 * n2)))
    return math.degrees(math.acos(cos_a))


def _centroid(coords):
    n = len(coords)
    return tuple(sum(c[i] for c in coords) / n for i in range(3))


def _ring_normal(coords):
    """Approximate ring normal via the cross product of two edge vectors
    from the centroid, averaged for stability on non-planar inputs."""
    c = _centroid(coords)
    normals = []
    for i in range(len(coords)):
        a = coords[i]
        b = coords[(i + 1) % len(coords)]
        v1 = _vec(c, a)
        v2 = _vec(c, b)
        cross = (
            v1[1] * v2[2] - v1[2] * v2[1],
            v1[2] * v2[0] - v1[0] * v2[2],
            v1[0] * v2[1] - v1[1] * v2[0],
        )
        norm = math.sqrt(sum(x * x for x in cross))
        if norm > 1e-6:
            normals.append(tuple(x / norm for x in cross))
    if not normals:
        return (0.0, 0.0, 1.0)
    avg = tuple(sum(n[i] for n in normals) / len(normals) for i in range(3))
    norm = math.sqrt(sum(x * x for x in avg))
    return tuple(x / norm for x in avg) if norm > 1e-6 else (0.0, 0.0, 1.0)


def _angle_between_vectors_deg(v1, v2):
    n1 = math.sqrt(sum(c * c for c in v1))
    n2 = math.sqrt(sum(c * c for c in v2))
    if n1 == 0 or n2 == 0:
        return 90.0
    dot = sum(v1[i] * v2[i] for i in range(3))
    cos_a = max(-1.0, min(1.0, dot / (n1 * n2)))
    return math.degrees(math.acos(cos_a))


# ============================================================================
# Protein parsing (Biopython)
# ============================================================================
def _parse_protein_atoms(protein_text: str, fmt: str) -> list:
    """Returns a flat list of dicts: {coord, element, atom_name, resname,
    resnum, chain, residue} for every protein atom, via Biopython."""
    from Bio.PDB import PDBParser, MMCIFParser

    fmt = (fmt or 'pdb').lower()
    handle = io.StringIO(protein_text)
    if fmt in ('cif', 'mmcif'):
        parser = MMCIFParser(QUIET=True)
    else:
        parser = PDBParser(QUIET=True)
    structure = parser.get_structure('protein', handle)

    atoms = []
    for model in structure:
        for chain in model:
            for residue in chain:
                resname = residue.get_resname()
                hetflag = residue.id[0]
                if hetflag != ' ':
                    continue  # skip HETATM/water in the protein-side atom list
                for atom in residue:
                    atoms.append({
                        'coord': tuple(float(x) for x in atom.get_coord()),
                        'element': (atom.element or atom.get_name()[0]).upper(),
                        'atom_name': atom.get_name(),
                        'resname': resname,
                        'resnum': residue.id[1],
                        'chain': chain.id,
                    })
        break  # first model only
    return atoms


def _parse_het_atoms(protein_text: str, fmt: str) -> tuple:
    """Waters and metal ions from the HETATM records, as (waters, metals).

    _parse_protein_atoms deliberately skips every HETATM record so the polymer
    interaction passes never see solvent. Water-mediated bridges and metal
    coordination need exactly those records, so they are collected here in a
    second pass rather than by loosening the polymer filter.
    """
    from Bio.PDB import PDBParser, MMCIFParser

    fmt = (fmt or 'pdb').lower()
    handle = io.StringIO(protein_text)
    parser = MMCIFParser(QUIET=True) if fmt in ('cif', 'mmcif') else PDBParser(QUIET=True)
    structure = parser.get_structure('het', handle)

    waters, metals = [], []
    for model in structure:
        for chain in model:
            for residue in chain:
                if residue.id[0] == ' ':
                    continue  # polymer residue, handled by _parse_protein_atoms
                resname = residue.get_resname().strip().upper()
                for atom in residue:
                    element = (atom.element or atom.get_name()[0]).strip().upper()
                    record = {
                        'coord': tuple(float(x) for x in atom.get_coord()),
                        'element': element,
                        'atom_name': atom.get_name(),
                        'resname': resname,
                        'resnum': residue.id[1],
                        'chain': chain.id,
                    }
                    if resname in WATER_RESNAMES:
                        # Only the oxygen carries the bridging lone pairs.
                        if element == 'O':
                            waters.append(record)
                    elif element in METAL_ELEMENTS and len(residue) == 1:
                        # A monatomic HETATM residue of a metal element is an ion;
                        # the same element inside a larger residue is part of a
                        # cofactor and is not treated as a free coordination centre.
                        metals.append(record)
        break  # first model only
    return waters, metals


# ============================================================================
# Ligand parsing (RDKit)
# ============================================================================
def _parse_ligand(ligand_text: str, fmt: str):
    """Returns (rdkit_mol, feature_list) where feature_list is from RDKit's
    default pharmacophore feature factory (Donor/Acceptor/Aromatic/
    Hydrophobe/NegIonizable/PosIonizable), each with atom indices."""
    from rdkit import Chem, RDConfig
    from rdkit.Chem import ChemicalFeatures
    import os

    fmt = (fmt or 'sdf').lower()
    if fmt in ('mol2',):
        mol = Chem.MolFromMol2Block(ligand_text, sanitize=True, removeHs=False)
    else:
        mol = Chem.MolFromMolBlock(ligand_text, sanitize=True, removeHs=False)
    if mol is None:
        raise ValueError('Could not parse ligand structure with RDKit (invalid SDF/MOL/MOL2).')
    if mol.GetNumConformers() == 0:
        raise ValueError('Ligand structure has no 3D coordinates.')

    fdef_path = os.path.join(RDConfig.RDDataDir, 'BaseFeatures.fdef')
    factory = ChemicalFeatures.BuildFeatureFactory(fdef_path)
    features = factory.GetFeaturesForMol(mol)
    return mol, features


def _ligand_atoms(mol) -> list:
    conf = mol.GetConformer()
    pt = __import__('rdkit').Chem.GetPeriodicTable()
    atoms = []
    for atom in mol.GetAtoms():
        idx = atom.GetIdx()
        pos = conf.GetAtomPosition(idx)
        atoms.append({
            'idx': idx,
            'coord': (pos.x, pos.y, pos.z),
            'element': atom.GetSymbol().upper(),
            'vdw': pt.GetRvdw(atom.GetAtomicNum()),
            # Bonded heavy-atom neighbours, needed for the C-X...acceptor angle
            # at a halogen (sigma-hole directionality).
            'neighbors': [n.GetIdx() for n in atom.GetNeighbors()
                          if n.GetAtomicNum() > 1],
        })
    return atoms


def _ligand_feature_index(features) -> dict:
    """family -> set of atom indices participating in that feature family."""
    idx = {}
    for f in features:
        idx.setdefault(f.GetFamily(), set()).update(f.GetAtomIds())
    return idx


# ============================================================================
# Main entry point
# ============================================================================
def analyze_interactions(protein_text: str, protein_format: str,
                          ligand_text: str, ligand_format: str,
                          distance_cutoff: float = 5.0) -> dict:
    """Detect and type protein-ligand interactions.

    Returns {'success': True, 'interactions': [...], 'counts': {...}}.
    Each interaction: {type, distance, angle (if applicable),
    protein: {chain, resname, resnum, atom}, ligand_atom_index, ligand_element}.
    """
    from Bio.PDB import NeighborSearch
    from Bio.PDB.Atom import Atom as BioAtom

    protein_atoms = _parse_protein_atoms(protein_text, protein_format)
    if not protein_atoms:
        raise ValueError('No protein atoms parsed - check protein_text/protein_format.')

    mol, features = _parse_ligand(ligand_text, ligand_format)
    lig_atoms = _ligand_atoms(mol)
    feat_idx = _ligand_feature_index(features)

    # Build a Biopython NeighborSearch index over protein atoms for fast
    # proximity queries (same tool used elsewhere in structure_analysis.py).
    bio_atoms = []
    for i, a in enumerate(protein_atoms):
        ba = BioAtom(a['atom_name'], a['coord'], 0.0, 1.0, ' ', a['atom_name'], i, element=a['element'])
        bio_atoms.append(ba)
    ns = NeighborSearch(bio_atoms)

    def protein_lookup(bio_atom):
        return protein_atoms[bio_atom.get_serial_number()]

    interactions = []

    for lig_atom in lig_atoms:
        nearby = ns.search(lig_atom['coord'], distance_cutoff, level='A')
        for bio_atom in nearby:
            p = protein_lookup(bio_atom)
            d = _dist(lig_atom['coord'], p['coord'])

            # ---- Hydrogen bond ----
            lig_is_donor = lig_atom['idx'] in feat_idx.get('Donor', set())
            lig_is_acceptor = lig_atom['idx'] in feat_idx.get('Acceptor', set())
            prot_donor_parent = None
            prot_acceptor = False
            prot_donor = False
            if p['atom_name'] in BACKBONE_DONOR_ATOMS and p['resname'] not in PROLINE_NO_BACKBONE_DONOR:
                prot_donor, prot_donor_parent = True, BACKBONE_DONOR_PARENT
            if p['atom_name'] in BACKBONE_ACCEPTOR_ATOMS:
                prot_acceptor = True
            sc_donor = SIDECHAIN_DONORS.get(p['resname'], {})
            if p['atom_name'] in sc_donor:
                prot_donor, prot_donor_parent = True, sc_donor[p['atom_name']]
            sc_acceptor = SIDECHAIN_ACCEPTORS.get(p['resname'], {})
            if p['atom_name'] in sc_acceptor:
                prot_acceptor = True

            # Each check below is independent - a single atom pair can
            # legitimately be classified as more than one interaction type
            # (e.g. an amidinium...carboxylate contact is both a salt
            # bridge and a hydrogen bond). Don't let one match suppress
            # another for the same pair.
            if d <= HBOND_DISTANCE_CUTOFF and lig_atom['element'] in ('N', 'O', 'F', 'S'):
                # Ligand donor -> protein acceptor
                if lig_is_donor and prot_acceptor:
                    interactions.append({
                        'type': 'hydrogen_bond', 'role': 'ligand_donor',
                        'distance': round(d, 2),
                        'protein': {'chain': p['chain'], 'resname': p['resname'], 'resnum': p['resnum'], 'atom': p['atom_name']},
                        'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
                    })
                # Protein donor -> ligand acceptor, with directionality check
                if prot_donor and lig_is_acceptor and prot_donor_parent:
                    parent_coord = next((a['coord'] for a in protein_atoms
                                          if a['chain'] == p['chain'] and a['resnum'] == p['resnum']
                                          and a['atom_name'] == prot_donor_parent), None)
                    if parent_coord:
                        angle = _angle_deg(parent_coord, p['coord'], lig_atom['coord'])
                        if angle >= HBOND_MIN_DIRECTIONALITY_ANGLE:
                            interactions.append({
                                'type': 'hydrogen_bond', 'role': 'protein_donor',
                                'distance': round(d, 2), 'angle': round(angle, 1),
                                'protein': {'chain': p['chain'], 'resname': p['resname'], 'resnum': p['resnum'], 'atom': p['atom_name']},
                                'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
                            })

            # ---- Hydrophobic contact ----
            if (lig_atom['idx'] in feat_idx.get('Hydrophobe', set()) or lig_atom['idx'] in feat_idx.get('LumpedHydrophobe', set())) \
                    and p['element'] == 'C' and lig_atom['element'] == 'C':
                vdw_sum = lig_atom['vdw'] + 1.7  # carbon vdW radius (Biopython doesn't expose vdW; use RDKit's C value)
                if d <= vdw_sum + HYDROPHOBIC_VDW_TOLERANCE:
                    interactions.append({
                        'type': 'hydrophobic', 'distance': round(d, 2),
                        'protein': {'chain': p['chain'], 'resname': p['resname'], 'resnum': p['resnum'], 'atom': p['atom_name']},
                        'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
                    })

            # ---- Halogen bond ----
            # The sigma-hole sits on the far side of the C-X bond, so a genuine
            # halogen bond needs a near-linear C-X...acceptor angle; a halogen
            # merely close to an acceptor is not one.
            if (d <= HALOGEN_BOND_DISTANCE_CUTOFF
                    and lig_atom['element'] in HALOGENS and prot_acceptor):
                parent_idx = lig_atom['neighbors'][0] if lig_atom['neighbors'] else None
                if parent_idx is not None:
                    c_coord = lig_atoms[parent_idx]['coord']
                    x_angle = _angle_deg(c_coord, lig_atom['coord'], p['coord'])
                    if x_angle >= HALOGEN_BOND_MIN_DONOR_ANGLE:
                        interactions.append({
                            'type': 'halogen_bond', 'distance': round(d, 2),
                            'angle': round(x_angle, 1),
                            'protein': {'chain': p['chain'], 'resname': p['resname'], 'resnum': p['resnum'], 'atom': p['atom_name']},
                            'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
                        })

            # ---- Salt bridge ----
            if d <= SALT_BRIDGE_DISTANCE_CUTOFF:
                lig_neg = lig_atom['idx'] in feat_idx.get('NegIonizable', set())
                lig_pos = lig_atom['idx'] in feat_idx.get('PosIonizable', set())
                prot_neg_atoms = NEGATIVE_RESIDUE_ATOMS.get(p['resname'], [])
                prot_pos_atoms = POSITIVE_RESIDUE_ATOMS.get(p['resname'], [])
                if lig_pos and p['atom_name'] in prot_neg_atoms:
                    interactions.append({
                        'type': 'salt_bridge', 'role': 'ligand_positive', 'distance': round(d, 2),
                        'protein': {'chain': p['chain'], 'resname': p['resname'], 'resnum': p['resnum'], 'atom': p['atom_name']},
                        'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
                    })
                elif lig_neg and p['atom_name'] in prot_pos_atoms:
                    interactions.append({
                        'type': 'salt_bridge', 'role': 'ligand_negative', 'distance': round(d, 2),
                        'protein': {'chain': p['chain'], 'resname': p['resname'], 'resnum': p['resnum'], 'atom': p['atom_name']},
                        'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
                    })

    # ---- Pi-stacking: ligand aromatic rings vs protein aromatic residues ----
    ring_info = mol.GetRingInfo()
    lig_rings = []
    for ring in ring_info.AtomRings():
        if all(mol.GetAtomWithIdx(i).GetIsAromatic() for i in ring):
            coords = [lig_atoms[i]['coord'] for i in ring]
            lig_rings.append({'atom_ids': ring, 'centroid': _centroid(coords), 'normal': _ring_normal(coords)})

    prot_rings = {}
    for a in protein_atoms:
        ring_atom_names = AROMATIC_RING_ATOMS.get(a['resname'])
        if ring_atom_names and a['atom_name'] in ring_atom_names:
            key = (a['chain'], a['resnum'], a['resname'])
            prot_rings.setdefault(key, []).append(a)

    for key, atoms in prot_rings.items():
        expected = AROMATIC_RING_ATOMS[key[2]]
        if len(atoms) < len(expected):
            continue  # incomplete ring in this structure, skip
        coords = [a['coord'] for a in atoms]
        p_centroid = _centroid(coords)
        p_normal = _ring_normal(coords)
        for lring in lig_rings:
            cd = _dist(p_centroid, lring['centroid'])
            if cd > PISTACK_CENTROID_DISTANCE_CUTOFF:
                continue
            normal_angle = _angle_between_vectors_deg(p_normal, lring['normal'])
            normal_angle = min(normal_angle, 180 - normal_angle)  # fold to [0,90]
            is_parallel = normal_angle <= PISTACK_PARALLEL_MAX_ANGLE
            is_perpendicular = abs(normal_angle - 90) <= PISTACK_PERPENDICULAR_TOLERANCE
            if is_parallel or is_perpendicular:
                interactions.append({
                    'type': 'pi_stacking',
                    'geometry': 'face-to-face' if is_parallel else 'edge-to-face',
                    'distance': round(cd, 2), 'angle': round(normal_angle, 1),
                    'protein': {'chain': key[0], 'resname': key[2], 'resnum': key[1], 'atom': 'ring_centroid'},
                    'ligand_atom_index': None, 'ligand_element': None,
                    # Ring-level interactions have no single ligand atom; the 2D
                    # diagram anchors them to the whole ring instead.
                    'ligand_atom_indices': list(lring['atom_ids']),
                })

    # ---- Cation-pi: protein cationic side chain over a ligand aromatic ring ----
    # The offset angle is measured between the ring normal and the ring-centroid
    # -> cation vector, so a cation sitting over the ring face scores ~0 deg while
    # one merely beside the ring in-plane scores ~90 deg and is rejected.
    cation_groups = {}
    for a in protein_atoms:
        group_atoms = CATION_GROUP_ATOMS.get(a['resname'])
        if group_atoms and a['atom_name'] in group_atoms:
            cation_groups.setdefault((a['chain'], a['resnum'], a['resname']), []).append(a)

    for key, group in cation_groups.items():
        cat_centre = _centroid([a['coord'] for a in group])
        for lring in lig_rings:
            cd = _dist(cat_centre, lring['centroid'])
            if cd > CATION_PI_DISTANCE_CUTOFF:
                continue
            offset = _angle_between_vectors_deg(lring['normal'], _vec(lring['centroid'], cat_centre))
            offset = min(offset, 180 - offset)  # ring normal sign is arbitrary
            if offset <= CATION_PI_MAX_OFFSET_ANGLE:
                interactions.append({
                    'type': 'cation_pi', 'role': 'protein_cation',
                    'distance': round(cd, 2), 'angle': round(offset, 1),
                    'protein': {'chain': key[0], 'resname': key[2], 'resnum': key[1],
                                'atom': 'charge_centre'},
                    'ligand_atom_index': None, 'ligand_element': None,
                    'ligand_atom_indices': list(lring['atom_ids']),
                })

    # ---- Cation-pi: ligand cation over a protein aromatic ring ----
    lig_cations = [a for a in lig_atoms if a['idx'] in feat_idx.get('PosIonizable', set())]
    for key, ring_atoms in prot_rings.items():
        if len(ring_atoms) < len(AROMATIC_RING_ATOMS[key[2]]):
            continue
        coords = [a['coord'] for a in ring_atoms]
        p_centroid, p_normal = _centroid(coords), _ring_normal(coords)
        for cat in lig_cations:
            cd = _dist(p_centroid, cat['coord'])
            if cd > CATION_PI_DISTANCE_CUTOFF:
                continue
            offset = _angle_between_vectors_deg(p_normal, _vec(p_centroid, cat['coord']))
            offset = min(offset, 180 - offset)
            if offset <= CATION_PI_MAX_OFFSET_ANGLE:
                interactions.append({
                    'type': 'cation_pi', 'role': 'ligand_cation',
                    'distance': round(cd, 2), 'angle': round(offset, 1),
                    'protein': {'chain': key[0], 'resname': key[2], 'resnum': key[1],
                                'atom': 'ring_centroid'},
                    'ligand_atom_index': cat['idx'], 'ligand_element': cat['element'],
                })

    # ---- Metal coordination and water-mediated bridges ----
    # Both rely on HETATM records, which the polymer parse drops; a parse failure
    # here must not lose the polymer interactions already collected above.
    try:
        waters, metals = _parse_het_atoms(protein_text, protein_format)
    except Exception as exc:
        logger.warning('HETATM parse for waters/metals failed: %s', exc, exc_info=True)
        waters, metals = [], []

    for metal in metals:
        for lig_atom in lig_atoms:
            if lig_atom['element'] not in METAL_COORDINATING_ELEMENTS:
                continue
            d = _dist(metal['coord'], lig_atom['coord'])
            if d > METAL_COORDINATION_CUTOFF:
                continue
            # Report the protein-side residues completing the same shell, which is
            # what makes a metal site interpretable rather than a bare contact.
            shell = []
            for p in protein_atoms:
                if p['atom_name'] not in METAL_PROTEIN_ATOMS.get(p['resname'], []):
                    continue
                if _dist(metal['coord'], p['coord']) <= METAL_COORDINATION_CUTOFF:
                    shell.append(f"{p['resname']}{p['resnum']}.{p['atom_name']}")
            interactions.append({
                'type': 'metal_coordination', 'distance': round(d, 2),
                'protein': {'chain': metal['chain'], 'resname': metal['resname'],
                            'resnum': metal['resnum'], 'atom': metal['atom_name']},
                'metal_element': metal['element'],
                'coordinating_residues': sorted(set(shell)),
                'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
            })

    if waters:
        # A bridging water must reach a ligand polar atom AND a protein polar atom
        # at once. Waters are indexed first so the protein sweep runs only for the
        # few waters that already touch the ligand.
        lig_polar = [a for a in lig_atoms
                     if a['element'] in ('N', 'O')
                     and (a['idx'] in feat_idx.get('Donor', set())
                          or a['idx'] in feat_idx.get('Acceptor', set()))]
        for water in waters:
            lig_side = []
            for lig_atom in lig_polar:
                d = _dist(water['coord'], lig_atom['coord'])
                if WATER_BRIDGE_MIN_DISTANCE <= d <= WATER_BRIDGE_MAX_DISTANCE:
                    lig_side.append((d, lig_atom))
            if not lig_side:
                continue
            for p in protein_atoms:
                if p['element'] not in ('N', 'O'):
                    continue
                is_polar_site = (
                    p['atom_name'] in BACKBONE_DONOR_ATOMS
                    or p['atom_name'] in BACKBONE_ACCEPTOR_ATOMS
                    or p['atom_name'] in SIDECHAIN_DONORS.get(p['resname'], {})
                    or p['atom_name'] in SIDECHAIN_ACCEPTORS.get(p['resname'], {})
                )
                if not is_polar_site:
                    continue
                dp = _dist(water['coord'], p['coord'])
                if not (WATER_BRIDGE_MIN_DISTANCE <= dp <= WATER_BRIDGE_MAX_DISTANCE):
                    continue
                dl, lig_atom = min(lig_side, key=lambda pair: pair[0])
                interactions.append({
                    'type': 'water_bridge',
                    'distance': round(dl + dp, 2),
                    'ligand_water_distance': round(dl, 2),
                    'protein_water_distance': round(dp, 2),
                    'water': {'chain': water['chain'], 'resnum': water['resnum'],
                              'resname': water['resname']},
                    'protein': {'chain': p['chain'], 'resname': p['resname'],
                                'resnum': p['resnum'], 'atom': p['atom_name']},
                    'ligand_atom_index': lig_atom['idx'], 'ligand_element': lig_atom['element'],
                })

    counts = {}
    for i in interactions:
        counts[i['type']] = counts.get(i['type'], 0) + 1

    return {
        'success': True,
        'interactions': interactions,
        'counts': counts,
        'criteria_note': (
            'Hydrogen bonds: heavy-atom donor...acceptor distance <=3.5A + a directional '
            'proxy angle at the donor (no explicit hydrogens are placed). Hydrophobic contacts: '
            'carbon-carbon distance within RDKit van der Waals radii + 0.5A tolerance. Salt bridges: '
            'charged-group distance <=4.0A (ligand ionizable groups via RDKit feature typing, protein '
            'via Asp/Glu/Lys/Arg side-chain atoms). Pi-stacking: aromatic ring centroid distance '
            '<=6.0A with face-to-face or edge-to-face ring-normal geometry. Halogen bonds: '
            'Cl/Br/I...acceptor <=4.0A with a C-X...acceptor angle >=140 degrees (sigma-hole '
            'directionality). Cation-pi: cation-to-ring-centroid <=6.0A with the cation within 30 '
            'degrees of the ring axis. Metal coordination: ligand N/O/S within 2.8A of a monatomic '
            'metal ion, reported with the protein residues completing the same shell. Water bridges: '
            'a single water oxygen 2.4-3.5A from both a ligand polar atom and a protein polar atom. '
            'Criteria are modeled loosely on PLIP\'s public documentation, implemented independently.'
        ),
    }
