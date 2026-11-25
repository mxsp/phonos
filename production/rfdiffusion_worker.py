#!/usr/bin/env python3
"""
RFDiffusion Worker - Type 12
Protein structure generation using NVIDIA RFDiffusion API.

This worker processes protein design jobs using the RFDiffusion model,
which generates novel protein structures via diffusion-based generation.
"""
import os
import sys
import json
import time
import base64
import hashlib
import logging
import traceback
import re
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import redis
import requests
from dotenv import load_dotenv

# Add production directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from encryption_util import decrypt, encrypt, canonical_dumps, sign_message
    from node_utils import send_job_result
    HAS_CRYPTO = True
except ImportError as e:
    print(f"Warning: Could not import crypto utilities: {e}")
    decrypt = None
    encrypt = None
    canonical_dumps = None
    sign_message = None
    send_job_result = None
    HAS_CRYPTO = False

# Node configuration (shared with other workers)
NODE_CONFIG_FILE = os.environ.get('NODE_CONFIG_FILE', 'node_config.json')
PRIVATE_KEY_FILE = 'node_private_key.pem'

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(name)s - %(message)s'
)
logger = logging.getLogger('rfdiffusion_worker')

# Worker configuration
WORKER_TYPE = "type-12"
WORKER_ID = f"rfdiffusion-worker-{os.getpid()}-{int(time.time())}"
HEARTBEAT_INTERVAL = 30

# NVIDIA API configuration
NVIDIA_API_BASE = "https://health.api.nvidia.com/v1/biology"
RFDIFFUSION_ENDPOINT = os.environ.get("RFDIFFUSION_API_URL", f"{NVIDIA_API_BASE}/ipd/rfdiffusion/generate")
NVIDIA_STATUS_ENDPOINT = os.environ.get("RFDIFFUSION_STATUS_URL", "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status")

# Redis configuration
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)

# API Key
NVIDIA_API_KEY = os.environ.get('NVIDIA_API_KEY', '')

# Job queues
PUBLIC_QUEUE = f"queue:job:{WORKER_TYPE}"
ORG_QUEUE_PREFIX = f"queue:job:{WORKER_TYPE}:org:"
RESULTS_QUEUE = "queue:job_results"
HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"


def get_redis_client():
    """Create Redis client connection."""
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=False  # Must be False to handle encrypted binary payloads
    )


def send_heartbeat(r: redis.Redis):
    """Send worker heartbeat."""
    try:
        heartbeat_data = {
            'worker_id': WORKER_ID,
            'worker_type': WORKER_TYPE,
            'timestamp': time.time(),
            'status': 'active',
            'model': 'rfdiffusion'
        }
        # Publish requires string/bytes, so we encode json
        r.publish(HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))
    except Exception as e:
        logger.warning(f"Failed to send heartbeat: {e}")


def validate_pdb_content(pdb_content: str) -> Tuple[bool, str]:
    """Validate PDB content format."""
    if not pdb_content or not pdb_content.strip():
        return False, "Empty PDB content"

    lines = pdb_content.strip().split('\n')
    atom_count = 0

    for line in lines:
        record_type = line[:6].strip().upper()
        if record_type in {'ATOM', 'HETATM'}:
            atom_count += 1
            # Basic validation of ATOM line format
            if len(line) < 54:
                return False, f"Invalid ATOM line format: too short"

    if atom_count == 0:
        return False, "No ATOM records found in PDB"

    if atom_count > 10000:
        return False, f"PDB too large: {atom_count} atoms (max 10000)"

    return True, f"Valid PDB with {atom_count} atoms"


def validate_contigs(contigs: str) -> Tuple[bool, str]:
    """
    Validate contig specification format.
    Format examples:
      - "A20-60/0 50-100"
      - "10-40/A163-181/10-40"
      - "100"
    """
    if not contigs or not contigs.strip():
        return False, "Empty contigs specification"

    # Basic pattern validation - contigs are ranges or single numbers separated by / or space
    # Format: [ChainID]Start-End or just Length
    pattern = r'^[A-Z]?\d+(-\d+)?([/\s][A-Z]?\d+(-\d+)?)*$'

    # Allow more flexible patterns
    contig_parts = re.split(r'[/\s]+', contigs.strip())

    for part in contig_parts:
        if not part:
            continue
        # Each part should be like: A20-60, 50-100, A163, 100, etc.
        part_pattern = r'^[A-Z]?\d+(-\d+)?$'
        if not re.match(part_pattern, part):
            return False, f"Invalid contig part: {part}"

    return True, "Valid contigs specification"


def validate_hotspot_residues(hotspots: List[str]) -> Tuple[bool, str]:
    """
    Validate hotspot residue specifications.
    Format: ChainID + ResidueNumber, e.g., ["A50", "A51", "B100"]
    """
    if not hotspots:
        return True, "No hotspots (optional)"

    for hs in hotspots:
        if not re.match(r'^[A-Z]\d+$', hs):
            return False, f"Invalid hotspot format: {hs} (expected format: A50)"

    return True, f"Valid hotspots: {len(hotspots)} residues"


def extract_structures_from_response(response_data: Dict) -> List[Dict]:
    """
    Extract individual structures from RFDiffusion response.
    Returns list of structure dicts with PDB content and metadata.
    """
    structures = []

    # Main output_pdb field
    if 'output_pdb' in response_data:
        output_pdb = response_data['output_pdb']

        # Check if multiple models are present (MODEL/ENDMDL blocks)
        if 'MODEL' in output_pdb:
            models = re.split(r'\nENDMDL\s*\n?', output_pdb)
            for i, model_content in enumerate(models):
                if not model_content.strip():
                    continue
                # Clean up model content
                model_lines = []
                for line in model_content.split('\n'):
                    if line.startswith('MODEL'):
                        continue  # Skip MODEL header
                    model_lines.append(line)

                clean_content = '\n'.join(model_lines).strip()
                if clean_content:
                    structures.append({
                        'pdb_content': clean_content + '\nEND\n',
                        'model_index': i,
                        'name': f'model_{i}'
                    })
        else:
            # Single structure
            structures.append({
                'pdb_content': output_pdb,
                'model_index': 0,
                'name': 'model_0'
            })

    # Check for additional output fields
    if 'generated_structures' in response_data:
        for i, struct in enumerate(response_data['generated_structures']):
            if isinstance(struct, dict) and 'pdb' in struct:
                structures.append({
                    'pdb_content': struct['pdb'],
                    'model_index': len(structures),
                    'name': f'generated_{i}'
                })
            elif isinstance(struct, str):
                structures.append({
                    'pdb_content': struct,
                    'model_index': len(structures),
                    'name': f'generated_{i}'
                })

    return structures


def call_rfdiffusion_api(
    input_pdb: str,
    contigs: str,
    hotspot_res: Optional[List[str]] = None,
    diffusion_steps: int = 50,
    num_designs: int = 1,
    noise_scale: float = 1.0,
    partial_T: Optional[int] = None
) -> Dict[str, Any]:
    """
    Call NVIDIA RFDiffusion API.

    Args:
        input_pdb: PDB content string
        contigs: Contig specification string
        hotspot_res: List of hotspot residues (optional)
        diffusion_steps: Number of diffusion steps (1-200)
        num_designs: Number of designs to generate (1-8)
        noise_scale: Noise scale parameter (0.1-2.0)
        partial_T: Partial diffusion timesteps (optional, for partial diffusion)

    Returns:
        API response dict with output structures
    """
    if not NVIDIA_API_KEY:
        raise ValueError("NVIDIA_API_KEY not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "input_pdb": input_pdb,
        "contigs": contigs,
        "diffusion_steps": diffusion_steps,
    }

    if hotspot_res:
        payload["hotspot_res"] = hotspot_res

    # num_designs is not supported by the API endpoint anymore
    # if num_designs > 1:
    #     payload["num_designs"] = num_designs

    # noise_scale is no longer accepted by the NVIDIA RFDiffusion API

    if partial_T is not None:
        payload["partial_T"] = partial_T

    logger.info(f"Calling RFDiffusion API with {diffusion_steps} steps, contigs: {contigs}")

    try:
        response = requests.post(
            RFDIFFUSION_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=600  # 10 minute timeout for diffusion
        )

        # Check for async task (202 Accepted)
        if response.status_code == 202:
            # Poll for completion
            task_id = response.headers.get('NVCF-REQID')
            if task_id:
                return poll_for_completion(task_id, headers)
            else:
                raise ValueError("Received 202 but no task ID in response")

        if response.status_code != 200:
            error_text = response.text[:500] if response.text else "No error message"
            logger.error(f"API error {response.status_code}: {error_text}")
            raise ValueError(f"API returned status {response.status_code}: {error_text}")

        return response.json()

    except requests.exceptions.Timeout:
        raise ValueError("API request timed out (600s)")
    except requests.exceptions.RequestException as e:
        raise ValueError(f"API request failed: {str(e)}")


def poll_for_completion(task_id: str, headers: Dict, timeout: int = 600) -> Dict:
    """Poll NVIDIA API for async task completion."""
    status_url = f"{NVIDIA_STATUS_ENDPOINT}/{task_id}"
    start_time = time.time()
    poll_interval = 2  # Start with 2 seconds

    while time.time() - start_time < timeout:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 202:
                # Still processing
                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 10)  # Back off up to 10s
                continue
            else:
                raise ValueError(f"Status check failed: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logger.warning(f"Status poll error: {e}")
            time.sleep(poll_interval)

    raise ValueError(f"Task timed out after {timeout}s")


def process_rfdiffusion_job(job_data: Dict) -> Dict[str, Any]:
    """
    Process a single RFDiffusion job.

    Returns result dict with status and artifacts.
    """
    job_id = job_data.get('job_id', 'unknown')
    params = job_data.get('params', {})

    logger.info(f"Processing RFDiffusion job {job_id}")
    start_time = time.time()

    try:
        # Extract parameters with backward-compatible fallbacks.
        # Some legacy payloads may carry PDB text in sequence/pdb_content fields,
        # or have input_pdb set to a file id string.
        candidate_pdb_fields = [
            params.get('input_pdb', ''),
            params.get('pdb_content', ''),
            params.get('pdb', ''),
            params.get('protein_pdb', ''),
            params.get('sequence', ''),
        ]
        input_pdb = ''
        for candidate in candidate_pdb_fields:
            if not isinstance(candidate, str):
                continue
            text = candidate.strip()
            if not text:
                continue
            if any(token in text[:500].upper() for token in ('ATOM', 'HETATM', 'HEADER', 'REMARK', 'MODEL')):
                input_pdb = text
                break

        if not input_pdb and isinstance(params.get('input_pdb'), str):
            input_pdb = params.get('input_pdb', '').strip()

        contigs = params.get('contigs', '')
        hotspot_res = params.get('hotspot_res', [])
        diffusion_steps = params.get('diffusion_steps', 50)
        num_designs = params.get('num_designs', 1)
        noise_scale = params.get('noise_scale', 1.0)
        partial_T = params.get('partial_T')

        # Validate inputs
        valid, msg = validate_pdb_content(input_pdb)
        if not valid:
            return {
                'status': 'error',
                'error': f"Invalid PDB: {msg}",
                'job_id': job_id
            }

        valid, msg = validate_contigs(contigs)
        if not valid:
            return {
                'status': 'error',
                'error': f"Invalid contigs: {msg}",
                'job_id': job_id
            }

        if hotspot_res:
            valid, msg = validate_hotspot_residues(hotspot_res)
            if not valid:
                return {
                    'status': 'error',
                    'error': f"Invalid hotspots: {msg}",
                    'job_id': job_id
                }

        # Clamp parameters to valid ranges
        diffusion_steps = max(1, min(200, diffusion_steps))
        num_designs = max(1, min(8, num_designs))
        noise_scale = max(0.1, min(2.0, noise_scale))

        # Call API
        api_response = call_rfdiffusion_api(
            input_pdb=input_pdb,
            contigs=contigs,
            hotspot_res=hotspot_res,
            diffusion_steps=diffusion_steps,
            num_designs=num_designs,
            noise_scale=noise_scale,
            partial_T=partial_T
        )

        # Extract structures
        structures = extract_structures_from_response(api_response)

        if not structures:
            return {
                'status': 'error',
                'error': 'No structures generated',
                'job_id': job_id
            }

        execution_time = time.time() - start_time

        # Build artifacts
        artifacts = []

        # Add input structure as reference
        input_b64 = base64.b64encode(input_pdb.encode('utf-8')).decode('utf-8')
        artifacts.append({
            'filename': f'{job_id}_input.pdb',
            'content_b64': input_b64,
            'name': 'input_structure',
            'display_name': 'Input Structure',
            'type': 'input',
            'model_index': -1,
            'color': '#808080'  # Gray for input
        })

        # Add generated structures
        structure_colors = ['#00ff00', '#ff6600', '#0099ff', '#ff00ff', '#ffff00', '#00ffff', '#ff0066', '#66ff00']

        for i, struct in enumerate(structures):
            pdb_b64 = base64.b64encode(struct['pdb_content'].encode('utf-8')).decode('utf-8')
            color = structure_colors[i % len(structure_colors)]

            artifacts.append({
                'filename': f'{job_id}_design_{i}.pdb',
                'content_b64': pdb_b64,
                'name': f'design_{i}',
                'display_name': f'Design {i + 1}',
                'type': 'output',
                'model_index': i,
                'color': color
            })

        # Create combined structure (all designs superimposed)
        if len(structures) > 1:
            combined_pdb = "REMARK Combined RFDiffusion designs\n"
            for i, struct in enumerate(structures):
                combined_pdb += f"MODEL     {i + 1}\n"
                combined_pdb += struct['pdb_content'].replace('END\n', '')
                combined_pdb += "ENDMDL\n"
            combined_pdb += "END\n"

            combined_b64 = base64.b64encode(combined_pdb.encode('utf-8')).decode('utf-8')
            artifacts.append({
                'filename': f'{job_id}_combined.pdb',
                'content_b64': combined_b64,
                'name': 'combined',
                'display_name': 'All Designs Combined',
                'type': 'combined',
                'model_index': -2
            })

        result = {
            'status': 'success',
            'job_id': job_id,
            'execution_time': execution_time,
            'artifacts': artifacts,
            'num_designs': len(structures),
            'parameters': {
                'contigs': contigs,
                'diffusion_steps': diffusion_steps,
                'hotspot_res': hotspot_res,
                'noise_scale': noise_scale
            },
            'output_filename': f'{job_id}_design_0.pdb',
            'output_file_content': artifacts[1]['content_b64'] if len(artifacts) > 1 else None  # First design
        }

        logger.info(f"Job {job_id} completed: {len(structures)} designs in {execution_time:.2f}s")
        return result

    except Exception as e:
        logger.error(f"Job {job_id} failed: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'status': 'error',
            'error': str(e),
            'job_id': job_id,
            'execution_time': time.time() - start_time
        }


def send_result_to_queue(result: Dict, private_key: Optional[str], r: redis.Redis, worker_id: str):
    """Send job result to results queue with proper signing and encryption."""
    job_id = result.get('job_id', 'unknown')

    # Build the result payload in the format MRS expects
    # Use the registered worker_id (from node config) not the dynamic WORKER_ID
    result_payload = {
        'job_id': job_id,
        'worker_type': WORKER_TYPE,
        'worker_id': worker_id,
        'status': result.get('status', 'error'),
        'timestamp': time.time(),
    }

    # Copy relevant fields from result
    if result.get('status') == 'success':
        result_payload['output_file_content'] = result.get('output_file_content')
        result_payload['output_filename'] = result.get('output_filename')
        result_payload['artifacts'] = result.get('artifacts', [])
        result_payload['execution_time'] = result.get('execution_time')
        result_payload['parameters'] = result.get('parameters', {})
    else:
        result_payload['message'] = result.get('error', 'Unknown error')

    try:
        # Try to use proper send_job_result from node_utils if available
        if HAS_CRYPTO and send_job_result and private_key:
            send_job_result(result_payload, private_key, r)
            logger.info(f"Result sent (signed) for job {job_id}")
        elif HAS_CRYPTO and encrypt:
            # Fallback: encrypt but don't sign (MRS may reject this)
            import secrets
            result_payload['nonce'] = secrets.token_urlsafe(32)
            result_json = json.dumps(result_payload)
            encrypted_result = encrypt(result_json.encode('utf-8'))
            r.lpush(RESULTS_QUEUE, encrypted_result)
            logger.info(f"Result sent (encrypted, unsigned) for job {job_id}")
        else:
            # Plain text fallback (MRS will likely reject this)
            r.lpush(RESULTS_QUEUE, json.dumps(result_payload))
            logger.warning(f"Result sent (plain text) for job {job_id} - may be rejected by MRS")
    except Exception as e:
        logger.error(f"Failed to send result for job {job_id}: {e}")


def load_node_config() -> Dict:
    """Load node configuration from file."""
    config_path = os.path.join(os.path.dirname(__file__), NODE_CONFIG_FILE)
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load node config: {e}")
    return {}


def load_private_key() -> Optional[str]:
    """Load private key from file or auto-provisioned local worker keys."""
    env_key = os.environ.get("NODE_PRIVATE_KEY", "").strip()
    if env_key:
        return env_key
    key_path = os.path.join(os.path.dirname(__file__), PRIVATE_KEY_FILE)
    if os.path.exists(key_path):
        try:
            with open(key_path, 'r') as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Could not load private key: {e}")
    try:
        from local_worker_keys import load_local_worker_private_key
        return load_local_worker_private_key()
    except Exception:
        return None


def send_signed_heartbeat(worker_id: str, private_key: Optional[str], r: redis.Redis):
    """Send signed heartbeat to Redis."""
    try:
        heartbeat_data = {
            'worker_id': worker_id,
            'worker_type': WORKER_TYPE,
            'timestamp': time.time(),
            'status': 'active',
            'model': 'rfdiffusion'
        }

        if private_key and HAS_CRYPTO and canonical_dumps and sign_message:
            payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
            signature = sign_message(private_key, payload_bytes)
            if signature:
                signed_message = {'payload': heartbeat_data, 'signature': signature}
                r.publish(HEARTBEAT_CHANNEL, json.dumps(signed_message))
                logger.debug(f"Published signed heartbeat for worker {worker_id}")
                return

        # Unsigned heartbeat fallback
        r.publish(HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))
    except Exception as e:
        logger.warning(f"Failed to send heartbeat: {e}")


def worker_loop(worker_id: str, private_key: Optional[str], organization_id: Optional[str], stop_event: threading.Event):
    """Main worker loop."""
    logger.info(f"Starting RFDiffusion worker {worker_id}")
    logger.info(f"Worker type: {WORKER_TYPE}")
    logger.info(f"Public queue: {PUBLIC_QUEUE}")
    logger.info(f"Crypto available: {HAS_CRYPTO}")
    logger.info(f"Private key available: {bool(private_key)}")

    if not NVIDIA_API_KEY:
        logger.error("NVIDIA_API_KEY not set - worker cannot process jobs")
        return

    r = get_redis_client()
    last_heartbeat = 0

    # List of queues to monitor (public + any org-specific)
    queues = [PUBLIC_QUEUE]
    if organization_id:
        queues.append(f"{ORG_QUEUE_PREFIX}{organization_id}")

    logger.info("Worker ready, waiting for jobs...")

    while not stop_event.is_set():
        try:
            # Send heartbeat periodically
            current_time = time.time()
            if current_time - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_signed_heartbeat(worker_id, private_key, r)
                last_heartbeat = current_time

            # Check for jobs (blocking pop with 1s timeout)
            result = r.brpop(queues, timeout=1)

            if result is None:
                continue

            queue_name_bytes, job_data_bytes = result
            queue_name = queue_name_bytes.decode('utf-8')

            # Try to decrypt
            job_json = None
            if decrypt:
                try:
                    decrypted = decrypt(job_data_bytes)
                    if decrypted:
                        job_json = decrypted
                except Exception:
                    pass

            # Fallback to plain text if decryption failed or wasn't needed
            if not job_json:
                try:
                    job_json = job_data_bytes.decode('utf-8')
                except UnicodeDecodeError:
                    logger.error(f"Failed to decode job data from {queue_name}")
                    continue

            try:
                job_data = json.loads(job_json)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid job JSON: {e}")
                continue

            job_id = job_data.get('job_id', 'unknown')
            logger.info(f"Received job {job_id} from {queue_name}")

            # Process job
            job_result = process_rfdiffusion_job(job_data)

            # Send result with the registered worker_id
            send_result_to_queue(job_result, private_key, r, worker_id)

        except redis.exceptions.ConnectionError as e:
            logger.error(f"Redis connection error: {e}")
            time.sleep(5)
            r = get_redis_client()
        except KeyboardInterrupt:
            logger.info("Worker shutting down...")
            break
        except Exception as e:
            logger.error(f"Worker error: {e}")
            logger.error(traceback.format_exc())
            time.sleep(1)


def main():
    """Main entry point."""
    # Load node configuration
    config = load_node_config()
    worker_id = config.get('worker_id', WORKER_ID)
    organization_id = config.get('organization_id')

    # Load private key
    private_key = config.get('private_key') or load_private_key()

    if not private_key:
        logger.warning("No private key found - results will not be signed and may be rejected by MRS")
        logger.warning("To register this worker, use the node registration system")

    stop_event = threading.Event()

    try:
        worker_loop(worker_id, private_key, organization_id, stop_event)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
        stop_event.set()


if __name__ == '__main__':
    main()
