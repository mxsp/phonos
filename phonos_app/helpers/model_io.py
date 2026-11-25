"""Model I/O chaining — turn a model's output into another model's input.

Every model on the platform emits files (FASTA, PDB, CIF, SDF, SMILES, A3M,
JSON) and every model consumes a small number of payload *kinds*. This module
is the single place that answers two questions:

    1. What is in this file?          -> :func:`classify_payload`
    2. Which models can consume it?   -> :func:`compatible_models`

Both the web UI and the REST API go through here, so a chain that works in the
browser works identically from a script — and adding a new model means adding
one entry to :data:`MODEL_INPUT_KINDS` rather than touching any UI code.

Everything in this module is pure: no database, no filesystem, no Flask. That
keeps it cheap to test and safe to call from workers.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------
# Payload kinds
# --------------------------------------------------------------------------

KIND_PROTEIN = 'protein_sequence'
KIND_NUCLEOTIDE = 'nucleotide_sequence'
KIND_STRUCTURE = 'structure'
KIND_LIGAND = 'ligand'
KIND_SMILES = 'smiles'
KIND_MSA = 'msa'

ALL_KINDS = (
    KIND_PROTEIN,
    KIND_NUCLEOTIDE,
    KIND_STRUCTURE,
    KIND_LIGAND,
    KIND_SMILES,
    KIND_MSA,
)

KIND_LABELS = {
    KIND_PROTEIN: 'Protein sequence',
    KIND_NUCLEOTIDE: 'DNA/RNA sequence',
    KIND_STRUCTURE: 'Structure (PDB/mmCIF)',
    KIND_LIGAND: 'Ligand (SDF/MOL)',
    KIND_SMILES: 'SMILES string',
    KIND_MSA: 'Multiple sequence alignment',
}

# Which payload kinds each model can be fed. Keys are model ids from
# Config.SUPPORTED_MODELS; user-defined models are handled separately since
# their inputs are declared per model.
MODEL_INPUT_KINDS: Dict[str, tuple] = {
    # --- Structure prediction from sequence ---
    'type-0':                 (KIND_PROTEIN,),                  # ESMFold
    'type-2':                 (KIND_PROTEIN,),                  # AlphaFold2
    'esmfold-re':             (KIND_PROTEIN,),                  # ESMFold-RE
    'alphafold2-re':          (KIND_PROTEIN,),                  # AlphaFold2-RE
    'alphafold2-re-multimer': (KIND_PROTEIN,),                  # AlphaFold2-RE Multimer
    'type-9':                 (KIND_PROTEIN, KIND_SMILES),      # Boltz2 (optional ligand)
    'boltz2-enhanced':        (KIND_PROTEIN, KIND_SMILES),
    'openfold3':              (KIND_PROTEIN, KIND_SMILES),
    'type-5':                 (KIND_PROTEIN, KIND_SMILES),      # fold & dock
    'type-6':                 (KIND_PROTEIN,),                  # metal binding

    # --- Sequence design from a backbone ---
    'proteinmpnn-re':         (KIND_STRUCTURE,),
    'type-4':                 (KIND_STRUCTURE,),
    'type-12':                (KIND_STRUCTURE,),                # RFDiffusion

    # --- Docking: receptor structure + ligand ---
    'type-3':                 (KIND_STRUCTURE, KIND_LIGAND, KIND_SMILES),
    'diffdock-re':            (KIND_STRUCTURE, KIND_LIGAND, KIND_SMILES),

    # --- Small molecules ---
    'molmim':                 (KIND_SMILES, KIND_LIGAND),
    'genmol':                 (KIND_SMILES, KIND_LIGAND),

    # --- Nucleotides & alignments ---
    'type-8':                 (KIND_NUCLEOTIDE,),               # Evo2 (legacy id)
    'evo2':                   (KIND_NUCLEOTIDE,),
    'msa-search':             (KIND_PROTEIN,),
}

# How a payload reaches the target model's form. 'sequence' fills the sequence
# box, 'smiles' the ligand SMILES box, 'file' selects the file in a file picker.
KIND_DELIVERY = {
    KIND_PROTEIN: 'sequence',
    KIND_NUCLEOTIDE: 'sequence',
    KIND_SMILES: 'smiles',
    KIND_STRUCTURE: 'file',
    KIND_LIGAND: 'file',
    KIND_MSA: 'file',
}

# --------------------------------------------------------------------------
# Format detection
# --------------------------------------------------------------------------

FASTA_EXTENSIONS = {'fasta', 'fa', 'faa', 'fna', 'ffn', 'frn', 'mpnn'}
STRUCTURE_EXTENSIONS = {'pdb', 'cif', 'mmcif', 'ent', 'pdb1'}
LIGAND_EXTENSIONS = {'sdf', 'mol', 'mol2'}
SMILES_EXTENSIONS = {'smi', 'smiles'}
MSA_EXTENSIONS = {'a3m', 'sto', 'stockholm', 'aln', 'clustal'}
TEXTUAL_EXTENSIONS = {'txt', 'text', 'seq', 'csv', 'tsv', 'json'}

# Residues that only ever appear in a protein, used as a hard "this is protein"
# signal. U (selenocysteine) and O (pyrrolysine) are deliberately NOT in this
# set: they are vanishingly rare as amino acids but U is the defining letter of
# RNA, so including them would misread every RNA sequence as a peptide.
_PROTEIN_ONLY = set('EFILPQZXJBVWY')
_NUCLEOTIDE_ALPHABET = set('ACGTUN-')
_PROTEIN_ALPHABET = set('ACDEFGHIKLMNPQRSTVWYXBZJUO*-')

THREE_TO_ONE = {
    'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C', 'GLN': 'Q',
    'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I', 'LEU': 'L', 'LYS': 'K',
    'MET': 'M', 'PHE': 'F', 'PRO': 'P', 'SER': 'S', 'THR': 'T', 'TRP': 'W',
    'TYR': 'Y', 'VAL': 'V', 'SEC': 'U', 'PYL': 'O',
    # Common modified/alternate residues that still fold as their parent.
    'MSE': 'M', 'HSD': 'H', 'HSE': 'H', 'HSP': 'H', 'CYX': 'C', 'HID': 'H',
    'HIE': 'H', 'HIP': 'H',
}

# Nucleic-acid residues seen in structure files, mapped to a one-letter code.
NA_THREE_TO_ONE = {
    'DA': 'A', 'DT': 'T', 'DG': 'G', 'DC': 'C', 'DU': 'U',
    'A': 'A', 'U': 'U', 'G': 'G', 'C': 'C', 'T': 'T', 'N': 'N',
}

# Residue names that are solvent/ions rather than ligands worth extracting.
_NON_LIGAND_HETATMS = {
    'HOH', 'WAT', 'DOD', 'SO4', 'PO4', 'GOL', 'EDO', 'PEG', 'MPD', 'ACT',
    'NA', 'CL', 'K', 'MG', 'CA', 'ZN', 'MN', 'FE', 'CU', 'NI', 'CD', 'CO',
}

MAX_SEQUENCES = 200
MAX_SEQUENCE_LENGTH = 100_000


def _ext(filename: str) -> str:
    """Lower-cased final extension, transparently unwrapping a .gz suffix."""
    name = (filename or '').strip().lower()
    if name.endswith('.gz'):
        name = name[:-3]
    _, _, ext = name.rpartition('.')
    return ext if ext and ext != name else ''


def clean_residues(raw: str, alphabet: Optional[set] = None) -> str:
    """Strip whitespace/numbers from a sequence body and upper-case it."""
    if not raw:
        return ''
    cleaned = re.sub(r'[\s\d]+', '', raw).upper()
    if alphabet is not None:
        cleaned = ''.join(ch for ch in cleaned if ch in alphabet)
    return cleaned


def detect_sequence_kind(sequence: str) -> str:
    """Classify a residue string as protein or nucleotide.

    Short sequences made only of A/C/G/T/U/N are ambiguous by construction —
    ``ACGT`` is both a valid peptide and valid DNA. We resolve that the way a
    biologist would: any residue outside the nucleotide alphabet means protein,
    otherwise a string that is overwhelmingly ACGTUN is nucleotide.
    """
    seq = clean_residues(sequence)
    if not seq:
        return KIND_PROTEIN
    letters = set(seq)
    if letters & _PROTEIN_ONLY:
        return KIND_PROTEIN
    na_hits = sum(1 for ch in seq if ch in _NUCLEOTIDE_ALPHABET)
    if na_hits / len(seq) >= 0.9:
        return KIND_NUCLEOTIDE
    return KIND_PROTEIN


def parse_fasta(content: str) -> List[Dict[str, Any]]:
    """Parse FASTA text into ``[{name, sequence, kind, length}]``.

    Tolerates a bare sequence with no ``>`` header — several models emit one —
    and skips records whose body has no residues left after cleaning.
    """
    if not content:
        return []

    records: List[Dict[str, Any]] = []
    name: Optional[str] = None
    buffer: List[str] = []

    def flush() -> None:
        if len(records) >= MAX_SEQUENCES:
            return
        body = clean_residues(''.join(buffer), _PROTEIN_ALPHABET | _NUCLEOTIDE_ALPHABET)
        if not body:
            return
        body = body[:MAX_SEQUENCE_LENGTH]
        records.append({
            'name': (name or f'sequence_{len(records) + 1}').strip(),
            'sequence': body,
            'kind': detect_sequence_kind(body),
            'length': len(body),
        })

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in '>;':
            flush()
            name = stripped[1:].strip() or None
            buffer = []
        else:
            buffer.append(stripped)
    flush()
    return records


def parse_structure_sequences(content: str, fmt: str = 'pdb') -> List[Dict[str, Any]]:
    """Extract one per-chain sequence from a PDB or mmCIF structure.

    Reads CA/backbone atoms rather than SEQRES so it also works for the
    predicted structures our models emit, which frequently omit SEQRES.
    """
    if not content:
        return []
    if fmt in ('cif', 'mmcif'):
        chains = _cif_chain_residues(content)
    else:
        chains = _pdb_chain_residues(content)

    out: List[Dict[str, Any]] = []
    for chain, residues in chains.items():
        if not residues:
            continue
        body = ''.join(residues)[:MAX_SEQUENCE_LENGTH]
        out.append({
            'name': f'chain_{chain}',
            'chain': chain,
            'sequence': body,
            'kind': detect_sequence_kind(body),
            'length': len(body),
        })
        if len(out) >= MAX_SEQUENCES:
            break
    return out


def _residue_letter(resname: str) -> Optional[str]:
    key = (resname or '').strip().upper()
    if key in THREE_TO_ONE:
        return THREE_TO_ONE[key]
    if key in NA_THREE_TO_ONE:
        return NA_THREE_TO_ONE[key]
    return None


def _pdb_chain_residues(content: str) -> Dict[str, List[str]]:
    chains: Dict[str, List[str]] = {}
    seen: set = set()
    for line in content.splitlines():
        if not line.startswith('ATOM'):
            continue
        atom = line[12:16].strip()
        # One representative atom per residue: CA for proteins, P/C1' for
        # nucleic acids (which have no CA at all).
        if atom not in ("CA", "P", "C1'"):
            continue
        chain = line[21:22].strip() or 'A'
        resname = line[17:20]
        resseq = line[22:27].strip()
        key = (chain, resseq)
        if key in seen:
            continue
        letter = _residue_letter(resname)
        if letter is None:
            continue
        seen.add(key)
        chains.setdefault(chain, []).append(letter)
    return chains


def _cif_chain_residues(content: str) -> Dict[str, List[str]]:
    chains: Dict[str, List[str]] = {}
    seen: set = set()
    fields: List[str] = []
    in_loop = False

    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith('_atom_site.'):
            if not in_loop:
                fields = []
                in_loop = True
            fields.append(line)
            continue
        if not in_loop:
            continue
        if not line or line[0] in '#_' or line == 'loop_':
            if chains:
                break
            in_loop = False
            fields = []
            continue

        parts = line.split()
        if len(parts) < len(fields):
            continue
        idx = {name: i for i, name in enumerate(fields)}

        def col(name: str) -> Optional[str]:
            i = idx.get(name)
            return parts[i] if i is not None and i < len(parts) else None

        if (col('_atom_site.group_PDB') or 'ATOM') != 'ATOM':
            continue
        atom = col('_atom_site.label_atom_id') or ''
        if atom.strip('"') not in ("CA", "P", "C1'"):
            continue
        chain = col('_atom_site.auth_asym_id') or col('_atom_site.label_asym_id') or 'A'
        resname = col('_atom_site.label_comp_id') or ''
        resseq = col('_atom_site.auth_seq_id') or col('_atom_site.label_seq_id') or ''
        key = (chain, resseq)
        if key in seen:
            continue
        letter = _residue_letter(resname)
        if letter is None:
            continue
        seen.add(key)
        chains.setdefault(chain, []).append(letter)
    return chains


def extract_structure_ligands(content: str) -> List[str]:
    """Residue names of non-solvent HETATM groups embedded in a structure."""
    names: List[str] = []
    seen: set = set()
    for line in (content or '').splitlines():
        if not line.startswith('HETATM'):
            continue
        resname = line[17:20].strip().upper()
        if not resname or resname in _NON_LIGAND_HETATMS or resname in seen:
            continue
        seen.add(resname)
        names.append(resname)
        if len(names) >= 20:
            break
    return names


_SMILES_RE = re.compile(r'^[A-Za-z0-9@+\-\[\]()=#$%/\\.*]+$')


def looks_like_smiles(text: str, trusted: bool = False) -> bool:
    """Heuristic SMILES check for a single-line candidate string.

    ``trusted`` relaxes the check to the character class alone — use it when
    the file extension already declares the format. Without it, a plain string
    like ``CCO`` (ethanol, but also a tripeptide) is rejected, because guessing
    wrong on an ambiguous ``.txt`` is worse than offering one fewer target.
    """
    candidate = (text or '').strip()
    if not candidate or len(candidate) > 1000 or ' ' in candidate:
        return False
    if not _SMILES_RE.match(candidate):
        return False
    if trusted:
        return True
    # A bare peptide/DNA string also matches the character class, so require at
    # least one token that only ever appears in SMILES.
    return bool(re.search(r'[=#()\[\]@+\-.]|[cnops]', candidate))


def parse_smiles(content: str, trusted: bool = False) -> List[Dict[str, str]]:
    """Parse a .smi/.smiles listing: one ``SMILES [name]`` per line."""
    out: List[Dict[str, str]] = []
    for line in (content or '').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        parts = stripped.split(None, 1)
        smiles = parts[0]
        if not looks_like_smiles(smiles, trusted=trusted):
            continue
        out.append({
            'smiles': smiles,
            'name': parts[1].strip() if len(parts) > 1 else f'molecule_{len(out) + 1}',
        })
        if len(out) >= MAX_SEQUENCES:
            break
    return out


def _payload_from_json(content: str) -> Dict[str, Any]:
    """Pull sequences / SMILES out of a JSON result file.

    Several models (Evo2, GenMol, metal binding) write JSON rather than a
    domain format; rather than special-casing each one, look for the handful of
    key names they all converge on.
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return {}

    sequences: List[Dict[str, Any]] = []
    smiles: List[Dict[str, str]] = []

    def take_sequence(value: Any, name: str) -> None:
        if not isinstance(value, str):
            return
        body = clean_residues(value, _PROTEIN_ALPHABET | _NUCLEOTIDE_ALPHABET)
        if len(body) < 2:
            return
        sequences.append({
            'name': name,
            'sequence': body[:MAX_SEQUENCE_LENGTH],
            'kind': detect_sequence_kind(body),
            'length': len(body),
        })

    def take_smiles(value: Any, name: str) -> None:
        if isinstance(value, str) and looks_like_smiles(value):
            smiles.append({'smiles': value.strip(), 'name': name})

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > 6 or len(sequences) >= MAX_SEQUENCES:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = key.lower()
                label = f'{path}.{key}' if path else key
                if lowered in ('sequence', 'sequences', 'generated_sequence', 'output_sequence', 'designed_sequence'):
                    if isinstance(value, list):
                        for i, item in enumerate(value):
                            take_sequence(item, f'{label}_{i + 1}')
                    else:
                        take_sequence(value, label)
                elif lowered in ('smiles', 'smi', 'molecules', 'generated_molecules'):
                    if isinstance(value, list):
                        for i, item in enumerate(value):
                            if isinstance(item, str):
                                candidate = item
                            elif isinstance(item, dict):
                                candidate = item.get('smiles') or item.get('smi')
                            else:
                                candidate = None
                            take_smiles(candidate, f'{label}_{i + 1}')
                    else:
                        take_smiles(value, label)
                else:
                    walk(value, label, depth + 1)
        elif isinstance(node, list):
            for i, item in enumerate(node[:MAX_SEQUENCES]):
                walk(item, f'{path}[{i}]', depth + 1)

    walk(data, '', 0)
    return {'sequences': sequences, 'smiles': smiles}


def classify_payload(filename: str, content: Optional[str] = None) -> Dict[str, Any]:
    """Describe what a file holds and what can be done with it.

    ``content`` is optional: without it the classification falls back to the
    extension alone, which is enough to answer "can this be a docking
    receptor?" but not to extract sequences.

    Returns a dict with ``format``, ``kinds`` (list), ``sequences``,
    ``smiles``, ``ligand_codes`` and ``needs_file`` (True when the payload is
    delivered as a file rather than pasted text).
    """
    ext = _ext(filename)
    result: Dict[str, Any] = {
        'filename': filename,
        'format': ext or None,
        'kinds': [],
        'sequences': [],
        'smiles': [],
        'ligand_codes': [],
    }
    kinds: List[str] = []

    if ext in STRUCTURE_EXTENSIONS:
        kinds.append(KIND_STRUCTURE)
        if content:
            result['sequences'] = parse_structure_sequences(content, 'cif' if ext in ('cif', 'mmcif') else 'pdb')
            result['ligand_codes'] = extract_structure_ligands(content)
    elif ext in LIGAND_EXTENSIONS:
        kinds.append(KIND_LIGAND)
    elif ext in SMILES_EXTENSIONS:
        kinds.append(KIND_SMILES)
        if content:
            # The extension declares the format, so the ambiguity guard in
            # looks_like_smiles() would only cost us valid short molecules.
            result['smiles'] = parse_smiles(content, trusted=True)
    elif ext in MSA_EXTENSIONS:
        kinds.append(KIND_MSA)
        if content:
            # An a3m's first record is the query sequence, which is a perfectly
            # good folding input.
            result['sequences'] = parse_fasta(content)[:1]
    elif ext in FASTA_EXTENSIONS:
        if content:
            result['sequences'] = parse_fasta(content)
    elif ext == 'json':
        if content:
            extracted = _payload_from_json(content)
            result['sequences'] = extracted.get('sequences', [])
            result['smiles'] = extracted.get('smiles', [])
    elif ext in TEXTUAL_EXTENSIONS or not ext:
        if content:
            stripped = content.strip()
            if stripped.startswith('>') or stripped.startswith(';'):
                result['sequences'] = parse_fasta(content)
            else:
                first_line = stripped.splitlines()[0].strip() if stripped else ''
                if looks_like_smiles(first_line):
                    result['smiles'] = parse_smiles(content)
                else:
                    result['sequences'] = parse_fasta(content)

    # Sequence-derived kinds are added from what was actually extracted, so a
    # .fasta of nucleotides is offered to Evo2 and not to ESMFold.
    for record in result['sequences']:
        if record['kind'] not in kinds:
            kinds.append(record['kind'])
    if result['smiles'] and KIND_SMILES not in kinds:
        kinds.append(KIND_SMILES)

    result['kinds'] = kinds
    result['needs_file'] = any(KIND_DELIVERY.get(k) == 'file' for k in kinds)
    return result


def compatible_models(
    kinds: Iterable[str],
    models: Optional[Sequence[Dict[str, Any]]] = None,
    include_unavailable: bool = False,
) -> List[Dict[str, Any]]:
    """Models that accept at least one of ``kinds``.

    ``models`` is the catalog (``Config.SUPPORTED_MODELS`` shape). When omitted,
    a minimal record is synthesised from :data:`MODEL_INPUT_KINDS` so callers
    without the catalog still get a usable answer.
    """
    wanted = [k for k in kinds if k in ALL_KINDS]
    if not wanted:
        return []

    if models is None:
        models = [{'id': mid, 'name': mid} for mid in MODEL_INPUT_KINDS]

    out: List[Dict[str, Any]] = []
    for model in models:
        model_id = model.get('id')
        accepted = MODEL_INPUT_KINDS.get(model_id)
        if not accepted:
            continue
        matched = [k for k in wanted if k in accepted]
        if not matched:
            continue
        available = bool(model.get('available', True)) and bool(model.get('is_active', True))
        if not available and not include_unavailable:
            continue
        out.append({
            'id': model_id,
            'name': model.get('name') or model_id,
            'short_name': model.get('short_name') or model.get('name') or model_id,
            'task': model.get('task'),
            'accepts': list(accepted),
            'matched_kinds': matched,
            'delivery': KIND_DELIVERY.get(matched[0], 'sequence'),
            'available': available,
        })
    return out


def describe_matrix(models: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The full compatibility matrix — what produces what, what accepts what.

    Exposed over the API so integrators can build chains without hard-coding
    this table on their side.
    """
    catalog = {m.get('id'): m for m in (models or [])}
    entries = []
    for model_id, accepted in MODEL_INPUT_KINDS.items():
        model = catalog.get(model_id, {})
        entries.append({
            'id': model_id,
            'name': model.get('name') or model_id,
            'short_name': model.get('short_name') or model.get('name') or model_id,
            'task': model.get('task'),
            'accepts': list(accepted),
            'output_extension': model.get('output_extension'),
            'available': bool(model.get('available', True)) and bool(model.get('is_active', True)),
            'known': model_id in catalog,
        })
    entries.sort(key=lambda e: e['id'])
    return {
        'kinds': [{'id': k, 'label': KIND_LABELS[k], 'delivery': KIND_DELIVERY[k]} for k in ALL_KINDS],
        'models': entries,
        'extensions': {
            'structure': sorted(STRUCTURE_EXTENSIONS),
            'sequence': sorted(FASTA_EXTENSIONS),
            'ligand': sorted(LIGAND_EXTENSIONS),
            'smiles': sorted(SMILES_EXTENSIONS),
            'msa': sorted(MSA_EXTENSIONS),
        },
    }
