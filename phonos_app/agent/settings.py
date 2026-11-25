"""Provider, model and assistant settings for Phonos Copilot (Gimel AgentLab parity).

Secrets live in ``phonos_agent_settings`` (and bridge automatically with the
unified ``model_providers`` vault) sealed via ``data_crypto``. Environment
variables take precedence when set, local inference servers (Ollama, LM Studio,
vLLM, LAN endpoints) work keylessly out-of-the-box, and plaintext keys are
never returned to the browser.
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..helpers import data_crypto
from .db import as_dict, ensure_schema

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_MODELS = GEMINI_BASE + "/models"
LABEL = "phonos_agent.api_key"
MODEL_CACHE_SECONDS = 600

DEFAULTS = {
    "provider_label": "Google Gemini",
    "base_url": GEMINI_BASE,
    "models_url": GEMINI_MODELS,
    "model": "gemini-2.5-flash",
    "temperature": "0.2",
    "max_steps": "10",
    "timeout_s": "90",
    "history_messages": "24",
    "web_enabled": "1",
    "system_prompt": "",
}

PRESETS = [
    {
        "id": "gemini",
        "label": "Google Gemini (OpenAI-compatible)",
        "base_url": GEMINI_BASE,
        "models_url": GEMINI_MODELS,
        "model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models_url": "https://api.openai.com/v1/models",
        "model": "gpt-4.1-mini",
        "key_env": "OPENAI_API_KEY",
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude (OpenAI-compatible)",
        "base_url": "https://api.anthropic.com/v1",
        "models_url": "https://api.anthropic.com/v1/models",
        "model": "claude-sonnet-4-20250514",
        "key_env": "ANTHROPIC_API_KEY",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "model": "google/gemini-2.5-flash",
        "key_env": "OPENROUTER_API_KEY",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models_url": "https://api.deepseek.com/v1/models",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    {
        "id": "groq",
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models_url": "https://api.groq.com/openai/v1/models",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    {
        "id": "mistral",
        "label": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "models_url": "https://api.mistral.ai/v1/models",
        "model": "mistral-medium-latest",
        "key_env": "MISTRAL_API_KEY",
    },
    {
        "id": "xai",
        "label": "xAI Grok",
        "base_url": "https://api.x.ai/v1",
        "models_url": "https://api.x.ai/v1/models",
        "model": "grok-3-mini",
        "key_env": "XAI_API_KEY",
    },
    {
        "id": "ollama",
        "label": "Ollama (Local — Keyless)",
        "base_url": "http://127.0.0.1:11434/v1",
        "models_url": "http://127.0.0.1:11434/v1/models",
        "model": "qwen2.5:14b",
        "key_env": "",
        "keyless": True,
        "note": "Runs 100% locally via Ollama with no API key required.",
    },
    {
        "id": "lmstudio",
        "label": "LM Studio (Local — Keyless)",
        "base_url": "http://127.0.0.1:1234/v1",
        "models_url": "http://127.0.0.1:1234/v1/models",
        "model": "local-model",
        "key_env": "",
        "keyless": True,
        "note": "Connects to LM Studio local server on port 1234.",
    },
    {
        "id": "vllm",
        "label": "vLLM / llama.cpp (Local GPU — Keyless)",
        "base_url": "http://127.0.0.1:8000/v1",
        "models_url": "http://127.0.0.1:8000/v1/models",
        "model": "",
        "key_env": "",
        "keyless": True,
        "note": "Connects to local vLLM or llama.cpp OpenAI-compatible server.",
    },
]

_MODEL_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()


class SettingsError(Exception):
    """A user-facing settings or provider error."""


def _strip(value: str | None) -> str:
    return str(value or "").strip()


def _env(name: str) -> str:
    return _strip(os.environ.get(name))


def _base_is_gemini(base_url: str) -> bool:
    host = urllib.parse.urlparse(base_url or "").hostname or ""
    return host.endswith("generativelanguage.googleapis.com")


def is_keyless_url(base_url: str) -> bool:
    """Return True if ``base_url`` points to a local/LAN keyless inference host."""
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama", "vllm"):
        return True
    if host.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def validate_url(url: str, *, field: str = "Base URL", allow_private: bool = True) -> str:
    url = _strip(url).rstrip("/")
    if not url:
        raise SettingsError(f"{field} is empty.")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SettingsError(f"{field} must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise SettingsError(f"{field} must not contain credentials.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise SettingsError(f"{field} has no host.")
    if host in ("169.254.169.254", "metadata.google.internal"):
        raise SettingsError(f"{field} points to a blocked cloud metadata host.")
    if len(url) > 400:
        raise SettingsError(f"{field} is too long.")
    return url


def _seal_key(value: str) -> str:
    value = _strip(value)
    if not value:
        return ""
    try:
        return data_crypto.seal(LABEL, value)
    except Exception as exc:
        raise SettingsError(f"The API key could not be stored: {exc}") from exc


def _open_key(value: str) -> str:
    if not value:
        return ""
    try:
        return data_crypto.unseal(LABEL, value) or ""
    except Exception:
        return ""


def env_key(base_url: str) -> str:
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    key = _env("PHONOS_AGENT_API_KEY") or _env("AGENT_API_KEY")
    if key:
        return key
    if "generativelanguage.googleapis.com" in host:
        return _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
    if "api.openai.com" in host:
        return _env("OPENAI_API_KEY")
    if "api.anthropic.com" in host:
        return _env("ANTHROPIC_API_KEY")
    if "openrouter.ai" in host:
        return _env("OPENROUTER_API_KEY")
    if "api.deepseek.com" in host:
        return _env("DEEPSEEK_API_KEY")
    if "api.groq.com" in host:
        return _env("GROQ_API_KEY")
    if "api.mistral.ai" in host:
        return _env("MISTRAL_API_KEY")
    if "api.x.ai" in host:
        return _env("XAI_API_KEY")
    return ""


def _vault_fallback_key(db, base_url: str) -> str:
    """Bridge with unified ``model_providers`` vault if a matching provider key was saved there."""
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    pid = None
    if "generativelanguage.googleapis.com" in host:
        pid = "gemini"
    elif "api.openai.com" in host:
        pid = "openai"
    elif "api.anthropic.com" in host:
        pid = "anthropic"
    elif "openrouter.ai" in host:
        pid = "openrouter"
    if not pid:
        return ""
    try:
        from ..helpers.model_providers import resolve_provider_credentials
        return resolve_provider_credentials(pid, db=db).get("api_key") or ""
    except Exception:
        return ""


def _read_rows(db) -> dict[str, str]:
    ensure_schema(db)
    rows = db.execute(
        "SELECT setting_key, setting_value FROM phonos_agent_settings"
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        item = as_dict(row) or {}
        key = item.get("setting_key")
        if key:
            out[str(key)] = item.get("setting_value") or ""
    return out


def read_settings(db) -> dict:
    rows = _read_rows(db)
    base_url = (
        _env("PHONOS_AGENT_BASE_URL")
        or rows.get("base_url")
        or DEFAULTS["base_url"]
    )
    model = _env("PHONOS_AGENT_MODEL") or rows.get("model") or DEFAULTS["model"]
    models_url = (
        _env("PHONOS_AGENT_MODELS_URL")
        or rows.get("models_url")
        or (base_url.rstrip("/") + "/models")
    )
    keyless = is_keyless_url(base_url)
    key = env_key(base_url)
    db_key = rows.get("api_key") or _vault_fallback_key(db, base_url)
    if key:
        key_source = "environment"
    elif db_key:
        key_source = "database"
    elif keyless:
        key_source = "keyless"
    else:
        key_source = "none"

    return {
        "provider_label": rows.get("provider_label") or DEFAULTS["provider_label"],
        "base_url": base_url,
        "models_url": models_url,
        "model": model,
        "temperature": rows.get("temperature") or DEFAULTS["temperature"],
        "max_steps": rows.get("max_steps") or DEFAULTS["max_steps"],
        "timeout_s": rows.get("timeout_s") or DEFAULTS["timeout_s"],
        "history_messages": rows.get("history_messages") or DEFAULTS["history_messages"],
        "web_enabled": rows.get("web_enabled", DEFAULTS["web_enabled"]) == "1",
        "system_prompt": rows.get("system_prompt") or "",
        "api_key_set": bool(key or db_key or keyless),
        "api_key_source": key_source,
        "keyless": keyless,
        "key_managed": False,
        "env_locked": bool(_env("PHONOS_AGENT_BASE_URL") or _env("PHONOS_AGENT_API_KEY")),
    }


def public_settings(db) -> dict:
    """Settings that are safe to send to every signed-in user."""
    data = read_settings(db)
    data.pop("api_key", None)
    return data


def save_settings(db, user_id: int, payload: dict) -> dict:
    ensure_schema(db)
    allowed = {
        "provider_label",
        "base_url",
        "models_url",
        "model",
        "temperature",
        "max_steps",
        "timeout_s",
        "history_messages",
        "web_enabled",
        "system_prompt",
    }
    updates: dict[str, str] = {}
    if "base_url" in payload:
        updates["base_url"] = validate_url(payload.get("base_url"), field="Base URL")
    if "models_url" in payload and _strip(payload.get("models_url")):
        updates["models_url"] = validate_url(payload.get("models_url"), field="Models URL")
    if "model" in payload:
        updates["model"] = _strip(payload.get("model"))[:160]
    if "provider_label" in payload:
        updates["provider_label"] = _strip(payload.get("provider_label"))[:80]
    if "temperature" in payload:
        try:
            temperature = float(payload.get("temperature"))
        except (TypeError, ValueError) as exc:
            raise SettingsError("Temperature must be a number.") from exc
        if not 0 <= temperature <= 2:
            raise SettingsError("Temperature must be between 0 and 2.")
        updates["temperature"] = str(temperature)
    for name, low, high in (("max_steps", 1, 20), ("timeout_s", 15, 180), ("history_messages", 4, 100)):
        if name in payload:
            try:
                value = int(payload.get(name))
            except (TypeError, ValueError) as exc:
                raise SettingsError(f"{name} must be a whole number.") from exc
            if not low <= value <= high:
                raise SettingsError(f"{name} must be between {low} and {high}.")
            updates[name] = str(value)
    if "web_enabled" in payload:
        updates["web_enabled"] = "1" if payload.get("web_enabled") else "0"
    if "system_prompt" in payload:
        updates["system_prompt"] = _strip(payload.get("system_prompt"))[:4000]

    for key, value in updates.items():
        if key not in allowed:
            continue
        existing = db.execute(
            "SELECT setting_key FROM phonos_agent_settings WHERE setting_key = %s",
            (key,),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE phonos_agent_settings SET setting_value = %s, "
                "updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE setting_key = %s",
                (value, user_id, key),
            )
        else:
            db.execute(
                "INSERT INTO phonos_agent_settings (setting_key, setting_value, updated_by, updated_at) "
                "VALUES (%s, %s, %s, CURRENT_TIMESTAMP)",
                (key, value, user_id),
            )
    if payload.get("clear_api_key"):
        db.execute(
            "DELETE FROM phonos_agent_settings WHERE setting_key = %s", ("api_key",)
        )
    if _strip(payload.get("api_key")):
        sealed = _seal_key(payload.get("api_key"))
        existing = db.execute(
            "SELECT setting_key FROM phonos_agent_settings WHERE setting_key = %s",
            ("api_key",),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE phonos_agent_settings SET setting_value = %s, updated_by = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE setting_key = %s",
                (sealed, user_id, "api_key"),
            )
        else:
            db.execute(
                "INSERT INTO phonos_agent_settings (setting_key, setting_value, updated_by, updated_at) "
                "VALUES (%s, %s, %s, CURRENT_TIMESTAMP)",
                ("api_key", sealed, user_id),
            )
    db.commit()
    return public_settings(db)


def provider(db) -> dict:
    settings = read_settings(db)
    base_url = validate_url(settings["base_url"])
    keyless = is_keyless_url(base_url)
    key = env_key(base_url)
    source = "environment" if key else "none"
    if not key:
        rows = _read_rows(db)
        key = _open_key(rows.get("api_key") or "")
        if key:
            source = "database"
        else:
            key = _vault_fallback_key(db, base_url)
            if key:
                source = "vault"
            elif keyless:
                source = "keyless"
    models_url = settings.get("models_url") or (base_url + "/models")
    try:
        models_url = validate_url(models_url, field="Models URL")
    except SettingsError:
        models_url = base_url + "/models"
    return {
        "base_url": base_url,
        "model": _strip(settings.get("model")),
        "models_url": models_url,
        "api_key": key,
        "keyless": keyless,
        "api_key_source": source,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_models(db, *, refresh: bool = False) -> list[dict]:
    """Return the endpoint's model list, cached for ten minutes."""
    info = provider(db)
    key = info["models_url"]
    base_url = info["base_url"]
    now = time.time()
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
    if cached and not refresh and now - cached[0] < MODEL_CACHE_SECONDS:
        return cached[1]

    headers = {"Accept": "application/json", "User-Agent": "phonos-agent/1.0"}
    if info["api_key"]:
        if "api.anthropic.com" in base_url:
            headers["x-api-key"] = info["api_key"]
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = "Bearer " + info["api_key"]
    request = urllib.request.Request(info["models_url"], headers=headers)
    try:
        with _OPENER.open(request, timeout=20) as response:
            body = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise SettingsError(f"The model endpoint answered with HTTP {exc.code}.") from exc
    except Exception as exc:
        raise SettingsError(f"The model endpoint is not reachable: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise SettingsError("The model endpoint did not return JSON.") from exc
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and not rows:
        rows = payload.get("models") or payload.get("items") or []
    models: list[dict] = []
    for row in rows or []:
        if isinstance(row, str):
            models.append({"id": row})
            continue
        if not isinstance(row, dict):
            continue
        ident = row.get("id") or row.get("name") or row.get("model")
        if not ident:
            continue
        ident = str(ident)
        if _base_is_gemini(base_url) and ident.startswith("models/"):
            ident = ident[len("models/"):]
        item = {"id": ident[:200]}
        if row.get("name") and row.get("name") != ident:
            item["name"] = str(row["name"])[:200]
        models.append(item)
    models.sort(key=lambda item: item["id"])
    with _CACHE_LOCK:
        _MODEL_CACHE[key] = (now, models)
    return models


def test_provider(db) -> dict:
    info = provider(db)
    if not info["api_key"] and not info.get("keyless"):
        raise SettingsError(
            "No API key is configured. Set one in Settings, Models & Endpoints, or the server environment."
        )
    models = fetch_models(db, refresh=True)
    return {
        "ok": True,
        "base_url": info["base_url"],
        "models": len(models),
        "key_source": info["api_key_source"],
    }


def preset_list() -> list[dict]:
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "base_url": item["base_url"],
            "models_url": item["models_url"],
            "model": item["model"],
            "key_env": item["key_env"],
            "keyless": bool(item.get("keyless")),
            "hidden_key": bool(item.get("hidden_key")),
            "note": item.get("note") or "",
        }
        for item in PRESETS
    ]


def numeric(settings: dict, key: str, default, cast=float):
    try:
        return cast(settings.get(key))
    except (TypeError, ValueError):
        return default


def model_cache_clear() -> None:
    with _CACHE_LOCK:
        _MODEL_CACHE.clear()


def build_chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"
