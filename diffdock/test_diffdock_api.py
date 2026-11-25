#!/usr/bin/env python3
"""
Test script for DiffDock NVIDIA API integration.
This submits a test job directly to the type-3 worker queue.

Demonstrates 3 protein input methods:
1. Direct PDB content (fastest, skips ESMFold)
2. PDB file path (fast, skips ESMFold)
3. Amino acid sequence (slow, uses ESMFold)
"""

import json
import redis
import sys
import os

# Add production directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from encryption_util import encrypt

# Example SMILES strings for testing
ASPIRIN_SMILES = "CC(=O)OC1=CC=CC=C1C(=O)O"
CAFFEINE_SMILES = "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"
IBUPROFEN_SMILES = "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"

# Example protein sequences
SHORT_PROTEIN = "MTEYKLVVVGAGGVGKSALTIQLIQNHFVDEYDPTIED"  # 40 amino acids
TINY_PROTEIN = "MKFLKFSLLTAVLLSVVFAT"  # 20 amino acids

# Example PDB content (small protein structure)
# This is a minimal PDB structure for testing
EXAMPLE_PDB_CONTENT = """HEADER    EXAMPLE PROTEIN
ATOM      1  N   MET A   1      -0.782  -8.860 -12.432  1.00  0.72           N
ATOM      2  CA  MET A   1      -0.849  -7.402 -12.419  1.00  0.73           C
ATOM      3  C   MET A   1       0.487  -6.701 -12.165  1.00  0.74           C
ATOM      4  O   MET A   1       1.546  -7.331 -12.207  1.00  0.71           O
ATOM      5  N   LYS A   2       0.442  -5.393 -11.897  1.00  0.75           N
ATOM      6  CA  LYS A   2       1.634  -4.580 -11.636  1.00  0.76           C
ATOM      7  C   LYS A   2       2.156  -3.895 -12.895  1.00  0.77           C
ATOM      8  O   LYS A   2       1.380  -3.287 -13.644  1.00  0.75           O
END
"""

# Example SDF content (Aspirin molecule)
# This is a simple molecule in SDF format for testing
EXAMPLE_SDF_CONTENT = """Aspirin
  Mrv0541 02231512512D

 13 13  0  0  0  0            999 V2000
    2.3644   -0.5600    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.0788   -0.1475    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.0788    0.6775    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.3644    1.0900    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.6500    0.6775    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.6500   -0.1475    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.3644   -1.3850    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.0788   -1.7975    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
    1.6500   -1.7975    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
    0.9356    1.0900    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
    0.2211    0.6775    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -0.4933    1.0900    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.2211   -0.1475    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  2  0  0  0  0
  2  3  1  0  0  0  0
  3  4  2  0  0  0  0
  4  5  1  0  0  0  0
  5  6  2  0  0  0  0
  6  1  1  0  0  0  0
  1  7  1  0  0  0  0
  7  8  2  0  0  0  0
  7  9  1  0  0  0  0
  5 10  1  0  0  0  0
 10 11  1  0  0  0  0
 11 12  1  0  0  0  0
 11 13  2  0  0  0  0
M  END
$$$$
"""


def submit_diffdock_job(
    job_id: str,
    ligand_smiles: str = None,
    protein_sequence: str = None,
    protein_path: str = None,
    pdb_content: str = None,
    sdf_content: str = None,
    sdf_path: str = None,
    samples: int = 5,
    steps: int = 10
):
    """
    Submit a DiffDock job to the type-3 worker queue.

    Protein Input (choose ONE):
        protein_sequence: Amino acid sequence (slow, uses ESMFold - ~20-25s)
        protein_path: Path to PDB file (fast, skips ESMFold)
        pdb_content: Direct PDB content string (fastest, skips ESMFold)

    Ligand Input (choose ONE):
        ligand_smiles: SMILES string (will be converted to SDF)
        sdf_path: Path to SDF file (fast)
        sdf_content: Direct SDF content string (fastest)

    Args:
        job_id: Unique job identifier
        samples: Number of binding poses to generate
        steps: Number of inference steps
    """

    # Validate protein input
    protein_inputs = sum([bool(protein_sequence), bool(protein_path), bool(pdb_content)])
    if protein_inputs == 0:
        print("✗ Error: Must provide one protein input method")
        print("  Options: protein_sequence, protein_path, or pdb_content")
        return False
    if protein_inputs > 1:
        print("⚠ Warning: Multiple protein inputs provided, will use priority: pdb_content > protein_path > sequence")

    # Validate ligand input
    ligand_inputs = sum([bool(ligand_smiles), bool(sdf_path), bool(sdf_content)])
    if ligand_inputs == 0:
        print("✗ Error: Must provide one ligand input method")
        print("  Options: ligand_smiles, sdf_path, or sdf_content")
        return False
    if ligand_inputs > 1:
        print("⚠ Warning: Multiple ligand inputs provided, will use priority: sdf_content > sdf_path > ligand_smiles")

    # Connect to Redis
    r = redis.Redis(
        host=Config.REDIS_HOST,
        port=Config.REDIS_PORT,
        db=Config.REDIS_DB
    )

    # Test Redis connection
    try:
        r.ping()
        print("✓ Connected to Redis")
    except redis.ConnectionError:
        print("✗ Failed to connect to Redis")
        return False

    # Prepare output path
    output_path = f'/opt/phonos/job_outputs/{job_id}/output.json'

    # Build params dict
    params = {
        'samples_per_complex': samples,
        'inference_steps': steps,
    }

    # Add protein input (priority: pdb_content > protein_path > sequence)
    if pdb_content:
        params['pdb_content'] = pdb_content
        protein_method = "Direct PDB content (fastest, skips ESMFold)"
    elif protein_path:
        params['protein_path'] = protein_path
        protein_method = f"PDB file path (fast, skips ESMFold): {protein_path}"
    elif protein_sequence:
        params['sequence'] = protein_sequence
        protein_method = f"Amino acid sequence (slow, uses ESMFold): {len(protein_sequence)} aa"

    # Add ligand input (priority: sdf_content > sdf_path > ligand_smiles)
    if sdf_content:
        params['sdf_content'] = sdf_content
        ligand_method = "Direct SDF content (fastest)"
    elif sdf_path:
        params['sdf_path'] = sdf_path
        ligand_method = f"SDF file path: {sdf_path}"
    elif ligand_smiles:
        params['ligand'] = ligand_smiles
        ligand_method = f"SMILES string (will convert to SDF): {ligand_smiles}"

    # Create job payload
    job_payload = {
        'job_id': job_id,
        'model': 'type-3',
        'task': 'molecular_docking',
        'organization_id': 'test-user',
        'params': params,
        'output_path': output_path,
    }

    print(f"\n{'='*60}")
    print("Job Submission Details:")
    print(f"{'='*60}")
    print(f"Job ID: {job_id}")
    print(f"Protein Method: {protein_method}")
    print(f"Ligand Method: {ligand_method}")
    print(f"Samples: {samples}, Steps: {steps}")
    print(f"{'='*60}\n")

    # Encrypt the job
    job_json = json.dumps(job_payload)
    encrypted_job = encrypt(job_json.encode('utf-8'))

    if not encrypted_job:
        print("✗ Failed to encrypt job payload")
        return False

    print("✓ Job payload encrypted")

    # Push to the type-3 worker queue
    try:
        r.lpush('queue:job:type-3', encrypted_job)
        print(f"✓ Job {job_id} submitted to queue:job:type-3")
        print(f"\nOutput will be saved to: {output_path}")
        print("\nMonitor the worker log:")
        print("  tail -f /opt/phonos/production/type-3_worker.log")
        return True
    except Exception as e:
        print(f"✗ Failed to submit job: {e}")
        return False


def main():
    """Run test cases."""
    print("\n" + "="*60)
    print("DiffDock NVIDIA API Integration Test")
    print("="*60)

    # Check if NVIDIA API key is configured
    nvidia_api_key = os.getenv('Nvidia_Api', '')
    if not nvidia_api_key:
        print("\n⚠ WARNING: NVIDIA API key not configured!")
        print("  Set Nvidia_Api in your .env file")
        print("  Jobs will fail without a valid API key")
    else:
        print(f"\n✓ NVIDIA API key configured: {nvidia_api_key[:20]}...")

    print("\n" + "="*60)
    print("Test Options:")
    print("="*60)
    print("1. Test with PDB + SDF content (FASTEST - no conversion needed)")
    print("2. Test with PDB content + SMILES (FAST - PDB direct, converts SMILES)")
    print("3. Test with protein sequence (SLOW - uses ESMFold ~20s)")
    print("4. Custom test")
    print("5. Exit")

    choice = input("\nSelect option (1-5): ").strip()

    if choice == "1":
        print("\n🚀 Submitting test with direct PDB + SDF content...")
        print("   This is the ABSOLUTE FASTEST method - no conversions!")
        success = submit_diffdock_job(
            job_id="test-pdb-sdf-001",
            pdb_content=EXAMPLE_PDB_CONTENT,
            sdf_content=EXAMPLE_SDF_CONTENT,
            samples=5,
            steps=10
        )

    elif choice == "2":
        print("\n🚀 Submitting test with PDB content + SMILES...")
        print("   PDB is direct, SMILES will be converted to SDF")
        success = submit_diffdock_job(
            job_id="test-pdb-smiles-001",
            pdb_content=EXAMPLE_PDB_CONTENT,
            ligand_smiles=ASPIRIN_SMILES,
            samples=5,
            steps=10
        )

    elif choice == "3":
        print("\n🚀 Submitting test with protein sequence...")
        print("   This uses ESMFold conversion (~20-25 seconds)")
        success = submit_diffdock_job(
            job_id="test-sequence-001",
            protein_sequence=TINY_PROTEIN,
            ligand_smiles=CAFFEINE_SMILES,
            samples=5,
            steps=10
        )

    elif choice == "4":
        print("\n📝 Custom test:")
        job_id = input("  Job ID: ").strip() or "test-custom-001"

        print("\nProtein input method:")
        print("  1. Direct PDB content (FASTEST - skips ESMFold)")
        print("  2. PDB file path (FAST - skips ESMFold)")
        print("  3. Amino acid sequence (SLOW - uses ESMFold ~20s)")
        method_choice = input("Select (1-3): ").strip()

        pdb_content_val = None
        protein_path_val = None
        protein_seq_val = None

        if method_choice == "1":
            print("\nUsing example PDB content...")
            pdb_content_val = EXAMPLE_PDB_CONTENT
        elif method_choice == "2":
            protein_path_val = input("  Enter PDB file path: ").strip()
        else:
            print("\nProtein sequence options:")
            print(f"  1. Tiny (20 aa): {TINY_PROTEIN}")
            print(f"  2. Short (40 aa): {SHORT_PROTEIN}")
            print("  3. Custom sequence")
            seq_choice = input("Select (1-3): ").strip()

            if seq_choice == "1":
                protein_seq_val = TINY_PROTEIN
            elif seq_choice == "2":
                protein_seq_val = SHORT_PROTEIN
            else:
                protein_seq_val = input("  Enter protein sequence: ").strip()

        print("\nLigand input method:")
        print("  1. Direct SDF content (FASTEST)")
        print("  2. SDF file path")
        print("  3. SMILES string (will convert to SDF)")
        ligand_method_choice = input("Select (1-3): ").strip()

        sdf_content_val = None
        sdf_path_val = None
        ligand_smiles_val = None

        if ligand_method_choice == "1":
            print("\nUsing example SDF content (Aspirin)...")
            sdf_content_val = EXAMPLE_SDF_CONTENT
        elif ligand_method_choice == "2":
            sdf_path_val = input("  Enter SDF file path: ").strip()
        else:
            print("\nLigand SMILES options:")
            print(f"  1. Aspirin: {ASPIRIN_SMILES}")
            print(f"  2. Caffeine: {CAFFEINE_SMILES}")
            print(f"  3. Ibuprofen: {IBUPROFEN_SMILES}")
            print("  4. Custom SMILES")
            lig_choice = input("Select (1-4): ").strip()

            if lig_choice == "1":
                ligand_smiles_val = ASPIRIN_SMILES
            elif lig_choice == "2":
                ligand_smiles_val = CAFFEINE_SMILES
            elif lig_choice == "3":
                ligand_smiles_val = IBUPROFEN_SMILES
            else:
                ligand_smiles_val = input("  Enter SMILES: ").strip()

        samples = int(input("\n  Number of poses (default 10): ").strip() or "10")
        steps = int(input("  Inference steps (default 20): ").strip() or "20")

        print(f"\n🚀 Submitting custom test...")
        success = submit_diffdock_job(
            job_id=job_id,
            protein_sequence=protein_seq_val,
            protein_path=protein_path_val,
            pdb_content=pdb_content_val,
            ligand_smiles=ligand_smiles_val,
            sdf_content=sdf_content_val,
            sdf_path=sdf_path_val,
            samples=samples,
            steps=steps
        )

    else:
        print("\nExiting...")
        return

    if success:
        print("\n" + "="*60)
        print("✅ Job submitted successfully!")
        print("="*60)
        print("\nNext steps:")
        print("1. Ensure the type-3 worker is running:")
        print("   python /opt/phonos/production/node3-diffdock.py")
        print("\n2. Monitor the worker log:")
        print("   tail -f /opt/phonos/production/type-3_worker.log")
        print("\n3. Check the output directory when complete:")
        print("   ls -la /opt/phonos/job_outputs/")
    else:
        print("\n" + "="*60)
        print("❌ Job submission failed")
        print("="*60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
