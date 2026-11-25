"""Sequence analysis utilities using BioPython.

This module provides comprehensive sequence analysis tools for DNA, RNA, and protein sequences.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, Any

try:
    from Bio.Seq import Seq
    from Bio.SeqUtils import gc_fraction, molecular_weight
    from Bio.SeqUtils.ProtParam import ProteinAnalysis
    BIOPYTHON_AVAILABLE = True
except ImportError:
    BIOPYTHON_AVAILABLE = False


# Valid character sets for each sequence type
VALID_DNA = set('ACGTN')
VALID_RNA = set('ACGUN')
VALID_PROTEIN = set('ACDEFGHIKLMNPQRSTVWXY*')

# Standard genetic code (DNA codons -> amino acids)
CODON_TABLE = {
    'TTT': 'F', 'TTC': 'F', 'TTA': 'L', 'TTG': 'L',
    'TCT': 'S', 'TCC': 'S', 'TCA': 'S', 'TCG': 'S',
    'TAT': 'Y', 'TAC': 'Y', 'TAA': '*', 'TAG': '*',
    'TGT': 'C', 'TGC': 'C', 'TGA': '*', 'TGG': 'W',
    'CTT': 'L', 'CTC': 'L', 'CTA': 'L', 'CTG': 'L',
    'CCT': 'P', 'CCC': 'P', 'CCA': 'P', 'CCG': 'P',
    'CAT': 'H', 'CAC': 'H', 'CAA': 'Q', 'CAG': 'Q',
    'CGT': 'R', 'CGC': 'R', 'CGA': 'R', 'CGG': 'R',
    'ATT': 'I', 'ATC': 'I', 'ATA': 'I', 'ATG': 'M',
    'ACT': 'T', 'ACC': 'T', 'ACA': 'T', 'ACG': 'T',
    'AAT': 'N', 'AAC': 'N', 'AAA': 'K', 'AAG': 'K',
    'AGT': 'S', 'AGC': 'S', 'AGA': 'R', 'AGG': 'R',
    'GTT': 'V', 'GTC': 'V', 'GTA': 'V', 'GTG': 'V',
    'GCT': 'A', 'GCC': 'A', 'GCA': 'A', 'GCG': 'A',
    'GAT': 'D', 'GAC': 'D', 'GAA': 'E', 'GAG': 'E',
    'GGT': 'G', 'GGC': 'G', 'GGA': 'G', 'GGG': 'G',
}

# RNA codon table (same as DNA but with U instead of T)
RNA_CODON_TABLE = {k.replace('T', 'U'): v for k, v in CODON_TABLE.items()}

# Amino acid molecular weights (average)
AA_MW = {
    'A': 89.09, 'R': 174.20, 'N': 132.12, 'D': 133.10, 'C': 121.16,
    'E': 147.13, 'Q': 146.15, 'G': 75.07, 'H': 155.16, 'I': 131.18,
    'L': 131.18, 'K': 146.19, 'M': 149.21, 'F': 165.19, 'P': 115.13,
    'S': 105.09, 'T': 119.12, 'W': 204.23, 'Y': 181.19, 'V': 117.15,
    'X': 110.0, '*': 0.0
}

# Kyte-Doolittle hydropathy scale
HYDROPATHY = {
    'A': 1.8, 'R': -4.5, 'N': -3.5, 'D': -3.5, 'C': 2.5,
    'E': -3.5, 'Q': -3.5, 'G': -0.4, 'H': -3.2, 'I': 4.5,
    'L': 3.8, 'K': -3.9, 'M': 1.9, 'F': 2.8, 'P': -1.6,
    'S': -0.8, 'T': -0.7, 'W': -0.9, 'Y': -1.3, 'V': 4.2,
    'X': 0.0, '*': 0.0
}

# pKa values for charged residues
PKA_VALUES = {
    'D': 3.9,   # Asp
    'E': 4.1,   # Glu
    'H': 6.0,   # His
    'C': 8.3,   # Cys
    'Y': 10.1,  # Tyr
    'K': 10.5,  # Lys
    'R': 12.5,  # Arg
}

# N-terminus and C-terminus pKa
PKA_N_TERM = 9.69
PKA_C_TERM = 2.34

# DNA complement mapping
DNA_COMPLEMENT = {'A': 'T', 'T': 'A', 'C': 'G', 'G': 'C', 'N': 'N'}
RNA_COMPLEMENT = {'A': 'U', 'U': 'A', 'C': 'G', 'G': 'C', 'N': 'N'}


def clean_sequence(sequence: str) -> str:
    """Remove whitespace and convert to uppercase."""
    return ''.join(sequence.split()).upper()


def detect_sequence_type(sequence: str) -> str:
    """
    Auto-detect if sequence is DNA, RNA, or Protein.

    Args:
        sequence: Input sequence string

    Returns:
        'dna', 'rna', or 'protein'
    """
    seq = clean_sequence(sequence)
    if not seq:
        return 'unknown'

    seq_set = set(seq)

    # Check for protein-specific amino acids (not in nucleotides)
    protein_only = {'D', 'E', 'F', 'H', 'I', 'K', 'L', 'M', 'P', 'Q', 'R', 'S', 'V', 'W', 'Y'}
    if seq_set & protein_only:
        return 'protein'

    # Check for U (RNA marker) - if U present and no T, it's RNA
    if 'U' in seq_set and 'T' not in seq_set:
        return 'rna'

    # Check for T (DNA marker) - if T present and no U, it's DNA
    if 'T' in seq_set and 'U' not in seq_set:
        return 'dna'

    # If only A, C, G - ambiguous, default to DNA
    if seq_set <= {'A', 'C', 'G', 'N'}:
        return 'dna'

    return 'dna'


def validate_sequence(sequence: str, seq_type: str) -> Tuple[bool, str]:
    """
    Validate sequence against type rules.

    Args:
        sequence: Input sequence
        seq_type: 'dna', 'rna', or 'protein'

    Returns:
        Tuple of (is_valid, error_message)
    """
    seq = clean_sequence(sequence)
    if not seq:
        return False, "Sequence is empty"

    seq_set = set(seq)

    if seq_type == 'dna':
        invalid = seq_set - VALID_DNA
        if invalid:
            return False, f"Invalid DNA characters: {', '.join(sorted(invalid))}"
    elif seq_type == 'rna':
        invalid = seq_set - VALID_RNA
        if invalid:
            return False, f"Invalid RNA characters: {', '.join(sorted(invalid))}"
    elif seq_type == 'protein':
        invalid = seq_set - VALID_PROTEIN
        if invalid:
            return False, f"Invalid protein characters: {', '.join(sorted(invalid))}"
    else:
        return False, f"Unknown sequence type: {seq_type}"

    return True, ""


def transcribe(dna_sequence: str) -> str:
    """
    Transcribe DNA to RNA (T -> U).

    Args:
        dna_sequence: Input DNA sequence

    Returns:
        RNA sequence
    """
    seq = clean_sequence(dna_sequence)
    return seq.replace('T', 'U')


def back_transcribe(rna_sequence: str) -> str:
    """
    Back-transcribe RNA to DNA (U -> T).

    Args:
        rna_sequence: Input RNA sequence

    Returns:
        DNA sequence
    """
    seq = clean_sequence(rna_sequence)
    return seq.replace('U', 'T')


def reverse_complement(sequence: str, seq_type: str = 'dna') -> str:
    """
    Get reverse complement of DNA or RNA sequence.

    Args:
        sequence: Input nucleotide sequence
        seq_type: 'dna' or 'rna'

    Returns:
        Reverse complement sequence
    """
    seq = clean_sequence(sequence)
    complement_map = DNA_COMPLEMENT if seq_type == 'dna' else RNA_COMPLEMENT

    # Get complement
    complemented = ''.join(complement_map.get(nt, 'N') for nt in seq)
    # Reverse
    return complemented[::-1]


def translate(sequence: str, reading_frame: int = 1, seq_type: str = 'dna') -> str:
    """
    Translate DNA or RNA sequence to protein.

    Args:
        sequence: Input nucleotide sequence
        reading_frame: 1, 2, or 3
        seq_type: 'dna' or 'rna'

    Returns:
        Protein sequence (amino acids)
    """
    seq = clean_sequence(sequence)

    # Convert RNA to DNA if needed for codon lookup
    if seq_type == 'rna':
        seq = seq.replace('U', 'T')

    # Adjust for reading frame (1-indexed)
    start = reading_frame - 1
    seq = seq[start:]

    # Translate codons
    protein = []
    for i in range(0, len(seq) - 2, 3):
        codon = seq[i:i+3]
        if 'N' in codon:
            protein.append('X')  # Unknown amino acid
        else:
            aa = CODON_TABLE.get(codon, 'X')
            protein.append(aa)

    return ''.join(protein)


def calculate_gc_content(sequence: str) -> float:
    """
    Calculate GC content percentage.

    Args:
        sequence: DNA or RNA sequence

    Returns:
        GC content as percentage (0-100)
    """
    seq = clean_sequence(sequence)
    if not seq:
        return 0.0

    gc_count = seq.count('G') + seq.count('C')
    return (gc_count / len(seq)) * 100


def calculate_nucleotide_composition(sequence: str) -> Dict[str, Dict[str, Any]]:
    """
    Calculate nucleotide composition.

    Args:
        sequence: DNA or RNA sequence

    Returns:
        Dictionary with count and percentage for each nucleotide
    """
    seq = clean_sequence(sequence)
    total = len(seq) if seq else 1

    composition = {}
    for nt in ['A', 'T', 'U', 'C', 'G', 'N']:
        count = seq.count(nt)
        if count > 0 or nt in ['A', 'C', 'G']:  # Always include A, C, G
            composition[nt] = {
                'count': count,
                'percentage': round((count / total) * 100, 2)
            }

    return composition


def calculate_amino_acid_composition(sequence: str) -> Dict[str, Dict[str, Any]]:
    """
    Calculate amino acid composition.

    Args:
        sequence: Protein sequence

    Returns:
        Dictionary with count and percentage for each amino acid
    """
    seq = clean_sequence(sequence)
    seq = seq.replace('*', '')  # Remove stop codons
    total = len(seq) if seq else 1

    composition = {}
    for aa in 'ACDEFGHIKLMNPQRSTVWY':
        count = seq.count(aa)
        composition[aa] = {
            'count': count,
            'percentage': round((count / total) * 100, 2)
        }

    return composition


def calculate_molecular_weight(sequence: str, seq_type: str = 'protein') -> float:
    """
    Calculate molecular weight.

    Args:
        sequence: Protein or nucleotide sequence
        seq_type: 'protein', 'dna', or 'rna'

    Returns:
        Molecular weight in Daltons
    """
    seq = clean_sequence(sequence)
    if not seq:
        return 0.0

    if seq_type == 'protein':
        # Calculate protein MW
        mw = sum(AA_MW.get(aa, 110.0) for aa in seq)
        # Subtract water for peptide bonds
        mw -= (len(seq) - 1) * 18.015
        return round(mw, 2)
    else:
        # DNA/RNA MW using BioPython if available
        if BIOPYTHON_AVAILABLE:
            try:
                return round(molecular_weight(seq, seq_type=seq_type.upper()), 2)
            except Exception:
                pass

        # Fallback: approximate calculation
        avg_nt_mw = 330 if seq_type == 'dna' else 340
        return round(len(seq) * avg_nt_mw, 2)


def calculate_isoelectric_point(sequence: str) -> float:
    """
    Calculate isoelectric point (pI) of a protein.

    Args:
        sequence: Protein sequence

    Returns:
        Isoelectric point (pH)
    """
    seq = clean_sequence(sequence)
    seq = seq.replace('*', '')

    if not seq:
        return 7.0

    if BIOPYTHON_AVAILABLE:
        try:
            analysis = ProteinAnalysis(seq)
            return round(analysis.isoelectric_point(), 2)
        except Exception:
            pass

    # Fallback: simple calculation
    # Count charged residues
    pos_charge = seq.count('K') + seq.count('R') + seq.count('H')
    neg_charge = seq.count('D') + seq.count('E')

    # Estimate pI based on charge balance
    if pos_charge > neg_charge:
        return round(7.0 + (pos_charge - neg_charge) * 0.5, 2)
    elif neg_charge > pos_charge:
        return round(7.0 - (neg_charge - pos_charge) * 0.5, 2)
    return 7.0


def calculate_hydropathy_profile(sequence: str, window_size: int = 9) -> List[Dict[str, Any]]:
    """
    Calculate Kyte-Doolittle hydropathy profile.

    Args:
        sequence: Protein sequence
        window_size: Sliding window size (default 9)

    Returns:
        List of {position, value} dicts
    """
    seq = clean_sequence(sequence)
    seq = seq.replace('*', '')

    if len(seq) < window_size:
        return []

    profile = []
    half_window = window_size // 2

    for i in range(half_window, len(seq) - half_window):
        window = seq[i - half_window:i + half_window + 1]
        avg_hydropathy = sum(HYDROPATHY.get(aa, 0) for aa in window) / window_size
        profile.append({
            'position': i + 1,
            'value': round(avg_hydropathy, 3)
        })

    return profile


def analyze_codon_usage(sequence: str) -> Dict[str, Any]:
    """
    Analyze codon usage frequencies.

    Args:
        sequence: DNA sequence

    Returns:
        Dictionary with codon frequencies and amino acid groupings
    """
    seq = clean_sequence(sequence)
    seq = seq.replace('U', 'T')  # Convert RNA to DNA

    # Count codons
    codon_counts = {}
    total_codons = 0

    for i in range(0, len(seq) - 2, 3):
        codon = seq[i:i+3]
        if 'N' not in codon and codon in CODON_TABLE:
            codon_counts[codon] = codon_counts.get(codon, 0) + 1
            total_codons += 1

    # Calculate frequencies and RSCU
    result = {
        'total_codons': total_codons,
        'codons': {}
    }

    # Group codons by amino acid for RSCU calculation
    aa_codon_counts = {}
    for codon, aa in CODON_TABLE.items():
        if aa not in aa_codon_counts:
            aa_codon_counts[aa] = []
        aa_codon_counts[aa].append(codon)

    for codon, count in codon_counts.items():
        aa = CODON_TABLE[codon]
        synonymous_codons = aa_codon_counts[aa]
        total_synonymous = sum(codon_counts.get(c, 0) for c in synonymous_codons)
        expected = total_synonymous / len(synonymous_codons) if total_synonymous > 0 else 0

        rscu = count / expected if expected > 0 else 0

        result['codons'][codon] = {
            'count': count,
            'frequency': round(count / total_codons * 100, 2) if total_codons > 0 else 0,
            'amino_acid': aa,
            'rscu': round(rscu, 3)
        }

    return result


def find_orfs(sequence: str, min_length: int = 100, include_nested: bool = False) -> List[Dict[str, Any]]:
    """
    Find Open Reading Frames (ORFs) in a DNA sequence.

    Args:
        sequence: DNA sequence
        min_length: Minimum ORF length in base pairs
        include_nested: Include nested ORFs

    Returns:
        List of ORF dictionaries with start, end, length, frame, strand, sequence
    """
    seq = clean_sequence(sequence)
    seq = seq.replace('U', 'T')  # Convert RNA to DNA

    orfs = []
    stop_codons = {'TAA', 'TAG', 'TGA'}

    def find_orfs_in_frame(seq: str, frame: int, strand: str, offset: int = 0) -> List[Dict]:
        """Find ORFs in a specific reading frame."""
        frame_orfs = []
        start_positions = []

        for i in range(frame, len(seq) - 2, 3):
            codon = seq[i:i+3]

            if codon == 'ATG':
                start_positions.append(i)
            elif codon in stop_codons:
                for start in start_positions:
                    orf_length = i + 3 - start
                    if orf_length >= min_length:
                        if strand == '+':
                            actual_start = start + offset + 1
                            actual_end = i + 3 + offset
                        else:
                            actual_start = len(seq) - (i + 3) + offset + 1
                            actual_end = len(seq) - start + offset

                        frame_orfs.append({
                            'start': actual_start,
                            'end': actual_end,
                            'length': orf_length,
                            'frame': frame + 1 if strand == '+' else -(frame + 1),
                            'strand': strand,
                            'sequence': seq[start:i+3],
                            'protein': translate(seq[start:i+3])
                        })

                        if not include_nested:
                            break

                start_positions = []

        return frame_orfs

    # Forward strand (frames +1, +2, +3)
    for frame in range(3):
        orfs.extend(find_orfs_in_frame(seq, frame, '+'))

    # Reverse complement (frames -1, -2, -3)
    rev_comp = reverse_complement(seq, 'dna')
    for frame in range(3):
        orfs.extend(find_orfs_in_frame(rev_comp, frame, '-'))

    # Sort by start position
    orfs.sort(key=lambda x: (x['start'], -x['length']))

    return orfs


def calculate_protein_properties(sequence: str) -> Dict[str, Any]:
    """
    Calculate comprehensive protein properties.

    Args:
        sequence: Protein sequence

    Returns:
        Dictionary with MW, pI, composition, charge, etc.
    """
    seq = clean_sequence(sequence)
    seq = seq.replace('*', '')

    if not seq:
        return {'error': 'Empty sequence'}

    result = {
        'length': len(seq),
        'molecular_weight': calculate_molecular_weight(seq, 'protein'),
        'isoelectric_point': calculate_isoelectric_point(seq),
        'composition': calculate_amino_acid_composition(seq),
    }

    # Calculate charge at pH 7
    pos_charge = seq.count('K') + seq.count('R') + 0.1 * seq.count('H')
    neg_charge = seq.count('D') + seq.count('E')
    result['net_charge_ph7'] = round(pos_charge - neg_charge, 1)

    # Property categories
    hydrophobic = sum(seq.count(aa) for aa in 'AILMFVWY')
    polar = sum(seq.count(aa) for aa in 'STNQ')
    charged = sum(seq.count(aa) for aa in 'DEKRH')
    special = sum(seq.count(aa) for aa in 'CGP')

    total = len(seq)
    result['property_percentages'] = {
        'hydrophobic': round(hydrophobic / total * 100, 1) if total > 0 else 0,
        'polar': round(polar / total * 100, 1) if total > 0 else 0,
        'charged': round(charged / total * 100, 1) if total > 0 else 0,
        'special': round(special / total * 100, 1) if total > 0 else 0,
    }

    # Extinction coefficient estimate (280nm)
    # Using molar extinction coefficients
    ext_coef = seq.count('W') * 5500 + seq.count('Y') * 1490 + seq.count('C') * 125
    result['extinction_coefficient'] = ext_coef

    # Instability index (simplified)
    if BIOPYTHON_AVAILABLE:
        try:
            analysis = ProteinAnalysis(seq)
            result['instability_index'] = round(analysis.instability_index(), 2)
            result['gravy'] = round(analysis.gravy(), 3)  # Grand average of hydropathy
        except Exception:
            pass

    return result


def get_sequence_statistics(sequence: str, seq_type: str) -> Dict[str, Any]:
    """
    Get comprehensive statistics for a sequence.

    Args:
        sequence: Input sequence
        seq_type: 'dna', 'rna', or 'protein'

    Returns:
        Dictionary with all relevant statistics
    """
    seq = clean_sequence(sequence)

    result = {
        'length': len(seq),
        'type': seq_type
    }

    if seq_type in ('dna', 'rna'):
        result['gc_content'] = round(calculate_gc_content(seq), 2)
        result['at_content'] = round(100 - result['gc_content'], 2)
        result['composition'] = calculate_nucleotide_composition(seq)
        result['molecular_weight'] = calculate_molecular_weight(seq, seq_type)
    else:
        result['composition'] = calculate_amino_acid_composition(seq)
        result['molecular_weight'] = calculate_molecular_weight(seq, 'protein')
        result['isoelectric_point'] = calculate_isoelectric_point(seq)

    return result


# =============================================================================
# PRIMER DESIGN FUNCTIONS
# =============================================================================

# Nearest-neighbor thermodynamic parameters (SantaLucia 1998)
# Format: {dinucleotide: (deltaH kcal/mol, deltaS cal/mol·K)}
NN_PARAMS = {
    'AA': (-7.9, -22.2), 'TT': (-7.9, -22.2),
    'AT': (-7.2, -20.4),
    'TA': (-7.2, -21.3),
    'CA': (-8.5, -22.7), 'TG': (-8.5, -22.7),
    'GT': (-8.4, -22.4), 'AC': (-8.4, -22.4),
    'CT': (-7.8, -21.0), 'AG': (-7.8, -21.0),
    'GA': (-8.2, -22.2), 'TC': (-8.2, -22.2),
    'CG': (-10.6, -27.2),
    'GC': (-9.8, -24.4),
    'GG': (-8.0, -19.9), 'CC': (-8.0, -19.9),
}

# Initiation parameters
NN_INIT = {
    'A': (2.3, 4.1), 'T': (2.3, 4.1),
    'G': (0.1, -2.8), 'C': (0.1, -2.8),
}


def calculate_tm_nearest_neighbor(primer: str, na_conc: float = 50.0, mg_conc: float = 1.5) -> float:
    """
    Calculate melting temperature using nearest-neighbor thermodynamics.

    Uses SantaLucia 1998 unified parameters for more accurate Tm prediction.

    Args:
        primer: Primer sequence (DNA)
        na_conc: Sodium concentration in mM (default 50)
        mg_conc: Magnesium concentration in mM (default 1.5)

    Returns:
        Melting temperature in Celsius
    """
    primer = primer.upper().replace('U', 'T')
    if len(primer) < 2:
        return 0.0

    # Calculate enthalpy and entropy
    delta_h = 0.0  # kcal/mol
    delta_s = 0.0  # cal/mol·K

    # Initiation parameters based on terminal bases
    first_base = primer[0]
    last_base = primer[-1]
    if first_base in NN_INIT:
        delta_h += NN_INIT[first_base][0]
        delta_s += NN_INIT[first_base][1]
    if last_base in NN_INIT:
        delta_h += NN_INIT[last_base][0]
        delta_s += NN_INIT[last_base][1]

    # Sum up dinucleotide contributions
    for i in range(len(primer) - 1):
        dinuc = primer[i:i+2]
        if dinuc in NN_PARAMS:
            delta_h += NN_PARAMS[dinuc][0]
            delta_s += NN_PARAMS[dinuc][1]

    # Salt correction (Owczarzy et al. 2008)
    # Simplified model using Na+ concentration
    na_m = na_conc / 1000.0  # Convert to M
    if na_m > 0:
        delta_s += 0.368 * len(primer) * 4.184 * (na_m ** 0.5)  # Approximate salt correction

    # Calculate Tm
    # Tm = deltaH / (deltaS + R * ln(Ct/4))
    # For primer concentration Ct = 0.25 µM (typical PCR)
    R = 1.987  # Gas constant cal/mol·K
    Ct = 0.25e-6  # Primer concentration in M

    if delta_s != 0:
        tm_k = (delta_h * 1000) / (delta_s + R * (4.184) * abs(delta_s / (delta_h * 1000 + 0.001)))
        tm_k = (delta_h * 1000) / (delta_s + R * 4.184)
        tm_c = tm_k - 273.15
    else:
        # Fallback to basic calculation
        gc = sum(1 for b in primer if b in 'GC')
        at = sum(1 for b in primer if b in 'AT')
        tm_c = 4 * gc + 2 * at

    return round(max(0, min(100, tm_c)), 1)


def calculate_tm_basic(primer: str) -> float:
    """
    Calculate melting temperature using basic Wallace rule.

    Tm = 4(G+C) + 2(A+T)

    Only accurate for primers < 14bp. Use nearest-neighbor for longer primers.

    Args:
        primer: Primer sequence

    Returns:
        Melting temperature in Celsius
    """
    primer = primer.upper().replace('U', 'T')
    gc = sum(1 for b in primer if b in 'GC')
    at = sum(1 for b in primer if b in 'AT')
    return 4 * gc + 2 * at


def check_self_complementarity(primer: str) -> Dict[str, Any]:
    """
    Check for self-complementary regions (hairpin and self-dimer formation).

    Args:
        primer: Primer sequence

    Returns:
        Dictionary with hairpin_score, self_dimer_score, and details
    """
    primer = primer.upper().replace('U', 'T')
    complement = {'A': 'T', 'T': 'A', 'G': 'C', 'C': 'G', 'N': 'N'}

    hairpin_score = 0
    self_dimer_score = 0
    hairpin_positions = []

    # Check for hairpin (3' end folding back on itself)
    # Look for 4+ bp complementary regions
    for loop_size in range(3, 8):
        for stem_len in range(4, len(primer) // 2):
            if 2 * stem_len + loop_size > len(primer):
                break

            for start in range(len(primer) - 2 * stem_len - loop_size + 1):
                stem1 = primer[start:start + stem_len]
                stem2_start = start + stem_len + loop_size
                stem2 = primer[stem2_start:stem2_start + stem_len]

                # Check if stems are complementary
                matches = sum(1 for a, b in zip(stem1, stem2[::-1])
                             if complement.get(a) == b)

                if matches >= stem_len - 1:  # Allow 1 mismatch
                    hairpin_score = max(hairpin_score, matches * 2)
                    hairpin_positions.append({
                        'start1': start,
                        'end1': start + stem_len,
                        'start2': stem2_start,
                        'end2': stem2_start + stem_len,
                        'loop_size': loop_size,
                        'matches': matches
                    })

    # Check for self-dimer (primer annealing to itself in anti-parallel)
    rc = ''.join(complement.get(b, 'N') for b in reversed(primer))

    # Slide one primer along the other
    for offset in range(1 - len(primer), len(primer)):
        matches = 0
        for i in range(len(primer)):
            j = i + offset
            if 0 <= j < len(primer):
                if primer[i] == rc[j]:
                    matches += 1

        if matches >= 4:
            self_dimer_score = max(self_dimer_score, matches)

    return {
        'hairpin_score': hairpin_score,
        'self_dimer_score': self_dimer_score,
        'has_hairpin': hairpin_score >= 8,
        'has_self_dimer': self_dimer_score >= 6,
        'hairpin_positions': hairpin_positions[:3],  # Top 3
        'quality': 'good' if hairpin_score < 6 and self_dimer_score < 4 else
                   'acceptable' if hairpin_score < 10 and self_dimer_score < 6 else 'poor'
    }


def check_primer_dimer(forward: str, reverse: str) -> Dict[str, Any]:
    """
    Check for primer dimer formation between forward and reverse primers.

    Args:
        forward: Forward primer sequence
        reverse: Reverse primer sequence

    Returns:
        Dictionary with dimer_score and details
    """
    forward = forward.upper().replace('U', 'T')
    reverse = reverse.upper().replace('U', 'T')
    complement = {'A': 'T', 'T': 'A', 'G': 'C', 'C': 'G', 'N': 'N'}

    # Convert reverse primer to its reverse complement for alignment
    rc_reverse = ''.join(complement.get(b, 'N') for b in reversed(reverse))

    max_score = 0
    best_alignment = None

    # Slide primers against each other
    for offset in range(1 - len(forward), len(rc_reverse)):
        score = 0
        alignment_fwd = ''
        alignment_match = ''
        alignment_rev = ''

        for i in range(len(forward)):
            j = i + offset
            if 0 <= j < len(rc_reverse):
                if forward[i] == rc_reverse[j]:
                    score += 2
                    alignment_fwd += forward[i]
                    alignment_match += '|'
                    alignment_rev += rc_reverse[j]
                elif complement.get(forward[i]) == rc_reverse[j]:
                    score += 1
                    alignment_fwd += forward[i]
                    alignment_match += ':'
                    alignment_rev += rc_reverse[j]
                else:
                    alignment_fwd += forward[i]
                    alignment_match += ' '
                    alignment_rev += rc_reverse[j]

        if score > max_score:
            max_score = score
            best_alignment = {
                'forward': alignment_fwd,
                'match': alignment_match,
                'reverse': alignment_rev,
                'offset': offset
            }

    # Check 3' end complementarity specifically (most problematic for PCR)
    three_prime_score = 0
    for length in range(3, min(8, len(forward), len(reverse)) + 1):
        fwd_3prime = forward[-length:]
        rev_3prime = reverse[-length:]
        rev_3prime_rc = ''.join(complement.get(b, 'N') for b in reversed(rev_3prime))

        matches = sum(1 for a, b in zip(fwd_3prime, rev_3prime_rc) if a == b)
        if matches == length:
            three_prime_score = max(three_prime_score, length)

    return {
        'dimer_score': max_score,
        'three_prime_score': three_prime_score,
        'has_dimer': max_score >= 8,
        'has_3prime_dimer': three_prime_score >= 4,
        'alignment': best_alignment,
        'quality': 'good' if max_score < 6 and three_prime_score < 3 else
                   'acceptable' if max_score < 10 and three_prime_score < 4 else 'poor'
    }


def design_primers(
    sequence: str,
    target_start: int = None,
    target_end: int = None,
    min_length: int = 18,
    max_length: int = 25,
    optimal_length: int = 20,
    min_tm: float = 55.0,
    max_tm: float = 65.0,
    optimal_tm: float = 60.0,
    min_gc: float = 40.0,
    max_gc: float = 60.0,
    max_results: int = 5
) -> List[Dict[str, Any]]:
    """
    Design primer pairs for a given sequence.

    Args:
        sequence: Template DNA sequence
        target_start: Start of amplification target (optional)
        target_end: End of amplification target (optional)
        min_length: Minimum primer length
        max_length: Maximum primer length
        optimal_length: Optimal primer length
        min_tm: Minimum melting temperature
        max_tm: Maximum melting temperature
        optimal_tm: Optimal melting temperature
        min_gc: Minimum GC content (%)
        max_gc: Maximum GC content (%)
        max_results: Maximum number of primer pairs to return

    Returns:
        List of primer pair dictionaries with quality scores
    """
    sequence = sequence.upper().replace('U', 'T')
    complement = {'A': 'T', 'T': 'A', 'G': 'C', 'C': 'G', 'N': 'N'}

    if len(sequence) < min_length * 2 + 50:
        return []

    # Default target region
    if target_start is None:
        target_start = len(sequence) // 4
    if target_end is None:
        target_end = 3 * len(sequence) // 4

    # Ensure valid target region
    target_start = max(0, min(target_start, len(sequence) - 100))
    target_end = min(len(sequence), max(target_end, target_start + 100))

    # Generate candidate forward primers (before target)
    forward_candidates = []
    search_start = max(0, target_start - 200)
    search_end = target_start

    for start in range(search_start, search_end):
        for length in range(min_length, min(max_length + 1, len(sequence) - start)):
            primer = sequence[start:start + length]

            # Quick filters
            gc = 100 * sum(1 for b in primer if b in 'GC') / len(primer)
            if gc < min_gc or gc > max_gc:
                continue

            # Check for runs of same base (avoid GGGG, TTTT, etc.)
            if any(primer.count(b * 4) for b in 'ATGC'):
                continue

            # Prefer primers ending in G or C (GC clamp)
            gc_clamp = 1 if primer[-1] in 'GC' else 0
            if primer[-2:].count('G') + primer[-2:].count('C') >= 2:
                gc_clamp = 2

            tm = calculate_tm_nearest_neighbor(primer)
            if tm < min_tm or tm > max_tm:
                continue

            # Score based on optimality
            length_score = 10 - abs(length - optimal_length)
            tm_score = 10 - abs(tm - optimal_tm)
            gc_score = 10 - abs(gc - 50) / 5

            self_comp = check_self_complementarity(primer)
            comp_penalty = self_comp['hairpin_score'] / 2 + self_comp['self_dimer_score'] / 2

            total_score = length_score + tm_score + gc_score + gc_clamp * 2 - comp_penalty

            forward_candidates.append({
                'sequence': primer,
                'start': start,
                'end': start + length,
                'length': length,
                'tm': tm,
                'gc': round(gc, 1),
                'gc_clamp': gc_clamp,
                'self_comp': self_comp,
                'score': total_score
            })

    # Sort by score
    forward_candidates.sort(key=lambda x: x['score'], reverse=True)
    forward_candidates = forward_candidates[:20]  # Keep top 20

    # Generate candidate reverse primers (after target)
    reverse_candidates = []
    search_start = target_end
    search_end = min(len(sequence), target_end + 200)

    for end in range(search_start + min_length, search_end):
        for length in range(min_length, min(max_length + 1, end)):
            start = end - length
            # Get reverse complement
            primer_template = sequence[start:end]
            primer = ''.join(complement.get(b, 'N') for b in reversed(primer_template))

            # Quick filters
            gc = 100 * sum(1 for b in primer if b in 'GC') / len(primer)
            if gc < min_gc or gc > max_gc:
                continue

            if any(primer.count(b * 4) for b in 'ATGC'):
                continue

            gc_clamp = 1 if primer[-1] in 'GC' else 0
            if primer[-2:].count('G') + primer[-2:].count('C') >= 2:
                gc_clamp = 2

            tm = calculate_tm_nearest_neighbor(primer)
            if tm < min_tm or tm > max_tm:
                continue

            length_score = 10 - abs(length - optimal_length)
            tm_score = 10 - abs(tm - optimal_tm)
            gc_score = 10 - abs(gc - 50) / 5

            self_comp = check_self_complementarity(primer)
            comp_penalty = self_comp['hairpin_score'] / 2 + self_comp['self_dimer_score'] / 2

            total_score = length_score + tm_score + gc_score + gc_clamp * 2 - comp_penalty

            reverse_candidates.append({
                'sequence': primer,
                'start': start,  # Position on template (5' end of primer)
                'end': end,      # Position on template (3' end of primer)
                'length': length,
                'tm': tm,
                'gc': round(gc, 1),
                'gc_clamp': gc_clamp,
                'self_comp': self_comp,
                'score': total_score
            })

    reverse_candidates.sort(key=lambda x: x['score'], reverse=True)
    reverse_candidates = reverse_candidates[:20]

    # Find best pairs
    pairs = []
    for fwd in forward_candidates[:10]:
        for rev in reverse_candidates[:10]:
            # Check product size
            product_size = rev['end'] - fwd['start']
            if product_size < 100 or product_size > 1000:
                continue

            # Check Tm difference
            tm_diff = abs(fwd['tm'] - rev['tm'])
            if tm_diff > 5:
                continue

            # Check primer dimer
            dimer = check_primer_dimer(fwd['sequence'], rev['sequence'])

            # Calculate pair score
            pair_score = (fwd['score'] + rev['score']) / 2
            pair_score -= tm_diff * 2  # Penalize Tm difference
            pair_score -= dimer['dimer_score'] / 2
            pair_score -= dimer['three_prime_score'] * 2

            # Prefer ~200-500bp products
            size_penalty = 0
            if product_size < 150 or product_size > 700:
                size_penalty = abs(product_size - 350) / 100
            pair_score -= size_penalty

            pairs.append({
                'forward': fwd,
                'reverse': rev,
                'product_size': product_size,
                'tm_difference': round(tm_diff, 1),
                'dimer_check': dimer,
                'pair_score': round(pair_score, 2),
                'quality': 'excellent' if pair_score > 15 else
                          'good' if pair_score > 10 else
                          'acceptable' if pair_score > 5 else 'poor'
            })

    # Sort by pair score
    pairs.sort(key=lambda x: x['pair_score'], reverse=True)

    return pairs[:max_results]


def analyze_primer(primer: str) -> Dict[str, Any]:
    """
    Comprehensive analysis of a single primer.

    Args:
        primer: Primer sequence

    Returns:
        Dictionary with all primer properties
    """
    primer = primer.upper().replace('U', 'T')

    gc = 100 * sum(1 for b in primer if b in 'GC') / len(primer) if primer else 0
    tm_basic = calculate_tm_basic(primer)
    tm_nn = calculate_tm_nearest_neighbor(primer)
    self_comp = check_self_complementarity(primer)

    # Check for problematic features
    issues = []
    if gc < 40:
        issues.append('Low GC content (<40%)')
    if gc > 60:
        issues.append('High GC content (>60%)')
    if tm_nn < 55:
        issues.append('Low melting temperature (<55°C)')
    if tm_nn > 65:
        issues.append('High melting temperature (>65°C)')
    if len(primer) < 18:
        issues.append('Primer too short (<18bp)')
    if len(primer) > 25:
        issues.append('Primer too long (>25bp)')
    if self_comp['has_hairpin']:
        issues.append('Strong hairpin formation potential')
    if self_comp['has_self_dimer']:
        issues.append('Strong self-dimer potential')
    if any(primer.count(b * 4) >= 1 for b in 'ATGC'):
        issues.append('Contains run of 4+ identical bases')

    # GC clamp analysis
    gc_3prime = sum(1 for b in primer[-5:] if b in 'GC')
    gc_clamp = 'good' if 1 <= gc_3prime <= 3 else 'weak' if gc_3prime == 0 else 'strong'

    return {
        'sequence': primer,
        'length': len(primer),
        'gc_content': round(gc, 1),
        'tm_basic': tm_basic,
        'tm_nearest_neighbor': tm_nn,
        'self_complementarity': self_comp,
        'gc_clamp': gc_clamp,
        'gc_3prime': gc_3prime,
        'issues': issues,
        'quality': 'good' if len(issues) == 0 else
                   'acceptable' if len(issues) <= 2 else 'poor'
    }


# =============================================================================
# RESTRICTION ENZYME DATABASE & MOTIF SEARCH
# =============================================================================

# Common restriction enzymes with recognition sequences and cut positions
# Format: {name: {'pattern': recognition_seq, 'cut_top': position, 'cut_bottom': position, 'overhang': type}}
RESTRICTION_ENZYMES = {
    # 6-cutters (most common)
    'EcoRI': {'pattern': 'GAATTC', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'BamHI': {'pattern': 'GGATCC', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'HindIII': {'pattern': 'AAGCTT', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'XhoI': {'pattern': 'CTCGAG', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'SalI': {'pattern': 'GTCGAC', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'PstI': {'pattern': 'CTGCAG', 'cut_top': 5, 'cut_bottom': 1, 'overhang': '3prime'},
    'SphI': {'pattern': 'GCATGC', 'cut_top': 5, 'cut_bottom': 1, 'overhang': '3prime'},
    'XbaI': {'pattern': 'TCTAGA', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'NcoI': {'pattern': 'CCATGG', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'NdeI': {'pattern': 'CATATG', 'cut_top': 2, 'cut_bottom': 4, 'overhang': '5prime'},
    'NheI': {'pattern': 'GCTAGC', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'SacI': {'pattern': 'GAGCTC', 'cut_top': 5, 'cut_bottom': 1, 'overhang': '3prime'},
    'SacII': {'pattern': 'CCGCGG', 'cut_top': 4, 'cut_bottom': 2, 'overhang': '3prime'},
    'KpnI': {'pattern': 'GGTACC', 'cut_top': 5, 'cut_bottom': 1, 'overhang': '3prime'},
    'ApaI': {'pattern': 'GGGCCC', 'cut_top': 5, 'cut_bottom': 1, 'overhang': '3prime'},

    # Blunt cutters
    'SmaI': {'pattern': 'CCCGGG', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},
    'EcoRV': {'pattern': 'GATATC', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},
    'HpaI': {'pattern': 'GTTAAC', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},
    'StuI': {'pattern': 'AGGCCT', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},
    'PvuII': {'pattern': 'CAGCTG', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},
    'HincII': {'pattern': 'GTYRAC', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},

    # 8-cutters (rare cutters)
    'NotI': {'pattern': 'GCGGCCGC', 'cut_top': 2, 'cut_bottom': 6, 'overhang': '5prime'},
    'SfiI': {'pattern': 'GGCCNNNNNGGCC', 'cut_top': 8, 'cut_bottom': 5, 'overhang': '3prime'},
    'PacI': {'pattern': 'TTAATTAA', 'cut_top': 5, 'cut_bottom': 3, 'overhang': '3prime'},
    'SwaI': {'pattern': 'ATTTAAAT', 'cut_top': 4, 'cut_bottom': 4, 'overhang': 'blunt'},
    'AscI': {'pattern': 'GGCGCGCC', 'cut_top': 2, 'cut_bottom': 6, 'overhang': '5prime'},
    'FseI': {'pattern': 'GGCCGGCC', 'cut_top': 6, 'cut_bottom': 2, 'overhang': '3prime'},

    # 4-cutters (frequent cutters)
    'MspI': {'pattern': 'CCGG', 'cut_top': 1, 'cut_bottom': 3, 'overhang': '5prime'},
    'HaeIII': {'pattern': 'GGCC', 'cut_top': 2, 'cut_bottom': 2, 'overhang': 'blunt'},
    'AluI': {'pattern': 'AGCT', 'cut_top': 2, 'cut_bottom': 2, 'overhang': 'blunt'},
    'RsaI': {'pattern': 'GTAC', 'cut_top': 2, 'cut_bottom': 2, 'overhang': 'blunt'},
    'TaqI': {'pattern': 'TCGA', 'cut_top': 1, 'cut_bottom': 3, 'overhang': '5prime'},
    'DpnI': {'pattern': 'GATC', 'cut_top': 2, 'cut_bottom': 2, 'overhang': 'blunt'},
    'Sau3AI': {'pattern': 'GATC', 'cut_top': 0, 'cut_bottom': 4, 'overhang': '5prime'},
    'MboI': {'pattern': 'GATC', 'cut_top': 0, 'cut_bottom': 4, 'overhang': '5prime'},
    'HpaII': {'pattern': 'CCGG', 'cut_top': 1, 'cut_bottom': 3, 'overhang': '5prime'},
    'ClaI': {'pattern': 'ATCGAT', 'cut_top': 2, 'cut_bottom': 4, 'overhang': '5prime'},

    # Additional common enzymes
    'BglII': {'pattern': 'AGATCT', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'SpeI': {'pattern': 'ACTAGT', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'AgeI': {'pattern': 'ACCGGT', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'BsrGI': {'pattern': 'TGTACA', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'MluI': {'pattern': 'ACGCGT', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'AvrII': {'pattern': 'CCTAGG', 'cut_top': 1, 'cut_bottom': 5, 'overhang': '5prime'},
    'SnaBI': {'pattern': 'TACGTA', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},
    'ScaI': {'pattern': 'AGTACT', 'cut_top': 3, 'cut_bottom': 3, 'overhang': 'blunt'},
    'PmeI': {'pattern': 'GTTTAAAC', 'cut_top': 4, 'cut_bottom': 4, 'overhang': 'blunt'},
    'SgrAI': {'pattern': 'CRCCGGYG', 'cut_top': 2, 'cut_bottom': 6, 'overhang': '5prime'},
}

# Common regulatory elements
REGULATORY_ELEMENTS = {
    'TATA_box': {
        'pattern': 'TATA[AT]A[AT]',
        'description': 'TATA box - core promoter element',
        'type': 'promoter'
    },
    'Kozak_strong': {
        'pattern': '[AG]CCATGG',
        'description': 'Strong Kozak consensus (eukaryotic translation initiation)',
        'type': 'translation'
    },
    'Kozak_weak': {
        'pattern': '[^AG]..ATGG',
        'description': 'Weak Kozak consensus',
        'type': 'translation'
    },
    'Shine_Dalgarno': {
        'pattern': 'AGGAGG',
        'description': 'Shine-Dalgarno sequence (prokaryotic RBS)',
        'type': 'translation'
    },
    'GC_box': {
        'pattern': 'GGGCGG',
        'description': 'GC box - Sp1 binding site',
        'type': 'promoter'
    },
    'CAAT_box': {
        'pattern': 'CCAAT',
        'description': 'CAAT box - enhancer element',
        'type': 'promoter'
    },
    'polyadenylation': {
        'pattern': 'AATAAA',
        'description': 'Polyadenylation signal',
        'type': '3utr'
    },
    'CpG_island': {
        'pattern': 'CG',
        'description': 'CpG dinucleotide (methylation site)',
        'type': 'methylation'
    },
    'E_box': {
        'pattern': 'CANNTG',
        'description': 'E-box motif (bHLH binding)',
        'type': 'enhancer'
    },
    'NF_kB': {
        'pattern': 'GGGACTTTCC',
        'description': 'NF-κB binding site',
        'type': 'enhancer'
    },
    'AP1': {
        'pattern': 'TGA[CG]TCA',
        'description': 'AP-1 binding site',
        'type': 'enhancer'
    },
    'start_codon': {
        'pattern': 'ATG',
        'description': 'Start codon (Methionine)',
        'type': 'coding'
    },
    'stop_codons': {
        'pattern': 'T[AG]A|TGA',
        'description': 'Stop codons (TAA, TAG, TGA)',
        'type': 'coding'
    },
}

# IUPAC ambiguity codes for pattern matching
IUPAC_CODES = {
    'R': '[AG]',
    'Y': '[CT]',
    'S': '[GC]',
    'W': '[AT]',
    'K': '[GT]',
    'M': '[AC]',
    'B': '[CGT]',
    'D': '[AGT]',
    'H': '[ACT]',
    'V': '[ACG]',
    'N': '[ACGT]',
}


def expand_iupac_pattern(pattern: str) -> str:
    """
    Convert IUPAC ambiguity codes in a pattern to regex character classes.

    Args:
        pattern: Pattern with IUPAC codes

    Returns:
        Regex-compatible pattern
    """
    result = pattern.upper()
    for code, replacement in IUPAC_CODES.items():
        result = result.replace(code, replacement)
    return result


def find_restriction_sites(sequence: str, enzymes: List[str] = None) -> List[Dict[str, Any]]:
    """
    Find restriction enzyme cut sites in a DNA sequence.

    Args:
        sequence: DNA sequence to search
        enzymes: List of enzyme names to search for (default: all common enzymes)

    Returns:
        List of dictionaries with site information
    """
    sequence = sequence.upper().replace('U', 'T')

    if enzymes is None:
        enzymes = list(RESTRICTION_ENZYMES.keys())

    sites = []

    for enzyme_name in enzymes:
        if enzyme_name not in RESTRICTION_ENZYMES:
            continue

        enzyme = RESTRICTION_ENZYMES[enzyme_name]
        pattern = expand_iupac_pattern(enzyme['pattern'])

        try:
            regex = re.compile(pattern)
            for match in regex.finditer(sequence):
                sites.append({
                    'enzyme': enzyme_name,
                    'position': match.start(),
                    'end': match.end(),
                    'sequence': match.group(),
                    'recognition': enzyme['pattern'],
                    'cut_top': match.start() + enzyme['cut_top'],
                    'cut_bottom': match.start() + enzyme['cut_bottom'],
                    'overhang': enzyme['overhang'],
                    'strand': '+'
                })

            # Also search reverse complement
            rc = reverse_complement(sequence)
            for match in regex.finditer(rc):
                # Convert position to forward strand coordinates
                rc_start = len(sequence) - match.end()
                sites.append({
                    'enzyme': enzyme_name,
                    'position': rc_start,
                    'end': rc_start + len(match.group()),
                    'sequence': match.group(),
                    'recognition': enzyme['pattern'],
                    'cut_top': rc_start + enzyme['cut_top'],
                    'cut_bottom': rc_start + enzyme['cut_bottom'],
                    'overhang': enzyme['overhang'],
                    'strand': '-'
                })

        except re.error:
            continue

    # Sort by position
    sites.sort(key=lambda x: x['position'])

    # Remove duplicates (same position, same enzyme)
    seen = set()
    unique_sites = []
    for site in sites:
        key = (site['enzyme'], site['position'])
        if key not in seen:
            seen.add(key)
            unique_sites.append(site)

    return unique_sites


def find_regulatory_elements(sequence: str, elements: List[str] = None) -> List[Dict[str, Any]]:
    """
    Find regulatory elements and motifs in a sequence.

    Args:
        sequence: Sequence to search
        elements: List of element names to search for (default: all)

    Returns:
        List of dictionaries with element information
    """
    sequence = sequence.upper().replace('U', 'T')

    if elements is None:
        elements = list(REGULATORY_ELEMENTS.keys())

    results = []

    for element_name in elements:
        if element_name not in REGULATORY_ELEMENTS:
            continue

        element = REGULATORY_ELEMENTS[element_name]
        pattern = element['pattern']

        try:
            regex = re.compile(pattern, re.IGNORECASE)
            for match in regex.finditer(sequence):
                results.append({
                    'name': element_name,
                    'position': match.start(),
                    'end': match.end(),
                    'sequence': match.group(),
                    'pattern': element['pattern'],
                    'description': element['description'],
                    'type': element['type'],
                    'strand': '+'
                })

        except re.error:
            continue

    # Sort by position
    results.sort(key=lambda x: x['position'])

    return results


def search_motif(sequence: str, pattern: str, is_regex: bool = False) -> List[Dict[str, Any]]:
    """
    Search for a custom motif or pattern in a sequence.

    Args:
        sequence: Sequence to search
        pattern: Pattern to search for (can include IUPAC codes or be a regex)
        is_regex: If True, treat pattern as a regex; if False, expand IUPAC codes

    Returns:
        List of dictionaries with match information
    """
    sequence = sequence.upper()

    if not is_regex:
        # Expand IUPAC codes
        pattern = expand_iupac_pattern(pattern)

    results = []

    try:
        regex = re.compile(pattern, re.IGNORECASE)
        for match in regex.finditer(sequence):
            results.append({
                'position': match.start(),
                'end': match.end(),
                'sequence': match.group(),
                'pattern': pattern,
                'length': match.end() - match.start()
            })

        # Also search reverse complement for DNA
        if all(c in 'ACGTN' for c in sequence):
            rc = reverse_complement(sequence)
            for match in regex.finditer(rc):
                rc_start = len(sequence) - match.end()
                results.append({
                    'position': rc_start,
                    'end': rc_start + len(match.group()),
                    'sequence': match.group(),
                    'pattern': pattern,
                    'length': match.end() - match.start(),
                    'strand': '-'
                })

    except re.error as e:
        return [{'error': f'Invalid pattern: {str(e)}'}]

    # Sort by position
    results.sort(key=lambda x: x.get('position', 0))

    return results


def get_enzyme_info(enzyme_name: str) -> Optional[Dict[str, Any]]:
    """
    Get information about a specific restriction enzyme.

    Args:
        enzyme_name: Name of the enzyme

    Returns:
        Dictionary with enzyme properties or None if not found
    """
    if enzyme_name in RESTRICTION_ENZYMES:
        enzyme = RESTRICTION_ENZYMES[enzyme_name]
        return {
            'name': enzyme_name,
            'recognition': enzyme['pattern'],
            'cut_top': enzyme['cut_top'],
            'cut_bottom': enzyme['cut_bottom'],
            'overhang': enzyme['overhang'],
            'length': len(enzyme['pattern'].replace('N', ''))
        }
    return None


def list_enzymes(filter_type: str = None) -> List[Dict[str, Any]]:
    """
    List available restriction enzymes.

    Args:
        filter_type: Optional filter by overhang type ('5prime', '3prime', 'blunt')

    Returns:
        List of enzyme information dictionaries
    """
    enzymes = []
    for name, info in RESTRICTION_ENZYMES.items():
        if filter_type is None or info['overhang'] == filter_type:
            enzymes.append({
                'name': name,
                'recognition': info['pattern'],
                'overhang': info['overhang'],
                'length': len(info['pattern'].replace('N', ''))
            })

    # Sort by name
    enzymes.sort(key=lambda x: x['name'])
    return enzymes


# =============================================================================
# SEQUENCE COMPARISON FUNCTIONS
# =============================================================================

# Scoring matrices for sequence alignment
DNA_MATCH_SCORE = 2
DNA_MISMATCH_SCORE = -1
DNA_GAP_OPEN = -5
DNA_GAP_EXTEND = -1

PROTEIN_MATCH_SCORE = 4
PROTEIN_MISMATCH_SCORE = -2
PROTEIN_GAP_OPEN = -10
PROTEIN_GAP_EXTEND = -1

# BLOSUM62 simplified for common amino acids
BLOSUM62 = {
    ('A', 'A'): 4, ('R', 'R'): 5, ('N', 'N'): 6, ('D', 'D'): 6, ('C', 'C'): 9,
    ('Q', 'Q'): 5, ('E', 'E'): 5, ('G', 'G'): 6, ('H', 'H'): 8, ('I', 'I'): 4,
    ('L', 'L'): 4, ('K', 'K'): 5, ('M', 'M'): 5, ('F', 'F'): 6, ('P', 'P'): 7,
    ('S', 'S'): 4, ('T', 'T'): 5, ('W', 'W'): 11, ('Y', 'Y'): 7, ('V', 'V'): 4,
    # Common mismatches
    ('I', 'V'): 3, ('V', 'I'): 3, ('L', 'I'): 2, ('I', 'L'): 2, ('L', 'V'): 1, ('V', 'L'): 1,
    ('F', 'Y'): 3, ('Y', 'F'): 3, ('D', 'E'): 2, ('E', 'D'): 2, ('N', 'Q'): 0, ('Q', 'N'): 0,
    ('S', 'T'): 1, ('T', 'S'): 1, ('K', 'R'): 2, ('R', 'K'): 2,
}


def get_score(a: str, b: str, is_protein: bool = False) -> int:
    """
    Get the alignment score for two characters.
    """
    if a == '-' or b == '-':
        return DNA_GAP_EXTEND if not is_protein else PROTEIN_GAP_EXTEND

    a, b = a.upper(), b.upper()

    if is_protein:
        if (a, b) in BLOSUM62:
            return BLOSUM62[(a, b)]
        elif a == b:
            return PROTEIN_MATCH_SCORE
        else:
            return PROTEIN_MISMATCH_SCORE
    else:
        if a == b:
            return DNA_MATCH_SCORE
        else:
            return DNA_MISMATCH_SCORE


def needleman_wunsch(seq1: str, seq2: str, is_protein: bool = False) -> Tuple[str, str, int]:
    """
    Perform global sequence alignment using Needleman-Wunsch algorithm.

    Args:
        seq1: First sequence
        seq2: Second sequence
        is_protein: Whether sequences are protein (True) or DNA (False)

    Returns:
        Tuple of (aligned_seq1, aligned_seq2, score)
    """
    seq1 = seq1.upper().replace(' ', '').replace('\n', '')
    seq2 = seq2.upper().replace(' ', '').replace('\n', '')

    gap_open = PROTEIN_GAP_OPEN if is_protein else DNA_GAP_OPEN
    gap_extend = PROTEIN_GAP_EXTEND if is_protein else DNA_GAP_EXTEND

    m, n = len(seq1), len(seq2)

    # Initialize scoring matrix
    score_matrix = [[0] * (n + 1) for _ in range(m + 1)]
    traceback = [[0] * (n + 1) for _ in range(m + 1)]

    # Initialize first row and column (gap penalties)
    for i in range(1, m + 1):
        score_matrix[i][0] = gap_open + (i - 1) * gap_extend
        traceback[i][0] = 1  # Up
    for j in range(1, n + 1):
        score_matrix[0][j] = gap_open + (j - 1) * gap_extend
        traceback[0][j] = 2  # Left

    # Fill the matrix
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            match = score_matrix[i-1][j-1] + get_score(seq1[i-1], seq2[j-1], is_protein)
            delete = score_matrix[i-1][j] + (gap_open if traceback[i-1][j] != 1 else gap_extend)
            insert = score_matrix[i][j-1] + (gap_open if traceback[i][j-1] != 2 else gap_extend)

            score_matrix[i][j] = max(match, delete, insert)

            if score_matrix[i][j] == match:
                traceback[i][j] = 0  # Diagonal
            elif score_matrix[i][j] == delete:
                traceback[i][j] = 1  # Up
            else:
                traceback[i][j] = 2  # Left

    # Traceback
    aligned1, aligned2 = [], []
    i, j = m, n

    while i > 0 or j > 0:
        if i > 0 and j > 0 and traceback[i][j] == 0:
            aligned1.append(seq1[i-1])
            aligned2.append(seq2[j-1])
            i -= 1
            j -= 1
        elif i > 0 and traceback[i][j] == 1:
            aligned1.append(seq1[i-1])
            aligned2.append('-')
            i -= 1
        else:
            aligned1.append('-')
            aligned2.append(seq2[j-1])
            j -= 1

    return ''.join(reversed(aligned1)), ''.join(reversed(aligned2)), score_matrix[m][n]


def smith_waterman(seq1: str, seq2: str, is_protein: bool = False) -> Tuple[str, str, int, int, int]:
    """
    Perform local sequence alignment using Smith-Waterman algorithm.

    Args:
        seq1: First sequence
        seq2: Second sequence
        is_protein: Whether sequences are protein (True) or DNA (False)

    Returns:
        Tuple of (aligned_seq1, aligned_seq2, score, start1, start2)
    """
    seq1 = seq1.upper().replace(' ', '').replace('\n', '')
    seq2 = seq2.upper().replace(' ', '').replace('\n', '')

    gap_open = PROTEIN_GAP_OPEN if is_protein else DNA_GAP_OPEN
    gap_extend = PROTEIN_GAP_EXTEND if is_protein else DNA_GAP_EXTEND

    m, n = len(seq1), len(seq2)

    # Initialize scoring matrix
    score_matrix = [[0] * (n + 1) for _ in range(m + 1)]
    traceback = [[0] * (n + 1) for _ in range(m + 1)]

    max_score = 0
    max_pos = (0, 0)

    # Fill the matrix
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            match = score_matrix[i-1][j-1] + get_score(seq1[i-1], seq2[j-1], is_protein)
            delete = score_matrix[i-1][j] + (gap_open if traceback[i-1][j] != 1 else gap_extend)
            insert = score_matrix[i][j-1] + (gap_open if traceback[i][j-1] != 2 else gap_extend)

            score_matrix[i][j] = max(0, match, delete, insert)

            if score_matrix[i][j] == 0:
                traceback[i][j] = -1  # Stop
            elif score_matrix[i][j] == match:
                traceback[i][j] = 0  # Diagonal
            elif score_matrix[i][j] == delete:
                traceback[i][j] = 1  # Up
            else:
                traceback[i][j] = 2  # Left

            if score_matrix[i][j] > max_score:
                max_score = score_matrix[i][j]
                max_pos = (i, j)

    # Traceback from max score position
    aligned1, aligned2 = [], []
    i, j = max_pos

    while i > 0 and j > 0 and traceback[i][j] != -1:
        if traceback[i][j] == 0:
            aligned1.append(seq1[i-1])
            aligned2.append(seq2[j-1])
            i -= 1
            j -= 1
        elif traceback[i][j] == 1:
            aligned1.append(seq1[i-1])
            aligned2.append('-')
            i -= 1
        else:
            aligned1.append('-')
            aligned2.append(seq2[j-1])
            j -= 1

    start1 = i
    start2 = j

    return ''.join(reversed(aligned1)), ''.join(reversed(aligned2)), max_score, start1, start2


def calculate_alignment_stats(aligned1: str, aligned2: str) -> Dict[str, Any]:
    """
    Calculate alignment statistics from aligned sequences.

    Args:
        aligned1: First aligned sequence (with gaps)
        aligned2: Second aligned sequence (with gaps)

    Returns:
        Dictionary with alignment statistics
    """
    if len(aligned1) != len(aligned2):
        raise ValueError("Aligned sequences must have equal length")

    length = len(aligned1)
    matches = 0
    mismatches = 0
    gaps1 = 0
    gaps2 = 0
    gap_opens1 = 0
    gap_opens2 = 0

    prev_gap1 = False
    prev_gap2 = False

    for i in range(length):
        a, b = aligned1[i], aligned2[i]

        if a == '-':
            gaps1 += 1
            if not prev_gap1:
                gap_opens1 += 1
            prev_gap1 = True
        else:
            prev_gap1 = False

        if b == '-':
            gaps2 += 1
            if not prev_gap2:
                gap_opens2 += 1
            prev_gap2 = True
        else:
            prev_gap2 = False

        if a != '-' and b != '-':
            if a.upper() == b.upper():
                matches += 1
            else:
                mismatches += 1

    total_gaps = gaps1 + gaps2
    aligned_positions = length - max(gaps1, gaps2)

    identity = (matches / length * 100) if length > 0 else 0
    similarity = (matches / aligned_positions * 100) if aligned_positions > 0 else 0
    gap_percentage = (total_gaps / (length * 2) * 100) if length > 0 else 0

    return {
        'length': length,
        'matches': matches,
        'mismatches': mismatches,
        'gaps_seq1': gaps1,
        'gaps_seq2': gaps2,
        'gap_opens_seq1': gap_opens1,
        'gap_opens_seq2': gap_opens2,
        'total_gaps': total_gaps,
        'identity': round(identity, 2),
        'similarity': round(similarity, 2),
        'gap_percentage': round(gap_percentage, 2)
    }


def generate_alignment_display(aligned1: str, aligned2: str, line_width: int = 60) -> List[Dict[str, str]]:
    """
    Generate formatted alignment display blocks.

    Args:
        aligned1: First aligned sequence
        aligned2: Second aligned sequence
        line_width: Characters per line

    Returns:
        List of alignment blocks for display
    """
    blocks = []
    length = len(aligned1)

    for start in range(0, length, line_width):
        end = min(start + line_width, length)

        seg1 = aligned1[start:end]
        seg2 = aligned2[start:end]

        # Build match line
        match_line = []
        for i in range(len(seg1)):
            a, b = seg1[i], seg2[i]
            if a == '-' or b == '-':
                match_line.append(' ')
            elif a.upper() == b.upper():
                match_line.append('|')
            else:
                match_line.append('.')

        blocks.append({
            'start': start + 1,
            'end': end,
            'seq1': seg1,
            'seq2': seg2,
            'match': ''.join(match_line)
        })

    return blocks


def generate_diff_visualization(seq1: str, seq2: str, aligned1: str, aligned2: str) -> Dict[str, Any]:
    """
    Generate diff-style visualization data for sequence comparison.

    Args:
        seq1: Original sequence 1
        seq2: Original sequence 2
        aligned1: Aligned sequence 1
        aligned2: Aligned sequence 2

    Returns:
        Dictionary with diff visualization data
    """
    # Generate character-by-character diff info
    diff_chars = []
    pos1, pos2 = 0, 0

    for i in range(len(aligned1)):
        a, b = aligned1[i], aligned2[i]

        char_info = {
            'idx': i,
            'char1': a,
            'char2': b,
            'pos1': pos1 if a != '-' else None,
            'pos2': pos2 if b != '-' else None,
            'type': 'match' if a.upper() == b.upper() and a != '-' else
                    'gap1' if a == '-' else
                    'gap2' if b == '-' else
                    'mismatch'
        }
        diff_chars.append(char_info)

        if a != '-':
            pos1 += 1
        if b != '-':
            pos2 += 1

    # Find conserved regions
    conserved_regions = []
    in_conserved = False
    conserved_start = 0

    for i, char_info in enumerate(diff_chars):
        if char_info['type'] == 'match':
            if not in_conserved:
                in_conserved = True
                conserved_start = i
        else:
            if in_conserved and i - conserved_start >= 5:
                conserved_regions.append({
                    'start': conserved_start,
                    'end': i - 1,
                    'length': i - conserved_start
                })
            in_conserved = False

    if in_conserved and len(diff_chars) - conserved_start >= 5:
        conserved_regions.append({
            'start': conserved_start,
            'end': len(diff_chars) - 1,
            'length': len(diff_chars) - conserved_start
        })

    return {
        'diff_chars': diff_chars,
        'conserved_regions': conserved_regions,
        'total_length': len(diff_chars)
    }


def compare_sequences(seq1: str, seq2: str, method: str = 'global',
                      is_protein: bool = False) -> Dict[str, Any]:
    """
    Compare two sequences using the specified alignment method.

    Args:
        seq1: First sequence
        seq2: Second sequence
        method: Alignment method ('global', 'local', or 'quick')
        is_protein: Whether sequences are protein

    Returns:
        Dictionary with comparison results
    """
    seq1 = seq1.upper().replace(' ', '').replace('\n', '')
    seq2 = seq2.upper().replace(' ', '').replace('\n', '')

    # Limit sequence length for performance
    max_len = 10000
    if len(seq1) > max_len or len(seq2) > max_len:
        return {
            'error': f'Sequences too long. Maximum length is {max_len} bp.',
            'success': False
        }

    result = {
        'success': True,
        'seq1_length': len(seq1),
        'seq2_length': len(seq2),
        'method': method,
        'is_protein': is_protein
    }

    if method == 'quick':
        # Quick comparison without full alignment
        # Use sliding window to find best match region
        window_size = min(100, min(len(seq1), len(seq2)))
        best_match = 0
        best_pos = (0, 0)

        for i in range(0, len(seq1) - window_size + 1, 10):
            window1 = seq1[i:i+window_size]
            for j in range(0, len(seq2) - window_size + 1, 10):
                window2 = seq2[j:j+window_size]
                matches = sum(1 for a, b in zip(window1, window2) if a == b)
                if matches > best_match:
                    best_match = matches
                    best_pos = (i, j)

        result['quick_identity'] = round(best_match / window_size * 100, 2) if window_size > 0 else 0
        result['best_match_region'] = {
            'seq1_start': best_pos[0],
            'seq2_start': best_pos[1],
            'window_size': window_size
        }

    elif method == 'local':
        aligned1, aligned2, score, start1, start2 = smith_waterman(seq1, seq2, is_protein)
        result['aligned_seq1'] = aligned1
        result['aligned_seq2'] = aligned2
        result['score'] = score
        result['start1'] = start1
        result['start2'] = start2
        result['stats'] = calculate_alignment_stats(aligned1, aligned2)
        result['display'] = generate_alignment_display(aligned1, aligned2)
        result['diff'] = generate_diff_visualization(seq1, seq2, aligned1, aligned2)

    else:  # global
        aligned1, aligned2, score = needleman_wunsch(seq1, seq2, is_protein)
        result['aligned_seq1'] = aligned1
        result['aligned_seq2'] = aligned2
        result['score'] = score
        result['stats'] = calculate_alignment_stats(aligned1, aligned2)
        result['display'] = generate_alignment_display(aligned1, aligned2)
        result['diff'] = generate_diff_visualization(seq1, seq2, aligned1, aligned2)

    return result


def generate_dotplot_data(seq1: str, seq2: str, window_size: int = 10,
                          threshold: float = 0.7) -> Dict[str, Any]:
    """
    Generate dot plot data for sequence comparison visualization.

    Args:
        seq1: First sequence
        seq2: Second sequence
        window_size: Size of comparison window
        threshold: Minimum match ratio to register a dot

    Returns:
        Dictionary with dotplot data
    """
    seq1 = seq1.upper().replace(' ', '').replace('\n', '')
    seq2 = seq2.upper().replace(' ', '').replace('\n', '')

    # Limit for performance
    max_len = 5000
    if len(seq1) > max_len:
        seq1 = seq1[:max_len]
    if len(seq2) > max_len:
        seq2 = seq2[:max_len]

    dots = []
    step = max(1, window_size // 2)

    for i in range(0, len(seq1) - window_size + 1, step):
        window1 = seq1[i:i+window_size]
        for j in range(0, len(seq2) - window_size + 1, step):
            window2 = seq2[j:j+window_size]
            matches = sum(1 for a, b in zip(window1, window2) if a == b)
            match_ratio = matches / window_size

            if match_ratio >= threshold:
                dots.append({
                    'x': i + window_size // 2,
                    'y': j + window_size // 2,
                    'intensity': match_ratio
                })

    return {
        'dots': dots,
        'seq1_length': len(seq1),
        'seq2_length': len(seq2),
        'window_size': window_size,
        'threshold': threshold,
        'total_dots': len(dots)
    }
