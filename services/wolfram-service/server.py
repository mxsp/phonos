"""Wolfram Language Execution Service (optimized)

Lightweight HTTP API that evaluates Wolfram Language code via wolframscript.

  Endpoints:
  POST /evaluate   Evaluate Wolfram Language code (returns text + optional image)
  POST /evaluate2  Single-pass mode: always returns image (used internally)
  POST /deploy    CloudDeploy a .nb string and return the embed URL
  GET  /health     Health check + capability flags
  GET  /cache/stats   Cache statistics (debug)
  POST /cache/clear   Clear result cache (debug)

Optimizations vs v1:
  * ThreadingHTTPServer: concurrent cells no longer block each other.
  * Single cloud call by default (InputForm); 2nd parallel PNG call only when the
    text result contains a graphic placeholder. Total latency drops from ~2.6s to
    ~1.3s for non-graphics cells, and ~1.4s (parallel) for graphics cells.
  * Hash-based result cache (default 5min TTL, max 1024 entries) for idempotent cells.
  * ImageSize and resolution are configurable per request.
  * Pool of worker threads serializes wolframscript calls to a sane bound
    (configurable via WOLFRAM_MAX_WORKERS, default 4) to avoid cloud rate limits.
  * Errors return structured messages (no cloud-activation guesswork).
"""

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import tempfile
import subprocess
import sys
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from typing import Any, Dict
from urllib.parse import urlparse

# Structured Wolfram error parser (turns the raw ``MessagesText:{...}`` blob
# from wolframscript into classified, caret-aware message records and a clean
# human-readable string). See wolfram_errors.py for the taxonomy.
try:
    from wolfram_errors import classify_warnings, render_clean_error
except ImportError:
    # Allow running server.py standalone (outside the container) without the
    # helper module — fall back to a passthrough that preserves the old shape.
    def classify_warnings(blob):  # type: ignore
        return False, blob or "", {"has_errors": False, "raw": blob or "", "messages": []}

    def render_clean_error(parsed):  # type: ignore
        return parsed.get("raw", "")

logger = logging.getLogger("wolfram-service")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

WOLFRAMSCRIPT = "/usr/bin/wolframscript"
HOST = os.environ.get("WOLFRAM_SERVICE_HOST", "0.0.0.0")
PORT = int(os.environ.get("WOLFRAM_SERVICE_PORT", "5100"))
USE_CLOUD = os.environ.get("WOLFRAM_USE_CLOUD", "true").lower() == "true"
DEFAULT_IMAGE_SIZE = int(os.environ.get("WOLFRAM_DEFAULT_IMAGE_SIZE", "400"))
MAX_WORKERS = int(os.environ.get("WOLFRAM_MAX_WORKERS", "4"))
CACHE_TTL_SECONDS = int(os.environ.get("WOLFRAM_CACHE_TTL", "300"))
CACHE_MAX_ENTRIES = int(os.environ.get("WOLFRAM_CACHE_MAX", "1024"))

_GRAPHICS_RE = re.compile(
    r"-(Graphics|Graphics3D|Image|GraphicsGrid|GraphicsRow|"
    r"ContourPlot|DensityPlot|Plot3D|RegionPlot|"
    r"VectorPlot|StreamPlot|Histogram|DiscretePlot|"
    r"BarChart|PieChart|BubbleChart|"
    r"GeoGraphics|Sound|"
    r"Image3D)\b"
)

# InputForm output is the literal "Graphics[{...}]" or "-Graphics-" form.
_GRAPHICS_FORMS_RE = re.compile(r"\b(Graphics|Graphics3D)\[|-(Graphics|Graphics3D|Image|Sound)\b")


def _is_graphics_expr(s: str) -> bool:
    if not s:
        return False
    if _GRAPHICS_RE.search(s):
        return True
    if _GRAPHICS_FORMS_RE.search(s):
        return True
    return False


_MOJIBAKE_PAIR_RE = re.compile(
    r"[\u00c2-\u00cf][\u0080-\u00bf]"
)


def _looks_like_mojibake(text):
    """Detect UTF-8-of-Latin-1 mojibake in text.

    The Wolfram Cloud sometimes returns non-ASCII bytes that the wolframscript
    subprocess decodes as Latin-1 (because the cloud's encoding hint is
    latin-1 / CP1252 rather than UTF-8). When those Latin-1 chars are then
    re-encoded as UTF-8 for the JSON response, you get classic mojibake.

    Signature: a Latin-1 high-bit first byte (U+00C2..U+00CF) immediately
    followed by a C1-control / Latin-1 second byte (U+0080..U+00BF). Each
    such pair represents one Unicode character that was mangled.

    We only return True when the text actually contains one of these pairs,
    to avoid false positives on legitimately short strings.
    """
    if not text or len(text) < 2:
        return False
    return bool(_MOJIBAKE_PAIR_RE.search(text))


def _detect_encoding(text):
    """Best-effort guess at the byte encoding the cloud used for the result.

    Returns 'utf-8' if the text looks clean, 'latin-1' if it shows classic
    mojibake (UTF-8 bytes that were decoded as Latin-1 and re-encoded).
    """
    if not text:
        return "utf-8"
    return "latin-1" if _looks_like_mojibake(text) else "utf-8"



# Transliterate non-ASCII characters in user-supplied code to Wolfram's
# \[Name] named-character syntax (or byte escapes for ones we don't have
# a name for). The wolframscript subprocess running under POSIX/C locale
# decodes argv bytes as Latin-1, so passing UTF-8 multibyte chars directly
# produces literal byte sequences (e.g. "\xCE\xB1" instead of "\u03b1").
# Wolfram's named-character syntax (\[Alpha], \[Pi], etc.) is ASCII-safe
# and always decodes correctly regardless of locale.
_NON_ASCII_NAMED = {
    "\u03b1": r"\[Alpha]", "\u03b2": r"\[Beta]", "\u03b3": r"\[Gamma]",
    "\u03b4": r"\[Delta]", "\u03b5": r"\[Epsilon]", "\u03b6": r"\[Zeta]",
    "\u03b7": r"\[Eta]", "\u03b8": r"\[Theta]", "\u03b9": r"\[Iota]",
    "\u03ba": r"\[Kappa]", "\u03bb": r"\[Lambda]", "\u03bc": r"\[Mu]",
    "\u03bd": r"\[Nu]", "\u03be": r"\[Xi]", "\u03bf": r"\[Omicron]",
    "\u03c0": r"\[Pi]", "\u03c1": r"\[Rho]", "\u03c3": r"\[Sigma]",
    "\u03c4": r"\[Tau]", "\u03c5": r"\[Upsilon]", "\u03c6": r"\[Phi]",
    "\u03c7": r"\[Chi]", "\u03c8": r"\[Psi]", "\u03c9": r"\[Omega]",
    "\u0391": r"\[CapitalAlpha]", "\u0392": r"\[CapitalBeta]",
    "\u0393": r"\[CapitalGamma]", "\u0394": r"\[CapitalDelta]",
    "\u0395": r"\[CapitalEpsilon]", "\u0396": r"\[CapitalZeta]",
    "\u0397": r"\[CapitalEta]", "\u0398": r"\[CapitalTheta]",
    "\u0399": r"\[CapitalIota]", "\u039a": r"\[CapitalKappa]",
    "\u039b": r"\[CapitalLambda]", "\u039c": r"\[CapitalMu]",
    "\u039d": r"\[CapitalNu]", "\u039e": r"\[CapitalXi]",
    "\u039f": r"\[CapitalOmicron]", "\u03a0": r"\[CapitalPi]",
    "\u03a1": r"\[CapitalRho]", "\u03a3": r"\[CapitalSigma]",
    "\u03a4": r"\[CapitalTau]", "\u03a5": r"\[CapitalUpsilon]",
    "\u03a6": r"\[CapitalPhi]", "\u03a7": r"\[CapitalChi]",
    "\u03a8": r"\[CapitalPsi]", "\u03a9": r"\[CapitalOmega]",
    "\u00b0": r"\[Degree]", "\u00b1": r"\[PlusMinus]",
    "\u00d7": r"\[Times]", "\u00f7": r"\[Divide]",
    "\u2192": r"\[RightArrow]", "\u2190": r"\[LeftArrow]",
    "\u21d2": r"\[DoubleRightArrow]", "\u21d0": r"\[DoubleLeftArrow]",
    "\u2264": r"\[LessEqual]", "\u2265": r"\[GreaterEqual]",
    "\u2260": r"\[NotEqual]", "\u2248": r"\[TildeEqual]",
    "\u221e": r"\[Infinity]", "\u2202": r"\[PartialD]",
    "\u2207": r"\[Del]", "\u222b": r"\[Integral]",
    "\u2211": r"\[Sum]", "\u220f": r"\[Product]",
    "\u221a": r"\[Sqrt]",
    "\u00e4": r"\[ADoubleDot]", "\u00f6": r"\[ODoubleDot]",
    "\u00fc": r"\[UDoubleDot]", "\u00c4": r"\[CapitalADoubleDot]",
    "\u00d6": r"\[CapitalODoubleDot]", "\u00dc": r"\[CapitalUDoubleDot]",
    "\u00df": r"\[SZ]", "\u00e9": r"\[EAcute]",
    "\u00f1": r"\[NTilde]",
}


def _encode_code_for_wolframscript(code):
    """Transliterate non-ASCII chars to Wolfram ASCII-safe syntax.

    Walks the code character-by-character, tracking whether we're inside a
    string literal (so we don't disturb user string contents). For non-ASCII
    chars outside strings, use the named-character syntax (\[Alpha], etc.).
    For non-ASCII chars inside strings, use Wolfram byte escapes (\:xxxx).
    """
    if not code:
        return code
    out = []
    i = 0
    in_string = False
    while i < len(code):
        ch = code[i]
        # Detect string boundary (Wolfram strings are only "...")
        if ch == "\"":
            prev = code[i - 1] if i > 0 else ""
            if prev != "\\":
                in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if ord(ch) < 128:
            out.append(ch)
            i += 1
            continue
        if in_string:
            out.append("\\:%02x%02x" % ((ord(ch) >> 8) & 0xFF, ord(ch) & 0xFF))
        else:
            replacement = _NON_ASCII_NAMED.get(ch)
            if replacement is not None:
                out.append(replacement)
            else:
                out.append("\\:%02x%02x" % ((ord(ch) >> 8) & 0xFF, ord(ch) & 0xFF))
        i += 1
    return "".join(out)


def _fix_encoding(text):
    """Fix double-encoding issues (UTF-8 bytes interpreted as Latin-1)."""
    if not text:
        return ""
    if _looks_like_mojibake(text):
        try:
            return text.encode("latin-1").decode("utf-8")
        except Exception:
            return text
    return text

def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# In-flight subprocess registry — enables /session/abort to interrupt a
# running evaluation. wolframscript processes are started in their own
# process group so an abort can kill the whole tree.
# ---------------------------------------------------------------------------
_RUNNING_PROCS: "dict[str, set]" = {}
_RUNNING_PROCS_LOCK = threading.Lock()


def _register_proc(session_id, proc):
    if not session_id:
        return
    with _RUNNING_PROCS_LOCK:
        _RUNNING_PROCS.setdefault(session_id, set()).add(proc)


def _unregister_proc(session_id, proc):
    if not session_id:
        return
    with _RUNNING_PROCS_LOCK:
        procs = _RUNNING_PROCS.get(session_id)
        if procs:
            procs.discard(proc)
            if not procs:
                _RUNNING_PROCS.pop(session_id, None)


def _abort_session_procs(session_id) -> int:
    """Kill every in-flight wolframscript process for a session. Returns count."""
    import signal as _signal
    with _RUNNING_PROCS_LOCK:
        procs = list(_RUNNING_PROCS.get(session_id, ()))
    killed = 0
    for proc in procs:
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
                killed += 1
            except Exception:
                pass
    return killed


def _wolfram_call(args, timeout, auth=None, session_id=None):
    """Run wolframscript with the given args; return (rc, stdout_bytes, stderr_text, duration).

    auth: optional (username, password) tuple. If provided AND USE_CLOUD, the
    subprocess is invoked with -username/-password (wolframscript does not read
    passwords from env vars for security).

    session_id: when provided, the subprocess is registered so /session/abort
    can kill it mid-evaluation (kernel interrupt semantics).

    Any "-code <code>" argument is transliterated to replace non-ASCII characters
    with Wolfram's ASCII-safe named-character syntax (e.g. \\[Alpha]) so that
    wolframscript running under the POSIX/C locale correctly receives the
    intended characters instead of literal byte sequences.
    """
    cmd = [WOLFRAMSCRIPT]
    if USE_CLOUD:
        cmd.append("-cloud")
    if auth:
        cmd.extend(["-username", auth[0], "-password", auth[1]])
    # Transliterate non-ASCII chars in any -code argument.
    new_args = []
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "-code" and i + 1 < len(args):
            new_args.append(a)
            new_args.append(_encode_code_for_wolframscript(args[i + 1]))
            skip_next = True
        else:
            new_args.append(a)
    cmd.extend(new_args)
    start = time.monotonic()
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _register_proc(session_id, proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                proc.kill()
            proc.communicate()
            return -1, b"", f"Execution timed out after {timeout}s", time.monotonic() - start
        if proc.returncode and proc.returncode < 0:
            # Killed by signal (e.g. /session/abort SIGKILL).
            return proc.returncode, out or b"", "Evaluation aborted", time.monotonic() - start
        return proc.returncode, out, _decode(err), time.monotonic() - start
    except Exception as e:
        return -1, b"", f"Subprocess error: {e}", time.monotonic() - start
    finally:
        if proc is not None:
            _unregister_proc(session_id, proc)


def _run_text(code, timeout, auth=None, session_id=None):
    """Return raw InputForm text via one cloud call."""
    rc, out, err, dur = _wolfram_call(
        ["-format", "InputForm", "-code", code], timeout, auth=auth, session_id=session_id)
    text = _decode(out).rstrip()
    err_clean = _fix_encoding(err)
    # Cloud mode with -format returns rc=0 but writes "Unable to evaluate
    # expression." to stderr on errors. Detect this and surface as failure.
    if "Unable to evaluate expression" in err_clean or "Syntax::" in err_clean:
        return 1, text, err_clean, dur
    # Detect auth failure so the caller can show a clear message.
    if auth and (
        "Authentication" in err_clean
        or "Invalid username" in err_clean
        or "Login failed" in err_clean
        or "WolframID" in err_clean
        or "Incorrect username or password" in err_clean
        or "Bad username/password" in err_clean
    ):
        return 2, text, "Authentication failed: check Wolfram ID and password", dur
    return rc, text, err_clean, dur


def _run_png(code, image_size, timeout, auth=None):
    """Return PNG bytes via one cloud call (rasterized)."""
    sized_code = code
    if "ImageSize" not in code:
        sized_code = f"Rasterize[{code}, ImageSize->{image_size}]"
    rc, out, err, dur = _wolfram_call(["-format", "PNG", "-code", sized_code], timeout, auth=auth)
    if rc == 0 and out.startswith(b"\x89PNG"):
        return out
    return None


def _parse_auth_header(value):
    """Parse "Basic <base64>" -> (user, pass). Returns None if missing/malformed."""
    if not value:
        return None
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    user, _, password = decoded.partition(":")
    if not user or not password:
        return None
    return user, password


# ---------- Result cache --------------------------------------------------------

class ResultCache:
    """Thread-safe LRU+TTL cache keyed by sha256(code)+image_size."""

    def __init__(self, max_entries=CACHE_MAX_ENTRIES, ttl=CACHE_TTL_SECONDS):
        self.max = max_entries
        self.ttl = ttl
        self._data = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(code, image_size):
        raw = f"{image_size}|{code}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, code, image_size):
        k = self._key(code, image_size)
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(k)
            if entry is None:
                self.misses += 1
                return None
            if now - entry["t"] > self.ttl:
                del self._data[k]
                self.misses += 1
                return None
            self.hits += 1
            entry["t"] = now  # refresh on hit
            return entry["v"]

    def put(self, code, image_size, value):
        k = self._key(code, image_size)
        with self._lock:
            if len(self._data) >= self.max:
                # Drop oldest by insertion order (dict is insertion-ordered).
                oldest = next(iter(self._data))
                self._data.pop(oldest, None)
            self._data[k] = {"t": time.monotonic(), "v": value}

    def stats(self):
        with self._lock:
            return {
                "size": len(self._data),
                "max": self.max,
                "ttl_s": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
            }

    def clear(self):
        with self._lock:
            self._data.clear()


_CACHE = ResultCache()


# ---------- Worker pool for cloud calls ----------------------------------------

_POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="wolfram")


def _structured_error(stderr_text, text_result=""):
    """Turn a raw wolframscript error blob into a structured error payload.

    Returns ``(error_string, messages_list)`` where ``error_string`` is the
    clean human-readable text (no ``Success. Saving connection data.`` spam,
    no ``General::stop`` noise) and ``messages_list`` is the per-message
    record list from wolfram_errors (each with symbol/tag/category/caret/
    snippet) suitable for the IDE's frontend to render inline markers.
    """
    blob = stderr_text or ""
    is_wolfram_error, clean_text, parsed = classify_warnings(blob)
    if is_wolfram_error:
        # Prefer the parsed clean text; fall back to the raw blob minus the
        # ``Unable to evaluate expression.`` line that wolframscript prints.
        return clean_text or parsed.get("raw", blob), parsed.get("messages", [])
    # Non-Wolfram error (timeout, auth, subprocess crash): keep raw text but
    # surface as a single ``general`` message so the frontend shape is uniform.
    raw = blob or text_result or "Wolfram execution failed"
    return raw, [{
        "symbol": "",
        "tag": "",
        "category": "general",
        "message": raw.strip(),
        "caret": None,
        "snippet": "",
        "prelude": "",
    }]


def _strip_trailing_null(text):
    if not text:
        return ""
    lines = text.splitlines()
    while lines and lines[-1].strip() == "Null":
        lines.pop()
    return "\n".join(lines)


def evaluate_code(code: str, timeout: int = 60, image_size: int = DEFAULT_IMAGE_SIZE,
                  auth=None):
    """Evaluate Wolfram code and return a dict suitable for JSON encoding.

    auth: optional (user, pass) forwarded to wolframscript -username/-password.
          If None, falls back to container's WOLFRAM_USERNAME/WOLFRAM_PASSWORD.

    Returns:
      {success, text, result, image, image_type, error, timings, encoding}
    """
    if not code or not code.strip():
        return {
            "success": False, "text": "", "result": "",
            "image": None, "image_type": None,
            "encoding": "utf-8",
            "error": "Empty code",
        }

    # Caching policy: only cache when no auth header was provided (i.e. the
    # container's global creds). Per-user requests are never cached to avoid
    # cross-user leakage via shared cache keys and to avoid serving cached
    # results to a request whose credentials failed.
    if auth is None:
        cached = _CACHE.get(code, image_size)
        if cached is not None:
            cached = dict(cached)
            cached["cached"] = True
            return cached

    timings = {}
    t0 = time.monotonic()

    # 1. Always fetch raw text result (InputForm) in one call.
    rc, text_result, stderr, dur_text = _run_text(code, timeout, auth=auth)
    timings["text_s"] = round(dur_text, 3)

    if rc != 0:
        # Detect and report encoding on error text too.
        encoding = _detect_encoding(stderr or text_result)
        err_str, messages = _structured_error(stderr, text_result)
        return {
            "success": False,
            "text": _strip_trailing_null(text_result),
            "result": "",
            "image": None,
            "image_type": None,
            "encoding": encoding,
            "error": err_str,
            "messages": messages,
            "timings": timings,
        }

    # Detect & normalize encoding (mojibake -> proper UTF-8).
    detected_encoding = _detect_encoding(text_result)
    text_result = _fix_encoding(text_result)

    # 2. If the text output looks like a graphic, fire a parallel PNG extraction.
    image_b64 = None
    image_type = None
    has_graphic = _is_graphics_expr(text_result)
    if has_graphic:
        png_future = _POOL.submit(_run_png, code, image_size, timeout, auth=auth)
        png_bytes = png_future.result(timeout=timeout + 5)
        dur_png = 0.0
        timings["image_s"] = round(dur_png, 3)
        if png_bytes:
            image_b64 = base64.b64encode(png_bytes).decode("ascii")
            image_type = "image/png"

    total = round(time.monotonic() - t0, 3)
    timings["total_s"] = total

    # When a graphic is being returned as an image, suppress the raw Graphics[...]
    # dump from the text field. The dump can be hundreds of KB of Line[{...}]
    # coordinates which clutters the cell output panel.
    if has_graphic and image_b64:
        # Try to keep a short, useful caption: the short-form placeholder
        # (-Graphics-, -Graphics3D-, etc.) plus the bounding box size if available.
        text_for_output = "-Graphics-"
        m = _GRAPHICS_RE.search(text_result)
        if m:
            text_for_output = "-" + m.group(1) + "-"
        # Include user-visible output that came BEFORE the Graphics[...] dump.
        head, sep, _tail = text_result.partition("Graphics[")
        if sep and head.strip():
            text_for_output = head.strip() + "\n" + text_for_output
    else:
        text_for_output = _strip_trailing_null(text_result)

    result = {
        "success": True,
        "text": text_for_output,
        "result": text_result if not (has_graphic and image_b64) else "",
        "image": image_b64,
        "image_type": image_type,
        "encoding": "utf-8",  # text in this response is always UTF-8
        "raw_encoding": detected_encoding,  # what the cloud actually used
        "timings": timings,
    }
    # Don't cache per-user results.
    if auth is None:
        _CACHE.put(code, image_size, {k: v for k, v in result.items() if k != "cached"})
    return result


# ---------- Persistent kernel sessions (state across cells) ------------------
#
# Each call to /evaluate spawns a FRESH wolframscript, so variables set in
# cell1 (``a = 1``) are gone when cell2 (``a + 2``) runs. /evaluate-session
# keeps ONE long-lived wolframscript subprocess per ``session_id`` so the
# Wolfram kernel's ``$Context`` persists between cells, exactly like a
# Jupyter kernel. The Phonos Notebook IDE passes the kernel ``session_token``
# as session_id, so every cell in a connected notebook shares state.
#
# Code is driven over the subprocess stdin. Each request appends:
#     <user code>; $phonosResult = %;
#     Print["@PHONOS_OUT_START_<nonce>@"];
#     Print[InputForm[$phonosResult]];
#     Print["@PHONOS_OUT_END_<nonce>@"];
#     <quiet rasterize + BaseEncode, captured between IMG sentinels>
# and we read stdout until the OUT_END sentinel (and the IMG sentinel that
# follows it) appears.

_PERSISTENT_SESSIONS_ENABLED = os.environ.get("WOLFRAM_PERSISTENT_SESSIONS", "true").lower() == "true"
_SESSION_IDLE_TTL = int(os.environ.get("WOLFRAM_SESSION_IDLE_TTL", "1800"))   # 30 min
_SESSION_SWEEP_INTERVAL = int(os.environ.get("WOLFRAM_SESSION_SWEEP_INTERVAL", "60"))
_SESSION_MAX_TITLE_LOOKBACK = 16384
_SESSIONS: "dict[str, WolframKernelSession]" = {}
_SESSIONS_LOCK = threading.Lock()

# Cap on the number of fragments the recovery pass will re-evaluate, to keep
# failure latency bounded (each fragment is one cloud call, ~1.3s). Cells with
# more top-level statements than this skip fragment-recovery and report the
# original whole-cell error.
_FRAGMENT_RECOVERY_MAX = 12


def _split_top_level_statements(code: str) -> list:
    """Split Wolfram code into top-level statements separated by ';'.

    Respects ``()``/``[]``/``{}`` bracket nesting, ``"..."`` string literals
    (with ``\\`` escapes), and ``(* ... *)`` comments, so a ';' inside any of
    those does not start a new statement. Trailing ';' produces no empty
    fragment. Returns a list of non-empty stripped statement strings.
    """
    fragments = []
    buf = []
    depth = 0
    in_string = False
    in_comment = False
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if in_comment:
            buf.append(ch)
            if ch == "*" and nxt == ")":
                buf.append(nxt)
                i += 2
                in_comment = False
                continue
            i += 1
            continue
        if in_string:
            buf.append(ch)
            if ch == "\\" and nxt:
                buf.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == "(" and nxt == "*":
            in_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch in "([{":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and depth == 0:
            # ``;;`` is the Wolfram ``Span`` operator (e.g. ``x[[1 ;; n]]``),
            # NOT two statement separators. If the next char is also ``;`` we
            # must keep them together as part of the current fragment —
            # splitting would destroy the Span and reconstruct malformed
            # expressions like ``{} ;; d1ANOVA`` that Wolfram then tries to
            # treat as an assignment target (``Set::write: Tag Span ... is
            # Protected``).
            if nxt == ";":
                buf.append(";;")
                i += 2
                continue
            frag = "".join(buf).strip()
            if frag:
                fragments.append(frag)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    final = "".join(buf).strip()
    if final:
        fragments.append(final)
    return fragments


class WolframKernelSession:
    """Simulated persistent Wolfram session using state accumulation.

    ``wolframscript -cloud`` does not support interactive stdin mode (it
    requires ``-code``/``-format`` flags). Instead of running a long-lived
    subprocess that keeps kernel state in memory, we accumulate every
    successfully evaluated cell in ``_code_history`` and prepend all prior
    cells before each new single-shot ``wolframscript -cloud -code "..."``
    call.  This gives semantically equivalent behaviour for the vast
    majority of notebooks — variables, function definitions, and ``%``
    (last output) all work as expected — without requiring a local Wolfram
    Engine licence.
    """

    def __init__(self, session_id: str, auth=None):
        self.session_id = session_id
        self.auth = auth
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        self._code_history: list[str] = []
        # Set by /session/abort while an evaluation is in flight. The running
        # wolframscript subprocess(es) are SIGKILLed and evaluate() notices
        # this flag to return a clean "interrupted" result instead of a
        # confusing subprocess error (and skips fragment recovery).
        self._abort_event = threading.Event()
        # Stdout boundary sentinel. History replay re-runs every prior cell,
        # so side-effect output (Print, Echo, ...) from those cells would be
        # re-emitted into the CURRENT cell's stdout on every evaluation. We
        # insert ``Print["<sentinel>"]`` between the replayed history and the
        # new cell's code, then return only the text after the sentinel. The
        # token is random per session so user code can't collide with it.
        self._boundary = f"<<PHONOS:CELL:{secrets.token_hex(8)}>>"
        # Names of packages already loaded via ``Needs["name`"]``. Each
        # ``wolframscript -cloud -code ...`` invocation is a FRESH kernel, so
        # we must re-``Needs`` the package on every cell that uses it — but
        # only ONCE per cell, otherwise the second ``Needs`` call runs after
        # ``Global`Symbol`` has been created by the user's code and produces
        # ``ANOVA::shdw`` / ``Bonferroni::shdw`` etc. shadow warnings. We dedup
        # by copying the FIRST ``Needs["X`"]`` statement per package into the
        # prepended history and dropping later duplicates.
        self._loaded_packages: set[str] = set()
        # Pattern: matches a top-level ``Needs["Context`"]`` statement with
        # optional surrounding whitespace and a trailing ``;``. We use this
        # both to extract the package name and to strip the statement from the
        # code string.
        self._needs_re = re.compile(
            r'^\s*Needs\s*\[\s*"([^"`]+)`"\s*\]\s*;?\s*$'
        )

    def _filter_needs(self, code: str) -> tuple[str, set]:
        """Return ``(code_with_deduped_Needs, loaded_packages_in_code)``.

        Modifies ``_loaded_packages`` so subsequent cells skip ``Needs`` for
        packages this session already loaded. Returns the code with duplicate
        ``Needs`` lines removed (only the FIRST ``Needs["X`"]`` per session
        survives; later ones are dropped to silence shadow warnings).
        """
        # Split the code into top-level statements so we can drop individual
        # ``Needs`` lines without disturbing the rest of the cell.
        fragments = _split_top_level_statements(code)
        # Whitespace-stripped comparison set for the dedup decision.
        kept: list[str] = []
        loaded_in_this_code: set[str] = set()
        for frag in fragments:
            m = self._needs_re.match(frag)
            if m:
                pkg = m.group(1) + "`"
                loaded_in_this_code.add(pkg)
                if pkg in self._loaded_packages:
                    # Skip this Needs statement: package already loaded earlier
                    # in this session's history. Re-running it would re-begin
                    # the package context after ``Global`Symbol`` exists,
                    # which produces ``Sym::shdw`` shadow warnings.
                    continue
                self._loaded_packages.add(pkg)
                kept.append(frag)
            else:
                kept.append(frag)
        return ("; ".join(kept)) if kept else "", loaded_in_this_code

    def _build_full_code(self, extra: str, include_boundary: bool = True) -> str:
        """Build the code string to send to wolframscript for one request.

        Prepends the saved history (with ``Needs[]`` deduplication) and the
        new ``extra`` code (also Needs-deduplicated). Each piece is stripped
        of trailing ``;`` before being joined with a single ``;`` separator so
        they don't accidentally form a Wolfram ``;;`` (Span) operator: history
        entries frequently end in ``;`` (preserved from user cells like
        ``assn;``), and ``"; ".join(["assn;", "next"])`` would otherwise
        produce ``assn;;  next`` which Wolfram parses as
        ``Span[assn, next]`` — exactly the ``Set::write: Tag Span … is
        Protected`` errors we were seeing on cells that follow assignments.
        """
        # 1) Filter and join the existing history. ``Needs`` lines for
        #    packages already in ``_loaded_packages`` are dropped.
        filtered_history: list[str] = []
        # ``_loaded_packages`` is mutated by ``_filter_needs``; to keep the
        # first-occurrence semantics across history + new code we reset and
        # rebuild the set as we walk the history left-to-right.
        seen_packages: set[str] = set()
        for frag in self._code_history:
            m = self._needs_re.match(frag)
            if m:
                pkg = m.group(1) + "`"
                if pkg in seen_packages:
                    continue
                seen_packages.add(pkg)
                filtered_history.append(frag)
            else:
                filtered_history.append(frag)
        # Sync the canonical set so future cells see all loaded packages.
        self._loaded_packages = seen_packages
        # 2) Filter Needs lines out of the NEW code too (in case the new cell
        #    re-runs Needs for an already-loaded package — drop it).
        new_code = self._filter_needs(extra)[0] if extra else ""

        def _strip_trailing_semicolons(s: str) -> str:
            """Remove trailing ``;`` chars (and surrounding whitespace) so we
            never accidentally glue two history pieces together with ``;;``.
            We do NOT touch ``;;`` inside fragments (those are legitimately
            Wolfram Span operators in user code such as ``x[[1;;n]]``)."""
            return re.sub(r"[\s;]+$", "", s)

        # Wolfram Cloud's ToExpression cannot parse a comment followed by ``;``
        # at the start of code (``(*...*);code`` triggers ToExpression::sntx).
        # Replace comment-only fragments with Null, which evaluates the same.
        _comment_re = re.compile(r'^\s*\(\*.*\*\)\s*$', re.DOTALL)

        def _clean(pieces_in):
            cleaned = [
                'Null' if _comment_re.match(p) else _strip_trailing_semicolons(p)
                for p in pieces_in
            ]
            return [p for p in cleaned if p]

        history_pieces = _clean(filtered_history)
        new_pieces = _clean([new_code] if new_code else [])
        # Mark where the replayed history ends so ``_slice_after_boundary``
        # can drop the history's re-emitted Print/Echo output from the
        # current cell's stdout. Print (not WriteString) — only Print output is
        # captured by wolframscript here. Under ``-format InputForm`` Print
        # renders the sentinel string WITH surrounding quotes + a trailing
        # newline; ``_slice_after_boundary`` strips that closing quote/newline.
        if include_boundary and history_pieces and new_pieces:
            history_pieces.append(f'Print["{self._boundary}"]')
        return ";".join(history_pieces + new_pieces)

    _BOUNDARY_RE = re.compile(r'<<PHONOS:CELL:[a-f0-9]{16}>>')

    def _slice_after_boundary(self, text: str) -> str:
        """Return only the stdout produced after the boundary sentinel.

        When there was no history (no sentinel emitted) the text is returned
        unchanged. Uses ``rfind`` so a (pathological) user echo of the token
        earlier in history still resolves to the last, real boundary. Because
        ``Print`` renders the sentinel string as ``"<sentinel>"`` under
        InputForm, the char immediately after the matched (bare) sentinel is
        the InputForm closing quote, followed by Print's newline — both are
        stripped so the real output starts cleanly.

        If the exact boundary is not found, falls back to a regex match over
        the whole ``<<PHONOS:CELL:...>>`` pattern, so that a mismatched
        boundary (e.g. the cloud returned Print output on a different stream
        or with unexpected formatting) does not let the sentinel leak.
        """
        if not text:
            return text
        idx = text.rfind(self._boundary)
        if idx == -1:
            m = self._BOUNDARY_RE.search(text)
            if m:
                idx = m.start()
                boundary_len = m.end() - m.start()
            else:
                return text
        else:
            boundary_len = len(self._boundary)
        after = text[idx + boundary_len:]
        if after.startswith('"'):
            after = after[1:]
        if after.startswith("\r\n"):
            after = after[2:]
        elif after.startswith("\n"):
            after = after[1:]
        return after

    def evaluate(self, code: str, timeout: int, image_size: int, persist: bool = True):
        """Evaluate one cell, preserving prior state by prepending history.

        ``persist=False`` evaluates against the current session state without
        recording the cell in the replay history — used for read-only probes
        (e.g. the Python bridge's ``get``) so they don't grow the history.
        """
        self._abort_event.clear()
        full_code = self._build_full_code(code)

        t0 = time.monotonic()
        rc_text, text_result, stderr, dur_text = _run_text(
            full_code, timeout, auth=self.auth, session_id=self.session_id,
        )
        if self._abort_event.is_set():
            return self._interrupted_result(t0)
        # Drop replayed history side-effect output (Print/Echo from prior
        # cells) so each cell only shows its own stdout. Must happen before
        # the graphics check: a prior ``Print[Plot[...]]`` would otherwise
        # leave a ``-Graphics-`` marker in the replayed text and wrongly
        # trigger rasterization of non-graphics cells.
        text_result = self._slice_after_boundary(text_result)
        timings = {"text_s": round(dur_text, 3)}

        if rc_text != 0:
            # Error "creep" guard: a cell like ``n=20; Print@fVers`` fails as
            # a whole when ``Print@fVers`` errors, which would discard the
            # ``n=20`` side-effect too. Later cells depending on ``n`` then
            # fail with "iterator does not have appropriate bounds", making
            # it look like the original error propagated. To prevent that,
            # split the NEW cell into top-level statements and re-evaluate
            # each one independently, appending the ones that succeed to
            # ``_code_history`` so their side-effects persist. The first
            # failing fragment supplies the reported error. This rescues
            # successful prefixes (assignments, definitions) and stops the
            # creep. Single-statement cells skip the recovery (no benefit,
            # cost). Cells with too many fragments skip it too (latency).
            #
            # CRUCIAL: we do NOT stop at the first failing fragment. A single
            # erroring statement must not abort the rest of the cell's
            # *independent* statements — otherwise that one error "transpires
            # through the whole runtime" (later definitions/assignments in the
            # same cell never run, so subsequent cells fail too). We therefore
            # CONTINUE past a failing fragment, keep appending the ones that
            # succeed, and report the FIRST error. Dependent fragments that
            # fail as a consequence are silently tolerated (their error is
            # not surfaced) so the user sees one clean error, not a cascade.
            fragments = _split_top_level_statements(code)
            if 2 <= len(fragments) <= _FRAGMENT_RECOVERY_MAX and not _is_graphics_expr(text_result):
                recovery_err_str = None
                recovery_messages = []
                recovery_encoding = "utf-8"
                all_succeeded = True
                first_failure_seen = False
                last_text = ""
                last_rc = 0
                extra_dur = 0.0
                for frag in fragments:
                    if self._abort_event.is_set():
                        return self._interrupted_result(t0)
                    frag_full = self._build_full_code(frag)
                    rc_f, text_f, err_f, dur_f = _run_text(
                        frag_full, timeout, auth=self.auth, session_id=self.session_id,
                    )
                    text_f = self._slice_after_boundary(text_f)
                    extra_dur += dur_f
                    if rc_f == 0:
                        # Fragment succeeded: side-effect captured. Append so
                        # subsequent cells see it (e.g. ``n=20``).
                        if persist:
                            self._code_history.append(frag)
                        last_text = text_f
                        last_rc = rc_f
                    else:
                        all_succeeded = False
                        if not first_failure_seen:
                            # Report the FIRST failing fragment's structured
                            # error. Do not append the failing fragment.
                            first_failure_seen = True
                            recovery_encoding = _detect_encoding(err_f or text_f)
                            recovery_err_str, recovery_messages = _structured_error(
                                err_f, text_f,
                            )
                        # Keep going: an independent later statement may still
                        # succeed and must persist its side-effect. A dependent
                        # later statement that also fails is tolerated (not
                        # surfaced) to avoid a confusing error cascade.
                timings["text_s"] = round(dur_text + extra_dur, 3)
                if not all_succeeded:
                    return {
                        "success": False,
                        "text": "",
                        "result": "",
                        "image": None,
                        "image_type": None,
                        "encoding": recovery_encoding,
                        "error": recovery_err_str,
                        "messages": recovery_messages,
                        "timings": timings,
                    }
                # All fragments succeeded individually (whole-cell failure was
                # likely a parse/order artifact). Treat as success using the
                # last fragment's text. Return directly rather than falling
                # into the graphic-rasterize path (which would re-evaluate the
                # whole cell and double-append).
                last_text = _fix_encoding(last_text) if last_rc == 0 else ""
                timings["total_s"] = round(time.monotonic() - t0, 3)
                return {
                    "success": True,
                    "text": _strip_trailing_null(last_text),
                    "result": last_text,
                    "image": None,
                    "image_type": None,
                    "encoding": "utf-8",
                    "timings": timings,
                }
            else:
                encoding = _detect_encoding(stderr or text_result)
                err_str, messages = _structured_error(stderr, text_result)
                return {
                    "success": False,
                    "text": _strip_trailing_null(text_result),
                    "result": "",
                    "image": None,
                    "image_type": None,
                    "encoding": encoding,
                    "error": err_str,
                    "messages": messages,
                    "timings": timings,
                }

        detected_encoding = _detect_encoding(text_result)
        text_result = _fix_encoding(text_result)

        image_b64 = None
        image_type = None
        has_graphic = _is_graphics_expr(text_result) or _is_graphics_expr(code)
        if has_graphic:
            # Build the code string for rasterization WITHOUT the boundary
            # sentinel Print[] statement! In Wolfram Cloud, Rasterize[] of a
            # CompoundExpression with Print[] side-effects can literally
            # rasterize the generated "Print" cell containing the sentinel text.
            # Building without the boundary eliminates this problem entirely.
            raster_code = self._build_full_code(code, include_boundary=False)
            rc_png, png_bytes, _, _ = _wolfram_call(
                ["-format", "PNG", "-code",
                 f"Rasterize[{raster_code}, ImageSize->{image_size}]"],
                timeout, auth=self.auth, session_id=self.session_id,
            )
            if self._abort_event.is_set():
                return self._interrupted_result(t0)
            timings["image_s"] = round(time.monotonic() - t0 - dur_text, 3)
            if rc_png == 0 and png_bytes.startswith(b"\x89PNG"):
                image_b64 = base64.b64encode(png_bytes).decode("ascii")
                image_type = "image/png"

        timings["total_s"] = round(time.monotonic() - t0, 3)

        if has_graphic:
            if image_b64:
                text_for_output = "-Graphics-"
                m = _GRAPHICS_RE.search(text_result)
                if m:
                    text_for_output = "-" + m.group(1) + "-"
            else:
                text_for_output = "[Graphics output - image generation failed]"
        else:
            text_for_output = _strip_trailing_null(text_result)

        if persist:
            self._code_history.append(code)

        return {
            "success": True,
            "text": text_for_output,
            "result": "" if has_graphic else text_result,
            "image": image_b64,
            "image_type": image_type,
            "encoding": "utf-8",
            "timings": timings,
        }

    def _interrupted_result(self, t0):
        """Result payload for an evaluation killed by /session/abort.

        The aborted cell is NOT appended to the replay history, so session
        state stays exactly as it was before the interrupted cell ran.
        """
        return {
            "success": False,
            "text": "",
            "result": "",
            "image": None,
            "image_type": None,
            "encoding": "utf-8",
            "error": "Evaluation interrupted by user",
            "interrupted": True,
            "messages": [],
            "timings": {"total_s": round(time.monotonic() - t0, 3)},
        }

    def abort(self) -> int:
        """Interrupt the in-flight evaluation (if any). Returns procs killed."""
        self._abort_event.set()
        return _abort_session_procs(self.session_id)

    def touch(self):
        self.last_used = time.monotonic()

    def close(self):
        """No-op: no subprocess to tear down."""
        pass


def _get_or_create_session(session_id: str, auth=None) -> WolframKernelSession:
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(session_id)
        if sess is None:
            sess = WolframKernelSession(session_id, auth=auth)
            _SESSIONS[session_id] = sess
        sess.touch()
        return sess


def _sweep_sessions():
    while True:
        time.sleep(_SESSION_SWEEP_INTERVAL)
        now = time.monotonic()
        to_close = []
        with _SESSIONS_LOCK:
            for sid, sess in _SESSIONS.items():
                if now - sess.last_used > _SESSION_IDLE_TTL:
                    to_close.append(sid)
            for sid in to_close:
                _SESSIONS.pop(sid, None)
        for sid in to_close:
            logger.warning("Reaping idle Wolfram kernel session %s", sid)


def evaluate_code_session(session_id: str, code: str, timeout: int = 60,
                          image_size: int = DEFAULT_IMAGE_SIZE, auth=None,
                          persist: bool = True):
    if not code or not code.strip():
        return {
            "success": False, "text": "", "result": "",
            "image": None, "image_type": None, "encoding": "utf-8",
            "error": "Empty code",
        }
    sess = _get_or_create_session(session_id, auth=auth)
    with sess.lock:
        sess.touch()
        return sess.evaluate(code, timeout=timeout, image_size=image_size,
                             persist=persist)


# ---------- CloudDeploy (server-side .nb -> Wolfram Cloud) --------------------
#
# The Phonos backend POSTs {name, nb_content, permissions, timeout} here; we run
# `wolframscript -cloud` and let it call CloudDeploy[NotebookImport[String], ...]
# under the user's own Wolfram ID. The returned CloudObject is turned into a
# public-facing embed URL.
#
# Why server-side: the user's Wolfram password never reaches the browser. The
# backend resolves creds from user_wolfram_credentials (encrypted at rest with
# ENCRYPTION_KEY) and forwards a Basic-auth header to this container, which in
# turn hands it to wolframscript's -username/-password flags.
#
# Output URL pattern (verified against the Wolfram Cloud frontend):
#   CloudObject["Users/<WolframIDEmail>/<deployment>"] evaluates to
#   https://www.wolframcloud.com/obj/<WolframIDEmail>/<deployment>
#   (or https://www.wolframcloud.com/obj/<UUID> when Permissions->"Public"
#    is used with a top-level CloudDeploy).
# We resolve the canonical URL by asking the kernel: CloudObject[...]["URL"].

CLOUD_DEPLOY_TIMEOUT = int(os.environ.get("WOLFRAM_CLOUD_DEPLOY_TIMEOUT", "120"))


def _resolve_cloud_object_url(raw_output: str) -> str:
    """Pull the embeddable https:// URL out of wolframscript's stdout.

    wolframscript's InputForm printer returns a fully-qualified string for
    CloudObject addresses, e.g. ``CloudObject["Users/foo@example.com/abc"]`` or
    ``CloudObject["https://www.wolframcloud.com/obj/..."]``. We only need the
    inner URL/path for the iframe.
    """
    if not raw_output:
        return ""
    # Strip the CloudObject[...] wrapper if present.
    text = raw_output.strip()
    m = re.search(r'CloudObject\[(?:"([^"]+)"|(.+?))\]', text, re.DOTALL)
    candidate = ""
    if m:
        candidate = (m.group(1) or m.group(2) or "").strip().strip('"')
    else:
        # No wrapper — assume the whole string is the address.
        candidate = text.strip('"')
    if not candidate:
        return ""
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return candidate
    if candidate.startswith("Users/"):
        return "https://www.wolframcloud.com/obj/" + candidate[len("Users/"):]
    return "https://www.wolframcloud.com/obj/" + candidate.lstrip("/")


def deploy_notebook(name: str, nb_content: str, timeout: int = CLOUD_DEPLOY_TIMEOUT,
                    permissions: str = "Public", auth=None) -> Dict[str, Any]:
    """Deploy ``nb_content`` to the user's Wolfram Cloud and return the embed URL.

    Parameters
    ----------
    name : str
        Cloud deployment name (must be a valid CloudObject name fragment).
    nb_content : str
        Full text of the .nb file.
    permissions : str
        "Public" (default — embeds cleanly) or "Private" (only the owner can
        see it; embed still works because the iframe loads with the user's
        session).
    auth : (user, pass) tuple forwarded to wolframscript.

    Returns
    -------
    {success, url, deployment, raw, error, duration_s}
    """
    t0 = time.monotonic()
    if not name or not re.match(r"^[A-Za-z0-9_\-.]+$", name):
        return {"success": False, "error": "Invalid deployment name", "url": ""}
    if not nb_content or len(nb_content) > 5 * 1024 * 1024:
        return {"success": False, "error": "Notebook content missing or too large (>5 MB)", "url": ""}

    # Build a Wolfram Language script that decodes the notebook content,
    # deploys it to Wolfram Cloud, and prints the resulting URL between
    # sentinels. We write the script to a temp file and run it with
    # wolframscript -file. This avoids two problems with the previous approach:
    # (1) ImportString[b64, {"NB", "String"}] is not valid WL, and
    # (2) huge notebooks exceed the OS command-line length limit when passed as
    # a single -code argument. Running from a file lets us embed arbitrarily
    # large base64 strings and still execute in Wolfram Cloud mode.
    nonce = secrets.token_hex(8)
    sent_start = f"@PHONOS_DEPLOY_START_{nonce}@"
    sent_end = f"@PHONOS_DEPLOY_END_{nonce}@"
    safe_name = name.replace("\\", "\\\\").replace('"', '\\"')

    # Embed the notebook as a single base64 string. File line length is not
    # subject to the OS command-line limit, so one long literal is fine.
    b64_content = base64.b64encode(nb_content.encode("utf-8")).decode("ascii")

    script = (
        f'$b64 = "{b64_content}";\n'
        '$nb = ImportString[$b64, "Base64"];\n'
        f'$name = "{safe_name}";\n'
        f'$perms = "{permissions}";\n'
        '$co = CloudDeploy[$nb, CloudObject[$name], Permissions -> $perms];\n'
        '$url = $co[[1]];\n'
        f'Print["{sent_start}"];\n'
        'Print[$url];\n'
        f'Print["{sent_end}"];\n'
    )

    script_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".wl", delete=False
        ) as tmp:
            tmp.write(script)
            script_path = tmp.name

        rc, out, err, dur_wl = _wolfram_call(
            ["-format", "InputForm", "-file", script_path], timeout, auth=auth
        )
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except Exception:
                pass

    text = _decode(out).rstrip()
    err_clean = _fix_encoding(err)
    duration = round(time.monotonic() - t0, 3)

    # Detect Wolfram Cloud authentication failures whether credentials were
    # supplied per-request or via the container's global account.
    _auth_markers = (
        "Authentication failed", "Incorrect username or password",
        "Invalid username", "Login failed", "WolframID",
    )
    if rc == 2 or any(m in err_clean for m in _auth_markers):
        return {
            "success": False,
            "error": "Authentication failed: check Wolfram ID and password",
            "url": "",
            "duration_s": duration,
        }
    if rc != 0 and rc != -1:
        err_msg = err_clean or text or "CloudDeploy failed"
        if "CloudDeploy::srverr" in err_msg or "Cloud server is not able" in err_msg:
            err_msg = (
                "Wolfram Cloud could not complete the deployment. "
                "This is usually a temporary Wolfram Cloud issue or an account limitation. "
                "Please verify your Wolfram Cloud account can deploy objects and try again."
            )
        return {
            "success": False,
            "error": err_msg,
            "url": "",
            "duration_s": duration,
        }
    if rc == -1:
        return {
            "success": False,
            "error": err or "CloudDeploy timed out",
            "url": "",
            "duration_s": duration,
        }

    # Parse the sentinels.
    if sent_start not in text or sent_end not in text:
        return {
            "success": False,
            "error": "Could not parse CloudDeploy response: " + (text[:300] or err_clean[:300]),
            "url": "",
            "raw": text,
            "duration_s": duration,
        }
    inner = text.split(sent_start, 1)[1].split(sent_end, 1)[0].strip()
    url = _resolve_cloud_object_url(inner)
    if not url:
        return {
            "success": False,
            "error": "CloudDeploy returned an unparseable URL: " + inner[:200],
            "url": "",
            "raw": inner,
            "duration_s": duration,
        }
    return {
        "success": True,
        "url": url,
        "deployment": name,
        "duration_s": duration,
    }


# ---------- HTTP layer ----------------------------------------------------------

class WolframHandler(BaseHTTPRequestHandler):
    server_version = "WolframService/2.0"

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-response; nothing to do.
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            with _SESSIONS_LOCK:
                session_count = len(_SESSIONS)
            self._send_json(200, {
                "status": "ok",
                "use_cloud": USE_CLOUD,
                "engine": "wolframresearch/wolframengine",
                "max_workers": MAX_WORKERS,
                "cache": _CACHE.stats(),
                "default_image_size": DEFAULT_IMAGE_SIZE,
                "endpoints": ["/evaluate", "/evaluate-session", "/session/reset", "/session/abort", "/deploy"],
                "cloud_deploy_timeout": CLOUD_DEPLOY_TIMEOUT,
                "sessions": session_count,
                "session_idle_ttl_s": _SESSION_IDLE_TTL,
            })
        elif parsed.path == "/cache/stats":
            self._send_json(200, _CACHE.stats())
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/evaluate":
            self._handle_evaluate()
        elif parsed.path == "/evaluate-session":
            self._handle_evaluate_session()
        elif parsed.path == "/deploy":
            self._handle_deploy()
        elif parsed.path == "/session/reset":
            self._handle_session_reset()
        elif parsed.path == "/session/abort":
            self._handle_session_abort()
        elif parsed.path == "/cache/clear":
            _CACHE.clear()
            self._send_json(200, {"cleared": True})
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_session_reset(self):
        """Drop a persistent session's accumulated state (kernel restart)."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(content_length)) if content_length else {}
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {"error": f"Invalid request body: {e}"})
            return
        session_id = (data.get("session_id") or "").strip()
        if not session_id:
            self._send_json(400, {"error": "session_id is required"})
            return
        with _SESSIONS_LOCK:
            existed = _SESSIONS.pop(session_id, None) is not None
        self._send_json(200, {"reset": existed})

    def _handle_session_abort(self):
        """Interrupt a session's in-flight evaluation (kernel interrupt).

        Kills the running wolframscript subprocess tree and flags the session
        so the in-flight /evaluate-session request returns a clean
        "interrupted" result. Session state (replay history) is untouched.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(content_length)) if content_length else {}
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {"error": f"Invalid request body: {e}"})
            return
        session_id = (data.get("session_id") or "").strip()
        if not session_id:
            self._send_json(400, {"error": "session_id is required"})
            return
        with _SESSIONS_LOCK:
            sess = _SESSIONS.get(session_id)
        if sess is not None:
            killed = sess.abort()
        else:
            # No session object (e.g. stateless /evaluate) — still kill any
            # registered procs for this id.
            killed = _abort_session_procs(session_id)
        logger.info("Abort for session %s: killed %d process(es)", session_id[:12], killed)
        self._send_json(200, {"aborted": True, "killed": killed})

    def _handle_evaluate(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_length == 0:
            self._send_json(400, {"error": "No request body"})
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        code = data.get("code", "")
        try:
            timeout = int(data.get("timeout", 60))
            timeout = max(1, min(timeout, 600))  # hard cap to protect cloud quota
        except (TypeError, ValueError):
            timeout = 60
        try:
            image_size = int(data.get("image_size", DEFAULT_IMAGE_SIZE))
            image_size = max(64, min(image_size, 2400))
        except (TypeError, ValueError):
            image_size = DEFAULT_IMAGE_SIZE

        if not code or not code.strip():
            self._send_json(400, {"error": "No code provided"})
            return

        # Per-request Wolfram credentials via Basic auth header.
        # Header: X-Wolfram-Auth: Basic base64(user:pass)
        # If absent, falls back to container's WOLFRAM_USERNAME/WOLFRAM_PASSWORD.
        auth = _parse_auth_header(self.headers.get("X-Wolfram-Auth"))

        result = evaluate_code(code, timeout=timeout, image_size=image_size, auth=auth)
        if result.get("error") and "Authentication failed" in str(result["error"]):
            # Surface as 401 so the Phonos backend can prompt the user.
            result["http_status"] = 401
            self._send_json(401, result)
            return
        self._send_json(200, result)

    def _handle_evaluate_session(self):
        # Persistent-state variant of /evaluate. Reuses (or spawns) a long-
        # lived wolframscript subprocess keyed by ``session_id`` so that
        # variables defined in cell1 (e.g. ``a = 1``) are visible in cell2
        # (``a + 2``), exactly like a Jupyter kernel keeping $Context.
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_length == 0:
            self._send_json(400, {"error": "No request body"})
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        session_id = (data.get("session_id") or "").strip()
        if not session_id:
            self._send_json(400, {"error": "session_id is required for persistent evaluation"})
            return
        code = data.get("code", "")
        try:
            timeout = int(data.get("timeout", 60))
            timeout = max(1, min(timeout, 600))
        except (TypeError, ValueError):
            timeout = 60
        try:
            image_size = int(data.get("image_size", DEFAULT_IMAGE_SIZE))
            image_size = max(64, min(image_size, 2400))
        except (TypeError, ValueError):
            image_size = DEFAULT_IMAGE_SIZE
        if not code or not code.strip():
            self._send_json(400, {"error": "No code provided"})
            return

        auth = _parse_auth_header(self.headers.get("X-Wolfram-Auth"))
        persist = bool(data.get("persist", True))

        try:
            result = evaluate_code_session(
                session_id=session_id, code=code,
                timeout=timeout, image_size=image_size, auth=auth,
                persist=persist,
            )
        except Exception as e:
            logger.exception("Persistent evaluate crashed for session %s", session_id)
            self._send_json(500, {
                "success": False, "error": f"Persistent session error: {e}",
                "text": "", "image": None,
            })
            return

        if result.get("error") and "Authentication failed" in str(result["error"]):
            self._send_json(401, result)
            return
        self._send_json(200, result)

    def _handle_deploy(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_length == 0:
            self._send_json(400, {"error": "No request body"})
            return
        if content_length > 6 * 1024 * 1024:  # 5 MB nb + JSON overhead headroom
            self._send_json(413, {"error": "Request body too large"})
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        name = (data.get("name") or "").strip()
        nb_content = data.get("nb_content") or ""
        permissions = (data.get("permissions") or "Public").strip()
        try:
            timeout = int(data.get("timeout", CLOUD_DEPLOY_TIMEOUT))
            timeout = max(10, min(timeout, 600))
        except (TypeError, ValueError):
            timeout = CLOUD_DEPLOY_TIMEOUT

        if not name:
            self._send_json(400, {"error": "name is required"})
            return
        if not nb_content:
            self._send_json(400, {"error": "nb_content is required"})
            return
        if permissions not in ("Public", "Private"):
            permissions = "Public"

        auth = _parse_auth_header(self.headers.get("X-Wolfram-Auth"))
        try:
            result = deploy_notebook(name=name, nb_content=nb_content,
                                     timeout=timeout, permissions=permissions,
                                     auth=auth)
        except Exception as e:
            logger.exception("CloudDeploy crashed")
            self._send_json(500, {
                "success": False,
                "error": f"CloudDeploy failed: {e}",
                "url": "",
            })
            return

        if not result.get("success"):
            logger.warning("CloudDeploy failed for %s: %s", name, result.get("error"))
        if result.get("error") and any(
            m in str(result["error"])
            for m in ("Authentication failed", "Incorrect username or password")
        ):
            self._send_json(401, result)
            return
        status = 200 if result.get("success") else 500
        self._send_json(status, result)

    def log_message(self, format, *args):
        sys.stderr.write("[WolframService] %s - %s\n" % (self.log_date_time_string(), format % args))


def main():
    server = ThreadingHTTPServer((HOST, PORT), WolframHandler)
    print(
        f"Wolfram Language service v2 starting on {HOST}:{PORT} "
        f"(cloud={USE_CLOUD}, workers={MAX_WORKERS}, cache_ttl={CACHE_TTL_SECONDS}s, "
        f"persistent_sessions={_PERSISTENT_SESSIONS_ENABLED})",
        flush=True,
    )
    if _PERSISTENT_SESSIONS_ENABLED:
        t = threading.Thread(target=_sweep_sessions, name="wolfram-session-reaper", daemon=True)
        t.start()
        logger.info("Started persistent-kernel session reaper (idle TTL=%ss)", _SESSION_IDLE_TTL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...", flush=True)
        # Tear down all persistent kernel subprocesses on exit.
        with _SESSIONS_LOCK:
            sessions = list(_SESSIONS.values())
            _SESSIONS.clear()
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
        server.server_close()


if __name__ == "__main__":
    main()
