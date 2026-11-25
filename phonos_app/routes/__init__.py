"""Aggregated route registrations for the Phonos application."""

from .pages import *  # noqa: F401,F403
from .api import *  # noqa: F401,F403 - Shared helpers only
from .api_tags import *  # noqa: F401,F403 - Tag management routes
from .api_accounts import *  # noqa: F401,F403 - Account management routes
from . import api_lab_accounts  # noqa: F401 - Gimel-style Lab Account Manager, Setup Wizard & Updater
from .api_admin import *  # noqa: F401,F403 - Admin and stats routes
from .api_trusted_tester import *  # noqa: F401,F403 - Trusted Tester Program (apply, review, graduate)
from .api_teams import *  # noqa: F401,F403 - Team and organization routes
from .api_projects import *  # noqa: F401,F403 - Project management routes
from .api_jobs import *  # noqa: F401,F403 - Job management routes
from .api_files import *  # noqa: F401,F403 - File management routes
from .api_settings import *  # noqa: F401,F403 - Settings and notifications routes
from .api_tickets import *  # noqa: F401,F403 - Help desk tickets routes
from .api_shared import *  # noqa: F401,F403 - Shared links routes
from .api_analytics import *  # noqa: F401,F403 - Analytics and budget routes
from .api_messages import *  # noqa: F401,F403 - Project messages routes
from .api_calls import *  # noqa: F401,F403 - Video/voice call routes
from .api_users import *  # noqa: F401,F403 - User profile and settings routes
from .api_search import *  # noqa: F401,F403 - Search endpoints for webapp rework
from .api_conversations import *  # noqa: F401,F403 - AI conversation management routes
from .api_flows import register_flow_routes  # noqa: F401 - Flow management routes
from .api_tutorial import *  # noqa: F401,F403 - Tutorial/onboarding routes
from .api_keys_management import *  # noqa: F401,F403 - API key management routes
from .api_activity import *  # noqa: F401,F403 - Activity feed routes
from .api_export import *  # noqa: F401,F403 - Project export routes
from .api_evo2 import *  # noqa: F401,F403 - Evo2 DNA generation routes
from .api_boltz2 import *  # noqa: F401,F403 - Boltz2 protein structure prediction routes
from .api_openfold3 import *  # noqa: F401,F403 - OpenFold3 structure prediction routes
from .api_sequence import *  # noqa: F401,F403 - Sequence analysis and manipulation routes
from .api_msa_search import *  # noqa: F401,F403 - MSA-Search utility routes (NVIDIA NIM ColabFold)
from .api_esmfold_re import *  # noqa: F401,F403 - ESMFold-RE protein structure prediction (NVIDIA NIM)
from .api_diffdock_re import *  # noqa: F401,F403 - DiffDock-RE molecular docking (NVIDIA NIM)
from .api_proteinmpnn_re import *  # noqa: F401,F403 - ProteinMPNN-RE sequence design (NVIDIA NIM)
from .api_molmim import *  # noqa: F401,F403 - MolMIM molecule generation (NVIDIA NIM)
from .api_alphafold2_re import *  # noqa: F401,F403 - AlphaFold2-RE structure prediction (NVIDIA NIM)
from .api_tools import *  # noqa: F401,F403 - Notebook IDE and IoT Device Bridge routes
from .api_model_io import *  # noqa: F401,F403 - Output->input chaining (imports helpers from api_files)
from . import api_model_registry  # noqa: F401 - Model registry: cost estimate, app version
from . import api_model_providers  # noqa: F401 - Unified encrypted model provider keys & endpoint overrides
# from .public_api import *  # noqa: F401,F403 - Public API endpoints - COMMENTED OUT to avoid duplicate registration
# Note: api_extensions routes are registered separately due to import order dependencies
from .api_user_models import register_user_model_routes  # noqa: F401 - User-defined models routes
from .api_bunches import register_bunch_routes  # noqa: F401 - Job bunches/batch processing routes
from .api_batches import register_batch_routes  # noqa: F401 - Batch jobs hierarchy routes
from .api_notebooks import register_notebook_routes  # noqa: F401 - Notebook IDE routes
from .api_rfdiffusion import register_rfdiffusion_routes  # noqa: F401 - RFDiffusion protein design routes
from .api_pipelines import register_pipeline_routes  # noqa: F401 - Multi-stage model pipeline routes
from . import api_bridge  # noqa: F401 - Bridge API for user-hosted workers
from . import api_external_bridge  # noqa: F401 - External API bridge for worker-based processing
from . import api_external_v1  # noqa: F401 - External API v1 - Complete REST API for external integration
from . import api_project_v1  # noqa: F401 - Project API v1 - project-scoped REST API with dual-use key encryption
from . import api_iot_bridge  # noqa: F401 - IoT Device Bridge v2 API
from .promo_routes import register_promo_routes  # noqa: F401
from . import oauth  # noqa: F401 — Google OAuth login/register routes
from .api_project_history import register_project_history_routes  # noqa: F401 - Project history/log routes
from .api_quantum import register_quantum_routes  # noqa: F401 - Quantum Settings & execution routes
from .api_hpc import register_hpc_routes  # noqa: F401 - HPC compute cluster routes


# Register promo routes (stripe routes are registered in app.py to avoid circular imports)
# User model routes will be registered in app.py after app is fully initialized
try:
    from ..app import app
    register_promo_routes(app)
    register_flow_routes(app)
    register_bunch_routes(app)
    register_batch_routes(app)
    register_notebook_routes(app)
    register_rfdiffusion_routes(app)
    register_pipeline_routes(app)
    register_project_history_routes(app)
    register_quantum_routes(app)
    register_hpc_routes(app)
    # Phonos Copilot: additive tables and its own /api/agent + /agent surface.
    # A failure here must never prevent the rest of Phonos from starting.
    try:
        from ..agent import register_agent_routes
        register_agent_routes(app)
    except Exception as agent_exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("Phonos Copilot routes unavailable: %s", agent_exc)
    # Don't register user model routes here - it causes circular import
    # They will be registered in app.py after initialization
except ImportError:
    # During initialization, app may not be available yet
    pass

__all__ = [name for name in globals() if not name.startswith('_')]
