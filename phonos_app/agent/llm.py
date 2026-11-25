"""A small OpenAI-compatible streaming client (stdlib only).

Phonos runs eventlet in production; the socket reads below are cooperative
there.  The client deliberately does not follow redirects and never sends the
API key to a second host.  Tool-call fragments are accumulated by index, which
is what all OpenAI-compatible providers emit while streaming.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from .settings import build_chat_url, provider as provider_config

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ProviderError(Exception):
    """A readable provider failure."""

    def __init__(self, detail: str, status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict)
        )
    return ""


def _headers(info: dict, session_id: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "phonos-agent/1.0",
    }
    base_url = info.get("base_url") or ""
    if info.get("api_key"):
        if "api.anthropic.com" in base_url:
            headers["x-api-key"] = info["api_key"]
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = "Bearer " + info["api_key"]
    elif info.get("keyless"):
        headers["Authorization"] = "Bearer ollama"
    return headers


def _post(info: dict, payload: dict, *, timeout: int, session_id: str):
    request = urllib.request.Request(
        build_chat_url(info["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(info, session_id),
        method="POST",
    )
    try:
        return _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(4000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise ProviderError(f"The provider answered with HTTP {exc.code}: {body}", 502) from exc
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"Could not reach the provider: {exc}", 502) from exc


def stream_completion(
    *,
    db,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    timeout_s: int = 180,
    session_id: str = "",
) -> Iterator[dict]:
    """Yield ``text`` / ``reasoning`` / ``done`` events from the provider."""
    info = provider_config(db)
    if not info.get("api_key") and not info.get("keyless"):
        raise ProviderError(
            "No API key is configured. Add one in Copilot Settings, Models & Endpoints (/custom-models), "
            "or select a keyless local preset (Ollama / LM Studio / vLLM).",
            400,
        )
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": max(0.0, min(float(temperature or 0.2), 2.0)),
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    timeout = max(15, min(int(timeout_s or 180), 900))
    try:
        response = _post(info, payload, timeout=timeout, session_id=session_id)
    except ProviderError as exc:
        # Some compatible endpoints reject stream_options. Retry once without
        # it rather than failing the whole turn.
        if "stream_options" in str(exc.detail).lower() or "HTTP 400" in str(exc.detail):
            payload.pop("stream_options", None)
            response = _post(info, payload, timeout=timeout, session_id=session_id)
        else:
            raise

    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict] = {}
    usage: dict = {}
    finish_reason = ""
    total = 0

    try:
        with response:
            for raw in response:
                total += len(raw)
                if total > MAX_RESPONSE_BYTES:
                    raise ProviderError("The provider response exceeded the size limit.", 502)
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                elif line.startswith("{"):
                    data = line
                else:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    error = chunk["error"]
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise ProviderError(f"The provider reported: {detail}", 502)
                if chunk.get("usage"):
                    usage = chunk["usage"] or {}
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}
                if not delta and choice.get("message"):
                    delta = choice["message"]
                text = _content_text(delta.get("content"))
                if text:
                    content.append(text)
                    yield {"type": "text", "text": text}
                think = _content_text(delta.get("reasoning_content") or delta.get("reasoning"))
                if think:
                    reasoning.append(think)
                    yield {"type": "reasoning", "text": think}
                for position, call in enumerate(delta.get("tool_calls") or []):
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index")
                    if not isinstance(index, int):
                        index = position
                    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if call.get("id"):
                        slot["id"] = str(call["id"])
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += str(fn["name"])
                    if fn.get("arguments"):
                        slot["arguments"] += str(fn["arguments"])
                finish_reason = choice.get("finish_reason") or finish_reason
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"The provider stream failed: {exc}", 502) from exc

    tool_calls = []
    for slot in calls.values():
        if not slot["name"]:
            continue
        tool_calls.append({
            "id": slot["id"] or f"call_{len(tool_calls) + 1}",
            "type": "function",
            "function": {
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            },
        })
    yield {
        "type": "done",
        "content": "".join(content),
        "reasoning": "".join(reasoning),
        "tool_calls": tool_calls,
        "usage": usage,
        "finish_reason": finish_reason,
    }
