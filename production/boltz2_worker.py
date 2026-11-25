#!/usr/bin/env python3
"""
Boltz2 Protein Structure Prediction Worker Node
Connects to NVIDIA's Boltz2 model API for protein structure prediction with ligand binding.
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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
WORKER_TYPE = "type-9"  # Must match model ID in config.py
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_boltz2.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings
try:
    NVCF_POLL_SECONDS = max(60, int(os.getenv("BOLTZ2_POLL_SECONDS", "900")))
except (TypeError, ValueError):
    NVCF_POLL_SECONDS = 900

try:
    NVCF_POLL_INTERVAL = max(1, int(os.getenv("BOLTZ2_POLL_INTERVAL", "2")))
except (TypeError, ValueError):
    NVCF_POLL_INTERVAL = 2

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

try:
    BOLTZ2_RATE_LIMIT_RETRIES = max(0, int(os.getenv("BOLTZ2_RATE_LIMIT_RETRIES", "4")))
except (TypeError, ValueError):
    BOLTZ2_RATE_LIMIT_RETRIES = 4

try:
    BOLTZ2_RATE_LIMIT_BACKOFF_SECONDS = max(
        0.5, float(os.getenv("BOLTZ2_RATE_LIMIT_BACKOFF_SECONDS", "2.0"))
    )
except (TypeError, ValueError):
    BOLTZ2_RATE_LIMIT_BACKOFF_SECONDS = 2.0

try:
    BOLTZ2_RATE_LIMIT_MAX_WAIT_SECONDS = max(
        1.0, float(os.getenv("BOLTZ2_RATE_LIMIT_MAX_WAIT_SECONDS", "60.0"))
    )
except (TypeError, ValueError):
    BOLTZ2_RATE_LIMIT_MAX_WAIT_SECONDS = 60.0

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

def _get_retry_delay_seconds(response, retry_index: int, default_backoff_seconds: float) -> float:
    """
    Compute retry delay, honoring Retry-After header when available.
    """
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        retry_after = retry_after.strip()
        if retry_after:
            try:
                retry_delay = float(retry_after)
                if retry_delay > 0:
                    return min(retry_delay, BOLTZ2_RATE_LIMIT_MAX_WAIT_SECONDS)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at is not None:
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        retry_delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                        if retry_delay > 0:
                            return min(retry_delay, BOLTZ2_RATE_LIMIT_MAX_WAIT_SECONDS)
                except (TypeError, ValueError, OverflowError):
                    pass

    retry_delay = default_backoff_seconds * max(1, retry_index)
    return min(retry_delay, BOLTZ2_RATE_LIMIT_MAX_WAIT_SECONDS)


def get_job_queue_key(org_id: Optional[int] = None) -> str:
    """Get Redis job queue key for Boltz2 jobs."""
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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (boltz2) - %(message)s',
        handlers=[
            logging.FileHandler(f'boltz2_worker_{worker_id}.log'),
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


def _normalize_pae_matrix(pae: Any) -> Optional[List[List[float]]]:
    """Return a single 2D PAE matrix (residue x residue) from the NVIDIA `pae`
    field, which may be a single NxN matrix or a per-model list of matrices."""
    if not isinstance(pae, list) or not pae:
        return None
    first = pae[0]
    # 3D: per-model list of NxN matrices -> use the first model.
    if isinstance(first, list) and first and isinstance(first[0], list):
        return first
    # 2D: already a single matrix.
    if isinstance(first, list):
        return pae
    return None


def extract_ligand_sdf_from_mmcif(mmcif_content: str, ligand_id: str = None) -> Optional[str]:
    """
    Extract ligand coordinates from mmCIF and convert to SDF format.

    Args:
        mmcif_content: The mmCIF file content as string
        ligand_id: Optional specific ligand ID to extract (extracts all HETATMs if None)

    Returns:
        SDF file content as string, or None if extraction fails
    """
    try:
        # Try using RDKit if available
        try:
            from rdkit import Chem
            from rdkit.Chem import AllChem

            # Parse mmCIF to get ligand atoms
            ligand_atoms = []
            ligand_bonds = []
            atom_coords = {}
            atom_elements = {}
            atom_names = {}
            current_ligand_resname = None

            in_atom_site = False
            atom_site_cols = {}

            for line in mmcif_content.split('\n'):
                line = line.strip()

                # Detect atom_site loop columns
                if line.startswith('_atom_site.'):
                    col_name = line.split('.')[1].strip()
                    atom_site_cols[col_name] = len(atom_site_cols)
                    in_atom_site = True
                    continue

                if in_atom_site and line.startswith('_'):
                    in_atom_site = False
                    atom_site_cols = {}
                    continue

                if in_atom_site and line.startswith('#'):
                    in_atom_site = False
                    atom_site_cols = {}
                    continue

                # Parse HETATM records
                if line.startswith('HETATM') or (in_atom_site and not line.startswith('_') and not line.startswith('#') and line):
                    parts = line.split()
                    if len(parts) < 10:
                        continue

                    # Simple parsing for standard mmCIF layout
                    try:
                        record_type = parts[0]
                        atom_id = parts[1]
                        atom_name = parts[3] if len(parts) > 3 else 'C'
                        comp_id = parts[5] if len(parts) > 5 else 'UNK'  # Residue name
                        # Get chain ID (auth_asym_id) - usually around index 9
                        chain_id = parts[9] if len(parts) > 9 else ''

                        # Skip water and common ions
                        if comp_id in ['HOH', 'WAT', 'NA', 'CL', 'MG', 'CA', 'ZN', 'K']:
                            continue

                        # If ligand_id specified, check both comp_id (residue name) and chain_id
                        # Boltz2 API uses 'L' as chain ID but comp_id may be 'LIG1', 'LIG', etc.
                        if ligand_id:
                            ligand_match = (comp_id == ligand_id or
                                          chain_id == ligand_id or
                                          comp_id.startswith('LIG'))  # Accept any LIG* residue
                            if not ligand_match:
                                continue

                        if record_type == 'HETATM':
                            # Find coordinates (usually around index 10-12)
                            x = y = z = None
                            for i, p in enumerate(parts):
                                try:
                                    val = float(p)
                                    if i >= 10 and x is None:
                                        x = val
                                    elif i >= 11 and y is None:
                                        y = val
                                    elif i >= 12 and z is None:
                                        z = val
                                        break
                                except ValueError:
                                    continue
                            x = x if x is not None else 0.0
                            y = y if y is not None else 0.0
                            z = z if z is not None else 0.0

                            # Get element (usually last column or from atom name)
                            element = parts[-1] if len(parts[-1]) <= 2 else atom_name[0]

                            atom_coords[atom_id] = (x, y, z)
                            atom_elements[atom_id] = element.capitalize()
                            atom_names[atom_id] = atom_name
                            ligand_atoms.append(atom_id)
                            current_ligand_resname = comp_id
                    except (IndexError, ValueError):
                        continue

            if not ligand_atoms:
                logger.debug("No ligand atoms found in mmCIF")
                return None

            # Create RDKit molecule from extracted atoms
            mol = Chem.RWMol()
            atom_map = {}

            for i, atom_id in enumerate(ligand_atoms):
                element = atom_elements.get(atom_id, 'C')
                # Handle common element symbols
                if element.upper() in ['C', 'N', 'O', 'S', 'P', 'F', 'CL', 'BR', 'I', 'H']:
                    atom = Chem.Atom(element.upper())
                else:
                    atom = Chem.Atom('C')  # Default to carbon
                idx = mol.AddAtom(atom)
                atom_map[atom_id] = idx

            # Set 3D coordinates
            conf = Chem.Conformer(len(ligand_atoms))
            for atom_id, idx in atom_map.items():
                x, y, z = atom_coords.get(atom_id, (0, 0, 0))
                conf.SetAtomPosition(idx, (x, y, z))

            mol.AddConformer(conf, assignId=True)

            # Try to connect atoms and assign bond orders
            try:
                Chem.SanitizeMol(mol)
            except:
                # If sanitization fails, try to determine connectivity from distances
                AllChem.EmbedMolecule(mol, useRandomCoords=False)

            # Convert to SDF
            sdf_content = Chem.MolToMolBlock(mol)
            if sdf_content:
                # Add proper SDF header
                header = f"{current_ligand_resname or 'LIGAND'}\n     RDKit          3D\n\n"
                return header + sdf_content.split('\n', 3)[-1] + "$$$$\n"

            return None

        except ImportError:
            logger.debug("RDKit not available, using simple SDF extraction")
            return extract_ligand_sdf_simple(mmcif_content, ligand_id)

    except Exception as e:
        logger.warning(f"Error extracting ligand SDF: {e}")
        return None


def extract_protein_only_cif(mmcif_content: str) -> Optional[str]:
    """
    Extract protein-only content from mmCIF, removing all HETATM (ligand) records.
    This creates a clean protein structure for docking-style display.

    Args:
        mmcif_content: The full mmCIF file content

    Returns:
        mmCIF content with only ATOM records (no HETATM/ligands)
    """
    try:
        lines = mmcif_content.split('\n')
        protein_lines = []
        skip_hetatm = False

        for line in lines:
            # Skip HETATM records
            if line.startswith('HETATM'):
                continue

            # Skip ligand entity definitions in _entity section
            # But keep most metadata intact
            protein_lines.append(line)

        result = '\n'.join(protein_lines)

        # Verify we still have ATOM records
        if 'ATOM' in result:
            return result
        return None

    except Exception as e:
        logger.warning(f"Error extracting protein-only CIF: {e}")
        return None


def extract_ligand_sdf_simple(mmcif_content: str, ligand_id: str = None) -> Optional[str]:
    """
    Simple extraction of ligand coordinates to SDF without RDKit.
    Creates a basic SDF file with atom coordinates (no bond information).

    Args:
        mmcif_content: The mmCIF file content as string
        ligand_id: Optional specific ligand ID to extract

    Returns:
        Basic SDF file content as string
    """
    try:
        atoms = []
        ligand_name = ligand_id or 'LIGAND'

        for line in mmcif_content.split('\n'):
            if line.startswith('HETATM'):
                parts = line.split()
                if len(parts) < 12:
                    continue

                try:
                    resname = parts[5] if len(parts) > 5 else 'UNK'
                    # Get chain ID (auth_asym_id) - usually around index 9
                    chain_id = parts[9] if len(parts) > 9 else ''

                    # Skip water and ions
                    if resname in ['HOH', 'WAT', 'NA', 'CL', 'MG', 'CA', 'ZN', 'K']:
                        continue

                    # Check both resname (comp_id) and chain_id for ligand matching
                    if ligand_id:
                        ligand_match = (resname == ligand_id or
                                      chain_id == ligand_id or
                                      resname.startswith('LIG'))  # Accept any LIG* residue
                        if not ligand_match:
                            continue

                    ligand_name = resname

                    # Extract coordinates and element
                    atom_name = parts[3] if len(parts) > 3 else 'C'

                    # Find coordinate values
                    x = y = z = 0.0
                    for i, p in enumerate(parts[10:15]):
                        try:
                            val = float(p)
                            if i == 0:
                                x = val
                            elif i == 1:
                                y = val
                            elif i == 2:
                                z = val
                        except ValueError:
                            continue

                    element = parts[-1] if len(parts[-1]) <= 2 else atom_name[0]
                    element = element.capitalize()

                    atoms.append({
                        'x': x,
                        'y': y,
                        'z': z,
                        'element': element
                    })
                except (IndexError, ValueError):
                    continue

        if not atoms:
            return None

        # Build basic SDF file
        lines = []
        lines.append(ligand_name)
        lines.append("  Phonos/Boltz2")
        lines.append("")

        # Counts line: atoms, bonds, etc.
        lines.append(f"{len(atoms):3d}  0  0  0  0  0  0  0  0999 V2000")

        # Atom block
        for atom in atoms:
            lines.append(f"{atom['x']:10.4f}{atom['y']:10.4f}{atom['z']:10.4f} {atom['element']:2s}   0  0  0  0  0  0  0  0  0  0  0  0")

        # End
        lines.append("M  END")
        lines.append("$$$$")

        return '\n'.join(lines)

    except Exception as e:
        logger.warning(f"Error in simple SDF extraction: {e}")
        return None


def extract_plddt_from_mmcif(mmcif_content: str) -> List[float]:
    """
    Extract per-residue pLDDT scores from mmCIF files.
    First tries ModelCIF _ma_qa_metric_local format, then falls back to B-factors.
    Returns list of scores normalized to 0-100.
    """
    try:
        lines = mmcif_content.split('\n')
        
        # Strategy 1: Look for _ma_qa_metric_local (ModelCIF standard) - most reliable
        qa_metric_indices = {}
        qa_scores = []
        in_qa_loop = False
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            
            if line.startswith('loop_'):
                in_qa_loop = False
                continue
            
            if '_ma_qa_metric_local.' in line:
                in_qa_loop = True
                header = line.split('.')[-1].strip()
                qa_metric_indices[header] = len(qa_metric_indices)
                continue
            
            if in_qa_loop and line.startswith('#'):
                in_qa_loop = False
                continue
            
            if in_qa_loop and not line.startswith('_') and line:
                parts = line.split()
                if len(parts) < len(qa_metric_indices):
                    continue
                
                try:
                    val_idx = qa_metric_indices.get('metric_value')
                    if val_idx is not None:
                        score = float(parts[val_idx])
                        qa_scores.append(score)
                except (ValueError, IndexError):
                    continue
            
            if in_qa_loop and line.startswith('_') and '_ma_qa_metric_local.' not in line:
                in_qa_loop = False
        
        if qa_scores:
            # Normalize if needed (0-1 -> 0-100)
            if max(qa_scores) <= 1.0:
                qa_scores = [s * 100 for s in qa_scores]
            logger.info(f"Extracted {len(qa_scores)} pLDDT scores from _ma_qa_metric_local")
            return qa_scores
        
        # Strategy 2: Fallback to ATOM records B-factors
        plddt_scores = []
        seen_residues = set()
        in_atom_site = False
        headers = {}
        
        for line in lines:
            line = line.strip()
            
            if line.startswith('loop_'):
                in_atom_site = False
                headers = {}
                continue
            
            if line.startswith('_atom_site.'):
                in_atom_site = True
                header = line.split('.')[1].strip()
                headers[header] = len(headers)
                continue
            
            if in_atom_site and line.startswith('#'):
                in_atom_site = False
                headers = {}
                continue
            
            if in_atom_site and not line.startswith('_') and line:
                parts = line.split()
                if len(parts) < len(headers):
                    continue
                
                try:
                    group_idx = headers.get('group_PDB')
                    atom_idx = headers.get('label_atom_id')
                    res_idx = headers.get('label_seq_id')
                    chain_idx = headers.get('label_asym_id')
                    bfactor_idx = headers.get('B_iso_or_equiv')
                    
                    if any(idx is None for idx in [group_idx, atom_idx, res_idx, chain_idx, bfactor_idx]):
                        continue
                    
                    if parts[group_idx] != 'ATOM':
                        continue
                    
                    atom_name = parts[atom_idx]
                    chain_id = parts[chain_idx]
                    res_id = parts[res_idx]
                    
                    if atom_name not in ['CA', "C4'"]:
                        continue
                    
                    res_key = (chain_id, res_id)
                    if res_key in seen_residues:
                        continue
                    
                    score = float(parts[bfactor_idx])
                    plddt_scores.append(score)
                    seen_residues.add(res_key)
                except (ValueError, IndexError):
                    continue
        
        # Normalize if needed (0-1 -> 0-100)
        if plddt_scores and max(plddt_scores) <= 1.0:
            plddt_scores = [s * 100 for s in plddt_scores]
        
        if plddt_scores:
            logger.info(f"Extracted {len(plddt_scores)} pLDDT scores from B-factors")
        
        return plddt_scores
        
    except Exception as e:
        logger.warning(f"Error extracting pLDDT scores: {e}")
        return []


def call_boltz2_api(
    polymers: List[Dict[str, Any]],
    ligands: List[Dict[str, Any]] = None,
    constraints: List[Dict[str, Any]] = None,
    recycling_steps: int = 3,
    sampling_steps: int = 50,
    diffusion_samples: int = 1,
    step_scale: float = 1.2,
    without_potentials: bool = False,
    write_full_pae: bool = False,
    concatenate_msas: bool = False,
    # Affinity parameters
    predict_affinity: bool = False,
    sampling_steps_affinity: int = 200,
    diffusion_samples_affinity: int = 5,
    affinity_mw_correction: bool = False
) -> Dict[str, Any]:
    """
    Call NVIDIA Boltz2 biomolecular structure prediction API with NVCF long-polling.

    Args:
        polymers: List of polymer definitions (protein, dna, rna) with optional msa/modifications
        ligands: List of ligand definitions (smiles or ccd)
        constraints: List of structural constraints (pocket, contact, bond)
        recycling_steps: Refinement iterations (1-6, default 3)
        sampling_steps: Diffusion sampling steps (10-1000, default 50)
        diffusion_samples: Independent structures to generate (1-5, default 1)
        step_scale: Magnitude per sampling step (0.5-5.0, default 1.2)
        without_potentials: Exclude potential calculations (default False)
        write_full_pae: Include full PAE matrix in output artifacts
        concatenate_msas: Concatenate MSAs for complex prediction (default False)
        predict_affinity: Calculate binding affinity for ligand
        sampling_steps_affinity: Steps for affinity calculation (10-1000, default 200)
        diffusion_samples_affinity: Samples for affinity (1-10, default 5)
        affinity_mw_correction: Apply molecular weight correction (default False)

    Returns:
        Dictionary with predicted structures and metrics
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "polymers": polymers,
        "recycling_steps": min(max(1, recycling_steps), 6),
        "sampling_steps": min(max(10, sampling_steps), 1000),
        "diffusion_samples": min(max(1, diffusion_samples), 5),
        "step_scale": min(max(0.5, step_scale), 5.0),
        "without_potentials": without_potentials,
        "write_full_pae": bool(write_full_pae),
        "concatenate_msas": concatenate_msas
    }

    # Add ligands if present
    if ligands:
        payload["ligands"] = ligands

    # Add constraints if present
    if constraints:
        payload["constraints"] = constraints

    # Add affinity parameters if predicting affinity
    if ligands and predict_affinity and len(ligands) == 1:
        ligands[0]["predict_affinity"] = True
        payload["sampling_steps_affinity"] = min(max(10, sampling_steps_affinity), 1000)
        payload["diffusion_samples_affinity"] = min(max(1, diffusion_samples_affinity), 10)
        payload["affinity_mw_correction"] = affinity_mw_correction

    logger.info(f"Calling Boltz2 API with {len(polymers)} polymers and {len(ligands or [])} ligands...")
    logger.debug(f"Payload: {json.dumps(payload, indent=2)[:800]}...")

    # Initial request (NVIDIA can take a while before returning 202 task ids)
    response = None
    attempt = 0
    rate_limit_retry_count = 0
    while attempt < BOLTZ2_INITIAL_REQUEST_RETRIES:
        attempt += 1
        try:
            response = requests.post(
                url=NVIDIA_BOLTZ2_URL,
                headers=headers,
                json=payload,
                timeout=(BOLTZ2_INITIAL_CONNECT_TIMEOUT, BOLTZ2_INITIAL_READ_TIMEOUT)
            )
            if response.status_code == 429:
                if rate_limit_retry_count >= BOLTZ2_RATE_LIMIT_RETRIES:
                    logger.warning(
                        "Initial Boltz2 request got HTTP 429 and exhausted rate-limit retries (%s/%s).",
                        rate_limit_retry_count,
                        BOLTZ2_RATE_LIMIT_RETRIES
                    )
                    break

                rate_limit_retry_count += 1
                retry_delay = _get_retry_delay_seconds(
                    response=response,
                    retry_index=rate_limit_retry_count,
                    default_backoff_seconds=BOLTZ2_RATE_LIMIT_BACKOFF_SECONDS
                )
                logger.warning(
                    "Initial Boltz2 request got HTTP 429 "
                    "(attempt %s/%s, rate-limit retry %s/%s). Retrying in %.1fs.",
                    attempt,
                    BOLTZ2_INITIAL_REQUEST_RETRIES,
                    rate_limit_retry_count,
                    BOLTZ2_RATE_LIMIT_RETRIES,
                    retry_delay
                )
                time.sleep(retry_delay)
                attempt -= 1  # Keep a separate budget for rate-limit retries.
                continue

            # Retry transient gateway/proxy responses from upstream.
            if response.status_code in (502, 503, 504):
                logger.warning(
                    "Initial Boltz2 request got HTTP %s "
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
                "Initial Boltz2 request timed out "
                f"(attempt {attempt}/{BOLTZ2_INITIAL_REQUEST_RETRIES}, "
                f"connect={BOLTZ2_INITIAL_CONNECT_TIMEOUT}s, read={BOLTZ2_INITIAL_READ_TIMEOUT}s)"
            )
            if attempt >= BOLTZ2_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.RequestException as req_err:
            logger.warning(
                "Initial Boltz2 request failed "
                f"(attempt {attempt}/{BOLTZ2_INITIAL_REQUEST_RETRIES}): {req_err}"
            )
            if attempt >= BOLTZ2_INITIAL_REQUEST_RETRIES:
                raise
            time.sleep(BOLTZ2_INITIAL_RETRY_BACKOFF_SECONDS * attempt)

    if response is None:
        raise requests.exceptions.Timeout("Boltz2 initial request did not return a response")

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
        raise Exception(f"Boltz2 API error: {response.status_code} - {response.text[:500]}")


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
    rate_limit_poll_count = 0

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            response = requests.get(
                status_url,
                headers=headers,
                timeout=BOLTZ2_STATUS_REQUEST_TIMEOUT
            )

            if response.status_code == 200:
                # Task complete
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return response.json()
                else:
                    return {"raw_content": response.text[:5000]}

            elif response.status_code == 202:
                # Still processing
                rate_limit_poll_count = 0
                logger.debug(f"Task {task_id} still processing...")
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            elif response.status_code == 429:
                rate_limit_poll_count += 1
                retry_delay = _get_retry_delay_seconds(
                    response=response,
                    retry_index=rate_limit_poll_count,
                    default_backoff_seconds=float(NVCF_POLL_INTERVAL)
                )
                logger.warning(
                    "Task %s status returned HTTP 429, retrying in %.1fs "
                    "(rate-limit retry %s).",
                    task_id,
                    retry_delay,
                    rate_limit_poll_count
                )
                time.sleep(retry_delay)
                continue

            elif response.status_code == 404:
                raise Exception(f"Task {task_id} not found")

            elif response.status_code >= 500:
                rate_limit_poll_count = 0
                logger.warning(
                    "Task %s status returned %s, retrying...",
                    task_id,
                    response.status_code
                )
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            else:
                raise Exception(f"Status check error: {response.status_code} - {response.text[:500]}")

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout polling status for {task_id}, retrying...")
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Task {task_id} timed out after {NVCF_POLL_SECONDS} seconds")


def process_boltz2_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Process a single Boltz2 structure prediction job.

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
        'model_used': 'boltz2',
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})

        # Build polymers list from job parameters
        polymers = []

        # Determine if multimer (chains list) or monomer (single sequence)
        chains = params.get('chains')
        if isinstance(chains, list) and len(chains) > 0:
            # --- Multimer mode ---
            for i, chain in enumerate(chains):
                chain_id = chain.get('chain_id', chr(65 + i))
                chain_type = chain.get('molecule_type', 'protein').lower()
                chain_seq = chain.get('sequence', '')
                chain_struct = chain.get('structure', '')

                polymer = {"id": chain_id, "molecule_type": chain_type}
                
                if chain_struct:
                    polymer["structure"] = chain_struct
                elif chain_seq:
                    polymer["sequence"] = chain_seq.upper().replace(' ', '').replace('\n', '')
                
                if chain.get('msa'):
                    polymer["msa"] = chain.get('msa')
                
                polymers.append(polymer)
            
            molecule_type = polymers[0]['molecule_type'] if polymers else 'protein'
        else:
            # --- Monomer mode (legacy) ---
            molecule_type = params.get('molecule_type', 'protein')
            sequence = params.get('sequence', params.get('protein_sequence', ''))
            is_cyclic = params.get('cyclic', False)

            if sequence:
                # Clean sequence
                sequence_clean = sequence.upper().replace(' ', '').replace('\n', '').replace('\r', '')
                
                # Build polymer definition
                polymer = {
                    "molecule_type": molecule_type,
                    "sequence": sequence_clean,
                    "id": params.get('chain_id', 'A')
                }
                if is_cyclic:
                    polymer["cyclic"] = True

                polymers.append(polymer)

        if not polymers:
            result['message'] = "No sequence provided"
            return result

        # Build ligands list
        ligands = []
        ligand_input_type = params.get('ligand_input_type', 'none')
        ligand_smiles = params.get('ligand_smiles', params.get('ligand', ''))
        ligand_ccd = params.get('ligand_ccd', '')

        # Auto-detect ligand type when ligand_input_type is not explicitly set
        if ligand_input_type == 'none':
            if ligand_smiles:
                ligand_input_type = 'smiles'
            elif ligand_ccd:
                ligand_input_type = 'ccd'

        if ligand_input_type == 'smiles' and ligand_smiles:
            ligands.append({
                "id": params.get('ligand_id', 'L'),
                "smiles": ligand_smiles.strip()
            })
        elif ligand_input_type == 'ccd' and ligand_ccd:
            ccd_clean = ligand_ccd.upper().strip()[:5]  # Max 5 chars
            if ccd_clean:
                ligands.append({
                    "id": params.get('ligand_id', 'L'),
                    "ccd": ccd_clean
                })

        # Extract prediction parameters
        recycling_steps = int(params.get('recycling_steps', 3))
        sampling_steps = int(params.get('sampling_steps', 50))
        diffusion_samples = int(params.get('diffusion_samples', 1))
        step_scale = float(params.get('step_scale', 1.2))
        without_potentials = params.get('without_potentials', False)
        write_full_pae = params.get('write_full_pae', False)
        concatenate_msas = params.get('concatenate_msas', False)

        # Affinity parameters
        predict_affinity = params.get('predict_affinity', False)
        sampling_steps_affinity = int(params.get('sampling_steps_affinity', 200))
        diffusion_samples_affinity = int(params.get('diffusion_samples_affinity', 5))
        affinity_mw_correction = params.get('affinity_mw_correction', False)

        # Constraints (optional)
        constraints = params.get('constraints', None)

        logger.info(f"Job {job_id}: Predicting structure for {len(polymers)} polymers ({molecule_type}) and {len(ligands)} ligands...")

        # Call Boltz2 API
        api_result = call_boltz2_api(
            polymers=polymers,
            ligands=ligands if ligands else None,
            constraints=constraints,
            recycling_steps=recycling_steps,
            sampling_steps=sampling_steps,
            diffusion_samples=diffusion_samples,
            step_scale=step_scale,
            without_potentials=without_potentials,
            write_full_pae=write_full_pae,
            concatenate_msas=concatenate_msas,
            predict_affinity=predict_affinity,
            sampling_steps_affinity=sampling_steps_affinity,
            diffusion_samples_affinity=diffusion_samples_affinity,
            affinity_mw_correction=affinity_mw_correction
        )

        execution_time = time.time() - start_time

        # Extract key metrics and runtime info from API response
        confidence_scores = api_result.get('confidence_scores', [])
        ptm_scores = api_result.get('ptm_scores', [])
        iptm_scores = api_result.get('iptm_scores', [])
        complex_plddt = api_result.get('complex_plddt_scores', [])
        structures = api_result.get('structures', [])
        runtime_metrics = api_result.get('metrics', {})
        # Pull the heavy PAE/PDE matrices OUT of api_result so they are not embedded in
        # the inline metadata JSON. For large proteins the full matrix made the result
        # payload huge — slow to serialize and large enough to exceed the Redis pub/sub
        # buffer — which left jobs stuck on "processing". When the user requested the
        # full PAE we persist it separately as a gzip-compressed artifact instead.
        # NB the NVIDIA API returns these under the keys `pae`/`pde` (not `pae_matrix`).
        pae_raw = api_result.pop('pae', None)
        api_result.pop('pde', None)
        pae_matrix = _normalize_pae_matrix(pae_raw) if write_full_pae else None

        if not structures:
            result['status'] = 'error'
            result['message'] = "Boltz2 API completed but returned no structures. This may be due to sequence complexity or model limitations."
            logger.error(f"Job {job_id}: No structures returned by API")
            return result

        # Get primary structure content
        primary_structure = ""
        structure_format = 'mmcif'  # Default format from Boltz2 API

        # Prepare artifacts for all structures
        artifacts = []

        # Get ligand info for SDF extraction
        ligand_id_for_extraction = None
        if ligands:
            ligand_id_for_extraction = ligands[0].get('id', 'L')

        num_models = len(structures)
        logger.info(f"Job {job_id}: Processing {num_models} model(s) from Boltz2 API")

        if structures:
            # Use first structure as primary
            primary_structure = structures[0].get('structure', '')
            structure_format = structures[0].get('format', 'mmcif')

            # Process all structures as artifacts
            for i, struct in enumerate(structures):
                content = struct.get('structure', '')
                fmt = struct.get('format', 'mmcif')
                ext = '.cif' if fmt == 'mmcif' else '.pdb'

                if content:
                    content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')

                    # Naming: jobid_model_0.cif (or _ranked_0 for first)
                    if num_models > 1:
                        filename = f"{job_id}_model_{i}{ext}"
                        display_name = f"Model {i+1}"
                        # Add confidence score to display name if available
                        if i < len(confidence_scores):
                            display_name = f"Model {i+1} (conf: {confidence_scores[i]:.2f})"
                    else:
                        filename = f"{job_id}{ext}"
                        display_name = "Structure"

                    structure_name = struct.get('name', '')
                    structure_source = struct.get('source', '')

                    artifacts.append({
                        "filename": filename,
                        "content_b64": content_b64,
                        "name": "structure_model",
                        "display_name": display_name,
                        "model_index": i,
                        "confidence_score": confidence_scores[i] if i < len(confidence_scores) else None,
                        "structure_name": structure_name or None,
                        "structure_source": structure_source or None,
                    })

                    # Extract ligand SDF from each model if ligands were used
                    if ligands and content:
                        try:
                            sdf_content = extract_ligand_sdf_from_mmcif(content, ligand_id_for_extraction)
                            if sdf_content:
                                sdf_b64 = base64.b64encode(sdf_content.encode('utf-8')).decode('utf-8')

                                if num_models > 1:
                                    sdf_filename = f"{job_id}_ligand_model_{i}.sdf"
                                    sdf_display = f"Ligand Model {i+1}"
                                else:
                                    sdf_filename = f"{job_id}_ligand.sdf"
                                    sdf_display = "Ligand (SDF)"

                                artifacts.append({
                                    "filename": sdf_filename,
                                    "content_b64": sdf_b64,
                                    "name": "ligand_sdf",
                                    "display_name": sdf_display,
                                    "model_index": i,
                                    "ligand_id": ligand_id_for_extraction,
                                    "type": "ligand"
                                })
                                logger.info(f"Job {job_id}: Extracted ligand SDF for model {i}")

                                # Also create protein-only CIF (without HETATM) for clean docking display
                                protein_only_cif = extract_protein_only_cif(content)
                                if protein_only_cif:
                                    protein_b64 = base64.b64encode(protein_only_cif.encode('utf-8')).decode('utf-8')
                                    if num_models > 1:
                                        protein_filename = f"{job_id}_protein_model_{i}.cif"
                                        protein_display = f"Protein Model {i+1}"
                                    else:
                                        protein_filename = f"{job_id}_protein.cif"
                                        protein_display = "Protein (CIF)"

                                    artifacts.append({
                                        "filename": protein_filename,
                                        "content_b64": protein_b64,
                                        "name": "protein_only",
                                        "display_name": protein_display,
                                        "model_index": i,
                                        "type": "protein"
                                    })
                                    logger.info(f"Job {job_id}: Created protein-only CIF for model {i}")

                        except Exception as e:
                            logger.warning(f"Job {job_id}: Could not extract ligand SDF for model {i}: {e}")

        # Persist the full PAE matrix as a gzip-compressed artifact (kept out of the
        # inline JSON so the result payload stays small). The UI fetches & renders it
        # lazily. Compression keeps even large matrices well under the pub/sub limit.
        if pae_matrix:
            try:
                import gzip as _gzip
                pae_doc = json.dumps({'pae_matrix': clean_nan_values(pae_matrix)}).encode('utf-8')
                pae_gz = _gzip.compress(pae_doc, compresslevel=6)
                artifacts.append({
                    'filename': f'{job_id}_pae.json.gz',
                    'content_b64': base64.b64encode(pae_gz).decode('utf-8'),
                    'name': 'pae_matrix',
                    'display_name': 'PAE Matrix',
                    'type': 'pae',
                })
                result['has_pae_matrix'] = True
                result['pae_matrix_size'] = len(pae_matrix)
                logger.info(
                    f"Job {job_id}: stored compressed PAE matrix "
                    f"({len(pae_matrix)}x{len(pae_matrix)} residues, {len(pae_gz)} bytes gz)"
                )
            except Exception as e:
                logger.warning(f"Job {job_id}: failed to store PAE matrix artifact: {e}")

        # Extract pLDDT scores from the primary structure (Model 0)
        plddt_scores = []
        if primary_structure:
            plddt_scores = extract_plddt_from_mmcif(primary_structure)
            if not plddt_scores and confidence_scores:
                # Fallback: if extraction failed but we have model confidence
                logger.warning(f"Job {job_id}: pLDDT extraction failed, using global confidence")
        
        # Calculate statistics
        plddt_avg = sum(plddt_scores) / len(plddt_scores) if plddt_scores else (confidence_scores[0] * 100 if confidence_scores else 0)
        plddt_min = min(plddt_scores) if plddt_scores else 0
        plddt_max = max(plddt_scores) if plddt_scores else 0

        # Prepare output in JSON format for metadata
        # Ensure we don't have NaN/Infinity which invalidates JSON
        cleaned_result = clean_nan_values(api_result)
        output_data = json.dumps(cleaned_result, indent=2)

        # Get output filename from job output_path
        output_path = job.get('output_path', '')
        if output_path:
            output_filename = os.path.basename(output_path)
        else:
            ext = '.cif' if structure_format == 'mmcif' else '.pdb'
            output_filename = f"{job_id}{ext}"

        # Encode output as base64 for the listener
        output_content_b64 = base64.b64encode(output_data.encode('utf-8')).decode('utf-8')

        # Also save structure file separately if present
        structure_file_b64 = None
        if primary_structure:
            structure_file_b64 = base64.b64encode(primary_structure.encode('utf-8')).decode('utf-8')

        # Count ligand SDFs and protein-only files in artifacts
        num_sdf_files = len([a for a in artifacts if a.get('name') == 'ligand_sdf'])
        num_protein_files = len([a for a in artifacts if a.get('name') == 'protein_only'])
        has_ligand = num_sdf_files > 0

        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': output_content_b64,
            'output_filename': output_filename.replace('.json', '') + '.json',
            'structure_content': structure_file_b64,
            'structure_filename': output_filename if not output_filename.endswith('.json') else output_filename.replace('.json', f'.{structure_format}'),
            'structure_format': structure_format,
            'artifacts': artifacts,
            # Model count info
            'num_models': num_models,
            'num_ligand_sdf': num_sdf_files,
            'num_protein_only': num_protein_files,
            'has_ligand': has_ligand,
            'docking_style': has_ligand,  # Treat as docking job when ligand present
            'diffusion_samples_requested': diffusion_samples,
            # Input parameters used (for metadata/reproducibility)
            'recycling_steps': recycling_steps,
            'sampling_steps': sampling_steps,
            'step_scale': step_scale,
            'predict_affinity': predict_affinity,
            'write_full_pae': write_full_pae,
            'concatenate_msas': concatenate_msas,
            'without_potentials': without_potentials,
            'ligand_input_type': ligand_input_type,
            'ligand_smiles': ligand_smiles if ligand_input_type == 'smiles' else None,
            'ligand_ccd': ligand_ccd if ligand_input_type == 'ccd' else None,
            # All scores (for multiple models)
            'confidence_scores': confidence_scores,
            'ptm_scores': ptm_scores,
            'iptm_scores': iptm_scores,
            'complex_plddt_scores': complex_plddt,
            'plddt_scores': plddt_scores,
            'plddt_avg': plddt_avg,
            'plddt_min': plddt_min,
            'plddt_max': plddt_max,
            # Key metrics for display (best/first model)
            'confidence_score': confidence_scores[0] if confidence_scores else None,
            'ptm_score': ptm_scores[0] if ptm_scores else None,
            'iptm_score': iptm_scores[0] if iptm_scores else None,
            'complex_plddt': complex_plddt[0] if complex_plddt else None,
            'affinities': api_result.get('affinities', {}),
            # Runtime metrics from NIM (debugging/performance)
            'runtime_metrics': runtime_metrics if runtime_metrics else None,
            'message': None
        })

        logger.info(f"Job {job_id}: Structure prediction completed in {execution_time:.2f}s")
        logger.info(f"Job {job_id}: Generated {num_models} model(s), {num_sdf_files} ligand SDF file(s)")
        if confidence_scores:
            logger.info(f"Job {job_id}: Confidence scores: {[f'{s:.3f}' for s in confidence_scores]}")

    except requests.exceptions.Timeout:
        result['message'] = "Boltz2 API request timed out"
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
    """Main worker loop - fetches and processes Boltz2 jobs."""
    r = get_redis_connection(decode_responses=False)  # Binary mode for encrypted data
    r_text = get_redis_connection(decode_responses=True)  # Text mode for results
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"Boltz2 worker started. Listening on queue: {job_queue}")

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
            result = process_boltz2_job(job, worker_id)

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
    """Main entry point for Boltz2 worker."""
    global NODE_CONFIG_FILE
    global logger

    parser = argparse.ArgumentParser(description='Boltz2 Protein Structure Prediction Worker')
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
    logger.info(f"Boltz2 Protein Structure Prediction Worker Starting")
    logger.info(f"Worker ID: {worker_id}")
    logger.info(f"Organization ID: {organization_id or 'None (public)'}")
    logger.info(f"API URL: {NVIDIA_BOLTZ2_URL}")
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
        logger.info("Boltz2 worker shutdown complete.")


# Standalone test function
def test_boltz2_api():
    """Test the Boltz2 API with a sample protein-ligand structure prediction."""
    print("Testing Boltz2 API...")

    # Test with insulin chain A (short) + ATP
    test_polymers = [{
        "id": "A",
        "molecule_type": "protein",
        "sequence": "GIVEQCCTSICSLYQLENYCN"  # Short insulin chain A
    }]

    test_ligands = [{
        "id": "LIG",
        "ccd": "ATP"  # Common ligand
    }]

    try:
        result = call_boltz2_api(
            polymers=test_polymers,
            ligands=test_ligands,
            recycling_steps=2,
            sampling_steps=50,
            diffusion_samples=1
        )

        print(f"Success!")
        print(f"Confidence scores: {result.get('confidence_scores', [])}")
        print(f"PTM scores: {result.get('ptm_scores', [])}")
        print(f"iPTM scores: {result.get('iptm_scores', [])}")
        print(f"Complex pLDDT: {result.get('complex_plddt_scores', [])}")

        structures = result.get('structures', [])
        if structures:
            print(f"Generated {len(structures)} structure(s)")
            print(f"First structure format: {structures[0].get('format')}")
            
            # Test pLDDT extraction
            structure_content = structures[0].get('structure', '')
            if structure_content:
                plddt = extract_plddt_from_mmcif(structure_content)
                print(f"Extracted {len(plddt)} pLDDT scores")
                if plddt:
                    print(f"First 5 pLDDT scores: {plddt[:5]}")
                    print(f"Min: {min(plddt):.2f}, Max: {max(plddt):.2f}, Avg: {sum(plddt)/len(plddt):.2f}")

        # Save full result
        output_file = Path("boltz2_test_output.json")
        output_file.write_text(json.dumps(result, indent=2))
        print(f"Full result saved to: {output_file}")

        # Save structure
        if structures:
            struct_file = Path("boltz2_test_structure.cif")
            struct_file.write_text(structures[0].get('structure', ''))
            print(f"Structure saved to: {struct_file}")

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
        test_boltz2_api()
    else:
        # Run as worker
        main()
