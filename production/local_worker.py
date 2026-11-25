import argparse
import json
import time
import os
import torch
import logging
import gc
import threading
import queue
import redis
from typing import Optional, Tuple, Any, Callable

from encryption_util import canonical_dumps

# Mock implementations for models that aren't fully integrated
# In a real scenario, these would be proper implementations.
def mock_alphafold_model_loader():
    """Simulates loading the AlphaFold model."""
    logging.info("Loading mock AlphaFold model...")
    time.sleep(2)
    logging.info("Mock AlphaFold model loaded.")
    return "mock_alphafold_model", "mock_alphafold_tokenizer", "cpu"

def mock_rosettafold_model_loader():
    """Simulates loading the RosettaFold model."""
    logging.info("Loading mock RosettaFold model...")
    time.sleep(2)
    logging.info("Mock RosettaFold model loaded.")
    return "mock_rosettafold_model", "mock_rosettafold_tokenizer", "cpu"

def mock_protein_folder(sequence: str, job_id: str, output_path: str, model_name: str, worker_id: str) -> dict:
    """Simulates protein folding for mock models."""
    import tempfile
    start_time = time.time()
    logging.info(f"'{model_name}' is folding sequence for job {job_id}...")
    time.sleep(5)  # Simulate work

    # Create a local temporary directory for output (works on external machines)
    temp_dir = tempfile.mkdtemp(prefix=f"phonos_job_{job_id}_")
    local_output_path = os.path.join(temp_dir, os.path.basename(output_path))

    pdb_string = f"HEADER    MOCK PDB FROM {model_name.upper()} WORKER {worker_id}\n"
    with open(local_output_path, "w") as f:
        f.write(pdb_string)
    logging.info(f"Job {job_id} folded successfully with '{model_name}'.")
    return {
        'job_id': job_id,
        'status': 'success',
        'model_used': model_name,
        'worker_id': worker_id,
        'execution_time': time.time() - start_time,
        'output_file': local_output_path,  # Local temp path for send_job_result to read
        'message': None
    }

# Real ESM-Fold implementation
def load_esm_model() -> Tuple[Optional[Any], Optional[Any], Optional[str]]:
    """Loads the real ESM model and tokenizer into memory."""
    try:
        from transformers import AutoTokenizer, EsmForProteinFolding
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logging.info(f"Loading ESM-Fold model on {device}...")
        tokenizer = AutoTokenizer.from_pretrained("facebook/esmfold_v1")
        model = EsmForProteinFolding.from_pretrained(
            "facebook/esmfold_v1",
            low_cpu_mem_usage=True,
            torch_dtype=torch.float16 if device == 'cuda' else torch.float32
        ).to(device)
        model.eval()
        logging.info("ESM-Fold model loaded successfully.")
        return model, tokenizer, device
    except Exception as e:
        logging.critical(f"Failed to load ESM-Fold model: {e}", exc_info=True)
        return None, None, None

def fold_protein_esm(sequence: str, job_id: str, output_path: str, tokenizer, model, device, worker_id: str, worker_type: str) -> dict:
    """Folds a protein sequence using the real ESM-Fold model."""
    import tempfile
    from transformers import EsmForProteinFolding
    result_data = {'job_id': job_id, 'status': 'error', 'model_used': worker_type, 'worker_id': worker_id}
    start_time = time.time()

    # Create a local temporary directory for output (works on external machines)
    temp_dir = tempfile.mkdtemp(prefix=f"phonos_job_{job_id}_")
    local_output_path = os.path.join(temp_dir, os.path.basename(output_path))

    try:
        tokenized_input = tokenizer([sequence], return_tensors="pt", add_special_tokens=False).to(device)
        with torch.no_grad():
            output = model(**tokenized_input)
        pdb_string = EsmForProteinFolding.infer_pdb(output)
        with open(local_output_path, "w") as f:
            f.write(pdb_string)
        result_data.update({
            'status': 'success',
            'execution_time': time.time() - start_time,
            'output_file': local_output_path,  # Local temp path for send_job_result to read
            'message': None
        })
        logging.info(f"Job {job_id}: ESM folding completed in {result_data['execution_time']:.2f}s")
        return result_data
    except Exception as e:
        result_data['message'] = f"Unexpected error during ESM folding: {e}"
        result_data['execution_time'] = time.time() - start_time
        logging.error(f"Job {job_id}: Unexpected ESM error - {e}", exc_info=True)
        return result_data

# --- Model Agnostic Worker Logic ---

# Global state for the worker
WORKER_TYPE = "unknown"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config.json'
logger = logging.getLogger(__name__)  # Placeholder, configured in main

# Redis keys are now dynamic based on WORKER_TYPE
def get_job_queue_key(org_id: Optional[int] = None) -> str:
    if org_id:
        return f"queue:job:{WORKER_TYPE}:org:{org_id}"
    return f"queue:job:{WORKER_TYPE}"

JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

def get_redis_connection():
    # Assuming config.py exists and is configured
    from config import Config
    return redis.Redis(host=Config.REDIS_HOST, port=Config.REDIS_PORT, db=Config.REDIS_DB, decode_responses=True)

def setup_logging(worker_id: str, worker_type: str):
    """Configures logging for the worker."""
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} ({worker_type}) - %(message)s',
        handlers=[
            logging.FileHandler(f'local_worker_{worker_type}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)

def send_heartbeat(worker_id: str, private_key: str, stop_event: threading.Event):
    """Periodically sends a signed heartbeat to Redis."""
    from node_utils import sign_message
    r = get_redis_connection()
    logger.info("Heartbeat thread started.")
    while not stop_event.wait(HEARTBEAT_INTERVAL):
        try:
            heartbeat_data = {'worker_id': worker_id, 'worker_type': WORKER_TYPE, 'timestamp': time.time()}
            payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
            signature = sign_message(private_key, payload_bytes)
            if not signature:
                logger.error("Failed to sign heartbeat. Skipping.")
                continue
            signed_message = {'payload': heartbeat_data, 'signature': signature}
            r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(signed_message))
        except Exception as e:
            logger.warning(f"Could not send heartbeat: {e}")
    logger.info("Heartbeat thread stopping.")

def process_job(job: dict, worker_id: str, model_logic: dict) -> dict:
    """Generic job processing function."""
    job_id = job.get('job_id', 'unknown')
    model, tokenizer, device = None, None, None
    try:
        sequence = job.get('params', {}).get('sequence')
        output_path = job.get('output_path')
        if not sequence or not output_path:
            return {'job_id': job_id, 'status': 'error', 'message': "Missing sequence or output path"}

        model, tokenizer, device = model_logic['loader']()
        if not model:
            return {'job_id': job_id, 'status': 'error', 'message': "Failed to load model"}

        return model_logic['folder'](sequence, job_id, output_path, tokenizer, model, device, worker_id, WORKER_TYPE)
    except Exception as e:
        logger.error(f"Job {job_id}: Unhandled error in process_job - {e}", exc_info=True)
        return {'job_id': job_id, 'status': 'error', 'message': f"Core job processing error: {e}"}
    finally:
        if model:
            del model
            if 'cuda' in str(device):
                torch.cuda.empty_cache()
            gc.collect()
            logger.info("Model unloaded and resources cleaned up.")

def worker_loop(worker_id: str, private_key: str, organization_id: Optional[int], model_logic: dict, stop_event: threading.Event):
    """Main worker loop, fetches jobs and processes them."""
    from node_utils import send_job_result
    from encryption_util import decrypt
    r = get_redis_connection()
    job_queue = get_job_queue_key(organization_id)
    logger.info(f"Waiting for jobs on Redis queue: '{job_queue}'")

    while not stop_event.is_set():
        try:
            job_payload = r.brpop(job_queue, timeout=1)
            if not job_payload:
                continue

            _, encrypted_job = job_payload
            job_json = decrypt(encrypted_job)
            if not job_json:
                logger.error("Failed to decrypt job. Skipping.")
                continue

            job = json.loads(job_json.decode('utf-8'))
            logger.info(f"Received Job {job.get('job_id', 'unknown')}")

            result = process_job(job, worker_id, model_logic)

            send_job_result(result, private_key, r)

        except redis.exceptions.RedisError as e:
            logger.error(f"Redis connection error: {e}. Reconnecting...")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Critical error in worker loop: {e}", exc_info=True)
            time.sleep(5)

    logger.info("Worker loop has exited.")

def main():
    """Main entry point for the local worker."""
    global WORKER_TYPE, logger
    parser = argparse.ArgumentParser(description="Phonos Local Compute Worker")
    parser.add_argument('--model', type=str, required=True, choices=['esm', 'alphafold', 'rosettafold'], help='The model to run.')
    args = parser.parse_args()
    WORKER_TYPE = args.model

    # --- Model-specific logic mapping ---
    MODEL_DISPATCH = {
        'esm': {'loader': load_esm_model, 'folder': fold_protein_esm},
        'alphafold': {'loader': mock_alphafold_model_loader, 'folder': lambda s, j, o, t, m, d, w, wt: mock_protein_folder(s, j, o, 'alphafold', w)},
        'rosettafold': {'loader': mock_rosettafold_model_loader, 'folder': lambda s, j, o, t, m, d, w, wt: mock_protein_folder(s, j, o, 'rosettafold', w)}
    }
    model_logic = MODEL_DISPATCH[WORKER_TYPE]

    # --- Node Initialization ---
    from node_utils import load_or_initialize_node, request_registration, set_node_organization
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

    is_new_node = not os.path.exists(NODE_CONFIG_FILE)
    worker_id, private_key, organization_id = load_or_initialize_node()

    if not worker_id or not private_key:
        logger.critical("Could not load or initialize node identity. Exiting.")
        return

    logger = setup_logging(worker_id, WORKER_TYPE)

    if is_new_node:
        logger.info("This is a new node. Attempting registration with the server...")
        try:
            with open(NODE_CONFIG_FILE, 'r') as f:
                config = json.load(f)
            public_key = config.get('public_key')
        except (IOError, json.JSONDecodeError) as e:
            logger.critical(f"Could not read public key from config file: {e}")
            return

        if not public_key:
            logger.critical("Public key not found in config file. Cannot register.")
            return

        approved, org_id = request_registration(worker_id, public_key, WORKER_TYPE)
        if not approved:
            logger.critical("Node registration failed or was denied. Exiting.")
            # Clean up the generated config file on failure
            if os.path.exists(NODE_CONFIG_FILE):
                os.remove(NODE_CONFIG_FILE)
            if os.path.exists('node_private_key.pem'):
                os.remove('node_private_key.pem')
            return

        if org_id and set_node_organization(org_id):
            organization_id = org_id
            logger.info(f"Successfully registered as a home node for organization {organization_id}.")
        else:
            logger.info("Registration successful as a public node.")

    # --- Start Worker Threads ---
    logger.info(f"Starting local worker '{worker_id}' for model '{WORKER_TYPE}'.")
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(target=send_heartbeat, args=(worker_id, private_key, stop_event), daemon=True)
    heartbeat_thread.start()

    try:
        worker_loop(worker_id, private_key, organization_id, model_logic, stop_event)
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
    finally:
        logger.info("Stopping worker...")
        stop_event.set()
        heartbeat_thread.join()
        logger.info("Worker has stopped.")

if __name__ == '__main__':
    main()