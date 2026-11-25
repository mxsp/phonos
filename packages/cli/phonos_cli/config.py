"""Configuration management for Phonos CLI."""
import os
from pathlib import Path
import yaml


CONFIG_DIR = Path.home() / ".phonos"
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def ensure_config_dir():
    """Ensure configuration directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    """Load configuration from file."""
    if not CONFIG_FILE.exists():
        return {
            "api_key": None,
            "endpoint": "https://phonos.fyreflysystems.de",
        }

    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f) or {}


def save_config(config):
    """Save configuration to file."""
    ensure_config_dir()

    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def get_api_key():
    """Get API key from config or environment."""
    # Check environment variable first
    api_key = os.environ.get("PHONOS_API_KEY")
    if api_key:
        return api_key

    # Check config file
    config = load_config()
    return config.get("api_key")


def get_endpoint():
    """Get API endpoint from config or environment."""
    # Check environment variable first
    endpoint = os.environ.get("PHONOS_ENDPOINT")
    if endpoint:
        return endpoint

    # Check config file
    config = load_config()
    return config.get("endpoint", "https://phonos.fyreflysystems.de")


def set_api_key(api_key):
    """Set API key in configuration."""
    config = load_config()
    config["api_key"] = api_key
    save_config(config)


def set_endpoint(endpoint):
    """Set API endpoint in configuration."""
    config = load_config()
    config["endpoint"] = endpoint
    save_config(config)
