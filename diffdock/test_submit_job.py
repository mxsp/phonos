#!/usr/bin/env python3
"""Submit a test DiffDock job to the Redis queue."""
import sys
import os
import json
import redis

# Add the diffdock directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.encryption_util import encrypt

# Connect to production Redis
redis_client = redis.Redis(
    host=os.environ.get('REDIS_HOST', '127.0.0.1'),
    port=6379,
    password=os.environ.get('REDIS_PASSWORD', None),
    decode_responses=False
)

# Sample job data matching the format from the main app
test_job = {
    'job_id': 'test-omegaconf-fixed-001',
    'params': {
        'protein': 'MTEYKLVVVGAGGVGKSALTIQLIQNHFVDEYDPTIED',  # Small test sequence
        'ligand': 'CC(=O)OC1=CC=CC=C1C(=O)O',  # Aspirin SMILES
        'samples_per_complex': 5,
        'inference_steps': 10
    }
}

print("Test job structure:")
print(json.dumps(test_job, indent=2))
print("\nEncrypting and queuing test job...")

# Encrypt the job
job_json = json.dumps(test_job)
encrypted_job = encrypt(job_json.encode())

# Push to queue
redis_client.rpush('queue:job:type-3', encrypted_job)
print(f"✅ Test job queued! Job ID: {test_job['job_id']}")
print("Check the worker logs for processing details.")
