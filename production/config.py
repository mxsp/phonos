# File: config.py

import os
import secrets as _secrets
from dotenv import load_dotenv

try:
    from .host_resolution import resolve_host
except ImportError:
    from host_resolution import resolve_host

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------------------------------------------
# Portable path resolution
#
# Every filesystem location the app uses is derived from PROJECT_ROOT (the
# repository root, i.e. the parent of this ``production`` package) and can be
# overridden with an environment variable. This keeps the app portable across
# machines, usernames, containers and Kubernetes pods where the home directory,
# the deploy path and the mounted data disks all differ.
#
#   PHONOS_DIR              -> repository root (defaults to the detected root)
#   JOB_OUTPUT_DIR          -> primary job output storage
#   JOB_OUTPUT_DIR_FALLBACK -> secondary job output storage
#   BACKUP_DIRS             -> os.pathsep / comma separated list of backup dirs
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.environ.get(
    'PHONOS_DIR',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)


def _env_path(var_name, *default_parts):
    """Return an absolute path from ``var_name`` or a PROJECT_ROOT-relative default."""
    value = os.environ.get(var_name)
    if value:
        return os.path.abspath(os.path.expanduser(value))
    return os.path.join(PROJECT_ROOT, *default_parts)


def _env_path_list(var_name, *defaults):
    """Parse a list of paths from an env var (``os.pathsep`` or comma separated)."""
    raw = os.environ.get(var_name)
    if raw:
        parts = [p for chunk in raw.split(os.pathsep) for p in chunk.split(',')]
        return [os.path.abspath(os.path.expanduser(p.strip())) for p in parts if p.strip()]
    return [os.path.join(PROJECT_ROOT, *d) if isinstance(d, tuple) else d for d in defaults]


def _load_or_create_local_secret(filename: str, min_len: int = 32) -> str:
    base = os.environ.get("PHONOS_DATA_DIR") or os.path.join(PROJECT_ROOT, ".data")
    try:
        os.makedirs(base, exist_ok=True)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        key_path = os.path.join(base, filename)
        if os.path.exists(key_path):
            val = open(key_path, "r", encoding="utf-8").read().strip()
            if len(val) >= min_len:
                return val
        val = _secrets.token_urlsafe(48)
        tmp_path = key_path + ".tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(val)
        os.replace(tmp_path, key_path)
        return val
    except Exception:
        return _secrets.token_urlsafe(48)


class Config:
    """Application configuration settings."""
    # Secret key for Flask session management (env wins; auto-persisted in .data/ for zero-friction local startup)
    SECRET_KEY = os.environ.get('SECRET_KEY') or _load_or_create_local_secret('secret.key', 32)

    # Development mode - SECURE DEFAULT: False unless explicitly enabled
    DEV_MODE = os.environ.get('DEV', 'false').lower() == 'true'

    # 2FA enforcement - disabled by default for open-source/local deployments (dormant code preserved)
    ENABLE_2FA = os.environ.get('ENABLE_2FA', 'false').lower() == 'true'

    # Self-hosted Local Lab Mode - unlocks role-based features without Stripe paywalls
    LOCAL_LAB_MODE = os.environ.get('LOCAL_LAB_MODE', 'true').lower() == 'true'

    # Public self-registration toggle (Admin can always create accounts via Account Manager)
    ALLOW_PUBLIC_SIGNUP = os.environ.get('ALLOW_PUBLIC_SIGNUP', 'true').lower() == 'true'

    # Localnet-only guard (RFC1918 + loopback + ULA when enabled)
    LOCALNET_ONLY = os.environ.get('PHONOS_LOCALNET_ONLY', '0').lower() in ('1', 'true', 'yes')

    # Beta mode - restrict registration to @hs-anhalt.de emails (bypassed in dev mode)
    BETA_MODE = os.environ.get('BETA', 'false').lower() == 'true'

    # Security Headers
    SECURE_HEADERS = {
        # X-Frame-Options is intentionally NOT set globally because the
        # Notebook IDE embeds interactive Wolfram Cloud notebooks via iframe
        # (https://www.wolframcloud.com/obj/...). X-Frame-Options has no
        # per-origin whitelist, so it would block those iframes. The CSP
        # ``frame-src`` directive below is the modern, origin-scoped
        # equivalent and whitelists only the Wolfram Cloud hosts.
        'X-Content-Type-Options': 'nosniff',
        'X-XSS-Protection': '0',
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        # CSP tuned to what the app actually loads: CDN libraries (pdbe-molstar/Mol*,
        # Chart.js, Prism, socket.io), the Mol* viewer's blob: web workers + WASM,
        # pervasive inline styles from innerHTML rendering, and the external
        # structure/compound endpoints the 3D viewer fetches from.
        # frame-src whitelists the Wolfram Cloud hosts used by the embedded
        # notebook view (Notebook IDE → "Open as embedded Wolfram notebook").
        'Content-Security-Policy': (
            "default-src 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' "
            "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://cdn.socket.io https://unpkg.com "
            "https://www.googletagmanager.com https://accounts.google.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "img-src 'self' data: blob: https:; "
            "font-src 'self' data: https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "worker-src 'self' blob:; "
            "child-src 'self' blob:; "
            "frame-src 'self' https://www.wolframcloud.com https://wolframcloud.com "
            "https://cloud.wolfram.com; "
            "connect-src 'self' blob: data: ws: wss: "
            "https://cdn.jsdelivr.net https://alphafold.ebi.ac.uk "
            "https://pubchem.ncbi.nlm.nih.gov https://eutils.ncbi.nlm.nih.gov "
            "https://files.rcsb.org "
            "https://data.rcsb.org https://www.rcsb.org "
            "https://accounts.google.com "
            "https://www.googletagmanager.com https://www.google-analytics.com "
            "https://region1.google-analytics.com;"
        ),
    }

    # TLS/SSL Configuration
    REQUIRE_TLS = os.environ.get('REQUIRE_TLS', 'false' if LOCAL_LAB_MODE else 'true').lower() == 'true'
    TLS_VERSION = os.environ.get('TLS_VERSION', 'TLSv1.2')

    # Session Security (allow plain HTTP on localnet / DEV_MODE unless SESSION_COOKIE_SECURE=true or REQUIRE_TLS=true)
    SESSION_COOKIE_SECURE = os.environ.get(
        'SESSION_COOKIE_SECURE',
        'true' if (REQUIRE_TLS and not DEV_MODE) else 'false',
    ).lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour

    # Database configuration
    DATABASE_FILE = 'phonos.db'
    # If DATABASE_URL is not set, default to sqlite:///phonos.db
    # This ensures easy deployment and fallback to SQLite.
    DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{DATABASE_FILE}')

    # Redis Configuration
    REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
    REDIS_DB = int(os.environ.get('REDIS_DB', 0))
    REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD')  # Optional password
    REDIS_SSL = os.environ.get('REDIS_SSL', 'false').lower() == 'true'
    REDIS_SSL_CERT_REQS = 'required' if REDIS_SSL else None

    # Encryption key for manager-worker communication & data at rest
    ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY') or _load_or_create_local_secret('encryption.key', 32)
    if len(ENCRYPTION_KEY) < 32:
        raise ValueError("ENCRYPTION_KEY must be at least 32 characters for security.")
    if not os.environ.get('ENCRYPTION_KEY'):
        os.environ['ENCRYPTION_KEY'] = ENCRYPTION_KEY

    # Rate Limiting
    RATE_LIMIT_ENABLED = os.environ.get('RATE_LIMIT_ENABLED', 'true').lower() == 'true'
    RATE_LIMIT_DEFAULT = os.environ.get('RATE_LIMIT_DEFAULT', '100/hour')
    RATE_LIMIT_STORAGE_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

    # File Upload Security
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 100 * 1024 * 1024))  # 100MB default
    UPLOAD_SCAN_ENABLED = os.environ.get('UPLOAD_SCAN_ENABLED', 'true').lower() == 'true'

    # User storage quota for the Standard plan (1 GB)
    USER_STORAGE_QUOTA = 1 * 1024 * 1024 * 1024
    
    # Upload folder for profile pictures
    # Use an absolute path so Flask can always resolve files regardless of the
    # current working directory the app was launched from.
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'user_uploads')

    # Repository root, used to derive portable, machine-independent defaults.
    PROJECT_ROOT = PROJECT_ROOT

    # Base directory for all user-generated job outputs.
    # Defaults to a PROJECT_ROOT-relative directory so the app is portable; set
    # JOB_OUTPUT_DIR (e.g. to a mounted data disk like /mnt/storage/...) in
    # production.
    JOB_OUTPUT_DIR = _env_path('JOB_OUTPUT_DIR', 'job_outputs')

    # Fallback directory for job outputs (used when the primary disk is unavailable).
    JOB_OUTPUT_DIR_FALLBACK = _env_path('JOB_OUTPUT_DIR_FALLBACK', 'job_outputs_fallback')

    # Backup directories for database and file backups.
    BACKUP_DIRS = _env_path_list('BACKUP_DIRS', ('backups',))

    # Extra absolute prefixes under which job outputs may have been stored by
    # previous/other deployments. Used only so already-stored DB paths still
    # resolve; safe to leave empty on a fresh install. Set
    # JOB_OUTPUT_LEGACY_PREFIXES (os.pathsep / comma separated) when migrating
    # data from a machine with a different layout.
    JOB_OUTPUT_LEGACY_PREFIXES = _env_path_list('JOB_OUTPUT_LEGACY_PREFIXES')

    # Optional external ESMFold runner configuration
    ESMFOLD_LAUNCH_SCRIPT = os.environ.get('ESMFOLD_LAUNCH_SCRIPT')
    ESMFOLD_MODEL_NAME = os.environ.get('ESMFOLD_MODEL_NAME', 'esm2_t36_3B_UR50D')
    ESMFOLD_TIMEOUT = int(os.environ.get('ESMFOLD_TIMEOUT', 1800))

    # Models hidden from user-facing model pickers (kept for compatibility paths)
    UI_HIDDEN_MODEL_IDS = {'type-5', 'type-6', 'boltz2-enhanced'}

    # Models shown in UI but greyed out / disabled (visible but not runnable)
    UI_GREYED_MODEL_IDS = set()

    # List of models the platform supports
    SUPPORTED_MODELS = [
        {
            'id': 'type-0',
            'node_id': 'node-1',
            'name': 'ESMFold (Protein Structure)',
            'short_name': 'ESMFold',
            'description': 'Predict 3D structures directly from amino-acid sequences using facebook/esmfold_v1.',
            'task': 'protein_folding',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.pdb',
            'available': True,
            'is_active': False,
            'retired': True,
            'offline_reason': 'Retired: NVIDIA ended the ESMFold endpoint on 2026-08-25. Use OpenFold3 or Boltz2.',
            'replacement': 'openfold3',
            'outline_color': 'blue',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'num_recycles',
                    'label': 'Number of Recycles',
                    'type': 'range',
                    'required': False,
                    'default': 4,
                    'min': 0,
                    'max': 20,
                    'step': 1,
                    'help': 'Recycling iterations for structure refinement (more = better quality but slower)',
                },
                # chunk_size is now automatically set to balanced (128)
                # Removed from UI - system automatically optimizes chunking
                {
                    'name': 'return_representations',
                    'label': 'Return Embeddings',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Include intermediate layer embeddings in the output (for advanced analysis)',
                },
            ],
        },
        {
            'id': 'type-3',
            'node_id': 'node-diffdock',
            'name': 'DiffDock (Molecular Docking)',
            'short_name': 'DiffDock',
            'description': 'Predict protein-ligand binding poses using DiffDock. Supports protein PDB files and ligand SMILES/SDF inputs.',
            'task': 'molecular_docking',
            'input_modes': ['file'],
            'output_extension': '.pdb',
            'available': True,
            'is_active': False,
            'retired': True,
            'offline_reason': 'Retired: the self-hosted DiffDock node is superseded by DiffDock-RE.',
            'replacement': 'diffdock-re',
            'outline_color': 'purple',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'protein_input_type',
                    'label': 'Protein Input Type',
                    'type': 'select',
                    'required': True,
                    'default': 'file',
                    'options': [
                        {'value': 'file', 'label': 'From Project Files'},
                        {'value': 'pdb', 'label': 'PDB ID'},
                        {'value': 'uniprot', 'label': 'UniProt ID'},
                    ],
                    'help': 'Choose how to provide the protein structure',
                },
                {
                    'name': 'protein_file',
                    'label': 'Protein Structure File (PDB)',
                    'type': 'select',
                    'required': False,
                    'options': [],
                    'help': 'Select a protein structure file (PDB format) from your project files',
                },
                {
                    'name': 'protein_pdb_id',
                    'label': 'PDB ID',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., 1XYZ',
                    'help': 'Enter a PDB ID to fetch the protein structure from RCSB PDB',
                },
                {
                    'name': 'protein_uniprot_id',
                    'label': 'UniProt ID',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., P12345',
                    'help': 'Enter a UniProt ID (structure will be predicted if not available)',
                },
                {
                    'name': 'ligand_input_type',
                    'label': 'Ligand Input Type',
                    'type': 'select',
                    'required': True,
                    'default': 'smiles',
                    'options': [
                        {'value': 'smiles', 'label': 'SMILES String'},
                        {'value': 'file', 'label': 'From Project Files'},
                    ],
                    'help': 'Choose how to provide the ligand structure',
                },
                {
                    'name': 'ligand',
                    'label': 'Ligand SMILES String',
                    'type': 'textarea',
                    'required': False,
                    'placeholder': 'e.g., CC(=O)OC1=CC=CC=C1C(=O)O (Aspirin)\nCN1C=NC2=C1C(=O)N(C(=O)N2C)C (Caffeine)',
                    'help': 'Enter SMILES string for the ligand molecule. Common examples: Aspirin, Caffeine, Ibuprofen',
                },
                {
                    'name': 'ligand_file',
                    'label': 'Ligand File (SDF/MOL/MOL2)',
                    'type': 'select',
                    'required': False,
                    'options': [],
                    'help': 'Select a ligand structure file from your project files',
                },
                {
                    'name': 'samples_per_complex',
                    'label': 'Samples Per Complex',
                    'type': 'range',
                    'required': False,
                    'default': 10,
                    'min': 1,
                    'max': 40,
                    'step': 1,
                    'help': 'Number of binding poses to generate (more = slower but more thorough)',
                },
                {
                    'name': 'inference_steps',
                    'label': 'Inference Steps',
                    'type': 'range',
                    'required': False,
                    'default': 20,
                    'min': 10,
                    'max': 50,
                    'step': 5,
                    'help': 'Diffusion steps for pose generation (more = higher quality but slower)',
                },
                {
                    'name': 'actual_steps',
                    'label': 'Actual Diffusion Steps',
                    'type': 'number',
                    'required': False,
                    'default': 18,
                    'min': 10,
                    'max': 50,
                    'step': 1,
                    'help': 'Fine-tune the actual number of diffusion steps (advanced)',
                },
                {
                    'name': 'no_final_step_noise',
                    'label': 'Disable Final Step Noise',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Skip noise in the final diffusion step for cleaner outputs',
                },
            ],
        },
        {
            'id': 'type-8',
            'node_id': 'node-evo2',
            'name': 'Evo2 (DNA/RNA Sequence Generation)',
            'short_name': 'Evo2',
            'description': 'Generate and optimize DNA/RNA sequences using Evo2, a state-of-the-art sequence generation model. Evo2 is experiencing an outage.',
            'task': 'dna_sequence_generation',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.json',
            'available': False,
            'is_active': False,
            'alias_of': 'evo2',  # duplicate of the canonical 'evo2'; kept for old jobs
            'badge': 'Alpha',
            'badge_color': 'orange',
            'outline_color': 'orange',
            'credit_cost': 20,
            'rate_limit': {
                'max_requests': 5,
                'period': 'day',
                'scope': 'user'
            },
            'custom_fields': [
                {
                    'name': 'num_tokens',
                    'label': 'Number of Nucleotides to Generate',
                    'type': 'number',
                    'required': True,
                    'default': 100,
                    'min': 1,
                    'max': 1000,
                    'step': 1,
                    'help': 'How many nucleotides (DNA/RNA bases) to generate',
                },
            ],
        },
        {
            'id': 'type-2',
            'node_id': 'node-alphafold',
            'name': 'AlphaFold2 (Protein Structure)',
            'short_name': 'AlphaFold2',
            'description': 'Predict protein structures with AlphaFold2 (retired: NVIDIA ended the endpoint on 2026-09-24; use OpenFold3).',
            'task': 'protein_folding',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.pdb',
            'available': True,
            'is_active': False,
            'retired': True,
            'offline_reason': 'Retired: NVIDIA ended the AlphaFold2 endpoint on 2026-09-24. Use OpenFold3 or Boltz2.',
            'replacement': 'openfold3',
            'outline_color': 'green',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'num_recycles',
                    'label': 'Number of Recycles',
                    'type': 'range',
                    'required': False,
                    'default': 3,
                    'min': 0,
                    'max': 20,
                    'step': 1,
                    'help': 'Recycling iterations for structure refinement (more = better quality but slower)',
                },
                {
                    'name': 'return_representations',
                    'label': 'Return Embeddings',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Include intermediate layer embeddings in the output (for advanced analysis)',
                },
            ],
        },
        {
            'id': 'type-4',
            'node_id': 'node-proteinmpnn',
            'name': 'ProteinMPNN (Protein Design)',
            'short_name': 'ProteinMPNN',
            'description': 'Design new protein sequences that fold into desired structures using ProteinMPNN.',
            'task': 'protein_design',
            'input_modes': ['file'],
            'output_extension': '.fasta',
            'available': False,
            'is_active': False,
            'coming_soon': True,
            'outline_color': 'green',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'num_sequences',
                    'label': 'Number of Designs',
                    'type': 'range',
                    'required': False,
                    'default': 8,
                    'min': 1,
                    'max': 48,
                    'step': 1,
                    'help': 'Number of different sequence designs to generate',
                },
                {
                    'name': 'sampling_temp',
                    'label': 'Sampling Temperature',
                    'type': 'range',
                    'required': False,
                    'default': 0.1,
                    'min': 0.01,
                    'max': 1.0,
                    'step': 0.01,
                    'help': 'Controls diversity of generated sequences (higher = more diverse)',
                },
            ],
        },
        {
            'id': 'type-5',
            'node_id': 'node-folddock',
            'name': 'Fold+Dock Pipeline (Integrated Workflow)',
            'short_name': 'Fold+Dock',
            'description': 'Complete protein-ligand workflow: fold the protein structure, then dock ligands to find binding sites.',
            'task': 'fold_and_dock',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.pdb',
            'available': True,
            'is_active': True,
            'outline_color': 'teal',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'ligand_input_type',
                    'label': 'Ligand Input Type',
                    'type': 'select',
                    'required': True,
                    'default': 'smiles',
                    'options': [
                        {'value': 'smiles', 'label': 'SMILES String'},
                        {'value': 'file', 'label': 'Project File (SDF/MOL/MOL2)'},
                    ],
                    'help': 'Choose ligand input method',
                },
                {
                    'name': 'ligand',
                    'label': 'Ligand SMILES String',
                    'type': 'textarea',
                    'required': False,
                    'placeholder': 'e.g., CC(=O)OC1=CC=CC=C1C(=O)O (Aspirin)',
                    'help': 'SMILES string for the ligand to dock',
                },
                {
                    'name': 'ligand_file',
                    'label': 'Ligand Structure File',
                    'type': 'file_select',
                    'required': False,
                    'file_extensions': ['.sdf', '.mol', '.mol2'],
                    'help': 'Select a ligand structure file from project files',
                },
                {
                    'name': 'num_recycles',
                    'label': 'Folding Recycles',
                    'type': 'range',
                    'required': False,
                    'default': 4,
                    'min': 0,
                    'max': 20,
                    'step': 1,
                    'help': 'ESMFold recycling iterations for initial structure prediction',
                },
                {
                    'name': 'samples_per_complex',
                    'label': 'Docking Samples',
                    'type': 'range',
                    'required': False,
                    'default': 10,
                    'min': 1,
                    'max': 40,
                    'step': 1,
                    'help': 'Number of binding poses to generate in docking step',
                },
            ],
        },
        {
            'id': 'type-6',
            'node_id': 'node-metal3d',
            'name': 'Metal3D (Metal Binding Site Prediction)',
            'short_name': 'Metal3D',
            'description': 'Predict metal binding sites in proteins using 3D convolutional neural networks. Identifies potential metal coordination sites with confidence scores.',
            'task': 'metal_binding_prediction',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.json',
            'available': False,
            'is_active': False,
            'outline_color': 'indigo',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'protein_input_type',
                    'label': 'Protein Input Type',
                    'type': 'select',
                    'required': True,
                    'default': 'file',
                    'options': [
                        {'value': 'file', 'label': 'From Project Files (PDB)'},
                        {'value': 'pdb', 'label': 'PDB ID'},
                        {'value': 'uniprot', 'label': 'UniProt ID (AlphaFold)'},
                    ],
                    'help': 'Choose how to provide the protein structure',
                },
                {
                    'name': 'protein_file',
                    'label': 'Protein Structure File (PDB)',
                    'type': 'select',
                    'required': False,
                    'options': [],
                    'help': 'Select a PDB structure file from your project files',
                },
                {
                    'name': 'protein_pdb_id',
                    'label': 'PDB ID',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., 6F5N',
                    'help': 'Enter a 4-letter PDB code to fetch from RCSB PDB',
                },
                {
                    'name': 'protein_uniprot_id',
                    'label': 'UniProt ID',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., Q5VSL9',
                    'help': 'Enter a UniProt accession to fetch AlphaFold structure',
                },
                {
                    'name': 'grid_resolution',
                    'label': 'Grid Resolution',
                    'type': 'select',
                    'required': False,
                    'default': '1.0',
                    'options': [
                        {'value': '1.0', 'label': '1.0 Å (Standard, faster)'},
                        {'value': '0.5', 'label': '0.5 Å (High precision, slower)'},
                    ],
                    'help': 'Voxel grid resolution for 3D CNN analysis',
                },
                {
                    'name': 'prediction_mode',
                    'label': 'Prediction Mode',
                    'type': 'select',
                    'required': False,
                    'default': 'metal_binding',
                    'options': [
                        {'value': 'all_residues', 'label': 'All Residues'},
                        {'value': 'metal_binding', 'label': 'Metal-Binding Residues Only'},
                        {'value': 'custom', 'label': 'Custom Residue List'},
                    ],
                    'help': 'Which residues to analyze for metal binding sites',
                },
                {
                    'name': 'custom_residues',
                    'label': 'Custom Residue IDs',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., 10,25,42,156',
                    'help': 'Comma-separated residue numbers (only used if mode is "custom")',
                },
                {
                    'name': 'confidence_threshold',
                    'label': 'Confidence Threshold',
                    'type': 'range',
                    'required': False,
                    'default': 0.5,
                    'min': 0.0,
                    'max': 1.0,
                    'step': 0.05,
                    'help': 'Minimum confidence score for reporting binding sites (0-1)',
                },
            ],
        },
        {
            'id': 'evo2',
            'node_id': 'node-evo2',
            'name': 'Evo2 (DNA Generation)',
            'short_name': 'Evo2',
            'description': 'Generate realistic DNA sequences using Arcs Evo2-40B, a 40-billion parameter genomic language model trained on DNA.',
            'task': 'dna_generation',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.json',
            'available': True,
            'is_active': True,
            'outline_color': 'green',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'num_tokens',
                    'label': 'Tokens to Generate',
                    'type': 'range',
                    'required': False,
                    'default': 100,
                    'min': 1,
                    'max': 1200,
                    'step': 1,
                    'help': 'Number of nucleotides to generate (1-1200)',
                },
                {
                    'name': 'temperature',
                    'label': 'Temperature',
                    'type': 'range',
                    'required': False,
                    'default': 0.7,
                    'min': 0.01,
                    'max': 1.3,
                    'step': 0.01,
                    'help': 'Sampling temperature (higher = more random, lower = more deterministic). Must be > 0.',
                },
                {
                    'name': 'top_k',
                    'label': 'Top-K Sampling',
                    'type': 'range',
                    'required': False,
                    'default': 3,
                    'min': 0,
                    'max': 6,
                    'step': 1,
                    'help': 'Number of highest probability tokens to consider (0 = disabled)',
                },
                {
                    'name': 'top_p',
                    'label': 'Top-P (Nucleus) Sampling',
                    'type': 'range',
                    'required': False,
                    'default': 0.0,
                    'min': 0.0,
                    'max': 1.0,
                    'step': 0.05,
                    'help': 'Nucleus sampling threshold (0 = disabled). Filters tokens when cumulative probability exceeds threshold.',
                },
                {
                    'name': 'random_seed',
                    'label': 'Random Seed',
                    'type': 'number',
                    'required': False,
                    'placeholder': 'Optional integer seed',
                    'help': 'Optional deterministic seed for reproducible generations',
                },
                {
                    'name': 'enable_sampled_probs',
                    'label': 'Return Sampling Probabilities',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Report confidence scores (0-1) for each generated token',
                },
                {
                    'name': 'enable_logits',
                    'label': 'Return Logits',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Include raw token logits in the model response',
                },
                {
                    'name': 'enable_elapsed_ms_per_token',
                    'label': 'Return Token Timing',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Include elapsed milliseconds per generated token',
                },
            ],
        },
        {
            'id': 'type-9',
            'node_id': 'node-boltz2',
            'name': 'Boltz2 (Protein-Ligand Structure)',
            'short_name': 'Boltz2',
            'description': 'Predict biomolecular structures including protein-ligand complexes using Boltz2 model with high confidence scoring.',
            'task': 'structure_prediction',
            'input_modes': ['sequence'],
            'output_extension': '.cif',
            'available': True,
            'is_active': True,
            'badge': 'New',
            'badge_color': 'cyan',
            'outline_color': 'cyan',
            'credit_cost': 20,
            'custom_fields': [
                # Molecule Type Configuration
                {
                    'name': 'molecule_type',
                    'label': 'Molecule Type',
                    'type': 'select',
                    'required': False,
                    'default': 'protein',
                    'options': [
                        {'value': 'protein', 'label': 'Protein (Amino Acids)'},
                        {'value': 'dna', 'label': 'DNA (A, C, G, T)'},
                        {'value': 'rna', 'label': 'RNA (A, C, G, U)'},
                    ],
                    'help': 'Type of polymer sequence to predict structure for',
                },
                {
                    'name': 'cyclic',
                    'label': 'Cyclic Sequence',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Treat sequence as circular/cyclic',
                },
                # Ligand Configuration
                {
                    'name': 'ligand_input_type',
                    'label': 'Ligand Input Type',
                    'type': 'select',
                    'required': False,
                    'default': 'none',
                    'options': [
                        {'value': 'none', 'label': 'No Ligand'},
                        {'value': 'smiles', 'label': 'SMILES String'},
                        {'value': 'ccd', 'label': 'CCD Code (ATP, GTP, etc.)'},
                    ],
                    'help': 'Optional: Add a small molecule ligand for complex prediction',
                },
                {
                    'name': 'ligand_smiles',
                    'label': 'Ligand SMILES',
                    'type': 'textarea',
                    'required': False,
                    'placeholder': 'e.g., CC(=O)OC1=CC=CC=C1C(=O)O (Aspirin)',
                    'help': 'SMILES string for custom small molecule ligand',
                },
                {
                    'name': 'ligand_ccd',
                    'label': 'Ligand CCD Code',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., ATP, GTP, NAD, FAD',
                    'help': 'Chemical Component Dictionary code (1-5 uppercase alphanumeric)',
                },
                {
                    'name': 'predict_affinity',
                    'label': 'Predict Binding Affinity',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Calculate predicted binding affinity for the ligand (only one ligand per request)',
                },
                # Prediction Parameters
                {
                    'name': 'recycling_steps',
                    'label': 'Recycling Steps',
                    'type': 'range',
                    'required': False,
                    'default': 3,
                    'min': 1,
                    'max': 6,
                    'step': 1,
                    'help': 'Number of refinement iterations (1-6)',
                },
                {
                    'name': 'sampling_steps',
                    'label': 'Sampling Steps',
                    'type': 'range',
                    'required': False,
                    'default': 50,
                    'min': 10,
                    'max': 1000,
                    'step': 10,
                    'help': 'Diffusion denoising steps (10-1000, higher = better quality)',
                },
                {
                    'name': 'diffusion_samples',
                    'label': 'Number of Samples',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 5,
                    'step': 1,
                    'help': 'Independent structures to generate (1-5)',
                },
                {
                    'name': 'step_scale',
                    'label': 'Step Scale',
                    'type': 'range',
                    'required': False,
                    'default': 1.2,
                    'min': 0.5,
                    'max': 5.0,
                    'step': 0.1,
                    'help': 'Scaling factor for diffusion steps (0.5-5.0)',
                },
                {
                    'name': 'without_potentials',
                    'label': 'Disable Potentials',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Skip potential calculations (faster but less accurate)',
                },
                {
                    'name': 'write_full_pae',
                    'label': 'Write Full PAE Matrix',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Include full pairwise aligned error matrix in outputs',
                },
                # Affinity Parameters (when predict_affinity is enabled)
                {
                    'name': 'sampling_steps_affinity',
                    'label': 'Affinity Sampling Steps',
                    'type': 'range',
                    'required': False,
                    'default': 200,
                    'min': 10,
                    'max': 1000,
                    'step': 10,
                    'help': 'Sampling steps for affinity prediction (10-1000)',
                },
                {
                    'name': 'diffusion_samples_affinity',
                    'label': 'Affinity Samples',
                    'type': 'range',
                    'required': False,
                    'default': 5,
                    'min': 1,
                    'max': 10,
                    'step': 1,
                    'help': 'Number of samples for affinity calculation (1-10)',
                },
                {
                    'name': 'affinity_mw_correction',
                    'label': 'Molecular Weight Correction',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Apply molecular weight correction to affinity prediction',
                },
            ],
        },
        {
            'id': 'openfold3',
            'node_id': 'node-openfold3',
            'name': 'OpenFold3 (Biomolecular Structure)',
            'short_name': 'OpenFold3',
            'description': 'Next-generation biomolecular structure prediction supporting proteins, DNA, RNA, and ligands with high accuracy.',
            'task': 'structure_prediction',
            'input_modes': ['sequence'],
            'output_extension': '.pdb',
            'available': True,
            'is_active': True,
            'badge': 'New',
            'badge_color': 'teal',
            'outline_color': 'teal',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'molecule_type',
                    'label': 'Molecule Type',
                    'type': 'select',
                    'required': False,
                    'default': 'protein',
                    'options': [
                        {'value': 'protein', 'label': 'Protein (Amino Acids)'},
                        {'value': 'dna', 'label': 'DNA (A, C, G, T)'},
                        {'value': 'rna', 'label': 'RNA (A, C, G, U)'},
                    ],
                    'help': 'Type of molecule to predict structure for',
                },
                {
                    'name': 'msa_input',
                    'label': 'MSA (Optional)',
                    'type': 'textarea',
                    'required': False,
                    'placeholder': 'Paste MSA in FASTA or A3M format (e.g., >query\nSEQUENCE\n>hit1\nSEQUENCE)',
                    'help': 'Multiple Sequence Alignment improves prediction accuracy. Leave empty for alignment-free prediction.',
                },
                {
                    'name': 'msa_format',
                    'label': 'MSA Format',
                    'type': 'select',
                    'required': False,
                    'default': 'a3m',
                    'options': [
                        {'value': 'a3m', 'label': 'A3M'},
                        {'value': 'csv', 'label': 'CSV'},
                    ],
                    'help': 'Format of the MSA data provided above',
                },
                {
                    'name': 'diffusion_samples',
                    'label': 'Number of Samples',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 5,
                    'step': 1,
                    'help': 'Number of independent structures to generate (more = slower but may find better solutions)',
                },
                {
                    'name': 'ligand_input_type',
                    'label': 'Ligand Input Type',
                    'type': 'select',
                    'required': False,
                    'default': 'none',
                    'options': [
                        {'value': 'none', 'label': 'No Ligand'},
                        {'value': 'smiles', 'label': 'SMILES String'},
                        {'value': 'ccd', 'label': 'CCD Code (ATP, GTP, etc.)'},
                    ],
                    'help': 'Optional: Add a small molecule ligand for complex prediction',
                },
                {
                    'name': 'ligand_smiles',
                    'label': 'Ligand SMILES',
                    'type': 'textarea',
                    'required': False,
                    'placeholder': 'e.g., CC(=O)OC1=CC=CC=C1C(=O)O (Aspirin)',
                    'help': 'SMILES string for custom small molecule ligand',
                },
                {
                    'name': 'ligand_ccd',
                    'label': 'Ligand CCD Code',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., ATP, GTP, NAD, FAD',
                    'help': 'Chemical Component Dictionary code',
                },
                {
                    'name': 'output_format',
                    'label': 'Output Format',
                    'type': 'select',
                    'required': False,
                    'default': 'pdb',
                    'options': [
                        {'value': 'pdb', 'label': 'PDB'},
                        {'value': 'cif', 'label': 'mmCIF'},
                    ],
                    'help': 'Structure output format',
                },
            ],
        },
        {
            'id': 'type-12',
            'node_id': 'node-rfdiffusion',
            'name': 'RFDiffusion (Protein Design)',
            'short_name': 'RFDiffusion',
            'description': 'De novo protein structure generation using diffusion-based design. Design binders, scaffolds, and novel proteins.',
            'task': 'protein_design',
            'input_modes': ['pdb'],
            'output_extension': '.pdb',
            'available': True,
            'is_active': True,
            'badge': 'New',
            'badge_color': 'purple',
            'outline_color': 'purple',
            'credit_cost': 20,
            'custom_fields': [
                # Input PDB Configuration
                {
                    'name': 'input_pdb',
                    'label': 'Input PDB Structure',
                    'type': 'file',
                    'required': True,
                    'accept': '.pdb',
                    'help': 'Upload a PDB file as the design template/target',
                },
                {
                    'name': 'pdb_id',
                    'label': 'Or Fetch from RCSB',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., 1R42',
                    'help': 'Fetch structure directly from RCSB PDB database',
                },
                # Contig Specification
                {
                    'name': 'contigs',
                    'label': 'Contig Specification',
                    'type': 'text',
                    'required': True,
                    'placeholder': 'e.g., A20-60/0 50-100',
                    'help': 'Define which regions to design. Format: ChainStart-End/Length or just Length. Separate with / or space.',
                },
                # Hotspot Residues
                {
                    'name': 'hotspot_res',
                    'label': 'Hotspot Residues',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., A50,A51,A52,A53,A54',
                    'help': 'Residues on the target that the binder should interact with (comma-separated)',
                },
                # Diffusion Parameters
                {
                    'name': 'diffusion_steps',
                    'label': 'Diffusion Steps',
                    'type': 'range',
                    'required': False,
                    'default': 50,
                    'min': 1,
                    'max': 200,
                    'step': 1,
                    'help': 'Number of diffusion denoising steps (1-200, higher = better quality)',
                },
                {
                    'name': 'num_designs',
                    'label': 'Number of Designs',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 8,
                    'step': 1,
                    'help': 'Number of independent designs to generate (1-8)',
                },
                {
                    'name': 'noise_scale',
                    'label': 'Noise Scale',
                    'type': 'range',
                    'required': False,
                    'default': 1.0,
                    'min': 0.1,
                    'max': 2.0,
                    'step': 0.1,
                    'help': 'Scale of noise during diffusion (0.1-2.0)',
                },
                {
                    'name': 'partial_T',
                    'label': 'Partial Diffusion Steps',
                    'type': 'number',
                    'required': False,
                    'placeholder': 'Optional',
                    'help': 'For partial diffusion/inpainting - number of initial steps to skip',
                },
            ],
        },
        {
            'id': 'alphafold2-re',
            'node_id': 'node-alphafold2-re',
            'name': 'AlphaFold2-RE (Protein Structure)',
            'short_name': 'AlphaFold2-RE',
            'description': 'Predict 3D protein structures using accelerated AlphaFold2 with enhanced parameters and database options.',
            'task': 'protein_folding',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.pdb',
            'available': True,
            # NVIDIA retired the hosted endpoint (HTTP 410); kept listed so old results stay reachable.
            'is_active': False,
            'retired': True,
            'replacement': 'openfold3',
            'offline_reason': 'Retired: NVIDIA ended the AlphaFold2 endpoint on 2026-09-24. Use OpenFold3 or Boltz2.',
            'is_visible': True,
            'runtime_managed': True,
            'outline_color': 'blue',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'databases',
                    'label': 'MSA Databases',
                    'type': 'multiselect',
                    'required': False,
                    'default': ['uniref90', 'mgnify', 'small_bfd'],
                    'options': [
                        {'value': 'uniref90', 'label': 'UniRef90'},
                        {'value': 'mgnify', 'label': 'MGnify'},
                        {'value': 'small_bfd', 'label': 'Small BFD'},
                    ],
                    'help': 'Choose databases for Multiple Sequence Alignment (more = better accuracy)',
                },
                {
                    'name': 'algorithm',
                    'label': 'MSA Algorithm',
                    'type': 'select',
                    'required': False,
                    'default': 'jackhmmer',
                    'options': [
                        {'value': 'jackhmmer', 'label': 'JackHMMER'},
                        {'value': 'mmseqs2', 'label': 'MMseqs2'},
                    ],
                    'help': 'Algorithm for Multiple Sequence Alignment',
                },
                {
                    'name': 'e_value',
                    'label': 'E-value Threshold',
                    'type': 'number',
                    'required': False,
                    'default': 0.0001,
                    'min': 0,
                    'max': 10,
                    'step': 0.0001,
                    'help': 'E-value threshold for MSA filtering (0 < x ≤ 10, smaller = stricter)',
                },
                {
                    'name': 'iterations',
                    'label': 'MSA Iterations',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 3,
                    'step': 1,
                    'help': 'Number of MSA iterations to perform',
                },
                {
                    'name': 'relax_prediction',
                    'label': 'Geometry Refinement',
                    'type': 'checkbox',
                    'required': False,
                    'default': True,
                    'help': 'Optimize atom positions based on molecular geometry constraints',
                },
                {
                    'name': 'structure_models_to_relax',
                    'label': 'Models to Relax',
                    'type': 'select',
                    'required': False,
                    'default': 'all',
                    'options': [
                        {'value': 'all', 'label': 'All predictions'},
                        {'value': 'best', 'label': 'Best prediction only'},
                        {'value': 'none', 'label': 'None (skip relaxation)'},
                    ],
                    'help': 'Which predictions to relax with AMBER force field',
                },
            ],
        },
        {
            'id': 'alphafold2-re-multimer',
            'node_id': 'node-alphafold2-re-multimer',
            'name': 'AlphaFold2-RE-Multimer (Protein Complex)',
            'short_name': 'AlphaFold2-Multimer',
            'description': 'Predict multi-protein complex structures using accelerated AlphaFold2-Multimer with enhanced parameters.',
            'task': 'protein_complex_folding',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.pdb',
            'available': True,
            # NVIDIA retired the hosted endpoint (HTTP 410); kept listed so old results stay reachable.
            'is_active': False,
            'retired': True,
            'replacement': 'openfold3',
            'offline_reason': 'Retired: NVIDIA ended the AlphaFold2-Multimer endpoint on 2026-09-24. Use OpenFold3 or Boltz2 for complexes.',
            'is_visible': True,
            'runtime_managed': True,
            'outline_color': 'blue',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'databases',
                    'label': 'MSA Databases',
                    'type': 'multiselect',
                    'required': False,
                    'default': ['uniref90', 'mgnify', 'small_bfd'],
                    'options': [
                        {'value': 'uniref90', 'label': 'UniRef90'},
                        {'value': 'mgnify', 'label': 'MGnify'},
                        {'value': 'small_bfd', 'label': 'Small BFD'},
                    ],
                    'help': 'Choose databases for Multiple Sequence Alignment (more = better accuracy)',
                },
                {
                    'name': 'algorithm',
                    'label': 'MSA Algorithm',
                    'type': 'select',
                    'required': False,
                    'default': 'jackhmmer',
                    'options': [
                        {'value': 'jackhmmer', 'label': 'JackHMMER'},
                        {'value': 'mmseqs2', 'label': 'MMseqs2'},
                    ],
                    'help': 'Algorithm for Multiple Sequence Alignment',
                },
                {
                    'name': 'e_value',
                    'label': 'E-value Threshold',
                    'type': 'number',
                    'required': False,
                    'default': 0.0001,
                    'min': 0,
                    'max': 10,
                    'step': 0.0001,
                    'help': 'E-value threshold for MSA filtering (0 < x ≤ 10, smaller = stricter)',
                },
                {
                    'name': 'iterations',
                    'label': 'MSA Iterations',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 3,
                    'step': 1,
                    'help': 'Number of MSA iterations to perform',
                },
                {
                    'name': 'num_predictions_per_model',
                    'label': 'Predictions Per Model',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 8,
                    'step': 1,
                    'help': 'Number of predictions per model (1-8)',
                },
                {
                    'name': 'template_searcher',
                    'label': 'Template Search Method',
                    'type': 'select',
                    'required': False,
                    'default': 'hhsearch',
                    'options': [
                        {'value': 'hhsearch', 'label': 'HHsearch'},
                        {'value': 'hmmsearch', 'label': 'HMMsearch'},
                    ],
                    'help': 'Template search method for multimer prediction',
                },
                {
                    'name': 'max_msa_sequences',
                    'label': 'Max MSA Sequences',
                    'type': 'number',
                    'required': False,
                    'placeholder': 'Optional (1-100000)',
                    'help': 'Limit maximum MSA sequences used (optional)',
                },
                {
                    'name': 'relax_prediction',
                    'label': 'Geometry Refinement',
                    'type': 'checkbox',
                    'required': False,
                    'default': True,
                    'help': 'Optimize atom positions based on molecular geometry constraints',
                },
                {
                    'name': 'structure_models_to_relax',
                    'label': 'Models to Relax',
                    'type': 'select',
                    'required': False,
                    'default': 'all',
                    'options': [
                        {'value': 'all', 'label': 'All predictions'},
                        {'value': 'best', 'label': 'Best prediction only'},
                        {'value': 'none', 'label': 'None (skip relaxation)'},
                    ],
                    'help': 'Which predictions to relax with AMBER force field',
                },
            ],
        },
        {
            'id': 'boltz2-enhanced',
            'node_id': 'node-boltz2-enhanced',
            'name': 'Boltz2-Enhanced (Advanced Structure Prediction)',
            'short_name': 'Boltz2+',
            'description': 'Enhanced Boltz2 biomolecular structure prediction with advanced parameters, ligand affinity, and ensemble generation.',
            'task': 'enhanced_structure_prediction',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.cif',
            'available': False,
            'is_active': False,
            'is_visible': False,
            'badge': 'Advanced',
            'badge_color': 'red',
            'outline_color': 'red',
            'credit_cost': 20,
            'custom_fields': [
                # Core Boltz2 parameters
                {
                    'name': 'molecule_type',
                    'label': 'Molecule Type',
                    'type': 'select',
                    'required': False,
                    'default': 'protein',
                    'options': [
                        {'value': 'protein', 'label': 'Protein (Amino Acids)'},
                        {'value': 'dna', 'label': 'DNA (A, C, G, T)'},
                        {'value': 'rna', 'label': 'RNA (A, C, G, U)'},
                    ],
                    'help': 'Type of polymer sequence to predict structure for',
                },
                {
                    'name': 'cyclic',
                    'label': 'Cyclic Sequence',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Treat sequence as circular/cyclic',
                },
                {
                    'name': 'recycling_steps',
                    'label': 'Recycling Steps',
                    'type': 'range',
                    'required': False,
                    'default': 3,
                    'min': 1,
                    'max': 6,
                    'step': 1,
                    'help': 'Refinement iterations (more = better quality but slower)',
                },
                {
                    'name': 'sampling_steps',
                    'label': 'Sampling Steps',
                    'type': 'range',
                    'required': False,
                    'default': 50,
                    'min': 10,
                    'max': 1000,
                    'step': 10,
                    'help': 'Diffusion sampling steps (more = better quality)',
                },
                {
                    'name': 'diffusion_samples',
                    'label': 'Diffusion Samples',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 5,
                    'step': 1,
                    'help': 'Number of independent structures to generate',
                },
                {
                    'name': 'step_scale',
                    'label': 'Step Scale',
                    'type': 'range',
                    'required': False,
                    'default': 1.2,
                    'min': 0.5,
                    'max': 5.0,
                    'step': 0.1,
                    'help': 'Scaling factor for diffusion steps (0.5-5.0)',
                },
                {
                    'name': 'without_potentials',
                    'label': 'Skip Potential Calculations',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Exclude potential calculations for faster processing',
                },
                # Enhanced Boltz2 parameters
                {
                    'name': 'temperature',
                    'label': 'Sampling Temperature',
                    'type': 'range',
                    'required': False,
                    'default': 1.0,
                    'min': 0.1,
                    'max': 2.0,
                    'step': 0.1,
                    'help': 'Temperature for sampling (0.1-2.0)',
                },
                {
                    'name': 'noise_scale',
                    'label': 'Noise Scale',
                    'type': 'range',
                    'required': False,
                    'default': 1.0,
                    'min': 0.1,
                    'max': 2.0,
                    'step': 0.1,
                    'help': 'Scale for diffusion noise (0.1-2.0)',
                },
                {
                    'name': 'use_template',
                    'label': 'Use Templates',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Use template information if available',
                },
                {
                    'name': 'confidence_threshold',
                    'label': 'Confidence Threshold',
                    'type': 'range',
                    'required': False,
                    'default': 0.5,
                    'min': 0.0,
                    'max': 1.0,
                    'step': 0.1,
                    'help': 'Threshold for confidence filtering (0.0-1.0)',
                },
                {
                    'name': 'bond_constraints',
                    'label': 'Bond Constraints',
                    'type': 'checkbox',
                    'required': False,
                    'default': True,
                    'help': 'Apply bond length constraints',
                },
                {
                    'name': 'symmetry_constraints',
                    'label': 'Symmetry Constraints',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Apply symmetry constraints',
                },
                {
                    'name': 'ensemble_prediction',
                    'label': 'Ensemble Prediction',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Generate ensemble of predictions',
                },
                {
                    'name': 'ensemble_size',
                    'label': 'Ensemble Size',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 5,
                    'step': 1,
                    'help': 'Number of predictions in ensemble (1-5)',
                },
                # Ligand configuration
                {
                    'name': 'ligand_input_type',
                    'label': 'Ligand Input Type',
                    'type': 'select',
                    'required': False,
                    'default': 'none',
                    'options': [
                        {'value': 'none', 'label': 'No Ligand'},
                        {'value': 'smiles', 'label': 'SMILES String'},
                        {'value': 'ccd', 'label': 'PDB CCD Code'},
                    ],
                    'help': 'Type of ligand input to use',
                },
                {
                    'name': 'ligand_smiles',
                    'label': 'Ligand SMILES',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'C1=CC(=O)N(C)C(=N1)C',
                    'help': 'SMILES string for ligand (when SMILES input selected)',
                },
                {
                    'name': 'ligand_ccd',
                    'label': 'Ligand CCD Code',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'ATP',
                    'help': 'PDB Chemical Component Dictionary code (when CCD input selected)',
                },
                # Affinity parameters
                {
                    'name': 'predict_affinity',
                    'label': 'Predict Binding Affinity',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Calculate binding affinity for ligand (requires single ligand)',
                },
                {
                    'name': 'sampling_steps_affinity',
                    'label': 'Affinity Sampling Steps',
                    'type': 'range',
                    'required': False,
                    'default': 200,
                    'min': 10,
                    'max': 1000,
                    'step': 10,
                    'help': 'Steps for affinity calculation (10-1000)',
                },
                {
                    'name': 'diffusion_samples_affinity',
                    'label': 'Affinity Diffusion Samples',
                    'type': 'range',
                    'required': False,
                    'default': 5,
                    'min': 1,
                    'max': 10,
                    'step': 1,
                    'help': 'Samples for affinity calculation (1-10)',
                },
                {
                    'name': 'affinity_mw_correction',
                    'label': 'Molecular Weight Correction',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Apply molecular weight correction to affinity scores',
                },
            ],
        },
        {
            'id': 'esmfold-re',
            'node_id': 'node-esmfold-re',
            'name': 'ESMFold-RE (Protein Structure)',
            'short_name': 'ESMFold-RE',
            'description': 'Fast single-sequence protein structure prediction using Meta ESMFold. No MSA required.',
            'task': 'protein_folding',
            'input_modes': ['sequence'],
            'output_extension': '.pdb',
            'available': True,
            # NVIDIA retired the hosted endpoint (HTTP 410); kept listed so old results stay reachable.
            'is_active': False,
            'retired': True,
            'replacement': 'openfold3',
            'offline_reason': 'Retired: NVIDIA ended the ESMFold endpoint on 2026-08-25. Use OpenFold3 or Boltz2.',
            'outline_color': 'green',
            'credit_cost': 20,
            'custom_fields': [],
        },
        {
            'id': 'diffdock-re',
            'node_id': 'node-diffdock-re',
            'name': 'DiffDock-RE (Molecular Docking)',
            'short_name': 'DiffDock-RE',
            'description': 'Diffusion-based molecular docking prediction for protein-ligand binding poses.',
            'task': 'molecular_docking',
            'input_modes': ['pdb'],
            'output_extension': '.sdf',
            'available': True,
            'is_active': True,
            'badge': 'Hosted',
            'badge_color': 'orange',
            'outline_color': 'orange',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'protein',
                    'label': 'Protein Structure (PDB)',
                    'type': 'file',
                    'required': True,
                    'accept': '.pdb',
                    'help': 'Upload protein structure in PDB format',
                },
                {
                    'name': 'ligand',
                    'label': 'Ligand Structure',
                    'type': 'textarea',
                    'required': True,
                    'placeholder': 'Enter ligand in SDF, Mol2, or SMILES format',
                    'help': 'Ligand molecule for docking',
                },
                {
                    'name': 'ligand_file_type',
                    'label': 'Ligand Format',
                    'type': 'select',
                    'required': True,
                    'default': 'sdf',
                    'options': [
                        {'value': 'sdf', 'label': 'SDF'},
                        {'value': 'mol2', 'label': 'Mol2'},
                        {'value': 'smiles', 'label': 'SMILES'},
                    ],
                    'help': 'Format of the ligand input',
                },
                {
                    'name': 'num_poses',
                    'label': 'Number of Poses',
                    'type': 'range',
                    'required': False,
                    'default': 10,
                    'min': 1,
                    'max': 20,
                    'step': 1,
                    'help': 'Number of docking poses to generate (1-20)',
                },
                {
                    'name': 'time_divisions',
                    'label': 'Time Divisions',
                    'type': 'range',
                    'required': False,
                    'default': 20,
                    'min': 1,
                    'max': 100,
                    'step': 1,
                    'help': 'Diffusion time steps (1-100)',
                },
                {
                    'name': 'steps',
                    'label': 'Diffusion Steps',
                    'type': 'range',
                    'required': False,
                    'default': 18,
                    'min': 1,
                    'max': 100,
                    'step': 1,
                    'help': 'Number of diffusion steps (1-100)',
                },
                {
                    'name': 'save_trajectory',
                    'label': 'Save Trajectory',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Save diffusion trajectory for visualization',
                },
                {
                    'name': 'is_staged',
                    'label': 'Staged Diffusion',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Use staged diffusion process',
                },
                {
                    'name': 'no_final_step_noise',
                    'label': 'No Final Step Noise',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Skip noise injection at the final diffusion step',
                },
            ],
        },
        {
            'id': 'proteinmpnn-re',
            'node_id': 'node-proteinmpnn-re',
            'name': 'ProteinMPNN-RE (Sequence Design)',
            'short_name': 'ProteinMPNN-RE',
            'description': 'Design protein sequences for given 3D structures using ProteinMPNN.',
            'task': 'protein_design',
            'input_modes': ['pdb'],
            'output_extension': '.fasta',
            'available': True,
            'is_active': True,
            'status_override': 'degraded',
            'badge': 'Hosted',
            'badge_color': 'indigo',
            'outline_color': 'indigo',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'input_pdb',
                    'label': 'Input PDB Structure',
                    'type': 'file',
                    'required': True,
                    'accept': '.pdb',
                    'help': 'Upload a PDB file as the design template',
                },
                {
                    'name': 'pdb_path_chains',
                    'label': 'Chains to Design',
                    'type': 'text',
                    'required': False,
                    'placeholder': 'e.g., A or A,B (defaults to all chains)',
                    'help': 'Comma-separated chain IDs to design sequences for (leave empty for all chains)',
                },
                {
                    'name': 'num_seq_per_target',
                    'label': 'Number of Sequences',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 100,
                    'step': 1,
                    'help': 'Number of sequences to generate per target (1-100)',
                },
                {
                    'name': 'sampling_temp',
                    'label': 'Sampling Temperature',
                    'type': 'text',
                    'required': False,
                    'default': '0.1',
                    'placeholder': 'e.g., 0.1 or 0.1,0.2,0.3',
                    'help': 'Sampling temperatures as comma-separated values (0-1 exclusive, recommended 0.1-0.3)',
                },
                {
                    'name': 'degraded_batch',
                    'label': 'Degraded Batch Mode',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Generate sequences in smaller internal batches for improved reliability on degraded runtimes (slower).',
                },
                {
                    'name': 'ca_only',
                    'label': 'C-alpha Only',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'CA-only model focuses on alpha carbon atoms for faster inference',
                },
                {
                    'name': 'use_soluble_model',
                    'label': 'Soluble Model',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Use soluble protein model (better for soluble protein design)',
                },
                {
                    'name': 'seed',
                    'label': 'Random Seed',
                    'type': 'number',
                    'required': False,
                    'placeholder': 'Optional (omit for random)',
                    'help': 'Set seed for reproducibility or leave empty for random',
                },
                {
                    'name': 'pssm_multi',
                    'label': 'PSSM Weight',
                    'type': 'range',
                    'required': False,
                    'default': 0,
                    'min': 0.0,
                    'max': 1.0,
                    'step': 0.1,
                    'help': 'PSSM influence weight (0 ignores PSSM, 1 relies solely on it)',
                },
                {
                    'name': 'pssm_threshold',
                    'label': 'PSSM Threshold',
                    'type': 'number',
                    'required': False,
                    'default': 0,
                    'help': 'PSSM threshold for filtering (higher = more restrictive)',
                },
                {
                    'name': 'pssm_bias_flag',
                    'label': 'PSSM Bias',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Apply bias based on Position-Specific Scoring Matrix',
                },
            ],
        },
        {
            'id': 'molmim',
            'node_id': 'node-molmim',
            'name': 'MolMIM (Molecule Generation)',
            'short_name': 'MolMIM',
            'description': 'Generate novel drug-like molecules from input SMILES using the MolMIM generative chemistry model. Research Preview only, for testing: NVIDIA licenses MolMIM for research and development only, so it is not part of paid plans.',
            'task': 'molecule_generation',
            'input_modes': ['smiles'],
            'output_extension': '.smi',
            'available': True,
            'is_active': True,
            'badge': 'Hosted',
            'badge_color': 'pink',
            'outline_color': 'pink',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'smi',
                    'label': 'Input SMILES',
                    'type': 'textarea',
                    'required': True,
                    'placeholder': 'e.g., CC(=O)OC1=CC=CC=C1C(=O)O',
                    'help': 'SMILES string of the seed molecule',
                },
                {
                    'name': 'num_molecules',
                    'label': 'Number of Molecules',
                    'type': 'range',
                    'required': False,
                    'default': 10,
                    'min': 1,
                    'max': 100,
                    'step': 1,
                    'help': 'Number of molecules to generate (1-100)',
                },
                {
                    'name': 'algorithm',
                    'label': 'Algorithm',
                    'type': 'select',
                    'required': False,
                    'default': 'CMA-ES',
                    'options': [
                        {'value': 'CMA-ES', 'label': 'CMA-ES'},
                        {'value': 'SGA', 'label': 'SGA'},
                        {'value': 'Greedy', 'label': 'Greedy'},
                        {'value': 'Random', 'label': 'Random'},
                    ],
                    'help': 'Optimization algorithm for molecule generation',
                },
                {
                    'name': 'property_name',
                    'label': 'Target Property',
                    'type': 'select',
                    'required': False,
                    'default': 'QED',
                    'options': [
                        {'value': 'QED', 'label': 'QED (Drug-likeness)'},
                        {'value': 'plogp', 'label': 'pLogP (Lipophilicity)'},
                    ],
                    'help': 'Property to optimize for',
                },
                {
                    'name': 'minimize',
                    'label': 'Minimize Property',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Minimize the target property instead of maximizing',
                },
                {
                    'name': 'min_similarity',
                    'label': 'Minimum Similarity',
                    'type': 'range',
                    'required': False,
                    'default': 0.3,
                    'min': 0.0,
                    'max': 1.0,
                    'step': 0.05,
                    'help': 'Minimum Tanimoto similarity to input molecule (0.0-1.0)',
                },
                {
                    'name': 'iterations',
                    'label': 'Optimization Iterations',
                    'type': 'range',
                    'required': False,
                    'default': 10,
                    'min': 1,
                    'max': 100,
                    'step': 1,
                    'help': 'Number of optimization iterations (1-100)',
                },
                {
                    'name': 'particles',
                    'label': 'Particles (CMA-ES)',
                    'type': 'range',
                    'required': False,
                    'default': 30,
                    'min': 1,
                    'max': 200,
                    'step': 1,
                    'help': 'Number of particles for CMA-ES algorithm (1-200)',
                },
            ],
        },
        {
            'id': 'genmol',
            'node_id': 'node-genmol',
            'name': 'GenMol (Fragment Molecule Generation)',
            'short_name': 'GenMol',
            'description': 'Generate novel, valid drug-like molecules from a SMILES seed or masked SAFE fragment sequence using the GenMol masked-diffusion generative chemistry model. Results are ranked by QED (drug-likeness) or LogP.',
            'task': 'molecule_generation',
            'input_modes': ['smiles'],
            'output_extension': '.json',
            'available': True,
            'is_active': True,
            'badge': 'Hosted',
            'badge_color': 'pink',
            'outline_color': 'pink',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'smiles',
                    'label': 'Input SMILES / SAFE Sequence',
                    'type': 'textarea',
                    'required': True,
                    'placeholder': 'e.g., C124CN3C1.S3(=O)(=O)CC.C4C#N.[*{20-20}]',
                    'help': 'A SMILES string or a SAFE sequence with masking segments (e.g. [*{20-20}]) marking where new fragments should be generated.',
                },
                {
                    'name': 'num_molecules',
                    'label': 'Number of Molecules',
                    'type': 'range',
                    'required': False,
                    'default': 30,
                    'min': 1,
                    'max': 1000,
                    'step': 1,
                    'help': 'Number of molecules to generate (1-1000). Invalid molecules are excluded from the count.',
                },
                {
                    'name': 'scoring',
                    'label': 'Scoring / Ranking',
                    'type': 'select',
                    'required': False,
                    'default': 'QED',
                    'options': [
                        {'value': 'QED', 'label': 'QED (Drug-likeness)'},
                        {'value': 'LogP', 'label': 'LogP (Lipophilicity)'},
                    ],
                    'help': 'Property used to rank generated molecules.',
                },
                {
                    'name': 'temperature',
                    'label': 'Temperature',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 0.01,
                    'max': 10,
                    'step': 0.01,
                    'help': 'Softmax sampling scaling factor (0.01-10). Higher = more diverse/random.',
                },
                {
                    'name': 'noise',
                    'label': 'Noise',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 0,
                    'max': 2,
                    'step': 0.05,
                    'help': 'Noise factor for top-k sampling (0-2).',
                },
                {
                    'name': 'step_size',
                    'label': 'Step Size',
                    'type': 'range',
                    'required': False,
                    'default': 1,
                    'min': 1,
                    'max': 10,
                    'step': 1,
                    'help': 'Number of masking tokens recovered per inference step (1-10).',
                },
                {
                    'name': 'unique',
                    'label': 'Unique Only',
                    'type': 'checkbox',
                    'required': False,
                    'default': False,
                    'help': 'Return only unique molecules.',
                },
            ],
        },
        {
            'id': 'msa-search',
            'node_id': 'node-msa-search',
            'name': 'MSA Search (Sequence Alignment)',
            'short_name': 'MSA Search',
            'description': 'Multiple Sequence Alignment search using ColabFold. Utility for AlphaFold2 and other MSA-dependent models.',
            'task': 'sequence_alignment',
            'input_modes': ['sequence'],
            'output_extension': '.a3m',
            'available': False,
            'is_active': False,
            'badge': 'Utility',
            'badge_color': 'gray',
            'outline_color': 'gray',
            'credit_cost': 20,
            'custom_fields': [
                {
                    'name': 'sequence',
                    'label': 'Protein Sequence',
                    'type': 'textarea',
                    'required': True,
                    'placeholder': 'Enter protein amino acid sequence',
                    'help': 'Protein sequence for MSA search (max 4096 residues)',
                },
                {
                    'name': 'databases',
                    'label': 'Search Databases',
                    'type': 'multiselect',
                    'required': False,
                    'default': ['uniref90', 'small_bfd', 'colabfold_envdb'],
                    'options': [
                        {'value': 'uniref90', 'label': 'UniRef90'},
                        {'value': 'small_bfd', 'label': 'Small BFD'},
                        {'value': 'colabfold_envdb', 'label': 'ColabFold Env DB'},
                    ],
                    'help': 'Databases to search for homologous sequences',
                },
                {
                    'name': 'search_type',
                    'label': 'Search Type',
                    'type': 'select',
                    'required': False,
                    'default': 'mmseqs2',
                    'options': [
                        {'value': 'mmseqs2', 'label': 'MMseqs2'},
                    ],
                    'help': 'Search algorithm to use',
                },
            ],
        },
    ]

    # ------------------------------------------------------------------
    # Model registry: UI metadata and hosting, merged into SUPPORTED_MODELS
    # ------------------------------------------------------------------
    # Where a model runs is configuration, not copy. Defaults describe today
    # (NVIDIA NIM, USA). To move models to AWS/GCP/own hardware set
    # PHONOS_DEFAULT_HOSTED_PROVIDER / PHONOS_DEFAULT_HOSTED_REGION, or per model
    # PHONOS_MODEL_HOSTING as JSON, e.g.
    #   {"type-9": {"provider": "AWS", "region": "eu-central-1"}}
    DEFAULT_HOSTED_BY = {
        'provider': os.environ.get('PHONOS_DEFAULT_HOSTED_PROVIDER', 'NVIDIA NIM'),
        'region': os.environ.get('PHONOS_DEFAULT_HOSTED_REGION', 'USA'),
    }
    # Legacy self-run nodes (no NIM endpoint); everything else uses the default.
    # Models that run on FyreFly-operated hardware. Empty today: every active model,
    # including the Fold+Dock pipeline (type-5), runs its stages at NVIDIA NIM.
    # Add ids here (or use PHONOS_MODEL_HOSTING) once own hardware is serving them.
    _SELF_HOSTED_MODEL_IDS = set()
    _SELF_HOSTED_BY = {'provider': 'FyreFly', 'region': 'Germany'}

    # ui = short_name, abbr (card chip), color (token; palette lives in
    # ModelRegistry.PALETTE in static/js/app.js), glyph (icon family), tagline.
    _MODEL_UI = {
        'type-0': ('ESMFold', 'ESM', 'blue', 'fold', 'Fast protein folding'),
        'type-2': ('AlphaFold2', 'Alpha', 'green', 'fold', 'High-accuracy folding'),
        'type-3': ('DiffDock', 'Dock', 'purple', 'dock', 'Molecular docking'),
        'type-4': ('ProteinMPNN', 'MPNN', 'orange', 'design', 'Sequence design'),
        'type-5': ('Fold+Dock', 'F+D', 'teal', 'dock', 'Fold and dock'),
        'type-6': ('Metal3D', 'M3D', 'indigo', 'hex', 'Metal binding sites'),
        'type-8': ('Evo2', 'Evo2', 'orange', 'dna', 'DNA generation'),
        'evo2': ('Evo2', 'Evo2', 'orange', 'dna', 'DNA generation'),
        'type-9': ('Boltz2', 'Boltz', 'cyan', 'hex', 'Structure prediction'),
        'openfold3': ('OpenFold3', 'OF3', 'teal', 'hex', 'Biomolecular structure'),
        'type-12': ('RFDiffusion', 'RFDiff', 'purple', 'design', 'Protein design'),
        'alphafold2-re': ('AlphaFold2-RE', 'AF2-RE', 'blue', 'fold', 'Protein structure'),
        'alphafold2-re-multimer': ('AlphaFold2-Multimer', 'AF2-Multi', 'blue', 'fold', 'Protein complex prediction'),
        'boltz2-enhanced': ('Boltz2+', 'Boltz+', 'red', 'hex', 'Advanced structure prediction'),
        'esmfold-re': ('ESMFold-RE', 'ESM-RE', 'green', 'fold', 'Protein folding'),
        'diffdock-re': ('DiffDock-RE', 'DDock-RE', 'orange', 'dock', 'Molecular docking'),
        'proteinmpnn-re': ('ProteinMPNN-RE', 'MPNN-RE', 'indigo', 'design', 'Sequence design'),
        'molmim': ('MolMIM', 'MolMIM', 'pink', 'molecule', 'Molecule generation'),
        'genmol': ('GenMol', 'GenMol', 'pink', 'molecule', 'Fragment molecule generation'),
        'msa-search': ('MSA Search', 'MSA', 'gray', 'align', 'Sequence alignment'),
    }

    _hosting_overrides = {}
    if os.environ.get('PHONOS_MODEL_HOSTING'):
        try:
            import json as _hjson
            _loaded = _hjson.loads(os.environ['PHONOS_MODEL_HOSTING'])
            if isinstance(_loaded, dict):
                _hosting_overrides = _loaded
        except ValueError:
            pass

    for _m in SUPPORTED_MODELS:
        _ui = _MODEL_UI.get(_m['id'])
        if _ui and 'ui' not in _m:
            _m['ui'] = {'short_name': _ui[0], 'abbr': _ui[1], 'color': _ui[2],
                        'glyph': _ui[3], 'tagline': _ui[4]}
        if 'hosted_by' not in _m:
            _hb = _SELF_HOSTED_BY if _m['id'] in _SELF_HOSTED_MODEL_IDS else DEFAULT_HOSTED_BY
            _hb = dict(_hb)
            _hb.update(_hosting_overrides.get(_m['id'], {}))
            _m['hosted_by'] = _hb


    # Notebook Configuration
    # Use a directory within the project structure for notebooks to ensure permissions
    _PROJECT_ROOT = PROJECT_ROOT
    NOTEBOOK_WORKSPACE_BASE = os.environ.get('NOTEBOOK_WORKSPACE_BASE', os.path.join(_PROJECT_ROOT, 'data', 'notebooks'))
    NOTEBOOK_FILES_DIR = os.environ.get('NOTEBOOK_FILES_DIR', os.path.join(_PROJECT_ROOT, 'data', 'notebooks'))
    
    # Project Files Base (aligned with UPLOAD_FOLDER for legacy compatibility)
    PROJECT_FILES_DIR = os.path.join(UPLOAD_FOLDER, 'projects')

    # Centralized Network Configuration
    _DEFAULT_MANAGER_HOST = resolve_host(
        os.environ.get('PHONOS_MANAGER_HOST', '127.0.0.1'),
        ('127.0.0.1', 'localhost'),
        context='manager host',
    )
    MANAGER_CLIENT_HOST = os.environ.get('MANAGER_CLIENT_HOST', _DEFAULT_MANAGER_HOST)
    MANAGER_CLIENT_PORT = int(os.environ.get('MANAGER_CLIENT_PORT', 65432))

    MANAGER_WORKER_HOST = os.environ.get('MANAGER_WORKER_HOST', _DEFAULT_MANAGER_HOST)
    MANAGER_WORKER_PORT = int(os.environ.get('MANAGER_WORKER_PORT', 65433))

    # Worker Health Configuration
    WORKER_HEARTBEAT_INTERVAL = int(os.environ.get('WORKER_HEARTBEAT_INTERVAL', 30))
    WORKER_HEARTBEAT_TIMEOUT = 90

    # Email Configuration for IONOS
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.ionos.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or (f"Phonos Service <{MAIL_USERNAME}>" if MAIL_USERNAME else None)

    # Google OAuth Configuration (optional — set GOOGLE_CLIENT_ID to enable)
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
    GOOGLE_DISCOVERY_URL = 'https://accounts.google.com/.well-known/openid-configuration'

    # Stripe Configuration
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

    # Stripe Price IDs (these should be created in your Stripe Dashboard)
    STRIPE_PREMIUM_PRICE_ID = os.environ.get('STRIPE_PREMIUM_PRICE_ID')  # Monthly Premium subscription
    STRIPE_MANAGED_PREMIUM_PRICE_ID = os.environ.get('STRIPE_MANAGED_PREMIUM_PRICE_ID')
    STRIPE_ENTERPRISE_PRICE_ID = os.environ.get('STRIPE_ENTERPRISE_PRICE_ID')

    # Stripe Payment Link (for Premium plan)
    STRIPE_PREMIUM_PAYMENT_LINK = os.environ.get('STRIPE_PREMIUM_PAYMENT_LINK', '')

    # Promo Codes (comma-separated list of valid codes that grant 30 days Premium)
    # Example: PROMO_CODES="WELCOME2024,BETA50,LAUNCH100"
    PROMO_CODES = os.environ.get('PROMO_CODES', '')

    # Redis Pub/Sub channel for job status updates
    REDIS_JOB_STATUS_CHANNEL = "pubsub:job_status"

    # Redis queue for completed job results
    REDIS_JOB_RESULTS_QUEUE = "queue:job_results"

    # Maximum sequence length for job submission (model-specific)
    MAX_SEQUENCE_LENGTH_ESMFOLD = 2048  # 2048 amino acids for ESMFold
    MAX_SEQUENCE_LENGTH_ALPHAFOLD2 = 2048  # 2048 amino acids for AlphaFold2
    MAX_SEQUENCE_LENGTH_FOLD_DOCK = 2048  # 2048 amino acids for Fold+Dock Pipeline
    MAX_SEQUENCE_LENGTH_DIFFDOCK = 1_000_000

    # Allowed file extensions for project uploads
    PROJECT_ALLOWED_EXTENSIONS = {
        "txt", "fasta", "fa", "faa", "py", "csv", "tsv", "json", "yaml", "yml",
        "pdb", "cif", "mmcif", "mol2", "sdf", "smi", "zip", "gz", "tar",
        "png", "jpg", "jpeg", "gif", "bmp", "pdf",
        "nb", "wl", "wls", "m", "mx", "wxf", "wdx",
    }
    # Allowed file extensions for image uploads (e.g., profile pictures)
    IMAGE_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}

    # Team Roles
    TEAM_ROLE_OWNER = "Owner"
    TEAM_ROLE_ADMIN = "Admin"
    TEAM_ROLE_MEMBER = "Member"
    TEAM_ROLE_BILLING = "Billing"
    TEAM_MUTABLE_ROLES = {TEAM_ROLE_ADMIN, TEAM_ROLE_MEMBER, TEAM_ROLE_BILLING}

    # Project Roles
    PROJECT_ROLE_MANAGER = "Project Manager"
    PROJECT_ROLE_CONTRIBUTOR = "Project Contributor"
    PROJECT_ROLE_VIEWER = "Project Viewer"
    PROJECT_ROLE_STUDENT = "Project Student"
    PROJECT_MUTABLE_ROLES = {PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER}

    # URL Generation Settings for background tasks (None = match any incoming Host/port in local/LAN deployments)
    SERVER_NAME = os.environ.get('SERVER_NAME') or None
    APPLICATION_ROOT = os.environ.get('APPLICATION_ROOT', '/')
    PREFERRED_URL_SCHEME = os.environ.get('PREFERRED_URL_SCHEME', 'http')

    # --- Publisher Node Verification ---
    # Populated dynamically from auto-provisioned deployment-local keys (.data/worker-keys)
    # and optional PHONOS_PUBLISHER_KEYS_FILE so zero RSA keys are tracked in Git.
    PUBLISHER_NODES: dict[str, str] = {}
    try:
        from .local_worker_keys import load_local_publisher_public_keys as _load_pub_keys
    except ImportError:
        try:
            from local_worker_keys import load_local_publisher_public_keys as _load_pub_keys
        except ImportError:
            _load_pub_keys = None
    if _load_pub_keys is not None:
        try:
            PUBLISHER_NODES.update(_load_pub_keys())
        except Exception:
            pass

    @classmethod
    def get_model_definition(cls, model_id: str) -> dict:
        for model in cls.SUPPORTED_MODELS:
            if model.get('id') == model_id:
                return model
        return None

    @classmethod
    def get_available_models(cls) -> list[dict]:
        return [model for model in cls.SUPPORTED_MODELS if model.get('available', True)]

    # Landing-page table (templates/partials/_model_table.html): id -> (group, name, input -> output).
    # Rows appear only while the model is active and not retired in SUPPORTED_MODELS.
    _MODEL_TABLE = (
        ('type-9', 'structure', 'Boltz-2', 'protein, DNA, RNA, ligand \u2192 complex'),
        ('openfold3', 'structure', 'OpenFold3', 'sequences \u2192 structure'),
        ('type-12', 'design', 'RFdiffusion', 'target PDB \u2192 backbones'),
        ('proteinmpnn-re', 'design', 'ProteinMPNN', 'backbone PDB \u2192 sequences'),
        ('diffdock-re', 'docking', 'DiffDock', 'protein + SMILES \u2192 poses'),
        ('evo2', 'genomics', 'Evo 2', 'DNA prompt \u2192 DNA'),
        ('genmol', 'chemistry', 'GenMol', 'SMILES fragments \u2192 molecules'),
        ('molmim', 'chemistry', 'MolMIM', 'seed SMILES \u2192 molecules'),
    )

    @classmethod
    def public_model_table(cls) -> list[dict]:
        rows = []
        for model_id, group, name, io in cls._MODEL_TABLE:
            model = cls.get_model_definition(model_id)
            if model and model.get('is_active') and not model.get('retired'):
                rows.append({'id': model_id, 'group': group, 'name': name, 'io': io})
        return rows

    @classmethod
    def model_ui_state(cls, model: dict) -> str:
        """'retired' | 'offline' | 'active' for a (possibly runtime-merged) model dict."""
        if model.get('retired'):
            return 'retired'
        if model.get('is_active') is False or model.get('available') is False:
            return 'offline'
        return 'active'

    @classmethod
    def public_model_view(cls, model: dict) -> dict:
        """Registry fields the UI needs, added to a model dict (copy)."""
        out = dict(model)
        state = cls.model_ui_state(out)
        out['ui_state'] = state
        out['retired'] = bool(out.get('retired'))
        out['degraded'] = state == 'active' and (
            out.get('status_override') == 'degraded' or out.get('runtime_status') == 'degraded')
        out['offline_reason'] = out.get('offline_reason') or ''
        if state == 'offline' and not out['offline_reason']:
            out['offline_reason'] = 'Currently offline'
        out['replacement'] = out.get('replacement')
        out['hosted_by'] = dict(out.get('hosted_by') or cls.DEFAULT_HOSTED_BY)
        out['credit_cost'] = cls.calculate_adaptive_credit_cost(out.get('id', ''))
        return out

    @classmethod
    def calculate_adaptive_credit_cost(cls, model_id: str, sequence: str = '', custom_params: dict = None) -> int:
        """
        Flat 20-credit cost per job regardless of model, sequence, or parameters.
        """
        return 20

    # ------------------------------------------------------------------
    # Multi-stage pipelines
    # ------------------------------------------------------------------
    # A pipeline chains models from different functional categories
    # (e.g. Fold -> Dock, DNAGen -> Fold -> Dock) and charges a single flat
    # fee regardless of how many internal jobs the matrix expansion produces.
    PIPELINE_FLAT_CREDITS = 100        # flat charge per pipeline, once at creation
    PIPELINE_STAGE_TOPN = 10           # carry forward only the top-N outputs per stage
    PIPELINE_MAX_TOTAL_JOBS = 200      # hard ceiling on internal jobs across the pipeline
    PIPELINE_MAX_STAGES = 5            # max chain length

    # Map a model's declared `task` to a pipeline category.
    PIPELINE_TASK_CATEGORY = {
        'protein_folding': 'folding',
        'structure_prediction': 'folding',
        'protein_complex_folding': 'folding',
        'enhanced_structure_prediction': 'folding',
        'molecular_docking': 'docking',
        'molecule_generation': 'molecule_gen',
        'dna_generation': 'dna_gen',
        'dna_sequence_generation': 'dna_gen',
        'protein_design': 'protein_design',
        'fold_and_dock': 'integrated',
        'metal_binding_prediction': 'analysis',
        'sequence_analysis': 'analysis',
    }

    # Which categories may immediately follow a given category in a chain.
    # Docking / integrated / analysis are terminal (no downstream stage).
    PIPELINE_NEXT_CATEGORIES = {
        'dna_gen': ['folding'],
        'molecule_gen': ['docking'],
        'folding': ['docking', 'protein_design'],
        'protein_design': ['folding', 'docking'],
        'integrated': [],
        'docking': [],
        'analysis': [],
    }

    @classmethod
    def pipeline_category_for_model(cls, model_id: str) -> str:
        """Return the pipeline category for a model id, or None if unknown."""
        model = cls.get_model_by_id(model_id) if hasattr(cls, 'get_model_by_id') else None
        if not model:
            for m in cls.SUPPORTED_MODELS:
                if m.get('id') == model_id:
                    model = m
                    break
        if not model:
            return None
        return cls.PIPELINE_TASK_CATEGORY.get(model.get('task'))

    # Placeholder for future worker-specific configuration.
