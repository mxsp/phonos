"""Multi-language cell execution for Phonos notebooks (chimera kernel).

Executes C, C++, Rust and R notebook cells *inside* the kernel container.
The Phonos backend wraps a lang cell's source into a one-line Python shim
(``phonos.lang.run('<lang>', '<base64 source>')``) and sends it down the
ordinary Jupyter execute path, so lang cells inherit the kernel's timeout,
interrupt (SIGUSR1 -> KeyboardInterrupt), stdin channel and live output
streaming.

Compiled languages (c / cpp / rust)
    Each cell is compiled with gcc / g++ / rustc and run as a child process
    on a pseudo-terminal. Cells without a ``main`` are auto-wrapped
    (includes/use-items hoisted, body placed in ``main``) so short snippets
    just work. When the program blocks reading stdin, the notebook's input
    dialog opens automatically; replies are echoed into the transcript.

    Pragmas (first lines of the cell)::

        //%flags: -O3 -lpthread     extra compiler flags
        //%args: alpha beta          argv for the program
        //%stdin: 3 4                pre-fed stdin (disables the input dialog)
        //%timeout: 120              run timeout in seconds

R (via rpy2, embedded — state persists across R cells)
    REPL semantics (visible values auto-print), warnings/messages on stderr,
    base-graphics plots captured as inline PNGs, ``readline()`` mapped to the
    notebook input dialog. Pragma prefix is ``#%`` (e.g. ``#%timeout: 120``).

    Python <-> R exchange::

        import phonos
        phonos.r.df = pandas_df          # push a DataFrame into R
        phonos.r('summary(df)')          # evaluate and fetch
        phonos.lang.r_get('fit')         # pull an R object into Python

Known limits (v1): no Cargo crates (single-file rustc) and base R packages
only; Rust ``print!`` without a newline needs ``io::stdout().flush()`` before
a blocking read.
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

BUILD_ROOT = '/tmp/phonos_build'
CACHE_DIR = os.path.join(BUILD_ROOT, 'cache')
CACHE_MAX_ENTRIES = 50
COMPILE_TIMEOUT = 60
RUN_CWD = '/workspace'
_POLL_SECS = 0.1
# consecutive quiet polls before the wchan fallback assumes "waiting for stdin"
_QUIET_TICKS = 3

LANGS = ('c', 'cpp', 'rust', 'r')


# ---------------------------------------------------------------------------
# Errors — rendered via IPython's _render_traceback_ protocol so the notebook
# shows exactly the lines we choose (compiler diagnostics, R messages, hints)
# instead of a Python traceback into this module.
# ---------------------------------------------------------------------------

class LangError(Exception):
    def __init__(self, message: str, tb_lines: Optional[list] = None):
        super().__init__(message)
        self._tb_lines = list(tb_lines) if tb_lines else [message]

    def _render_traceback_(self):
        return self._tb_lines


class CompileError(LangError):
    """Compilation failed; traceback = the compiler diagnostics."""


class RunFailedError(LangError):
    """The program exited abnormally (nonzero exit code or fatal signal)."""


class RuntimeNotAvailableError(LangError):
    """The language runtime is missing from this kernel container."""


class RError(LangError):
    """An R evaluation error; traceback = R's own error message."""


_SIGNAL_HINTS = {
    signal.SIGSEGV: 'Segmentation fault (SIGSEGV) — likely an invalid pointer or out-of-bounds array access.',
    signal.SIGFPE: 'Floating point exception (SIGFPE) — e.g. integer division by zero.',
    signal.SIGABRT: 'Aborted (SIGABRT) — assert()/abort() fired or heap corruption was detected.',
    signal.SIGBUS: 'Bus error (SIGBUS) — misaligned or invalid memory access.',
    signal.SIGKILL: 'Killed (SIGKILL) — most likely out of memory (container limit ~2g).',
    signal.SIGILL: 'Illegal instruction (SIGILL) — corrupted function pointer or miscompiled binary.',
}


# ---------------------------------------------------------------------------
# Pragmas
# ---------------------------------------------------------------------------

_PRAGMA_KEYS = ('flags', 'args', 'stdin', 'timeout')


def _parse_pragmas(source: str, prefix: str):
    """Strip ``<prefix>%key: value`` lines off the top of the cell.

    Returns (clean_source, opts). Pragma lines are replaced with empty lines
    so user line numbers are preserved for diagnostics.
    """
    opts: dict = {}
    out_lines = []
    in_header = True
    pragma_re = re.compile(re.escape(prefix) + r'%([a-z]+)\s*:\s*(.*)$')
    for line in source.splitlines():
        stripped = line.strip()
        m = pragma_re.match(stripped) if in_header else None
        if m and m.group(1) in _PRAGMA_KEYS:
            key, value = m.group(1), m.group(2).strip()
            if key == 'timeout':
                try:
                    opts['timeout'] = max(1, min(int(float(value)), 86400))
                except ValueError:
                    pass
            elif key == 'flags':
                opts['flags'] = value.split()
            elif key == 'args':
                opts['args'] = value.split()
            elif key == 'stdin':
                # \n in the pragma value = multiple input lines
                opts['stdin'] = value.replace('\\n', '\n')
            out_lines.append('')
            continue
        if stripped and not stripped.startswith(prefix + '%'):
            in_header = False
        out_lines.append(line)
    return '\n'.join(out_lines), opts


# ---------------------------------------------------------------------------
# Auto-main wrapping
# ---------------------------------------------------------------------------

_C_DEFAULT_HEADERS = ('stdio.h', 'stdlib.h', 'string.h', 'math.h')
_CPP_DEFAULT_HEADERS = ('cstdio', 'cstdlib', 'cstring', 'cmath',
                        'iostream', 'string', 'vector')

_C_MAIN_RE = re.compile(r'\bmain\s*\(')
_RUST_MAIN_RE = re.compile(r'\bfn\s+main\b')
_C_HOIST_RE = re.compile(r'^\s*#\s*(include|define|pragma|if|ifdef|ifndef|else|elif|endif|undef)\b')
_CPP_USING_RE = re.compile(r'^\s*using\s+')
_C_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def _wrap_c_family(source: str, cpp: bool) -> str:
    """Wrap a main-less C/C++ snippet in ``int main`` with default headers.

    Preprocessor lines and (C++) ``using`` lines are hoisted above main with
    ``#line`` markers, and the body keeps its original line numbering via a
    single ``#line 1 "cell"`` so compiler diagnostics point at the user's
    cell lines.
    """
    if _C_MAIN_RE.search(source):
        return source

    lines = source.splitlines()
    included = {m.group(1) for line in lines
                if (m := _C_INCLUDE_RE.match(line))}
    prelude = []
    for hdr in (_CPP_DEFAULT_HEADERS if cpp else _C_DEFAULT_HEADERS):
        if hdr not in included:
            prelude.append(f'#include <{hdr}>')

    body = []
    for idx, line in enumerate(lines, start=1):
        if _C_HOIST_RE.match(line) or (cpp and _CPP_USING_RE.match(line)):
            prelude.append(f'#line {idx} "cell"')
            prelude.append(line)
            body.append('')            # keep body line numbering intact
        else:
            body.append(line)

    main_sig = 'int main()' if cpp else 'int main(void)'
    return '\n'.join(
        prelude
        + [main_sig + ' {', '#line 1 "cell"']
        + body
        + ['    ;', '    return 0;', '}', '']
    )


_RUST_ITEM_RE = re.compile(
    r'^\s*(?:pub\s+)?(?:unsafe\s+)?'
    r'(?:fn|struct|enum|impl|trait|type|macro_rules!|use|mod|extern|const|static)\b'
)
_RUST_ATTR_RE = re.compile(r'^\s*#!?\[')


def _wrap_rust(source: str):
    """Wrap a main-less Rust snippet in ``fn main``.

    Top-level items (use/fn/struct/... with brace tracking) are hoisted above
    main; the remaining statements form the main body. Returns
    (wrapped_source, line_map) where line_map[wrapped_line_no] = original
    cell line number (1-based) for diagnostic rewriting — rustc has no
    ``#line`` directive.
    """
    if _RUST_MAIN_RE.search(source):
        return source, {}

    lines = source.splitlines()
    hoisted, body = [], []            # lists of (text, orig_line_no)
    i = 0
    while i < len(lines):
        line = lines[i]
        if _RUST_ITEM_RE.match(line) or _RUST_ATTR_RE.match(line):
            depth = 0
            start = i
            while i < len(lines):
                depth += lines[i].count('{') - lines[i].count('}')
                ended = (depth <= 0
                         and (lines[i].rstrip().endswith(('}', ';'))
                              or _RUST_ATTR_RE.match(lines[i])))
                i += 1
                if ended:
                    break
            for j in range(start, i):
                hoisted.append((lines[j], j + 1))
        else:
            body.append((line, i + 1))
            i += 1

    wrapped, line_map = [], {}
    for text, orig in hoisted:
        wrapped.append(text)
        line_map[len(wrapped)] = orig
    wrapped.append('fn main() {')
    for text, orig in body:
        wrapped.append(text)
        line_map[len(wrapped)] = orig
    wrapped.append('}')
    return '\n'.join(wrapped) + '\n', line_map


_RUST_LOC_RE = re.compile(r'\bmain\.rs:(\d+)(:\d+)?')


def _shift_rust_diagnostics(text: str, line_map: dict) -> str:
    """Rewrite ``main.rs:N`` locations in rustc output to ``cell:M`` using
    the wrap line map (identity for cells that had their own ``fn main``)."""
    if not line_map:
        return text.replace('main.rs', 'cell')

    def _sub(m):
        n = int(m.group(1))
        col = m.group(2) or ''
        return f'cell:{line_map.get(n, n)}{col}'

    return _RUST_LOC_RE.sub(_sub, text)


# ---------------------------------------------------------------------------
# Compile + cache
# ---------------------------------------------------------------------------

def _tool_for(lang: str) -> str:
    tool = {'c': 'gcc', 'cpp': 'g++', 'rust': 'rustc'}[lang]
    if shutil.which(tool) is None:
        raise RuntimeNotAvailableError(
            f'{tool} is not available in this kernel container', [
                f'{tool} is not available in this kernel container.',
                'Stop this notebook session and start a new one to pick up '
                'the current kernel image.',
                '(An in-place kernel restart is NOT enough — the container '
                'keeps its old image.)',
            ])
    return tool


def _compile_cmd(lang: str, src: str, out: str, flags: list) -> list:
    tool = _tool_for(lang)
    if lang == 'c':
        return [tool, '-std=c17', '-O2', '-Wall', '-pthread',
                '-fdiagnostics-color=never', '-o', out, src] + flags + ['-lm']
    if lang == 'cpp':
        return [tool, '-std=c++20', '-O2', '-Wall', '-pthread',
                '-fdiagnostics-color=never', '-o', out, src] + flags
    return [tool, '--edition', '2021', '-O', '--color', 'never',
            '-o', out, src] + flags


def _prune_cache():
    try:
        entries = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)]
        entries.sort(key=lambda p: os.path.getmtime(p))
        for stale in entries[:max(0, len(entries) - CACHE_MAX_ENTRIES)]:
            os.unlink(stale)
    except OSError:
        pass


def _compile(lang: str, wrapped: str, flags: list, line_map: dict) -> str:
    """Compile and return the path of a cached executable."""
    key = hashlib.sha256(
        f'{lang}\x00{" ".join(flags)}\x00{wrapped}'.encode()).hexdigest()
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached = os.path.join(CACHE_DIR, key)
    if os.path.isfile(cached):
        os.utime(cached)               # LRU touch
        return cached

    workdir = tempfile.mkdtemp(dir=BUILD_ROOT)
    try:
        src = os.path.join(workdir,
                           {'c': 'cell.c', 'cpp': 'cell.cpp',
                            'rust': 'main.rs'}[lang])
        out = os.path.join(workdir, 'prog')
        with open(src, 'w') as f:
            f.write(wrapped)
        try:
            proc = subprocess.run(
                _compile_cmd(lang, src, out, flags),
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT,
                cwd=workdir)
        except subprocess.TimeoutExpired:
            raise CompileError(
                f'{lang} compilation timed out after {COMPILE_TIMEOUT}s')
        diagnostics = (proc.stderr or '') + (proc.stdout or '')
        diagnostics = diagnostics.replace(src, 'cell')
        if lang == 'rust':
            diagnostics = _shift_rust_diagnostics(diagnostics, line_map)
        if proc.returncode != 0:
            raise CompileError(f'{lang} compilation failed',
                               diagnostics.splitlines() or
                               [f'{lang} compilation failed'])
        if diagnostics.strip():        # warnings from a successful build
            sys.stderr.write(diagnostics if diagnostics.endswith('\n')
                             else diagnostics + '\n')
        shutil.move(out, cached)
        os.chmod(cached, 0o755)
        _prune_cache()
        return cached
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Run on a PTY with stdin bridging
# ---------------------------------------------------------------------------

def _child_wants_stdin(pid: int) -> Optional[bool]:
    """True if the child is blocked reading fd 0.

    Primary: /proc/<pid>/syscall (definitive). Returns None when that file
    is unreadable (ptrace_scope >= 2) so the caller can use the quiet-period
    fallback instead.
    """
    try:
        with open(f'/proc/{pid}/syscall') as f:
            parts = f.read().split()
        if not parts or parts[0] == 'running':
            return False
        # x86_64: read=0, readv=19, pread64=17; arg0 is the fd
        return parts[0] in ('0', '19', '17') and int(parts[1], 16) == 0
    except (OSError, ValueError, IndexError):
        return None


def _child_sleeping_in_tty_read(pid: int) -> bool:
    """Fallback heuristic: process is sleeping inside a tty read."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            state = f.read().rsplit(')', 1)[1].split()[0]
        with open(f'/proc/{pid}/wchan') as f:
            wchan = f.read().strip()
        return state == 'S' and wchan in ('n_tty_read', 'wait_woken')
    except (OSError, IndexError):
        return False


def _request_input() -> str:
    """Route a blocked read to the notebook's input dialog."""
    try:
        return input()
    except (EOFError, OSError):
        return ''
    except Exception as exc:           # ipykernel StdinNotImplementedError
        if type(exc).__name__ == 'StdinNotImplementedError':
            raise RunFailedError(
                'This kernel has no interactive stdin channel', [
                    'The program asked for input but this kernel has no '
                    'interactive stdin channel.',
                    "Pre-feed input with a pragma on the cell's first line, "
                    'e.g.  //%stdin: 3 4',
                ])
        raise


def _run_binary(binary: str, opts: dict):
    import pty
    import termios

    args = [binary] + list(opts.get('args') or [])
    stdin_text = opts.get('stdin')
    timeout = opts.get('timeout')

    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO          # we echo replies ourselves
    termios.tcsetattr(slave, termios.TCSANOW, attrs)

    cwd = RUN_CWD if os.path.isdir(RUN_CWD) else None
    proc = subprocess.Popen(
        args, stdin=slave, stdout=slave, stderr=subprocess.PIPE,
        start_new_session=True, cwd=cwd, bufsize=0)
    os.close(slave)

    if stdin_text is not None:
        data = stdin_text if stdin_text.endswith('\n') else stdin_text + '\n'
        os.write(master, data.encode() + b'\x04')   # \x04 = EOF for canonical reads

    import threading

    def _pump_stderr():
        fd = proc.stderr.fileno()
        dec = codecs.getincrementaldecoder('utf-8')('replace')
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            sys.stderr.write(dec.decode(chunk))
            sys.stderr.flush()

    err_thread = threading.Thread(target=_pump_stderr, daemon=True)
    err_thread.start()

    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    started = time.monotonic()
    quiet_ticks = 0
    eof = False
    try:
        while True:
            try:
                ready, _, _ = select.select([master], [], [], _POLL_SECS)
            except InterruptedError:
                continue
            if ready:
                quiet_ticks = 0
                try:
                    chunk = os.read(master, 4096)
                except OSError:        # EIO: child closed its end
                    eof = True
                    chunk = b''
                if not chunk:
                    eof = True
                if chunk:
                    text = decoder.decode(chunk).replace('\r\n', '\n')
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    continue

            if eof or proc.poll() is not None:
                if proc.poll() is None:
                    proc.wait()
                break

            if timeout and time.monotonic() - started > timeout:
                raise RunFailedError(
                    f'Timed out after {timeout}s', [
                        f'Process timed out after {timeout}s and was killed.',
                        'Raise the limit with a  //%timeout: <seconds>  '
                        'pragma if this is expected.',
                    ])

            # stdin bridging (skipped when //%stdin pre-fed the input)
            if stdin_text is None:
                wants = _child_wants_stdin(proc.pid)
                if wants is None:
                    quiet_ticks += 1
                    wants = (quiet_ticks >= _QUIET_TICKS
                             and _child_sleeping_in_tty_read(proc.pid))
                if wants:
                    reply = _request_input()
                    os.write(master, reply.encode() + b'\n')
                    sys.stdout.write(reply + '\n')   # transcript echo
                    sys.stdout.flush()
                    quiet_ticks = 0

        # drain whatever arrived between the last read and process exit
        while not eof:
            try:
                ready, _, _ = select.select([master], [], [], 0.05)
                if not ready:
                    break
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                sys.stdout.write(decoder.decode(chunk).replace('\r\n', '\n'))
                sys.stdout.flush()
            except OSError:
                break
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.close(master)
        except OSError:
            pass
        try:
            proc.stderr.close()
        except OSError:
            pass
        err_thread.join(timeout=1)

    rc = proc.returncode
    if rc == 0:
        return
    if rc < 0:
        signum = -rc
        try:
            signame = signal.Signals(signum).name
        except ValueError:
            signame = f'signal {signum}'
        hint = _SIGNAL_HINTS.get(signum,
                                 f'Process was terminated by {signame}.')
        raise RunFailedError(f'Process died with {signame}', [hint])
    raise RunFailedError(
        f'Process exited with code {rc}',
        [f'Process exited with non-zero code {rc}.'])


def _run_compiled(lang: str, source: str):
    source, opts = _parse_pragmas(source, '//')
    line_map: dict = {}
    if lang in ('c', 'cpp'):
        wrapped = _wrap_c_family(source, cpp=(lang == 'cpp'))
    else:
        wrapped, line_map = _wrap_rust(source)
    binary = _compile(lang, wrapped, opts.get('flags') or [], line_map)
    _run_binary(binary, opts)


# ---------------------------------------------------------------------------
# R via embedded rpy2 (state persists across cells for the session)
# ---------------------------------------------------------------------------

_R_RUN_CELL = '''
.phonos_run_cell <- function(src) {
    exprs <- parse(text = src)
    for (e in exprs) {
        res <- withVisible(eval(e, globalenv()))
        if (res$visible) print(res$value)
    }
    invisible(NULL)
}
'''


class _REnv:
    """Lazy singleton around the embedded R runtime.

    All R calls happen on the kernel main thread (embedded R is not
    thread-safe); the per-session execution lock already serializes cells.
    """
    _instance = None

    @classmethod
    def get(cls) -> '_REnv':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        try:
            import rpy2.robjects as ro
            from rpy2.rinterface_lib import callbacks
            from rpy2.rinterface_lib.embedded import RRuntimeError
            from rpy2.robjects import conversion, default_converter
            from rpy2.robjects import numpy2ri, pandas2ri
        except Exception as exc:
            raise RuntimeNotAvailableError(
                'R is not available in this kernel container', [
                    f'R support could not be loaded here ({exc}).',
                    'This kernel container was started from an image '
                    'without R/rpy2.',
                    'Stop this notebook session and start a new one to pick '
                    'up the current kernel image.',
                    '(An in-place kernel restart is NOT enough — the '
                    'container keeps its old image.)',
                ])

        self.ro = ro
        self.conversion = conversion
        self.RRuntimeError = RRuntimeError
        self.converter = (default_converter
                          + numpy2ri.converter + pandas2ri.converter)

        callbacks.consolewrite_print = self._write_out
        callbacks.consolewrite_warnerror = self._write_err
        callbacks.consoleread = self._read_line

        ro.r(_R_RUN_CELL)
        self._run_cell_fn = ro.r['.phonos_run_cell']

    @staticmethod
    def _write_out(text: str):
        sys.stdout.write(text)
        sys.stdout.flush()

    @staticmethod
    def _write_err(text: str):
        sys.stderr.write(text)
        sys.stderr.flush()

    @staticmethod
    def _read_line(prompt: str) -> str:
        try:
            return input(prompt) + '\n'
        except EOFError:
            return '\n'

    def _clean_r_error(self, exc) -> list:
        msg = str(exc).strip() or 'R evaluation failed'
        return [line for line in msg.splitlines() if line.strip()] or [msg]

    def run_cell(self, source: str, opts: Optional[dict] = None):
        ro = self.ro
        plot_dir = tempfile.mkdtemp(prefix='phonos_r_')
        pattern = os.path.join(plot_dir, 'plot%03d.png').replace('\\', '/')
        ro.r(f'grDevices::png("{pattern}", width = 896, height = 640, res = 120)')
        try:
            self._run_cell_fn(source)
        except self.RRuntimeError as exc:
            raise RError('R evaluation failed', self._clean_r_error(exc))
        except KeyboardInterrupt:
            raise
        finally:
            try:
                ro.r('if (grDevices::dev.cur() != 1) grDevices::dev.off()')
            except self.RRuntimeError:
                pass
            self._display_plots(plot_dir)

    @staticmethod
    def _display_plots(plot_dir: str):
        try:
            plots = sorted(f for f in os.listdir(plot_dir)
                           if f.endswith('.png'))
            if plots:
                from IPython.display import Image, display
                for name in plots:
                    path = os.path.join(plot_dir, name)
                    if os.path.getsize(path) > 0:
                        display(Image(filename=path))
        except Exception:
            pass
        finally:
            shutil.rmtree(plot_dir, ignore_errors=True)


def _run_r(source: str):
    source, opts = _parse_pragmas(source, '#')
    env = _REnv.get()
    if opts.get('timeout'):
        # Best effort: R has no safe preemption from Python; rely on the
        # kernel watchdog / interrupt for hard stops.
        env.ro.r(f'setTimeLimit(elapsed = {int(opts["timeout"])}, transient = TRUE)')
    env.run_cell(source, opts)


# ---------------------------------------------------------------------------
# Python <-> R exchange
# ---------------------------------------------------------------------------

_R_NAME_RE = re.compile(r'^[A-Za-z.][A-Za-z0-9._]*$')


def r_eval(code: str) -> Any:
    """Evaluate R code and return the result converted to Python
    (numpy/pandas aware)."""
    env = _REnv.get()
    from rpy2.robjects.conversion import localconverter
    try:
        with localconverter(env.converter):
            return env.ro.r(code)
    except env.RRuntimeError as exc:
        raise RError('R evaluation failed', env._clean_r_error(exc))


def r_get(name: str) -> Any:
    """Fetch an R global as a Python object (DataFrames -> pandas)."""
    if not _R_NAME_RE.match(name):
        raise ValueError(f'Invalid R variable name {name!r}')
    return r_eval(name)


def r_set(name: str, value: Any) -> None:
    """Assign a Python object (numpy/pandas aware) to an R global."""
    if not _R_NAME_RE.match(name):
        raise ValueError(f'Invalid R variable name {name!r}')
    env = _REnv.get()
    from rpy2.robjects.conversion import localconverter
    with localconverter(env.converter):
        env.ro.globalenv[name] = env.conversion.get_conversion().py2rpy(value)


class RProxy:
    """``phonos.r`` sugar: ``r.x`` / ``r.x = value`` map to R globals,
    ``r('summary(x)')`` evaluates."""

    def __call__(self, code: str) -> Any:
        return r_eval(code)

    def __getattr__(self, name: str) -> Any:
        try:
            return r_get(name)
        except RError:
            raise AttributeError(f'R has no object {name!r}')

    def __setattr__(self, name: str, value: Any) -> None:
        r_set(name, value)

    def __repr__(self) -> str:
        return '<phonos.r: attribute access maps to R globals>'


r = RProxy()


# ---------------------------------------------------------------------------
# Entry point (called by the backend shim)
# ---------------------------------------------------------------------------

def run(lang: str, source_b64: str, opts: Optional[dict] = None):
    """Execute a notebook lang cell. ``source_b64`` is the base64-encoded
    cell source (the backend encodes it to avoid any quoting issues)."""
    if lang not in LANGS:
        raise ValueError(f'Unsupported language {lang!r} (one of {LANGS})')
    source = base64.b64decode(source_b64).decode('utf-8', 'replace')
    if not source.strip():
        return
    os.makedirs(BUILD_ROOT, exist_ok=True)
    if lang == 'r':
        _run_r(source)
    else:
        _run_compiled(lang, source)


def help() -> str:  # noqa: A001 - deliberate SDK surface name
    return __doc__ or ''
