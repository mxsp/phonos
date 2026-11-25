"""Upload helpers and constants."""
from __future__ import annotations

from typing import Set

from config import Config


PROJECT_ALLOWED_EXTENSIONS: Set[str] = Config.PROJECT_ALLOWED_EXTENSIONS
IMAGE_ALLOWED_EXTENSIONS: Set[str] = Config.IMAGE_ALLOWED_EXTENSIONS


def allowed_file(filename: str, allowed_extensions: Set[str]) -> bool:
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in allowed_extensions
