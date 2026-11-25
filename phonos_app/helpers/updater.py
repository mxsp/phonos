"""GitHub Release updater connected to mxsp/phonos (Gimel-compatible design).

Checks ``https://api.github.com/repos/<repo>/releases/latest`` (default:
``mxsp/phonos``, override via ``PHONOS_UPDATE_REPO``), compares semantic versions
against ``APP_VERSION``, verifies ``SHA256SUMS.txt`` on release archives, and
applies updates cleanly across binary, git, and container installations.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from ..version import APP_VERSION, DEFAULT_UPDATE_REPO

CACHE_TTL_SECONDS = 5 * 60
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.](.+))?$")

InstallKind = Literal["binary", "docker", "git", "package"]


@dataclass
class ReleaseAsset:
    name: str
    download_url: str
    size: int = 0


@dataclass
class UpdateStatus:
    repo: str
    current_version: str
    latest_version: str | None
    latest_tag: str | None
    update_available: bool
    install_kind: InstallKind
    checked_at: float
    release_name: str = ""
    release_notes: str = ""
    release_url: str = ""
    published_at: str = ""
    matched_asset: ReleaseAsset | None = None
    checksum_url: str = ""
    error: str = ""
    assets: list[ReleaseAsset] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UpdateResult:
    ok: bool
    install_kind: InstallKind
    from_version: str
    to_version: str
    restart_required: bool = False
    restarting: bool = False
    detail: str = ""
    command_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_cache: tuple[str, UpdateStatus] | None = None


def configured_repo() -> str:
    raw = os.environ.get("PHONOS_UPDATE_REPO", DEFAULT_UPDATE_REPO).strip()
    return raw.strip("/") or DEFAULT_UPDATE_REPO


def parse_semver(raw: str | None) -> tuple[int, int, int, int] | None:
    if not raw:
        return None
    m = _SEMVER_RE.match(raw.strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    stable = 0 if m.group(4) else 1
    return (major, minor, patch, stable)


def is_newer_version(current: str, candidate: str | None) -> bool:
    cur = parse_semver(current)
    cand = parse_semver(candidate)
    if cur is None or cand is None:
        return False
    return cand > cur


def _repo_root() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        return root
    return None


def detect_install_kind() -> InstallKind:
    if getattr(sys, "frozen", False):
        return "binary"
    if Path("/.dockerenv").exists() or os.environ.get("PHONOS_IN_DOCKER") == "1":
        return "docker"
    if _repo_root() is not None:
        return "git"
    return "package"


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"phonos-updater/{APP_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("PHONOS_GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get_json(url: str, timeout: float = 12.0) -> dict | list:
    if not url.startswith("https://api.github.com/"):
        raise ValueError("Only https://api.github.com/ endpoints are permitted.")
    req = urllib.request.Request(url, headers=_github_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _match_platform_asset(assets: list[ReleaseAsset]) -> ReleaseAsset | None:
    sys_name = platform.system().lower()
    for a in assets:
        low = a.name.lower()
        if sys_name == "windows" and "windows" in low and low.endswith(".zip") and "setup" not in low:
            return a
        if sys_name == "linux" and "linux" in low and (low.endswith(".tar.gz") or low.endswith(".tgz") or low == "phonos"):
            return a
        if sys_name == "darwin" and ("darwin" in low or "macos" in low):
            return a
    return None


def clear_cache() -> None:
    global _cache
    _cache = None


def check_for_update(*, force: bool = False, repo: str | None = None) -> UpdateStatus:
    global _cache
    target_repo = (repo or configured_repo()).strip("/")
    now = time.time()
    if not force and _cache is not None:
        cached_repo, cached_status = _cache
        if cached_repo == target_repo and (now - cached_status.checked_at) < CACHE_TTL_SECONDS:
            return cached_status

    kind = detect_install_kind()
    api_url = f"https://api.github.com/repos/{target_repo}/releases/latest"
    releases_page = f"https://github.com/{target_repo}/releases"

    try:
        payload = _http_get_json(api_url)
    except urllib.error.HTTPError as exc:
        err_msg = "No GitHub Release published yet." if exc.code == 404 else f"GitHub API error (HTTP {exc.code})."
        status_obj = UpdateStatus(
            repo=target_repo,
            current_version=APP_VERSION,
            latest_version=None,
            latest_tag=None,
            update_available=False,
            install_kind=kind,
            checked_at=now,
            release_url=releases_page,
            error=err_msg,
        )
        if exc.code == 404:
            _cache = (target_repo, status_obj)
        return status_obj
    except Exception as exc:
        return UpdateStatus(
            repo=target_repo,
            current_version=APP_VERSION,
            latest_version=None,
            latest_tag=None,
            update_available=False,
            install_kind=kind,
            checked_at=now,
            release_url=releases_page,
            error=f"Update check failed: {exc}",
        )

    if not isinstance(payload, dict):
        return UpdateStatus(
            repo=target_repo,
            current_version=APP_VERSION,
            latest_version=None,
            latest_tag=None,
            update_available=False,
            install_kind=kind,
            checked_at=now,
            release_url=releases_page,
            error="Unexpected response from GitHub API.",
        )

    tag_name = str(payload.get("tag_name") or "").strip()
    latest_ver = tag_name.lstrip("v") if tag_name else None
    assets: list[ReleaseAsset] = []
    checksum_url = ""
    for raw_asset in payload.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        aname = str(raw_asset.get("name") or "")
        aurl = str(raw_asset.get("browser_download_url") or "")
        asize = int(raw_asset.get("size") or 0)
        if aname and aurl:
            assets.append(ReleaseAsset(name=aname, download_url=aurl, size=asize))
            if aname.upper() == "SHA256SUMS.TXT":
                checksum_url = aurl

    status_obj = UpdateStatus(
        repo=target_repo,
        current_version=APP_VERSION,
        latest_version=latest_ver,
        latest_tag=tag_name or None,
        update_available=is_newer_version(APP_VERSION, latest_ver),
        install_kind=kind,
        checked_at=now,
        release_name=str(payload.get("name") or tag_name or ""),
        release_notes=str(payload.get("body") or "")[:8000],
        release_url=str(payload.get("html_url") or releases_page),
        published_at=str(payload.get("published_at") or ""),
        matched_asset=_match_platform_asset(assets),
        checksum_url=checksum_url,
        assets=assets,
    )
    _cache = (target_repo, status_obj)
    return status_obj


def schedule_restart(delay_seconds: float = 1.2) -> bool:
    kind = detect_install_kind()
    if kind == "docker":
        return False

    def _restart() -> None:
        time.sleep(delay_seconds)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)

    threading.Thread(target=_restart, daemon=True, name="phonos-restart").start()
    return True


def _verify_sha256(file_path: Path, asset_name: str, checksum_url: str) -> None:
    if not checksum_url or not checksum_url.startswith("https://github.com/"):
        return
    req = urllib.request.Request(checksum_url, headers=_github_headers())
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310
        lines = resp.read().decode("utf-8", errors="replace").splitlines()
    expected = None
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            expected = parts[0].lower()
            break
    if not expected:
        return
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest().lower()
    if digest != expected:
        raise RuntimeError(f"SHA-256 mismatch for {asset_name}: expected {expected}, got {digest}")


def apply_update(*, target_tag: str | None = None, auto_restart: bool = True) -> UpdateResult:
    kind = detect_install_kind()
    status_obj = check_for_update(force=True)
    to_ver = (target_tag or status_obj.latest_tag or APP_VERSION).lstrip("v")

    if kind == "docker":
        return UpdateResult(
            ok=False,
            install_kind="docker",
            from_version=APP_VERSION,
            to_version=to_ver,
            restart_required=True,
            restarting=False,
            detail="Container installations are updated on the Docker host.",
            command_hint="docker compose pull && docker compose up -d --build",
        )

    if kind == "git":
        root = _repo_root()
        if root is None:
            raise RuntimeError("Git repository root not found.")
        subprocess.run(["git", "-C", str(root), "fetch", "--tags", "--quiet"], check=True, timeout=60)
        if target_tag:
            subprocess.run(["git", "-C", str(root), "checkout", target_tag], check=True, timeout=60)
        else:
            subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], check=True, timeout=90)
        clear_cache()
        restarting = schedule_restart() if auto_restart else False
        return UpdateResult(
            ok=True,
            install_kind="git",
            from_version=APP_VERSION,
            to_version=to_ver,
            restart_required=True,
            restarting=restarting,
            detail=f"Updated Git checkout in {root}.",
        )

    if kind == "binary" and status_obj.matched_asset:
        asset = status_obj.matched_asset
        if not asset.download_url.startswith("https://github.com/"):
            raise RuntimeError("Refusing non-GitHub release asset URL.")
        with tempfile.TemporaryDirectory(prefix="phonos-upd-") as tmp:
            archive_path = Path(tmp) / asset.name
            req = urllib.request.Request(asset.download_url, headers=_github_headers())
            with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310
                archive_path.write_bytes(resp.read())
            _verify_sha256(archive_path, asset.name, status_obj.checksum_url)
            exe_path = Path(sys.executable).resolve()
            extract_dir = Path(tmp) / "extracted"
            extract_dir.mkdir(parents=True, exist_ok=True)
            if asset.name.lower().endswith(".zip"):
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(extract_dir)
            elif asset.name.lower().endswith((".tar.gz", ".tgz")):
                with tarfile.open(archive_path, "r:gz") as tf:
                    tf.extractall(extract_dir, filter="data")
            candidates = list(extract_dir.rglob(exe_path.name))
            if not candidates:
                raise RuntimeError(f"Updated executable {exe_path.name} not found in archive.")
            backup_path = exe_path.with_suffix(exe_path.suffix + ".old")
            if backup_path.exists():
                backup_path.unlink()
            os.replace(exe_path, backup_path)
            shutil.copy2(candidates[0], exe_path)
            try:
                os.chmod(exe_path, 0o755)
            except OSError:
                pass
        clear_cache()
        restarting = schedule_restart() if auto_restart else False
        return UpdateResult(
            ok=True,
            install_kind="binary",
            from_version=APP_VERSION,
            to_version=to_ver,
            restart_required=True,
            restarting=restarting,
            detail="Standalone binary replaced and verified via SHA-256.",
        )

    return UpdateResult(
        ok=False,
        install_kind=kind,
        from_version=APP_VERSION,
        to_version=to_ver,
        restart_required=False,
        restarting=False,
        detail="Automatic in-place update is not available for this installation type.",
        command_hint="pip install --upgrade .",
    )
