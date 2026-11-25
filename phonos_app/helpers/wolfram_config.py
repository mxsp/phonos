"""
Wolfram Language Configuration Manager

Manages Wolfram Engine activation credentials.
Credentials are read from environment variables, which should be
set in the host environment (e.g. via .env or the systemd service file).
"""

import os
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def get_wolfram_config() -> Optional[Dict[str, Any]]:
    """Get Wolfram Engine activation credentials from environment."""
    license_key = os.environ.get('WOLFRAM_LICENSE', '').strip()
    username = os.environ.get('WOLFRAM_USERNAME', '').strip()
    password = os.environ.get('WOLFRAM_PASSWORD', '').strip()

    if license_key:
        logger.info("Wolfram activation via license key configured")
        return {
            'license': license_key,
        }

    if username and password:
        logger.info("Wolfram activation via username/password configured")
        return {
            'username': username,
            'password': password,
        }

    logger.warning("Wolfram credentials not configured. "
                   "Set WOLFRAM_LICENSE or WOLFRAM_USERNAME + WOLFRAM_PASSWORD in environment.")
    return None
