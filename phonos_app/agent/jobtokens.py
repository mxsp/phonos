"""Short-lived, signed permission for automatic bio-model runs.

Auto-run is a per-chat permission (``phonos_agent_chats.write_mode``).  When it
is on, ``propose_job`` returns a token over the exact draft.  The job endpoint
accepts an automatic run only with a valid token for the same user, chat and
draft; a model cannot fabricate one and a different draft cannot be substituted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from ..app import app


def _secret() -> bytes:
    value = app.secret_key or ""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _canonical(draft: dict) -> str:
    clean = {
        "model": str(draft.get("model") or ""),
        "project_id": draft.get("project_id"),
        "name": str(draft.get("name") or ""),
        "sequence": str(draft.get("sequence") or ""),
        "file_id": draft.get("file_id"),
        "custom_params": draft.get("custom_params") or {},
    }
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def draft_hash(draft: dict) -> str:
    return hashlib.sha256(_canonical(draft).encode("utf-8")).hexdigest()


def issue(user_id: int, chat_id: int, draft: dict, ttl: int = 300) -> str:
    payload = {
        "u": int(user_id),
        "c": int(chat_id),
        "e": int(time.time()) + max(30, min(int(ttl), 900)),
        "h": draft_hash(draft),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return body + "." + signature


def verify(token: str, user_id: int, chat_id: int, draft: dict) -> bool:
    if not token or "." not in token:
        return False
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return False
    if int(payload.get("u") or 0) != int(user_id):
        return False
    if int(payload.get("c") or 0) != int(chat_id):
        return False
    if int(payload.get("e") or 0) < int(time.time()):
        return False
    return hmac.compare_digest(str(payload.get("h") or ""), draft_hash(draft))
