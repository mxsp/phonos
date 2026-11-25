"""API routes for MolMIM molecule generation (NVIDIA NIM)."""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
import requests
from flask import jsonify, request, session, render_template, Response

from ..app import app, get_redis_connection, logger
from ..helpers import get_db, login_required
from ..helpers.security import csrf_protect


# MolMIM API configuration
NVIDIA_MOLMIM_URL = os.getenv(
    "MOLMIM_API_URL",
    "https://health.api.nvidia.com/v1/biology/nvidia/molmim/generate"
)
NVIDIA_STATUS_URL = os.getenv(
    "MOLMIM_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
class _DynamicNvidiaKey:
    """Resolves NVIDIA_API_KEY dynamically (User BYOK -> Env -> Encrypted Instance DB)."""
    def _val(self) -> str:
        try:
            from flask import has_request_context, session
            from ..helpers.model_providers import resolve_nvidia_api_key
            uid = session.get("user_id") if has_request_context() else None
            val = resolve_nvidia_api_key(user_id=uid)
            if val:
                return val
        except Exception:
            pass
        return os.environ.get("NVIDIA_API_KEY", "") or ""

    def __bool__(self) -> bool:
        return bool(self._val())

    def __str__(self) -> str:
        return self._val()

    def __len__(self) -> int:
        return len(self._val())

    def strip(self, *args, **kwargs) -> str:
        return self._val().strip(*args, **kwargs)

NVIDIA_API_KEY = _DynamicNvidiaKey()

# Cost per generation (credits)
MOLMIM_CREDIT_COST = 20

# Valid algorithms per NVIDIA docs
VALID_ALGORITHMS = {'CMA-ES', 'SGA', 'Greedy', 'Random'}

# Valid properties per NVIDIA docs
VALID_PROPERTIES = {'QED', 'plogp'}


def _cloudflare_error(status_code: int = 502) -> tuple:
    """Generate a Cloudflare-style error response to mask upstream errors."""
    ray_id = ''.join(random.choices('0123456789abcdef', k=16))

    error_messages = {
        502: {'code': 502, 'title': 'Bad Gateway', 'message': 'The web server reported a bad gateway error.', 'ray_id': ray_id},
        503: {'code': 503, 'title': 'Service Temporarily Unavailable', 'message': 'The server is temporarily unable to service your request.', 'ray_id': ray_id},
        504: {'code': 504, 'title': 'Gateway Timeout', 'message': 'The connection timed out.', 'ray_id': ray_id},
        520: {'code': 520, 'title': 'Web Server Returned an Unknown Error', 'message': 'The origin web server returned an unexpected response.', 'ray_id': ray_id},
        521: {'code': 521, 'title': 'Web Server Is Down', 'message': 'The origin web server is refusing connections.', 'ray_id': ray_id},
        522: {'code': 522, 'title': 'Connection Timed Out', 'message': 'The initial connection timed out.', 'ray_id': ray_id},
        524: {'code': 524, 'title': 'A Timeout Occurred', 'message': 'The origin server took too long to respond.', 'ray_id': ray_id},
    }

    error_info = error_messages.get(status_code, error_messages[502])
    return jsonify({
        'error': True,
        'cloudflare': True,
        'code': error_info['code'],
        'title': error_info['title'],
        'message': error_info['message'],
        'ray_id': error_info['ray_id']
    }), error_info['code']


# ============================================================================
# PAGE ROUTE
# ============================================================================

@app.route('/molmim')
@login_required
def molmim_page():
    """Render the MolMIM molecule generation page."""
    db = get_db()
    user_id = session.get('user_id')
    user = db.execute('SELECT credits FROM users WHERE id = %s', (user_id,)).fetchone()
    return render_template('molmim.html', user_credits=user['credits'] if user else 0)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/molmim/generate', methods=['POST'])
@login_required
@csrf_protect
def molmim_generate():
    """
    Generate novel molecules using MolMIM (NVIDIA NIM).

    Request body (exact NVIDIA API parameters):
        smi: Input SMILES molecule string (required)
        num_molecules: Number of molecules to generate (1-100, default 10)
        algorithm: Generation algorithm - CMA-ES, SGA, Greedy, or Random (default CMA-ES)
        property_name: Property to optimize - QED or plogp (default QED)
        minimize: Minimize the property instead of maximizing (boolean, default false)
        min_similarity: Min Tanimoto similarity to input (0.0-1.0, default 0.3)
        iterations: Optimization iterations (1-100, default 10)
        particles: Particles for CMA-ES (1-200, default 30)
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': True, 'message': 'No data provided'}), 400

        # Validate SMILES input
        smi = data.get('smi', '').strip()
        if not smi:
            return jsonify({'error': True, 'message': 'No SMILES string provided (smi is required)'}), 400

        if len(smi) > 2000:
            return jsonify({'error': True, 'message': 'SMILES string too long (max 2000 characters)'}), 400

        # Get user and check credits
        db = get_db()
        user = db.execute(
            'SELECT id, credits FROM users WHERE id = %s', (session['user_id'],)
        ).fetchone()

        if not user:
            return jsonify({'error': True, 'message': 'User not found'}), 404

        if user['credits'] < MOLMIM_CREDIT_COST:
            return jsonify({
                'error': True,
                'message': f'Insufficient credits. Need {MOLMIM_CREDIT_COST}, have {user["credits"]}'
            }), 402

        # Validate and extract parameters
        num_molecules = min(max(1, int(data.get('num_molecules', 10))), 100)

        algorithm = data.get('algorithm', 'CMA-ES')
        if algorithm not in VALID_ALGORITHMS:
            return jsonify({'error': True, 'message': f'Invalid algorithm. Must be one of: {", ".join(sorted(VALID_ALGORITHMS))}'}), 400

        property_name = data.get('property_name', 'QED')
        if property_name not in VALID_PROPERTIES:
            return jsonify({'error': True, 'message': f'Invalid property_name. Must be one of: {", ".join(sorted(VALID_PROPERTIES))}'}), 400

        minimize = bool(data.get('minimize', False))

        try:
            min_similarity = float(data.get('min_similarity', 0.3))
            min_similarity = max(0.0, min(min_similarity, 1.0))
        except (TypeError, ValueError):
            min_similarity = 0.3

        iterations = min(max(1, int(data.get('iterations', 10))), 100)
        particles = min(max(1, int(data.get('particles', 30))), 200)

        # Prepare API request
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "NVCF-POLL-SECONDS": "30",
        }

        payload = {
            "smi": smi,
            "num_molecules": num_molecules,
            "algorithm": algorithm,
            "property_name": property_name,
            "minimize": minimize,
            "min_similarity": min_similarity,
            "iterations": iterations,
            "particles": particles,
        }

        logger.info(
            f"MolMIM generation request: user={session['user_id']}, "
            f"smi_len={len(smi)}, num_molecules={num_molecules}, "
            f"algorithm={algorithm}, property={property_name}"
        )

        # Call MolMIM API (60s connect, 120s read)
        response = requests.post(
            url=NVIDIA_MOLMIM_URL,
            headers=headers,
            json=payload,
            timeout=(60, 120)
        )

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = response.json()
            else:
                logger.error(f"MolMIM unexpected content type: {content_type}")
                return _cloudflare_error(520)

            # Deduct credits on success
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (MOLMIM_CREDIT_COST, session['user_id'])
            )
            db.commit()

            num_generated = len(result.get('molecules', []))
            logger.info(f"MolMIM generation completed (sync): user={session['user_id']}, generated={num_generated}")
            return jsonify(result)

        elif response.status_code == 202:
            # Async task
            nvcf_reqid = response.headers.get("NVCF-REQID")
            if not nvcf_reqid:
                logger.error("MolMIM 202 response missing NVCF-REQID")
                return _cloudflare_error(520)

            logger.info(f"MolMIM async task started: {nvcf_reqid}, user={session['user_id']}")

            try:
                r = get_redis_connection()
                task_meta = json.dumps({
                    'user_id': session['user_id'],
                    'credit_cost': MOLMIM_CREDIT_COST,
                    'started_at': time.time(),
                    'num_molecules': num_molecules,
                    'algorithm': algorithm,
                })
                r.setex(f"molmim:task:{nvcf_reqid}", 28800, task_meta)
            except Exception as redis_err:
                logger.warning(f"MolMIM Redis store failed: {redis_err}")

            return jsonify({'status': 'pending', 'task_id': nvcf_reqid})

        else:
            logger.error(f"MolMIM API error: {response.status_code} - {response.text[:500]}")
            if response.status_code == 429:
                return _cloudflare_error(503)
            elif response.status_code >= 500:
                return _cloudflare_error(502)
            elif response.status_code in (401, 403):
                return _cloudflare_error(521)
            else:
                return _cloudflare_error(520)

    except requests.exceptions.Timeout:
        logger.error("MolMIM API timeout")
        return _cloudflare_error(524)
    except requests.exceptions.ConnectionError:
        logger.error("MolMIM connection error")
        return _cloudflare_error(522)
    except requests.exceptions.RequestException as e:
        logger.error(f"MolMIM network error: {e}")
        return _cloudflare_error(502)
    except Exception as e:
        logger.error(f"MolMIM generation error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/molmim/status/<task_id>', methods=['GET'])
@login_required
def molmim_status(task_id):
    """Check status of an async MolMIM generation task."""
    try:
        if not task_id or len(task_id) > 128 or not task_id.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': True, 'message': 'Invalid task ID'}), 400

        task_meta = None
        try:
            r = get_redis_connection()
            raw = r.get(f"molmim:task:{task_id}")
            if raw:
                task_meta = json.loads(raw)
        except Exception as redis_err:
            logger.warning(f"MolMIM Redis lookup failed: {redis_err}")

        if not task_meta:
            return jsonify({'error': True, 'message': 'Task not found or expired'}), 404

        if task_meta.get('user_id') != session['user_id']:
            return jsonify({'error': True, 'message': 'Access denied'}), 403

        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json"
        }
        status_url = NVIDIA_STATUS_URL.format(task_id=task_id)

        try:
            status_response = requests.get(status_url, headers=headers, timeout=30)
        except requests.exceptions.Timeout:
            return jsonify({'status': 'pending', 'message': 'Status check timed out, still processing'})
        except requests.exceptions.RequestException as e:
            logger.error(f"MolMIM status check error: {e}")
            return jsonify({'status': 'pending', 'message': 'Status check failed, still processing'})

        if status_response.status_code == 202:
            elapsed = int(time.time() - task_meta.get('started_at', time.time()))
            return jsonify({'status': 'pending', 'elapsed': elapsed})

        elif status_response.status_code == 200:
            content_type = status_response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                result = status_response.json()
            else:
                result = {"raw_content": status_response.text[:5000]}

            db = get_db()
            db.execute(
                'UPDATE users SET credits = credits - %s WHERE id = %s',
                (task_meta['credit_cost'], session['user_id'])
            )
            db.commit()

            try:
                r = get_redis_connection()
                r.delete(f"molmim:task:{task_id}")
            except Exception:
                pass

            logger.info(f"MolMIM async task completed: {task_id}, user={session['user_id']}")
            return jsonify(result)

        elif status_response.status_code == 404:
            try:
                r = get_redis_connection()
                r.delete(f"molmim:task:{task_id}")
            except Exception:
                pass
            return jsonify({'error': True, 'message': 'Task expired or not found'}), 404

        else:
            logger.error(f"MolMIM status error: {status_response.status_code}")
            return _cloudflare_error(502)

    except Exception as e:
        logger.error(f"MolMIM status check error: {e}", exc_info=True)
        return _cloudflare_error(520)


@app.route('/api/molmim/info', methods=['GET'])
@login_required
def molmim_info():
    """Get MolMIM model information and user's available credits."""
    try:
        db = get_db()
        user = db.execute('SELECT credits FROM users WHERE id = %s', (session['user_id'],)).fetchone()

        return jsonify({
            'model': 'MolMIM',
            'provider': 'BioNeMo',
            'description': 'Generate novel drug-like molecules from input SMILES using CMA-ES optimization',
            'credit_cost': MOLMIM_CREDIT_COST,
            'user_credits': user['credits'] if user else 0,
            'parameters': {
                'smi': {'type': 'string', 'required': True, 'max_length': 2000, 'description': 'Input SMILES molecule'},
                'num_molecules': {'type': 'integer', 'min': 1, 'max': 100, 'default': 10},
                'algorithm': {'type': 'string', 'options': sorted(VALID_ALGORITHMS), 'default': 'CMA-ES'},
                'property_name': {'type': 'string', 'options': sorted(VALID_PROPERTIES), 'default': 'QED'},
                'minimize': {'type': 'boolean', 'default': False},
                'min_similarity': {'type': 'number', 'min': 0.0, 'max': 1.0, 'default': 0.3},
                'iterations': {'type': 'integer', 'min': 1, 'max': 100, 'default': 10},
                'particles': {'type': 'integer', 'min': 1, 'max': 200, 'default': 30},
            },
            'capabilities': [
                'Molecule generation via latent space optimization',
                'Multiple optimization algorithms (CMA-ES, SGA, Greedy, Random)',
                'QED and pLogP property optimization',
                'Tanimoto similarity constraint',
            ],
        })
    except Exception as e:
        logger.error(f"MolMIM info error: {e}")
        return _cloudflare_error(520)


# ============================================================================
# MOLECULE CACHE API (database-backed image/SDF serving)
# ============================================================================

def _smiles_hash(smiles: str) -> str:
    """SHA-256 hash of normalized SMILES for cache key."""
    return hashlib.sha256(smiles.strip().encode('utf-8')).hexdigest()


def _render_molecule_png_rdkit(smiles: str, width: int = 600, height: int = 450):
    """
    Render a 2D molecule PNG locally with RDKit (no network dependency).

    Used as a fallback when PubChem has no matching record — e.g. for
    novel molecules generated by MolMIM/GenMol that don't exist in PubChem.

    Returns PNG bytes, or None if the SMILES cannot be parsed by RDKit.
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from rdkit.Chem.Draw import rdMolDraw2D

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None

    try:
        AllChem.Compute2DCoords(mol)
    except Exception:
        pass

    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


@app.route('/api/molmim/molecule/image', methods=['GET'])
@login_required
def molmim_molecule_image():
    """Serve a 2D molecule PNG from DB cache, fetching from PubChem on miss,
    falling back to a local RDKit rendering if PubChem has no match (common
    for novel MolMIM/GenMol-generated molecules)."""
    smiles = request.args.get('smiles', '').strip()
    if not smiles or len(smiles) > 2000:
        return jsonify({'error': True, 'message': 'Invalid SMILES'}), 400

    h = _smiles_hash(smiles)
    db = get_db()

    # Check cache
    row = db.execute(
        'SELECT image_png FROM molecule_cache WHERE smiles_hash = %s',
        (h,)
    ).fetchone()

    if row and row['image_png']:
        # Update last_accessed
        db.execute(
            'UPDATE molecule_cache SET last_accessed = NOW() WHERE smiles_hash = %s',
            (h,)
        )
        db.commit()
        png_data = bytes(row['image_png'])
        return Response(png_data, mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=86400'})

    # Cache miss — try PubChem first
    png_data = None
    encoded = requests.utils.quote(smiles, safe='')
    pubchem_url = (
        'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/'
        + encoded
        + '/PNG?image_size=600x450&record_type=2d'
    )
    try:
        resp = requests.get(pubchem_url, timeout=15)
        if resp.status_code == 200 and 'image/png' in resp.headers.get('Content-Type', ''):
            png_data = resp.content
    except Exception as e:
        logger.warning(f"MolMIM PubChem image fetch failed for SMILES, falling back to RDKit: {e}")

    # PubChem had no match (or the request itself failed) — render locally
    # with RDKit instead of surfacing a broken image for what is often the
    # most interesting output the feature produces (novel molecules).
    if png_data is None:
        try:
            png_data = _render_molecule_png_rdkit(smiles)
        except Exception as e:
            logger.error(f"RDKit local rendering failed for SMILES: {e}")
            png_data = None

        if png_data is None:
            return jsonify({'error': True, 'message': 'Could not render molecule image: invalid SMILES or no PubChem match'}), 404

    # Store in cache (upsert) — same convention regardless of PNG source
    try:
        db.execute(
            '''INSERT INTO molecule_cache (smiles_hash, smiles, image_png, created_at, last_accessed)
               VALUES (%s, %s, %s, NOW(), NOW())
               ON CONFLICT (smiles_hash) DO UPDATE SET image_png = EXCLUDED.image_png, last_accessed = NOW()''',
            (h, smiles, png_data)
        )
        db.commit()
    except Exception as e:
        logger.warning(f"MolMIM image cache store failed: {e}")

    return Response(png_data, mimetype='image/png',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/api/molmim/molecule/sdf', methods=['GET'])
@login_required
def molmim_molecule_sdf():
    """Serve SDF data from DB cache, fetching from PubChem on miss."""
    smiles = request.args.get('smiles', '').strip()
    if not smiles or len(smiles) > 2000:
        return jsonify({'error': True, 'message': 'Invalid SMILES'}), 400

    h = _smiles_hash(smiles)
    db = get_db()

    # Check cache
    row = db.execute(
        'SELECT sdf_data FROM molecule_cache WHERE smiles_hash = %s',
        (h,)
    ).fetchone()

    if row and row['sdf_data']:
        db.execute(
            'UPDATE molecule_cache SET last_accessed = NOW() WHERE smiles_hash = %s',
            (h,)
        )
        db.commit()
        return Response(row['sdf_data'], mimetype='chemical/x-mdl-sdfile',
                        headers={'Cache-Control': 'public, max-age=86400'})

    # Cache miss — try 3D then 2D from PubChem
    encoded = requests.utils.quote(smiles, safe='')
    urls = [
        'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/' + encoded + '/record/SDF?record_type=3d',
        'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/' + encoded + '/record/SDF?record_type=2d',
    ]
    sdf_data = None
    for url in urls:
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and 'M  END' in resp.text:
                sdf_data = resp.text
                break
        except Exception:
            continue

    if not sdf_data:
        return Response('', status=404)

    # Store in cache (upsert)
    try:
        db.execute(
            '''INSERT INTO molecule_cache (smiles_hash, smiles, sdf_data, created_at, last_accessed)
               VALUES (%s, %s, %s, NOW(), NOW())
               ON CONFLICT (smiles_hash) DO UPDATE SET sdf_data = EXCLUDED.sdf_data, last_accessed = NOW()''',
            (h, smiles, sdf_data)
        )
        db.commit()
    except Exception as e:
        logger.warning(f"MolMIM SDF cache store failed: {e}")

    return Response(sdf_data, mimetype='chemical/x-mdl-sdfile',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/api/molmim/molecule/properties', methods=['GET'])
@login_required
def molmim_molecule_properties():
    """Molecule properties from PubChem, or computed with RDKit for novel molecules; DB-cached."""
    smiles = request.args.get('smiles', '').strip()
    if not smiles or len(smiles) > 2000:
        return jsonify({'error': True, 'message': 'Invalid SMILES'}), 400

    h = _smiles_hash(smiles)
    db = get_db()

    # Check cache
    row = db.execute(
        'SELECT properties_json FROM molecule_cache WHERE smiles_hash = %s',
        (h,)
    ).fetchone()

    if row and row.get('properties_json'):
        db.execute(
            'UPDATE molecule_cache SET last_accessed = NOW() WHERE smiles_hash = %s',
            (h,)
        )
        db.commit()
        try:
            cached = json.loads(row['properties_json'])
            # Older entries can be empty PubChem stubs; recompute those.
            if cached.get('molecular_formula'):
                return jsonify(cached)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    # PubChem first (it has IUPAC names and complexity); generated molecules are
    # usually not in PubChem, so fall back to computing the descriptors locally.
    result = _pubchem_properties(smiles) or _rdkit_properties(smiles)
    if not result:
        return jsonify({'error': True, 'message': 'Properties not available for this molecule'}), 404

    try:
        db.execute(
            '''INSERT INTO molecule_cache (smiles_hash, smiles, properties_json, created_at, last_accessed)
               VALUES (%s, %s, %s, NOW(), NOW())
               ON CONFLICT (smiles_hash) DO UPDATE SET properties_json = EXCLUDED.properties_json, last_accessed = NOW()''',
            (h, smiles, json.dumps(result))
        )
        db.commit()
    except Exception as e:
        logger.warning(f"MolMIM properties cache store failed: {e}")

    return jsonify(result)


def _pubchem_properties(smiles: str) -> dict | None:
    """Look the molecule up on PubChem. None when it is unknown or PubChem is unreachable."""
    encoded = requests.utils.quote(smiles, safe='')
    props_url = (
        'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/'
        + encoded
        + '/property/MolecularFormula,MolecularWeight,XLogP,ExactMass,'
        + 'TPSA,Complexity,HBondDonorCount,HBondAcceptorCount,'
        + 'RotatableBondCount,HeavyAtomCount,IUPACName/JSON'
    )
    try:
        resp = requests.get(props_url, timeout=15)
        if resp.status_code != 200:
            return None
        props_list = resp.json().get('PropertyTable', {}).get('Properties', [])
    except Exception as e:
        logger.warning(f"MolMIM properties PubChem lookup failed: {e}")
        return None
    # Unknown molecules come back as a stub record (CID 0, no properties).
    raw = props_list[0] if props_list else {}
    if not raw.get('MolecularFormula'):
        return None

    return {
        'molecular_formula': raw.get('MolecularFormula'),
        'molecular_weight': raw.get('MolecularWeight'),
        'xlogp': raw.get('XLogP'),
        'exact_mass': raw.get('ExactMass'),
        'tpsa': raw.get('TPSA'),
        'complexity': raw.get('Complexity'),
        'h_bond_donors': raw.get('HBondDonorCount'),
        'h_bond_acceptors': raw.get('HBondAcceptorCount'),
        'rotatable_bonds': raw.get('RotatableBondCount'),
        'heavy_atom_count': raw.get('HeavyAtomCount'),
        'iupac_name': raw.get('IUPACName'),
        'source': 'pubchem',
    }


def _rdkit_properties(smiles: str) -> dict | None:
    """Compute the same descriptors with RDKit. None when the SMILES does not parse."""
    try:
        from rdkit import Chem
        from rdkit.Chem import Crippen, Descriptors, Lipinski, rdMolDescriptors
    except ImportError:
        return None
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    return {
        'molecular_formula': rdMolDescriptors.CalcMolFormula(mol),
        'molecular_weight': round(Descriptors.MolWt(mol), 2),
        'xlogp': round(Crippen.MolLogP(mol), 2),  # Crippen logP, close to but not identical with PubChem XLogP3
        'exact_mass': round(Descriptors.ExactMolWt(mol), 4),
        'tpsa': round(rdMolDescriptors.CalcTPSA(mol), 2),
        'complexity': None,  # PubChem-specific (Bertz/Hendrickson), no RDKit equivalent
        'h_bond_donors': Lipinski.NumHDonors(mol),
        'h_bond_acceptors': Lipinski.NumHAcceptors(mol),
        'rotatable_bonds': rdMolDescriptors.CalcNumRotatableBonds(mol),
        'heavy_atom_count': mol.GetNumHeavyAtoms(),
        'iupac_name': None,
        'source': 'rdkit',
    }
