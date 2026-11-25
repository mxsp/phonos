"""Phonos Project Library — unified access to project objects in notebooks."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ProjectConfig

__version__ = "1.1.0"


class PhonosLibError(RuntimeError):
    """Base exception for phonos_lib."""


class ModelManager:
    def __init__(self, config: ProjectConfig):
        self.config = config

    def list(self) -> List[Dict[str, Any]]:
        try:
            from config import Config
            return [
                {"id": m.get("id"), "name": m.get("name"), "active": bool(m.get("is_active", True))}
                for m in getattr(Config, "SUPPORTED_MODELS", [])
                if m.get("is_active", True) and not m.get("retired")
            ]
        except Exception:
            return []

    def get_available_types(self) -> List[str]:
        return [m["id"] for m in self.list() if m.get("id")]


class FileManager:
    def __init__(self, config: ProjectConfig):
        self.config = config

    def _workspace(self) -> Path:
        ws = os.environ.get("NOTEBOOK_WORKSPACE", "") or os.getcwd()
        return Path(ws)

    def list(self) -> List[str]:
        ws = self._workspace()
        if not ws.exists():
            return []
        return sorted(p.name for p in ws.iterdir() if not p.name.startswith("."))

    def read(self, filename: str) -> str:
        target = (self._workspace() / filename).resolve()
        if not str(target).startswith(str(self._workspace().resolve())):
            raise PhonosLibError("Path traversal blocked.")
        return target.read_text(encoding="utf-8")

    def get_total_size(self) -> int:
        ws = self._workspace()
        if not ws.exists():
            return 0
        return sum(p.stat().st_size for p in ws.rglob("*") if p.is_file())


class DatabaseManager:
    def __init__(self, config: ProjectConfig):
        self.config = config

    def is_connected(self) -> bool:
        return bool(os.environ.get("DATABASE_URL") or Path("phonos.db").exists())

    def list_tables(self) -> List[str]:
        return []


class PhonosProject:
    """Main entry point for accessing project resources in notebooks."""

    def __init__(self, project_id: Optional[int] = None, auto_detect: bool = False):
        self.config = ProjectConfig(project_id, auto_detect=auto_detect)
        self.models = ModelManager(self.config)
        self.files = FileManager(self.config)
        self.database = DatabaseManager(self.config)

    def info(self) -> Dict[str, Any]:
        return {
            "project": self.config.get_project_info() if hasattr(self.config, "get_project_info") else {"project_id": getattr(self.config, "project_id", None)},
            "models": {
                "count": len(self.models.list()),
                "types": self.models.get_available_types(),
            },
            "files": {
                "count": len(self.files.list()),
                "size_mb": round(self.files.get_total_size() / (1024 * 1024), 3),
            },
            "database": {
                "connected": self.database.is_connected(),
                "tables": self.database.list_tables(),
            },
        }

    def __repr__(self) -> str:
        return f"PhonosProject(project_id={getattr(self.config, 'project_id', None)})"


def get_project(project_id: Optional[int] = None) -> PhonosProject:
    return PhonosProject(project_id=project_id, auto_detect=bool(project_id is None and os.environ.get("PHONOS_PROJECT_ID")))


__all__ = [
    "PhonosProject",
    "ModelManager",
    "FileManager",
    "DatabaseManager",
    "ProjectConfig",
    "PhonosLibError",
    "get_project",
]
