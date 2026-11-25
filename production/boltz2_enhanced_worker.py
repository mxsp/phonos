#!/usr/bin/env python3
"""
Enhanced Boltz2 Protein Structure Prediction Worker Node
Connects to NVIDIA's Boltz2 model API for protein structure prediction with ligand binding.
Improved with additional parameter options and better error handling.
Uses NVCF long-polling for async results.
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
from typing import Optional, Dict, Any, List
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
NVIDIA_BOLTZ2_URL = os.getenv(
    "BOLTZ2_API_URL",
    "https://health.api.nvidia.com/v1/biology/mit/boltz2/predict"
)
NVIDIA_STATUS_URL = os.getenv(
    "BOLTZ2_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "boltz2-enhanced"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_boltz2_enhanced.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings
try:
    NVCF_POLL_SECONDS = max(60, int(os.getenv("BOLTZ2_POLL_SECONDS", "900")))
except (TypeError, ValueError):
    NVCF_POLL_SECONDS = 900

try:
    NVCF_POLL_INTERVAL = max(1, int(os.getenv("BOLTZ2_POLL_INTERVAL", "3")))
except (TypeError, ValueError):
    NVCF_POLL_INTERVAL = 3

try:
    BOLTZ2_STATUS_REQUEST_TIMEOUT = max(10, int(os.getenv("BOLTZ2_STATUS_REQUEST_TIMEOUT", "30")))
except (TypeError, ValueError):
    BOLTZ2_STATUS_REQUEST_TIMEOUT = 30

# Initial API request timeout/retry settings
try:
    BOLTZ2_INITIAL_CONNECT_TIMEOUT = max(5, int(os.getenv("BOLTZ2_INITIAL_CONNECT_TIMEOUT", "20")))
except (TypeError, ValueError):
    BOLTZ2_INITIAL_CONNECT_TIMEOUT = 20

try:
    BOLTZ2_INITIAL_READ_TIMEOUT = max(30, int(os.getenv("BOLTZ2_INITIAL_READ_TIMEOUT", "300")))
except (TypeError, ValueError):
    BOLTZ2_INITIAL_READ_TIMEOUT = 300

try:
    BOLTZ2_INITIAL_REQUEST_RETRIES = max(1, int(os.getenv("BOLTZ2_INITIAL_REQUEST_RETRIES", "3")))
except (TypeError, ValueError):
    BOLTZ2_INITIAL_REQUEST_RETRIES = 3

try:
    BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS = max(
        0.5, float(os.getenv("BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS", "2.0"))
    )
except (TypeError, ValueError):
    BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS = 2.0

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
    """Get Redis job queue key for Boltz2 jobs."""
    if org_id:
        return f"queue:job:{WORKER_TYPE}:org:{org_id}"
    return f"queue:job:{WORKER_TYPE}"


def get_redis_connection(decode_responses: bool = False):
    """Get Redis connection using config or environment."""
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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (boltz2-enhanced) - %(message)s',
        handlers=[
            logging.FileHandler(f'boltz2_enhanced_worker_{worker_id}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)


def clean_nan_values(data: Any) -> Any:
    """Recursively replace NaN and Infinity with None to ensure valid JSON."""
    import math
    if isinstance(data, dict):
        return {k: clean_nan_values(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [clean_nan_values(v) for v in data]
    elif isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            return None
    return data


def validate_sequence_by_type(sequence: str, molecule_type: str) -> bool:
    """
    Validate sequence based on molecule type.
    
    Args:
        sequence: Input sequence
        molecule_type: Type of molecule (protein, dna, rna)
        
    Returns:
        True if valid for the molecule type
    """
    sequence_clean = sequence.upper().replace(' ', '').replace('\n', '').replace('\r', '')
    
    if not sequence_clean:
        return False
    
    if molecule_type == 'protein':
        valid_chars = set('ACDEFGHIKLMNPQRSTVWY')
        return all(c in valid_chars for c in sequence_clean)
    elif molecule_type == 'dna':
        valid_chars = set('ACGT')
        return all(c in valid_chars for c in sequence_clean)
    elif molecule_type == 'rna':
        valid_chars = set('ACGU')
        return all(c in valid_chars for c in sequence_clean)
    
    return False


def build_ligand_definition(ligand_input_type: str, ligand_value: str, ligand_id: str = 'L') -> Optional[Dict[str, Any]]:
    """
    Build ligand definition from different input types.
    
    Args:
        ligand_input_type: Type of ligand input ('smiles', 'ccd', 'none')
        ligand_value: Value of the ligand (SMILES string or CCD code)
        ligand_id: Identifier for the ligand
        
    Returns:
        Ligand dictionary or None if invalid
    """
    if ligand_input_type == 'none' or not ligand_value:
        return None
    
    ligand_def = {"id": ligand_id}
    
    if ligand_input_type == 'smiles':
        ligand_def["smiles"] = ligand_value.strip()
    elif ligand_input_type == 'ccd':
        ccd_clean = ligand_value.upper().strip()[:5]  # CCD codes are max 5 chars
        if ccd_clean:
            ligand_def["ccd"] = ccd_clean
        else:
            logger.warning(f"Invalid CCD code: {ligand_value}")
            return None
    else:
        logger.warning(f"Unsupported ligand input type: {ligand_input_type}")
        return None
    
    return ligand_def


def call_boltz2_api_enhanced(
    polymers: List[Dict[str, Any]],
    ligands: List[Dict[str, Any]] = None,
    # Enhanced prediction parameters
    recycling_steps: int = 3,
    sampling_steps: int = 50,
    diffusion_samples: int = 1,
    step_scale: float = 1.2,
    without_potentials: bool = False,
    # Enhanced affinity parameters
    predict_affinity: bool = False,
    sampling_steps_affinity: int = 200,
    diffusion_samples_affinity: int = 5,
    affinity_mw_correction: bool = False,
    # New advanced parameters
    temperature: float = 1.0,
    noise_scale: float = 1.0,
    use_template: bool = False,
    confidence_threshold: float = 0.5,
    max_recycling_steps: int = 6,
    bond_constraints: bool = True,
    symmetry_constraints: bool = False,
    ensemble_prediction: bool = False,
    ensemble_size: int = 1
) -> Dict[str, Any]:
    """
    Call NVIDIA Boltz2 API with enhanced parameter options.

    Args:
        polymers: List of polymer definitions (protein, dna, rna)
        ligands: List of ligand definitions (optional)
        recycling_steps: Refinement iterations (1-6)
        sampling_steps: Diffusion sampling steps (10-1000)
        diffusion_samples: Independent structures to generate (1-5)
        step_scale: Magnitude per sampling step (0.5-5.0)
        without_potentials: Exclude potential calculations
        predict_affinity: Calculate binding affinity for ligand
        sampling_steps_affinity: Steps for affinity calculation (10-1000)
        diffusion_samples_affinity: Samples for affinity (1-10)
        affinity_mw_correction: Apply molecular weight correction
        # Enhanced parameters
        temperature: Temperature for sampling (0.1-2.0)
        noise_scale: Scale for diffusion noise (0.1-2.0)
        use_template: Use template information if available
        confidence_threshold: Threshold for confidence filtering (0.0-1.0)
        max_recycling_steps: Maximum recycling steps allowed (1-6)
        bond_constraints: Apply bond length constraints
        symmetry_constraints: Apply symmetry constraints
        ensemble_prediction: Generate ensemble of predictions
        ensemble_size: Number of predictions in ensemble (1-5)

    Returns:
        Dictionary with predicted structures and enhanced metrics
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Base payload with core parameters
    payload = {
        "polymers": polymers,
        "recycling_steps": min(max(1, recycling_steps), max_recycling_steps),
        "sampling_steps": min(max(10, sampling_steps), 1000),
        "diffusion_samples": min(max(1, diffusion_samples), 5),
        "step_scale": min(max(0.5, step_scale), 5.0),
        "without_potentials": without_potentials
    }

    # Add enhanced parameters if supported by API
    enhanced_params = {
        "temperature": min(max(0.1, temperature), 2.0),
        "noise_scale": min(max(0.1, noise_scale), 2.0),
        "use_template": use_template,
        "confidence_threshold": min(max(0.0, confidence_threshold), 1.0),
        "bond_constraints": bond_constraints,
        "symmetry_constraints": symmetry_constraints
    }
    
    # Add ensemble parameters
    if ensemble_prediction and ensemble_size > 1:
        enhanced_params["ensemble_size"] = min(max(1, ensemble_size), 5)
        enhanced_params["diffusion_samples"] = min(max(1, diffusion_samples), 5)
    
    payload.update(enhanced_params)

    # Add ligands if present
    if ligands:
        payload["ligands"] = ligands
        
        # Enhanced affinity parameters
        if predict_affinity and len(ligands) == 1:
            ligands[0]["predict_affinity"] = True
            payload.update({
                "sampling_steps_affinity": min(max(10, sampling_steps_affinity), 1000),
                "diffusion_samples_affinity": min(max(1, diffusion_samples_affinity), 10),
                "affinity_mw_correction": affinity_mw_correction
            })

    logger.info(f"Calling Enhanced Boltz2 API with {len(polymers)} polymers and {len(ligands or [])} ligands...")
    logger.debug(f"Payload keys: {list(payload.keys())}")

    # Initial request (NVIDIA can take a while before returning 202 task ids)
    response = None
    for attempt in range(1, BOLTZ2_INITIAL_REQUEST_RETRIES + 1):
        try:
            response = requests.post(
                url=NVIDIA_BOLTZ2_URL,
                headers=headers,
                json=payload,
                timeout=(BOLTZ2_INITIAL_CONNECT_TIMEOUT, BOLTZ2_INITIAL_READ_TIMEOUT)
            )
            # Retry transient gateway/proxy responses from upstream.
            if response.status_code in (502, 503, 504):
                logger.warning(
                    "Initial enhanced Boltz2 request got HTTP %s "
                    f"(attempt {attempt}/{BOLTZ2_INITIAL_REQUEST_RETRIES})",
                    response.status_code
                )
                if attempt >= BOLTZ2_INITIAL_REQUEST_RETRIES:
                    break
                time.sleep(BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS * attempt)
                continue
            break
        except requests.exceptions.Timeout:
            logger.warning(
                "Initial enhanced Boltz2 request timed out "
                f"(attempt {attempt}/{BOLTZ2_INITIAL_REQUEST_RETRIES}, "
                f"connect={BOLTZ2_INITIAL_CONNECT_TIMEOUT}s, read={BOLTZ2_INITIAL_READ_TIMEOUT}s)"
            )
            if attempt >= BOLTZ2_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.RequestException as req_err:
            logger.warning(
                "Initial enhanced Boltz2 request failed "
                f"(attempt {attempt}/{BOLTZ2_INITIAL_REQUEST_RETRIES}): {req_err}"
            )
            if attempt >= BOLTZ2_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS * attempt)

    if response is None:
        raise requests.exceptions.Timeout("Enhanced Boltz2 initial request did not return a response")

    # Check for immediate response or async task
    if response.status_code == 200:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        else:
            raise Exception(f"Unexpected content type: {content_type}")

    elif response.status_code == 202:
        # Async task - need to poll for results
        nvcf_reqid = response.headers.get("NVCF-REQID")
        if not nvcf_reqid:
            raise Exception("No NVCF-REQID in 202 response")

        logger.info(f"Enhanced async task started, polling for results (task_id: {nvcf_reqid})")
        return poll_nvcf_status_enhanced(nvcf_reqid)

    else:
        raise Exception(f"Enhanced Boltz2 API error: {response.status_code} - {response.text[:500]}")


def poll_nvcf_status_enhanced(task_id: str) -> Dict[str, Any]:
    """
    Enhanced polling of NVCF status endpoint with better error handling and progress tracking.
    
    Args:
        task_id: NVCF request ID
        
    Returns:
        Dictionary with results
    """
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Accept": "application/json"
    }

    status_url = NVIDIA_STATUS_URL.format(task_id=task_id)
    start_time = time.time()
    last_status = None
    poll_count = 0

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            poll_count += 1
            response = requests.get(
                status_url,
                headers=headers,
                timeout=BOLTZ2_STATUS_REQUEST_TIMEOUT
            )

            if response.status_code == 200:
                # Task complete
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    result = response.json()
                    result['poll_count'] = poll_count
                    result['total_poll_time'] = time.time() - start_time
                    return result
                else:
                    result = {"raw_content": response.text[:5000], 'poll_count': poll_count}
                    return result

            elif response.status_code == 202:
                # Still processing - log progress every 10 polls
                if poll_count % 10 == 0:
                    elapsed = time.time() - start_time
                    logger.info(f"Task {task_id} still processing... (poll #{poll_count}, {elapsed:.1f}s elapsed)")
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            elif response.status_code == 404:
                raise Exception(f"Task {task_id} not found")
            elif response.status_code >= 500:
                logger.warning(
                    "Enhanced task %s status returned %s, retrying...",
                    task_id,
                    response.status_code
                )
                time.sleep(NVCF_POLL_INTERVAL)
                continue
            else:
                raise Exception(f"Status check error: {response.status_code} - {response.text[:500]}")

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout polling status for {task_id} (poll #{poll_count}), retrying...")
            time.sleep(NVCF_POLL_INTERVAL)
            continue
        except Exception as e:
            logger.warning(f"Error polling status for {task_id}: {e}")
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Task {task_id} timed out after {NVCF_POLL_SECONDS} seconds ({poll_count} polls)")


def process_boltz2_enhanced_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Process a single enhanced Boltz2 structure prediction job.

    Args:
        job: Job dictionary containing sequences and enhanced parameters
        worker_id: Worker identifier

    Returns:
        Result dictionary with predicted structure and enhanced metrics
    """
    job_id = job.get('job_id', 'unknown')
    result = {
        'job_id': job_id,
        'status': 'error',
        'model_used': 'boltz2-enhanced',
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})

        # Build polymers list from job parameters
        polymers = []

        # Handle multiple sequence inputs
        sequences_input = params.get('sequences', [])
        if not sequences_input:
            # Fallback to single sequence input
            sequence = params.get('sequence', params.get('protein_sequence', ''))
            if sequence:
                sequences_input = [sequence]

        if not sequences_input:
            result['message'] = "No sequences provided"
            return result

        # Process each sequence
        for i, seq_input in enumerate(sequences_input):
            if isinstance(seq_input, dict):
                # If sequence is provided as dict with type info
                molecule_type = seq_input.get('molecule_type', 'protein')
                sequence = seq_input.get('sequence', '')
                chain_id = seq_input.get('id', chr(65 + i))  # A, B, C, etc.
                is_cyclic = seq_input.get('cyclic', False)
            else:
                # If sequence is provided as string
                molecule_type = params.get('molecule_type', 'protein')
                sequence = seq_input
                chain_id = chr(65 + i)  # A, B, C, etc.
                is_cyclic = params.get('cyclic', False)

            # Clean and validate sequence
            sequence_clean = sequence.upper().replace(' ', '').replace('\n', '').replace('\r', '')

            if not validate_sequence_by_type(sequence_clean, molecule_type):
                result['message'] = f"Invalid {molecule_type} sequence at position {i+1}"
                return result

            # Check sequence length (1-4096)
            if len(sequence_clean) < 1 or len(sequence_clean) > 4096:
                result['message'] = f"Sequence {i+1} length must be between 1 and 4096 characters (got {len(sequence_clean)})"
                return result

            # Build polymer definition
            polymer = {
                "molecule_type": molecule_type,
                "sequence": sequence_clean,
                "id": chain_id
            }
            if is_cyclic:
                polymer["cyclic"] = True

            polymers.append(polymer)

        # Build ligands list with enhanced support
        ligands = []
        ligand_inputs = params.get('ligands', [])
        
        if isinstance(ligand_inputs, dict):
            # Single ligand as dict
            ligand_def = build_ligand_definition(
                ligand_inputs.get('input_type', 'none'),
                ligand_inputs.get('value', ''),
                ligand_inputs.get('id', 'L')
            )
            if ligand_def:
                ligands.append(ligand_def)
        elif isinstance(ligand_inputs, list):
            # Multiple ligands
            for i, ligand_input in enumerate(ligand_inputs):
                ligand_def = build_ligand_definition(
                    ligand_input.get('input_type', 'none'),
                    ligand_input.get('value', ''),
                    ligand_input.get('id', f'L{i+1}')
                )
                if ligand_def:
                    ligands.append(ligand_def)

        # Extract enhanced prediction parameters
        recycling_steps = int(params.get('recycling_steps', 3))
        sampling_steps = int(params.get('sampling_steps', 50))
        diffusion_samples = int(params.get('diffusion_samples', 1))
        step_scale = float(params.get('step_scale', 1.2))
        without_potentials = params.get('without_potentials', False)

        # Enhanced parameters
        predict_affinity = params.get('predict_affinity', False)
        sampling_steps_affinity = int(params.get('sampling_steps_affinity', 200))
        diffusion_samples_affinity = int(params.get('diffusion_samples_affinity', 5))
        affinity_mw_correction = params.get('affinity_mw_correction', False)

        # New advanced parameters
        temperature = float(params.get('temperature', 1.0))
        noise_scale = float(params.get('noise_scale', 1.0))
        use_template = params.get('use_template', False)
        confidence_threshold = float(params.get('confidence_threshold', 0.5))
        max_recycling_steps = int(params.get('max_recycling_steps', 6))
        bond_constraints = params.get('bond_constraints', True)
        symmetry_constraints = params.get('symmetry_constraints', False)
        ensemble_prediction = params.get('ensemble_prediction', False)
        ensemble_size = int(params.get('ensemble_size', 1))

        logger.info(f"Job {job_id}: Enhanced prediction for {len(polymers)} polymers and {len(ligands)} ligands...")

        # Call Enhanced Boltz2 API
        api_result = call_boltz2_api_enhanced(
            polymers=polymers,
            ligands=ligands if ligands else None,
            recycling_steps=recycling_steps,
            sampling_steps=sampling_steps,
            diffusion_samples=diffusion_samples,
            step_scale=step_scale,
            without_potentials=without_potentials,
            predict_affinity=predict_affinity,
            sampling_steps_affinity=sampling_steps_affinity,
            diffusion_samples_affinity=diffusion_samples_affinity,
            affinity_mw_correction=affinity_mw_correction,
            temperature=temperature,
            noise_scale=noise_scale,
            use_template=use_template,
            confidence_threshold=confidence_threshold,
            max_recycling_steps=max_recycling_steps,
            bond_constraints=bond_constraints,
            symmetry_constraints=symmetry_constraints,
            ensemble_prediction=ensemble_prediction,
            ensemble_size=ensemble_size
        )

        execution_time = time.time() - start_time

        # Extract enhanced metrics
        confidence_scores = api_result.get('confidence_scores', [])
        ptm_scores = api_result.get('ptm_scores', [])
        iptm_scores = api_result.get('iptm_scores', [])
        complex_plddt = api_result.get('complex_plddt_scores', [])
        structures = api_result.get('structures', [])
        affinities = api_result.get('affinities', {})
        poll_count = api_result.get('poll_count', 0)

        if not structures:
            result['status'] = 'error'
            result['message'] = "Enhanced Boltz2 API completed but returned no structures. This may be due to sequence complexity or enhanced parameter constraints."
            logger.error(f"Job {job_id}: No structures returned by enhanced API")
            return result

        # Get primary structure content
        primary_structure = ""
        structure_format = 'mmcif'

        # Prepare enhanced artifacts for all structures
        artifacts = []
        num_structures = len(structures)
        
        logger.info(f"Job {job_id}: Processing {num_structures} enhanced structure(s) from Boltz2 API")

        if structures:
            # Use first structure as primary
            primary_structure = structures[0].get('structure', '')
            structure_format = structures[0].get('format', 'mmcif')

            # Process all structures with enhanced metadata
            for i, struct in enumerate(structures):
                content = struct.get('structure', '')
                fmt = struct.get('format', 'mmcif')
                ext = '.cif' if fmt == 'mmcif' else '.pdb'

                if content:
                    content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')

                    # Enhanced naming with parameter info
                    if num_structures > 1:
                        filename = f"{job_id}_enhanced_model_{i}{ext}"
                        display_name = f"Enhanced Model {i+1}"
                        if i < len(confidence_scores):
                            display_name += f" (conf: {confidence_scores[i]:.3f})"
                    else:
                        filename = f"{job_id}_enhanced{ext}"
                        display_name = "Enhanced Structure"

                    artifact_data = {
                        "filename": filename,
                        "content_b64": content_b64,
                        "name": "enhanced_structure_model",
                        "display_name": display_name,
                        "model_index": i,
                        "confidence_score": confidence_scores[i] if i < len(confidence_scores) else None,
                        "ptm_score": ptm_scores[i] if i < len(ptm_scores) else None,
                        "iptm_score": iptm_scores[i] if i < len(iptm_scores) else None,
                        "enhanced_parameters": {
                            "recycling_steps": recycling_steps,
                            "sampling_steps": sampling_steps,
                            "temperature": temperature,
                            "noise_scale": noise_scale,
                            "bond_constraints": bond_constraints,
                            "symmetry_constraints": symmetry_constraints
                        }
                    }
                    artifacts.append(artifact_data)

        # Enhanced output data with full parameter tracking
        output_data = json.dumps({
            'polymers': [{'type': p['molecule_type'], 'length': len(p['sequence']), 'id': p['id']} for p in polymers],
            'num_polymers': len(polymers),
            'ligands': ligands,
            'num_ligands': len(ligands),
            # Core parameters
            'recycling_steps': recycling_steps,
            'sampling_steps': sampling_steps,
            'diffusion_samples': diffusion_samples,
            'step_scale': step_scale,
            'without_potentials': without_potentials,
            # Enhanced parameters
            'temperature': temperature,
            'noise_scale': noise_scale,
            'use_template': use_template,
            'confidence_threshold': confidence_threshold,
            'max_recycling_steps': max_recycling_steps,
            'bond_constraints': bond_constraints,
            'symmetry_constraints': symmetry_constraints,
            'ensemble_prediction': ensemble_prediction,
            'ensemble_size': ensemble_size,
            # Affinity parameters
            'predict_affinity': predict_affinity,
            'sampling_steps_affinity': sampling_steps_affinity,
            'diffusion_samples_affinity': diffusion_samples_affinity,
            'affinity_mw_correction': affinity_mw_correction,
            # Results
            'confidence_scores': confidence_scores,
            'ptm_scores': ptm_scores,
            'iptm_scores': iptm_scores,
            'complex_plddt_scores': complex_plddt,
            'affinities': affinities,
            'execution_time': execution_time,
            'poll_count': poll_count,
            'api_response': api_result
        }, indent=2)

        # Get output filename
        output_path = job.get('output_path', '')
        output_filename = os.path.basename(output_path) if output_path else f"{job_id}_enhanced.json"

        # Encode output as base64
        output_content_b64 = base64.b64encode(output_data.encode('utf-8')).decode('utf-8')

        # Also save structure file separately
        structure_file_b64 = None
        if primary_structure:
            structure_file_b64 = base64.b64encode(primary_structure.encode('utf-8')).decode('utf-8')

        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename,
            'structure_content': structure_file_b64,
            'structure_filename': output_filename.replace('.json', f'.{structure_format}'),
            'structure_format': structure_format,
            'artifacts': artifacts,
            'num_structures': num_structures,
            # Enhanced result metadata
            'polymers': [{'type': p['molecule_type'], 'length': len(p['sequence']), 'id': p['id']} for p in polymers],
            'num_polymers': len(polymers),
            'ligands': ligands,
            'num_ligands': len(ligands),
            'enhanced_parameters': {
                'recycling_steps': recycling_steps,
                'sampling_steps': sampling_steps,
                'diffusion_samples': diffusion_samples,
                'step_scale': step_scale,
                'temperature': temperature,
                'noise_scale': noise_scale,
                'use_template': use_template,
                'confidence_threshold': confidence_threshold,
                'bond_constraints': bond_constraints,
                'symmetry_constraints': symmetry_constraints,
                'ensemble_prediction': ensemble_prediction,
                'ensemble_size': ensemble_size
            },
            # Affinity results
            'predict_affinity': predict_affinity,
            'affinities': affinities,
            'affinity_parameters': {
                'sampling_steps_affinity': sampling_steps_affinity,
                'diffusion_samples_affinity': diffusion_samples_affinity,
                'affinity_mw_correction': affinity_mw_correction
            } if predict_affinity else {},
            # All scores
            'confidence_scores': confidence_scores,
            'ptm_scores': ptm_scores,
            'iptm_scores': iptm_scores,
            'complex_plddt_scores': complex_plddt,
            # Compatibility with existing UI
            'confidence_score': confidence_scores[0] if confidence_scores else None,
            'ptm_score': ptm_scores[0] if ptm_scores else None,
            'iptm_score': iptm_scores[0] if iptm_scores else None,
            'complex_plddt': complex_plddt[0] if complex_plddt else None,
            'poll_count': poll_count,
            'message': None
        })

        logger.info(f"Job {job_id}: Enhanced prediction completed in {execution_time:.2f}s")
        logger.info(f"Job {job_id}: Generated {num_structures} enhanced model(s) in {poll_count} polls")
        if confidence_scores:
            logger.info(f"Job {job_id}: Enhanced confidence scores: {[f'{s:.3f}' for s in confidence_scores]}")

    except requests.exceptions.Timeout:
        result['message'] = "Enhanced Boltz2 API request timed out"
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
    logger.info("Enhanced heartbeat thread started.")

    try:
        r = get_redis_connection()
        logger.debug(f"Successfully got Redis connection for enhanced worker {worker_id}")

        while not stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                r.ping()
                logger.debug(f"Enhanced Redis ping successful for worker {worker_id}")

                heartbeat_data = {
                    'worker_id': worker_id,
                    'worker_type': WORKER_TYPE,
                    'timestamp': time.time(),
                    'enhanced': True
                }
                logger.debug(f"Private key available for enhanced heartbeat: {bool(private_key)}")

                if private_key and canonical_dumps:
                    payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
                    signature = sign_message(private_key, payload_bytes)
                    if signature:
                        signed_message = {'payload': heartbeat_data, 'signature': signature}
                        r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(signed_message))
                        logger.debug(f"Published enhanced signed heartbeat for worker {worker_id}")
                else:
                    r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))

            except Exception as e:
                logger.warning(f"Could not send enhanced heartbeat (or Redis ping failed): {e}")

        logger.info("Enhanced heartbeat thread stopping.")
    except Exception as e:
        logger.critical(f"FATAL: Enhanced heartbeat thread encountered an unhandled exception: {e}", exc_info=True)


def worker_loop(
    worker_id: str,
    private_key: Optional[str],
    organization_id: Optional[int],
    stop_event: threading.Event
):
    """Main enhanced worker loop - fetches and processes Boltz2 jobs."""
    r = get_redis_connection(decode_responses=False)
    r_text = get_redis_connection(decode_responses=True)
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"Enhanced Boltz2 worker started. Listening on queue: {job_queue}")

    while not stop_event.is_set():
        try:
            job_payload = r.brpop(job_queue, timeout=1)

            if not job_payload:
                continue

            _, encrypted_data = job_payload

            # Decrypt job data if encryption available
            if decrypt and isinstance(encrypted_data, bytes):
                try:
                    decrypted = decrypt(encrypted_data)
                    if decrypted:
                        job_data = decrypted.decode('utf-8')
                    else:
                        job_data = encrypted_data.decode('utf-8')
                except Exception as e:
                    logger.warning(f"Decryption failed, trying as plain text: {e}")
                    job_data = encrypted_data.decode('utf-8') if isinstance(encrypted_data, bytes) else encrypted_data
            else:
                job_data = encrypted_data.decode('utf-8') if isinstance(encrypted_data, bytes) else encrypted_data

            job = json.loads(job_data)

            job_id = job.get('job_id', 'unknown')
            logger.info(f"Received enhanced job: {job_id}")

            # Process enhanced job
            result = process_boltz2_enhanced_job(job, worker_id)

            # Send result back
            if send_job_result:
                send_job_result(result, private_key, r)
            else:
                result_json = json.dumps(result)
                if encrypt:
                    encrypted_result = encrypt(result_json.encode('utf-8'))
                    r.lpush(JOB_RESULTS_QUEUE, encrypted_result)
                else:
                    r_text.lpush(JOB_RESULTS_QUEUE, result_json)

            logger.info(f"Enhanced job {job_id} completed with status: {result['status']}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid job JSON: {e}")
        except Exception as e:
            logger.error(f"Enhanced worker loop error: {e}", exc_info=True)
            time.sleep(1)

    logger.info("Enhanced worker loop stopped.")


def load_node_config() -> Dict:
    """Load enhanced node configuration from file."""
    config_path = _resolve_runtime_path(NODE_CONFIG_FILE)
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def main():
    """Main entry point for Enhanced Boltz2 worker."""
    global NODE_CONFIG_FILE
    global logger

    parser = argparse.ArgumentParser(description='Enhanced Boltz2 Protein Structure Prediction Worker')
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
    logger.info(f"Enhanced Boltz2 Protein Structure Prediction Worker Starting")
    logger.info(f"Worker ID: {worker_id}")
    logger.info(f"Organization ID: {organization_id or 'None (public)'}")
    logger.info(f"API URL: {NVIDIA_BOLTZ2_URL}")
    logger.info("=" * 60)

    # Create stop event
    stop_event = threading.Event()

    # Start enhanced heartbeat thread
    heartbeat_thread = threading.Thread(
        target=send_heartbeat,
        args=(worker_id, private_key, stop_event),
        daemon=True
    )
    heartbeat_thread.start()

    try:
        # Run main enhanced worker loop
        worker_loop(worker_id, private_key, organization_id, stop_event)
    except KeyboardInterrupt:
        logger.info("Enhanced shutdown requested...")
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=5)
        logger.info("Enhanced Boltz2 worker shutdown complete.")


# Standalone enhanced test function
def test_boltz2_enhanced_api():
    """Test the Enhanced Boltz2 API with advanced parameters."""
    print("Testing Enhanced Boltz2 API...")

    # Enhanced test with protein + ligand + advanced parameters
    test_polymers = [{
        "id": "A",
        "molecule_type": "protein",
        "sequence": "GIVEQCCTSICSLYQLENYCN"  # Short insulin chain A
    }]

    test_ligands = [{
        "id": "LIG",
        "smiles": "CC(C)C1=NC(=O)N(C)C(=N1)C"  # Simple test ligand
    }]

    try:
        result = call_boltz2_api_enhanced(
            polymers=test_polymers,
            ligands=test_ligands,
            recycling_steps=2,
            sampling_steps=50,
            diffusion_samples=1,
            temperature=1.2,
            noise_scale=0.8,
            bond_constraints=True,
            symmetry_constraints=False,
            predict_affinity=True,
            sampling_steps_affinity=100,
            ensemble_prediction=False
        )

        print(f"Enhanced Success!")
        print(f"Confidence scores: {result.get('confidence_scores', [])}")
        print(f"PTM scores: {result.get('ptm_scores', [])}")
        print(f"Affinities: {result.get('affinities', {})}")
        print(f"Poll count: {result.get('poll_count', 0)}")

        structures = result.get('structures', [])
        if structures:
            print(f"Generated {len(structures)} enhanced structure(s)")
            
            # Save enhanced structure
            struct_file = Path("boltz2_enhanced_test_structure.cif")
            struct_file.write_text(structures[0].get('structure', ''))
            print(f"Enhanced structure saved to: {struct_file}")

        # Save full enhanced result
        output_file = Path("boltz2_enhanced_test_output.json")
        output_file.write_text(json.dumps(result, indent=2))
        print(f"Full enhanced result saved to: {output_file}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False

    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        # Run enhanced API test
        test_boltz2_enhanced_api()
    else:
        # Run as enhanced worker
        main()
