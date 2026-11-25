"""
Phonos User Model Worker SDK - Python
=====================================

This SDK allows you to run custom ML models on your own infrastructure
and integrate them seamlessly with the Phonos platform.

Installation:
    pip install requests

Usage:
    1. Create a custom model in Phonos dashboard
    2. Generate an API key for your model
    3. Implement your model processing logic in the process_job() function
    4. Run this worker script

Example:
    python worker.py --api-key="your-api-key-here" --endpoint="https://phonos.example.com"
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
from typing import Dict, Any, Optional
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PhonosWorker:
    """
    Phonos Worker Client for processing custom model jobs.

    This worker polls the Phonos Bridge API for jobs, processes them,
    and submits results back securely.
    """

    def __init__(
        self,
        api_key: str,
        phonos_endpoint: str,
        worker_id: Optional[str] = None,
        poll_interval: int = 5,
        max_retries: int = 3
    ):
        """
        Initialize the Phonos Worker.

        Args:
            api_key: Your Phonos model/project API key
            phonos_endpoint: Phonos server URL (e.g., https://phonos.example.com)
            worker_id: Optional worker identifier for tracking
            poll_interval: Seconds between poll requests (default: 5)
            max_retries: Max retries for failed requests (default: 3)
        """
        self.api_key = api_key
        self.phonos_endpoint = phonos_endpoint.rstrip('/')
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        })

    def health_check(self) -> bool:
        """Check if the Phonos Bridge API is accessible."""
        try:
            response = self.session.get(
                f"{self.phonos_endpoint}/api/bridge/v1/health",
                timeout=10
            )
            if response.status_code == 200:
                logger.info("✓ Bridge API is healthy")
                return True
            else:
                logger.error(f"Bridge API health check failed: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Bridge API health check error: {e}")
            return False

    def get_worker_info(self) -> Optional[Dict]:
        """Get information about this worker's authorized models and projects."""
        try:
            response = self.session.get(
                f"{self.phonos_endpoint}/api/bridge/v1/worker/info",
                timeout=10
            )
            if response.status_code == 200:
                info = response.json()
                logger.info(f"✓ Authorized for user {info.get('user_id')}")
                logger.info(f"  Models: {len(info.get('authorized_models', []))}")
                logger.info(f"  Projects: {len(info.get('authorized_projects', []))}")
                return info
            elif response.status_code == 401:
                logger.error("✗ Invalid API key")
                return None
            else:
                logger.error(f"Failed to get worker info: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Error getting worker info: {e}")
            return None

    def poll_jobs(self, model_id: Optional[int] = None, max_jobs: int = 1) -> list:
        """
        Poll for new jobs from Phonos.

        Args:
            model_id: Optional specific model ID to poll for
            max_jobs: Maximum number of jobs to fetch (default: 1)

        Returns:
            List of job objects
        """
        try:
            payload = {
                'worker_id': self.worker_id,
                'max_jobs': max_jobs
            }
            if model_id:
                payload['model_id'] = model_id

            response = self.session.post(
                f"{self.phonos_endpoint}/api/bridge/v1/jobs/poll",
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                jobs = data.get('jobs', [])
                if jobs:
                    logger.info(f"✓ Received {len(jobs)} job(s)")
                return jobs
            elif response.status_code == 401:
                logger.error("✗ Authentication failed - check your API key")
                return []
            else:
                logger.warning(f"Poll request failed: {response.status_code}")
                return []

        except Exception as e:
            logger.error(f"Error polling for jobs: {e}")
            return []

    def submit_result(
        self,
        job_id: str,
        status: str,
        result: Dict[str, Any],
        output_file: Optional[bytes] = None,
        error_message: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Submit job result back to Phonos.

        Args:
            job_id: Job ID from the polled job
            status: "completed" or "failed"
            result: Result data dictionary
            output_file: Optional binary file output (will be base64 encoded)
            error_message: Optional error message if failed
            metrics: Optional metrics dict (e.g., runtime_seconds, memory_used_mb)

        Returns:
            True if submission successful
        """
        try:
            payload = {
                'status': status,
                'result': result,
                'metrics': metrics or {}
            }

            if error_message:
                payload['error_message'] = error_message

            if output_file:
                payload['output_file'] = base64.b64encode(output_file).decode('utf-8')

            response = self.session.post(
                f"{self.phonos_endpoint}/api/bridge/v1/jobs/{job_id}/result",
                json=payload,
                timeout=60
            )

            if response.status_code == 200:
                logger.info(f"✓ Result submitted for job {job_id}")
                return True
            elif response.status_code == 403:
                logger.error(f"✗ Not authorized to submit results for job {job_id}")
                return False
            else:
                logger.error(f"Failed to submit result: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error submitting result for job {job_id}: {e}")
            return False

    def process_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a job. Override this method with your custom model logic.

        Args:
            job: Job object containing:
                - job_id: Unique job identifier
                - model_id: Model ID
                - input_data: Input data dictionary
                - custom_params: Custom parameters
                - output_format: Expected output format

        Returns:
            Dict with keys:
                - status: "completed" or "failed"
                - result: Result data dictionary
                - output_file: Optional bytes of output file
                - error_message: Optional error message if failed
                - metrics: Optional metrics dict
        """
        # IMPLEMENT YOUR MODEL LOGIC HERE
        logger.info(f"Processing job {job['job_id']} with model {job['model_id']}")

        # Example: Extract input data
        input_data = job.get('input_data', {})
        custom_params = job.get('custom_params', {})

        try:
            # TODO: Replace this with your actual model inference code
            # Example:
            # 1. Load your model
            # model = load_my_model()
            #
            # 2. Process input
            # result = model.predict(input_data)
            #
            # 3. Generate output file if needed
            # output_file = generate_output_file(result)

            # Placeholder result
            result = {
                'message': 'Job processed successfully',
                'input_received': input_data,
                'params_received': custom_params
            }

            return {
                'status': 'completed',
                'result': result,
                'output_file': None,  # Set to bytes if you have file output
                'metrics': {
                    'runtime_seconds': 1.0,
                    'memory_used_mb': 128
                }
            }

        except Exception as e:
            logger.error(f"Error processing job: {e}")
            return {
                'status': 'failed',
                'result': {},
                'error_message': str(e)
            }

    def run(self, model_id: Optional[int] = None):
        """
        Start the worker main loop.

        Args:
            model_id: Optional specific model ID to process jobs for
        """
        logger.info(f"Starting Phonos Worker {self.worker_id}")
        logger.info(f"Endpoint: {self.phonos_endpoint}")

        # Health check
        if not self.health_check():
            logger.error("Bridge API is not accessible. Exiting.")
            sys.exit(1)

        # Get worker info
        info = self.get_worker_info()
        if not info:
            logger.error("Failed to authenticate. Check your API key.")
            sys.exit(1)

        logger.info(f"Worker started successfully. Polling every {self.poll_interval}s...")
        logger.info("Press Ctrl+C to stop")

        try:
            while True:
                # Poll for jobs
                jobs = self.poll_jobs(model_id=model_id, max_jobs=1)

                # Process each job
                for job in jobs:
                    job_id = job['job_id']
                    logger.info(f"→ Processing job {job_id}")

                    start_time = time.time()
                    result = self.process_job(job)
                    elapsed = time.time() - start_time

                    # Add runtime to metrics if not already present
                    if 'metrics' not in result:
                        result['metrics'] = {}
                    if 'runtime_seconds' not in result['metrics']:
                        result['metrics']['runtime_seconds'] = elapsed

                    # Submit result
                    success = self.submit_result(
                        job_id=job_id,
                        status=result['status'],
                        result=result.get('result', {}),
                        output_file=result.get('output_file'),
                        error_message=result.get('error_message'),
                        metrics=result['metrics']
                    )

                    if success:
                        logger.info(f"✓ Job {job_id} completed in {elapsed:.2f}s")
                    else:
                        logger.error(f"✗ Failed to submit result for job {job_id}")

                # Sleep before next poll
                if not jobs:
                    logger.debug(f"No jobs available. Sleeping {self.poll_interval}s...")
                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            logger.info("\nWorker stopped by user")
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            sys.exit(1)


# Example usage
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Phonos Custom Model Worker')
    parser.add_argument('--api-key', required=True, help='Phonos API key')
    parser.add_argument('--endpoint', required=True, help='Phonos server URL')
    parser.add_argument('--worker-id', help='Worker identifier')
    parser.add_argument('--model-id', type=int, help='Specific model ID to process')
    parser.add_argument('--poll-interval', type=int, default=5, help='Poll interval in seconds')

    args = parser.parse_args()

    # Create and run worker
    worker = PhonosWorker(
        api_key=args.api_key,
        phonos_endpoint=args.endpoint,
        worker_id=args.worker_id,
        poll_interval=args.poll_interval
    )

    worker.run(model_id=args.model_id)
