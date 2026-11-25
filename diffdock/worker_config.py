# Worker Configuration for DiffDock Node

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """DiffDock worker configuration settings."""

    # Redis Configuration
    REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
    REDIS_DB = int(os.environ.get('REDIS_DB', 0))
    REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD')
    REDIS_SSL = os.environ.get('REDIS_SSL', 'false').lower() == 'true'

    # Encryption key for manager-worker communication
    ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY must be set in the .env file.")
    if len(ENCRYPTION_KEY) < 32:
        raise ValueError("ENCRYPTION_KEY must be at least 32 characters for security.")

    # Worker Configuration
    WORKER_TYPE = os.environ.get('WORKER_TYPE', 'type-3')
    WORKER_ID = os.environ.get('WORKER_ID', 'publisher-node-type-3')
    WORKER_HEARTBEAT_INTERVAL = int(os.environ.get('WORKER_HEARTBEAT_INTERVAL', 30))

    # Job Configuration
    MAX_RETRIES = int(os.environ.get('MAX_RETRIES', 3))
    JOB_TIMEOUT = int(os.environ.get('JOB_TIMEOUT', 1200))
    POLL_TIMEOUT = int(os.environ.get('POLL_TIMEOUT', 5))

    # Logging
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_DIR = os.environ.get('LOG_DIR', 'logs')

    # Network Configuration
    REDIS_TIMEOUT = int(os.environ.get('REDIS_TIMEOUT', 10))
    REDIS_CONNECT_TIMEOUT = int(os.environ.get('REDIS_CONNECT_TIMEOUT', 10))
