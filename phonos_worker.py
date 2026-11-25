#!/usr/bin/env python3
"""
Phonos Worker Client

This script allows users to run computational models on their own infrastructure
and connect securely to the Phonos platform via the Bridge API.

Usage:
    python phonos_worker.py --api-key YOUR_API_KEY --model-id 123

Features:
    - Polls for jobs from the Bridge API
    - Executes custom model code
    - Submits results back securely
    - No direct Redis access needed
    - No encryption keys needed

Security:
    - All communication over HTTPS
    - Uses project/model-specific API keys
    - Only sees plaintext data for authorized jobs
"""

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, Any, Optional, List

import requests

# Configuration
DEFAULT_BRIDGE_URL = "https://phonos.fyreflysystems.de"
DEFAULT_POLL_INTERVAL = 5  # seconds
DEFAULT_MAX_RETRIES = 3

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('phonos_worker')


class PhonosWorker:
    """Worker client for processing Phonos jobs."""

    def __init__(
        self,
        api_key: str,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        model_id: Optional[int] = None,
        project_id: Optional[int] = None,
        worker_id: Optional[str] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        model_script: Optional[str] = None
    ):
        """
        Initialize the Phonos worker.

        Args:
            api_key: Bridge API authentication key
            bridge_url: Base URL of the Bridge API
            model_id: Optional specific model ID to poll for
            project_id: Optional project ID to poll for
            worker_id: Optional custom worker identifier
            poll_interval: Seconds between poll requests
            model_script: Path to custom model execution script
        """
        self.api_key = api_key
        self.bridge_url = bridge_url.rstrip('/')
        self.model_id = model_id
        self.project_id = project_id
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self.poll_interval = poll_interval
        self.model_script = model_script
        self.running = False

        # Verify connectivity and authentication
        self._verify_connection()

    def _verify_connection(self):
        """Verify connection to Bridge API and authentication."""
        try:
            response = requests.get(
                f"{self.bridge_url}/api/bridge/v1/health",
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"Connected to Bridge API at {self.bridge_url}")

            # Get worker info
            response = requests.get(
                f"{self.bridge_url}/api/bridge/v1/worker/info",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10
            )
            response.raise_for_status()
            info = response.json()

            logger.info(f"Authenticated as user {info.get('user_id')}")
            logger.info(f"Authorized models: {info.get('authorized_models', [])}")
            logger.info(f"Authorized projects: {info.get('authorized_projects', [])}")

        except requests.RequestException as e:
            logger.error(f"Failed to connect to Bridge API: {e}")
            raise

    def poll_for_jobs(self) -> List[Dict[str, Any]]:
        """
        Poll the Bridge API for new jobs.

        Returns:
            List of job dictionaries
        """
        try:
            payload = {
                "worker_id": self.worker_id,
                "max_jobs": 1
            }

            if self.model_id:
                payload["model_id"] = self.model_id

            if self.project_id:
                payload["project_id"] = self.project_id

            response = requests.post(
                f"{self.bridge_url}/api/bridge/v1/jobs/poll",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=30
            )
            response.raise_for_status()

            data = response.json()
            if data.get('status') == 'success':
                jobs = data.get('jobs', [])
                if jobs:
                    logger.info(f"Received {len(jobs)} job(s)")
                return jobs
            else:
                logger.warning(f"Unexpected poll response: {data}")
                return []

        except requests.RequestException as e:
            logger.error(f"Error polling for jobs: {e}")
            return []

    def process_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a single job.

        Args:
            job: Job dictionary from poll response

        Returns:
            Result dictionary
        """
        job_id = job['job_id']
        logger.info(f"Processing job {job_id}")

        start_time = time.time()

        try:
            # Extract job data
            input_data = job.get('input_data', {})
            custom_params = job.get('custom_params', {})
            timeout_seconds = job.get('timeout_seconds', 3600)

            # Execute model
            if self.model_script:
                result = self._execute_model_script(input_data, custom_params, timeout_seconds)
            else:
                # Default: Echo input back (for testing)
                result = {
                    'status': 'completed',
                    'output': input_data,
                    'message': 'Default echo processor - no model script provided'
                }

            runtime_seconds = time.time() - start_time

            return {
                'status': 'completed',
                'result': result,
                'metrics': {
                    'runtime_seconds': runtime_seconds,
                    'processed_at': datetime.utcnow().isoformat()
                }
            }

        except Exception as e:
            logger.error(f"Error processing job {job_id}: {e}")
            logger.error(traceback.format_exc())

            runtime_seconds = time.time() - start_time

            return {
                'status': 'failed',
                'error_message': str(e),
                'metrics': {
                    'runtime_seconds': runtime_seconds,
                    'failed_at': datetime.utcnow().isoformat()
                }
            }

    def _execute_model_script(
        self,
        input_data: Dict[str, Any],
        custom_params: Dict[str, Any],
        timeout_seconds: int
    ) -> Dict[str, Any]:
        """
        Execute custom model script.

        Args:
            input_data: Input data for the model
            custom_params: Custom parameters
            timeout_seconds: Execution timeout

        Returns:
            Model output dictionary
        """
        # Write input data to temporary file
        input_file = f"/tmp/phonos_job_input_{os.getpid()}.json"
        output_file = f"/tmp/phonos_job_output_{os.getpid()}.json"

        try:
            with open(input_file, 'w') as f:
                json.dump({
                    'input_data': input_data,
                    'custom_params': custom_params
                }, f)

            # Execute model script
            result = subprocess.run(
                [self.model_script, input_file, output_file],
                timeout=timeout_seconds,
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                raise Exception(f"Model script failed: {result.stderr}")

            # Read output
            with open(output_file, 'r') as f:
                output = json.load(f)

            return output

        finally:
            # Cleanup temp files
            for f in [input_file, output_file]:
                if os.path.exists(f):
                    os.remove(f)

    def submit_result(self, job_id: int, result: Dict[str, Any]) -> bool:
        """
        Submit job result to Bridge API.

        Args:
            job_id: Job ID
            result: Result dictionary

        Returns:
            True if successful, False otherwise
        """
        try:
            response = requests.post(
                f"{self.bridge_url}/api/bridge/v1/jobs/{job_id}/result",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=result,
                timeout=60
            )
            response.raise_for_status()

            data = response.json()
            if data.get('status') == 'success':
                logger.info(f"Successfully submitted result for job {job_id}")
                return True
            else:
                logger.error(f"Failed to submit result: {data}")
                return False

        except requests.RequestException as e:
            logger.error(f"Error submitting result for job {job_id}: {e}")
            return False

    def run(self):
        """Main worker loop."""
        self.running = True
        logger.info(f"Worker {self.worker_id} started. Polling every {self.poll_interval}s...")

        try:
            while self.running:
                # Poll for jobs
                jobs = self.poll_for_jobs()

                # Process each job
                for job in jobs:
                    job_id = job['job_id']

                    # Process job
                    result = self.process_job(job)

                    # Submit result
                    self.submit_result(job_id, result)

                # Sleep before next poll
                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            logger.info("Worker stopped by user")
        except Exception as e:
            logger.error(f"Worker error: {e}")
            logger.error(traceback.format_exc())
        finally:
            self.running = False
            logger.info("Worker shutdown complete")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Phonos Worker - Run computational models for Phonos jobs'
    )

    parser.add_argument(
        '--api-key',
        required=True,
        help='Bridge API authentication key'
    )

    parser.add_argument(
        '--bridge-url',
        default=DEFAULT_BRIDGE_URL,
        help=f'Bridge API base URL (default: {DEFAULT_BRIDGE_URL})'
    )

    parser.add_argument(
        '--model-id',
        type=int,
        help='Specific model ID to process jobs for'
    )

    parser.add_argument(
        '--project-id',
        type=int,
        help='Specific project ID to process jobs for'
    )

    parser.add_argument(
        '--worker-id',
        help='Custom worker identifier'
    )

    parser.add_argument(
        '--poll-interval',
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f'Seconds between poll requests (default: {DEFAULT_POLL_INTERVAL})'
    )

    parser.add_argument(
        '--model-script',
        help='Path to custom model execution script'
    )

    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Create and run worker
    worker = PhonosWorker(
        api_key=args.api_key,
        bridge_url=args.bridge_url,
        model_id=args.model_id,
        project_id=args.project_id,
        worker_id=args.worker_id,
        poll_interval=args.poll_interval,
        model_script=args.model_script
    )

    worker.run()


if __name__ == '__main__':
    main()
