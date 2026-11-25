#!/usr/bin/env python3
"""Minimal Bring-Your-Own-Model (BYOM) HTTP server example for Phonos.

Run locally with zero external dependencies (Python 3.11+ stdlib only):
    python3 examples/custom_model_server.py --port 8090

Then in Phonos → Models & Endpoints → Register Custom Endpoint:
  - Endpoint Mode: Direct HTTP REST (`direct_http`)
  - Endpoint URL:  `http://127.0.0.1:8090/predict`
  - Output Format: `pdb` (auto-detected by Phonos and rendered in the 3D Mol* Viewer!)

Replace `run_inference(input_data)` below with your own PyTorch, JAX, OpenMM,
RDKit, Vina, or ColabFold inference code.
"""
from __future__ import annotations

import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict


def generate_alpha_helix_pdb(sequence: str) -> str:
    """Synthesize a clean ideal alpha-helix C-alpha/backbone PDB for demonstration."""
    seq = "".join(c for c in (sequence or "ACDEFGHIKLMNPQRSTVWY").upper() if c.isalpha())[:250] or "GGGGGGGG"
    aa3_map = {
        "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE",
        "G": "GLY", "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU",
        "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN", "R": "ARG",
        "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR",
    }
    lines = ["HEADER    PHONOS CUSTOM BYOM DEMO STRUCTURE"]
    atom_idx = 1
    for i, aa in enumerate(seq, start=1):
        resname = aa3_map.get(aa, "ALA")
        angle = math.radians(i * 100.0)
        z_base = i * 1.5
        coords = (
            ("N", 2.1 * math.cos(angle - 0.3), 2.1 * math.sin(angle - 0.3), z_base - 0.5, "N"),
            ("CA", 2.3 * math.cos(angle), 2.3 * math.sin(angle), z_base, "C"),
            ("C", 2.1 * math.cos(angle + 0.35), 2.1 * math.sin(angle + 0.35), z_base + 0.55, "C"),
            ("O", 2.9 * math.cos(angle + 0.45), 2.9 * math.sin(angle + 0.45), z_base + 0.85, "O"),
        )
        plddt = round(92.5 - (i % 7) * 1.8, 2)
        for aname, x, y, z, elem in coords:
            lines.append(
                f"ATOM  {atom_idx:5d}  {aname:<3s} {resname:3s} A{i:4d}    "
                f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00{plddt:6.2f}           {elem:>2s}"
            )
            atom_idx += 1
    lines.append("END")
    return "\n".join(lines) + "\n"


def run_inference(input_data: Dict[str, Any]) -> Dict[str, Any]:
    sequence = str(input_data.get("sequence") or input_data.get("prompt") or "MKTAYIAKQRQISFVKSHFSRQLE")
    pdb_str = generate_alpha_helix_pdb(sequence)
    return {
        "status": "completed",
        "sequence_length": len(sequence),
        "pdb": pdb_str,
        "metrics": {"mean_plddt": 87.4, "engine": "phonos-custom-demo"},
    }


class CustomModelHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200, {"ok": True, "service": "phonos-custom-model-example", "endpoints": ["/health", "/predict"]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send(400, {"error": f"Invalid JSON: {exc}"})
            return
        input_data = body.get("input_data") if isinstance(body.get("input_data"), dict) else body
        result = run_inference(input_data)
        self._send(200, result)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[custom-model] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phonos BYOM Starter Model Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), CustomModelHandler)
    print(f"Phonos Custom Model Server listening on http://{args.host}:{args.port}/predict")
    srv.serve_forever()


if __name__ == "__main__":
    main()
