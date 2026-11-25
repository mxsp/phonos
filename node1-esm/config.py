# File: config.py

import os
from dotenv import load_dotenv, find_dotenv

from host_resolution import resolve_host

# Load environment variables from .env file
load_dotenv(find_dotenv())

class Config:
    """Application configuration settings."""
    # Secret key for Flask session management
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        raise ValueError("SECRET_KEY must be set in the .env file or environment variables.")

    # Development mode - SECURE DEFAULT: False unless explicitly enabled
    DEV_MODE = os.environ.get('DEV', 'false').lower() == 'true'

    # Beta mode - restrict registration to @hs-anhalt.de emails (bypassed in dev mode)
    BETA_MODE = os.environ.get('BETA', 'false').lower() == 'true'

    # Security Headers
    SECURE_HEADERS = {
        'X-Frame-Options': 'DENY',
        'X-Content-Type-Options': 'nosniff',
        'X-XSS-Protection': '1; mode=block',
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self';",
    }

    # TLS/SSL Configuration
    REQUIRE_TLS = os.environ.get('REQUIRE_TLS', 'true').lower() == 'true'
    TLS_VERSION = os.environ.get('TLS_VERSION', 'TLSv1.2')

    # Session Security
    SESSION_COOKIE_SECURE = not DEV_MODE  # HTTPS only in production
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour

    # Database file path
    DATABASE_FILE = 'phonos.db'

    # Redis Configuration
    REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
    REDIS_DB = int(os.environ.get('REDIS_DB', 0))
    REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD')  # Optional password
    REDIS_SSL = os.environ.get('REDIS_SSL', 'false').lower() == 'true'
    REDIS_SSL_CERT_REQS = 'required' if REDIS_SSL else None

    # Encryption key for manager-worker communication
    ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY must be set in the .env file.")
    if len(ENCRYPTION_KEY) < 32:
        raise ValueError("ENCRYPTION_KEY must be at least 32 characters for security.")

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

    # Repository root for this worker node; override with PHONOS_DIR.
    _PROJECT_ROOT = os.environ.get('PHONOS_DIR', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Base directory for all user-generated job outputs. Defaults to a
    # PROJECT_ROOT-relative directory; set JOB_OUTPUT_DIR (e.g. a mounted data
    # disk) in production.
    JOB_OUTPUT_DIR = os.environ.get('JOB_OUTPUT_DIR', os.path.join(_PROJECT_ROOT, 'job_outputs'))

    # Optional external ESMFold runner configuration
    ESMFOLD_LAUNCH_SCRIPT = os.environ.get('ESMFOLD_LAUNCH_SCRIPT')
    ESMFOLD_MODEL_NAME = os.environ.get('ESMFOLD_MODEL_NAME', 'esm2_t36_3B_UR50D')
    ESMFOLD_TIMEOUT = int(os.environ.get('ESMFOLD_TIMEOUT', 1800))

    # List of models the platform supports
    SUPPORTED_MODELS = [
        {
            'id': 'type-0',
            'node_id': 'node-1',
            'name': 'Node 1 · ESMFold (Protein Structure)',
            'short_name': 'ESMFold',
            'description': 'Predict 3D structures directly from amino-acid sequences using facebook/esmfold_v1.',
            'task': 'protein_folding',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.pdb',
            'available': True,
            'credit_cost': 10,
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
            'description': 'Predict protein-ligand binding poses using DiffDock. Supports protein sequences/PDB and ligand SMILES/SDF inputs. CPU-only mode.',
            'task': 'molecular_docking',
            'input_modes': ['sequence', 'file'],
            'output_extension': '.pdb',
            'available': True,
            'credit_cost': 15,
            'custom_fields': [
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
    ]

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
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', MAIL_USERNAME)

    # Stripe Configuration
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

    # Stripe Price IDs (these should be created in your Stripe Dashboard)
    STRIPE_PREMIUM_PRICE_ID = os.environ.get('STRIPE_PREMIUM_PRICE_ID')  # Monthly Premium subscription
    STRIPE_MANAGED_PREMIUM_PRICE_ID = os.environ.get('STRIPE_MANAGED_PREMIUM_PRICE_ID')
    STRIPE_ENTERPRISE_PRICE_ID = os.environ.get('STRIPE_ENTERPRISE_PRICE_ID')

    # Promo Codes (comma-separated list of valid codes that grant 30 days Premium)
    # Example: PROMO_CODES="WELCOME2024,BETA50,LAUNCH100"
    PROMO_CODES = os.environ.get('PROMO_CODES', '')

    # Redis Pub/Sub channel for job status updates
    REDIS_JOB_STATUS_CHANNEL = "pubsub:job_status"

    # Redis queue for completed job results
    REDIS_JOB_RESULTS_QUEUE = "queue:job_results"

    # Maximum sequence length for job submission (model-specific)
    MAX_SEQUENCE_LENGTH_ESMFOLD = None  # No limit for ESMFold
    MAX_SEQUENCE_LENGTH_DIFFDOCK = 1_000_000

    # Allowed file extensions for project uploads
    PROJECT_ALLOWED_EXTENSIONS = {
        "txt", "fasta", "fa", "faa", "py", "csv", "tsv", "json", "yaml", "yml",
        "pdb", "cif", "mmcif", "mol2", "sdf", "smi", "zip", "gz", "tar",
        "png", "jpg", "jpeg", "gif", "bmp", "pdf",
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
    PROJECT_MUTABLE_ROLES = {PROJECT_ROLE_CONTRIBUTOR, PROJECT_ROLE_VIEWER}

    # URL Generation Settings for background tasks
    SERVER_NAME = os.environ.get('SERVER_NAME', 'localhost:5000')
    APPLICATION_ROOT = os.environ.get('APPLICATION_ROOT', '/')
    PREFERRED_URL_SCHEME = os.environ.get('PREFERRED_URL_SCHEME', 'http')

    # --- Publisher Node Verification ---
    # A dictionary mapping the official worker node IDs to their public keys for signature verification.
    # This ensures that only authorized, official nodes can submit results for these types.
    PUBLISHER_NODES = {
        "publisher-node-type-0": """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtQhnzHzRM0VhYfIBjAaM
13+wznJWQ7f5szyoqDhhPbX1QHjmKWRTWba3vhnDOsdBbg4Xeo2xwpS5bz0tAbYA
1rqcKPpzC/i5Fgz+xbQQbKdWSFIGDSJVu6u4tt4oAJCj76LRr0OMcl4/TiYiIzHz
SFKGph1k/P/+yf0lmrppo0ZO363SqPq1ZK7CBaAVaKh6FM5Avym7xJCm5itO5eoz
37PHr6J/aE61kMv9pcXfng8/FLSd8ThgQTfwYFXsU5GEo/mq3uTgpwgiSWztUxVw
PVMLK7YBF2enjc22KWAc2rdUs4TbnkCk1NG+yOwoGbACebQUJSzi/q/d4zzdtyPp
YQIDAQAB
-----END PUBLIC KEY-----""",
        "publisher-node-type-3": """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtQhnzHzRM0VhYfIBjAaM
13+wznJWQ7f5szyoqDhhPbX1QHjmKWRTWba3vhnDOsdBbg4Xeo2xwpS5bz0tAbYA
1rqcKPpzC/i5Fgz+xbQQbKdWSFIGDSJVu6u4tt4oAJCj76LRr0OMcl4/TiYiIzHz
SFKGph1k/P/+yf0lmrppo0ZO363SqPq1ZK7CBaAVaKh6FM5Avym7xJCm5itO5eoz
37PHr6J/aE61kMv9pcXfng8/FLSd8ThgQTfwYFXsU5GEo/mq3uTgpwgiSWztUxVw
PVMLK7YBF2enjc22KWAc2rdUs4TbnkCk1NG+yOwoGbACebQUJSzi/q/d4zzdtyPp
YQIDAQAB
-----END PUBLIC KEY-----"""
    }

    @classmethod
    def get_model_definition(cls, model_id: str) -> dict:
        for model in cls.SUPPORTED_MODELS:
            if model.get('id') == model_id:
                return model
        return None

    @classmethod
    def get_available_models(cls) -> list[dict]:
        return [model for model in cls.SUPPORTED_MODELS if model.get('available', True)]

    @classmethod
    def calculate_adaptive_credit_cost(cls, model_id: str, sequence: str = '', custom_params: dict = None) -> int:
        """
        Calculate adaptive credit cost based on model, sequence length, and custom parameters.

        Args:
            model_id: The model identifier
            sequence: The input sequence (for length-based calculation)
            custom_params: Custom parameters that affect compute time

        Returns:
            Calculated credit cost (minimum 5 credits)
        """
        model_info = cls.get_model_definition(model_id)
        if not model_info:
            return 10  # Default fallback

        base_cost = model_info.get('credit_cost', 10)

        # Only apply adaptive pricing for models that support it
        if model_id == 'type-0':  # ESMFold node
            # Base cost starts at 5 credits
            cost = 5

            # Add cost based on sequence length (longer = more compute)
            seq_length = len(sequence) if sequence else 0
            if seq_length > 0:
                # Add 1 credit per 100 amino acids
                length_cost = max(1, seq_length // 100)
                cost += length_cost

            # Add cost based on num_recycles parameter
            if custom_params and isinstance(custom_params, dict):
                num_recycles = custom_params.get('num_recycles', 4)
                if isinstance(num_recycles, (int, float)):
                    # Each recycle above 4 (default) adds 1 credit
                    # Each recycle below 4 reduces by 0.5 credits
                    recycle_diff = int(num_recycles) - 4
                    if recycle_diff > 0:
                        cost += recycle_diff
                    elif recycle_diff < 0:
                        cost = max(5, cost + (recycle_diff * 0.5))

            # Ensure minimum cost
            return max(5, int(cost))

        # For other models, use the base cost
        return base_cost

    # Placeholder for future worker-specific configuration.
