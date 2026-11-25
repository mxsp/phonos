import base64
import json
import time
import os
import subprocess
import tempfile
import logging
import gc
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import redis
import shutil

from worker_config import Config
from shared.encryption_util import canonical_dumps, decrypt, derive_public_key, sign_message
from shared.node_utils import send_job_result

# Configure multi-threading for CPU-only operation
os.environ['OMP_NUM_THREADS'] = str(os.environ.get('OMP_NUM_THREADS', '6'))
os.environ['MKL_NUM_THREADS'] = str(os.environ.get('MKL_NUM_THREADS', '6'))
os.environ['OPENBLAS_NUM_THREADS'] = str(os.environ.get('OPENBLAS_NUM_THREADS', '6'))
os.environ['NUMEXPR_NUM_THREADS'] = str(os.environ.get('NUMEXPR_NUM_THREADS', '6'))

def detect_suitable_gpu(min_vram_gb=8, preferred_gpu="1"):
    """
    Detect if a suitable GPU is available (8GB+ VRAM recommended for DiffDock).
    GPU-first approach with preference for GPU 1 (to run alongside ESMFold on GPU 0).

    Args:
        min_vram_gb: Minimum VRAM in GB required (default: 8)
        preferred_gpu: Preferred GPU index (default: "1" for DiffDock)

    Returns:
        str: GPU device ID to use, or empty string for CPU-only
    """
    # Check for explicit CPU override
    cuda_env = os.environ.get('CUDA_VISIBLE_DEVICES', None)
    if cuda_env == "" or (cuda_env and cuda_env.lower() == "cpu"):
        return ""

    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,memory.total,name', '--format=csv,noheader,nounits'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return ""

        gpus = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(',')
            if len(parts) >= 3:
                gpu_index = parts[0].strip()
                vram_mb = float(parts[1].strip())
                vram_gb = vram_mb / 1024
                gpu_name = parts[2].strip()
                gpus.append({
                    'index': gpu_index,
                    'vram_gb': vram_gb,
                    'name': gpu_name
                })

        if not gpus:
            return ""

        # First, check if preferred GPU is available and suitable
        for gpu in gpus:
            if gpu['index'] == preferred_gpu and gpu['vram_gb'] >= min_vram_gb:
                logger.info(f"Using preferred GPU {gpu['index']}: {gpu['name']} ({gpu['vram_gb']:.1f}GB VRAM)")
                return gpu['index']

        # Fall back to any suitable GPU (prefer higher index to avoid conflict with ESMFold on GPU 0)
        suitable_gpus = [g for g in gpus if g['vram_gb'] >= min_vram_gb]
        if suitable_gpus:
            # Prefer higher index GPUs
            selected = sorted(suitable_gpus, key=lambda x: int(x['index']), reverse=True)[0]
            logger.info(f"Using GPU {selected['index']}: {selected['name']} ({selected['vram_gb']:.1f}GB VRAM)")
            return selected['index']

        # Log available GPUs that don't meet requirements
        for gpu in gpus:
            if gpu['vram_gb'] < min_vram_gb:
                logger.info(f"GPU {gpu['index']} has {gpu['vram_gb']:.1f}GB VRAM (< {min_vram_gb}GB required) - skipping")

        logger.info(f"No GPU with >= {min_vram_gb}GB VRAM found - using CPU")
        return ""

    except Exception as e:
        logger.warning(f"Could not detect GPU: {e} - using CPU")
        return ""

# --- Configuration ---
WORKER_TYPE = 'type-3'
WORKER_ID = "publisher-node-type-3"  # Static ID for this DiffDock node
HEARTBEAT_INTERVAL = Config.WORKER_HEARTBEAT_INTERVAL

# --- Node-specific Private Key ---
# This key should be kept secret and is used to sign messages to the manager.
NODE_PRIVATE_KEY = os.environ.get("NODE_PRIVATE_KEY", "")

# Derive the public key from the private key
NODE_PUBLIC_KEY = derive_public_key(NODE_PRIVATE_KEY)
if not NODE_PUBLIC_KEY:
    raise RuntimeError(f"Failed to derive public key for node {WORKER_ID}")

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

# --- Global State ---
redis_client = None
heartbeat_thread = None
shutdown_event = threading.Event()

# --- DiffDock Configuration ---
DIFFDOCK_INSTALL_DIR = os.environ.get('DIFFDOCK_INSTALL_DIR', '/opt/DiffDock')
DIFFDOCK_CONDA_ENV = os.environ.get('DIFFDOCK_CONDA_ENV', 'diffdock')

# Auto-detect suitable GPU (8GB+ VRAM) or fallback to CPU
# DiffDock prefers GPU 1 by default to run alongside ESMFold on GPU 0
PREFERRED_GPU = os.environ.get('PREFERRED_GPU', '1')
DETECTED_GPU = detect_suitable_gpu(min_vram_gb=8, preferred_gpu=PREFERRED_GPU)
if DETECTED_GPU:
    os.environ['CUDA_VISIBLE_DEVICES'] = DETECTED_GPU
    logger.info(f"GPU mode enabled: Using GPU {DETECTED_GPU}")
else:
    os.environ['CUDA_VISIBLE_DEVICES'] = ""
    logger.info(f"CPU mode enabled: Using {os.environ.get('OMP_NUM_THREADS', 'default')} threads")


@dataclass
class DiffDockJob:
    """Represents a DiffDock molecular docking job."""
    job_id: str
    protein_pdb: str  # PDB file content or path
    ligand_sdf: str   # SDF file content or path
    parameters: dict  # Additional DiffDock parameters


def initialize_redis() -> redis.Redis:
    """Initialize Redis connection."""
    try:
        client = redis.Redis(
            host=Config.REDIS_HOST,
            port=Config.REDIS_PORT,
            db=Config.REDIS_DB,
            password=Config.REDIS_PASSWORD,
            ssl=Config.REDIS_SSL,
            decode_responses=False
        )
        client.ping()
        logger.info(f"✅ Connected to Redis at {Config.REDIS_HOST}:{Config.REDIS_PORT}")
        return client
    except Exception as e:
        logger.error(f"❌ Failed to connect to Redis: {e}")
        raise


def send_heartbeat():
    """Send periodic signed heartbeat to main server."""
    while not shutdown_event.is_set():
        try:
            if redis_client:
                heartbeat_data = {
                    'worker_id': WORKER_ID,
                    'worker_type': WORKER_TYPE,
                    'public_key': NODE_PUBLIC_KEY,
                    'timestamp': time.time()
                }

                # Sign the heartbeat payload
                payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
                signature = sign_message(NODE_PRIVATE_KEY, payload_bytes)

                if not signature:
                    logger.error("Failed to sign heartbeat. Skipping.")
                    shutdown_event.wait(HEARTBEAT_INTERVAL)
                    continue

                signed_message = {
                    'payload': heartbeat_data,
                    'signature': signature
                }

                # Publish signed heartbeat
                redis_client.publish('pubsub:worker_heartbeats', json.dumps(signed_message))

                # Update health status
                redis_client.hset(
                    f'worker_health:{WORKER_ID}',
                    mapping={
                        'last_heartbeat': time.time(),
                        'status': 'running',
                        'worker_type': WORKER_TYPE
                    }
                )

                logger.debug(f"Sent signed heartbeat for {WORKER_ID}")
        except redis.exceptions.RedisError as e:
            logger.warning(f"Could not send heartbeat: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in heartbeat thread: {e}", exc_info=True)

        shutdown_event.wait(HEARTBEAT_INTERVAL)


def process_diffdock_job(job_data: dict) -> dict:
    """
    Process a DiffDock molecular docking job.

    Args:
        job_data: Dictionary containing job information

    Returns:
        Dictionary containing results in format compatible with send_job_result
    """
    job_id = job_data.get('job_id', 'unknown')
    logger.info(f"Processing DiffDock job {job_id}")
    start_time = time.time()

    try:
        # Extract job parameters - support both old and new field names
        params = job_data.get('params', job_data.get('parameters', {}))

        # Debug: Log received job data structure
        logger.info(f"Job data keys: {list(job_data.keys())}")
        logger.info(f"Params keys: {list(params.keys())}")

        # Get output path from job data (where main output should be written)
        output_path_raw = job_data.get('output_path')
        if not output_path_raw:
            raise ValueError("Missing output_path in job data")

        output_path = Path(output_path_raw).expanduser().resolve()
        output_dir = output_path.parent

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directory: {output_dir}")

        # Protein input - priority: pdb_content > protein_path > protein_pdb > protein > protein_sequence
        protein_pdb = params.get('pdb_content') or job_data.get('protein_pdb') or params.get('pdb') or ''
        protein_path_input = params.get('protein_path') or ''
        protein_sequence = params.get('protein') or job_data.get('protein_sequence') or params.get('sequence') or ''

        # Handle case where PDB content is mistakenly sent as "sequence"
        # PDB files start with keywords like HEADER, ATOM, REMARK
        if protein_sequence and any(keyword in protein_sequence[:200].upper()
                                   for keyword in ['HEADER', 'ATOM ', 'HETATM', 'REMARK', 'CRYST1']):
            logger.info(f"Detected PDB content in sequence field, converting to pdb_content")
            protein_pdb = protein_sequence
            protein_sequence = ''

        # Ligand input - priority: ligand (SMILES, preferred) > sdf_content (legacy) > sdf_path > ligand_sdf
        # NOTE: Backend now converts all file inputs to SMILES, so 'ligand' param is primary
        ligand_smiles = params.get('ligand') or job_data.get('ligand_smiles') or params.get('smiles') or ''
        ligand_sdf = params.get('sdf_content') or job_data.get('ligand_sdf') or ''
        ligand_path_input = params.get('sdf_path') or ''

        parameters = params

        logger.info(f"Protein: pdb={bool(protein_pdb)}, path={bool(protein_path_input)}, seq={bool(protein_sequence)}")
        logger.info(f"Ligand: sdf={bool(ligand_sdf)}, path={bool(ligand_path_input)}, smiles={bool(ligand_smiles)}")

        # Get DiffDock parameters with defaults
        samples_per_complex = parameters.get('samples_per_complex', 10)
        inference_steps = parameters.get('inference_steps', 20)
        actual_steps = parameters.get('actual_steps', 18)
        no_final_step_noise = parameters.get('no_final_step_noise', False)

        # Create temporary directory for DiffDock processing
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            diffdock_output_dir = tmpdir_path / 'output'
            diffdock_output_dir.mkdir(exist_ok=True)

            # Prepare protein input
            protein_arg = ""
            if protein_pdb:
                protein_path = tmpdir_path / 'protein.pdb'
                protein_path.write_text(protein_pdb)
                protein_arg = f"--protein_path {protein_path}"
                logger.info(f"Using PDB file: {protein_path}")
            elif protein_path_input:
                 if os.path.exists(protein_path_input):
                    protein_arg = f"--protein_path {protein_path_input}"
                    logger.info(f"Using PDB file path: {protein_path_input}")
                 else:
                     logger.warning(f"Provided protein_path does not exist: {protein_path_input}")
            
            if not protein_arg and protein_sequence:
                protein_arg = f"--protein_sequence {protein_sequence}"
                logger.info(f"Using protein sequence (will fold with ESMFold)")

            # Prepare ligand input - prefer SMILES (text) over files
            ligand_arg = ""
            if ligand_smiles:
                # SMILES is the preferred input format (converted from files in backend)
                ligand_arg = f'--ligand "{ligand_smiles}"'
                logger.info(f"Using SMILES: {ligand_smiles[:100]}{'...' if len(ligand_smiles) > 100 else ''}")
            elif ligand_sdf:
                # Legacy SDF content support
                ligand_path = tmpdir_path / 'ligand.sdf'
                ligand_path.write_text(ligand_sdf)
                ligand_arg = f"--ligand {ligand_path}"
                logger.info(f"Using SDF file content (legacy): {ligand_path}")
            elif ligand_path_input:
                # Legacy SDF path support
                if os.path.exists(ligand_path_input):
                    ligand_arg = f"--ligand {ligand_path_input}"
                    logger.info(f"Using SDF file path (legacy): {ligand_path_input}")
                else:
                    logger.warning(f"Provided sdf_path does not exist: {ligand_path_input}")

            if not protein_arg or not ligand_arg:
                raise ValueError("Both protein and ligand inputs are required")

            # Build DiffDock command
            cmd = [
                "conda", "run", "-n", DIFFDOCK_CONDA_ENV,
                "python", "-m", "inference",
                "--config", f"{DIFFDOCK_INSTALL_DIR}/default_inference_args.yaml",
                protein_arg,
                ligand_arg,
                f"--out_dir", str(diffdock_output_dir),
                f"--samples_per_complex", str(samples_per_complex),
                f"--inference_steps", str(inference_steps),
                f"--actual_steps", str(actual_steps)
            ]

            if no_final_step_noise:
                cmd.append("--no_final_step_noise")

            # Join the command properly
            cmd_str = " ".join(cmd)
            logger.info(f"Running DiffDock: {cmd_str}")

            # Run DiffDock
            process = subprocess.run(
                cmd_str,
                shell=True,
                cwd=DIFFDOCK_INSTALL_DIR,
                capture_output=True,
                text=True,
                timeout=1800  # 30 minute timeout
            )

            if process.returncode != 0:
                logger.error(f"DiffDock failed with return code {process.returncode}")
                logger.error(f"STDOUT: {process.stdout}")
                logger.error(f"STDERR: {process.stderr}")
                raise RuntimeError(f"DiffDock failed: {process.stderr}")

            logger.info(f"DiffDock completed successfully")
            logger.debug(f"STDOUT: {process.stdout}")

            # Collect output files and sort by rank (rank1 is best)
            sdf_files = sorted(diffdock_output_dir.glob("*.sdf"), key=lambda f: f.name)
            pdb_files = sorted(diffdock_output_dir.glob("*.pdb"), key=lambda f: f.name)

            if not sdf_files and not pdb_files:
                raise RuntimeError("DiffDock produced no output files")

            # Copy files to final output directory and build artifacts list
            artifacts = []
            confidence_scores = []
            main_output_file = None

            # Process PDB files (receptor structures)
            # The FIRST PDB file (receptor/protein) becomes the MAIN OUTPUT
            for i, pdb_file in enumerate(pdb_files):
                dest_filename = pdb_file.name
                dest_path = output_dir / dest_filename
                shutil.copy2(pdb_file, dest_path)

                # First PDB file is the main output (receptor structure)
                if i == 0:
                    main_output_file = str(dest_path)
                    logger.info(f"Main output file (receptor PDB): {dest_path}")

                artifacts.append({
                    'name': f'receptor_{i + 1}',
                    'path': str(dest_path),
                    'filename': dest_filename,
                    'display_name': f'Receptor Structure - {dest_filename}'
                })

            # Process SDF files (ligand poses) - these are ALL artifacts, not main output
            for i, sdf_file in enumerate(sdf_files):
                # Determine filename for output
                dest_filename = sdf_file.name
                dest_path = output_dir / dest_filename

                # Copy file to output directory
                shutil.copy2(sdf_file, dest_path)

                # All SDF files are artifacts (ligand poses)
                artifacts.append({
                    'name': f'pose_{i + 1}',
                    'path': str(dest_path),
                    'filename': dest_filename,
                    'display_name': f'Pose {i + 1} - {dest_filename}'
                })

                # Extract confidence score from filename if available
                # Format: rank1_confidence-1.234.sdf
                if 'confidence' in sdf_file.name:
                    try:
                        score_str = sdf_file.name.split('confidence')[1].split('.sdf')[0]
                        score = float(score_str.replace('-', '-').replace('_', ''))
                        confidence_scores.append(score)
                    except Exception:
                        pass

            # Fallback: If no PDB files but we have SDFs, use the original protein as receptor
            # and save it as the main output file
            if not main_output_file and sdf_files:
                # Create a receptor PDB from the original protein input
                if protein_pdb:
                    receptor_filename = f"{job_id}_receptor.pdb"
                    receptor_path = output_dir / receptor_filename
                    with open(receptor_path, 'w') as f:
                        f.write(protein_pdb)
                    main_output_file = str(receptor_path)
                    logger.warning(f"No receptor PDB from DiffDock, using original protein as receptor: {main_output_file}")
                    
                    # Add receptor as first artifact
                    artifacts.insert(0, {
                        'name': 'receptor_1',
                        'path': str(receptor_path),
                        'filename': receptor_filename,
                        'display_name': f'Receptor Structure - {receptor_filename}'
                    })
                else:
                    # No protein available, use first SDF as main output (with correct extension)
                    sdf_filename = sdf_files[0].name
                    if not sdf_filename.endswith('.sdf'):
                        # Fix incorrect extension
                        sdf_filename = sdf_filename.rsplit('.', 1)[0] + '.sdf'
                    dest_path = output_dir / sdf_filename
                    shutil.copy2(sdf_files[0], dest_path)
                    main_output_file = str(dest_path)
                    logger.warning(f"No receptor PDB found, using first ligand SDF as main output: {main_output_file}")


            processing_time = time.time() - start_time

            # Build result in format compatible with send_job_result and listener
            result = {
                'job_id': job_id,
                'status': 'success',
                'model': WORKER_TYPE,
                'worker_id': WORKER_ID,
                'message': None,
                'execution_time': processing_time,
                'output_file': main_output_file,
                'artifacts': artifacts,
                'metrics': {
                    'num_poses': len(sdf_files),
                    'confidence_scores': confidence_scores
                }
            }

            logger.info(f"Job {job_id} completed in {processing_time:.2f}s with {len(sdf_files)} poses")
            return result

    except subprocess.TimeoutExpired:
        logger.error(f"DiffDock job {job_id} timed out")
        return {
            'job_id': job_id,
            'status': 'error',
            'model': WORKER_TYPE,
            'worker_id': WORKER_ID,
            'message': 'Job timed out after 30 minutes',
            'execution_time': time.time() - start_time
        }
    except Exception as e:
        logger.error(f"Error processing job {job_id}: {e}", exc_info=True)
        return {
            'job_id': job_id,
            'status': 'error',
            'model': WORKER_TYPE,
            'worker_id': WORKER_ID,
            'message': str(e),
            'execution_time': time.time() - start_time
        }


def main_loop():
    """Main worker loop - polls Redis for jobs."""
    global redis_client

    logger.info(f"Starting DiffDock worker {WORKER_ID}")

    # Initialize Redis
    redis_client = initialize_redis()

    # Start heartbeat thread
    global heartbeat_thread
    heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
    heartbeat_thread.start()
    logger.info("Heartbeat thread started")

    # Main processing loop
    queue_key = f'queue:job:{WORKER_TYPE}'
    logger.info(f"Polling queue: {queue_key}")

    while not shutdown_event.is_set():
        try:
            # Poll for jobs (blocking pop with timeout)
            result = redis_client.brpop(queue_key, timeout=5)

            if result:
                _, job_data_bytes = result

                # Decrypt and verify job data
                try:
                    job_data = decrypt(job_data_bytes)
                    job_dict = json.loads(job_data)
                    logger.info(f"Received job: {job_dict.get('job_id', 'unknown')}")
                except Exception as e:
                    logger.error(f"Failed to decrypt/parse job: {e}")
                    continue

                # Process the job
                result_data = process_diffdock_job(job_dict)

                # Send result using shared node_utils (handles signing, encryption, file embedding)
                send_job_result(result_data, NODE_PRIVATE_KEY, redis_client)

                # Cleanup
                gc.collect()

        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            time.sleep(1)

    logger.info("Worker shutting down")


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        shutdown_event.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=5)
        logger.info("Shutdown complete")
