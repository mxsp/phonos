"""Legacy entrypoint for the Management & Routing Service."""
from __future__ import annotations

import logging
import os
import sys
from typing import Tuple

# Add production directory and parent directory to path for imports
_production_dir = os.path.dirname(os.path.abspath(__file__))
_phonos_dir = os.path.dirname(_production_dir)
sys.path.insert(0, _production_dir)
sys.path.insert(0, _phonos_dir)

PREFERRED_HOSTNAME = os.environ.get("PHONOS_MANAGER_HOST", "127.0.0.1")
FALLBACK_HOSTS: Tuple[str, ...] = ("127.0.0.1", "localhost")

logger = logging.getLogger("phonos.mrs.entrypoint")

from host_resolution import resolve_host


resolved_host = resolve_host(
    PREFERRED_HOSTNAME,
    FALLBACK_HOSTS,
    logger=logger,
    context="MRS bindings",
)
os.environ.setdefault("MANAGER_CLIENT_HOST", resolved_host)
os.environ.setdefault("MANAGER_WORKER_HOST", resolved_host)

from mrs_service import main


if __name__ == "__main__":
    main()
