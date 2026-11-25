#!/usr/bin/env python3
"""
Example User Model Worker for Phonos Bridge API

This script demonstrates how to create a simple worker that:
1. Authenticates with the Phonos Bridge API using an API key
2. Polls for pending jobs assigned to your user model
3. Processes jobs and returns results (including PDB files)

Usage:
    1. Create a User Model in Phonos dashboard
    2. Create an API key for that model
    3. Set the API key as an environment variable: export PHONOS_API_KEY="pk_..."
    4. Run this script: python user_model_worker_example.py

Bridge API Endpoints:
    - GET  /api/bridge/v1/health       - Health check
    - POST /api/bridge/v1/jobs/poll    - Poll for new jobs
    - POST /api/bridge/v1/jobs/:id/result - Submit job result
    - GET  /api/bridge/v1/worker/info  - Get authorized models/projects
"""

import os
import sys
import json
import time
import base64
import logging
import requests
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
PHONOS_API_URL = os.environ.get('PHONOS_API_URL', 'https://phonos.fyreflysystems.com')
PHONOS_API_KEY = os.environ.get('PHONOS_API_KEY', '')
WORKER_ID = os.environ.get('WORKER_ID', 'example-worker-1')
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', 10))  # seconds


def get_headers():
    """Get authentication headers for API requests."""
    return {
        'Authorization': f'Bearer {PHONOS_API_KEY}',
        'Content-Type': 'application/json'
    }


def check_health():
    """Check Bridge API health."""
    try:
        response = requests.get(
            f'{PHONOS_API_URL}/api/bridge/v1/health',
            timeout=10
        )
        if response.ok:
            logger.info(f"Bridge API is healthy: {response.json()}")
            return True
        else:
            logger.error(f"Health check failed: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return False


def get_worker_info():
    """Get information about authorized models and projects."""
    try:
        response = requests.get(
            f'{PHONOS_API_URL}/api/bridge/v1/worker/info',
            headers=get_headers(),
            timeout=10
        )
        if response.ok:
            info = response.json()
            logger.info(f"Worker authorized for models: {info.get('authorized_models', [])}")
            return info
        else:
            logger.error(f"Failed to get worker info: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error getting worker info: {e}")
        return None


def poll_jobs(model_id=None, max_jobs=1):
    """Poll for pending jobs from the Bridge API."""
    try:
        payload = {
            'worker_id': WORKER_ID,
            'max_jobs': max_jobs
        }
        if model_id:
            payload['model_id'] = model_id

        response = requests.post(
            f'{PHONOS_API_URL}/api/bridge/v1/jobs/poll',
            headers=get_headers(),
            json=payload,
            timeout=30
        )

        if response.ok:
            data = response.json()
            jobs = data.get('jobs', [])
            if jobs:
                logger.info(f"Received {len(jobs)} job(s)")
            return jobs
        else:
            logger.warning(f"Poll failed: {response.status_code} - {response.text}")
            return []
    except Exception as e:
        logger.error(f"Error polling jobs: {e}")
        return []


def process_job(job):
    """
    Process a job and return a result.

    This example demonstrates returning a simple PDB file.
    Replace this with your actual processing logic.
    """
    job_id = job['job_id']
    model_id = job['model_id']
    input_data = job.get('input_data', {})

    logger.info(f"Processing job {job_id} for model {model_id}")
    logger.info(f"Input data: {json.dumps(input_data, indent=2)}")

    start_time = time.time()

    try:
        # Example: Generate a simple PDB file
        # In a real implementation, this would be your actual computation
        pdb_content = generate_example_pdb(input_data)

        runtime = time.time() - start_time

        # Return success result with PDB file
        return {
            'status': 'completed',
            'result': {
                'message': 'Job completed successfully',
                'output_format': 'pdb',
                'structure_count': 1
            },
            'output_file': base64.b64encode(pdb_content.encode()).decode(),
            'metrics': {
                'runtime_seconds': runtime,
                'memory_used_mb': 50  # Example metric
            }
        }

    except Exception as e:
        runtime = time.time() - start_time
        logger.error(f"Error processing job {job_id}: {e}")

        return {
            'status': 'failed',
            'error_message': str(e),
            'metrics': {
                'runtime_seconds': runtime
            }
        }


def generate_example_pdb(input_data):
    """
    Generate an example PDB file.

    This creates a simple alanine residue as a demonstration.
    Replace this with your actual structure prediction logic.
    """
    sequence = input_data.get('sequence', 'ALA')
    job_name = input_data.get('job_name', 'Unknown')

    # Simple alanine residue PDB structure
    pdb_content = f"""HEADER    EXAMPLE STRUCTURE                        {datetime.now().strftime('%d-%b-%y')}
TITLE     GENERATED BY PHONOS USER MODEL WORKER
TITLE    2 INPUT: {sequence[:50]}
REMARK   1 THIS IS AN EXAMPLE PDB FILE GENERATED BY THE WORKER
REMARK   2 JOB NAME: {job_name}
REMARK   3 SEQUENCE: {sequence[:100]}
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C
ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C
ATOM      4  O   ALA A   1       1.246   2.390   0.000  1.00  0.00           O
ATOM      5  CB  ALA A   1       1.986  -0.728  -1.234  1.00  0.00           C
TER       6      ALA A   1
END
"""
    return pdb_content


def submit_result(job_id, result):
    """Submit job result to the Bridge API."""
    try:
        response = requests.post(
            f'{PHONOS_API_URL}/api/bridge/v1/jobs/{job_id}/result',
            headers=get_headers(),
            json=result,
            timeout=60
        )

        if response.ok:
            logger.info(f"Successfully submitted result for job {job_id}")
            return True
        else:
            logger.error(f"Failed to submit result: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error submitting result for job {job_id}: {e}")
        return False


def main():
    """Main worker loop."""
    if not PHONOS_API_KEY:
        logger.error("PHONOS_API_KEY environment variable not set!")
        logger.info("Set it with: export PHONOS_API_KEY='pk_your_api_key_here'")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Phonos User Model Worker - Example Implementation")
    logger.info("=" * 60)
    logger.info(f"API URL: {PHONOS_API_URL}")
    logger.info(f"Worker ID: {WORKER_ID}")
    logger.info(f"Poll Interval: {POLL_INTERVAL}s")

    # Check health
    if not check_health():
        logger.error("Bridge API health check failed. Exiting.")
        sys.exit(1)

    # Get worker info
    worker_info = get_worker_info()
    if not worker_info:
        logger.error("Failed to get worker info. Check your API key.")
        sys.exit(1)

    logger.info("Starting job polling loop...")
    logger.info(f"Authorized models: {worker_info.get('authorized_models', [])}")

    # Main polling loop
    while True:
        try:
            jobs = poll_jobs()

            for job in jobs:
                job_id = job['job_id']
                logger.info(f"Processing job: {job_id}")

                # Process the job
                result = process_job(job)

                # Submit the result
                success = submit_result(job_id, result)

                if success:
                    logger.info(f"Job {job_id} completed with status: {result['status']}")
                else:
                    logger.error(f"Failed to submit result for job {job_id}")

            if not jobs:
                logger.debug(f"No pending jobs. Sleeping for {POLL_INTERVAL}s...")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Shutting down worker...")
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            time.sleep(POLL_INTERVAL)

    logger.info("Worker stopped.")


if __name__ == '__main__':
    main()
