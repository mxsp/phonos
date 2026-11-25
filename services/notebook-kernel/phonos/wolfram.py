"""Python ↔ Wolfram Language bridge for Phonos notebooks (experimental).

Available from any Phonos notebook kernel; designed for the hybrid
"Quantum + Wolfram" runtime. Evaluations are keyed by the notebook session
token on the server, so code run here shares Wolfram kernel state with the
notebook's Wolfram cells: a variable assigned in a Wolfram cell is visible
to :func:`eval` and vice versa.

Quick reference::

    import phonos

    phonos.wolfram.eval('Integrate[Sin[x]^2, x]')      # -> WolframResult
    phonos.wolfram.get('Solve[x^2 == 4, x]')            # -> Python object (via JSON)
    phonos.wolfram.set('counts', {'00': 512, '11': 488})  # push Python data
    phonos.wolfram.eval('BarChart[Values[counts]]')     # use it from Wolfram

Requires Wolfram Cloud credentials (Notebook IDE Settings → Wolfram Account)
unless you are an admin using the platform account.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

_NAME_RE = re.compile(r'^[A-Za-z$][A-Za-z0-9$]*$')


class WolframBridgeError(RuntimeError):
    """Raised when a bridge evaluation fails (auth, transport, or Wolfram error)."""


class WolframResult:
    """Result of a bridge evaluation.

    Attributes:
        text:  textual output (InputForm) of the evaluated expression
        image: raw PNG bytes when the result was graphics, else None
        error: error string when the evaluation failed, else None
        data:  the raw service response dict
    """

    def __init__(self, data: dict):
        self.data = data or {}
        self.success = bool(self.data.get('success'))
        self.text = (self.data.get('text') or '').strip()
        self.error = self.data.get('error')
        self.messages = self.data.get('messages') or []
        self.image = None
        img_b64 = self.data.get('image')
        if img_b64:
            try:
                self.image = base64.b64decode(img_b64)
            except Exception:
                self.image = None

    def __bool__(self) -> bool:
        return self.success

    def __repr__(self) -> str:
        if not self.success:
            return f"WolframResult(error={self.error!r})"
        if self.image and not self.text:
            return "WolframResult(<graphics>)"
        return self.text or "WolframResult(<empty>)"

    def _repr_png_(self):
        """Jupyter inline display of graphics results."""
        return self.image

    def __str__(self) -> str:
        return self.text if self.success else (self.error or '')


def _bridge_request(code: str, timeout: int = 60,
                    image_size: Optional[int] = None,
                    persist: bool = True) -> dict:
    from phonos import _api_request, PhonosError  # lazy: avoid circular import
    payload: dict = {'code': code, 'timeout': timeout, 'persist': persist}
    if image_size is not None:
        payload['image_size'] = image_size
    try:
        return _api_request(
            'POST', '/api/notebooks/wolfram/bridge',
            data=payload, timeout=float(timeout) + 30.0,
        ) or {}
    except PhonosError as e:
        raise WolframBridgeError(str(e)) from None


def eval(code: str, timeout: int = 60,
         image_size: Optional[int] = None,
         raise_on_error: bool = True,
         persist: bool = True) -> WolframResult:
    """Evaluate Wolfram Language code in this notebook's Wolfram session.

    Returns a :class:`WolframResult`; graphics results display inline in
    Jupyter. Raises :class:`WolframBridgeError` on failure unless
    ``raise_on_error=False``. ``persist=False`` evaluates without recording
    the code in the session's replay state (read-only probe).
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty Wolfram Language string")
    result = WolframResult(_bridge_request(
        code, timeout=timeout, image_size=image_size, persist=persist))
    if not result.success and raise_on_error:
        raise WolframBridgeError(result.error or 'Wolfram evaluation failed')
    return result


# Explicit alias for callers who avoid shadowing the builtin.
evaluate = eval


def get(expr: str, timeout: int = 60) -> Any:
    """Evaluate ``expr`` in Wolfram and return it as a Python object.

    The expression is exported as JSON on the Wolfram side (RawJSON), so it
    must be JSON-representable (numbers, strings, lists, associations,
    True/False/Null). Falls back to the raw InputForm text when the value
    cannot be exported as JSON.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("expr must be a non-empty Wolfram Language expression")
    # Export the value to a JSON string on the Wolfram side, then Print it.
    # Under ``wolframscript -format InputForm`` Print renders that JSON string
    # in InputForm — i.e. wrapped in quotes with inner quotes escaped — so the
    # captured text is one extra layer of JSON-string encoding. json.loads once
    # peels that layer back to the raw JSON payload; a second parse yields the
    # Python value.
    probe = f'Print[ExportString[({expr}), "RawJSON", "Compact" -> True]]'
    result = eval(probe, timeout=timeout, persist=False, raise_on_error=False)
    text = (result.text or '').strip()
    if not result.success or not text or text == '$Failed':
        # Not JSON-representable (or ExportString failed) — return the plain
        # InputForm text of the expression instead.
        return eval(expr, timeout=timeout, persist=False).text
    try:
        raw = json.loads(text)          # undo the InputForm quoting layer
    except json.JSONDecodeError:
        raw = text
    if isinstance(raw, str):
        try:
            return json.loads(raw)      # parse the actual JSON payload
        except json.JSONDecodeError:
            return raw
    return raw


def set(name: str, value: Any, timeout: int = 60) -> None:
    """Assign ``value`` (any JSON-serializable Python object) to the Wolfram
    symbol ``name`` in this notebook's Wolfram session.

    dicts become Associations, lists become Lists, None becomes Null.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid Wolfram symbol name {name!r} "
            "(letters, digits and $ only; must not start with a digit)"
        )
    payload = json.dumps(value)
    escaped = payload.replace('\\', '\\\\').replace('"', '\\"')
    code = f'{name} = ImportString["{escaped}", "RawJSON"];'
    eval(code, timeout=timeout)


def help() -> str:  # noqa: A001 - deliberate SDK surface name
    return __doc__ or ''
