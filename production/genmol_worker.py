#!/usr/bin/env python3
"""
GenMol Molecule Generation Worker Node
Connects to NVIDIA's GenMol NIM API for fragment-based / SAFE molecule generation.

GenMol is a masked-diffusion generative chemistry model that completes masked
SAFE sequences (or plain SMILES) into novel, valid molecules ranked by a scoring
function (QED or LogP). Mirrors the MolMIM worker: synchronous 200 responses are
returned directly, async 202 responses are long-polled via NVCF.
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
    Config = None
    decrypt = None
    encrypt = None

# Configuration
NVIDIA_GENMOL_URL = os.getenv(
    "GENMOL_API_URL",
    "https://health.api.nvidia.com/v1/biology/nvidia/genmol/generate"
)
NVIDIA_STATUS_URL = os.getenv(
    "GENMOL_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "genmol"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_genmol.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings
NVCF_POLL_SECONDS = 300
NVCF_POLL_INTERVAL = 2

# Valid NVIDIA API parameter sets
VALID_SCORING = {'QED', 'LogP'}

logger = logging.getLogger(__name__)
WORKER_BASE_DIR = Path(__file__).resolve().parent
PRIVATE_KEY_FILE = "node_private_key.pem"


def _resolve_runtime_path(path_value: str) -> Path:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return WORKER_BASE_DIR / candidate


def _load_private_key(config_private_key: Optional[str], arg_private_key: Optional[str]) -> Optional[str]:
    if arg_private_key:
        return arg_private_key
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
    if org_id:
        return f"queue:job:{WORKER_TYPE}:org:{org_id}"
    return f"queue:job:{WORKER_TYPE}"


def get_redis_connection(decode_responses: bool = False):
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
    logging.basicConfig(
        level=logging.DEBUG,
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (genmol) - %(message)s',
        handlers=[
            logging.FileHandler(f'genmol_worker_{worker_id}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)


def poll_nvcf_status(task_id: str, headers: dict) -> Dict[str, Any]:
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
                    return {"raw_output": response.text[:50000]}

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


def call_genmol_api(params: dict) -> Dict[str, Any]:
    """
    Call NVIDIA GenMol NIM API for molecule generation.

    Params follow the exact NVIDIA API spec:
        smiles: Input SMILES or SAFE sequence with masking segments (required)
        num_molecules: 1-1000 (default 30)
        temperature: softmax sampling scaling factor, 0.01-10 (default 1)
        noise: noise factor for top-k sampling, 0-2 (default 1)
        step_size: masking tokens recovered per inference, 1-10 (default 1)
        scoring: QED | LogP (default QED)
        unique: return unique molecules only (default False)
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    smiles = params.get('smiles', '') or params.get('smi', '')

    scoring = params.get('scoring', 'QED')
    if scoring not in VALID_SCORING:
        scoring = 'QED'

    # temperature and noise are strings per the NVIDIA spec; clamp to valid ranges
    try:
        temperature = float(params.get('temperature', 1))
        temperature = max(0.01, min(temperature, 10.0))
    except (TypeError, ValueError):
        temperature = 1.0

    try:
        noise = float(params.get('noise', 1))
        noise = max(0.0, min(noise, 2.0))
    except (TypeError, ValueError):
        noise = 1.0

    payload = {
        "smiles": smiles,
        "num_molecules": min(max(1, int(params.get('num_molecules', 30))), 1000),
        "temperature": str(temperature),
        "noise": str(noise),
        "step_size": min(max(1, int(params.get('step_size', 1))), 10),
        "scoring": scoring,
        "unique": bool(params.get('unique', False)),
    }

    response = requests.post(
        url=NVIDIA_GENMOL_URL,
        headers=headers,
        json=payload,
        timeout=120
    )

    if response.status_code == 200:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        else:
            return {"raw_output": response.text}

    elif response.status_code == 202:
        nvcf_reqid = response.headers.get("NVCF-REQID")
        if not nvcf_reqid:
            raise Exception("GenMol 202 response missing NVCF-REQID")
        logger.info(f"GenMol async task started: {nvcf_reqid}")
        return poll_nvcf_status(nvcf_reqid, headers)

    else:
        raise Exception(f"GenMol API error: {response.status_code} - {response.text[:500]}")


def process_genmol_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    job_id = job.get('job_id', 'unknown')
    result = {
        'job_id': job_id,
        'status': 'error',
        'model_used': WORKER_TYPE,
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})
        smiles = (params.get('smiles', '') or params.get('smi', '')).strip()

        if not smiles:
            result['message'] = "No SMILES/SAFE string provided (smiles is required)"
            return result

        if len(smiles) > 2000:
            result['message'] = f"SMILES string too long: {len(smiles)} chars (max 2000)"
            return result

        logger.info(f"Job {job_id}: Generating molecules from input ({len(smiles)} chars)")

        api_result = call_genmol_api(params)

        execution_time = time.time() - start_time

        # Normalize: GenMol returns 'molecules': [{'smiles','score'}, ...]
        molecules = api_result.get('molecules', [])
        if not molecules and 'generated' in api_result:
            generated = api_result['generated']
            if isinstance(generated, list):
                molecules = [
                    {'smiles': s, 'score': None} if isinstance(s, str) else s
                    for s in generated
                ]
                api_result['molecules'] = molecules

        output_content = json.dumps(api_result, indent=2)
        output_path = job.get('output_path', '')
        output_filename = os.path.basename(output_path) if output_path else f"{job_id}.json"
        output_content_b64 = base64.b64encode(output_content.encode('utf-8')).decode('utf-8')

        num_molecules = len(molecules)
        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename,
            'num_molecules_generated': num_molecules,
            'message': None
        })

        logger.info(f"Job {job_id}: Completed in {execution_time:.2f}s, generated {num_molecules} molecules")

    except requests.exceptions.Timeout:
        result['message'] = "GenMol API request timed out"
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
    logger.info("Heartbeat thread started.")
    try:
        r = get_redis_connection()
        while not stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                r.ping()
                heartbeat_data = {
                    'worker_id': worker_id,
                    'worker_type': WORKER_TYPE,
                    'timestamp': time.time()
                }
                if private_key and canonical_dumps:
                    payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
                    signature = sign_message(private_key, payload_bytes)
                    if signature:
                        signed_message = {'payload': heartbeat_data, 'signature': signature}
                        r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(signed_message))
                else:
                    r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))
            except Exception as e:
                logger.warning(f"Could not send heartbeat: {e}")
        logger.info("Heartbeat thread stopping.")
    except Exception as e:
        logger.critical(f"Heartbeat thread fatal error: {e}", exc_info=True)


def worker_loop(worker_id: str, private_key: Optional[str], organization_id: Optional[int], stop_event: threading.Event):
    r = get_redis_connection(decode_responses=False)
    r_text = get_redis_connection(decode_responses=True)
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"GenMol worker started. Listening on queue: {job_queue}")

    while not stop_event.is_set():
        try:
            job_payload = r.brpop(job_queue, timeout=1)
            if not job_payload:
                continue

            _, encrypted_data = job_payload

            if decrypt and isinstance(encrypted_data, bytes):
                try:
                    decrypted = decrypt(encrypted_data)
                    job_data = decrypted.decode('utf-8') if decrypted else encrypted_data.decode('utf-8')
                except Exception as e:
                    logger.warning(f"Decryption failed, trying as plain text: {e}")
                    job_data = encrypted_data.decode('utf-8') if isinstance(encrypted_data, bytes) else encrypted_data
            else:
                job_data = encrypted_data.decode('utf-8') if isinstance(encrypted_data, bytes) else encrypted_data

            job = json.loads(job_data)
            job_id = job.get('job_id', 'unknown')
            logger.info(f"Received job: {job_id}")

            result = process_genmol_job(job, worker_id)

            if send_job_result:
                send_job_result(result, private_key, r)
            else:
                result_json = json.dumps(result)
                if encrypt:
                    r.lpush(JOB_RESULTS_QUEUE, encrypt(result_json.encode('utf-8')))
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
    config_path = _resolve_runtime_path(NODE_CONFIG_FILE)
    if config_path.exists():
        try:
            with open(config_path, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading node config: {e}")
    return {}


def main():
    parser = argparse.ArgumentParser(description='GenMol Worker Node')
    parser.add_argument('--worker-id', type=str, default=None, help='Worker ID (auto-generated if not provided)')
    parser.add_argument('--private-key', type=str, default=None, help='Private key for signing heartbeats')
    parser.add_argument('--org-id', type=int, default=None, help='Organization ID for job queue filtering')
    parser.add_argument('--test', action='store_true', help='Run a test generation and exit')
    args = parser.parse_args()

    if args.test:
        print("Running GenMol test generation (default SAFE seed)...")
        test_params = {
            'smiles': 'C124CN3C1.S3(=O)(=O)CC.C4C#N.[*{20-20}]',
            'num_molecules': 5,
            'temperature': 1,
            'noise': 1,
            'step_size': 1,
            'scoring': 'QED',
            'unique': True,
        }
        try:
            result = call_genmol_api(test_params)
            mols = result.get('molecules', [])
            print(f"Test successful! Generated {len(mols)} molecules")
            print(json.dumps(result, indent=2)[:1000])
        except Exception as e:
            print(f"Test failed: {e}")
        return

    # Load config and setup
    node_config = load_node_config()
    worker_id = args.worker_id or node_config.get('worker_id') or f"{WORKER_TYPE}-worker-1"
    private_key = _load_private_key(node_config.get('private_key'), args.private_key)
    org_id = args.org_id or node_config.get('org_id')

    global logger
    logger = setup_logging(worker_id)
    logger.info(f"Starting GenMol worker: {worker_id}")

    stop_event = threading.Event()

    heartbeat_thread = threading.Thread(
        target=send_heartbeat,
        args=(worker_id, private_key, stop_event),
        daemon=True
    )
    heartbeat_thread.start()

    try:
        worker_loop(worker_id, private_key, org_id, stop_event)
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt. Shutting down...")
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=5)
        logger.info("GenMol worker shut down.")


if __name__ == '__main__':
    main()
