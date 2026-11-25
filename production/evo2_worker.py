#!/usr/bin/env python3
"""
Evo2 DNA Generation Worker Node
Connects to NVIDIA's Evo2-40B model API to generate DNA sequences.
"""
import argparse
import base64
import json
import time
import os
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
NVIDIA_EVO2_URL = os.getenv(
    "EVO2_API_URL",
    "https://health.api.nvidia.com/v1/biology/arc/evo2-40b/generate"
)
NVIDIA_STATUS_URL = os.getenv(
    "EVO2_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "evo2"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_evo2.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings
try:
    NVCF_POLL_SECONDS = max(60, int(os.getenv("EVO2_POLL_SECONDS", "900")))
except (TypeError, ValueError):
    NVCF_POLL_SECONDS = 900

try:
    NVCF_POLL_INTERVAL = max(1, int(os.getenv("EVO2_POLL_INTERVAL", "3")))
except (TypeError, ValueError):
    NVCF_POLL_INTERVAL = 3

try:
    EVO2_STATUS_REQUEST_TIMEOUT = max(10, int(os.getenv("EVO2_STATUS_REQUEST_TIMEOUT", "30")))
except (TypeError, ValueError):
    EVO2_STATUS_REQUEST_TIMEOUT = 30

# Initial API request timeout/retry settings
try:
    EVO2_INITIAL_CONNECT_TIMEOUT = max(5, int(os.getenv("EVO2_INITIAL_CONNECT_TIMEOUT", "20")))
except (TypeError, ValueError):
    EVO2_INITIAL_CONNECT_TIMEOUT = 20

try:
    EVO2_INITIAL_READ_TIMEOUT = max(30, int(os.getenv("EVO2_INITIAL_READ_TIMEOUT", "300")))
except (TypeError, ValueError):
    EVO2_INITIAL_READ_TIMEOUT = 300

try:
    EVO2_INITIAL_REQUEST_RETRIES = max(1, int(os.getenv("EVO2_INITIAL_REQUEST_RETRIES", "3")))
except (TypeError, ValueError):
    EVO2_INITIAL_REQUEST_RETRIES = 3

try:
    EVO2_INITIAL_RETRY_BACKOFF_SECONDS = max(
        0.5, float(os.getenv("EVO2_INITIAL_RETRY_BACKOFF_SECONDS", "2.0"))
    )
except (TypeError, ValueError):
    EVO2_INITIAL_RETRY_BACKOFF_SECONDS = 2.0

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
    """Get Redis job queue key for Evo2 jobs."""
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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (evo2) - %(message)s',
        handlers=[
            logging.FileHandler(f'evo2_worker_{worker_id}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)


def call_evo2_api(
    sequence: str,
    num_tokens: int = 128,
    temperature: float = 0.7,
    top_k: int = 3,
    top_p: float = 0.0,
    enable_sampled_probs: bool = False,
    enable_logits: bool = False,
    enable_elapsed_ms_per_token: bool = False,
    random_seed: Optional[int] = None
) -> Dict[str, Any]:
    """
    Call NVIDIA Evo2-40B DNA generation API.

    Args:
        sequence: Input DNA sequence (seed sequence)
        num_tokens: Number of nucleotides to generate (1-1200)
        temperature: Sampling temperature (0-1.3)
        top_k: Top-k sampling parameter (0-6)
        top_p: Nucleus sampling parameter (0-1)
        enable_sampled_probs: Whether to return sampling probabilities
        enable_logits: Whether to include output logits
        enable_elapsed_ms_per_token: Whether to include per-token latency metrics
        random_seed: Optional deterministic seed

    Returns:
        Dictionary with generated sequence and optional probabilities
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    enable_sampled_probs = bool(
        enable_sampled_probs or
        enable_logits or
        enable_elapsed_ms_per_token
    )

    payload = {
        "sequence": sequence.upper(),
        "num_tokens": min(max(1, num_tokens), 1200),
        "temperature": min(max(0.01, temperature), 1.3),
        "top_k": min(max(0, top_k), 6),
        "top_p": min(max(0, top_p), 1.0),
        "enable_sampled_probs": enable_sampled_probs,
        "enable_logits": bool(enable_logits),
        "enable_elapsed_ms_per_token": bool(enable_elapsed_ms_per_token),
    }
    if random_seed is not None:
        payload["random_seed"] = min(max(0, int(random_seed)), 4294967295)

    response = None
    for attempt in range(1, EVO2_INITIAL_REQUEST_RETRIES + 1):
        try:
            response = requests.post(
                url=NVIDIA_EVO2_URL,
                headers=headers,
                json=payload,
                timeout=(EVO2_INITIAL_CONNECT_TIMEOUT, EVO2_INITIAL_READ_TIMEOUT)
            )
            break
        except requests.exceptions.Timeout:
            logger.warning(
                "Initial Evo2 request timed out "
                f"(attempt {attempt}/{EVO2_INITIAL_REQUEST_RETRIES}, "
                f"connect={EVO2_INITIAL_CONNECT_TIMEOUT}s, read={EVO2_INITIAL_READ_TIMEOUT}s)"
            )
            if attempt >= EVO2_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(EVO2_INITIAL_RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.RequestException as req_err:
            logger.warning(
                "Initial Evo2 request failed "
                f"(attempt {attempt}/{EVO2_INITIAL_REQUEST_RETRIES}): {req_err}"
            )
            if attempt >= EVO2_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(EVO2_INITIAL_RETRY_BACKOFF_SECONDS * attempt)

    if response is None:
        raise requests.exceptions.Timeout("Evo2 initial request did not return a response")

    if response.status_code == 200:
        return parse_evo2_response(response)

    if response.status_code == 202:
        task_id = response.headers.get("NVCF-REQID")
        if not task_id:
            raise Exception("Evo2 202 response missing NVCF-REQID")
        logger.info("Evo2 async task started: %s", task_id)
        return poll_nvcf_status(task_id, headers)

    raise Exception(f"Evo2 API error: {response.status_code} - {response.text[:500]}")


def parse_evo2_response(response: requests.Response) -> Dict[str, Any]:
    """Parse Evo2 response payload in JSON or zip forms."""
    content_type = response.headers.get("Content-Type", "").lower()

    if "application/json" in content_type:
        return response.json()

    if "application/zip" in content_type or "application/octet-stream" in content_type:
        return {
            "sequence": None,
            "zip_data_b64": base64.b64encode(response.content).decode("utf-8"),
            "zip_filename": "evo2_output.zip",
            "message": "Large response returned as zip"
        }

    raise Exception(f"Unexpected Evo2 response type: {content_type}")


def poll_nvcf_status(task_id: str, request_headers: Dict[str, str]) -> Dict[str, Any]:
    """Poll NVIDIA NVCF status endpoint for async Evo2 tasks."""
    status_url = NVIDIA_STATUS_URL.format(task_id=task_id)
    headers = {
        "Authorization": request_headers.get("Authorization", ""),
        "Accept": "application/json"
    }
    start_time = time.time()

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            response = requests.get(status_url, headers=headers, timeout=EVO2_STATUS_REQUEST_TIMEOUT)

            if response.status_code == 200:
                return parse_evo2_response(response)

            if response.status_code == 202:
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            if response.status_code == 404:
                raise Exception(f"Evo2 async task {task_id} not found")

            if response.status_code >= 500:
                logger.warning(
                    "Evo2 status polling got %s for task %s, retrying...",
                    response.status_code,
                    task_id
                )
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            raise Exception(f"Evo2 status error: {response.status_code} - {response.text[:500]}")
        except requests.exceptions.Timeout:
            logger.warning("Evo2 status polling timeout for task %s, retrying...", task_id)
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Evo2 async task timed out after {NVCF_POLL_SECONDS}s")


def process_evo2_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Process a single Evo2 DNA generation job.

    Args:
        job: Job dictionary containing sequence and parameters
        worker_id: Worker identifier

    Returns:
        Result dictionary with generated sequence or error
    """
    job_id = job.get('job_id', 'unknown')
    result = {
        'job_id': job_id,
        'status': 'error',
        'model_used': 'evo2-40b',
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})
        sequence = params.get('sequence', '')

        if not sequence:
            result['message'] = "No input sequence provided"
            return result

        # Validate sequence contains only valid nucleotides
        valid_nucleotides = set('ATCGN')
        sequence_upper = sequence.upper()
        if not all(c in valid_nucleotides for c in sequence_upper):
            result['message'] = "Invalid sequence: must contain only A, T, C, G, N"
            return result

        # Extract generation parameters
        num_tokens = int(params.get('num_tokens', 128))
        temperature = float(params.get('temperature', 0.7))
        top_k = int(params.get('top_k', 3))
        top_p = float(params.get('top_p', 0.0))
        enable_probs = True  # Always enable for confidence visualization
        enable_logits = bool(params.get('enable_logits', False))
        enable_elapsed_ms_per_token = bool(params.get('enable_elapsed_ms_per_token', False))
        random_seed = params.get('random_seed')
        if random_seed in ('', 'null'):
            random_seed = None
        if random_seed is not None:
            random_seed = int(random_seed)

        logger.info(f"Job {job_id}: Generating {num_tokens} nucleotides from seed: {sequence[:20]}...")

        # Call Evo2 API
        api_result = call_evo2_api(
            sequence=sequence_upper,
            num_tokens=num_tokens,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            enable_sampled_probs=enable_probs,
            enable_logits=enable_logits,
            enable_elapsed_ms_per_token=enable_elapsed_ms_per_token,
            random_seed=random_seed
        )

        execution_time = time.time() - start_time

        # Prepare output in JSON format
        output_data = json.dumps(api_result, indent=2)

        # Get output filename from job output_path
        output_path = job.get('output_path', '')
        output_filename = os.path.basename(output_path) if output_path else f"{job_id}.json"

        # Encode output as base64 for the listener
        output_content_b64 = base64.b64encode(output_data.encode('utf-8')).decode('utf-8')

        # Build artifacts list
        artifacts = []
        raw_seq = api_result.get('sequence', '')
        if raw_seq:
            def _make_fasta(header, seq):
                wrapped = '\n'.join(seq[i:i+80] for i in range(0, len(seq), 80))
                return f">{header}\n{wrapped}\n"

            # Determine whether the API returned full (input+generated) or just generated tokens
            if raw_seq.upper().startswith(sequence_upper) and len(raw_seq) > len(sequence):
                combined = raw_seq
                gen_only = raw_seq[len(sequence):]
            else:
                # API returned only generated tokens — prepend input
                combined = sequence_upper + raw_seq.upper()
                gen_only = raw_seq

            # Full sequence FASTA (input seed + generated output)
            full_fasta = _make_fasta(f"evo2_full_{job_id} input+generated len={len(combined)}", combined)
            artifacts.append({
                'filename': f"{job_id}_full.fasta",
                'display_name': 'Full Sequence - Input + Output (FASTA)',
                'content_b64': base64.b64encode(full_fasta.encode('utf-8')).decode('utf-8'),
                'type': 'sequence',
            })

            # Generated-only portion FASTA
            gen_fasta = _make_fasta(f"evo2_generated_{job_id} len={len(gen_only)}", gen_only)
            artifacts.append({
                'filename': f"{job_id}_generated.fasta",
                'display_name': 'Generated Portion Only (FASTA)',
                'content_b64': base64.b64encode(gen_fasta.encode('utf-8')).decode('utf-8'),
                'type': 'sequence',
            })
        else:
            combined = ''

        # Build metrics for result_summary (stored in job metadata by listener)
        sampled_probs = api_result.get('sampled_probs', []) if enable_probs else []
        gen_length = len(combined) - len(sequence) if len(combined) > len(sequence) else len(combined)
        metrics = {
            'sequence': combined,
            'sampled_probs': sampled_probs,
            'input_length': len(sequence),
            'generated_length': gen_length,
            'elapsed_ms': api_result.get('elapsed_ms_per_token', []),
        }
        if sampled_probs:
            avg_prob = sum(sampled_probs) / len(sampled_probs)
            metrics['avg_confidence'] = round(avg_prob * 100, 1)
            metrics['min_confidence'] = round(min(sampled_probs) * 100, 1)
            metrics['max_confidence'] = round(max(sampled_probs) * 100, 1)

        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename,
            'generated_sequence': combined,
            'sampled_probs': sampled_probs,
            'artifacts': artifacts,
            'metrics': metrics,
            'message': None
        })

        logger.info(f"Job {job_id}: Generation completed in {execution_time:.2f}s")

    except requests.exceptions.Timeout:
        result['message'] = "Evo2 API request timed out"
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
    """Main worker loop - fetches and processes Evo2 jobs."""
    r = get_redis_connection(decode_responses=False)  # Binary mode for encrypted data
    r_text = get_redis_connection(decode_responses=True)  # Text mode for results
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"Evo2 worker started. Listening on queue: {job_queue}")

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
            result = process_evo2_job(job, worker_id)

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
    """Main entry point for Evo2 worker."""
    global NODE_CONFIG_FILE
    global logger # Moved to the beginning

    parser = argparse.ArgumentParser(description='Evo2 DNA Generation Worker')
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
    logger.info(f"Evo2 DNA Generation Worker Starting")
    logger.info(f"Worker ID: {worker_id}")
    logger.info(f"Organization ID: {organization_id or 'None (public)'}")
    logger.info(f"API URL: {NVIDIA_EVO2_URL}")
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
        logger.info("Evo2 worker shutdown complete.")


# Standalone test function
def test_evo2_api():
    """Test the Evo2 API with a sample sequence."""
    print("Testing Evo2 API...")

    test_sequence = "ACTGACTGACTGACTG"

    try:
        result = call_evo2_api(
            sequence=test_sequence,
            num_tokens=32,
            temperature=0.7,
            top_k=4,
            enable_sampled_probs=True
        )

        print(f"Success!")
        print(f"Input sequence: {test_sequence}")
        print(f"Generated sequence: {result.get('sequence', 'N/A')[:100]}...")

        probs = result.get('sampled_probs', [])
        if probs:
            print(f"Sampled probs (first 5): {probs[:5]}")

        # Save full result
        output_file = Path("evo2_test_output.json")
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
        test_evo2_api()
    else:
        # Run as worker
        main()
