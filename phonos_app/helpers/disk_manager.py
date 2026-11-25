"""
Disk Management and Load Balancing System for Phonos.

This module provides:
- Multi-disk detection and discovery
- Space-based load balancing for job outputs
- Health monitoring and alerts
- Graceful handling of full/unavailable disks
"""
from __future__ import annotations

import os
import shutil
import json
import logging
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

# Resolve portable, env-overridable storage locations from the central config.
# Falls back to sensible PROJECT_ROOT-relative defaults if Config is unavailable
# (e.g. when this module is imported by tooling outside the app).
try:  # pragma: no cover - import path depends on how the app is launched
    from production.config import Config as _Config
except Exception:  # noqa: BLE001
    try:
        from config import Config as _Config
    except Exception:  # noqa: BLE001
        _Config = None

if _Config is not None:
    _PRIMARY_JOB_DIR = _Config.JOB_OUTPUT_DIR
    _FALLBACK_JOB_DIR = _Config.JOB_OUTPUT_DIR_FALLBACK
    _BACKUP_DIRS = list(_Config.BACKUP_DIRS)
else:
    _root = os.environ.get(
        'PHONOS_DIR', os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    _PRIMARY_JOB_DIR = os.environ.get('JOB_OUTPUT_DIR', os.path.join(_root, 'job_outputs'))
    _FALLBACK_JOB_DIR = os.environ.get('JOB_OUTPUT_DIR_FALLBACK', os.path.join(_root, 'job_outputs_fallback'))
    _BACKUP_DIRS = [os.path.join(_root, 'backups')]


@dataclass
class DiskInfo:
    """Information about a storage disk."""
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float
    is_mounted: bool
    is_writable: bool
    priority: int  # Lower = preferred for new data
    description: str = ""

    @property
    def free_gb(self) -> float:
        """Return free space in gigabytes."""
        return self.free_bytes / (1024**3)

    @property
    def total_gb(self) -> float:
        """Return total space in gigabytes."""
        return self.total_bytes / (1024**3)

    @property
    def used_gb(self) -> float:
        """Return used space in gigabytes."""
        return self.used_bytes / (1024**3)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d['free_gb'] = round(self.free_gb, 2)
        d['total_gb'] = round(self.total_gb, 2)
        d['used_gb'] = round(self.used_gb, 2)
        return d


class DiskManager:
    """
    Manages multiple storage disks with space-based load balancing.

    Configuration is loaded from the JSON file pointed to by
    PHONOS_DISK_CONFIG (default /etc/phonos/disk_config.json). When absent, the
    defaults are derived from the central config (JOB_OUTPUT_DIR,
    JOB_OUTPUT_DIR_FALLBACK, BACKUP_DIRS), e.g.:
    {
        "disks": [
            {"path": "<JOB_OUTPUT_DIR>", "priority": 1, "min_free_percent": 10},
            {"path": "<JOB_OUTPUT_DIR_FALLBACK>", "priority": 2, "min_free_percent": 15}
        ],
        "alert_threshold_percent": 85,
        "critical_threshold_percent": 95
    }
    """

    CONFIG_PATH = os.environ.get("PHONOS_DISK_CONFIG", "/etc/phonos/disk_config.json")
    DEFAULT_CONFIG = {
        "disks": [
            {"path": _PRIMARY_JOB_DIR, "priority": 1, "min_free_percent": 10, "description": "Primary storage"},
            {"path": _FALLBACK_JOB_DIR, "priority": 2, "min_free_percent": 15, "description": "Fallback storage"}
        ],
        "alert_threshold_percent": 85,
        "critical_threshold_percent": 95,
        "backup_paths": list(_BACKUP_DIRS)
    }

    def __init__(self, config_path: Optional[str] = None):
        """Initialize DiskManager with optional config path override."""
        self.config_path = config_path or self.CONFIG_PATH
        self.config = self._load_config()
        self._disk_cache: Dict[str, DiskInfo] = {}
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = 30  # Refresh disk info every 30 seconds

    def _load_config(self) -> dict:
        """Load disk configuration from file or use defaults."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    config = json.load(f)
                    logger.info(f"Loaded disk config from {self.config_path}")
                    return config
        except (IOError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load disk config from {self.config_path}: {e}, using defaults")
        return self.DEFAULT_CONFIG.copy()

    def reload_config(self) -> None:
        """Reload configuration from file."""
        self.config = self._load_config()
        self._disk_cache.clear()
        self._cache_timestamp = None
        logger.info("Disk configuration reloaded")

    def _get_disk_info(self, path: str, priority: int = 99, description: str = "") -> Optional[DiskInfo]:
        """Get information about a disk at the given path."""
        try:
            # Try to create directory if it doesn't exist
            if not os.path.exists(path):
                try:
                    os.makedirs(path, exist_ok=True)
                    logger.info(f"Created disk path: {path}")
                except OSError as e:
                    logger.warning(f"Could not create path {path}: {e}")
                    return None

            stat = shutil.disk_usage(path)

            # Check if writable
            is_writable = os.access(path, os.W_OK)

            # Check if actually mounted (not just on root)
            # A path is considered "mounted" if it's on /mnt/, or if it's on the root filesystem
            parent = os.path.dirname(path.rstrip('/'))
            is_mounted = (
                os.path.ismount(parent) or
                path.startswith('/mnt/') or
                path.startswith('/home/')
            )

            return DiskInfo(
                path=path,
                total_bytes=stat.total,
                used_bytes=stat.used,
                free_bytes=stat.free,
                percent_used=(stat.used / stat.total) * 100 if stat.total > 0 else 100,
                is_mounted=is_mounted,
                is_writable=is_writable,
                priority=priority,
                description=description
            )
        except OSError as e:
            logger.error(f"Failed to get disk info for {path}: {e}")
            return None

    def get_all_disks(self, refresh: bool = False) -> List[DiskInfo]:
        """Get information about all configured disks."""
        now = datetime.now()

        # Use cache if valid and not forcing refresh
        if not refresh and self._cache_timestamp:
            age = (now - self._cache_timestamp).total_seconds()
            if age < self._cache_ttl_seconds and self._disk_cache:
                return list(self._disk_cache.values())

        disks = []
        for disk_config in self.config.get("disks", []):
            path = disk_config.get("path")
            priority = disk_config.get("priority", 99)
            description = disk_config.get("description", "")
            if path:
                info = self._get_disk_info(path, priority, description)
                if info:
                    disks.append(info)
                    self._disk_cache[path] = info

        self._cache_timestamp = now
        return disks

    def get_disk_paths(self) -> List[str]:
        """Get all configured disk paths (for path validation)."""
        return [d.get("path") for d in self.config.get("disks", []) if d.get("path")]

    def select_disk_for_output(self, required_bytes: int = 0) -> Optional[str]:
        """
        Select the best disk for storing new job output.

        Strategy:
        1. Filter disks that have enough free space
        2. Filter disks that are mounted and writable
        3. Sort by free space (descending) then priority (ascending)
        4. Return the path of the best disk

        Args:
            required_bytes: Minimum space needed (default 0 for general selection)

        Returns:
            Path to use for job outputs, or None if no suitable disk
        """
        disks = self.get_all_disks()

        if not disks:
            logger.error("No disks configured!")
            return None

        # Filter available disks
        available = []
        for disk in disks:
            if not disk.is_writable:
                logger.debug(f"Disk {disk.path} is not writable, skipping")
                continue

            # Get minimum free percent from config
            disk_config = next(
                (d for d in self.config.get("disks", []) if d.get("path") == disk.path),
                {}
            )
            min_free_pct = disk_config.get("min_free_percent", 10)

            # Calculate minimum free bytes
            min_free_bytes = disk.total_bytes * (min_free_pct / 100)
            actual_required = max(required_bytes, min_free_bytes)

            if disk.free_bytes < actual_required:
                logger.warning(
                    f"Disk {disk.path} has insufficient space: "
                    f"{disk.free_gb:.1f}GB free, need {actual_required / (1024**3):.1f}GB"
                )
                continue

            available.append(disk)

        if not available:
            logger.error("No disks available with sufficient space!")
            # Try to return at least a writable disk even if low on space
            for disk in disks:
                if disk.is_writable:
                    logger.warning(f"Falling back to low-space disk: {disk.path}")
                    return disk.path
            return None

        # Sort by free space (most free first), then by priority (lower first)
        available.sort(key=lambda d: (-d.free_bytes, d.priority))

        selected = available[0]
        logger.info(f"Selected disk {selected.path} with {selected.free_gb:.1f}GB free")
        return selected.path

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of all disks."""
        disks = self.get_all_disks(refresh=True)
        alert_threshold = self.config.get("alert_threshold_percent", 85)
        critical_threshold = self.config.get("critical_threshold_percent", 95)

        status = {
            "overall": "healthy",
            "disks": [],
            "alerts": [],
            "timestamp": datetime.now().isoformat(),
            "config_path": self.config_path
        }

        for disk in disks:
            disk_status = {
                "path": disk.path,
                "description": disk.description,
                "total_gb": round(disk.total_gb, 2),
                "used_gb": round(disk.used_gb, 2),
                "free_gb": round(disk.free_gb, 2),
                "percent_used": round(disk.percent_used, 1),
                "is_writable": disk.is_writable,
                "is_mounted": disk.is_mounted,
                "priority": disk.priority,
                "status": "healthy"
            }

            if disk.percent_used >= critical_threshold:
                disk_status["status"] = "critical"
                status["overall"] = "critical"
                status["alerts"].append(
                    f"CRITICAL: {disk.path} is {disk.percent_used:.1f}% full!"
                )
            elif disk.percent_used >= alert_threshold:
                disk_status["status"] = "warning"
                if status["overall"] == "healthy":
                    status["overall"] = "warning"
                status["alerts"].append(
                    f"WARNING: {disk.path} is {disk.percent_used:.1f}% full"
                )

            if not disk.is_writable:
                disk_status["status"] = "error"
                status["overall"] = "error"
                status["alerts"].append(f"ERROR: {disk.path} is not writable!")

            status["disks"].append(disk_status)

        return status

    def get_backup_paths(self) -> List[str]:
        """Get all configured backup paths."""
        return self.config.get("backup_paths", [])

    def resolve_file_path(self, stored_path: str) -> Optional[str]:
        """
        Resolve a stored file path, checking all configured disks.

        This handles migration from single-disk to multi-disk setup.
        Files that were stored with old paths will still be found.

        Args:
            stored_path: The path as stored in the database

        Returns:
            The actual path to the file, or None if not found
        """
        # Check if path already exists as-is
        if os.path.exists(stored_path):
            return stored_path

        # Extract relative path from stored path
        disk_paths = self.get_disk_paths()
        relative = stored_path

        # Remove any known disk prefix to get relative path
        for disk_path in disk_paths:
            if stored_path.startswith(disk_path):
                relative = stored_path[len(disk_path):].lstrip('/')
                break

        # Also handle legacy paths: deployment-specific prefixes plus the
        # relative 'job_outputs/' form.
        legacy_prefixes = [p.rstrip('/') + '/' for p in (_Config.JOB_OUTPUT_LEGACY_PREFIXES if _Config else [])]
        legacy_prefixes.append('job_outputs/')
        for prefix in legacy_prefixes:
            if stored_path.startswith(prefix):
                relative = stored_path[len(prefix):]
                break

        # Try to find the file on any configured disk
        for disk_path in disk_paths:
            candidate = os.path.join(disk_path, relative)
            if os.path.exists(candidate):
                logger.debug(f"Resolved {stored_path} to {candidate}")
                return candidate

        logger.warning(f"Could not resolve path: {stored_path}")
        return None

    def get_total_storage_stats(self) -> Dict[str, Any]:
        """Get aggregate storage statistics across all disks."""
        disks = self.get_all_disks()

        total_bytes = sum(d.total_bytes for d in disks)
        used_bytes = sum(d.used_bytes for d in disks)
        free_bytes = sum(d.free_bytes for d in disks)

        return {
            "total_disks": len(disks),
            "total_gb": round(total_bytes / (1024**3), 2),
            "used_gb": round(used_bytes / (1024**3), 2),
            "free_gb": round(free_bytes / (1024**3), 2),
            "percent_used": round((used_bytes / total_bytes) * 100, 1) if total_bytes > 0 else 0,
            "disks": [d.to_dict() for d in disks]
        }


# Module-level singleton
_disk_manager: Optional[DiskManager] = None


def get_disk_manager() -> DiskManager:
    """Get the global DiskManager instance."""
    global _disk_manager
    if _disk_manager is None:
        _disk_manager = DiskManager()
    return _disk_manager


def reset_disk_manager() -> None:
    """Reset the global DiskManager instance (useful for testing)."""
    global _disk_manager
    _disk_manager = None
