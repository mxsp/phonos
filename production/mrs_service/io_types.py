"""Defines standard data types for the pipeline."""
from __future__ import annotations
from enum import Enum

class IOType(str, Enum):
    # Biological Data
    PROTEIN_SEQUENCE = "protein_sequence"  # Raw string sequence (e.g., "MVLSE...")
    PDB_TEXT = "pdb_text"                  # Content of a .pdb file
    PDB_FILE_ID = "pdb_file_id"            # Internal file ID pointing to a PDB
    SDF_TEXT = "sdf_text"                  # Content of a .sdf file (ligand)
    SDF_FILE_ID = "sdf_file_id"            # Internal file ID pointing to a SDF
    
    # Generic Data
    TEXT = "text"
    JSON = "json"
    FILE_ID = "file_id"
    ANY = "any"

# Registry of Model Signatures
# Maps model_type -> { inputs: {arg_name: type}, outputs: {key_in_result: type} }
MODEL_SIGNATURES = {
    "evo2": {
        "inputs": {
            "prompt": IOType.TEXT
        },
        "outputs": {
            "sequence": IOType.PROTEIN_SEQUENCE
        }
    },
    "esmfold": {
        "inputs": {
            "sequence": IOType.PROTEIN_SEQUENCE
        },
        "outputs": {
            "pdb_content": IOType.PDB_TEXT,
            "pdb_file_id": IOType.PDB_FILE_ID
        }
    },
    "diffdock": {
        "inputs": {
            "protein_pdb": IOType.PDB_TEXT,
            "ligand_sdf": IOType.SDF_TEXT
        },
        "optional_inputs": {
            "protein_file_id": IOType.PDB_FILE_ID,
            "ligand_file_id": IOType.SDF_FILE_ID
        },
        "outputs": {
            "docked_pdb": IOType.PDB_TEXT
        }
    },
    # Generic or Test models
    "echo": {
        "inputs": {"data": IOType.ANY},
        "outputs": {"result": IOType.ANY}
    }
}
