"""Zero-Docker Stateful Local Python Kernel Worker for Phonos Notebook IDE.

Launched as a per-session subprocess bound strictly to ``127.0.0.1:<port>``
authenticated with a random per-session token. Provides full stateful cell
execution across Linux, macOS, Windows, and single-container Docker installs
without requiring a Docker daemon or external Jupyter ZMQ image.

Includes built-in bioinformatics & cheminformatics display helpers:
  - ``show_smiles(smiles)``    -> RDKit 2D SVG + Lipinski/QED card
  - ``show_structure(pdb_str)``-> Interactive 3D viewer trigger + chain/pLDDT card
"""
from __future__ import annotations

import ast
import base64
import ctypes
import html
import io
import json
import os
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List


class _DisplayCollector:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def push_html(self, raw_html: str) -> None:
        self.items.append({"output_type": "display_data", "data": {"text/html": raw_html}})

    def push_png_b64(self, b64_str: str) -> None:
        self.items.append({"output_type": "display_data", "data": {"image/png": b64_str}})


_ACTIVE_COLLECTOR = _DisplayCollector()


def _make_helpers(workspace: Path) -> Dict[str, Any]:
    def display(obj: Any) -> None:
        if hasattr(obj, "_repr_html_"):
            h = obj._repr_html_()
            if h:
                _ACTIVE_COLLECTOR.push_html(str(h))
                return
        if hasattr(obj, "_repr_svg_"):
            s = obj._repr_svg_()
            if s:
                _ACTIVE_COLLECTOR.push_html(str(s))
                return
        _ACTIVE_COLLECTOR.push_html(f"<pre>{html.escape(repr(obj))}</pre>")

    def show_smiles(smiles: str, title: str = "", width: int = 320, height: int = 200, **_kwargs: Any) -> None:
        """Render an RDKit 2D depiction + Lipinski/QED summary directly in the notebook."""
        try:
            from rdkit import Chem
            from rdkit.Chem import Crippen, Descriptors, Draw, QED, rdMolDescriptors

            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                _ACTIVE_COLLECTOR.push_html(f"<div style='color:#f87171;'>Invalid SMILES: {html.escape(smiles)}</div>")
                return
            drawer = Draw.MolDraw2DSVG(max(120, int(width or 320)), max(100, int(height or 200)))
            drawer.DrawMolecule(mol)
            drawer.FinishDrawing()
            svg = drawer.GetDrawingText()
            mw = round(Descriptors.MolWt(mol), 2)
            logp = round(Crippen.MolLogP(mol), 2)
            tpsa = round(rdMolDescriptors.CalcTPSA(mol), 2)
            qed_v = round(float(QED.qed(mol)), 3)
            hdr = f"<div style='font-weight:600;margin-bottom:6px;color:#34d399;'>{html.escape(title or smiles)}</div>"
            card = (
                "<div style='display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:12px;"
                "background:#121216;border:1px solid rgba(255,255,255,0.1);border-radius:10px;margin:6px 0;'>"
                f"<div style='background:#fff;border-radius:8px;padding:4px;'>{svg}</div>"
                "<div style='font-size:13px;color:#e4e4e7;line-height:1.7;'>"
                f"{hdr}"
                f"<div><b>Formula:</b> {rdMolDescriptors.CalcMolFormula(mol)}</div>"
                f"<div><b>MW:</b> {mw} Da &nbsp;·&nbsp; <b>cLogP:</b> {logp}</div>"
                f"<div><b>TPSA:</b> {tpsa} Å² &nbsp;·&nbsp; <b>QED:</b> {qed_v}</div>"
                "</div></div>"
            )
            _ACTIVE_COLLECTOR.push_html(card)
        except Exception as exc:
            _ACTIVE_COLLECTOR.push_html(f"<div style='color:#f87171;'>show_smiles error: {html.escape(str(exc))}</div>")

    def show_structure(pdb_or_path: str, title: str = "3D Molecular Structure", fmt: str = "pdb", **_kwargs: Any) -> None:
        """Render an interactive structure summary card in the notebook output."""
        content = str(pdb_or_path or "")
        if len(content) < 400 and "\n" not in content:
            candidate = Path(content) if os.path.isabs(content) else (workspace / content)
            if candidate.exists():
                content = candidate.read_text(encoding="utf-8", errors="replace")
        chains = set()
        ca_bfacs: list[float] = []
        atom_cnt = 0
        for line in content.splitlines():
            if line.startswith(("ATOM  ", "HETATM")):
                atom_cnt += 1
                if len(line) >= 22 and line[21].strip():
                    chains.add(line[21].strip())
                if line.startswith("ATOM  ") and len(line) >= 66 and line[12:16].strip() == "CA":
                    try:
                        v = float(line[60:66].strip())
                        ca_bfacs.append(v * 100.0 if 0 < v <= 1.0 else v)
                    except ValueError:
                        pass
        mean_plddt = f"{sum(ca_bfacs)/len(ca_bfacs):.1f}" if ca_bfacs else "n/a"
        b64_pdb = base64.b64encode(content.encode("utf-8")).decode("ascii")
        card = (
            "<div style='padding:12px 14px;background:#121216;border:1px solid rgba(16,185,129,0.32);"
            "border-radius:10px;margin:6px 0;color:#f4f4f5;font-size:13px;'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;gap:12px;'>"
            f"<div><strong style='color:#34d399;'>🧬 {html.escape(title)}</strong>"
            f"<div style='color:#a1a1aa;font-size:12px;margin-top:3px;'>Atoms: {atom_cnt} · Chains: {', '.join(sorted(chains)) or 'A'} · Residues (Cα): {len(ca_bfacs)} · Mean pLDDT/B: {mean_plddt}</div></div>"
            f"<a download='notebook_structure.pdb' href='data:chemical/x-pdb;base64,{b64_pdb}' "
            "style='padding:5px 10px;background:#10b981;color:#052e1c;font-weight:700;border-radius:6px;text-decoration:none;font-size:12px;'>Download .pdb</a>"
            "</div></div>"
        )
        _ACTIVE_COLLECTOR.push_html(card)

    return {
        "__name__": "__main__",
        "display": display,
        "show_smiles": show_smiles,
        "show_structure": show_structure,
    }


class _KernelState:
    def __init__(self, workspace: Path, token: str) -> None:
        self.workspace = workspace
        self.token = token
        self.lock = threading.Lock()
        self.exec_thread_id: int | None = None
        self.execution_count = 0
        self.ns: Dict[str, Any] = _make_helpers(workspace)

    def reset(self) -> None:
        with self.lock:
            self.execution_count = 0
            self.ns = _make_helpers(self.workspace)

    def interrupt(self) -> bool:
        tid = self.exec_thread_id
        if not tid:
            return False
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(tid),
            ctypes.py_object(KeyboardInterrupt),
        )
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), None)
            return False
        return res == 1

    def execute(self, code: str) -> Dict[str, Any]:
        with self.lock:
            self.execution_count += 1
            count = self.execution_count
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            _ACTIVE_COLLECTOR.items.clear()
            outputs: List[Dict[str, Any]] = []
            status = "ok"

            os.chdir(self.workspace)
            self.exec_thread_id = threading.get_ident()
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    tree = ast.parse(code, filename="<notebook-cell>", mode="exec")
                    last_expr = None
                    if tree.body and isinstance(tree.body[-1], ast.Expr):
                        last_expr = ast.Expression(tree.body.pop().value)
                        ast.fix_missing_locations(tree)
                        ast.fix_missing_locations(last_expr)
                    if tree.body:
                        exec(compile(tree, "<notebook-cell>", "exec"), self.ns)  # nosec B102
                    result_val = None
                    if last_expr is not None:
                        result_val = eval(compile(last_expr, "<notebook-cell>", "eval"), self.ns)  # nosec B307

                    # Capture open matplotlib figures if matplotlib was used
                    if "matplotlib.pyplot" in sys.modules:
                        try:
                            plt = sys.modules["matplotlib.pyplot"]
                            for fnum in plt.get_fignums():
                                fig = plt.figure(fnum)
                                img_buf = io.BytesIO()
                                fig.savefig(img_buf, format="png", bbox_inches="tight", dpi=130)
                                _ACTIVE_COLLECTOR.push_png_b64(base64.b64encode(img_buf.getvalue()).decode("ascii"))
                            plt.close("all")
                        except Exception:
                            pass
            except BaseException as exc:
                status = "error"
                tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
                outputs.append({
                    "output_type": "error",
                    "ename": type(exc).__name__,
                    "evalue": str(exc),
                    "traceback": [line.rstrip("\n") for line in tb_lines],
                })
                result_val = None
            finally:
                self.exec_thread_id = None

            out_txt = stdout_buf.getvalue()
            err_txt = stderr_buf.getvalue()
            if out_txt:
                outputs.insert(0, {"output_type": "stream", "name": "stdout", "text": out_txt})
            if err_txt:
                outputs.append({"output_type": "stream", "name": "stderr", "text": err_txt})

            outputs.extend(_ACTIVE_COLLECTOR.items)

            if status == "ok" and result_val is not None:
                data_bundle: Dict[str, Any] = {"text/plain": repr(result_val)}
                if hasattr(result_val, "_repr_html_"):
                    try:
                        h = result_val._repr_html_()
                        if h:
                            data_bundle["text/html"] = str(h)
                    except Exception:
                        pass
                elif hasattr(result_val, "_repr_svg_"):
                    try:
                        s = result_val._repr_svg_()
                        if s:
                            data_bundle["text/html"] = str(s)
                    except Exception:
                        pass
                outputs.append({
                    "output_type": "execute_result",
                    "execution_count": count,
                    "data": data_bundle,
                })

            return {
                "status": status,
                "execution_count": count,
                "outputs": outputs,
            }


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: local_kernel_worker.py <port> <token> <workspace_dir>", file=sys.stderr)
        sys.exit(2)
    port = int(sys.argv[1])
    token = sys.argv[2]
    workspace = Path(sys.argv[3]).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)

    # Set non-interactive matplotlib backend if installed
    os.environ.setdefault("MPLBACKEND", "Agg")
    state = _KernelState(workspace, token)

    class Handler(BaseHTTPRequestHandler):
        def _auth_ok(self) -> bool:
            got = self.headers.get("X-Phonos-Kernel-Token", "")
            return bool(got and got == state.token)

        def _json(self, code_num: int, payload: Dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code_num)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"ok": True, "execution_count": state.execution_count})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._json(403, {"error": "forbidden"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            if self.path == "/execute":
                res = state.execute(str(body.get("code") or ""))
                self._json(200, res)
            elif self.path == "/interrupt":
                ok = state.interrupt()
                self._json(200, {"ok": ok})
            elif self.path == "/reset":
                state.reset()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
