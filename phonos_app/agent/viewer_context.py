"""Ephemeral 3D/sequence snapshot for the Copilot.

The browser owns the Mol* viewer and the sequence panel.  When the user has a
structure open (or has selected residues/ligands), the page posts a snapshot
here.  Tools can then read the full structure and sequence without putting
megabytes into every prompt.

Nothing is written to disk and nothing is logged.  Snapshots are per user,
capped, and expire after an hour.  A second user can never read another user's
snapshot.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import zlib
from typing import Any

MAX_STRUCTURE_CHARS = 8 * 1024 * 1024
MAX_SEQUENCE_CHARS = 2 * 1024 * 1024
MAX_SELECTION = 200
TTL_SECONDS = 3600
REDIS_PREFIX = "phonos_agent:viewer:"

_LOCK = threading.Lock()
_SNAPSHOTS: dict[int, dict] = {}


def _redis():
    try:
        from ..app import get_redis_connection
        return get_redis_connection(binary_mode=True)
    except Exception:  # pragma: no cover - Redis is optional in tests
        return None


def _redis_key(user_id: int) -> str:
    return f"{REDIS_PREFIX}{int(user_id)}"


def _redis_get(user_id: int) -> dict | None:
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(_redis_key(user_id))
        if not raw:
            return None
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return json.loads(zlib.decompress(raw).decode("utf-8"))
    except Exception:
        return None


def _redis_put(user_id: int, item: dict) -> None:
    client = _redis()
    if client is None:
        return
    try:
        blob = zlib.compress(json.dumps(item, ensure_ascii=False).encode("utf-8"), 3)
        client.set(_redis_key(user_id), blob, ex=TTL_SECONDS)
    except Exception:
        pass


def _redis_clear(user_id: int) -> None:
    client = _redis()
    if client is None:
        return
    try:
        client.delete(_redis_key(user_id))
    except Exception:
        pass


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text[:limit]


def _clean_selection(raw: Any) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_SELECTION]:
        if not isinstance(item, dict):
            continue
        chain = _clip(item.get("chain"), 8)
        resi = item.get("resi")
        try:
            resi = int(resi)
        except (TypeError, ValueError):
            continue
        key = f"{chain}:{resi}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "chain": chain or "A",
            "resi": resi,
            "resname": _clip(item.get("resname"), 12),
            "aa": _clip(item.get("aa"), 4),
        })
    return out


def put(user_id: int, payload: dict) -> dict:
    """Store a sanitised snapshot and return its public summary."""
    if not isinstance(payload, dict):
        payload = {}
    structure = _clip(payload.get("structure_text") or payload.get("structure"), MAX_STRUCTURE_CHARS)
    chains = []
    for chain in (payload.get("chains") or [])[:20]:
        if not isinstance(chain, dict):
            continue
        chains.append({
            "chain": _clip(chain.get("chain") or chain.get("id"), 12) or "A",
            "sequence": _clip(chain.get("sequence"), 500_000),
            "length": len(_clip(chain.get("sequence"), 500_000)),
        })
    sequence = _clip(payload.get("sequence"), 500_000)
    if sequence and not chains:
        chains = [{"chain": "A", "sequence": sequence, "length": len(sequence)}]
    snapshot = {
        "updated_at": time.time(),
        "name": _clip(payload.get("name") or payload.get("structure_name"), 240),
        "format": _clip(payload.get("format"), 16) or ("cif" if "data_" in structure[:4000].lower() else "pdb"),
        "structure_text": structure,
        "chains": chains,
        "selection": _clean_selection(payload.get("selection")),
        "ligand": _clip(payload.get("ligand"), 120),
        "project_id": payload.get("project_id"),
        "job_id": _clip(payload.get("job_id"), 200),
        "file_id": payload.get("file_id"),
        "analysis": payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {},
        "sha256": hashlib.sha256(structure.encode("utf-8", "replace")).hexdigest() if structure else "",
    }
    with _LOCK:
        _SNAPSHOTS[int(user_id)] = snapshot
    _redis_put(int(user_id), snapshot)
    return summary(user_id)


def get(user_id: int) -> dict | None:
    item = _redis_get(int(user_id))
    if not item:
        with _LOCK:
            item = _SNAPSHOTS.get(int(user_id))
    if not item:
        return None
    if time.time() - float(item.get("updated_at") or 0) > TTL_SECONDS:
        clear(int(user_id))
        return None
    with _LOCK:
        _SNAPSHOTS[int(user_id)] = item
    return dict(item)


def summary(user_id: int) -> dict:
    item = get(user_id)
    if not item:
        return {"attached": False}
    return {
        "attached": bool(item.get("structure_text") or item.get("chains")),
        "name": item.get("name") or "",
        "format": item.get("format") or "",
        "structure_chars": len(item.get("structure_text") or ""),
        "structure_sha256": item.get("sha256") or "",
        "chains": [
            {"chain": chain.get("chain"), "length": len(chain.get("sequence") or "")}
            for chain in item.get("chains") or []
        ],
        "selection": item.get("selection") or [],
        "ligand": item.get("ligand") or "",
        "project_id": item.get("project_id"),
        "job_id": item.get("job_id") or "",
        "file_id": item.get("file_id"),
        "updated_at": item.get("updated_at"),
    }


def clear(user_id: int) -> None:
    with _LOCK:
        _SNAPSHOTS.pop(int(user_id), None)
    _redis_clear(int(user_id))
