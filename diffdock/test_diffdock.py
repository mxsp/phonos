#!/usr/bin/env python3
"""
Test script for DiffDock molecular docking node.
This demonstrates how to submit a docking job to the worker.
"""

import json
import redis
from config import Config
from encryption_util import encrypt

# Example 1: Aspirin (acetylsalicylic acid) - Simple drug molecule
EXAMPLE_LIGAND_ASPIRIN = "CC(=O)OC1=CC=CC=C1C(=O)O"

# Example 2: Caffeine - Another common molecule
EXAMPLE_LIGAND_CAFFEINE = "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"

# Example 3: Ibuprofen - Anti-inflammatory drug
EXAMPLE_LIGAND_IBUPROFEN = "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"

# Example 4: Dopamine - Neurotransmitter
EXAMPLE_LIGAND_DOPAMINE = "C1=CC(=C(C=C1CCN)O)O"

# Example protein sequence: Small protein (40 amino acids)
# This is a fragment from a real protein kinase
EXAMPLE_PROTEIN_SEQUENCE = "MTEYKLVVVGAGGVGKSALTIQLIQNHFVDEYDPTIED"

# Example protein sequence: Shorter peptide (20 amino acids)
EXAMPLE_PROTEIN_SHORT = "MKFLKFSLLTAVLLSVVFAT"


def create_test_job(job_id: str, protein: str, ligand: str, samples: int = 10, steps: int = 20):
    """Create a test job for DiffDock."""
    job = {
        'job_id': job_id,
        'task': 'molecular_docking',
        'organization_id': 'test-org',
        'params': {
            'protein': protein,
            'ligand': ligand,
            'sequence': protein,  # Alias for protein
            'output_pdb': f'/tmp/diffdock_test/{job_id}/{job_id}.pdb',
            'output_sdf': f'/tmp/diffdock_test/{job_id}/{job_id}.sdf',
            'samples_per_complex': samples,
            'inference_steps': steps,
        }
    }
    return job


def submit_job(job: dict):
    """Submit a job to the DiffDock worker queue."""
    # Connect to Redis
    r = redis.Redis(
        host=Config.REDIS_HOST,
        port=Config.REDIS_PORT,
        db=Config.REDIS_DB,
    )

    # Encrypt the job
    job_json = json.dumps(job)
    encrypted_job = encrypt(job_json.encode('utf-8'))

    if encrypted_job:
        # Push to the queue
        queue_name = 'queue:job:type-3'
        r.lpush(queue_name, encrypted_job)
        print(f"✓ Submitted job {job['job_id']} to queue {queue_name}")
        return True
    else:
        print(f"✗ Failed to encrypt job {job['job_id']}")
        return False


def print_example(name: str, protein: str, ligand: str):
    """Print example input data."""
    print(f"\n{'='*60}")
    print(f"EXAMPLE: {name}")
    print(f"{'='*60}")
    print(f"\nProtein sequence ({len(protein)} amino acids):")
    print(f"  {protein}")
    print(f"\nLigand SMILES:")
    print(f"  {ligand}")
    print(f"\nTo submit this job, run:")
    print(f"  job = create_test_job('test-{name.lower().replace(' ', '-')}', ")
    print(f"                        EXAMPLE_PROTEIN_SEQUENCE,")
    print(f"                        EXAMPLE_LIGAND_{name.upper().replace(' ', '_')})")
    print(f"  submit_job(job)")


if __name__ == '__main__':
    print("\n" + "="*60)
    print("DiffDock Test Examples")
    print("="*60)

    # Print all examples
    print_example("Aspirin", EXAMPLE_PROTEIN_SEQUENCE, EXAMPLE_LIGAND_ASPIRIN)
    print_example("Caffeine", EXAMPLE_PROTEIN_SEQUENCE, EXAMPLE_LIGAND_CAFFEINE)
    print_example("Ibuprofen", EXAMPLE_PROTEIN_SEQUENCE, EXAMPLE_LIGAND_IBUPROFEN)
    print_example("Dopamine", EXAMPLE_PROTEIN_SHORT, EXAMPLE_LIGAND_DOPAMINE)

    print("\n" + "="*60)
    print("QUICK TEST - Submit a job now? (y/n): ", end="")
    response = input().strip().lower()

    if response == 'y':
        print("\nSubmitting test job with Aspirin + protein...")
        job = create_test_job(
            'test-aspirin-001',
            EXAMPLE_PROTEIN_SEQUENCE,
            EXAMPLE_LIGAND_ASPIRIN,
            samples=5,
            steps=10
        )

        if submit_job(job):
            print("\n✓ Job submitted successfully!")
            print(f"  Job ID: {job['job_id']}")
            print(f"  Check worker logs: tail -f type-3_worker.log")
            print(f"  Results will be at: {job['params']['output_pdb']}")
        else:
            print("\n✗ Failed to submit job")
    else:
        print("\nNo job submitted. Use the examples above to test manually.")

    print("\n" + "="*60)
    print("\nDirect copy-paste examples:\n")
    print("# Example 1: Quick test with Aspirin")
    print("job1 = create_test_job('test-001', ")
    print(f"    '{EXAMPLE_PROTEIN_SEQUENCE}',")
    print(f"    '{EXAMPLE_LIGAND_ASPIRIN}')")
    print("submit_job(job1)")
    print()
    print("# Example 2: Caffeine docking")
    print("job2 = create_test_job('test-002', ")
    print(f"    '{EXAMPLE_PROTEIN_SHORT}',")
    print(f"    '{EXAMPLE_LIGAND_CAFFEINE}')")
    print("submit_job(job2)")
    print()
