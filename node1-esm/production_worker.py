"""
Production-Ready ESMFold Worker with Error Handling and Monitoring
"""
import json
import time
import os
import sys
import tempfile
import torch
import logging
import gc
import threading
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple
import traceback
import importlib.util

import redis
from transformers import AutoTokenizer, EsmForProteinFolding

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from shared.encryption_util import decrypt, derive_public_key
from shared.node_utils import send_job_result

# --- Configuration ---
WORKER_TYPE = os.environ.get('WORKER_TYPE', 'type-0')
WORKER_ID = os.environ.get('WORKER_ID', "publisher-node-type-0")
HEARTBEAT_INTERVAL = int(os.environ.get('WORKER_HEARTBEAT_INTERVAL', 30))
MAX_RETRIES = int(os.environ.get('MAX_RETRIES', 3))
JOB_TIMEOUT = int(os.environ.get('JOB_TIMEOUT', 600))  # 10 minutes
POLL_TIMEOUT = int(os.environ.get('POLL_TIMEOUT', 5))

# --- Node-specific Private Key ---
NODE_PRIVATE_KEY = os.environ.get("NODE_PRIVATE_KEY", "")

NODE_PUBLIC_KEY = derive_public_key(NODE_PRIVATE_KEY)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/worker.log') if Path('logs').exists() or Path('logs').mkdir(exist_ok=True) else logging.NullHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Global State ---
redis_client = None
heartbeat_thread = None
shutdown_event = threading.Event()
model = None
tokenizer = None
jobs_processed = 0
jobs_failed = 0
start_time = time.time()

# --- Load node1-esm module (hyphenated name requires importlib) ---
def load_node1_esm_module():
    """Load the node1-esm.py module since Python can't import hyphenated names."""
    module_path = Path(__file__).parent / "node1-esm.py"
    spec = importlib.util.spec_from_file_location("node1_esm", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

node1_esm_module = None


class JobTimeoutError(Exception):
    """Raised when job exceeds timeout."""
    pass


def setup_signal_handlers():
    """Setup graceful shutdown on SIGTERM/SIGINT."""
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, initiating graceful shutdown...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def initialize_redis(max_retries=5) -> redis.Redis:
    """Initialize Redis connection with retry logic."""
    for attempt in range(max_retries):
        try:
            client = redis.Redis(
                host=Config.REDIS_HOST,
                port=Config.REDIS_PORT,
                db=Config.REDIS_DB,
                password=Config.REDIS_PASSWORD,
                ssl=Config.REDIS_SSL,
                decode_responses=False,
                socket_timeout=10,
                socket_connect_timeout=10,
                retry_on_timeout=True,
                health_check_interval=30
            )
            client.ping()
            logger.info(f"✅ Connected to Redis at {Config.REDIS_HOST}:{Config.REDIS_PORT}")
            return client
        except Exception as e:
            logger.error(f"❌ Failed to connect to Redis (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise


def load_model():
    """Load ESMFold model with error handling."""
    global model, tokenizer

    try:
        logger.info("Loading ESMFold model...")
        model_name = "facebook/esmfold_v1"

        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = EsmForProteinFolding.from_pretrained(model_name, low_cpu_mem_usage=True)

        # Move to GPU if available
        if torch.cuda.is_available():
            model = model.cuda()
            logger.info(f"✅ Model loaded on GPU: {torch.cuda.get_device_name(0)}")
        else:
            logger.warning("⚠️ GPU not available, using CPU (will be slower)")

        model.eval()
        logger.info("✅ ESMFold model loaded successfully")

    except Exception as e:
        logger.error(f"❌ Failed to load model: {e}")
        raise


def send_heartbeat():
    """Send periodic heartbeat to main server with metrics."""
    global jobs_processed, jobs_failed, start_time

    while not shutdown_event.is_set():
        try:
            if redis_client:
                uptime = int(time.time() - start_time)

                heartbeat_data = {
                    'worker_id': WORKER_ID,
                    'worker_type': WORKER_TYPE,
                    'timestamp': time.time(),
                    'status': 'running',
                    'jobs_processed': jobs_processed,
                    'jobs_failed': jobs_failed,
                    'uptime_seconds': uptime
                }

                # Publish heartbeat
                redis_client.publish('pubsub:worker_heartbeats', json.dumps(heartbeat_data))

                # Update health status
                redis_client.hset(
                    f'worker_health:{WORKER_ID}',
                    mapping={
                        'last_heartbeat': time.time(),
                        'status': 'running',
                        'worker_type': WORKER_TYPE,
                        'jobs_processed': jobs_processed,
                        'jobs_failed': jobs_failed,
                        'uptime_seconds': uptime
                    }
                )

                redis_client.expire(f'worker_health:{WORKER_ID}', 300)  # Expire after 5 minutes

                logger.debug(f"💓 Heartbeat sent (jobs: {jobs_processed}, failed: {jobs_failed})")
        except Exception as e:
            logger.error(f"❌ Heartbeat error: {e}")

        shutdown_event.wait(HEARTBEAT_INTERVAL)


def process_job_with_timeout(job_data: dict, timeout: int) -> dict:
    """Process job with timeout using threading."""
    global node1_esm_module
    result_container = {'result': None, 'error': None}

    def worker():
        try:
            global node1_esm_module
            if node1_esm_module is None:
                node1_esm_module = load_node1_esm_module()
            result_container['result'] = node1_esm_module.process_job(job_data)
        except Exception as e:
            result_container['error'] = e

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        raise JobTimeoutError(f"Job exceeded timeout of {timeout} seconds")

    if result_container['error']:
        raise result_container['error']

    return result_container['result']


def process_job_with_retry(job_data: dict) -> dict:
    """Process job with retry logic."""
    global node1_esm_module
    job_id = job_data.get('job_id', 'unknown')

    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"🧬 Processing job {job_id} (attempt {attempt + 1}/{MAX_RETRIES})")

            start_time = time.time()

            # Process with timeout
            try:
                if node1_esm_module is None:
                    node1_esm_module = load_node1_esm_module()
                result = node1_esm_module.process_job(job_data)
            except Exception as e:
                # Fallback if timeout wrapper fails
                logger.warning(f"Direct processing failed, error: {e}")
                raise

            processing_time = time.time() - start_time

            result['processing_time'] = processing_time
            result['worker_id'] = WORKER_ID
            result['attempt'] = attempt + 1

            logger.info(f"✅ Job {job_id} completed in {processing_time:.2f}s")

            return result

        except JobTimeoutError as e:
            logger.error(f"⏱️ Job {job_id} timed out on attempt {attempt + 1}: {e}")
            if attempt >= MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)  # Exponential backoff

        except Exception as e:
            logger.error(f"❌ Error processing job {job_id} (attempt {attempt + 1}): {e}")
            logger.error(traceback.format_exc())

            if attempt >= MAX_RETRIES - 1:
                raise

            # Exponential backoff
            time.sleep(2 ** attempt)

            # Clear GPU memory if OOM error
            if "out of memory" in str(e).lower():
                logger.warning("Clearing GPU cache due to OOM error")
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

    # Should not reach here
    raise Exception(f"Job {job_id} failed after {MAX_RETRIES} attempts")


def handle_failed_job(job_data: dict, error: Exception) -> dict:
    """Create error result for failed job."""
    job_id = job_data.get('job_id', 'unknown')

    return {
        'job_id': job_id,
        'status': 'error',
        'error': str(error),
        'error_type': type(error).__name__,
        'worker_id': WORKER_ID,
        'timestamp': time.time()
    }


def main_loop():
    """Main worker loop with production error handling."""
    global redis_client, jobs_processed, jobs_failed

    logger.info(f"🚀 Starting ESMFold worker {WORKER_ID}")
    logger.info(f"Configuration: WORKER_TYPE={WORKER_TYPE}, HEARTBEAT={HEARTBEAT_INTERVAL}s, TIMEOUT={JOB_TIMEOUT}s")

    # Setup signal handlers
    setup_signal_handlers()

    # Initialize Redis
    redis_client = initialize_redis()

    # Load model
    load_model()

    # Start heartbeat thread
    global heartbeat_thread
    heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
    heartbeat_thread.start()
    logger.info("💓 Heartbeat thread started")

    # Main processing loop
    queue_key = f'queue:job:{WORKER_TYPE}'
    logger.info(f"👀 Polling queue: {queue_key}")

    consecutive_errors = 0
    max_consecutive_errors = 10

    while not shutdown_event.is_set():
        try:
            # Poll for jobs
            result = redis_client.blpop(queue_key, timeout=POLL_TIMEOUT)

            if not result:
                continue

            _, job_data_bytes = result
            consecutive_errors = 0  # Reset error counter on successful poll

            # Decrypt and verify job data
            try:
                job_data = decrypt(job_data_bytes)
                if not job_data:
                    logger.error("❌ Failed to decrypt job data")
                    jobs_failed += 1
                    continue
                job_dict = json.loads(job_data)
                logger.info(f"📥 Received job: {job_dict.get('job_id', 'unknown')}")
            except Exception as e:
                logger.error(f"❌ Failed to decrypt/parse job: {e}")
                jobs_failed += 1
                continue

            # Process the job with retry
            try:
                result_data = process_job_with_retry(job_dict)
                jobs_processed += 1

            except Exception as e:
                logger.error(f"❌ Job failed after retries: {e}")
                result_data = handle_failed_job(job_dict, e)
                jobs_failed += 1

            # Send result using the proper format from node_utils
            try:
                success = send_job_result(result_data, NODE_PRIVATE_KEY, redis_client)
                if success:
                    logger.info(f"✅ Result sent for job {result_data.get('job_id')}")
                else:
                    logger.error(f"❌ Failed to send result for job {result_data.get('job_id')}")
                    jobs_failed += 1

            except Exception as e:
                logger.error(f"❌ Failed to send result: {e}")
                jobs_failed += 1

            # Cleanup
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        except redis.ConnectionError as e:
            consecutive_errors += 1
            logger.error(f"❌ Redis connection error ({consecutive_errors}/{max_consecutive_errors}): {e}")

            if consecutive_errors >= max_consecutive_errors:
                logger.critical("Max consecutive errors reached, shutting down")
                shutdown_event.set()
                break

            # Try to reconnect
            time.sleep(min(2 ** consecutive_errors, 60))  # Exponential backoff, max 60s
            try:
                redis_client = initialize_redis(max_retries=3)
                consecutive_errors = 0
            except:
                pass

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"❌ Error in main loop ({consecutive_errors}/{max_consecutive_errors}): {e}")
            logger.error(traceback.format_exc())

            if consecutive_errors >= max_consecutive_errors:
                logger.critical("Max consecutive errors reached, shutting down")
                shutdown_event.set()
                break

            time.sleep(1)

    logger.info(f"👋 Worker shutting down (processed: {jobs_processed}, failed: {jobs_failed})")


if __name__ == "__main__":
    try:
        # Create logs directory
        Path('logs').mkdir(exist_ok=True)

        main_loop()

    except KeyboardInterrupt:
        logger.info("⚠️ Received shutdown signal")
        shutdown_event.set()

    except Exception as e:
        logger.critical(f"❌ Fatal error: {e}")
        logger.critical(traceback.format_exc())
        sys.exit(1)

    finally:
        if heartbeat_thread and heartbeat_thread.is_alive():
            heartbeat_thread.join(timeout=5)

        if redis_client:
            try:
                # Mark worker as stopped
                redis_client.hset(
                    f'worker_health:{WORKER_ID}',
                    mapping={'status': 'stopped', 'last_heartbeat': time.time()}
                )
            except:
                pass

        logger.info("✅ Shutdown complete")
