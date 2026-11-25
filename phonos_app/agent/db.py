"""Storage for the assistant: additive tables, rows and small helpers.

All tables are prefixed ``phonos_agent_`` and are created lazily on first use.
The rest of Phonos never reads them, so a deployment without the assistant
behaves exactly as before.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from ..app import app
from ..helpers.database import (
    PostgreSQLConnection,
    SQLiteCompatConnection,
    _get_raw_connection,
    _use_sqlite,
    get_db,
)

_LOCK = threading.Lock()
_READY: set[str] = set()


def _is_sqlite() -> bool:
    if app.config.get("TESTING") and app.config.get("DATABASE_FILE"):
        return True
    return str(app.config.get("DATABASE_URL") or "").startswith("sqlite")


def _id_column() -> str:
    # PostgreSQL uses SERIAL; SQLite needs INTEGER PRIMARY KEY AUTOINCREMENT.
    return "INTEGER PRIMARY KEY AUTOINCREMENT" if _is_sqlite() else "SERIAL PRIMARY KEY"


def _schema_key() -> str:
    return "|".join(
        str(part)
        for part in (
            app.config.get("DATABASE_URL"),
            app.config.get("DATABASE_FILE"),
        )
    )


def ensure_schema(db=None) -> None:
    """Create the assistant tables once per process and database."""
    key = _schema_key()
    if key in _READY:
        return
    with _LOCK:
        if key in _READY:
            return
        conn = db or get_db()
        pk = _id_column()
        statements = [
            f"""
            CREATE TABLE IF NOT EXISTS phonos_agent_chats (
                id {pk},
                user_id INTEGER NOT NULL,
                title TEXT,
                provider TEXT,
                model TEXT,
                write_mode INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_message_at TIMESTAMP,
                deleted_at TIMESTAMP
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS phonos_agent_messages (
                id {pk},
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                name TEXT,
                model TEXT,
                usage TEXT,
                interrupted INTEGER NOT NULL DEFAULT 0,
                hidden_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phonos_agent_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT,
                updated_by INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_phonos_agent_chats_user "
            "ON phonos_agent_chats(user_id, last_message_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_phonos_agent_messages_chat "
            "ON phonos_agent_messages(chat_id, id)",
        ]
        for statement in statements:
            conn.execute(statement)
        conn.commit()
        _READY.add(key)


def as_dict(row) -> dict | None:
    """Normalise a sqlite.Row / psycopg2 RealDictRow into a plain dict."""
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:  # pragma: no cover - defensive
        return None


def as_dicts(rows: Iterable[Any] | None) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        item = as_dict(row)
        if item is not None:
            out.append(item)
    return out


def jsonable(value: Any) -> Any:
    """Make database values safe for JSON without leaking bytes or Decimals."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")[:4096]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def clamp(value: Any, default: int = 25, maximum: int = 100) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def as_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(jsonable(value), ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        return None


def load_json(value: Any, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


@contextmanager
def independent_db():
    """A connection that is not tied to the Flask request teardown.

    SSE handlers return a streaming response; the request-scoped ``g.db`` can
    be closed by the time the generator runs.  The assistant turn gets its own
    connection and closes it when the response ends.
    """
    raw = _get_raw_connection()
    conn = SQLiteCompatConnection(raw) if _use_sqlite() else PostgreSQLConnection(raw)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            pass
