"""Model lookup helpers."""
from __future__ import annotations

from config import Config

MODEL_NAME_LOOKUP = {model["id"]: model["name"] for model in Config.SUPPORTED_MODELS}
MODEL_SHORT_NAME_LOOKUP = {
    model["id"]: (model.get("short_name") or model["name"])
    for model in Config.SUPPORTED_MODELS
}

# Legacy / alternate identifiers that map onto a canonical SUPPORTED_MODELS id.
# Jobs and workers persist the numeric "type-N" ids (Boltz2 == "type-9"), while
# submissions, flows and the external bridge sometimes pass the friendly alias.
# Resolve them all so nothing user-facing ever shows a raw id like "type-9".
MODEL_ID_ALIASES = {
    "boltz2": "type-9",
    "type-boltz2": "type-9",
}


def canonical_model_id(model_id: str) -> str:
    """Map a model alias onto its canonical ``SUPPORTED_MODELS`` id."""
    if not model_id:
        return model_id
    return MODEL_ID_ALIASES.get(model_id, model_id)


def get_supported_model_name(model_id: str, short: bool = False) -> str:
    """Return a human readable name for a model identifier (canonical id or alias).

    ``short=True`` returns the compact ``short_name`` (e.g. "Boltz2") suited to
    chips/sidebars; otherwise the full ``name``. A known model never resolves to
    a raw "type-N" id.
    """
    if not model_id:
        return "Unknown model"
    canonical = canonical_model_id(model_id)
    if short:
        return MODEL_SHORT_NAME_LOOKUP.get(canonical) or MODEL_NAME_LOOKUP.get(canonical, model_id)
    return MODEL_NAME_LOOKUP.get(canonical, model_id)
