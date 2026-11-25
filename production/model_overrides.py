"""Helpers for persisting model availability overrides."""
from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, Tuple

from config import Config

ModelOverride = Dict[str, bool]


def _normalize_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"0", "false", "no", "off", ""}:
            return False
        return True
    return bool(value)


def get_default_model_state(model_id: str) -> Tuple[bool, bool]:
    """Return the default ``(is_active, is_visible)`` state from :class:`Config`."""

    model = Config.get_model_definition(model_id)
    if not model:
        return True, True

    available = bool(model.get("available", True))
    coming_soon = bool(model.get("coming_soon", False))

    # Allow model definitions to explicitly declare default UI state.
    if "is_active" in model:
        is_active = _normalize_bool(model.get("is_active"))
    else:
        is_active = available

    if "is_visible" in model:
        is_visible = _normalize_bool(model.get("is_visible"))
    else:
        # Coming-soon models remain visible but inactive by default.
        is_visible = available or coming_soon

    return is_active, is_visible


def fetch_model_overrides(conn: sqlite3.Connection) -> Dict[str, ModelOverride]:
    """Return all persisted overrides keyed by ``model_id``."""

    overrides: Dict[str, ModelOverride] = {}
    try:
        rows = conn.execute(
            "SELECT model_id, is_active, is_visible FROM model_overrides"
        ).fetchall()
    except sqlite3.Error:
        return overrides

    for row in rows:
        model_id = row["model_id"] if isinstance(row, sqlite3.Row) else row[0]
        is_active = row["is_active"] if isinstance(row, sqlite3.Row) else row[1]
        is_visible = row["is_visible"] if isinstance(row, sqlite3.Row) else row[2]
        overrides[model_id] = {
            "is_active": _normalize_bool(is_active),
            "is_visible": _normalize_bool(is_visible),
        }
    return overrides


def get_model_state(conn: sqlite3.Connection, model_id: str) -> Tuple[bool, bool]:
    """Return merged ``(is_active, is_visible)`` state for ``model_id``."""

    try:
        row = conn.execute(
            "SELECT is_active, is_visible FROM model_overrides WHERE model_id = ?",
            (model_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None

    if row is None:
        return get_default_model_state(model_id)

    is_active = row["is_active"] if isinstance(row, sqlite3.Row) else row[0]
    is_visible = row["is_visible"] if isinstance(row, sqlite3.Row) else row[1]
    return _normalize_bool(is_active), _normalize_bool(is_visible)


def apply_overrides_to_models(
    models: Iterable[dict], overrides: Dict[str, ModelOverride]
) -> list[dict]:
    """Return copies of ``models`` with ``available``/state fields merged."""

    merged: list[dict] = []
    for model in models:
        model_id = model.get("id")
        default_active, default_visible = get_default_model_state(model_id)
        override = overrides.get(model_id, {}) if model_id else {}
        is_active = _normalize_bool(
            override.get("is_active", default_active)
        )
        is_visible = _normalize_bool(
            override.get("is_visible", default_visible)
        )

        merged_model = dict(model)
        merged_model["is_active"] = is_active
        merged_model["is_visible"] = is_visible
        merged_model["available"] = bool(model.get("available", True)) and is_active and is_visible
        merged.append(merged_model)
    return merged


def upsert_model_override(
    conn: sqlite3.Connection,
    model_id: str,
    *,
    is_active: bool,
    is_visible: bool,
) -> None:
    """Persist ``model_id`` availability and visibility overrides."""

    try:
        conn.execute(
            """
            INSERT INTO model_overrides (model_id, is_active, is_visible, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(model_id) DO UPDATE SET
                is_active = excluded.is_active,
                is_visible = excluded.is_visible,
                updated_at = CURRENT_TIMESTAMP
            """,
            (model_id, int(bool(is_active)), int(bool(is_visible))),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise exc
