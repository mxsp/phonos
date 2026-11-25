"""Split a predicted protein+ligand complex (PDB/mmCIF) into its parts.

OpenFold3 / Boltz2 return the bound ligand baked into a single combined
structure file (the ligand atoms are HETATM records alongside the polymer
ATOM records). For the output viewer we want to offer three artifacts:

    * the combined complex (as-returned)            -> rendered by default
    * the protein only (combined minus the ligand)  -> opt-in
    * the ligand on its own, as an SDF              -> opt-in / downloadable

These helpers are the read-only, pure-Python counterpart of the extraction the
OpenFold3 worker already does at job time (``production/openfold3_worker.py``).
They are reused by ``view_output`` so existing jobs — whose split files were
never written — gain the same protein/ligand/combined split on the fly.

No heavy dependencies: RDKit is used when present (proper bond perception),
otherwise a bond-less V2000 block is emitted and the viewer infers bonds from
inter-atomic distances.
"""

from typing import List, Optional

# Waters and monatomic ions are never the ligand of interest — drop them from
# both the ligand SDF and the "is there a ligand?" check.
_WATER_OR_ION = ('HOH', 'WAT', 'NA', 'CL', 'MG', 'CA', 'ZN', 'K', 'H2O')


def _is_water_or_ion_cif(parts: List[str]) -> bool:
    return len(parts) > 5 and parts[5] in _WATER_OR_ION


def _is_water_or_ion_pdb(resname: str) -> bool:
    return resname in _WATER_OR_ION


def _line_residue_id(line: str, is_cif: bool) -> Optional[tuple]:
    """(chain, resnum, resname) for a HETATM line, or None if unparseable.

    mmCIF field positions follow the same positional convention the rest of this
    module already relies on (the canonical RCSB ``_atom_site`` column order):
    5 = label_comp_id, 6 = label_asym_id, 16 = auth_seq_id. ``label_seq_id`` is
    typically '.' for a HETATM ligand, so the author numbering is preferred.
    """
    try:
        if is_cif:
            parts = line.split()
            if len(parts) < 13:
                return None
            resname = parts[5]
            chain = parts[6] if len(parts) > 6 else ''
            resnum = ''
            for idx in (16, 8):
                if len(parts) > idx and parts[idx] not in ('.', '?'):
                    resnum = parts[idx]
                    break
            return (chain, str(resnum), resname)
        if len(line) < 27:
            return None
        return (line[21].strip(), line[22:26].strip(), line[17:20].strip())
    except (IndexError, ValueError):
        return None


def _matches_selector(residue_id: Optional[tuple], selector: Optional[dict]) -> bool:
    """True when a HETATM residue matches an optional {chain,resnum,resname}.

    Only the keys present in the selector are compared, so a selector of
    ``{'resname': 'ATP'}`` picks every ATP copy while adding ``chain``/``resnum``
    narrows it to one binding site.
    """
    if not selector:
        return True
    if residue_id is None:
        return False
    chain, resnum, resname = residue_id
    if selector.get('resname') and str(selector['resname']).strip().upper() != resname.strip().upper():
        return False
    if selector.get('chain') not in (None, '') and str(selector['chain']).strip() != chain.strip():
        return False
    if selector.get('resnum') not in (None, '') and str(selector['resnum']).strip() != str(resnum).strip():
        return False
    return True


def _extract_ligand_lines(lines: List[str], is_cif: bool,
                          selector: Optional[dict] = None) -> List[str]:
    """Non-water / non-ion HETATM lines from a structure file.

    With a ``selector`` only the matching ligand residue's lines are returned,
    which is what keeps a multi-ligand complex from being merged into one
    nonsensical molecule.
    """
    ligand_lines = []
    for line in lines:
        stripped = line.rstrip('\n')
        if not stripped.startswith('HETATM'):
            continue
        if is_cif:
            parts = stripped.split()
            if _is_water_or_ion_cif(parts):
                continue
        elif len(stripped) > 17:
            if _is_water_or_ion_pdb(stripped[17:20].strip()):
                continue
        else:
            continue
        if selector and not _matches_selector(_line_residue_id(stripped, is_cif), selector):
            continue
        ligand_lines.append(stripped)
    return ligand_lines


def list_ligand_residues(structure_text: str, fmt: str) -> List[dict]:
    """Every distinct non-water/ion HETATM residue in a structure.

    Used to populate a ligand picker: a complex with several bound ligands (or
    several copies of one) needs the user to say which one the diagram is for.
    """
    if not structure_text:
        return []
    is_cif = fmt in ('cif', 'mmcif')
    found = {}
    order = []
    for line in _extract_ligand_lines(structure_text.split('\n'), is_cif):
        residue_id = _line_residue_id(line, is_cif)
        if residue_id is None:
            continue
        if residue_id not in found:
            chain, resnum, resname = residue_id
            found[residue_id] = {
                'chain': chain, 'resnum': resnum, 'resname': resname, 'atom_count': 0,
            }
            order.append(residue_id)
        found[residue_id]['atom_count'] += 1
    return [found[key] for key in order]


# Covalent radii (Å, Cordero et al.) for distance-based bond perception. HETATM
# records carry no connectivity, so without this a ligand SDF has zero bonds and
# the viewer draws a cloud of disconnected atoms instead of a molecule.
_COVALENT_RADII = {
    'H': 0.31, 'B': 0.84, 'C': 0.76, 'N': 0.71, 'O': 0.66, 'F': 0.57,
    'P': 1.07, 'S': 1.05, 'CL': 1.02, 'BR': 1.20, 'I': 1.39, 'SI': 1.11,
    'SE': 1.20, 'NA': 1.66, 'MG': 1.41, 'K': 2.03, 'CA': 1.76, 'ZN': 1.22,
    'FE': 1.32, 'MN': 1.39, 'CO': 1.26, 'NI': 1.24, 'CU': 1.32,
}
_DEFAULT_RADIUS = 0.77
_BOND_TOLERANCE = 0.45

# Elements kept as themselves when rebuilding a ligand for RDKit; anything else
# is approximated as carbon so connectivity perception still produces a drawable
# skeleton. Compared by upper-case symbol, constructed with canonical casing.
_SDF_ELEMENTS = frozenset({
    'C', 'N', 'O', 'S', 'P', 'F', 'CL', 'BR', 'I', 'H', 'B', 'SE', 'SI', 'AS',
})


def _perceive_bonds(coords) -> List[tuple]:
    """Single bonds between atoms within covalent range, as 0-based (i, j) pairs.

    ``coords`` is a list of ``(x, y, z, element)``. Bond orders are not needed
    for the viewer's ball-and-stick display, only connectivity (sticks).
    """
    bonds = []
    n = len(coords)
    for i in range(n):
        xi, yi, zi, ei = coords[i]
        ri = _COVALENT_RADII.get(ei, _DEFAULT_RADIUS)
        for j in range(i + 1, n):
            xj, yj, zj, ej = coords[j]
            cutoff = ri + _COVALENT_RADII.get(ej, _DEFAULT_RADIUS) + _BOND_TOLERANCE
            dx, dy, dz = xi - xj, yi - yj, zi - zj
            d2 = dx * dx + dy * dy + dz * dz
            if 0.16 < d2 <= cutoff * cutoff:  # 0.16 = 0.4**2 guards atom overlaps
                bonds.append((i, j))
    return bonds


def _molblock_has_bonds(molblock: str) -> bool:
    """True when a V2000 mol block's counts line declares at least one bond."""
    try:
        return int(molblock.split('\n')[3][3:6]) > 0
    except (IndexError, ValueError):
        return False


def _ligand_lines_to_sdf_simple(ligand_lines: List[str], is_cif: bool) -> Optional[str]:
    """Distance-bonded V2000 SDF fallback (no RDKit required)."""
    if not ligand_lines:
        return None
    coords_list = []
    resname = 'LIG'
    for line in ligand_lines:
        if is_cif:
            parts = line.split()
            if len(parts) < 13:
                continue
            atom_name = parts[3] if len(parts) > 3 else 'C'
            resname = parts[5] if len(parts) > 5 else 'LIG'
            try:
                x, y, z = float(parts[10]), float(parts[11]), float(parts[12])
            except (ValueError, IndexError):
                continue
            element = parts[-1] if len(parts[-1]) <= 2 else atom_name[0]
        else:
            if len(line) < 54:
                continue
            atom_name = line[12:16].strip()
            resname = line[17:20].strip()
            try:
                x = float(line[30:38])
                y = float(line[38:46])
                z = float(line[46:54])
            except ValueError:
                continue
            element = line[76:78].strip() or atom_name[0]
        coords_list.append((x, y, z, element.upper()[:2]))

    if not coords_list:
        return None

    bonds = _perceive_bonds(coords_list)
    out = [f"{resname}\n", "  Phonos\n\n",
           f"{len(coords_list):3d}{len(bonds):3d}  0  0  0  0  0  0  0  0999 V2000\n"]
    for x, y, z, element in coords_list:
        out.append(f"{x:10.4f}{y:10.4f}{z:10.4f} {element.ljust(2)} 0  0  0  0  0  0  0  0  0  0  0  0\n")
    for i, j in bonds:
        out.append(f"{i + 1:3d}{j + 1:3d}  1  0\n")
    out.append("M  END\n$$$$\n")
    return ''.join(out)


def _ligand_lines_to_sdf(ligand_lines: List[str], is_cif: bool) -> Optional[str]:
    """SDF for the extracted ligand atoms, with RDKit bonds when available."""
    if not ligand_lines:
        return None
    try:
        from rdkit import Chem
        from rdkit.Chem import rdDetermineBonds
    except ImportError:
        return _ligand_lines_to_sdf_simple(ligand_lines, is_cif)

    try:
        mol = Chem.RWMol()
        atom_map = {}
        coords = {}
        resname = 'LIG'
        for line in ligand_lines:
            if is_cif:
                parts = line.split()
                if len(parts) < 13:
                    continue
                atom_id = parts[1]
                atom_name = parts[3] if len(parts) > 3 else 'C'
                resname = parts[5] if len(parts) > 5 else 'LIG'
                try:
                    x, y, z = float(parts[10]), float(parts[11]), float(parts[12])
                except (ValueError, IndexError):
                    continue
                element = parts[-1] if len(parts[-1]) <= 2 else atom_name[0]
            else:
                if len(line) < 54:
                    continue
                atom_id = line[6:11].strip()
                atom_name = line[12:16].strip()
                resname = line[17:20].strip()
                try:
                    x = float(line[30:38])
                    y = float(line[38:46])
                    z = float(line[46:54])
                except ValueError:
                    continue
                element = line[76:78].strip() or atom_name[0]

            # RDKit's Atom() wants the canonical symbol casing ('Cl', not 'CL'),
            # and raises on anything else - which previously aborted the whole
            # RDKit path for any halogenated ligand and silently dropped it to
            # the distance-bonded fallback.
            element = element.upper()
            atom = (Chem.Atom(element.capitalize()) if element in _SDF_ELEMENTS
                    else Chem.Atom('C'))
            idx = mol.AddAtom(atom)
            atom_map[atom_id] = idx
            coords[atom_id] = (x, y, z)

        if not atom_map:
            return None

        conf = Chem.Conformer(len(atom_map))
        for atom_id, idx in atom_map.items():
            conf.SetAtomPosition(idx, coords.get(atom_id, (0, 0, 0)))
        mol.AddConformer(conf, assignId=True)

        # HETATM blocks have no bonds — perceive them from the 3D coordinates so
        # the ligand renders connected. Never re-embed (that would scramble the
        # real binding pose); on any failure fall back to the distance-bonded SDF.
        # Connectivity (single bonds) is all the ball-and-stick viewer needs;
        # perceiving bond orders would stamp radical records onto charged ligands
        # (e.g. GTP phosphates) for no visual gain.
        perceived = mol.GetMol()
        sdf = None
        try:
            rdDetermineBonds.DetermineConnectivity(perceived)
            sdf = Chem.MolToMolBlock(perceived, kekulize=False)
        except Exception:
            sdf = None

        if sdf and _molblock_has_bonds(sdf):
            header = f"{resname}\n     RDKit          3D\n\n"
            return header + sdf.split('\n', 3)[-1] + "$$$$\n"
        # Connectivity perception failed or produced no bonds -> distance fallback.
        return _ligand_lines_to_sdf_simple(ligand_lines, is_cif)
    except Exception:
        # Any RDKit hiccup -> still produce a usable (distance-bonded) SDF.
        return _ligand_lines_to_sdf_simple(ligand_lines, is_cif)


def structure_has_ligand(structure_text: str, fmt: str) -> bool:
    """True when the structure carries a real (non-water/ion) HETATM ligand."""
    if not structure_text:
        return False
    is_cif = fmt in ('cif', 'mmcif')
    return bool(_extract_ligand_lines(structure_text.split('\n'), is_cif))


def extract_ligand_sdf(structure_text: str, fmt: str,
                       selector: Optional[dict] = None) -> Optional[str]:
    """Ligand atoms of a complex as an SDF string, or None if there is none.

    ``selector`` optionally restricts extraction to a single ligand residue -
    see ``_matches_selector``. Without it every non-water/ion HETATM atom is
    emitted as one molecule, which is the long-standing behaviour used by the
    output viewer's protein/ligand split.
    """
    if not structure_text:
        return None
    is_cif = fmt in ('cif', 'mmcif')
    ligand_lines = _extract_ligand_lines(structure_text.split('\n'), is_cif, selector)
    return _ligand_lines_to_sdf(ligand_lines, is_cif)


def extract_protein_only(structure_text: str, fmt: str) -> Optional[str]:
    """Complex with the HETATM ligand records removed, or None if not parseable."""
    _ = fmt  # plain HETATM filter works for both PDB and mmCIF
    if not structure_text:
        return None
    protein_lines = [ln.rstrip('\n') for ln in structure_text.split('\n')
                     if not ln.startswith('HETATM')]
    result = '\n'.join(protein_lines)
    if '_atom_site.' in result or 'ATOM' in result:
        return result
    return None
