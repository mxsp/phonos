"""Tests for output->input chaining classification (phonos_app.helpers.model_io).

The module is deliberately dependency-free, so these tests load it directly
rather than importing the Flask app.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'phonos_app', 'helpers', 'model_io.py',
)
_spec = importlib.util.spec_from_file_location('phonos_model_io_under_test', _MODULE_PATH)
model_io = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(model_io)


PDB = """\
ATOM      1  N   MET A   1      27.340  24.430   2.614  1.00 92.00           N
ATOM      2  CA  MET A   1      26.266  25.413   2.842  1.00 92.00           C
ATOM      3  CA  LYS A   2      26.913  26.639   3.531  1.00 88.00           C
ATOM      4  CA  GLY A   3      27.000  27.000   4.000  1.00 70.00           C
ATOM      5  CA  ALA B   1      10.000  10.000  10.000  1.00 60.00           C
ATOM      6  CA  TRP B   2      11.000  11.000  11.000  1.00 60.00           C
HETATM   99  C1  LIG A 900       5.000   5.000   5.000  1.00 30.00           C
HETATM  100  O   HOH A 901       6.000   6.000   6.000  1.00 30.00           O
END
"""

CIF = """\
data_model
loop_
_atom_site.group_PDB
_atom_site.label_atom_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.auth_asym_id
_atom_site.label_seq_id
_atom_site.auth_seq_id
ATOM CA MET A A 1 1
ATOM CA GLU A A 2 2
ATOM CA TRP A A 3 3
#
"""

MODELS = [
    {'id': 'esmfold-re', 'name': 'ESMFold-RE', 'short_name': 'ESMFold-RE',
     'task': 'protein_folding', 'available': True},
    {'id': 'proteinmpnn-re', 'name': 'ProteinMPNN-RE', 'short_name': 'ProteinMPNN-RE',
     'task': 'protein_design', 'available': True},
    {'id': 'diffdock-re', 'name': 'DiffDock-RE', 'short_name': 'DiffDock-RE',
     'task': 'molecular_docking', 'available': True},
    {'id': 'evo2', 'name': 'Evo2', 'short_name': 'Evo2',
     'task': 'dna_generation', 'available': False},
]


# --------------------------------------------------------------------------
# Sequence kind detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize('sequence,expected', [
    ('MKTAYIAKQRQISFVK', model_io.KIND_PROTEIN),
    ('ATGCGTACGTTAGCGATCGATCGGGCATTA', model_io.KIND_NUCLEOTIDE),
    ('AUGCGUACGUUAGCGAUCG', model_io.KIND_NUCLEOTIDE),
    ('MKGAWTEEE', model_io.KIND_PROTEIN),
    # W is not in the nucleotide alphabet, so a mostly-ACGT string with one
    # protein-only residue must still read as protein.
    ('ACGTACGTW', model_io.KIND_PROTEIN),
])
def test_detect_sequence_kind(sequence, expected):
    assert model_io.detect_sequence_kind(sequence) == expected


def test_detect_sequence_kind_empty_defaults_to_protein():
    assert model_io.detect_sequence_kind('') == model_io.KIND_PROTEIN


# --------------------------------------------------------------------------
# FASTA
# --------------------------------------------------------------------------

def test_parse_fasta_multiple_records():
    records = model_io.parse_fasta('>design_1\nMKGAW\nTEEE\n>design_2\nMKTAYIAK\n')
    assert [r['name'] for r in records] == ['design_1', 'design_2']
    assert records[0]['sequence'] == 'MKGAWTEEE'
    assert records[0]['length'] == 9
    assert records[1]['kind'] == model_io.KIND_PROTEIN


def test_parse_fasta_accepts_headerless_body():
    records = model_io.parse_fasta('MKTAYIAKQRQ\n')
    assert len(records) == 1
    assert records[0]['sequence'] == 'MKTAYIAKQRQ'


def test_parse_fasta_skips_empty_records():
    assert model_io.parse_fasta('>only_a_header\n\n>another\n\n') == []


def test_parse_fasta_strips_digits_and_whitespace():
    records = model_io.parse_fasta('>x\n1 MKT 10\nAYI\n')
    assert records[0]['sequence'] == 'MKTAYI'


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------

def test_pdb_yields_one_sequence_per_chain():
    sequences = model_io.parse_structure_sequences(PDB, 'pdb')
    assert {s['chain']: s['sequence'] for s in sequences} == {'A': 'MKG', 'B': 'AW'}


def test_pdb_ignores_hetatm_residues():
    # LIG/HOH are HETATM, so they must not leak into the chain sequences.
    sequences = model_io.parse_structure_sequences(PDB, 'pdb')
    assert all('X' not in s['sequence'] for s in sequences)


def test_cif_chain_extraction():
    sequences = model_io.parse_structure_sequences(CIF, 'cif')
    assert len(sequences) == 1
    assert sequences[0]['sequence'] == 'MEW'


def test_extract_structure_ligands_skips_solvent():
    assert model_io.extract_structure_ligands(PDB) == ['LIG']


# --------------------------------------------------------------------------
# SMILES
# --------------------------------------------------------------------------

def test_looks_like_smiles_rejects_ambiguous_without_trust():
    # 'CCO' is ethanol but also a valid tripeptide — only trust it when the
    # extension already says the file is SMILES.
    assert model_io.looks_like_smiles('CCO') is False
    assert model_io.looks_like_smiles('CCO', trusted=True) is True


def test_looks_like_smiles_accepts_unambiguous():
    assert model_io.looks_like_smiles('CC(=O)Oc1ccccc1C(=O)O') is True


def test_parse_smiles_reads_name_column():
    molecules = model_io.parse_smiles('CC(=O)Oc1ccccc1C(=O)O aspirin\n# comment\n', trusted=True)
    assert molecules == [{'smiles': 'CC(=O)Oc1ccccc1C(=O)O', 'name': 'aspirin'}]


# --------------------------------------------------------------------------
# classify_payload
# --------------------------------------------------------------------------

def test_classify_pdb():
    result = model_io.classify_payload('boltz2_model_0.pdb', PDB)
    assert model_io.KIND_STRUCTURE in result['kinds']
    assert model_io.KIND_PROTEIN in result['kinds']
    assert result['ligand_codes'] == ['LIG']
    assert result['needs_file'] is True


def test_classify_designed_fasta():
    result = model_io.classify_payload('proteinmpnn_designs.fasta', '>d1\nMKGAW\n>d2\nMKTAYIAK\n')
    assert result['kinds'] == [model_io.KIND_PROTEIN]
    assert len(result['sequences']) == 2
    assert result['needs_file'] is False


def test_classify_nucleotide_fasta_is_not_offered_to_folding():
    result = model_io.classify_payload('evo2_out.fasta', '>g\nATGCGTACGTTAGCGATCGATCGGGCATTA\n')
    assert result['kinds'] == [model_io.KIND_NUCLEOTIDE]
    assert [m['id'] for m in model_io.compatible_models(result['kinds'], MODELS)] == []


def test_classify_sdf_without_content():
    result = model_io.classify_payload('rank1_ligand.sdf')
    assert result['kinds'] == [model_io.KIND_LIGAND]
    assert result['needs_file'] is True


def test_classify_gzipped_extension_is_unwrapped():
    result = model_io.classify_payload('model.pdb.gz', PDB)
    assert model_io.KIND_STRUCTURE in result['kinds']


def test_classify_txt_containing_fasta():
    result = model_io.classify_payload('output.txt', '>seq\nMKVLEEQ\n')
    assert result['kinds'] == [model_io.KIND_PROTEIN]


def test_classify_txt_containing_smiles():
    result = model_io.classify_payload('output.txt', 'CC(=O)Oc1ccccc1C(=O)O\n')
    assert model_io.KIND_SMILES in result['kinds']


def test_classify_json_generated_sequence():
    result = model_io.classify_payload('evo2.json', '{"generated_sequence": "ATGCGTACGTTAGC"}')
    assert result['kinds'] == [model_io.KIND_NUCLEOTIDE]


def test_classify_json_molecule_list():
    payload = '{"molecules": [{"smiles": "CC(=O)Oc1ccccc1C(=O)O", "score": 1}]}'
    result = model_io.classify_payload('molmim.json', payload)
    assert model_io.KIND_SMILES in result['kinds']


def test_classify_unknown_binary_extension_is_inert():
    result = model_io.classify_payload('checkpoint.bin', None)
    assert result['kinds'] == []
    assert model_io.compatible_models(result['kinds'], MODELS) == []


def test_classify_a3m_keeps_query_sequence_only():
    a3m = '>query\nMKTAYIAK\n>hit1\nMKTAYIAK\n>hit2\nMKTAYIAK\n'
    result = model_io.classify_payload('msa.a3m', a3m)
    assert model_io.KIND_MSA in result['kinds']
    assert len(result['sequences']) == 1


# --------------------------------------------------------------------------
# Compatibility
# --------------------------------------------------------------------------

def test_structure_routes_to_design_and_docking():
    ids = [m['id'] for m in model_io.compatible_models([model_io.KIND_STRUCTURE], MODELS)]
    assert set(ids) == {'proteinmpnn-re', 'diffdock-re'}


def test_protein_sequence_routes_to_folding():
    ids = [m['id'] for m in model_io.compatible_models([model_io.KIND_PROTEIN], MODELS)]
    assert ids == ['esmfold-re']


def test_unavailable_models_are_hidden_unless_requested():
    assert model_io.compatible_models([model_io.KIND_NUCLEOTIDE], MODELS) == []
    included = model_io.compatible_models([model_io.KIND_NUCLEOTIDE], MODELS, include_unavailable=True)
    assert [m['id'] for m in included] == ['evo2']
    assert included[0]['available'] is False


def test_compatible_models_reports_delivery_channel():
    structure_target = model_io.compatible_models([model_io.KIND_STRUCTURE], MODELS)[0]
    assert structure_target['delivery'] == 'file'
    sequence_target = model_io.compatible_models([model_io.KIND_PROTEIN], MODELS)[0]
    assert sequence_target['delivery'] == 'sequence'


def test_compatible_models_ignores_unknown_kinds():
    assert model_io.compatible_models(['not_a_kind'], MODELS) == []


def test_full_chain_proteinmpnn_output_folds():
    """The headline chain: ProteinMPNN designs -> a folding model."""
    designs = model_io.classify_payload('designs.fa', '>d1\nMKTAYIAKQRQISFVK\n')
    targets = model_io.compatible_models(designs['kinds'], MODELS)
    assert 'esmfold-re' in [t['id'] for t in targets]


def test_full_chain_predicted_structure_designs():
    """And the reverse: a predicted structure -> sequence design / docking."""
    predicted = model_io.classify_payload('esmfold_out.pdb', PDB)
    targets = {t['id'] for t in model_io.compatible_models(predicted['kinds'], MODELS)}
    assert {'proteinmpnn-re', 'diffdock-re', 'esmfold-re'} <= targets


def test_describe_matrix_covers_every_declared_model():
    matrix = model_io.describe_matrix(MODELS)
    assert len(matrix['models']) == len(model_io.MODEL_INPUT_KINDS)
    assert {k['id'] for k in matrix['kinds']} == set(model_io.ALL_KINDS)
    known = {m['id']: m for m in matrix['models']}
    assert known['esmfold-re']['known'] is True
    assert known['molmim']['known'] is False


def test_every_model_kind_is_a_known_kind():
    for model_id, kinds in model_io.MODEL_INPUT_KINDS.items():
        for kind in kinds:
            assert kind in model_io.ALL_KINDS, f'{model_id} declares unknown kind {kind}'
            assert kind in model_io.KIND_DELIVERY, f'{kind} has no delivery channel'
