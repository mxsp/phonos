#!/usr/bin/env python3
"""
ESMFold-RE Protein Structure Prediction Worker Node
Connects to NVIDIA's ESMFold NIM API for protein structure prediction.
Uses NVCF long-polling for async results.
"""
import argparse
import base64
import json
import time
import os
import re
import logging
import threading
import requests
import redis
from typing import Optional, Dict, Any
from pathlib import Path

# Import from phonos production utilities
try:
    from encryption_util import canonical_dumps, decrypt, encrypt
    from node_utils import sign_message, send_job_result
    from config import Config
except ImportError:
    # Fallback for standalone testing
    Config = None
    decrypt = None
    encrypt = None

# Configuration
NVIDIA_ESMFOLD_RE_URL = os.getenv(
    "ESMFOLD_RE_API_URL",
    "https://health.api.nvidia.com/v1/biology/meta/esmfold"
)
NVIDIA_STATUS_URL = os.getenv(
    "ESMFOLD_RE_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "esmfold-re"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_esmfold_re.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings
NVCF_POLL_SECONDS = 300  # Max time to poll for results
NVCF_POLL_INTERVAL = 2   # Poll every 2 seconds

# Sequence validation
ESMFOLD_RE_MAX_SEQ_LENGTH = 1024
ESMFOLD_RE_VALID_PATTERN = re.compile(r'^[ARNDCQEGHILKMFPSTWYVXBOU]+$')

logger = logging.getLogger(__name__)
WORKER_BASE_DIR = Path(__file__).resolve().parent
PRIVATE_KEY_FILE = "node_private_key.pem"


def _resolve_runtime_path(path_value: str) -> Path:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return WORKER_BASE_DIR / candidate


def _load_private_key(config_private_key: Optional[str]) -> Optional[str]:
    if config_private_key:
        return config_private_key
    env_key = os.environ.get("NODE_PRIVATE_KEY", "").strip()
    if env_key:
        return env_key
    key_path = _resolve_runtime_path(PRIVATE_KEY_FILE)
    if key_path.exists():
        try:
            return key_path.read_text(encoding='utf-8')
        except Exception as e:
            logger.warning(f"Could not read private key file {key_path}: {e}")
    try:
        from local_worker_keys import load_local_worker_private_key
        return load_local_worker_private_key()
    except Exception:
        return None

def get_job_queue_key(org_id: Optional[int] = None) -> str:
    """Get Redis job queue key for ESMFold-RE jobs."""
    if org_id:
        return f"queue:job:{WORKER_TYPE}:org:{org_id}"
    return f"queue:job:{WORKER_TYPE}"


def get_redis_connection(decode_responses: bool = False):
    """Get Redis connection using config or environment.

    Args:
        decode_responses: If False, returns binary data (for encrypted payloads)
    """
    if Config:
        return redis.Redis(
            host=Config.REDIS_HOST,
            port=Config.REDIS_PORT,
            db=Config.REDIS_DB,
            password=getattr(Config, 'REDIS_PASSWORD', None),
            decode_responses=decode_responses
        )
    return redis.Redis(
        host=os.getenv('REDIS_HOST', 'localhost'),
        port=int(os.getenv('REDIS_PORT', 6379)),
        db=int(os.getenv('REDIS_DB', 0)),
        password=os.getenv('REDIS_PASSWORD'),
        decode_responses=decode_responses
    )


def setup_logging(worker_id: str):
    """Configure logging for the worker."""
    logging.basicConfig(
        level=logging.DEBUG,
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (esmfold-re) - %(message)s',
        handlers=[
            logging.FileHandler(f'esmfold_re_worker_{worker_id}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)


def call_esmfold_re_api(sequence: str) -> Dict[str, Any]:
    """
    Call NVIDIA ESMFold-RE NIM API for protein structure prediction.

    Args:
        sequence: Protein amino acid sequence (max 1024 chars)
            Valid characters: ARNDCQEGHILKMFPSTWYVXBOU

    Returns:
        Dictionary with PDB structure prediction result

    Raises:
        Exception: On API error or timeout
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "sequence": sequence.upper()
    }

    response = requests.post(
        url=NVIDIA_ESMFOLD_RE_URL,
        headers=headers,
        json=payload,
        timeout=60
    )

    if response.status_code == 200:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        else:
            # Could be raw PDB text
            return {"pdb_content": response.text}

    elif response.status_code == 202:
        # Async task - poll for completion
        nvcf_reqid = response.headers.get("NVCF-REQID")
        if not nvcf_reqid:
            raise Exception("ESMFold-RE 202 response missing NVCF-REQID")

        logger.info(f"ESMFold-RE async task started: {nvcf_reqid}")
        return poll_nvcf_status(nvcf_reqid, headers)

    else:
        raise Exception(f"ESMFold-RE API error: {response.status_code} - {response.text[:500]}")


def poll_nvcf_status(task_id: str, headers: dict) -> Dict[str, Any]:
    """
    Poll NVCF status endpoint until task completes.

    Args:
        task_id: NVCF request ID
        headers: Authorization headers

    Returns:
        Dictionary with results
    """
    status_url = NVIDIA_STATUS_URL.format(task_id=task_id)
    start_time = time.time()

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)

            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return response.json()
                else:
                    return {"pdb_content": response.text[:50000]}

            elif response.status_code == 202:
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            elif response.status_code == 404:
                raise Exception(f"Task {task_id} not found")

            else:
                raise Exception(f"Status check error: {response.status_code}")

        except requests.exceptions.Timeout:
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Task {task_id} timed out after {NVCF_POLL_SECONDS} seconds")


def process_esmfold_re_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Process a single ESMFold-RE protein structure prediction job.

    Args:
        job: Job dictionary containing sequence and parameters
        worker_id: Worker identifier

    Returns:
        Result dictionary with PDB structure or error
    """
    job_id = job.get('job_id', 'unknown')
    result = {
        'job_id': job_id,
        'status': 'error',
        'model_used': 'esmfold-re',
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})
        sequence = params.get('sequence', '')

        if not sequence:
            result['message'] = "No protein sequence provided"
            return result

        # Clean and validate sequence
        sequence_upper = sequence.upper().replace(' ', '').replace('\n', '').replace('\r', '')

        if not ESMFOLD_RE_VALID_PATTERN.match(sequence_upper):
            result['message'] = "Invalid protein sequence: must contain only valid amino acid characters (ARNDCQEGHILKMFPSTWYVXBOU)"
            return result

        if len(sequence_upper) > ESMFOLD_RE_MAX_SEQ_LENGTH:
            result['message'] = f"Sequence too long: {len(sequence_upper)} residues (max {ESMFOLD_RE_MAX_SEQ_LENGTH})"
            return result

        logger.info(f"Job {job_id}: Predicting structure for sequence ({len(sequence_upper)} residues): {sequence_upper[:20]}...")

        # Call ESMFold-RE API
        api_result = call_esmfold_re_api(sequence=sequence_upper)

        execution_time = time.time() - start_time

        # Extract PDB content from result
        # API returns {"pdbs": ["...pdb content..."]} as a list
        pdbs = api_result.get('pdbs', [])
        if pdbs and isinstance(pdbs, list):
            pdb_content = pdbs[0] if pdbs[0] else ''
        else:
            pdb_content = api_result.get('pdb_content', '') or api_result.get('output', '') or api_result.get('structure', '')
        if not pdb_content:
            pdb_content = json.dumps(api_result, indent=2)

        # Extract pLDDT confidence scores from PDB B-factor column
        plddt_scores = []
        seen_residues = set()
        for line in pdb_content.split('\n'):
            if line.startswith('ATOM') and len(line) > 60:
                atom_name = line[12:16].strip()
                if atom_name == 'CA':
                    chain = line[21:22].strip()
                    res_num = line[22:26].strip()
                    key = (chain, res_num)
                    if key not in seen_residues:
                        try:
                            b_factor = float(line[60:66].strip())
                            plddt_scores.append(b_factor)
                            seen_residues.add(key)
                        except (ValueError, IndexError):
                            pass

        plddt_avg = round(sum(plddt_scores) / len(plddt_scores), 1) if plddt_scores else 0
        plddt_min = round(min(plddt_scores), 1) if plddt_scores else 0
        plddt_max = round(max(plddt_scores), 1) if plddt_scores else 0

        if plddt_scores:
            logger.info(f"Job {job_id}: Extracted {len(plddt_scores)} pLDDT scores, avg={plddt_avg}")

        # Get output filename from job output_path
        output_path = job.get('output_path', '')
        output_filename = os.path.basename(output_path) if output_path else f"{job_id}.pdb"

        # Encode output as base64 for the listener
        output_content_b64 = base64.b64encode(pdb_content.encode('utf-8')).decode('utf-8')

        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename,
            'sequence_length': len(sequence_upper),
            'plddt_scores': plddt_scores,
            'plddt_avg': plddt_avg,
            'plddt_min': plddt_min,
            'plddt_max': plddt_max,
            'confidence_score': plddt_avg,
            'message': None
        })

        logger.info(f"Job {job_id}: Structure prediction completed in {execution_time:.2f}s")

    except requests.exceptions.Timeout:
        result['message'] = "ESMFold-RE API request timed out"
        result['execution_time'] = time.time() - start_time
        logger.error(f"Job {job_id}: API timeout")
    except requests.exceptions.RequestException as e:
        result['message'] = f"Network error: {str(e)}"
        result['execution_time'] = time.time() - start_time
        logger.error(f"Job {job_id}: Network error - {e}")
    except Exception as e:
        result['message'] = f"Error: {str(e)}"
        result['execution_time'] = time.time() - start_time
        logger.error(f"Job {job_id}: Error - {e}", exc_info=True)

    return result


def send_heartbeat(worker_id: str, private_key: str, stop_event: threading.Event):
    """Periodically sends a signed heartbeat to Redis."""
    logger.info("Heartbeat thread started.")

    try:
        r = get_redis_connection()
        logger.debug(f"Successfully got Redis connection for worker {worker_id}")

        while not stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                # Direct Redis ping test
                r.ping()
                logger.debug(f"Redis ping successful for worker {worker_id}")

                heartbeat_data = {
                    'worker_id': worker_id,
                    'worker_type': WORKER_TYPE,
                    'timestamp': time.time()
                }
                logger.debug(f"Private key available for heartbeat: {bool(private_key)}")

                if private_key and canonical_dumps:
                    payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
                    signature = sign_message(private_key, payload_bytes)
                    if signature:
                        signed_message = {'payload': heartbeat_data, 'signature': signature}
                        r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(signed_message))
                        logger.debug(f"Published signed heartbeat for worker {worker_id}")
                else:
                    # Unsigned heartbeat for testing
                    r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))

            except Exception as e:
                logger.warning(f"Could not send heartbeat (or Redis ping failed): {e}")

        logger.info("Heartbeat thread stopping.")
    except Exception as e:
        logger.critical(f"FATAL: Heartbeat thread encountered an unhandled exception during Redis connection or initial setup: {e}", exc_info=True)


def worker_loop(
    worker_id: str,
    private_key: Optional[str],
    organization_id: Optional[int],
    stop_event: threading.Event
):
    """Main worker loop - fetches and processes ESMFold-RE jobs."""
    r = get_redis_connection(decode_responses=False)  # Binary mode for encrypted data
    r_text = get_redis_connection(decode_responses=True)  # Text mode for results
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"ESMFold-RE worker started. Listening on queue: {job_queue}")

    while not stop_event.is_set():
        try:
            # Block pop with 1 second timeout
            job_payload = r.brpop(job_queue, timeout=1)

            if not job_payload:
                continue

            _, encrypted_data = job_payload

            # Decrypt the job data if encryption is available
            if decrypt and isinstance(encrypted_data, bytes):
                try:
                    decrypted = decrypt(encrypted_data)
                    if decrypted:
                        job_data = decrypted.decode('utf-8')
                    else:
                        # Not encrypted, try as plain text
                        job_data = encrypted_data.decode('utf-8')
                except Exception as e:
                    logger.warning(f"Decryption failed, trying as plain text: {e}")
                    job_data = encrypted_data.decode('utf-8') if isinstance(encrypted_data, bytes) else encrypted_data
            else:
                job_data = encrypted_data.decode('utf-8') if isinstance(encrypted_data, bytes) else encrypted_data

            job = json.loads(job_data)

            job_id = job.get('job_id', 'unknown')
            logger.info(f"Received job: {job_id}")

            # Process the job
            result = process_esmfold_re_job(job, worker_id)

            # Send result back
            if send_job_result:
                send_job_result(result, private_key, r)
            else:
                # Fallback for testing - push to results queue
                result_json = json.dumps(result)
                if encrypt:
                    encrypted_result = encrypt(result_json.encode('utf-8'))
                    r.lpush(JOB_RESULTS_QUEUE, encrypted_result)
                else:
                    r_text.lpush(JOB_RESULTS_QUEUE, result_json)

            logger.info(f"Job {job_id} completed with status: {result['status']}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid job JSON: {e}")
        except Exception as e:
            logger.error(f"Worker loop error: {e}", exc_info=True)
            time.sleep(1)

    logger.info("Worker loop stopped.")


def load_node_config() -> Dict:
    """Load node configuration from file."""
    config_path = _resolve_runtime_path(NODE_CONFIG_FILE)
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def main():
    """Main entry point for ESMFold-RE worker."""
    global NODE_CONFIG_FILE
    global logger

    parser = argparse.ArgumentParser(description='ESMFold-RE Protein Structure Prediction Worker')
    parser.add_argument('--worker-id', type=str, help='Worker ID')
    parser.add_argument('--org-id', type=int, help='Organization ID for private queue')
    parser.add_argument('--config', type=str, default=NODE_CONFIG_FILE, help='Config file path')
    args = parser.parse_args()

    # Load configuration
    NODE_CONFIG_FILE = args.config
    config = load_node_config()

    worker_id = args.worker_id or config.get('worker_id') or f'{WORKER_TYPE}-worker-1'
    organization_id = args.org_id or config.get('organization_id')
    private_key = _load_private_key(config.get('private_key'))

    # Setup logging
    _logger_instance = setup_logging(worker_id)
    logger = _logger_instance

    logger.info("=" * 60)
    logger.info("ESMFold-RE Protein Structure Prediction Worker Starting")
    logger.info(f"Worker ID: {worker_id}")
    logger.info(f"Organization ID: {organization_id or 'None (public)'}")
    logger.info(f"API URL: {NVIDIA_ESMFOLD_RE_URL}")
    logger.info("=" * 60)

    # Create stop event
    stop_event = threading.Event()

    # Start heartbeat thread
    heartbeat_thread = threading.Thread(
        target=send_heartbeat,
        args=(worker_id, private_key, stop_event),
        daemon=True
    )
    heartbeat_thread.start()

    try:
        # Run main worker loop
        worker_loop(worker_id, private_key, organization_id, stop_event)
    except KeyboardInterrupt:
        logger.info("Shutdown requested...")
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=5)
        logger.info("ESMFold-RE worker shutdown complete.")


# Standalone test function
def test_esmfold_re_api():
    """Test the ESMFold-RE API with a sample sequence."""
    print("Testing ESMFold-RE API...")

    test_sequence = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSH"

    try:
        result = call_esmfold_re_api(sequence=test_sequence)

        print("Success!")
        print(f"Input sequence: {test_sequence}")

        pdb_content = result.get('pdb_content', '')
        if pdb_content:
            print(f"PDB output length: {len(pdb_content)} chars")
            print(f"PDB first 200 chars: {pdb_content[:200]}")
        else:
            print(f"Result keys: {list(result.keys())}")

        # Save full result
        output_file = Path("esmfold_re_test_output.json")
        output_file.write_text(json.dumps(result, indent=2))
        print(f"Full result saved to: {output_file}")

    except Exception as e:
        print(f"Error: {e}")
        return False

    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        # Run API test
        test_esmfold_re_api()
    else:
        # Run as worker
        main()
