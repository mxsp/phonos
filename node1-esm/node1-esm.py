import json
import time
import os
import subprocess
import tempfile
import torch
import logging
import gc
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import redis
from transformers import AutoTokenizer, EsmForProteinFolding

from config import Config
from shared.encryption_util import canonical_dumps, decrypt, encrypt, sign_message, derive_public_key

# --- Configuration ---
WORKER_TYPE = 'type-0'
WORKER_ID = "publisher-node-type-0"  # Static ID for this publisher node
HEARTBEAT_INTERVAL = Config.WORKER_HEARTBEAT_INTERVAL

# --- GPU Configuration ---
def detect_and_configure_gpu(preferred_gpu: str = "0") -> str:
    """
    Detect available GPUs and configure the device.
    GPU-first approach: tries GPU, falls back to CPU if unavailable.

    Args:
        preferred_gpu: Preferred GPU index (default: "0" for ESMFold)

    Returns:
        str: Device string ('cuda' or 'cpu')
    """
    # Check environment variable override
    cuda_devices = os.environ.get('CUDA_VISIBLE_DEVICES', preferred_gpu)

    if cuda_devices == "" or cuda_devices.lower() == "cpu":
        return "cpu"

    try:
        # Try to query nvidia-smi for GPU availability
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,memory.total,name', '--format=csv,noheader,nounits'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0 and result.stdout.strip():
            gpus = []
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    parts = line.split(',')
                    if len(parts) >= 3:
                        gpus.append({
                            'index': parts[0].strip(),
                            'memory_mb': float(parts[1].strip()),
                            'name': parts[2].strip()
                        })

            if gpus:
                # Set the preferred GPU
                os.environ['CUDA_VISIBLE_DEVICES'] = cuda_devices

                # Verify CUDA is available in PyTorch
                if torch.cuda.is_available():
                    gpu_name = torch.cuda.get_device_name(0)
                    gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                    return "cuda"

        # Fall back to PyTorch CUDA detection
        if torch.cuda.is_available():
            os.environ['CUDA_VISIBLE_DEVICES'] = cuda_devices
            return "cuda"

    except Exception as e:
        pass  # Fall through to CPU

    return "cpu"

# Detect GPU at module load time (GPU 0 default for ESMFold)
PREFERRED_GPU = os.environ.get('PREFERRED_GPU', '0')
DEVICE = detect_and_configure_gpu(PREFERRED_GPU)

# --- Node-specific Private Key ---
# This key should be kept secret and is used to sign messages to the manager.
NODE_PRIVATE_KEY = os.environ.get("NODE_PRIVATE_KEY", "")

NODE_PUBLIC_KEY = derive_public_key(NODE_PRIVATE_KEY)
if not NODE_PUBLIC_KEY:
    raise RuntimeError("Failed to derive public key for node publisher-node-type-0")

# --- Redis Keys ---
JOB_QUEUE = f"queue:job:{WORKER_TYPE}"
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"


# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format=f'%(asctime)s - [%(levelname)s] - {WORKER_ID} - %(message)s',
    handlers=[
        logging.FileHandler(f'{WORKER_TYPE}_worker.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FoldingJob:
    """Normalized representation of a folding job payload."""

    job_id: str
    sequence: str
    output_path: Path
    output_dir: Path
    pdb_name: Optional[str] = None
    num_recycles: int = 4
    chunk_size: Optional[int] = None

    @property
    def output_filename(self) -> str:
        return self.pdb_name or self.output_path.name


@dataclass(frozen=True)
class ExternalRunnerConfig:
    """Configuration for invoking an external ESMFold launcher script."""

    script_path: Optional[Path]
    model_name: str
    timeout: int

    @classmethod
    def from_environment(cls) -> "ExternalRunnerConfig":
        raw_path = getattr(Config, "ESMFOLD_LAUNCH_SCRIPT", None)
        path = Path(raw_path).expanduser() if raw_path else None
        if path and not path.exists():
            logger.warning(
                "Configured ESMFold launch script %s does not exist; falling back to in-process model.",
                path,
            )
            path = None
        return cls(
            script_path=path,
            model_name=getattr(Config, "ESMFOLD_MODEL_NAME", "esm2_t36_3B_UR50D"),
            timeout=int(getattr(Config, "ESMFOLD_TIMEOUT", 1800)),
        )

    @property
    def enabled(self) -> bool:
        return self.script_path is not None


EXTERNAL_RUNNER_CONFIG = ExternalRunnerConfig.from_environment()
if EXTERNAL_RUNNER_CONFIG.enabled:
    logger.info("External ESMFold runner configured at %s", EXTERNAL_RUNNER_CONFIG.script_path)

# --- Redis Connection ---
redis_pool = redis.ConnectionPool(
    host=Config.REDIS_HOST,
    port=Config.REDIS_PORT,
    db=Config.REDIS_DB,
    password=Config.REDIS_PASSWORD
)

def get_redis_connection():
    """Gets a Redis connection from the pool."""
    return redis.Redis(connection_pool=redis_pool)


def parse_job_payload(job: Any) -> Tuple[Optional[FoldingJob], Optional[str]]:
    """Validate and normalize the job payload before processing."""

    if not isinstance(job, dict):
        return None, "Invalid job payload format."

    job_id = str(job.get('job_id', 'unknown'))
    params = job.get('params')
    if not isinstance(params, dict):
        return None, "Job parameters are missing or malformed."

    sequence_raw = params.get('sequence')
    if not isinstance(sequence_raw, str) or not sequence_raw.strip():
        return None, "Missing sequence in job parameters."
    sequence = sequence_raw.strip().upper()

    output_path_raw = job.get('output_path')
    if not isinstance(output_path_raw, str) or not output_path_raw.strip():
        return None, "Missing output path for job result."

    try:
        output_path = Path(output_path_raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        logger.error("Failed to resolve output path for job %s: %s", job_id, exc)
        return None, "Output path is invalid."

    pdb_name_raw = params.get('pdb_name') or job.get('pdb_name')
    pdb_name: Optional[str] = None
    if isinstance(pdb_name_raw, str) and pdb_name_raw.strip():
        sanitized = os.path.basename(pdb_name_raw.strip())
        if sanitized != pdb_name_raw.strip():
            return None, "Invalid PDB filename specified."
        if sanitized and Path(sanitized).suffix and Path(sanitized).suffix.lower() != '.pdb':
            return None, "PDB filenames must use the .pdb extension."
        pdb_name = sanitized or None

    # Extract custom parameters with defaults
    num_recycles = params.get('num_recycles', 4)
    if isinstance(num_recycles, (int, float)):
        num_recycles = int(max(0, min(20, num_recycles)))  # Clamp to 0-20
    else:
        num_recycles = 4

    chunk_size_raw = params.get('chunk_size')
    chunk_size: Optional[int] = None
    if chunk_size_raw and chunk_size_raw != 'auto':
        try:
            chunk_size = int(chunk_size_raw)
        except (ValueError, TypeError):
            chunk_size = None

    return FoldingJob(
        job_id=job_id,
        sequence=sequence,
        output_path=output_path,
        output_dir=output_path.parent,
        pdb_name=pdb_name,
        num_recycles=num_recycles,
        chunk_size=chunk_size,
    ), None


def build_external_command(job_request: FoldingJob, config: ExternalRunnerConfig, fasta_path: Path) -> list[str]:
    """Construct the command used to invoke the external ESMFold script."""

    assert config.script_path is not None
    command = [
        str(config.script_path),
        "--sequence",
        str(fasta_path),
        "--output_dir",
        str(job_request.output_dir),
        "--model_name",
        config.model_name,
        "--output_name",
        job_request.output_filename,
    ]
    return command


def run_external_esmfold(job_request: FoldingJob, config: ExternalRunnerConfig) -> dict:
    """Execute the external ESMFold launcher and return a result payload."""

    result = {
        'job_id': job_request.job_id,
        'status': 'error',
        'model': WORKER_TYPE,
        'worker_id': WORKER_ID,
        'message': None,
    }
    start_time = time.time()

    if not config.script_path:
        result['message'] = 'External ESMFold runner is not configured.'
        result['execution_time'] = time.time() - start_time
        return result

    if not config.script_path.exists():
        result['message'] = f"ESMFold launch script not found at {config.script_path}"
        result['execution_time'] = time.time() - start_time
        return result

    # Create a local temporary directory for output (works on external machines)
    local_temp_dir = tempfile.mkdtemp(prefix=f"phonos_job_{job_request.job_id}_")
    local_output_path = Path(local_temp_dir) / job_request.output_filename

    try:
        with tempfile.TemporaryDirectory(prefix=f"esmfold_{job_request.job_id}_") as temp_dir:
            fasta_path = Path(temp_dir) / 'input.fasta'
            fasta_path.write_text(f">{job_request.job_id}\n{job_request.sequence}\n", encoding='utf-8')

            # Modify command to use local temp output directory
            assert config.script_path is not None
            command = [
                str(config.script_path),
                "--sequence",
                str(fasta_path),
                "--output_dir",
                str(local_temp_dir),  # Use local temp dir instead of remote path
                "--model_name",
                config.model_name,
                "--output_name",
                job_request.output_filename,
            ]

            logger.debug("Running external ESMFold command: %s", command)
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=config.timeout,
            )
            if completed.stdout:
                logger.debug("ESMFold stdout: %s", completed.stdout.strip())
            if completed.stderr:
                logger.debug("ESMFold stderr: %s", completed.stderr.strip())

        if not local_output_path.exists():
            result['message'] = (
                f"Expected output file {local_output_path} was not produced by the ESMFold script."
            )
            result['execution_time'] = time.time() - start_time
            return result

        result.update({
            'status': 'success',
            'execution_time': time.time() - start_time,
            'output_file': str(local_output_path),  # Local temp path for send_job_result to read
            'message': None,
        })
        logger.info(
            "Job %s: External ESMFold completed in %.2fs",
            job_request.job_id,
            result['execution_time'],
        )
        return result
    except subprocess.TimeoutExpired:
        result['message'] = (
            f"ESMFold launch script timed out after {config.timeout} seconds."
        )
    except subprocess.CalledProcessError as exc:
        logger.error(
            "External ESMFold script failed for job %s with code %s",
            job_request.job_id,
            exc.returncode,
            exc_info=True,
        )
        snippet = (exc.stderr or exc.stdout or '').strip()
        if snippet:
            snippet = snippet.splitlines()[0]
        result['message'] = snippet or 'External ESMFold script returned a non-zero exit status.'
    except FileNotFoundError:
        result['message'] = f"ESMFold launch script not found at {config.script_path}"
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("Unexpected failure while running external ESMFold for job %s", job_request.job_id)
        result['message'] = f"Unexpected error while running ESMFold: {exc}"

    result['execution_time'] = time.time() - start_time
    return result


def load_model() -> Tuple[Optional[AutoTokenizer], Optional[EsmForProteinFolding], Optional[str]]:
    """Loads the ESM model and tokenizer into memory with GPU-first approach."""
    try:
        device = DEVICE  # Use globally detected device (GPU-first)
        logger.info(f"Loading ESM-Fold model on {device}...")

        if device == "cuda":
            gpu_name = torch.cuda.get_device_name(0)
            gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            logger.info(f"Using GPU: {gpu_name} ({gpu_memory:.1f}GB VRAM)")
        else:
            logger.info("Using CPU (no GPU available or GPU disabled)")

        tokenizer = AutoTokenizer.from_pretrained("facebook/esmfold_v1")

        # Use float16 for GPU to save memory, float32 for CPU
        dtype = torch.float16 if device == "cuda" else torch.float32

        model = EsmForProteinFolding.from_pretrained(
            "facebook/esmfold_v1",
            torch_dtype=dtype
        ).to(device)
        model.eval()
        logger.info(f"ESM-Fold model loaded successfully on {device}.")
        return tokenizer, model, device
    except Exception as e:
        logger.critical(f"Failed to load model: {e}", exc_info=True)
        return None, None, None

def unload_model(model: Optional[EsmForProteinFolding], device: str):
    """Explicitly unloads the model from memory."""
    try:
        del model
        if device.startswith('cuda'):
            torch.cuda.empty_cache()
        gc.collect()
        logger.info("Model and associated resources unloaded from memory.")
    except Exception as e:
        logger.warning(f"Failed to unload model cleanly: {e}")

def validate_protein_sequence(sequence: str) -> Tuple[bool, str]:
    """Validate protein sequence format and content."""
    if not sequence: return False, "Empty sequence provided"
    if len(sequence) < 10: return False, "Sequence too short (minimum 10 amino acids)"
    if len(sequence) > 96: return False, f"Sequence too long (maximum 96 amino acids, got {len(sequence)})"
    valid_chars = set('ACDEFGHIKLMNPQRSTVWY')
    invalid_chars = set(sequence.upper()) - valid_chars
    if invalid_chars:
        return False, f"Invalid amino acid characters: {', '.join(invalid_chars)}"
    return True, "Valid sequence"

def fold_protein(job_request: FoldingJob, tokenizer, model, device) -> dict:
    """Folds a protein sequence using ESM-Fold with custom parameters."""
    result_data = {
        'job_id': job_request.job_id,
        'status': 'error',
        'model': WORKER_TYPE,
        'worker_id': WORKER_ID,
    }
    start_time = time.time()

    # Create a temporary directory for output (works on external machines)
    temp_dir = tempfile.mkdtemp(prefix=f"phonos_job_{job_request.job_id}_")
    local_output_path = Path(temp_dir) / job_request.output_filename

    try:
        is_valid, validation_msg = validate_protein_sequence(job_request.sequence)
        if not is_valid:
            result_data['message'] = validation_msg
            return result_data

        tokenized_input = tokenizer([job_request.sequence], return_tensors="pt", add_special_tokens=False).to(device)

        # Configure model with custom parameters
        with torch.no_grad():
            # Apply num_recycles if the model supports it
            if hasattr(model, 'config') and hasattr(model.config, 'num_recycles'):
                original_recycles = model.config.num_recycles
                model.config.num_recycles = job_request.num_recycles
                logger.info(f"Job {job_request.job_id}: Using {job_request.num_recycles} recycles")

            # Apply chunk_size if specified and supported
            if job_request.chunk_size and hasattr(model, 'config') and hasattr(model.config, 'chunk_size'):
                original_chunk_size = model.config.chunk_size
                model.config.chunk_size = job_request.chunk_size
                logger.info(f"Job {job_request.job_id}: Using chunk_size {job_request.chunk_size}")

            output = model(**tokenized_input)

            # Restore original config values
            if hasattr(model, 'config'):
                if hasattr(model.config, 'num_recycles') and 'original_recycles' in locals():
                    model.config.num_recycles = original_recycles
                if hasattr(model.config, 'chunk_size') and 'original_chunk_size' in locals():
                    model.config.chunk_size = original_chunk_size

        pdb_string = model.infer_pdb(job_request.sequence)

        if not pdb_string:
            result_data['message'] = "Generated PDB string is empty"
            return result_data

        # Write to local temporary path instead of remote output_path
        with open(local_output_path, "w") as f:
            f.write(pdb_string)

        result_data.update({
            'status': 'success',
            'execution_time': time.time() - start_time,
            'output_file': str(local_output_path),  # Local temp path for send_job_result to read
            'message': None
        })
        logger.info(
            "Job %s: Folding completed in %.2fs with %d recycles",
            job_request.job_id,
            result_data['execution_time'],
            job_request.num_recycles,
        )
        return result_data
    except Exception as e:
        result_data['message'] = f"Unexpected error during folding: {e}"
        result_data['execution_time'] = time.time() - start_time
        logger.error("Job %s: Unexpected error - %s", job_request.job_id, e, exc_info=True)
        return result_data

def process_job(job: dict) -> dict:
    """Process a single job with load-on-demand model logic."""

    if not isinstance(job, dict):
        return {
            'job_id': 'unknown',
            'status': 'error',
            'model': WORKER_TYPE,
            'worker_id': WORKER_ID,
            'message': 'Invalid job payload received.',
        }

    job_id = str(job.get('job_id', 'unknown'))

    if job.get('task') not in (None, 'protein_folding'):
        return {
            'job_id': job_id,
            'status': 'error',
            'model': WORKER_TYPE,
            'worker_id': WORKER_ID,
            'message': f"Unsupported task: {job.get('task')}",
        }

    normalized_job, parse_error = parse_job_payload(job)
    if not normalized_job:
        return {
            'job_id': job_id,
            'status': 'error',
            'model': WORKER_TYPE,
            'worker_id': WORKER_ID,
            'message': parse_error,
        }

    is_valid, validation_message = validate_protein_sequence(normalized_job.sequence)
    if not is_valid:
        return {
            'job_id': job_id,
            'status': 'error',
            'model': WORKER_TYPE,
            'worker_id': WORKER_ID,
            'message': validation_message,
        }

    if EXTERNAL_RUNNER_CONFIG.enabled:
        external_result = run_external_esmfold(normalized_job, EXTERNAL_RUNNER_CONFIG)
        message = (external_result.get('message') or '').lower()
        should_fallback = external_result['status'] != 'success' and (
            'not configured' in message or 'launch script not found' in message
        )
        if external_result['status'] == 'success' or not should_fallback:
            return external_result

    tokenizer, model, device = None, None, None
    try:
        tokenizer, model, device = load_model()
        if not model:
            return {
                'job_id': job_id,
                'status': 'error',
                'model': WORKER_TYPE,
                'worker_id': WORKER_ID,
                'message': 'Failed to load model',
            }

        return fold_protein(normalized_job, tokenizer, model, device)
    except Exception as e:
        logger.error("Job %s: Error processing job - %s", job_id, e, exc_info=True)
        return {
            'job_id': job_id,
            'status': 'error',
            'model': WORKER_TYPE,
            'worker_id': WORKER_ID,
            'message': f"Job processing error: {e}",
        }
    finally:
        if model:
            unload_model(model, device)

def send_heartbeat():
    """Periodically sends a signed heartbeat to Redis."""
    r = get_redis_connection()
    logger.info("Heartbeat thread started.")
    while True:
        try:
            heartbeat_data = {
                'worker_id': WORKER_ID,
                'worker_type': WORKER_TYPE,
                'public_key': NODE_PUBLIC_KEY,
                'timestamp': time.time()
            }
            payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
            signature = sign_message(NODE_PRIVATE_KEY, payload_bytes)

            if not signature:
                logger.error("Failed to sign heartbeat. Skipping.")
                time.sleep(HEARTBEAT_INTERVAL)
                continue

            signed_message = {
                'payload': heartbeat_data,
                'signature': signature
            }

            r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(signed_message))
            logger.debug("Sent signed heartbeat to manager.")
        except redis.exceptions.RedisError as e:
            logger.warning(f"Could not send heartbeat: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in heartbeat thread: {e}", exc_info=True)
        time.sleep(HEARTBEAT_INTERVAL)

from shared.node_utils import send_job_result

def main():
    """Main worker loop to fetch jobs from Redis."""
    logger.info(f"{WORKER_TYPE.upper()}-WORKER ({WORKER_ID}) starting...")

    # Start heartbeat thread
    heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
    heartbeat_thread.start()

    r = get_redis_connection()
    
    logger.info(f"Waiting for jobs on Redis queue: '{JOB_QUEUE}'")
    while True:
        try:
            # Blocking pop from the job queue
            _, encrypted_job = r.brpop(JOB_QUEUE)
            
            job_json = decrypt(encrypted_job)
            if not job_json:
                logger.error("Failed to decrypt job. Skipping.")
                continue

            try:
                job = json.loads(job_json.decode('utf-8'))
            except json.JSONDecodeError as exc:
                logger.error("Failed to decode job payload: %s", exc)
                continue

            job_id = job.get('job_id', 'unknown') if isinstance(job, dict) else 'unknown'
            logger.info(f"Received Job {job_id}")

            result = process_job(job)

            send_job_result(result, NODE_PRIVATE_KEY, r)

        except redis.exceptions.RedisError as e:
            logger.error(f"Redis connection error in main loop: {e}. Reconnecting...")
            time.sleep(5)
            r = get_redis_connection() # Attempt to re-establish connection
        except Exception as e:
            logger.error(f"Critical error in worker loop: {e}", exc_info=True)
            time.sleep(5) # Avoid rapid-fire logging in case of a persistent error

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info(f"{WORKER_TYPE.upper()}-WORKER ({WORKER_ID}) shutting down")
    except Exception as e:
        logger.critical(f"Unhandled exception at top level: {e}", exc_info=True)
