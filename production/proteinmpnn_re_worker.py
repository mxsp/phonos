#!/usr/bin/env python3
"""
ProteinMPNN-RE Protein Sequence Design Worker Node
Connects to NVIDIA's ProteinMPNN NIM API for inverse folding / sequence design.
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
from typing import Optional, Dict, Any, List
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
NVIDIA_PROTEINMPNN_URL = os.getenv(
    "PROTEINMPNN_RE_API_URL",
    "https://health.api.nvidia.com/v1/biology/ipd/proteinmpnn/predict"
)
NVIDIA_STATUS_URL = os.getenv(
    "PROTEINMPNN_RE_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "proteinmpnn-re"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_proteinmpnn_re.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings
NVCF_POLL_SECONDS = 300
NVCF_POLL_INTERVAL = 2

# PDB ATOM record pattern
PDB_ATOM_PATTERN = re.compile(r'^ATOM\s+', re.MULTILINE)

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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (proteinmpnn-re) - %(message)s',
        handlers=[
            logging.FileHandler(f'proteinmpnn_re_worker_{worker_id}.log'),
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
                    return {"mfasta": response.text}

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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _request_proteinmpnn(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    response = requests.post(
        url=NVIDIA_PROTEINMPNN_URL,
        headers=headers,
        json=payload,
        timeout=120
    )

    if response.status_code == 200:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        return {"mfasta": response.text}

    if response.status_code == 202:
        nvcf_reqid = response.headers.get("NVCF-REQID")
        if not nvcf_reqid:
            raise Exception("ProteinMPNN-RE 202 response missing NVCF-REQID")
        logger.info(f"ProteinMPNN-RE async task started: {nvcf_reqid}")
        return poll_nvcf_status(nvcf_reqid, headers)

    raise Exception(f"ProteinMPNN-RE API error: {response.status_code} - {response.text[:500]}")


def call_proteinmpnn_api(input_pdb: str, params: dict) -> Dict[str, Any]:
    """
    Call NVIDIA ProteinMPNN NIM API.

    Exact NVIDIA API parameters:
        input_pdb: PDB structure content (required)
        input_pdb_chains: list of chain IDs to design (optional, defaults to all)
        ca_only: bool (default false)
        use_soluble_model: bool (default false)
        num_seq_per_target: 1-100 (default 1)
        sampling_temp: array of floats 0-1 exclusive (default [0.1])
        seed: int (optional)
        pssm_multi: float 0-1 (default 0)
        pssm_threshold: float (default 0)
        pssm_bias_flag: bool (default false)
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "NVCF-POLL-SECONDS": str(NVCF_POLL_SECONDS),
    }

    # Parse sampling_temp
    sampling_temp_raw = params.get('sampling_temp', [0.1])
    if isinstance(sampling_temp_raw, list):
        sampling_temp = [max(0.001, min(float(t), 0.999)) for t in sampling_temp_raw]
    elif isinstance(sampling_temp_raw, str):
        sampling_temp = [max(0.001, min(float(t.strip()), 0.999)) for t in sampling_temp_raw.split(',') if t.strip()]
    else:
        sampling_temp = [max(0.001, min(float(sampling_temp_raw), 0.999))]
    if not sampling_temp:
        sampling_temp = [0.1]

    # Parse chain selection (new key: input_pdb_chains; legacy alias: pdb_path_chains)
    chains_raw = params.get('input_pdb_chains', params.get('pdb_path_chains', []))
    input_pdb_chains = None
    if chains_raw:
        if isinstance(chains_raw, list):
            input_pdb_chains = [c.strip() for c in chains_raw if c.strip()]
        elif isinstance(chains_raw, str):
            input_pdb_chains = [c.strip() for c in chains_raw.split(',') if c.strip()]

    requested_num_seq = min(max(1, int(params.get('num_seq_per_target', 1))), 100)
    payload = {
        "input_pdb": input_pdb,
        "ca_only": bool(params.get('ca_only', False)),
        "use_soluble_model": bool(params.get('use_soluble_model', False)),
        "num_seq_per_target": requested_num_seq,
        "sampling_temp": sampling_temp,
        "pssm_multi": float(params.get('pssm_multi', 0)),
        "pssm_threshold": float(params.get('pssm_threshold', 0)),
        "pssm_bias_flag": bool(params.get('pssm_bias_flag', False)),
    }

    if input_pdb_chains:
        payload["input_pdb_chains"] = input_pdb_chains

    seed = params.get('seed')
    if seed is not None:
        payload["seed"] = int(seed)

    degraded_batch = _as_bool(params.get('degraded_batch', False))
    degraded_batch_size = min(max(1, int(params.get('degraded_batch_size', 8))), 20)
    if not degraded_batch or requested_num_seq <= degraded_batch_size:
        return _request_proteinmpnn(payload, headers)

    logger.info(
        f"ProteinMPNN-RE degraded batch enabled: splitting {requested_num_seq} sequences into chunks of {degraded_batch_size}"
    )

    remaining = requested_num_seq
    request_index = 0
    merged_fastas: List[str] = []
    merged_scores: List[Any] = []

    while remaining > 0:
        chunk_count = min(degraded_batch_size, remaining)
        chunk_payload = dict(payload)
        chunk_payload["num_seq_per_target"] = chunk_count
        if seed is not None:
            chunk_payload["seed"] = int(seed) + request_index

        chunk_result = _request_proteinmpnn(chunk_payload, headers)
        chunk_fasta = str(chunk_result.get("mfasta", "") or "").strip()
        if chunk_fasta:
            merged_fastas.append(chunk_fasta)
        chunk_scores = chunk_result.get("scores", [])
        if isinstance(chunk_scores, list):
            merged_scores.extend(chunk_scores)

        remaining -= chunk_count
        request_index += 1

    merged_result: Dict[str, Any] = {
        "mfasta": ("\n".join(merged_fastas).strip() + "\n") if merged_fastas else ""
    }
    if merged_scores:
        merged_result["scores"] = merged_scores
    merged_result["degraded_batch"] = True
    merged_result["degraded_batch_calls"] = request_index
    return merged_result


def process_proteinmpnn_job(job: Dict, worker_id: str) -> Dict[str, Any]:
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

        # Get PDB content — could be in params or job
        input_pdb = params.get('input_pdb', '') or params.get('pdb_content', '')
        if not input_pdb:
            result['message'] = "No PDB structure provided (input_pdb is required)"
            return result

        # Validate PDB has ATOM records
        if not PDB_ATOM_PATTERN.search(input_pdb):
            result['message'] = "Invalid PDB structure: no ATOM records found"
            return result

        if len(input_pdb) > 10_000_000:
            result['message'] = f"PDB structure too large: {len(input_pdb)} bytes (max 10 MB)"
            return result

        raw_chain_param = params.get('input_pdb_chains', params.get('pdb_path_chains', []))
        if isinstance(raw_chain_param, str):
            chains_str = raw_chain_param or 'all'
        elif isinstance(raw_chain_param, list) and raw_chain_param:
            chains_str = ','.join(raw_chain_param)
        else:
            chains_str = 'all'
        num_seq = params.get('num_seq_per_target', 1)
        degraded_batch = _as_bool(params.get('degraded_batch', False))
        logger.info(
            f"Job {job_id}: Designing sequences for chains={chains_str}, num_seq={num_seq}, "
            f"pdb_len={len(input_pdb)}, degraded_batch={degraded_batch}"
        )

        api_result = call_proteinmpnn_api(input_pdb, params)

        execution_time = time.time() - start_time

        # Extract FASTA output from response
        fasta_content = api_result.get('mfasta', '')
        if not fasta_content:
            result['message'] = (
                "ProteinMPNN API returned no sequence data. "
                f"Response keys: {list(api_result.keys())}"
            )
            return result

        output_path = job.get('output_path', '')
        output_filename = os.path.basename(output_path) if output_path else f"{job_id}.fasta"

        output_content_b64 = base64.b64encode(fasta_content.encode('utf-8')).decode('utf-8')

        scores = api_result.get('scores', [])
        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename,
            'num_sequences': len([l for l in fasta_content.split('\n') if l.startswith('>')]),
            'scores': scores,
            'degraded_batch': bool(api_result.get('degraded_batch', False)),
            'degraded_batch_calls': api_result.get('degraded_batch_calls'),
            'message': None
        })

        logger.info(f"Job {job_id}: Completed in {execution_time:.2f}s, {len(scores)} sequences scored")

    except requests.exceptions.Timeout:
        result['message'] = "ProteinMPNN-RE API request timed out"
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

    logger.info(f"ProteinMPNN-RE worker started. Listening on queue: {job_queue}")

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

            result = process_proteinmpnn_job(job, worker_id)

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
    parser = argparse.ArgumentParser(description='ProteinMPNN-RE Worker Node')
    parser.add_argument('--worker-id', type=str, default=None)
    parser.add_argument('--private-key', type=str, default=None)
    parser.add_argument('--org-id', type=int, default=None)
    parser.add_argument('--test', action='store_true', help='Run a quick test and exit')
    args = parser.parse_args()

    if args.test:
        print("ProteinMPNN-RE test: requires a valid PDB file. Pass --test with a PDB path.")
        return

    node_config = load_node_config()
    worker_id = args.worker_id or node_config.get('worker_id') or f"{WORKER_TYPE}-worker-1"
    private_key = _load_private_key(node_config.get('private_key'), args.private_key)
    org_id = args.org_id or node_config.get('org_id')

    global logger
    logger = setup_logging(worker_id)
    logger.info(f"Starting ProteinMPNN-RE worker: {worker_id}")

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
        logger.info("ProteinMPNN-RE worker shut down.")


if __name__ == '__main__':
    main()
