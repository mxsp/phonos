"""Minimal, dependency-free .env loader and runtime settings.

The watchdog is intentionally *self-contained*: it must keep running and be able
to heal the rest of the stack even when the main application's Python modules are
broken. So it does NOT import anything from ``phonos`` / ``production`` — it reads
the same ``.env`` file directly and keeps its own tiny config surface.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

# Repo root = parent of this package (…/phonos)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_dotenv(path: Path = ENV_PATH) -> Dict[str, str]:
    """Parse a KEY=VALUE .env file into a dict (no os.environ mutation).

    Handles ``export KEY=...``, ``#`` comments, blank lines, and single/double
    quoted values. Values already present in the real environment win, so a
    systemd ``EnvironmentFile`` / explicit ``Environment=`` can override the file.
    """
    parsed: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raw = ""

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        parsed[key] = value

    # Real environment takes precedence over the file.
    for key in list(parsed):
        if key in os.environ:
            parsed[key] = os.environ[key]
    return parsed


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """Connection details + tunables, sourced from .env with safe defaults."""

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = None
    redis_ssl: bool = False

    # --- Postgres ---
    database_url: str = None

    # --- App HTTP ---
    app_url: str = "http://127.0.0.1:5000/"

    env: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        env = load_dotenv()
        return cls(
            redis_host=env.get("REDIS_HOST", "localhost"),
            redis_port=_as_int(env.get("REDIS_PORT"), 6379),
            redis_db=_as_int(env.get("REDIS_DB"), 0),
            redis_password=env.get("REDIS_PASSWORD") or None,
            redis_ssl=_as_bool(env.get("REDIS_SSL"), False),
            database_url=env.get("DATABASE_URL") or None,
            app_url=env.get("WATCHDOG_APP_URL", "http://127.0.0.1:5000/"),
            env=env,
        )
