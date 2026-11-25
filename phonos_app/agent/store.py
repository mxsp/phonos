"""Chat storage and history shaping for Phonos Copilot."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import as_dict, ensure_schema, jsonable, load_json

MAX_TOOL_CHARS = 24000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _title_from(prompt: str) -> str:
    line = " ".join((prompt or "").split())
    return (line[:77] + "…") if len(line) > 80 else (line or "New chat")


def _chat_out(row) -> dict:
    item = as_dict(row) or {}
    return {
        "id": item.get("id"),
        "title": item.get("title") or "New chat",
        "provider": item.get("provider"),
        "model": item.get("model"),
        "write_mode": bool(item.get("write_mode")),
        "created_at": jsonable(item.get("created_at")),
        "updated_at": jsonable(item.get("updated_at")),
        "last_message_at": jsonable(item.get("last_message_at")),
        "message_count": item.get("message_count"),
    }


def list_chats(db, user_id: int, limit: int = 100) -> list[dict]:
    ensure_schema(db)
    rows = db.execute(
        "SELECT c.id, c.title, c.provider, c.model, c.write_mode, c.created_at, "
        "c.updated_at, c.last_message_at, "
        "(SELECT COUNT(*) FROM phonos_agent_messages m "
        " WHERE m.chat_id = c.id AND m.hidden_at IS NULL) AS message_count "
        "FROM phonos_agent_chats c WHERE c.user_id = %s AND c.deleted_at IS NULL "
        "ORDER BY COALESCE(c.last_message_at, c.created_at) DESC LIMIT %s",
        (user_id, max(1, min(int(limit or 100), 200))),
    ).fetchall()
    return [_chat_out(row) for row in rows]


def create_chat(db, user_id: int, *, title: str = "", model: str = "", provider: str = "") -> dict:
    ensure_schema(db)
    clean_title = " ".join(str(title or "").split())[:120] or "New chat"
    cursor = db.execute(
        "INSERT INTO phonos_agent_chats (user_id, title, provider, model, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (user_id, clean_title, provider[:120], model[:160]),
    )
    chat_id = cursor.lastrowid
    db.commit()
    return get_chat(db, user_id, chat_id)


def get_chat(db, user_id: int, chat_id: int, *, include_deleted: bool = False) -> dict | None:
    ensure_schema(db)
    row = db.execute(
        "SELECT id, user_id, title, provider, model, write_mode, created_at, updated_at, "
        "last_message_at, deleted_at FROM phonos_agent_chats "
        "WHERE id = %s AND user_id = %s" + ("" if include_deleted else " AND deleted_at IS NULL"),
        (chat_id, user_id),
    ).fetchone()
    item = as_dict(row)
    if not item:
        return None
    item = _chat_out(item)
    item["user_id"] = user_id
    return item


def update_chat(db, user_id: int, chat_id: int, *, title=None, model=None, provider=None, write_mode=None) -> dict | None:
    ensure_schema(db)
    chat = get_chat(db, user_id, chat_id)
    if not chat:
        return None
    updates = []
    params: list[Any] = []
    if title is not None:
        updates.append("title = %s")
        params.append(" ".join(str(title).split())[:120] or "New chat")
    if model is not None:
        updates.append("model = %s")
        params.append(str(model)[:160])
    if provider is not None:
        updates.append("provider = %s")
        params.append(str(provider)[:120])
    if write_mode is not None:
        updates.append("write_mode = %s")
        params.append(1 if write_mode else 0)
    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.extend([chat_id, user_id])
        db.execute(
            "UPDATE phonos_agent_chats SET " + ", ".join(updates) +
            " WHERE id = %s AND user_id = %s",
            tuple(params),
        )
        db.commit()
    return get_chat(db, user_id, chat_id)


def soft_delete(db, user_id: int, chat_id: int) -> bool:
    ensure_schema(db)
    cursor = db.execute(
        "UPDATE phonos_agent_chats SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
        (chat_id, user_id),
    )
    db.commit()
    return bool(cursor.rowcount)


def touch(db, chat_id: int) -> None:
    db.execute(
        "UPDATE phonos_agent_chats SET last_message_at = CURRENT_TIMESTAMP, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (chat_id,),
    )
    db.commit()


def message_out(row) -> dict:
    item = as_dict(row) or {}
    return {
        "id": item.get("id"),
        "chat_id": item.get("chat_id"),
        "role": item.get("role"),
        "content": item.get("content") or "",
        "tool_calls": load_json(item.get("tool_calls"), []),
        "tool_call_id": item.get("tool_call_id"),
        "name": item.get("name"),
        "model": item.get("model"),
        "usage": load_json(item.get("usage"), {}),
        "interrupted": bool(item.get("interrupted")),
        "created_at": jsonable(item.get("created_at")),
    }


def messages_for(db, chat_id: int, *, include_hidden: bool = False) -> list[dict]:
    ensure_schema(db)
    sql = (
        "SELECT id, chat_id, role, content, tool_calls, tool_call_id, name, model, usage, "
        "interrupted, hidden_at, created_at FROM phonos_agent_messages WHERE chat_id = %s"
    )
    if not include_hidden:
        sql += " AND hidden_at IS NULL"
    sql += " ORDER BY id ASC"
    return [message_out(row) for row in db.execute(sql, (chat_id,)).fetchall()]


def add_message(db, chat_id: int, role: str, content: str = "", **extra) -> dict:
    columns = ["chat_id", "role", "content", "tool_calls", "tool_call_id", "name",
               "model", "usage", "interrupted"]
    values = [
        chat_id,
        role,
        content or "",
        json.dumps(extra.get("tool_calls"), ensure_ascii=False) if extra.get("tool_calls") else None,
        extra.get("tool_call_id"),
        extra.get("name"),
        extra.get("model"),
        json.dumps(extra.get("usage"), ensure_ascii=False) if extra.get("usage") else None,
        1 if extra.get("interrupted") else 0,
    ]
    cursor = db.execute(
        "INSERT INTO phonos_agent_messages (" + ", ".join(columns) + ") VALUES (" +
        ", ".join(["%s"] * len(columns)) + ")",
        tuple(values),
    )
    db.commit()
    message_id = cursor.lastrowid
    row = db.execute(
        "SELECT id, chat_id, role, content, tool_calls, tool_call_id, name, model, usage, "
        "interrupted, created_at FROM phonos_agent_messages WHERE id = %s",
        (message_id,),
    ).fetchone()
    return message_out(row)


def history(db, chat_id: int, limit: int = 24) -> list[dict]:
    """Build the provider message list, starting on a user turn and keeping tool pairs intact."""
    rows = db.execute(
        "SELECT id, role, content, tool_calls, tool_call_id FROM phonos_agent_messages "
        "WHERE chat_id = %s AND hidden_at IS NULL ORDER BY id DESC LIMIT %s",
        (chat_id, max(4, min(int(limit or 24), 200))),
    ).fetchall()
    items = [as_dict(row) or {} for row in reversed(rows)]
    start = next((index for index, item in enumerate(items) if item.get("role") == "user"), 0)
    items = items[start:]
    result_ids = {
        str(item.get("tool_call_id"))
        for item in items
        if item.get("role") == "tool" and item.get("tool_call_id")
    }
    messages: list[dict] = []
    for item in items:
        role = item.get("role")
        content = item.get("content") or ""
        if role == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("tool_call_id") or ""),
                "content": content,
            })
            continue
        message: dict = {"role": role, "content": content}
        calls = load_json(item.get("tool_calls"), [])
        if role == "assistant" and calls:
            ids = {str(call.get("id")) for call in calls if isinstance(call, dict)}
            if not ids.issubset(result_ids):
                message = {"role": "assistant", "content": content}
            else:
                message["tool_calls"] = calls
        messages.append(message)
    return messages


def trim_result(result: dict) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = json.dumps({"ok": False, "error": "The tool result could not be serialised."})
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + "\n… (truncated)"
    return text


def preview(result: dict, limit: int = 6000) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return ""
    return text[:limit]


def rewind(db, user_id: int, chat_id: int, message_id: int) -> dict:
    ensure_schema(db)
    chat = get_chat(db, user_id, chat_id)
    if not chat:
        return {"ok": False, "error": "Chat not found."}
    row = db.execute(
        "SELECT id FROM phonos_agent_messages WHERE id = %s AND chat_id = %s",
        (message_id, chat_id),
    ).fetchone()
    if not row:
        return {"ok": False, "error": "Message not found."}
    cursor = db.execute(
        "UPDATE phonos_agent_messages SET hidden_at = CURRENT_TIMESTAMP "
        "WHERE chat_id = %s AND id >= %s AND hidden_at IS NULL",
        (chat_id, message_id),
    )
    db.execute(
        "UPDATE phonos_agent_chats SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (chat_id,),
    )
    db.commit()
    return {"ok": True, "hidden": cursor.rowcount or 0, "message_id": message_id}


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def stats(db, user_id: int, days: int = 30) -> dict:
    ensure_schema(db)
    days = 0 if int(days or 0) <= 0 else max(1, min(int(days), 3650))
    params: list[Any] = [user_id]
    where = "c.user_id = %s"
    if days:
        cutoff = _now() - timedelta(days=days)
        where += " AND m.created_at >= %s"
        params.append(cutoff)
    rows = db.execute(
        "SELECT m.model, m.usage, m.created_at, c.id AS chat_id, c.title AS chat_title "
        "FROM phonos_agent_messages m JOIN phonos_agent_chats c ON c.id = m.chat_id "
        "WHERE " + where + " AND m.role = 'assistant' ORDER BY m.created_at ASC",
        tuple(params),
    ).fetchall()
    totals = {
        "rounds": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "cache_read": 0, "reasoning_tokens": 0, "cost": 0.0,
    }
    by_model: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    by_chat: dict[int, dict] = {}
    for row in rows:
        item = as_dict(row) or {}
        usage = load_json(item.get("usage"), {})
        if not isinstance(usage, dict):
            usage = {}
        prompt = int(_number(usage.get("prompt_tokens") or usage.get("input_tokens")))
        completion = int(_number(usage.get("completion_tokens") or usage.get("output_tokens")))
        total = int(_number(usage.get("total_tokens"))) or (prompt + completion)
        if not (prompt or completion or total):
            continue
        model = str(item.get("model") or "unknown")
        cost = _number(usage.get("cost"))
        totals["rounds"] += 1
        totals["input_tokens"] += prompt
        totals["output_tokens"] += completion
        totals["total_tokens"] += total
        totals["cache_read"] += int(_number(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            if isinstance(usage.get("prompt_tokens_details"), dict) else 0
        ))
        totals["reasoning_tokens"] += int(_number(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            if isinstance(usage.get("completion_tokens_details"), dict) else 0
        ))
        totals["cost"] += cost
        for bucket, key in ((by_model, model),):
            entry = bucket.setdefault(key, {"model": key, "rounds": 0, "input_tokens": 0,
                                            "output_tokens": 0, "total_tokens": 0, "cost": 0.0})
            entry["rounds"] += 1
            entry["input_tokens"] += prompt
            entry["output_tokens"] += completion
            entry["total_tokens"] += total
            entry["cost"] += cost
        created = item.get("created_at")
        day = created.date().isoformat() if hasattr(created, "date") else str(created)[:10]
        entry = by_day.setdefault(day, {"day": day, "tokens": 0, "cost": 0.0})
        entry["tokens"] += total
        entry["cost"] += cost
        chat_entry = by_chat.setdefault(
            int(item.get("chat_id") or 0),
            {"chat_id": item.get("chat_id"), "title": item.get("chat_title"),
             "tokens": 0, "cost": 0.0},
        )
        chat_entry["tokens"] += total
        chat_entry["cost"] += cost
    return {
        "days": days or "all",
        "totals": totals,
        "by_model": sorted(by_model.values(), key=lambda item: item["total_tokens"], reverse=True),
        "by_day": sorted(by_day.values(), key=lambda item: item["day"]),
        "top_chats": sorted(by_chat.values(), key=lambda item: item["tokens"], reverse=True)[:12],
    }


def auto_title(chat: dict, prompt: str) -> str:
    title = chat.get("title") or ""
    if title and title not in ("New chat", "Untitled"):
        return title
    return _title_from(prompt)
