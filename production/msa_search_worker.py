#!/usr/bin/env python3
"""
MSA Search Worker Node
Connects to NVIDIA's ColabFold MSA-Search NIM API for Multiple Sequence Alignment.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import redis
import requests

try:
    from encryption_util import canonical_dumps, decrypt, encrypt
    from node_utils import sign_message, send_job_result
    from config import Config
except ImportError:
    Config = None
    decrypt = None
    encrypt = None
    canonical_dumps = None
    sign_message = None
    send_job_result = None

NVIDIA_MSA_SEARCH_URL = os.getenv(
    "MSA_SEARCH_API_URL",
    "https://health.api.nvidia.com/v1/biology/colabfold/msa-search/predict"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

WORKER_TYPE = "msa-search"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = "node_config_msa_search.json"
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

logger = logging.getLogger(__name__)
WORKER_BASE_DIR = Path(__file__).resolve().parent
PRIVATE_KEY_FILE = "node_private_key.pem"


def get_job_queue_key(org_id: Optional[int] = None) -> str:
    """Get Redis job queue key for MSA Search jobs."""
    if org_id:
        return f"queue:job:{WORKER_TYPE}:org:{org_id}"
    return f"queue:job:{WORKER_TYPE}"


def get_redis_connection(decode_responses: bool = False):
    """Get Redis connection."""
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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (msa-search) - %(message)s',
        handlers=[
            logging.FileHandler(f'msa_search_worker_{worker_id}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)


def call_msa_search_api(
    sequence: str,
    databases: list = None,
    e_value: float = 0.0001,
    iterations: int = 1,
    max_msa_sequences: int = 500,
    output_alignment_formats: list = None,
    search_type: str = "colabfold"
) -> Dict[str, Any]:
    """Call NVIDIA MSA Search NIM API for multiple sequence alignment."""
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    if databases is None:
        databases = ["uniref90", "small_bfd"]
    if output_alignment_formats is None:
        output_alignment_formats = ["a3m"]

    # Map frontend database names to NVIDIA API names
    db_mapping = {
        "uniref90": "Uniref30_2302",
        "small_bfd": "Uniref30_2302",  # Map to available DB
        "colabfold_envdb": "colabfold_envdb_202108",
    }
    api_databases = [db_mapping.get(db, db) for db in databases]

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "sequence": sequence.upper(),
        "databases": api_databases,
        "e_value": e_value,
        "iterations": iterations,
        "max_msa_sequences": max_msa_sequences,
        "output_alignment_formats": output_alignment_formats,
        "search_type": search_type
    }

    logger.info(f"Calling MSA Search API with databases: {api_databases}, search_type: {search_type}")

    response = requests.post(
        url=NVIDIA_MSA_SEARCH_URL,
        headers=headers,
        json=payload,
        timeout=300
    )

    if response.status_code == 200:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        else:
            return {"raw_content": response.text}
    elif response.status_code == 202:
        task_id = response.headers.get("NVCF-REQID")
        if not task_id:
            raise Exception("MSA Search 202 response missing NVCF-REQID")
        logger.info(f"MSA Search async task started: {task_id}")
        return poll_nvcf_status(task_id, headers)
    else:
        raise Exception(f"MSA Search API error: {response.status_code} - {response.text[:500]}")


def poll_nvcf_status(task_id: str, headers: dict, timeout: int = 300) -> Dict[str, Any]:
    """Poll NVCF for task completion."""
    status_url = f"https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)
            if response.status_code == 200:
                result = response.json()
                status = result.get("status", "")

                if status == "fulfilled":
                    content = result.get("result", {})
                    logger.info(f"MSA Search task {task_id} completed successfully")
                    return content
                elif status in ("errored", "failed"):
                    error_msg = result.get("error", "Unknown error")
                    raise Exception(f"MSA Search task failed: {error_msg}")
                elif status in ("pending", "in_progress"):
                    time.sleep(5)
                    continue
                else:
                    logger.warning(f"Unknown status: {status}, waiting...")
                    time.sleep(5)
            elif response.status_code == 202:
                time.sleep(5)
                continue
            else:
                raise Exception(f"Status check failed: {response.status_code}")
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout polling status for {task_id}")
            time.sleep(5)
            continue

    raise Exception(f"MSA Search timeout after {timeout}s")


def process_msa_search_job(job: Dict[str, Any], worker_id: str) -> Dict[str, Any]:
    """Process an MSA Search job."""
    start_time = time.time()
    job_id = job.get("job_id", "unknown")
    params = job.get("params", {})

    result = {
        "job_id": job_id,
        "status": "success",
        "worker_id": worker_id,
        "timestamp": time.time(),
        "output_path": "/dev/null"
    }

    try:
        sequence = params.get("sequence", "")
        if not sequence:
            raise ValueError("No sequence provided")

        sequence = sequence.replace(" ", "").replace("\n", "").replace("\r", "").upper()

        databases = params.get("databases", ["uniref90", "small_bfd"])
        # Map frontend database names to NVIDIA API names
        db_mapping = {
            "uniref90": "Uniref30_2302",
            "small_bfd": "colabfold_envdb_202108",
            "colabfold_envdb": "colabfold_envdb_202108",
        }
        databases = [db_mapping.get(db, "colabfold_envdb_202108") for db in databases]

        e_value = float(params.get("e_value", 0.0001))
        iterations = int(params.get("iterations", 1))
        max_msa_sequences = int(params.get("max_msa_sequences", 500))
        output_alignment_formats = params.get("output_alignment_formats", ["a3m"])
        search_type = params.get("search_type", "colabfold")
        if search_type == "mmseqs2":
            search_type = "colabfold"

        logger.info(f"Job {job_id}: Running MSA search for {len(sequence)} aa sequence")

        msa_result = call_msa_search_api(
            sequence=sequence,
            databases=databases,
            e_value=e_value,
            iterations=iterations,
            max_msa_sequences=max_msa_sequences,
            output_alignment_formats=output_alignment_formats,
            search_type=search_type
        )

        output_filename = "msa_result.a3m"
        output_path = "/tmp/msa_search_results"
        os.makedirs(output_path, exist_ok=True)
        
        output_file_path = os.path.join(output_path, f"{job_id}_{output_filename}")
        
        with open(output_file_path, 'w') as f:
            if isinstance(msa_result, dict):
                if "a3m" in msa_result:
                    f.write(msa_result["a3m"])
                elif "fasta" in msa_result:
                    f.write(msa_result["fasta"])
                else:
                    f.write(json.dumps(msa_result, indent=2))
            else:
                f.write(str(msa_result))

        result["output_path"] = output_file_path
        result["output_filename"] = output_filename
        result["output_file_content"] = open(output_file_path, 'r').read()
        result["msa_result"] = msa_result
        result["message"] = "MSA search completed successfully"

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        result["status"] = "failed"
        result["error"] = str(e)
        result["message"] = f"Error: {str(e)}"

    execution_time = time.time() - start_time
    result["execution_time"] = execution_time
    logger.info(f"Job {job_id} completed in {execution_time:.2f}s")

    return result


def send_result(job_result: Dict[str, Any]) -> bool:
    """Send job result to the results queue."""
    r = get_redis_connection()
    r_text = get_redis_connection(decode_responses=True)
    try:
        if encrypt:
            encrypted_result = encrypt(json.dumps(job_result).encode('utf-8'))
            if encrypted_result:
                r.lpush(JOB_RESULTS_QUEUE, encrypted_result)
                return True
        r_text.lpush(JOB_RESULTS_QUEUE, json.dumps(job_result))
        return True
    except Exception as e:
        logger.error(f"Failed to send result: {e}")
        return False


def heartbeat_thread(
    worker_id: str,
    private_key: Optional[str],
    stop_event: threading.Event
):
    """Send periodic heartbeats."""
    r = get_redis_connection()

    while not stop_event.wait(HEARTBEAT_INTERVAL):
        try:
            heartbeat_data = {
                "worker_id": worker_id,
                "worker_type": WORKER_TYPE,
                "timestamp": time.time()
            }

            if private_key and canonical_dumps and sign_message:
                payload_bytes = canonical_dumps(heartbeat_data).encode("utf-8")
                signature = sign_message(private_key, payload_bytes)
                if signature:
                    signed_message = {"payload": heartbeat_data, "signature": signature}
                    r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(signed_message))
            else:
                r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))

        except Exception as e:
            logger.warning(f"Could not send heartbeat: {e}")

    logger.info("Heartbeat thread stopping.")


def worker_loop(
    worker_id: str,
    private_key: Optional[str],
    organization_id: Optional[int],
    stop_event: threading.Event
):
    """Main worker loop."""
    r = get_redis_connection(decode_responses=False)
    r_text = get_redis_connection(decode_responses=True)
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"MSA Search worker started. Listening on queue: {job_queue}")

    while not stop_event.is_set():
        try:
            job_payload = r.brpop(job_queue, timeout=1)

            if not job_payload:
                continue

            _, encrypted_data = job_payload

            if decrypt and isinstance(encrypted_data, bytes):
                try:
                    decrypted = decrypt(encrypted_data)
                    if decrypted:
                        job_data = decrypted.decode("utf-8")
                    else:
                        job_data = encrypted_data.decode("utf-8")
                except Exception:
                    job_data = encrypted_data.decode("utf-8") if isinstance(encrypted_data, bytes) else encrypted_data
            else:
                job_data = encrypted_data.decode("utf-8") if isinstance(encrypted_data, bytes) else encrypted_data

            job = json.loads(job_data)
            job_id = job.get("job_id", "unknown")
            logger.info(f"Received job: {job_id}")

            result = process_msa_search_job(job, worker_id)

            # Send result to results queue - use lpush like other workers
            result_json = json.dumps(result)
            if encrypt:
                try:
                    encrypted_result = encrypt(result_json.encode('utf-8'))
                    r.lpush(JOB_RESULTS_QUEUE, encrypted_result)
                except Exception as e:
                    logger.error(f"Failed to encrypt result: {e}")
                    r_text.lpush(JOB_RESULTS_QUEUE, result_json)
            else:
                r_text.lpush(JOB_RESULTS_QUEUE, result_json)

            # Also update job status directly in database as fallback
            try:
                import psycopg2
                db_conn = psycopg2.connect(Config.DATABASE_URL)
                db_cursor = db_conn.cursor()
                if result.get("status") == "success":
                    db_cursor.execute(
                        "UPDATE jobs SET status = 'completed', output_file = %s WHERE job_id = %s",
                        (result.get("output_path"), job_id)
                    )
                else:
                    db_cursor.execute(
                        "UPDATE jobs SET status = 'failed', message = %s WHERE job_id = %s",
                        (result.get("message"), job_id)
                    )
                db_conn.commit()
                db_conn.close()
                logger.info(f"Job {job_id} status updated in database")
            except Exception as db_err:
                logger.error(f"Failed to update database: {db_err}")

            logger.info(f"Job {job_id} result sent")

        except Exception as e:
            logger.error(f"Worker loop error: {e}", exc_info=True)

    logger.info("Worker loop stopped.")


def main():
    parser = argparse.ArgumentParser(description="MSA Search Worker")
    parser.add_argument("--worker-id", default=None, help="Worker ID")
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--organization-id", type=int, default=None, help="Organization ID for dedicated queue")
    args = parser.parse_args()

    worker_id = args.worker_id
    organization_id = args.organization_id
    private_key = None

    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
                worker_id = worker_id or config.get("worker_id")
                organization_id = organization_id or config.get("organization_id")
                private_key = config.get("private_key")

    if not worker_id:
        worker_id = f"{WORKER_TYPE}-{os.getpid()}"

    logger = setup_logging(worker_id)
    logger.info(f"Starting MSA Search worker: {worker_id}")

    stop_event = threading.Event()

    heartbeat_thread_handle = threading.Thread(
        target=heartbeat_thread,
        args=(worker_id, private_key, stop_event),
        daemon=True
    )
    heartbeat_thread_handle.start()

    worker_thread_handle = threading.Thread(
        target=worker_loop,
        args=(worker_id, private_key, organization_id, stop_event),
        daemon=True
    )
    worker_thread_handle.start()

    try:
        worker_thread_handle.join()
    except KeyboardInterrupt:
        logger.info("Stopping worker...")
        stop_event.set()


if __name__ == "__main__":
    main()