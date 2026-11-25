#!/usr/bin/env python3
"""
DiffDock-RE Molecular Docking Worker Node
Connects to NVIDIA's DiffDock NIM API for protein-ligand docking prediction.
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
NVIDIA_DIFFDOCK_URL = os.getenv(
    "DIFFDOCK_API_URL",
    "https://health.api.nvidia.com/v1/biology/mit/diffdock"
)
NVIDIA_STATUS_URL = os.getenv(
    "DIFFDOCK_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "diffdock-re"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_diffdock_re.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings
NVCF_POLL_SECONDS = 300  # Max time to poll for results
NVCF_POLL_INTERVAL = 2   # Poll every 2 seconds

# Valid ligand file types
VALID_LIGAND_FILE_TYPES = {'sdf', 'mol2', 'smiles'}


def detect_ligand_file_type(ligand: str, declared: str) -> str:
    """Reconcile the declared ligand_file_type with what the content actually is.

    Users routinely paste a SMILES string into the submit form while the
    "Ligand Format" selector is left at its default ('sdf'). The cloud API
    then rejects the 1-line SMILES with "Fail to read ligand molecule
    description". Sniff the content for unambiguous structure-file markers and
    correct obvious mismatches; otherwise trust the declared type.

    Args:
        ligand: Raw ligand content as submitted.
        declared: The ligand_file_type the caller claims it is.

    Returns:
        The reconciled file type ('sdf', 'mol2', or 'smiles').
    """
    text = (ligand or '').strip()
    if not text:
        return declared

    upper = text.upper()

    # Mol2 has an unmistakable record-type header.
    if '@<TRIPOS>' in upper:
        return 'mol2'

    # SDF/MOL files carry a V2000/V3000 connection table or the molfile end
    # marker. Either is conclusive evidence of a structure file, not SMILES.
    if 'V2000' in upper or 'V3000' in upper or 'M  END' in upper:
        return 'sdf'

    # No structure-file markers found. A single non-empty line is a SMILES
    # string regardless of how it was labelled (the failure mode above).
    non_empty_lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(non_empty_lines) == 1 and declared != 'smiles':
        return 'smiles'

    return declared


def smiles_to_sdf(smiles: str) -> str:
    """Convert a SMILES string to a 3D V2000 SDF block.

    The NVIDIA cloud API no longer accepts SMILES directly, so SMILES ligands
    must be embedded into a 3D structure first. RDKit is preferred because it
    is the toolkit actually installed in this environment; openbabel is kept as
    a fallback for hosts that have it. Raises ValueError if conversion fails or
    no toolkit is available.
    """
    smiles = (smiles or '').strip()
    if not smiles:
        raise ValueError("Empty SMILES string")

    # --- Preferred: RDKit ---
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        Chem = None
    if Chem is not None:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            raise ValueError(f"RDKit could not parse SMILES: {smiles!r}")
        mol = Chem.AddHs(mol)
        params = AllChem.ETKDGv3()
        params.randomSeed = 42
        if AllChem.EmbedMolecule(mol, params) != 0:
            # Retry with random coordinates for awkward molecules
            params.useRandomCoords = True
            if AllChem.EmbedMolecule(mol, params) != 0:
                raise ValueError("RDKit failed to generate 3D coordinates")
        try:
            if AllChem.MMFFHasAllMoleculeParams(mol):
                AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
            else:
                AllChem.UFFOptimizeMolecule(mol, maxIters=500)
        except Exception as opt_err:
            logger.warning(f"RDKit geometry optimization skipped: {opt_err}")
        sdf_content = Chem.MolToMolBlock(mol)
        if 'V2000' not in sdf_content.upper() and 'V3000' not in sdf_content.upper():
            raise ValueError("RDKit produced an SDF without a V2000/V3000 block")
        # A valid SDF needs the $$$$ record terminator. Do NOT .strip() the block:
        # stripping removes the mandatory leading blank title line, collapsing the
        # 3-line MOL header to 2 lines so the counts row shifts up and DiffDock reads
        # it as coordinates → "Fail to read ligand molecule description".
        sdf_content = sdf_content.rstrip() + '\n$$$$\n'
        logger.info(f"Converted SMILES to SDF via RDKit ({len(sdf_content)} chars)")
        return sdf_content

    # --- Fallback: openbabel ---
    try:
        from openbabel import openbabel as ob
    except ImportError:
        raise ValueError(
            "No SMILES→SDF toolkit available (neither RDKit nor openbabel is installed)"
        )
    obConversion = ob.OBConversion()
    obConversion.SetInAndOutFormats("smi", "sdf")
    mol = ob.OBMol()
    if not obConversion.ReadString(mol, smiles):
        raise ValueError("Failed to parse SMILES string")
    if mol.NumAtoms() == 0:
        raise ValueError("No atoms found in molecule")
    builder = ob.OBBuilder()
    builder.Build(mol)
    try:
        ff = ob.OBForceField.FindForceField("mmff94")
        if ff:
            ff.Setup(mol)
            ff.ConjugateGradients(500)
            ff.GetCoordinates(mol)
    except Exception:
        ff_uff = ob.OBForceField.FindForceField("uff")
        if ff_uff:
            ff_uff.Setup(mol)
            ff_uff.ConjugateGradients(500)
            ff_uff.GetCoordinates(mol)
    sdf_content = obConversion.WriteString(mol).rstrip()
    if not sdf_content:
        raise ValueError("Failed to generate SDF from SMILES")
    if 'V2000' not in sdf_content.upper() and 'V3000' not in sdf_content.upper():
        raise ValueError("Generated SDF is missing V2000/V3000 block")
    if not sdf_content.endswith('$$$$'):
        sdf_content += '\n$$$$\n'
    logger.info(f"Converted SMILES to SDF via openbabel ({len(sdf_content)} chars)")
    return sdf_content

# Initial API request timeout/retry settings
try:
    DIFFDOCK_INITIAL_CONNECT_TIMEOUT = max(5, int(os.getenv("DIFFDOCK_INITIAL_CONNECT_TIMEOUT", "20")))
except (TypeError, ValueError):
    DIFFDOCK_INITIAL_CONNECT_TIMEOUT = 20

try:
    DIFFDOCK_INITIAL_READ_TIMEOUT = max(30, int(os.getenv("DIFFDOCK_INITIAL_READ_TIMEOUT", "180")))
except (TypeError, ValueError):
    DIFFDOCK_INITIAL_READ_TIMEOUT = 180

try:
    DIFFDOCK_INITIAL_REQUEST_RETRIES = max(1, int(os.getenv("DIFFDOCK_INITIAL_REQUEST_RETRIES", "2")))
except (TypeError, ValueError):
    DIFFDOCK_INITIAL_REQUEST_RETRIES = 2

try:
    DIFFDOCK_INITIAL_RETRY_BACKOFF_SECONDS = max(
        0.5, float(os.getenv("DIFFDOCK_INITIAL_RETRY_BACKOFF_SECONDS", "2.0"))
    )
except (TypeError, ValueError):
    DIFFDOCK_INITIAL_RETRY_BACKOFF_SECONDS = 2.0

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
    """Get Redis job queue key for DiffDock-RE jobs."""
    if org_id:
        return f"queue:job:{WORKER_TYPE}:org:{org_id}"
    return f"queue:job:{WORKER_TYPE}"


def get_redis_connection(decode_responses: bool = False):
    """Get Redis connection using config or environment.

    Args:
        decode_responses: If False, returns binary data (for encrypted payloads)
    """
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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (diffdock-re) - %(message)s',
        handlers=[
            logging.FileHandler(f'diffdock_re_worker_{worker_id}.log'),
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


def call_diffdock_re_api(
    protein: str,
    ligand: str,
    ligand_file_type: str,
    num_poses: int = 10,
    time_divisions: int = 20,
    steps: int = 18,
    save_trajectory: bool = False,
    is_staged: bool = False
) -> Dict[str, Any]:
    """
    Call NVIDIA DiffDock-RE molecular docking API with NVCF long-polling.

    Args:
        protein: Protein structure in PDB format
        ligand: Molecular structure in SDF, Mol2, or SMILES format
        ligand_file_type: Format of ligand input ("sdf", "mol2", or "smiles")
        num_poses: Number of docking poses to generate (1-20, default 10)
        time_divisions: Diffusion time steps (1-100, default 20)
        steps: Number of diffusion steps (1-100, default 18)
        save_trajectory: Save diffusion trajectory (default False)
        is_staged: Use staged diffusion (default False)

    Returns:
        Dictionary with docking results including docked_ligand, visualization_files,
        and pose_confidence
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    # Capture original SMILES before potential SDF conversion (for display on output page)
    _original_smiles = ligand if ligand_file_type == 'smiles' else None

    # NVIDIA cloud API no longer accepts SMILES directly — convert to SDF
    if ligand_file_type == 'smiles':
        try:
            ligand = smiles_to_sdf(ligand)
            ligand_file_type = 'sdf'
        except Exception as conv_err:
            logger.error(f"SMILES→SDF conversion failed: {conv_err}")
            raise ValueError(f"Failed to process SMILES ligand: {conv_err}")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "protein": protein,
        "ligand": ligand,
        "ligand_file_type": ligand_file_type,
        "num_poses": min(max(1, num_poses), 20),
        "time_divisions": min(max(1, time_divisions), 100),
        "steps": min(max(1, steps), 100),
        "save_trajectory": save_trajectory,
        "is_staged": is_staged
    }

    logger.info(
        f"Calling DiffDock-RE API: ligand_type={ligand_file_type}, "
        f"num_poses={num_poses}, steps={steps}, time_divisions={time_divisions}"
    )
    logger.debug(f"Protein length: {len(protein)}, Ligand length: {len(ligand)}")

    # Initial request with retries
    response = None
    for attempt in range(1, DIFFDOCK_INITIAL_REQUEST_RETRIES + 1):
        try:
            response = requests.post(
                url=NVIDIA_DIFFDOCK_URL,
                headers=headers,
                json=payload,
                timeout=(DIFFDOCK_INITIAL_CONNECT_TIMEOUT, DIFFDOCK_INITIAL_READ_TIMEOUT)
            )
            break
        except requests.exceptions.Timeout:
            logger.warning(
                "Initial DiffDock-RE request timed out "
                f"(attempt {attempt}/{DIFFDOCK_INITIAL_REQUEST_RETRIES}, "
                f"connect={DIFFDOCK_INITIAL_CONNECT_TIMEOUT}s, read={DIFFDOCK_INITIAL_READ_TIMEOUT}s)"
            )
            if attempt >= DIFFDOCK_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(DIFFDOCK_INITIAL_RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.RequestException as req_err:
            logger.warning(
                "Initial DiffDock-RE request failed "
                f"(attempt {attempt}/{DIFFDOCK_INITIAL_REQUEST_RETRIES}): {req_err}"
            )
            if attempt >= DIFFDOCK_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(DIFFDOCK_INITIAL_RETRY_BACKOFF_SECONDS * attempt)

    if response is None:
        raise requests.exceptions.Timeout("DiffDock-RE initial request did not return a response")

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

        logger.info(f"Async task started, polling for results (task_id: {nvcf_reqid})")
        return poll_nvcf_status(nvcf_reqid)

    else:
        raise Exception(f"DiffDock-RE API error: {response.status_code} - {response.text[:500]}")


def poll_nvcf_status(task_id: str) -> Dict[str, Any]:
    """
    Poll NVCF status endpoint until task completes.

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

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)

            if response.status_code == 200:
                # Task complete
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return response.json()
                else:
                    return {"raw_content": response.text[:5000]}

            elif response.status_code == 202:
                # Still processing
                logger.debug(f"Task {task_id} still processing...")
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            elif response.status_code == 404:
                raise Exception(f"Task {task_id} not found")

            else:
                raise Exception(f"Status check error: {response.status_code} - {response.text[:500]}")

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout polling status for {task_id}, retrying...")
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Task {task_id} timed out after {NVCF_POLL_SECONDS} seconds")


def process_diffdock_re_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Process a single DiffDock-RE molecular docking job.

    Args:
        job: Job dictionary containing protein, ligand, and parameters
        worker_id: Worker identifier

    Returns:
        Result dictionary with docking poses or error
    """
    job_id = job.get('job_id', 'unknown')
    result = {
        'job_id': job_id,
        'status': 'error',
        'model_used': 'diffdock-re',
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})

        # Get protein PDB. The molecular_docking backend handler emits the
        # structure under 'pdb_content' (or 'protein_sequence'); direct queue
        # submissions use 'protein'. Accept all so both paths work.
        protein = (
            params.get('protein', '')
            or params.get('pdb_content', '')
            or params.get('protein_sequence', '')
        )
        if not protein:
            result['message'] = "No protein structure provided (PDB format required)"
            return result

        # Unwrap JSON-encoded PDB (e.g. ESMFold/AlphaFold2 output format {"pdbs":[...]} or {"pdb_content":"..."})
        if protein.startswith('{') or protein.startswith('['):
            try:
                wrapped = json.loads(protein)
                if isinstance(wrapped, dict):
                    if 'pdbs' in wrapped and isinstance(wrapped['pdbs'], list) and wrapped['pdbs']:
                        protein = wrapped['pdbs'][0]
                    elif 'pdb_content' in wrapped:
                        protein = wrapped['pdb_content']
                    elif 'structure' in wrapped:
                        protein = wrapped['structure']
            except (json.JSONDecodeError, KeyError):
                pass

        # Basic PDB/CIF validation
        protein_upper = protein.upper()
        has_atom = 'ATOM' in protein_upper or 'HETATM' in protein_upper
        has_cif = protein_upper.startswith('DATA_') or '_atom_site.' in protein_upper
        if not has_atom and not has_cif:
            result['message'] = "Invalid protein structure: must be in PDB or mmCIF format containing ATOM records"
            return result

        # Get ligand
        ligand = params.get('ligand', '')
        if not ligand:
            result['message'] = "No ligand provided"
            return result

        ligand_file_type = params.get('ligand_file_type', '').lower()
        if ligand_file_type not in VALID_LIGAND_FILE_TYPES:
            result['message'] = f"Invalid ligand_file_type. Must be one of: {', '.join(sorted(VALID_LIGAND_FILE_TYPES))}"
            return result

        # Correct mismatches between the declared format and the actual content
        # (e.g. a SMILES pasted while the format selector was left at 'sdf'),
        # which otherwise fails the API with "Fail to read ligand molecule
        # description".
        detected_file_type = detect_ligand_file_type(ligand, ligand_file_type)
        if detected_file_type != ligand_file_type:
            logger.warning(
                f"Job {job_id}: ligand_file_type declared '{ligand_file_type}' but "
                f"content looks like '{detected_file_type}'; using detected type"
            )
            ligand_file_type = detected_file_type

        # Extract prediction parameters
        num_poses = min(max(1, int(params.get('num_poses', 10))), 20)
        time_divisions = min(max(1, int(params.get('time_divisions', 20))), 100)
        steps = min(max(1, int(params.get('steps', 18))), 100)
        save_trajectory = bool(params.get('save_trajectory', False))
        is_staged = bool(params.get('is_staged', False))

        logger.info(
            f"Job {job_id}: Docking prediction with {num_poses} poses, "
            f"ligand_type={ligand_file_type}, steps={steps}"
        )

        # Call DiffDock-RE API
        api_result = call_diffdock_re_api(
            protein=protein,
            ligand=ligand,
            ligand_file_type=ligand_file_type,
            num_poses=num_poses,
            time_divisions=time_divisions,
            steps=steps,
            save_trajectory=save_trajectory,
            is_staged=is_staged
        )

        execution_time = time.time() - start_time

        # Ensure valid JSON by cleaning NaN/Infinity values
        api_result = clean_nan_values(api_result)

        # Treat explicit API-level failures as job failures (even if the payload
        # includes placeholder arrays like [null, null, ...]).
        api_status = str(api_result.get('status') or '').strip().lower()
        if api_status in {'failed', 'error'}:
            detail_message = (
                api_result.get('details')
                or api_result.get('message')
                or api_result.get('error')
                or "DiffDock-RE returned failure status"
            )
            result['status'] = 'error'
            result['message'] = f"DiffDock-RE failed: {detail_message}"
            logger.error(f"Job {job_id}: API returned failure status '{api_status}' ({detail_message})")
            return result

        def _is_valid_ligand_pose(pose_block: Any) -> bool:
            """Return True only for non-empty SDF/MOL pose blocks with coordinates."""
            if not isinstance(pose_block, str):
                return False
            block = pose_block.strip()
            if not block or block == '$$$$':
                return False
            upper_block = block.upper()
            # Typical molfile signatures
            if 'V2000' in upper_block or 'V3000' in upper_block or 'M  END' in upper_block:
                return True
            # Fallback for edge cases that still carry explicit coordinates
            if 'ATOM' in upper_block or 'HETATM' in upper_block:
                return True
            return False

        # Extract key output fields
        # Self-hosted NIM: ligand_positions, position_confidence, protein
        # Hosted API (build.nvidia.com): docked_ligand, pose_confidence, visualization_files
        ligand_positions = api_result.get('ligand_positions', [])
        pose_confidence = api_result.get('position_confidence', [])
        protein_pdb = api_result.get('protein', '')

        # Fallback to hosted API field names
        if not ligand_positions:
            docked_ligand = api_result.get('docked_ligand', '')
            if docked_ligand:
                # Split multi-pose SDF into individual poses
                ligand_positions = [block.strip() for block in docked_ligand.split('$$$$') if block.strip()]
        if not pose_confidence:
            pose_confidence = api_result.get('pose_confidence', [])
        if not protein_pdb:
            protein_pdb = api_result.get('visualization_files', '')

        # Drop empty/invalid pose blocks before deciding success.
        ligand_positions = [
            pose.strip() for pose in ligand_positions
            if _is_valid_ligand_pose(pose)
        ]

        if not ligand_positions:
            result['status'] = 'error'
            result['message'] = (
                "DiffDock-RE API completed but returned no valid ligand poses. "
                "This is usually caused by an unreadable/incompatible ligand input."
            )
            logger.error(
                f"Job {job_id}: No valid ligand poses returned by API "
                f"(raw pose entries: {len(api_result.get('ligand_positions', []) or [])})"
            )
            return result

        # Prepare artifacts
        artifacts = []

        # Save the input protein PDB as an artifact if the API didn't return one
        if not protein_pdb:
            protein_pdb = protein  # Use the original input protein

        # Save the input ligand SMILES for display on the output page
        # Use the ligand parameter (which is SMILES if ligand_file_type == 'smiles')
        input_ligand_smiles = ligand if ligand_file_type == 'smiles' else None

        # Each entry in ligand_positions is an SDF string for one pose
        if ligand_positions:
            # Combined multi-pose SDF
            combined_sdf = '\n$$$$\n'.join(pos for pos in ligand_positions if pos) + '\n$$$$\n'
            docked_b64 = base64.b64encode(combined_sdf.encode('utf-8')).decode('utf-8')
            artifacts.append({
                "filename": f"{job_id}_docked_ligand.sdf",
                "content_b64": docked_b64,
                "name": "docked_ligand",
                "display_name": f"Docked Ligand ({len(ligand_positions)} poses)",
                "type": "ligand"
            })

            for i, pose_sdf in enumerate(ligand_positions):
                if not pose_sdf:
                    continue
                pose_b64 = base64.b64encode((pose_sdf + '\n$$$$\n').encode('utf-8')).decode('utf-8')
                conf = pose_confidence[i] if i < len(pose_confidence) else None
                confidence_str = f" (conf: {conf:.3f})" if conf is not None else ""
                artifacts.append({
                    "filename": f"{job_id}_pose_{i}.sdf",
                    "content_b64": pose_b64,
                    "name": "docked_pose",
                    "display_name": f"Pose {i + 1}{confidence_str}",
                    "pose_index": i,
                    "confidence": conf,
                    "type": "ligand"
                })

        # Protein PDB from the API output
        if protein_pdb:
            vis_b64 = base64.b64encode(protein_pdb.encode('utf-8')).decode('utf-8')
            artifacts.append({
                "filename": f"{job_id}_protein.pdb",
                "content_b64": vis_b64,
                "name": "visualization_protein",
                "display_name": "Protein (PDB)",
                "type": "protein"
            })

        # Prepare output in JSON format for metadata
        output_data = json.dumps(api_result, indent=2)
        output_content_b64 = base64.b64encode(output_data.encode('utf-8')).decode('utf-8')

        # Get output filename from job output_path
        output_path = job.get('output_path', '')
        if output_path:
            output_filename = os.path.basename(output_path)
        else:
            output_filename = f"{job_id}_docking_result.json"

        # Best pose metrics
        valid_confidences = [c for c in pose_confidence if c is not None]
        best_confidence = max(valid_confidences) if valid_confidences else None
        best_pose_index = pose_confidence.index(best_confidence) if best_confidence is not None else 0

        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename,
            'artifacts': artifacts,
            # Docking-specific fields
            'docking_style': True,
            'num_poses': len(ligand_positions),
            'num_poses_requested': num_poses,
            'pose_confidence': pose_confidence,
            'best_confidence': best_confidence,
            'best_pose_index': best_pose_index,
            'ligand_file_type': ligand_file_type,
            'has_ligand': bool(ligand_positions),
            'has_trajectory': save_trajectory,
            'input_ligand_smiles': input_ligand_smiles,
            'message': None
        })

        logger.info(f"Job {job_id}: Docking prediction completed in {execution_time:.2f}s")
        logger.info(f"Job {job_id}: Generated {len(ligand_positions)} poses")
        if valid_confidences:
            logger.info(f"Job {job_id}: Confidence scores: {[f'{s:.3f}' for s in valid_confidences]}")
            logger.info(f"Job {job_id}: Best pose: {best_pose_index + 1} (confidence: {best_confidence:.3f})")

    except requests.exceptions.Timeout:
        result['message'] = "DiffDock-RE API request timed out"
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
                # Direct Redis ping test
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
                    # Unsigned heartbeat for testing
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
    """Main worker loop - fetches and processes DiffDock-RE jobs."""
    r = get_redis_connection(decode_responses=False)  # Binary mode for encrypted data
    r_text = get_redis_connection(decode_responses=True)  # Text mode for results
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"DiffDock-RE worker started. Listening on queue: {job_queue}")

    while not stop_event.is_set():
        try:
            # Block pop with 1 second timeout
            job_payload = r.brpop(job_queue, timeout=1)

            if not job_payload:
                continue

            _, encrypted_data = job_payload

            # Decrypt the job data if encryption is available
            if decrypt and isinstance(encrypted_data, bytes):
                try:
                    decrypted = decrypt(encrypted_data)
                    if decrypted:
                        job_data = decrypted.decode('utf-8')
                    else:
                        # Not encrypted, try as plain text
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
            result = process_diffdock_re_job(job, worker_id)

            # Send result back
            if send_job_result:
                send_job_result(result, private_key, r)
            else:
                # Fallback for testing - push to results queue
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
    """Main entry point for DiffDock-RE worker."""
    global NODE_CONFIG_FILE
    global logger

    parser = argparse.ArgumentParser(description='DiffDock-RE Molecular Docking Worker')
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
    logger.info(f"DiffDock-RE Molecular Docking Worker Starting")
    logger.info(f"Worker ID: {worker_id}")
    logger.info(f"Organization ID: {organization_id or 'None (public)'}")
    logger.info(f"API URL: {NVIDIA_DIFFDOCK_URL}")
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
        logger.info("DiffDock-RE worker shutdown complete.")


if __name__ == '__main__':
    main()
