#!/usr/bin/env python3
"""
AlphaFold2-RE-Multimer Protein Structure Prediction Worker Node
Connects to NVIDIA's AlphaFold2-Multimer model API for multi-protein complex prediction.
Uses the multimer-specific endpoint for protein complex predictions.
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
# AlphaFold2-Multimer is available via NVIDIA Health API
# API key must be generated from https://build.nvidia.com/deepmind/alphafold2-multimer
NVIDIA_ALPHAFOLD2_MULTIMER_URL = os.getenv(
    "ALPHAFOLD2_RE_MULTIMER_API_URL",
    "https://health.api.nvidia.com/v1/biology/deepmind/alphafold2-multimer"
)
NVIDIA_STATUS_URL = os.getenv(
    "ALPHAFOLD2_RE_STATUS_URL",
    "https://health.api.nvidia.com/v1/status/{task_id}"
)
LEGACY_NVIDIA_STATUS_URL = "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "alphafold2-re-multimer"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_alphafold2_re_multimer.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"
NVCF_POLL_SECONDS = 7200   # 2-hour max poll window for long AlphaFold2-Multimer jobs
NVCF_POLL_INTERVAL = 5
MAX_SEQUENCE_LENGTH = 4096
MAX_MULTIMER_SEQUENCES = 6

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
    """Get Redis job queue key for AlphaFold2-RE-Multimer jobs."""
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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (alphafold2-re-multimer) - %(message)s',
        handlers=[
            logging.FileHandler(f'alphafold2_re_multimer_worker_{worker_id}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)


def validate_protein_sequences(sequences: List[str]) -> bool:
    """
    Validate all protein sequences contain only standard amino acids.
    
    Args:
        sequences: List of protein sequences to validate
        
    Returns:
        True if all valid, False otherwise
    """
    valid_aa = set('ACDEFGHIKLMNPQRSTVWY')
    
    for i, sequence in enumerate(sequences):
        sequence_clean = sequence.upper().replace(' ', '').replace('\n', '').replace('\r', '')
        
        if not sequence_clean:
            logger.error(f"Sequence {i+1} is empty")
            return False
            
        if not all(aa in valid_aa for aa in sequence_clean):
            logger.error(f"Sequence {i+1} contains invalid amino acids")
            return False
    
    return True


def _looks_like_structure_text(candidate: Any) -> bool:
    """Return True when text appears to contain molecular coordinates."""
    if not isinstance(candidate, str):
        return False
    text = candidate.strip()
    if not text:
        return False
    upper = text.upper()
    return (
        'ATOM' in upper or
        'HETATM' in upper or
        '_ATOM_SITE.' in upper or
        text.startswith('data_')
    )


def _decode_structure_b64(candidate: Any) -> Optional[str]:
    """Decode base64 only when the payload looks like structure text."""
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except Exception:
        return None

    try:
        text = decoded.decode('utf-8')
    except UnicodeDecodeError:
        text = decoded.decode('utf-8', errors='ignore')
    return text if _looks_like_structure_text(text) else None


def _extract_structure_from_entry(entry: Any, default_format: str = 'pdb') -> tuple[str, str]:
    """Extract one structure payload from a response entry."""
    if isinstance(entry, str):
        if _looks_like_structure_text(entry):
            return entry, default_format
        decoded = _decode_structure_b64(entry)
        if decoded:
            return decoded, default_format
        return '', default_format

    if not isinstance(entry, dict):
        return '', default_format

    entry_format = str(entry.get('format') or default_format or 'pdb').lower()
    text_keys = (
        'content', 'structure', 'pdb_content', 'pdb',
        'cif_content', 'cif', 'mmcif',
    )
    for key in text_keys:
        candidate = entry.get(key)
        if _looks_like_structure_text(candidate):
            detected_format = 'cif' if key in ('cif_content', 'cif', 'mmcif') else entry_format
            return candidate, detected_format

    for key in ('content_b64', 'structure_b64', 'pdb_b64', 'cif_b64'):
        decoded = _decode_structure_b64(entry.get(key))
        if decoded:
            detected_format = 'cif' if key == 'cif_b64' else entry_format
            return decoded, detected_format

    for collection_key in ('pdbs', 'structures', 'outputs', 'predictions', 'samples'):
        collection = entry.get(collection_key)
        if not isinstance(collection, list):
            continue
        for child in collection:
            structure_text, detected_format = _extract_structure_from_entry(child, entry_format)
            if structure_text:
                return structure_text, detected_format

    return '', entry_format


def _extract_structure_from_api_result(api_result: Dict[str, Any], default_format: str = 'pdb') -> tuple[str, str]:
    """Extract the first usable structure payload from an API response."""
    return _extract_structure_from_entry(api_result, default_format)


def _extract_sequence_text(entry: Any) -> str:
    """Normalize one sequence entry from string or chain-like dict payloads."""
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ''

    for key in ('sequence', 'protein_sequence'):
        candidate = entry.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    protein_entry = entry.get('protein')
    if isinstance(protein_entry, dict):
        candidate = protein_entry.get('sequence')
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    return ''


def _normalize_multimer_sequences(sequence_entries: Any) -> List[str]:
    """Normalize string/dict multimer inputs into plain sequence strings."""
    if not isinstance(sequence_entries, list):
        return []

    sequences: List[str] = []
    for entry in sequence_entries:
        sequence = _extract_sequence_text(entry)
        if sequence:
            sequences.append(sequence)
    return sequences


def _extract_task_id(response: requests.Response) -> Optional[str]:
    """Extract async task/request id from NVIDIA response."""
    for header in ("NVCF-REQID", "Nvcf-Reqid", "nvcf-reqid", "X-Request-Id", "x-request-id"):
        task_id = response.headers.get(header)
        if task_id:
            return task_id.strip()

    content_type = response.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        for key in ("task_id", "taskId", "reqid", "request_id", "requestId", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _is_async_response(response: requests.Response) -> bool:
    """True when response indicates task exists and can be polled."""
    task_id = _extract_task_id(response)
    if not task_id:
        return False
    if response.status_code == 202:
        return True
    if response.status_code == 504:
        status_header = (
            response.headers.get("NVCF-STATUS")
            or response.headers.get("Nvcf-Status")
            or response.headers.get("nvcf-status")
            or ""
        ).strip().lower()
        return status_header not in {"errored", "error", "failed"}
    return False


def _status_url_candidates(task_id: str) -> List[str]:
    """Return status URLs in priority order."""
    primary = NVIDIA_STATUS_URL.format(task_id=task_id)
    legacy = LEGACY_NVIDIA_STATUS_URL.format(task_id=task_id)
    if primary == legacy:
        return [primary]
    return [primary, legacy]


def call_alphafold2_multimer_api(
    sequences: List[str],
    databases: List[str] = None,
    algorithm: str = "jackhmmer",
    e_value: float = 0.0001,
    bit_score: Optional[float] = None,
    iterations: int = 1,
    relax_prediction: bool = True,
    structure_models_to_relax: str = "all",
    num_predictions_per_model: int = 1,
    max_msa_sequences: Optional[int] = None,
    template_searcher: str = "hhsearch"
) -> Dict[str, Any]:
    """
    Call NVIDIA AlphaFold2-Multimer API for protein complex structure prediction.

    Args:
        sequences: List of protein amino acid sequences (1-6 sequences)
        databases: List of databases for MSA (uniref90, mgnify, small_bfd)
        algorithm: MSA algorithm (jackhmmer or mmseqs2, default: jackhmmer)
        e_value: E-value threshold for MSA filtering (0 < x <= 10, default: 0.0001)
        bit_score: Bit-score threshold for MSA filtering (overrides e_value if provided)
        iterations: Number of MSA iterations (min: 1, default: 1)
        relax_prediction: Whether to run structural relaxation (default: true)
        structure_models_to_relax: Which models to relax - all/best/none (default: all)
        num_predictions_per_model: Number of predictions per model (1-8, default: 1)
        max_msa_sequences: Maximum MSA sequences (1-100000, optional)
        template_searcher: Template search method - hhsearch/hmmsearch (default: hhsearch)

    Returns:
        Dictionary with predicted structure and metadata
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Short poll: get 202 + task ID quickly, then poll handles the long wait.
        "NVCF-POLL-SECONDS": "30",
    }

    # Set default databases if not provided
    if not databases:
        databases = ["uniref90", "small_bfd", "mgnify"]

    # Normalize legacy spelling used by some older jobs.
    databases = ["small_bfd" if db == "smallbfd" else db for db in databases]

    if not (1 <= len(sequences) <= MAX_MULTIMER_SEQUENCES):
        raise ValueError(f"AlphaFold2-Multimer supports 1-{MAX_MULTIMER_SEQUENCES} sequences, got {len(sequences)}")

    payload = {
        "sequences": sequences,  # Multimer expects array of sequences
        "databases": databases,
        "algorithm": algorithm,
        "relax_prediction": relax_prediction,
        "structure_models_to_relax": structure_models_to_relax,
        "e_value": float(e_value),
        "iterations": max(1, int(iterations)),
        "num_predictions_per_model": min(max(1, int(num_predictions_per_model)), 8),
    }

    if bit_score is not None:
        payload["bit_score"] = float(bit_score)

    if max_msa_sequences is not None:
        payload["max_msa_sequences"] = min(max(1, int(max_msa_sequences)), 100000)

    if template_searcher in ("hhsearch", "hmmsearch"):
        payload["template_searcher"] = template_searcher
    elif template_searcher == "none":
        payload["skip_template_search"] = True

    total_length = sum(len(seq) for seq in sequences)
    logger.info(f"Calling AlphaFold2-Multimer API for {len(sequences)} sequences (total {total_length} aa)")
    logger.debug(f"Payload: {json.dumps(payload, indent=2)[:800]}...")

    # connect timeout = 30s; read timeout = 90s (exceeds NVCF-POLL-SECONDS of 30)
    response = requests.post(
        url=NVIDIA_ALPHAFOLD2_MULTIMER_URL,
        headers=headers,
        json=payload,
        timeout=(30, 90)
    )

    if response.status_code == 200:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        else:
            # AlphaFold2 returns PDB format directly
            return {
                "pdb_content": response.text,
                "format": "pdb",
                "num_sequences": len(sequences),
                "sequence_lengths": [len(seq) for seq in sequences],
                "total_length": total_length,
                "databases_used": databases,
                "algorithm": algorithm,
                "iterations": iterations,
                "relax_prediction": relax_prediction
            }
    elif _is_async_response(response):
        task_id = _extract_task_id(response)
        if not task_id:
            raise Exception("AlphaFold2-Multimer async response missing task ID")

        logger.info("AlphaFold2-Multimer async task started: %s (status=%s)", task_id, response.status_code)
        return poll_nvcf_status(task_id, headers)
    else:
        nvcf_status = (
            response.headers.get("NVCF-STATUS")
            or response.headers.get("Nvcf-Status")
            or ""
        ).strip().lower()
        body_preview = response.text[:300] if response.text else "(empty)"
        if nvcf_status in ("errored", "error", "failed"):
            raise Exception(
                f"AlphaFold2-Multimer: NVIDIA model execution failed "
                f"(HTTP {response.status_code}, NVCF-STATUS: {nvcf_status}). "
                f"Response: {body_preview}"
            )
        raise Exception(
            f"AlphaFold2-Multimer API error: HTTP {response.status_code}, "
            f"NVCF-STATUS: {nvcf_status or '(none)'}, body: {body_preview}"
        )


def poll_nvcf_status(task_id: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Poll NVIDIA NVCF status endpoint for async AlphaFold2-Multimer tasks."""
    status_headers = {
        "Authorization": headers.get("Authorization", ""),
        "Accept": "application/json"
    }
    start_time = time.time()

    while time.time() - start_time < NVCF_POLL_SECONDS:
        saw_pending = False
        try:
            for status_url in _status_url_candidates(task_id):
                response = requests.get(status_url, headers=status_headers, timeout=30)

                if response.status_code == 200:
                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return response.json()
                    return {"pdb_content": response.text, "format": "pdb"}

                if response.status_code in (202, 504):
                    saw_pending = True
                    continue

                if response.status_code == 404:
                    continue

                raise Exception(f"AlphaFold2-Multimer status error: {response.status_code} - {response.text[:500]}")
        except requests.exceptions.Timeout:
            time.sleep(NVCF_POLL_INTERVAL)
            continue

        if saw_pending:
            time.sleep(NVCF_POLL_INTERVAL)
            continue

        raise Exception(f"AlphaFold2-Multimer async task {task_id} not found")

    raise Exception(f"AlphaFold2-Multimer async task timed out after {NVCF_POLL_SECONDS}s")


def extract_confidence_scores_multimer(pdb_content: str, num_chains: int) -> Dict[str, List[float]]:
    """
    Extract confidence scores (pLDDT) from AlphaFold2-Multimer PDB output.
    
    Args:
        pdb_content: PDB file content from AlphaFold2-Multimer
        num_chains: Expected number of protein chains
        
    Returns:
        Dictionary mapping chain IDs to pLDDT scores (0-100 scale)
    """
    chain_scores = {}
    
    try:
        current_chain = None
        for line in pdb_content.split('\n'):
            if line.startswith('ATOM') and len(line) > 60:
                # Chain ID is in column 22
                chain_id = line[21:22].strip()
                if not chain_id:
                    continue
                    
                # pLDDT scores are in columns 61-66 (B-factor field)
                try:
                    plddt = float(line[60:66].strip())
                    if 0 <= plddt <= 100:  # Valid pLDDT range
                        if chain_id not in chain_scores:
                            chain_scores[chain_id] = []
                        chain_scores[chain_id].append(plddt)
                except (ValueError, IndexError):
                    continue
                    
    except Exception as e:
        logger.warning(f"Error extracting pLDDT scores: {e}")
    
    return chain_scores


def process_alphafold2_multimer_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Process a single AlphaFold2-RE-Multimer structure prediction job.

    Args:
        job: Job dictionary containing sequences and parameters
        worker_id: Worker identifier

    Returns:
        Result dictionary with predicted structure or error
    """
    job_id = job.get('job_id', 'unknown')
    result = {
        'job_id': job_id,
        'status': 'error',
        'model_used': 'alphafold2-re-multimer',
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})
        
        # Handle different sequence input formats
        sequences_input = params.get('sequences')
        if not sequences_input:
            sequences_input = params.get('chains', [])
        if not sequences_input:
            # Fallback to single sequence
            sequence = params.get('sequence', params.get('protein_sequence', ''))
            if sequence:
                sequences_input = [sequence]
        
        if not sequences_input:
            result['message'] = "No protein sequences provided"
            return result

        # Clean and validate sequences
        sequences_clean = []
        for seq in _normalize_multimer_sequences(sequences_input):
            seq_clean = seq.upper().replace(' ', '').replace('\n', '').replace('\r', '')
            if seq_clean:
                sequences_clean.append(seq_clean)

        if not validate_protein_sequences(sequences_clean):
            result['message'] = "Invalid protein sequences: must contain only standard amino acids (ACDEFGHIKLMNPQRSTVWY)"
            return result

        if not (1 <= len(sequences_clean) <= MAX_MULTIMER_SEQUENCES):
            result['message'] = (
                f"AlphaFold2-Multimer supports 1-{MAX_MULTIMER_SEQUENCES} sequences "
                f"(got {len(sequences_clean)})"
            )
            return result

        total_length = sum(len(seq) for seq in sequences_clean)
        if any(len(seq) > MAX_SEQUENCE_LENGTH for seq in sequences_clean):
            result['message'] = f"Each sequence must be {MAX_SEQUENCE_LENGTH} amino acids or fewer"
            return result

        # Extract prediction parameters
        databases = params.get('databases', ["uniref90", "mgnify", "small_bfd"])
        algorithm = params.get('algorithm', 'jackhmmer')
        e_value = float(params.get('e_value', 0.0001))
        bit_score = params.get('bit_score', None)
        iterations = int(params.get('iterations', 1))
        relax_prediction = params.get('relax_prediction', True)
        structure_models_to_relax = params.get('structure_models_to_relax', 'all')
        num_predictions_per_model = int(params.get('num_predictions_per_model', 1))
        max_msa_sequences = params.get('max_msa_sequences', None)
        if max_msa_sequences is not None:
            max_msa_sequences = int(max_msa_sequences)
        template_searcher = params.get('template_searcher', 'hhsearch')

        logger.info(f"Job {job_id}: Predicting multimer structure for {len(sequences_clean)} proteins (total {total_length} aa)...")

        # Call AlphaFold2-Multimer API
        api_result = call_alphafold2_multimer_api(
            sequences=sequences_clean,
            databases=databases,
            algorithm=algorithm,
            e_value=e_value,
            bit_score=bit_score,
            iterations=iterations,
            relax_prediction=relax_prediction,
            structure_models_to_relax=structure_models_to_relax,
            num_predictions_per_model=num_predictions_per_model,
            max_msa_sequences=max_msa_sequences,
            template_searcher=template_searcher
        )

        execution_time = time.time() - start_time

        pdb_content, structure_format = _extract_structure_from_api_result(api_result, 'pdb')
        if not pdb_content:
            raise Exception("API response does not contain structure data")

        # Extract confidence scores per chain
        chain_plddt_scores = extract_confidence_scores_multimer(pdb_content, len(sequences_clean))
        
        # Calculate overall statistics
        all_scores = []
        for chain_scores in chain_plddt_scores.values():
            all_scores.extend(chain_scores)
        
        overall_avg = sum(all_scores) / len(all_scores) if all_scores else 0
        overall_min = min(all_scores) if all_scores else 0
        overall_max = max(all_scores) if all_scores else 0
        
        # Per-chain statistics
        chain_stats = {}
        for chain_id, scores in chain_plddt_scores.items():
            chain_stats[chain_id] = {
                'avg': sum(scores) / len(scores) if scores else 0,
                'min': min(scores) if scores else 0,
                'max': max(scores) if scores else 0,
                'count': len(scores)
            }

        # Prepare output metadata
        output_data = json.dumps({
            'sequences': sequences_clean,
            'num_sequences': len(sequences_clean),
            'sequence_lengths': [len(seq) for seq in sequences_clean],
            'total_length': total_length,
            'databases_used': databases,
            'algorithm': algorithm,
            'iterations': iterations,
            'relax_prediction': relax_prediction,
            'chain_plddt_scores': chain_plddt_scores,
            'chain_stats': chain_stats,
            'overall_plddt_avg': overall_avg,
            'overall_plddt_min': overall_min,
            'overall_plddt_max': overall_max,
            'execution_time': execution_time,
            'api_response': api_result
        }, indent=2)

        # Get output filename
        output_path = job.get('output_path', '')
        output_filename = os.path.basename(output_path) if output_path else f"{job_id}_multimer.pdb"

        # Encode structure content as base64
        structure_content_b64 = base64.b64encode(pdb_content.encode('utf-8')).decode('utf-8')
        
        # Also encode metadata as base64
        output_content_b64 = base64.b64encode(output_data.encode('utf-8')).decode('utf-8')

        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename.replace('.json', '') + '.json',
            'structure_content': structure_content_b64,
            'structure_filename': output_filename if not output_filename.endswith('.json') else output_filename.replace('.json', '.pdb'),
            'structure_format': structure_format,
            'num_sequences': len(sequences_clean),
            'sequence_lengths': [len(seq) for seq in sequences_clean],
            'total_length': total_length,
            'databases_used': databases,
            'algorithm': algorithm,
            'iterations': iterations,
            'relax_prediction': relax_prediction,
            'chain_plddt_scores': chain_plddt_scores,
            'chain_stats': chain_stats,
            'overall_plddt_avg': overall_avg,
            'overall_plddt_min': overall_min,
            'overall_plddt_max': overall_max,
            'confidence_score': overall_avg,  # For compatibility with existing UI
            'plddt_scores': all_scores,  # Flat list for compatibility
            'plddt_avg': overall_avg,
            'plddt_min': overall_min,
            'plddt_max': overall_max,
            'message': None
        })

        logger.info(f"Job {job_id}: Multimer prediction completed in {execution_time:.2f}s")
        logger.info(f"Job {job_id}: Overall pLDDT: {overall_avg:.2f} (chains: {list(chain_plddt_scores.keys())})")

    except requests.exceptions.Timeout:
        result['message'] = "AlphaFold2-Multimer API request timed out"
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
                    r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))

            except Exception as e:
                logger.warning(f"Could not send heartbeat (or Redis ping failed): {e}")

        logger.info("Heartbeat thread stopping.")
    except Exception as e:
        logger.critical(f"FATAL: Heartbeat thread encountered an unhandled exception: {e}", exc_info=True)


def worker_loop(
    worker_id: str,
    private_key: Optional[str],
    organization_id: Optional[int],
    stop_event: threading.Event
):
    """Main worker loop - fetches and processes AlphaFold2-Multimer jobs."""
    r = get_redis_connection(decode_responses=False)
    r_text = get_redis_connection(decode_responses=True)
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"AlphaFold2-Multimer worker started. Listening on queue: {job_queue}")

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
            logger.info(f"Received job: {job_id}")

            # Process the job
            result = process_alphafold2_multimer_job(job, worker_id)

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
    """Main entry point for AlphaFold2-Multimer worker."""
    global NODE_CONFIG_FILE
    global logger

    parser = argparse.ArgumentParser(description='AlphaFold2-Multimer Protein Structure Prediction Worker')
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
    logger.info(f"AlphaFold2-Multimer Protein Structure Prediction Worker Starting")
    logger.info(f"Worker ID: {worker_id}")
    logger.info(f"Organization ID: {organization_id or 'None (public)'}")
    logger.info(f"API URL: {NVIDIA_ALPHAFOLD2_MULTIMER_URL}")
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
        logger.info("AlphaFold2-Multimer worker shutdown complete.")


# Standalone test function
def test_alphafold2_multimer_api():
    """Test the AlphaFold2-Multimer API with sample protein sequences."""
    print("Testing AlphaFold2-Multimer API...")

    test_sequences = [
        "MNVIDIAAI",  # Chain A
        "ACDEFGHIK"  # Chain B
    ]

    try:
        result = call_alphafold2_multimer_api(
            sequences=test_sequences,
            databases=["uniref90"],
            iterations=1,
            relax_prediction=True
        )

        if 'pdb_content' in result:
            print(f"Success! Got PDB content ({len(result['pdb_content'])} chars)")
            
            # Test pLDDT extraction
            chain_scores = extract_confidence_scores_multimer(result['pdb_content'], len(test_sequences))
            print(f"Chains found: {list(chain_scores.keys())}")
            
            for chain_id, scores in chain_scores.items():
                print(f"Chain {chain_id}: {len(scores)} pLDDT scores")
                if scores:
                    print(f"  Min: {min(scores):.2f}, Max: {max(scores):.2f}, Avg: {sum(scores)/len(scores):.2f}")
            
            # Save PDB file
            pdb_file = Path("alphafold2_multimer_test_structure.pdb")
            pdb_file.write_text(result['pdb_content'])
            print(f"PDB structure saved to: {pdb_file}")
        else:
            print(f"Success! Got JSON response: {json.dumps(result, indent=2)[:500]}...")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False

    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        # Run API test
        test_alphafold2_multimer_api()
    else:
        # Run as worker
        main()
