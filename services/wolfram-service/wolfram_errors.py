"""Parser for Wolfram Language error messages emitted by ``wolframscript -cloud``.

When ``wolframscript -cloud -code <expr>`` fails it writes a blob to stderr:

    Success. Saving connection data.\n
    Unable to evaluate expression.\n
    MessagesText:{"<Sym>::<tag> : <msg>", "<Sym2>::<tag2> : <msg2>"}

Each item is a JSON-encoded string. Some items are multi-line, e.g.
``ToExpression::sntx`` carries a caret row (``^``) pointing at the error column.

This module turns that blob into structured error records so the IDE can render
clean, classified messages (syntax / iterator / part / runtime / assignment)
and highlight the exact column for ``sntx`` errors instead of dumping the raw
"Success. Saving connection data." boilerplate at the user.
"""
from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Tag taxonomy. Keys are the lowercase ``<tag>`` portion of ``Sym::tag``.
# Driven by the message families listed in the Wolfram documentation
# (tutorial/Messages) plus the families we have actually observed in Phonos
# notebook imports: Statistik, ANOVA, Parameterschaetzung, Resonanz, etc.
# ---------------------------------------------------------------------------

CATEGORIES = (
    "syntax",        # sntx, sntxf, sntxi, sntxb, ...
    "iterator",      # nliter, iterb, iter, irtlw, ...
    "part",          # partw, pkspec, partd, pkshape, ...
    "list",          # lpx, lst, ...
    "type",          # argx, argrx, argt, argm, argtu, argmu, argrx, argb, ...
    "assignment",    # set, write, setps, setssl, ...
    "numeric",       # infy, div, divz, ovfl, undf, nnum, ...
    "fit",           # fitd, ...
    "heads",         # ...
    "cloud",         # srvc, ...
    "general",       # anything else, inc. General::stop / General:: nospc / etc.
)

_SYNTAX_TAGS = {
    "sntx", "sntxa", "sntxb", "sntxc", "sntxd", "sntxf", "sntxg",
    "sntxi", "sntxo", "sntxp", "sntxr", "sntxu", "sntxw", "sntxx",
    "prim1", "seq", "seqs",
}
_ITERATOR_TAGS = {"nliter", "iterb", "iter", "irtlw", "iterc", "iterd", "dvnoiter"}
_PART_TAGS = {"partw", "partd", "pkspec1", "pkspec", "pkshape", "psl", "psarg"}
_LIST_TAGS = {"lpx", "lst", "list", "lvlarg", "mtc", "ival", "ovls"}
_TYPE_TAGS = {
    "argx", "argrx", "argt", "argm", "argmu", "argtu", "argr", "argb",
    "argrx", "argtx", "argctu", "argct", "optb", "options", "bdec", "bbox",
    "bfnint", "biospec", "bform", "bgray", "blist", "blevel", "bmtd", "bname",
    "bobs", "bseq", "stride", "rvalue",
}
_ASSIGN_TAGS = {
    "set", "setps", "setssl", "setpsl", "write", "wretval", "tag",
    "protected", "readonly", "cfunc", "ovset",
}
_NUMERIC_TAGS = {"infy", "div", "divz", "ovfl", "undf", "nnum", "indet",
                  "ovls", "merror", "munfl", "movfl", "numq", "realset"}
_FIT_TAGS = {"fitd", "fitr"}
_HEADS_TAGS = {"heads", "hmmss", "rnmns"}
_CLOUD_TAGS = {"srvc", "srverr", "auth", "quot Wynne", "cloudobject", "inpac", "spof", "untx"}

def _classify_tag(tag: str) -> str:
    t = (tag or "").strip()
    if t in _SYNTAX_TAGS:    return "syntax"
    if t in _ITERATOR_TAGS:  return "iterator"
    if t in _PART_TAGS:      return "part"
    if t in _LIST_TAGS:      return "list"
    if t in _TYPE_TAGS:      return "type"
    if t in _ASSIGN_TAGS:    return "assignment"
    if t in _NUMERIC_TAGS:   return "numeric"
    if t in _FIT_TAGS:       return "fit"
    if t in _HEADS_TAGS:     return "heads"
    if t in _CLOUD_TAGS:     return "cloud"
    return "general"


# ``Sym::tag : rest`` — search anywhere in the entry (Wolfram pretty-prints
# some messages with a multi-line numerator/denominator *before* the tag,
# e.g. ``"  1\nPower::infy : Infinite expression - encountered.\n  0"``).
_SYM_TAG_RE = re.compile(
    r'(?P<sym>[A-Za-z][A-Za-z0-9$]*)::(?P<tag>[A-Za-z0-9]+)\s*:\s*(?P<rest>.*)',
    re.DOTALL,
)


def _parse_one(item: str) -> dict:
    """Parse one MessagesText entry, e.g. ``Table::nliter : Nonlist ...``."""
    m = _SYM_TAG_RE.search(item)
    if not m:
        # Non-tagged noise (rare): surface as general with the raw payload.
        return {
            "symbol": "",
            "tag": "",
            "category": "general",
            "message": item.strip(),
            "caret": None,
            "snippet": "",
            "prelude": "",
        }
    symbol = m.group("sym")
    tag = m.group("tag")
    rest = m.group("rest")
    prelude = item[: m.start()].rstrip()
    caret = None
    snippet = ""
    if tag in _SYNTAX_TAGS:
        # sntx messages include a caret line, ``^`` pointing at error column.
        # Extract the caret's column and the offending snippet if present.
        lines = rest.splitlines()
        for i, ln in enumerate(lines):
            stripped = ln.strip()
            if stripped and set(stripped) <= {"^"}:
                # column = number of leading spaces + 1
                col = len(ln) - len(ln.lstrip(" ")) + 1
                caret = col
                if i > 0 and "Invalid syntax in or before" in lines[i - 1]:
                    # try to pull the offending snippet from inside the quotes
                    sm = re.search(r'"([^"]*)"', lines[i - 1])
                    if sm:
                        snippet = sm.group(1)
                break
        # fall back: scan for a lone caret anywhere in the multiline rest
        if caret is None:
            cm = re.search(r'(?m)^\s*(\^+)\s*$', rest)
            if cm:
                line_start = rest.rfind("\n", 0, cm.start()) + 1
                col = cm.start() - line_start + 1
                caret = col
    # Keep the pretty-printed prelude (e.g. "  1\n  -\n  0" for 1/0) so the
    # user still sees the offending expression, and append the tagged message.
    message = rest.strip()
    if prelude:
        message = prelude + "\n" + message
    return {
        "symbol": symbol,
        "tag": tag,
        "category": _classify_tag(tag),
        "message": message,
        "caret": caret,        # 1-based column if known, else None
        "snippet": snippet,     # offending snippet (sntx)
        "prelude": prelude,     # pretty-printed expression preceding the tag
    }


# Strip the auth boilerplate that wolframscript prints even on success.
_BOILERPLATE_RE = re.compile(
    r"^(?:Success\. Saving connection data\.\s*|Activating cloud account\s*...\s*)\n",
    re.MULTILINE,
)


def parse_error_blob(raw: str) -> dict:
    """Parse a raw ``error`` string from ``_run_text`` into structured errors.

    Returns ``{has_errors, messages: [...]}`` where each message is a dict
    produced by :func:`_parse_one`. If the blob isn't a Wolfram error blob
    (no ``MessagesText:`` block) we return ``{has_errors: False, raw: ...}``
    so the caller can keep the existing behaviour for unrelated failures
    (timeouts, auth, etc.).
    """
    blob = raw or ""
    # Drop the line "Success. Saving connection data." that wolframscript prints
    # to *stdout/stderr* as part of the cloud activation handshake even when the
    # actual expression evaluated fine. Without this the user-facing error
    # string in the IDE starts with this auth spam.
    cleaned = _BOILERPLATE_RE.sub("", blob).strip()

    if "MessagesText:" not in cleaned:
        return {
            "has_errors": False,
            "raw": cleaned,
            "messages": [],
        }

    # Take the substring after ``MessagesText:``
    payload = cleaned.split("MessagesText:", 1)[1].strip()
    # The payload is a Wolfram-printed list of strings, e.g.
    #   {"ToExpression::sntx : ...", "General::stop : ..."}
    # Wolfram's ToString form uses ``{...}`` for lists — but that shape is NOT
    # valid JSON (it's an object literal with key-less entries). Convert the
    # outer braces into square brackets so json.loads sees a real JSON array.
    if payload.startswith("{") and payload.endswith("}"):
        json_payload = "[" + payload[1:-1] + "]"
    else:
        json_payload = payload
    # Wolfram uses ``\[Name]`` for named characters (e.g. ``\[Nu]1``), which
    # is not a valid JSON string escape. Escape the backslash so JSON treats
    # it as a literal ``\[`` sequence (we restore it after parsing).
    json_payload = json_payload.replace("\\[", "\\\\[")
    # Also tolerate Wolfram ``\<`` / ``\>`` word-boundary escapes (rare).
    json_payload = json_payload.replace("\\<", "\\\\<").replace("\\>", "\\\\>")
    try:
        items = json.loads(json_payload)
    except (json.JSONDecodeError, ValueError):
        # Be permissive: it may be a single tagged message w/o brackets.
        items = [payload]
    # ``json.loads`` already collapsed ``\\[`` back to ``\[`` in the parsed
    # strings, so ``\[Nu]1`` round-trips verbatim.

    if not isinstance(items, list):
        items = [items]

    messages: list[dict] = []
    for it in items:
        if not isinstance(it, str):
            # Some messages carry a nested list (rare). Stringify defensively.
            it = json.dumps(it, default=str)
        messages.append(_parse_one(it))

    # Categorise the overall error for quick IDE display.
    primary = next(iter(messages), None)
    return {
        "has_errors": True,
        "raw": cleaned,
        "messages": messages,
        "category": primary["category"] if primary else "general",
        "is_syntax_error": any(m["category"] == "syntax" for m in messages),
        "is_iterator_error": any(m["category"] == "iterator" for m in messages),
    }


def render_clean_error(parsed: dict) -> str:
    """Render a parsed blob back into a single human-readable multi-line string
    suitable for the IDE's ``error`` field. Much cleaner than the raw blob:

        Table::nliter (iterator): Nonlist iterator n at position 2 does not
        evaluate to a real numeric value.

        ToExpression::sntx (syntax): Invalid syntax in or before "Plot[x, {x,0,1] ".
                                                                    ^ (column 62)

    Suppresses the noisy ``General::stop`` follow-on by default (kept only when
    it's the only message).
    """
    if not parsed.get("has_errors"):
        return parsed.get("raw", "")
    msgs = [m for m in parsed["messages"] if m["tag"] != "stop"]
    if not msgs:
        msgs = parsed["messages"]
    lines = []
    for m in msgs:
        head = f"{m['symbol']}::{m['tag']}" if m["symbol"] else m["tag"]
        cat = f" ({m['category']})" if m["category"] != "general" else ""
        body = m["message"]
        col = ""
        if m.get("caret") is not None:
            col = f"\n  ^ at column {m['caret']}"
            if m.get("snippet"):
                col = f"\n  in: {m['snippet']!r}{col}"
        lines.append(f"{head}{cat}: {body}{col}")
    return "\n\n".join(lines)


# Convenience used by the wolfram-service: a single call that returns the
# structured dict AND a pre-rendered clean string.
def classify_warnings(blob: str) -> tuple[bool, str, dict]:
    """Return (is_wolfram_error_blob, cleaned_error_text, parsed_dict)."""
    parsed = parse_error_blob(blob)
    return parsed.get("has_errors", False), render_clean_error(parsed), parsed


# ---------------------------------------------------------------------------
# Self-test against real blob samples collected from the wolfram-service.
# Run ``python3 wolfram_errors.py`` to verify classification on those samples.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    samples = {
        "syntax_unclosed": (
            'Success. Saving connection data.\n'
            'Unable to evaluate expression.\n'
            'MessagesText:{"ToExpression::sntx : Invalid syntax in or before \\"Plot[x, {x,0,1] \\".\\n                                                                 ^"}\n'
        ),
        "iterator_bare_n": (
            'Unable to evaluate expression.\n'
            'MessagesText:{"Table::nliter : Nonlist iterator n at position 2 does not evaluate to a real numeric value."}\n'
        ),
        "iterator_bounds": (
            'Unable to evaluate expression.\n'
            'MessagesText:{"Table::iterb : Iterator {\\[Nu]1, 1, n, 5} does not have appropriate bounds."}\n'
        ),
        "fitd": (
            'Unable to evaluate expression.\n'
            'MessagesText:{"Fit::fitd : The first argument xyL in Fit is not a list or a rectangular array."}\n'
        ),
        "part_out_of_range": (
            'Unable to evaluate expression.\n'
            'MessagesText:{"Part::partw : Part 10 of {1, 2, 3, 4, 5} does not exist."}\n'
        ),
        "infinite_expr": (
            'Unable to evaluate expression.\n'
            'MessagesText:{"                                  1\\nPower::infy : Infinite expression - encountered.\\n                                  0"}\n'
        ),
        "non_error_blob": "Authentication failed: check Wolfram ID and password",
        "timeout": "Execution timed out after 60s",
        "boilerplate_only": "Success. Saving connection data.\n",
        "multi": (
            'Unable to evaluate expression.\n'
            'MessagesText:{"Table::nliter : Nonlist iterator n at position 2 does not evaluate to a real numeric value.", "Fit::fitd : The first argument Table[RandomReal[], n, 2] in Fit is not a list or a rectangular array.", "General::stop : Further output of Table::nliter will be suppressed during this calculation."}\n'
        ),
        "greek_iter": (
            'Unable to evaluate expression.\n'
            'MessagesText:{"Table::iterb : Iterator {\\[Nu]1, 1, n, 5} does not have appropriate bounds."}\n'
        ),
    }
    ok = True
    for label, blob in samples.items():
        is_err, clean, parsed = classify_warnings(blob)
        print(f"\n=== {label} ===")
        print(f"  is_wolfram_error_blob = {is_err}")
        print(f"  category = {parsed.get('category')!r}")
        print(f"  is_syntax_error = {parsed.get('is_syntax_error')}")
        print(f"  is_iterator_error = {parsed.get('is_iterator_error')}")
        print(f"  parsed messages:")
        for m in parsed["messages"]:
            print(f"    - {m['symbol']}::{m['tag']} [{m['category']}] caret={m['caret']} snippet={m['snippet']!r}")
            print(f"        msg: {m['message'][:80]}")
        print(f"  rendered clean:")
        for ln in clean.split("-------------") if False else clean.split("\n"):
            print("    | " + ln)
        # assertions
        if label.startswith("syntax"):
            ok = ok and parsed.get("is_syntax_error") is True
            ok = ok and parsed["messages"][0]["caret"] is not None
        elif label.startswith("iterator"):
            ok = ok and parsed.get("is_iterator_error") is True
        elif label == "part_out_of_range":
            ok = ok and parsed["category"] == "part"
        elif label == "infinite_expr":
            ok = ok and parsed.get("category") == "numeric"
        elif label in ("non_error_blob", "timeout", "boilerplate_only"):
            ok = ok and is_err is False
        elif label == "multi":
            ok = ok and len(parsed["messages"]) == 3
            ok = ok and parsed["messages"][0]["category"] == "iterator"
            ok = ok and parsed["messages"][1]["category"] == "fit"
    print("\n" + ("ALL ASSERTIONS PASSED" if ok else "ASSERTION FAILURES — see above"))