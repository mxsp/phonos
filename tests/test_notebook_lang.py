"""Unit tests for the chimera-kernel lang cell machinery.

Covers the pure functions of the kernel-side phonos.lang module (pragma
parsing, auto-main wrapping, rustc diagnostic line-shifting) and the backend
shim generator. lang.py is loaded standalone from its file path so these
tests never import the full phonos SDK package (or rpy2).
"""
import base64
import importlib.util
import os
import sys

import pytest

_LANG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'production', 'docker', 'notebook-kernel', 'phonos', 'lang.py')

_spec = importlib.util.spec_from_file_location('phonos_lang_standalone', _LANG_PATH)
lang = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lang)


# ---------------------------------------------------------------------------
# Pragmas
# ---------------------------------------------------------------------------

def test_pragmas_parsed_and_blanked():
    src = '//%flags: -O3 -lpthread\n//%args: a b\n//%timeout: 12\nint x = 1;'
    clean, opts = lang._parse_pragmas(src, '//')
    assert opts == {'flags': ['-O3', '-lpthread'], 'args': ['a', 'b'], 'timeout': 12}
    lines = clean.splitlines()
    # pragma lines become empty lines so user line numbers survive
    assert lines[:3] == ['', '', '']
    assert lines[3] == 'int x = 1;'


def test_pragma_stdin_multiline_and_r_prefix():
    src = '#%stdin: 3\\n4\n#%timeout: 5\nx <- 1'
    clean, opts = lang._parse_pragmas(src, '#')
    assert opts['stdin'] == '3\n4'
    assert opts['timeout'] == 5
    assert clean.splitlines()[2] == 'x <- 1'


def test_pragmas_only_at_top():
    src = 'int x = 1;\n//%flags: -O3\n'
    clean, opts = lang._parse_pragmas(src, '//')
    assert opts == {}
    assert '//%flags: -O3' in clean


def test_pragma_timeout_clamped_and_invalid_ignored():
    _, opts = lang._parse_pragmas('//%timeout: 999999999\nint main(){}', '//')
    assert opts['timeout'] == 86400
    _, opts = lang._parse_pragmas('//%timeout: soon\nint main(){}', '//')
    assert 'timeout' not in opts


# ---------------------------------------------------------------------------
# C / C++ auto-main wrap
# ---------------------------------------------------------------------------

def test_c_with_main_untouched():
    src = '#include <stdio.h>\nint main(void){return 0;}'
    assert lang._wrap_c_family(src, cpp=False) == src


def test_c_snippet_wrapped_with_line_marker():
    src = 'printf("hi\\n");'
    out = lang._wrap_c_family(src, cpp=False)
    assert '#include <stdio.h>' in out
    assert 'int main(void) {' in out
    assert '#line 1 "cell"' in out
    assert out.rstrip().endswith('}')
    assert 'return 0;' in out


def test_c_snippet_hoists_includes_without_duplicating():
    src = '#include <math.h>\n#include <fcntl.h>\nprintf("%f", sqrt(2.0));'
    out = lang._wrap_c_family(src, cpp=False)
    # user include hoisted above main, not duplicated by the default set
    assert out.count('#include <math.h>') == 1
    assert out.count('#include <fcntl.h>') == 1
    body_start = out.index('int main(void) {')
    assert '#include' not in out[body_start:]
    # hoisted lines keep their original position via #line markers
    assert '#line 2 "cell"' in out[:body_start]


def test_cpp_snippet_gets_cpp_headers_and_using_hoist():
    src = 'using namespace std;\ncout << 1;'
    out = lang._wrap_c_family(src, cpp=True)
    assert '#include <iostream>' in out
    assert 'int main() {' in out
    assert out.index('using namespace std;') < out.index('int main() {')


# ---------------------------------------------------------------------------
# Rust auto-main wrap + diagnostic shifting
# ---------------------------------------------------------------------------

def test_rust_with_main_untouched():
    src = 'fn main() { println!("hi"); }'
    wrapped, line_map = lang._wrap_rust(src)
    assert wrapped == src
    assert line_map == {}


def test_rust_snippet_hoists_items_and_maps_lines():
    src = ('use std::fs;\n'
           'fn sq(x: i64) -> i64 {\n'
           '    x * x\n'
           '}\n'
           'let y = sq(3);\n'
           'println!("{}", y);')
    wrapped, line_map = lang._wrap_rust(src)
    lines = wrapped.splitlines()
    assert lines[0] == 'use std::fs;'
    assert lines[1].startswith('fn sq')
    main_idx = lines.index('fn main() {')
    assert lines[main_idx + 1] == 'let y = sq(3);'
    # wrapped line numbers map back to original cell lines (1-based)
    assert line_map[1] == 1               # use std::fs;
    assert line_map[main_idx + 2] == 5    # let y = sq(3);


def test_rust_diagnostics_shifted_via_line_map():
    src = 'let x: i32 = "nope";'
    wrapped, line_map = lang._wrap_rust(src)
    n = wrapped.splitlines().index('let x: i32 = "nope";') + 1
    diag = f'error[E0308]: mismatched types\n --> main.rs:{n}:14\n'
    shifted = lang._shift_rust_diagnostics(diag, line_map)
    assert '--> cell:1:14' in shifted
    assert 'main.rs' not in shifted


def test_rust_diagnostics_identity_when_user_had_main():
    diag = ' --> main.rs:3:5\n'
    assert 'cell:3:5' in lang._shift_rust_diagnostics(diag, {})


# ---------------------------------------------------------------------------
# Compile command shape (no compiler invocation)
# ---------------------------------------------------------------------------

def test_compile_cmd_flags_and_lm_ordering(monkeypatch):
    monkeypatch.setattr(lang.shutil, 'which', lambda tool: '/usr/bin/' + tool)
    cmd = lang._compile_cmd('c', 'cell.c', 'prog', ['-DFOO'])
    assert cmd[0] == 'gcc'
    assert '-fdiagnostics-color=never' in cmd
    assert cmd[-1] == '-lm' and '-DFOO' in cmd
    assert cmd.index('-DFOO') > cmd.index('cell.c')
    cmd = lang._compile_cmd('rust', 'main.rs', 'prog', [])
    assert cmd[:3] == ['rustc', '--edition', '2021']


def test_missing_toolchain_raises_runtime_not_available(monkeypatch):
    monkeypatch.setattr(lang.shutil, 'which', lambda tool: None)
    with pytest.raises(lang.RuntimeNotAvailableError) as exc:
        lang._tool_for('rust')
    rendered = '\n'.join(exc.value._render_traceback_())
    assert 'new one' in rendered            # advises a NEW session, not restart


# ---------------------------------------------------------------------------
# Error rendering protocol
# ---------------------------------------------------------------------------

def test_lang_error_renders_custom_traceback():
    err = lang.CompileError('c compilation failed', ['cell:1:1: error: boom'])
    assert err._render_traceback_() == ['cell:1:1: error: boom']
    assert str(err) == 'c compilation failed'


def test_run_rejects_unknown_language():
    with pytest.raises(ValueError):
        lang.run('fortran', base64.b64encode(b'x').decode())


# ---------------------------------------------------------------------------
# Backend shim (socket_handlers_notebook)
# ---------------------------------------------------------------------------

def _backend_module():
    return pytest.importorskip(
        'phonos_app.socket_handlers_notebook',
        reason='backend deps (flask/zmq/eventlet) not importable here')


def test_make_lang_shim_round_trip():
    backend = _backend_module()
    source = 'printf("weird \\" quotes\' and\nnewlines");'
    shim = backend._make_lang_shim('c', source)
    assert '\n' not in shim                 # single line, safe for exec path
    b64 = shim.split("'")[3]
    assert base64.b64decode(b64).decode('utf-8') == source
    assert shim.startswith('import phonos.lang as __phonos_lang;')


def test_lang_cell_types_constant():
    assert set(_backend_module().LANG_CELL_TYPES) == {'c', 'cpp', 'rust', 'r'}
