#!/usr/bin/env python3
"""
OpenFold3 Biomolecular Structure Prediction Worker Node
Connects to NVIDIA's OpenFold3 model API for structure prediction.
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
from typing import Optional, Dict, Any, List, Tuple
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
NVIDIA_OPENFOLD3_URL = os.getenv(
    "OPENFOLD3_API_URL",
    "https://health.api.nvidia.com/v1/biology/openfold/openfold3/predict"
)
NVIDIA_STATUS_URL = os.getenv(
    "OPENFOLD3_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Worker settings
WORKER_TYPE = "openfold3"
HEARTBEAT_INTERVAL = 30
NODE_CONFIG_FILE = 'node_config_openfold3.json'
JOB_RESULTS_QUEUE = "queue:job_results"
WORKER_HEARTBEAT_CHANNEL = "pubsub:worker_heartbeats"

# NVCF polling settings (configurable via env, default 900s for multimer jobs)
try:
    NVCF_POLL_SECONDS = max(60, int(os.getenv("OPENFOLD3_POLL_SECONDS", "900")))
except (TypeError, ValueError):
    NVCF_POLL_SECONDS = 900
NVCF_POLL_INTERVAL = 2

# API request settings — timeout must exceed NVCF_POLL_SECONDS for long-poll to work
API_TIMEOUT = NVCF_POLL_SECONDS + 30
API_MAX_RETRIES = 2
API_RETRY_DELAY = 5

logger = logging.getLogger(__name__)
WORKER_BASE_DIR = Path(__file__).resolve().parent
PRIVATE_KEY_FILE = "node_private_key.pem"


def _looks_like_structure_text(candidate: Any) -> bool:
    """Return True if text appears to contain structure coordinates."""
    if not isinstance(candidate, str):
        return False
    text = candidate.strip()
    if not text:
        return False
    upper = text.upper()
    return (
        'ATOM' in upper or
        'HETATM' in upper or
        '_atom_site.' in text or
        text.startswith('data_') or
        'V2000' in upper or
        'V3000' in upper or
        'M  END' in upper
    )


def _decode_structure_b64(candidate: Any) -> Optional[str]:
    """Decode a base64 string only when it decodes into likely structure text."""
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    try:
        decoded_bytes = base64.b64decode(candidate, validate=True)
    except Exception:
        return None

    try:
        decoded_text = decoded_bytes.decode('utf-8')
    except UnicodeDecodeError:
        decoded_text = decoded_bytes.decode('utf-8', errors='ignore')

    return decoded_text if _looks_like_structure_text(decoded_text) else None


def _extract_from_structure_entry(entry: Any, default_format: str) -> Tuple[str, str]:
    """Extract structure text from one structure-like entry."""
    if isinstance(entry, str):
        if _looks_like_structure_text(entry):
            return entry, default_format
        decoded = _decode_structure_b64(entry)
        if decoded:
            return decoded, default_format
        return '', default_format

    if not isinstance(entry, dict):
        return '', default_format

    entry_format = (entry.get('format') or default_format or 'pdb')
    text_keys = (
        'content', 'structure', 'pdb_content', 'pdb',
        'cif_content', 'cif', 'mmcif', 'sdf', 'mol', 'mol2'
    )
    for key in text_keys:
        candidate = entry.get(key)
        if _looks_like_structure_text(candidate):
            detected_format = entry_format
            if key in ('cif_content', 'cif', 'mmcif'):
                detected_format = 'cif'
            return candidate, detected_format

    b64_keys = ('content_b64', 'structure_b64', 'pdb_b64', 'cif_b64')
    for key in b64_keys:
        decoded = _decode_structure_b64(entry.get(key))
        if decoded:
            detected_format = entry_format
            if key == 'cif_b64':
                detected_format = 'cif'
            return decoded, detected_format

    return '', default_format


def _extract_all_structures_from_api_result(api_result: Dict[str, Any], default_format: str) -> List[Tuple[str, str]]:
    """Extract ALL structure payloads from OpenFold3 responses (for diffusion_samples > 1)."""
    results = []

    if not isinstance(api_result, dict):
        return results

    outputs = api_result.get('outputs', [])
    if isinstance(outputs, list):
        for output_entry in outputs:
            if not isinstance(output_entry, dict):
                continue

            # Current OpenFold3 response: outputs[].structures_with_scores[].structure
            for collection_key in ('structures', 'structures_with_scores', 'predictions', 'samples'):
                entries = output_entry.get(collection_key, [])
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    structure_text, detected_format = _extract_from_structure_entry(entry, default_format)
                    if structure_text:
                        results.append((structure_text, detected_format))

            # Some variants flatten one structure object directly under output entry
            if not results:
                structure_text, detected_format = _extract_from_structure_entry(output_entry, default_format)
                if structure_text:
                    results.append((structure_text, detected_format))

    if results:
        return results

    # Fallback: flat keys (structure, content, pdb_content, pdb, cif, mmcif)
    structure_text, detected_format = _extract_from_structure_entry(api_result, default_format)
    if structure_text:
        return [(structure_text, detected_format)]

    # Fallback: arrays like {"pdbs": ["ATOM..."]}
    for collection_key in ('pdbs', 'structures'):
        entries = api_result.get(collection_key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            structure_text, detected_format = _extract_from_structure_entry(entry, default_format)
            if structure_text:
                results.append((structure_text, detected_format))

    return results


def _extract_scores_from_api_result(api_result: Dict[str, Any]) -> Dict[str, List[float]]:
    """Extract per-sample scores (confidence, pTM, ipTM) from OpenFold3 response.

    Returns:
        Dict with keys 'confidence_scores', 'ptm_scores', 'iptm_scores',
        each a list of floats (one per sample).
    """
    scores: Dict[str, List[float]] = {
        'confidence_scores': [],
        'ptm_scores': [],
        'iptm_scores': [],
    }

    if not isinstance(api_result, dict):
        return scores

    outputs = api_result.get('outputs', [])
    if not isinstance(outputs, list):
        return scores

    for output_entry in outputs:
        if not isinstance(output_entry, dict):
            continue

        for collection_key in ('structures_with_scores', 'structures', 'samples'):
            entries = output_entry.get(collection_key, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # The NIM exposes the AF3 composite ranking score (0.8*ipTM + 0.2*pTM +
                # 0.5*disorder - clash). Accept every naming variant seen across versions.
                # NOTE: this is a ranking score, not a confidence percentage — for monomers
                # ipTM~0 so it sits near 0.2*pTM. Real confidence is pTM/ipTM and pLDDT.
                ranking = (entry.get('confidence_score')
                           if entry.get('confidence_score') is not None
                           else entry.get('sample_ranking_score')
                           if entry.get('sample_ranking_score') is not None
                           else entry.get('ranking_score'))
                if ranking is not None:
                    scores['confidence_scores'].append(float(ranking))
                ptm = entry.get('ptm_score') if entry.get('ptm_score') is not None else entry.get('ptm')
                if ptm is not None:
                    scores['ptm_scores'].append(float(ptm))
                iptm = entry.get('iptm_score') if entry.get('iptm_score') is not None else entry.get('iptm')
                if iptm is not None:
                    scores['iptm_scores'].append(float(iptm))

            if scores['confidence_scores']:
                break

    return scores


def _extract_structure_text_from_api_result(api_result: Dict[str, Any], default_format: str) -> Tuple[str, str]:
    """Extract first structure payload from OpenFold3 responses (backward compat)."""
    all_structures = _extract_all_structures_from_api_result(api_result, default_format)
    if all_structures:
        return all_structures[0]
    return '', default_format


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
    """Get Redis job queue key for OpenFold3 jobs."""
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
        format=f'%(asctime)s - [%(levelname)s] - {worker_id} (openfold3) - %(message)s',
        handlers=[
            logging.FileHandler(f'openfold3_worker_{worker_id}.log'),
            logging.StreamHandler()
        ],
        force=True
    )
    return logging.getLogger(__name__)


def poll_nvcf_status(task_id: str, headers: dict) -> dict:
    """
    Poll NVCF status endpoint until task completes.

    Args:
        task_id: NVCF request ID
        headers: Authorization headers

    Returns:
        Dictionary with results
    """
    status_url = NVIDIA_STATUS_URL.format(task_id=task_id)
    start_time = time.time()

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)

            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return response.json()
                else:
                    # Text response (PDB/mmCIF structure)
                    return {"structure": response.text, "output_format": "pdb"}

            elif response.status_code == 202:
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            elif response.status_code == 404:
                raise Exception(f"Task {task_id} not found")

            else:
                raise Exception(f"Status check error: {response.status_code}")

        except requests.exceptions.Timeout:
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Task {task_id} timed out after {NVCF_POLL_SECONDS} seconds")


def call_openfold3_api(
    sequence: str,
    molecule_type: str = "protein",
    chain_id: str = "A",
    output_format: str = "pdb",
    ligand_smiles: Optional[str] = None,
    ligand_ccd: Optional[str] = None,
    ligand_id: str = "L",
    msa: Optional[str] = None,
    msa_format: str = "a3m",
    diffusion_samples: int = 1,
    request_id: Optional[str] = None,
    chains: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Call NVIDIA OpenFold3 NIM structure prediction API.

    Args:
        sequence: Input sequence (protein, DNA, or RNA) — used for monomer mode
        molecule_type: Type of molecule (protein, dna, rna)
        chain_id: Chain identifier
        output_format: Output structure format (pdb or cif)
        ligand_smiles: Optional ligand SMILES string
        ligand_ccd: Optional ligand CCD code
        ligand_id: Ligand chain identifier
        msa: Optional MSA data in a3m or csv format
        msa_format: MSA format (a3m or csv)
        diffusion_samples: Number of structures to generate (1-5)
        request_id: Optional request identifier
        chains: Optional list of chain dicts for multimer mode
                 Each dict: {sequence, molecule_type, id, msa_input?}

    Returns:
        Dictionary with predicted structure
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "NVCF-POLL-SECONDS": str(NVCF_POLL_SECONDS)
    }

    molecules = []

    if chains and isinstance(chains, list) and len(chains) >= 2:
        # Multimer mode: build molecules from chains list
        for c in chains:
            c_type = c.get('molecule_type', 'protein')
            c_id = c.get('id', c.get('chain_id', 'A'))
            c_seq = c.get('sequence', '').upper().replace(' ', '').replace('\n', '')
            mol = {
                "type": c_type,
                "id": c_id,
                "sequence": c_seq,
            }
            # Protein chains need MSA
            if c_type == 'protein':
                c_msa = (c.get('msa_input') or '').strip()
                if not c_msa:
                    c_msa = f">{c_id}\n{c_seq}\n"
                mol["msa"] = {
                    "main": {
                        "a3m": {
                            "alignment": c_msa,
                            "format": "a3m"
                        }
                    }
                }
            molecules.append(mol)
    else:
        # Monomer mode
        msa = (msa or "").strip()

        # OpenFold3 requires protein sequences to include a non-empty MSA/paired MSA.
        if molecule_type == "protein" and not msa:
            msa = f">query\n{sequence.upper()}\n"
            msa_format = "a3m"

        molecule = {
            "type": molecule_type,
            "id": chain_id,
            "sequence": sequence.upper()
        }

        if msa and molecule_type == "protein":
            molecule["msa"] = {
                "main": {
                    msa_format: {
                        "alignment": msa,
                        "format": msa_format
                    }
                }
            }

        molecules = [molecule]

    # Add ligand if specified (NIM format: smiles or ccd_code)
    if ligand_smiles:
        molecules.append({
            "type": "ligand",
            "id": ligand_id,
            "smiles": ligand_smiles
        })
    elif ligand_ccd:
        if isinstance(ligand_ccd, list):
            ligand_ccd = ligand_ccd[0] if ligand_ccd else ''
        ligand_ccd = str(ligand_ccd).strip().upper()
        if ligand_ccd:
            molecules.append({
                "type": "ligand",
                "id": ligand_id,
                "ccd_codes": [ligand_ccd]
            })

    if not request_id:
        request_id = f"worker-{int(time.time())}"

    # Clamp diffusion_samples to valid range
    diffusion_samples = max(1, min(5, diffusion_samples))

    # Convert output_format for API (cif -> mmcif)
    api_output_format = "cif" if output_format == "mmcif" else output_format

    payload = {
        "request_id": request_id,
        "inputs": [
            {
                "input_id": "input-1",
                "molecules": molecules,
                "diffusion_samples": diffusion_samples,
                "output_format": api_output_format
            }
        ]
    }

    # Make the API request with retry logic
    last_error = None
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = requests.post(
                url=NVIDIA_OPENFOLD3_URL,
                headers=headers,
                json=payload,
                timeout=API_TIMEOUT
            )

            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return response.json()
                else:
                    return {"structure": response.text, "output_format": output_format}

            elif response.status_code == 202:
                # Async task - poll for results
                nvcf_reqid = response.headers.get("NVCF-REQID")
                if not nvcf_reqid:
                    raise Exception("202 response missing NVCF-REQID header")

                logger.info(f"Async task started: {nvcf_reqid}, polling...")
                return poll_nvcf_status(nvcf_reqid, {
                    "Authorization": f"Bearer {NVIDIA_API_KEY}",
                    "Accept": "application/json"
                })

            elif response.status_code == 429:
                # Rate limited - wait and retry
                if attempt < API_MAX_RETRIES:
                    wait_time = API_RETRY_DELAY * (attempt + 1)
                    logger.warning(f"Rate limited, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                raise Exception(f"Rate limited after {API_MAX_RETRIES + 1} attempts")

            else:
                raise Exception(f"API error: {response.status_code} - {response.text[:500]}")

        except requests.exceptions.Timeout:
            last_error = "API request timed out"
            if attempt < API_MAX_RETRIES:
                logger.warning(f"Timeout, retrying ({attempt + 1}/{API_MAX_RETRIES})...")
                time.sleep(API_RETRY_DELAY)
                continue
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            if attempt < API_MAX_RETRIES:
                logger.warning(f"Request error, retrying: {e}")
                time.sleep(API_RETRY_DELAY)
                continue

    raise Exception(f"OpenFold3 API failed after {API_MAX_RETRIES + 1} attempts: {last_error}")


def _is_water_or_ion_cif(parts):
    """Check if mmCIF atom entry is water or common ion."""
    return len(parts) > 5 and parts[5] in ('HOH', 'WAT', 'NA', 'CL', 'MG', 'CA', 'ZN', 'K', 'H2O')


def _is_water_or_ion_pdb(resname):
    """Check if PDB HETATM residue is water or common ion."""
    return resname in ('HOH', 'WAT', 'NA', 'CL', 'MG', 'CA', 'ZN', 'K')


def _extract_ligand_lines(lines, is_cif):
    """Extract all non-water/non-ion HETATM lines from a structure file."""
    ligand_lines = []
    for line in lines:
        stripped = line.rstrip('\n')
        if stripped.startswith('HETATM'):
            if is_cif:
                parts = stripped.split()
                if not _is_water_or_ion_cif(parts):
                    ligand_lines.append(stripped)
            else:
                if len(stripped) > 17:
                    resname = stripped[17:20].strip()
                    if not _is_water_or_ion_pdb(resname):
                        ligand_lines.append(stripped)
    return ligand_lines


def _extract_protein_only_lines(lines):
    """Keep everything except HETATM records (simple filter)."""
    result = []
    for line in lines:
        if line.startswith('HETATM'):
            continue
        result.append(line.rstrip('\n'))
    return result


# Covalent radii (Å, Cordero et al.) for distance-based bond perception. HETATM
# records carry no connectivity, so without this a ligand SDF has zero bonds and
# the viewer draws a cloud of disconnected atoms instead of a molecule.
_COVALENT_RADII = {
    'H': 0.31, 'B': 0.84, 'C': 0.76, 'N': 0.71, 'O': 0.66, 'F': 0.57,
    'P': 1.07, 'S': 1.05, 'CL': 1.02, 'BR': 1.20, 'I': 1.39, 'SI': 1.11,
    'SE': 1.20, 'NA': 1.66, 'MG': 1.41, 'K': 2.03, 'CA': 1.76, 'ZN': 1.22,
    'FE': 1.32, 'MN': 1.39, 'CO': 1.26, 'NI': 1.24, 'CU': 1.32,
}
_DEFAULT_RADIUS = 0.77
_BOND_TOLERANCE = 0.45


def _perceive_bonds(coords):
    """Single bonds between atoms within covalent range, as 0-based (i, j) pairs."""
    bonds = []
    n = len(coords)
    for i in range(n):
        xi, yi, zi, ei = coords[i]
        ri = _COVALENT_RADII.get(ei, _DEFAULT_RADIUS)
        for j in range(i + 1, n):
            xj, yj, zj, ej = coords[j]
            cutoff = ri + _COVALENT_RADII.get(ej, _DEFAULT_RADIUS) + _BOND_TOLERANCE
            dx, dy, dz = xi - xj, yi - yj, zi - zj
            d2 = dx * dx + dy * dy + dz * dz
            if 0.16 < d2 <= cutoff * cutoff:  # 0.16 = 0.4**2 guards atom overlaps
                bonds.append((i, j))
    return bonds


def _molblock_has_bonds(molblock):
    """True when a V2000 mol block's counts line declares at least one bond."""
    try:
        return int(molblock.split('\n')[3][3:6]) > 0
    except (IndexError, ValueError):
        return False


def _ligand_lines_to_sdf(ligand_lines, is_cif):
    """Convert extracted ligand atom lines to SDF format."""
    if not ligand_lines:
        return None

    try:
        from rdkit import Chem
        from rdkit.Chem import rdDetermineBonds

        atom_map = {}
        mol = Chem.RWMol()
        coords = {}
        resname = 'LIG'

        for line in ligand_lines:
            if is_cif:
                parts = line.split()
                if len(parts) < 12:
                    continue
                atom_id = parts[1]
                atom_name = parts[3] if len(parts) > 3 else 'C'
                resname = parts[5] if len(parts) > 5 else 'LIG'
                try:
                    x, y, z = float(parts[10]), float(parts[11]), float(parts[12])
                except (ValueError, IndexError):
                    continue
                element = parts[-1] if len(parts[-1]) <= 2 else atom_name[0]
            else:
                # PDB format
                if len(line) < 54:
                    continue
                atom_id = line[6:11].strip()
                atom_name = line[12:16].strip()
                resname = line[17:20].strip()
                try:
                    x = float(line[30:38])
                    y = float(line[38:46])
                    z = float(line[46:54])
                except ValueError:
                    continue
                element = line[76:78].strip() or atom_name[0]

            element = element.upper()
            if element in ('C', 'N', 'O', 'S', 'P', 'F', 'CL', 'BR', 'I', 'H'):
                atom = Chem.Atom(element)
            else:
                atom = Chem.Atom('C')
            idx = mol.AddAtom(atom)
            atom_map[atom_id] = idx
            coords[atom_id] = (x, y, z)

        if not atom_map:
            return None

        conf = Chem.Conformer(len(atom_map))
        for atom_id, idx in atom_map.items():
            x, y, z = coords.get(atom_id, (0, 0, 0))
            conf.SetAtomPosition(idx, (x, y, z))
        mol.AddConformer(conf, assignId=True)

        # HETATM blocks have no bonds — perceive them from the 3D coordinates so
        # the ligand renders connected. Never re-embed (that would scramble the
        # real binding pose); on any failure fall back to the distance-bonded SDF.
        # Connectivity (single bonds) is all the ball-and-stick viewer needs;
        # perceiving bond orders would stamp radical records onto charged ligands
        # (e.g. GTP phosphates) for no visual gain.
        perceived = mol.GetMol()
        sdf = None
        try:
            rdDetermineBonds.DetermineConnectivity(perceived)
            sdf = Chem.MolToMolBlock(perceived, kekulize=False)
        except Exception:
            sdf = None

        if sdf and _molblock_has_bonds(sdf):
            header = f"{resname}\n     RDKit          3D\n\n"
            return header + sdf.split('\n', 3)[-1] + "$$$$\n"
        return _ligand_lines_to_sdf_simple(ligand_lines, is_cif)

    except ImportError:
        return _ligand_lines_to_sdf_simple(ligand_lines, is_cif)
    except Exception:
        return _ligand_lines_to_sdf_simple(ligand_lines, is_cif)


def _ligand_lines_to_sdf_simple(ligand_lines, is_cif):
    """Distance-bonded V2000 SDF fallback (no RDKit required)."""
    if not ligand_lines:
        return None

    lines = []
    resname = 'LIG'
    coords_list = []

    for line in ligand_lines:
        if is_cif:
            parts = line.split()
            if len(parts) < 12:
                continue
            atom_name = parts[3] if len(parts) > 3 else 'C'
            resname = parts[5] if len(parts) > 5 else 'LIG'
            try:
                x = float(parts[10])
                y = float(parts[11])
                z = float(parts[12])
            except (ValueError, IndexError):
                continue
            element = parts[-1] if len(parts[-1]) <= 2 else atom_name[0]
        else:
            if len(line) < 54:
                continue
            atom_name = line[12:16].strip()
            resname = line[17:20].strip()
            try:
                x = float(line[30:38])
                y = float(line[38:46])
                z = float(line[46:54])
            except ValueError:
                continue
            element = line[76:78].strip() or atom_name[0]

        coords_list.append((x, y, z, element.upper()[:2]))

    if not coords_list:
        return None

    bonds = _perceive_bonds(coords_list)
    lines.append(f"{resname}\n")
    lines.append("  Phonos\n\n")
    lines.append(f"{len(coords_list):3d}{len(bonds):3d}  0  0  0  0  0  0  0  0999 V2000\n")
    for x, y, z, element in coords_list:
        lines.append(f"{x:10.4f}{y:10.4f}{z:10.4f} {element.ljust(2)} 0  0  0  0  0  0  0  0  0  0  0  0\n")
    for i, j in bonds:
        lines.append(f"{i + 1:3d}{j + 1:3d}  1  0\n")
    lines.append("M  END\n$$$$\n")
    return ''.join(lines)


def _extract_ligand_sdf(structure_text: str, fmt: str, ligand_id: str = 'L') -> Optional[str]:
    """Extract ligand atoms from CIF or PDB and return as SDF string."""
    is_cif = fmt in ('cif', 'mmcif')
    lines = structure_text.split('\n')
    ligand_lines = _extract_ligand_lines(lines, is_cif)
    return _ligand_lines_to_sdf(ligand_lines, is_cif)


def _extract_protein_only(structure_text: str, fmt: str) -> Optional[str]:
    """Remove ligand atoms from structure, keeping only protein."""
    _ = fmt  # format not needed for this simple filter
    lines = structure_text.split('\n')
    protein_lines = _extract_protein_only_lines(lines)
    result = '\n'.join(protein_lines)
    if '_atom_site.' in result or 'ATOM' in result:
        return result
    return None


def process_openfold3_job(job: Dict, worker_id: str) -> Dict[str, Any]:
    """
    Process a single OpenFold3 structure prediction job.

    Args:
        job: Job dictionary containing sequence and parameters
        worker_id: Worker identifier

    Returns:
        Result dictionary with predicted structure or error
    """
    job_id = job.get('job_id', 'unknown')
    result = {
        'job_id': job_id,
        'status': 'error',
        'model_used': 'openfold3',
        'worker_id': worker_id
    }

    start_time = time.time()

    try:
        params = job.get('params', {})

        # Check for multimer chains
        chains = params.get('chains') or params.get('sequences')
        is_multimer = isinstance(chains, list) and len(chains) >= 2

        sequence = params.get('sequence', '')
        molecule_type = params.get('molecule_type', 'protein').lower()

        if not is_multimer:
            if not sequence:
                result['message'] = "No input sequence provided"
                return result

            if molecule_type not in ('protein', 'dna', 'rna'):
                result['message'] = f"Invalid molecule type: {molecule_type}"
                return result

            # Validate sequence
            sequence_upper = sequence.upper().replace(' ', '').replace('\n', '')
            valid_chars = {
                'protein': set('ACDEFGHIKLMNPQRSTVWY'),
                'dna': set('ACGTN'),
                'rna': set('ACGU')
            }

            if not all(c in valid_chars[molecule_type] for c in sequence_upper):
                result['message'] = f"Invalid {molecule_type} sequence characters"
                return result
        else:
            sequence_upper = sequence.upper().replace(' ', '').replace('\n', '') if sequence else ''

        # Extract parameters
        chain_id = params.get('chain_id', 'A')
        output_format = params.get('output_format', 'pdb')
        ligand_smiles = params.get('ligand_smiles')
        ligand_ccd = params.get('ligand_ccd')
        ligand_id = params.get('ligand_id', 'L')
        msa = (params.get('msa_input') or '').strip()
        msa_format = params.get('msa_format', 'a3m')
        diffusion_samples = params.get('diffusion_samples', 1)

        if is_multimer:
            total_residues = sum(len(c.get('sequence', '')) for c in chains)
            logger.info(f"Job {job_id}: Predicting multimer structure, {len(chains)} chains, total_residues={total_residues}, samples={diffusion_samples}")
        else:
            logger.info(f"Job {job_id}: Predicting {molecule_type} structure, seq_len={len(sequence_upper)}, samples={diffusion_samples}")

        # Call OpenFold3 API
        api_result = call_openfold3_api(
            sequence=sequence_upper,
            molecule_type=molecule_type,
            chain_id=chain_id,
            output_format=output_format,
            ligand_smiles=ligand_smiles,
            ligand_ccd=ligand_ccd,
            ligand_id=ligand_id,
            msa=msa,
            msa_format=msa_format,
            diffusion_samples=diffusion_samples,
            request_id=f"job-{job_id}",
            chains=chains if is_multimer else None,
        )

        execution_time = time.time() - start_time

        # Extract per-sample scores from API response
        sample_scores = _extract_scores_from_api_result(api_result)

        # Extract input_id from response (for metadata/reproducibility)
        input_id = None
        outputs_section = api_result.get('outputs', [])
        if isinstance(outputs_section, list) and outputs_section:
            first_output = outputs_section[0]
            if isinstance(first_output, dict):
                input_id = first_output.get('input_id') or api_result.get('request_id')

        # Extract ALL structure texts from API response (supports diffusion_samples > 1).
        all_structures = _extract_all_structures_from_api_result(api_result, output_format)

        # If nothing found, fail the job instead of saving raw JSON as a PDB
        if not all_structures:
            logger.error(
                f"Job {job_id}: Could not extract structure from OpenFold3 response. "
                f"Keys: {list(api_result.keys())}"
            )
            result['message'] = (
                "OpenFold3 API returned a response but no structure could be extracted. "
                "The model may have encountered an error processing this input."
            )
            result['execution_time'] = time.time() - start_time
            return result

        # First structure is the main output file
        structure_text, actual_format = all_structures[0]

        # Get output filename
        output_path = job.get('output_path', '')
        normalized_format = (actual_format or output_format or 'pdb').lower()
        ext = '.cif' if normalized_format in ('cif', 'mmcif') else '.pdb'
        output_filename = os.path.basename(output_path) if output_path else f"{job_id}{ext}"

        # Encode main output as base64
        structure_b64 = base64.b64encode(structure_text.encode('utf-8')).decode('utf-8')

        result.update({
            'status': 'success',
            'execution_time': execution_time,
            'output_file_content': structure_b64,
            'output_filename': output_filename,
            'output_format': actual_format,
            'message': None
        })

        # Get scores for display
        confidence_scores = sample_scores.get('confidence_scores', [])
        ptm_scores = sample_scores.get('ptm_scores', [])
        iptm_scores = sample_scores.get('iptm_scores', [])

        # If multiple structures returned (diffusion_samples > 1), add extras as artifacts
        if len(all_structures) > 1:
            artifacts = []
            base_name = os.path.splitext(output_filename)[0]
            for idx, (struct_text, struct_fmt) in enumerate(all_structures[1:], start=2):
                struct_ext = '.cif' if struct_fmt in ('cif', 'mmcif') else '.pdb'
                artifact_filename = f"{base_name}_sample{idx}{struct_ext}"
                # Get scores for this sample (shift by 1 since all_structures[0] is main output)
                sample_idx = idx - 1
                sample_conf = confidence_scores[sample_idx] if sample_idx < len(confidence_scores) else None
                sample_ptm = ptm_scores[sample_idx] if sample_idx < len(ptm_scores) else None
                sample_iptm = iptm_scores[sample_idx] if sample_idx < len(iptm_scores) else None
                display = f'Structure Sample {idx}'
                if sample_conf is not None:
                    display = f'Structure Sample {idx} (conf: {sample_conf:.3f})'
                artifacts.append({
                    'filename': artifact_filename,
                    'name': f'Sample {idx}',
                    'display_name': display,
                    'content_b64': base64.b64encode(struct_text.encode('utf-8')).decode('utf-8'),
                    'confidence_score': sample_conf,
                    'ptm_score': sample_ptm,
                    'iptm_score': sample_iptm,
                })
            result['artifacts'] = artifacts
            # Rename main output for clarity
            main_ext = '.cif' if actual_format in ('cif', 'mmcif') else '.pdb'
            result['output_filename'] = f"{base_name}_sample1{main_ext}"

        # Extract ligand SDF and protein-only content if ligand was used
        has_ligand_input = bool(ligand_smiles or ligand_ccd)
        if has_ligand_input:
            base_name = os.path.splitext(output_filename)[0]
            artifacts = result.get('artifacts')
            if artifacts is None:
                artifacts = []
                result['artifacts'] = artifacts

            # Extract from main structure
            try:
                ligand_sdf = _extract_ligand_sdf(structure_text, actual_format, ligand_id)
                if ligand_sdf:
                    sdf_b64 = base64.b64encode(ligand_sdf.encode('utf-8')).decode('utf-8')
                    artifacts.append({
                        'filename': f"{base_name}_ligand.sdf",
                        'content_b64': sdf_b64,
                        'name': 'ligand_sdf',
                        'display_name': 'Ligand (SDF)',
                        'type': 'ligand',
                    })
                    logger.info(f"Job {job_id}: Extracted ligand SDF from main structure")

                protein_only = _extract_protein_only(structure_text, actual_format)
                if protein_only:
                    protein_b64 = base64.b64encode(protein_only.encode('utf-8')).decode('utf-8')
                    artifacts.append({
                        'filename': f"{base_name}_protein.cif",
                        'content_b64': protein_b64,
                        'name': 'protein_only',
                        'display_name': 'Protein (CIF)',
                        'type': 'protein',
                    })
                    logger.info(f"Job {job_id}: Created protein-only CIF")
            except Exception as e:
                logger.warning(f"Job {job_id}: Could not extract ligand from main structure: {e}")

            # Also extract from each additional sample
            if len(all_structures) > 1:
                for idx, (struct_text, struct_fmt) in enumerate(all_structures[1:], start=2):
                    try:
                        sample_ligand_sdf = _extract_ligand_sdf(struct_text, struct_fmt, ligand_id)
                        if sample_ligand_sdf:
                            sdf_b64 = base64.b64encode(sample_ligand_sdf.encode('utf-8')).decode('utf-8')
                            artifacts.append({
                                'filename': f"{base_name}_ligand_sample{idx}.sdf",
                                'content_b64': sdf_b64,
                                'name': 'ligand_sdf',
                                'display_name': f'Ligand Sample {idx}',
                                'type': 'ligand',
                            })
                    except Exception as e:
                        logger.warning(f"Job {job_id}: Could not extract ligand from sample {idx}: {e}")

        # Add rich metadata to top-level result
        result.update({
            # Per-sample scores
            'num_samples': len(all_structures),
            'confidence_scores': confidence_scores,
            'ptm_scores': ptm_scores,
            'iptm_scores': iptm_scores,
            # Best/first sample scores
            'confidence_score': confidence_scores[0] if confidence_scores else None,
            'ptm_score': ptm_scores[0] if ptm_scores else None,
            'iptm_score': iptm_scores[0] if iptm_scores else None,
            # Input metadata
            'input_id': input_id,
            'diffusion_samples_requested': diffusion_samples,
            'molecule_type': molecule_type,
            'is_multimer': is_multimer,
            'output_format': actual_format,
        })

        logger.info(f"Job {job_id}: Prediction completed in {execution_time:.2f}s")
        if confidence_scores:
            logger.info(f"Job {job_id}: Confidence scores: {[f'{s:.3f}' for s in confidence_scores]}")

    except requests.exceptions.Timeout:
        result['message'] = "OpenFold3 API request timed out"
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

        while not stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                r.ping()

                heartbeat_data = {
                    'worker_id': worker_id,
                    'worker_type': WORKER_TYPE,
                    'timestamp': time.time()
                }

                if private_key and canonical_dumps:
                    payload_bytes = canonical_dumps(heartbeat_data).encode('utf-8')
                    signature = sign_message(private_key, payload_bytes)
                    if signature:
                        signed_message = {'payload': heartbeat_data, 'signature': signature}
                        r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(signed_message))
                else:
                    r.publish(WORKER_HEARTBEAT_CHANNEL, json.dumps(heartbeat_data))

            except Exception as e:
                logger.warning(f"Could not send heartbeat: {e}")

        logger.info("Heartbeat thread stopping.")
    except Exception as e:
        logger.critical(f"FATAL: Heartbeat thread error: {e}", exc_info=True)


def worker_loop(
    worker_id: str,
    private_key: Optional[str],
    organization_id: Optional[int],
    stop_event: threading.Event
):
    """Main worker loop - fetches and processes OpenFold3 jobs."""
    r = get_redis_connection(decode_responses=False)
    r_text = get_redis_connection(decode_responses=True)
    job_queue = get_job_queue_key(organization_id)

    logger.info(f"OpenFold3 worker started. Listening on queue: {job_queue}")

    while not stop_event.is_set():
        try:
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
            result = process_openfold3_job(job, worker_id)

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
    """Main entry point for OpenFold3 worker."""
    global NODE_CONFIG_FILE
    global logger

    parser = argparse.ArgumentParser(description='OpenFold3 Structure Prediction Worker')
    parser.add_argument('--worker-id', type=str, help='Worker ID')
    parser.add_argument('--org-id', type=int, help='Organization ID for private queue')
    parser.add_argument('--config', type=str, default=NODE_CONFIG_FILE, help='Config file path')
    args = parser.parse_args()

    NODE_CONFIG_FILE = args.config
    config = load_node_config()

    worker_id = args.worker_id or config.get('worker_id') or f'{WORKER_TYPE}-worker-1'
    organization_id = args.org_id or config.get('organization_id')
    private_key = _load_private_key(config.get('private_key'))

    # Setup logging
    _logger_instance = setup_logging(worker_id)
    logger = _logger_instance

    logger.info("=" * 60)
    logger.info("OpenFold3 Structure Prediction Worker Starting")
    logger.info(f"Worker ID: {worker_id}")
    logger.info(f"Organization ID: {organization_id or 'None (public)'}")
    logger.info(f"API URL: {NVIDIA_OPENFOLD3_URL}")
    logger.info("=" * 60)

    stop_event = threading.Event()

    heartbeat_thread = threading.Thread(
        target=send_heartbeat,
        args=(worker_id, private_key, stop_event),
        daemon=True
    )
    heartbeat_thread.start()

    try:
        worker_loop(worker_id, private_key, organization_id, stop_event)
    except KeyboardInterrupt:
        logger.info("Shutdown requested...")
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=5)
        logger.info("OpenFold3 worker shutdown complete.")


def test_openfold3_api():
    """Test the OpenFold3 API with a sample sequence."""
    print("Testing OpenFold3 API...")

    test_sequence = "GIVEQCCTSICSLYQLENYCN"  # Insulin chain A

    try:
        result = call_openfold3_api(
            sequence=test_sequence,
            molecule_type="protein",
            output_format="pdb"
        )

        print("Success!")
        print(f"Input sequence: {test_sequence}")

        if 'structure' in result:
            print(f"Structure output: {result['structure'][:200]}...")
        else:
            print(f"Result keys: {list(result.keys())}")

        output_file = Path("openfold3_test_output.json")
        output_file.write_text(json.dumps(result, indent=2))
        print(f"Full result saved to: {output_file}")

    except Exception as e:
        print(f"Error: {e}")
        return False

    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        test_openfold3_api()
    else:
        main()
