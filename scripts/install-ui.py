#!/usr/bin/env python3
"""Phonos Browser GUI Installer — zero-dependency one-time setup wizard.

Runs a local-only HTTP wizard on http://127.0.0.1:8098 using Python's standard
library only (works on Linux, macOS, and Windows before any packages are
installed):

  python3 scripts/install-ui.py
  python3 scripts/install-ui.py --port 8098 --no-browser
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = platform.system().lower() == "windows"


def _venv_dir() -> Path:
    if (ROOT / ".venv").is_dir():
        return ROOT / ".venv"
    if (ROOT / "venv").is_dir():
        return ROOT / "venv"
    return ROOT / ".venv"


def _venv_bin(name: str) -> Path:
    vdir = _venv_dir()
    if IS_WIN:
        return vdir / "Scripts" / f"{name}.exe"
    return vdir / "bin" / name


def _cmd_version(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return out[0].strip() if r.returncode == 0 and out else ""
    except Exception:
        return ""


def _lan_ips() -> list[str]:
    ips: list[str] = []
    for target in (("10.255.255.255", 1), ("192.0.2.1", 80)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect(target)
                ip = s.getsockname()[0]
                addr = ipaddress.ip_address(ip)
                if addr.is_private and not addr.is_loopback and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
    return ips


def system_check() -> dict:
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    git_ver = _cmd_version(["git", "--version"]) if shutil.which("git") else ""
    docker_ver = _cmd_version(["docker", "--version"]) if shutil.which("docker") else ""
    docker_running = False
    if docker_ver:
        try:
            chk_d = subprocess.run(["docker", "info"], capture_output=True, timeout=2, check=False)
            docker_running = chk_d.returncode == 0
        except Exception:
            docker_running = False

    venv_ready = _venv_bin("python").is_file()
    rdkit_ready = False
    if venv_ready:
        chk = subprocess.run(
            [str(_venv_bin("python")), "-c", "import rdkit, Bio"],
            capture_output=True,
            check=False,
        )
        rdkit_ready = chk.returncode == 0

    return {
        "root": str(ROOT),
        "platform": f"{platform.system()} {platform.machine()}",
        "python": {"ok": sys.version_info >= (3, 11), "version": py_ver},
        "git": {"ok": bool(git_ver), "version": git_ver or "not found"},
        "docker": {"ok": bool(docker_ver), "version": docker_ver or "optional (not installed)", "running": docker_running},
        "venv_ready": venv_ready,
        "rdkit_ready": rdkit_ready,
        "lan_ips": _lan_ips(),
    }


class InstallState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.done = False
        self.ok = False
        self.step = ""
        self.logs: list[str] = []
        self.mode = "both"
        self.host = "0.0.0.0"
        self.launched_url = ""
        self.lan_url = ""

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "done": self.done,
                "ok": self.ok,
                "step": self.step,
                "logs": self.logs[-400:],
                "mode": self.mode,
                "host": self.host,
                "launched_url": self.launched_url,
                "lan_url": self.lan_url,
            }

    def log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line.rstrip())

    def set_step(self, step: str) -> None:
        with self.lock:
            self.step = step
            self.logs.append(f"\n==> {step}")


STATE = InstallState()


def _run_stream(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
    STATE.log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        STATE.log(line)
    return proc.wait()


def _install_worker(opts: dict) -> None:
    mode = opts.get("mode", "both")
    if mode not in ("playground", "production", "both"):
        mode = "both"
    host = "0.0.0.0" if opts.get("lan_access", True) else "127.0.0.1"
    admin_user = str(opts.get("admin_user") or "").strip().lower()
    admin_password = str(opts.get("admin_password") or "")
    lab_name = str(opts.get("lab_name") or "Phonos Lab").strip() or "Phonos Lab"
    nvidia_key = str(opts.get("nvidia_api_key") or "").strip()
    reset_pg = bool(opts.get("reset_playground", False))
    auto_launch = bool(opts.get("auto_launch", True))

    with STATE.lock:
        STATE.running = True
        STATE.done = False
        STATE.ok = False
        STATE.logs = []
        STATE.mode = mode
        STATE.host = host
        STATE.launched_url = ""
        STATE.lan_url = ""

    try:
        STATE.set_step("1/3 Creating Python virtual environment & installing Phonos + RDKit + BioPython")
        vdir = _venv_dir()
        py_bin = _venv_bin("python")
        if not py_bin.is_file():
            rc = _run_stream([sys.executable, "-m", "venv", str(vdir)], ROOT)
            if rc != 0:
                raise RuntimeError("Failed to create Python virtual environment.")
        rc = _run_stream([str(py_bin), "-m", "pip", "install", "--upgrade", "pip", "--quiet"], ROOT)
        if rc != 0:
            raise RuntimeError("pip upgrade failed.")
        rc = _run_stream([str(py_bin), "-m", "pip", "install", "-e", str(ROOT)], ROOT)
        if rc != 0:
            raise RuntimeError("Phonos package installation failed.")

        if nvidia_key:
            env_file = ROOT / ".env"
            existing_env = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
            if "NVIDIA_API_KEY=" not in existing_env:
                with env_file.open("a", encoding="utf-8") as fh:
                    fh.write(f"\nNVIDIA_API_KEY={nvidia_key}\n")
                try:
                    os.chmod(env_file, 0o600)
                except OSError:
                    pass
                STATE.log("Saved NVIDIA_API_KEY to local .env (0600).")

        if mode in ("playground", "both"):
            STATE.set_step("2/3 Initializing isolated Playground sandbox (:5040)")
            pg_cmd = [str(py_bin), "-m", "phonos_app.cli", "setup", "--mode", "playground"]
            if reset_pg:
                pg_cmd.append("--reset")
            if _run_stream(pg_cmd, ROOT) != 0:
                raise RuntimeError("Playground initialization failed.")

        if mode in ("production", "both"):
            STATE.set_step("3/3 Initializing Production lab environment (:5041)")
            pr_cmd = [str(py_bin), "-m", "phonos_app.cli", "setup", "--mode", "production", "--lab-name", lab_name]
            if admin_user and len(admin_password) >= 8:
                pr_cmd += ["--admin-user", admin_user, "--admin-password", admin_password]
            if _run_stream(pr_cmd, ROOT) != 0:
                raise RuntimeError("Production initialization failed.")

        target_port = 5040 if mode == "playground" else 5041
        launched_url = f"http://127.0.0.1:{target_port}"
        lan_ips = _lan_ips()
        lan_url = f"http://{lan_ips[0]}:{target_port}" if (host == "0.0.0.0" and lan_ips) else ""

        if auto_launch:
            STATE.set_step(f"Launching Phonos ({mode}) in background…")
            launch_cmd = [str(py_bin), "-m", "phonos_app.launcher", "--mode", mode, "--host", host, "--no-browser"]
            subprocess.Popen(
                launch_cmd,
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(not IS_WIN),
            )
            STATE.log(f"Started Phonos ({mode}) -> {launched_url}")

        with STATE.lock:
            STATE.ok = True
            STATE.launched_url = launched_url
            STATE.lan_url = lan_url
            STATE.step = "Installation complete!"
    except Exception as exc:
        STATE.log(f"\nERROR: {exc}")
        with STATE.lock:
            STATE.ok = False
            STATE.step = f"Setup failed: {exc}"
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.done = True


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phonos - Guided Lab Installer</title>
<style>
  :root {
    --bg:#293742; --card:#30404D; --field:rgba(0,0,0,0.22);
    --border:rgba(255,255,255,0.24); --border-strong:rgba(245,248,250,0.72);
    --text:#F5F8FA; --muted:#A3B3C5; --accent:#0F9960; --accent-light:#34d399;
    --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    background-color:var(--bg);
    background-image:
      linear-gradient(rgba(245,248,250,0.09) 1px, transparent 1px),
      linear-gradient(90deg, rgba(245,248,250,0.09) 1px, transparent 1px),
      linear-gradient(rgba(245,248,250,0.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(245,248,250,0.045) 1px, transparent 1px);
    background-size:160px 160px, 160px 160px, 32px 32px, 32px 32px;
    color:var(--text);
    font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    padding:2rem 1rem 3rem;
    line-height:1.5;
  }
  .wrap { max-width:860px; margin:0 auto; }
  .hdr { display:flex; align-items:flex-end; justify-content:space-between; gap:1rem; flex-wrap:wrap; margin-bottom:1.5rem; padding-bottom:1rem; border-bottom:1px solid var(--border-strong); }
  h1 { font-size:1.35rem; font-weight:700; letter-spacing:-0.02em; }
  .badge { font-family:var(--mono); font-size:11px; letter-spacing:0.1em; text-transform:uppercase; padding:4px 9px; border-radius:0; background:var(--field); color:var(--accent-light); border:1px solid var(--accent-light); }
  .card { background:var(--card); border:1px solid var(--border); border-top:2px solid var(--border-strong); border-radius:0; margin-bottom:1.25rem; }
  .card h2 { font-family:var(--mono); font-size:12px; letter-spacing:0.12em; text-transform:uppercase; padding:10px 16px; background:var(--field); border-bottom:1px solid var(--border); color:var(--text); font-weight:500; }
  .card-body { padding:16px; }
  .checks { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:8px; }
  .chk { padding:10px 12px; border-radius:0; background:var(--field); border:1px solid var(--border); border-top:2px solid var(--border-strong); font-size:0.84rem; }
  .chk b { display:block; font-family:var(--mono); font-size:10px; letter-spacing:0.12em; text-transform:uppercase; color:var(--muted); margin-bottom:4px; }
  .ok { color:var(--accent-light); font-family:var(--mono); } .warn { color:#fbbf24; font-family:var(--mono); }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  @media(max-width:640px){ .grid { grid-template-columns:1fr; } }
  label { display:block; font-family:var(--mono); font-size:11px; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); margin-bottom:5px; }
  input, select { width:100%; padding:8px 10px; background:var(--field); border:1px solid var(--border); border-radius:0; color:var(--text); font-size:0.9rem; }
  input:focus, select:focus { outline:2px solid var(--accent-light); border-color:var(--accent-light); }
  .row-chk { display:flex; align-items:center; gap:8px; margin-top:10px; font-family:inherit; text-transform:none; letter-spacing:0; font-size:0.86rem; color:#C7D0DB; cursor:pointer; }
  .row-chk input { width:auto; accent-color:var(--accent); }
  .btn { background:var(--accent); color:#fff; font-family:var(--mono); font-size:12px; font-weight:500; letter-spacing:0.1em; text-transform:uppercase; border:1px solid var(--accent-light); border-radius:0; padding:11px 18px; cursor:pointer; width:100%; margin-top:12px; }
  .btn:hover:not(:disabled) { box-shadow:3px 3px 0 rgba(0,0,0,0.55); transform:translate(-2px,-2px); }
  .btn:disabled { opacity:0.5; cursor:not-allowed; }
  pre { background:rgba(0,0,0,0.34); border:1px solid var(--border); border-radius:0; padding:12px; font-family:var(--mono); font-size:12px; color:#C7D0DB; max-height:280px; overflow:auto; white-space:pre-wrap; margin-top:10px; }
  .links a { display:inline-block; margin-right:8px; margin-top:8px; padding:7px 14px; border-radius:0; background:var(--accent); color:#fff; border:1px solid var(--accent-light); font-family:var(--mono); font-size:11px; letter-spacing:0.08em; text-transform:uppercase; text-decoration:none; }
  .ruo { font-size:0.78rem; color:var(--muted); margin-top:10px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <div>
      <h1>PHONOS // GUIDED ONE-TIME SETUP</h1>
      <div style="font-size:0.85rem;color:var(--muted);">Self-Hosted Molecular Discovery, 3D Mol* Viewer, Python Notebook &amp; Copilot</div>
    </div>
    <span class="badge">ZERO-SMTP // LOCAL LAB</span>
  </div>

  <div class="card">
    <h2>01 // System Environment Check</h2>
    <div class="card-body">
      <div id="checks" class="checks">Checking host...</div>
    </div>
  </div>

  <div class="card">
    <h2>02 // Lab &amp; First Admin Configuration</h2>
    <div class="card-body">
      <div class="grid">
        <div>
          <label>Install &amp; Launch Mode</label>
          <select id="mode">
            <option value="both">Playground (:5040) + Production (:5041)</option>
            <option value="production">Production Lab Only (:5041)</option>
            <option value="playground">Playground Sandbox Only (:5040)</option>
          </select>
        </div>
        <div>
          <label>Lab / Organization Name</label>
          <input id="lab_name" value="Phonos Lab" placeholder="Structural Biology Lab">
        </div>
        <div>
          <label>Production Admin Username (Optional)</label>
          <input id="admin_user" placeholder="admin">
        </div>
        <div>
          <label>Production Admin Password (Min 8 Chars)</label>
          <input id="admin_password" type="password" placeholder="Leave blank to configure at /setup">
        </div>
        <div style="grid-column: 1 / -1;">
          <label>Optional NVIDIA NIM API Key (Can Be Added Encrypted Later)</label>
          <input id="nvidia_api_key" type="password" placeholder="nvapi-...">
        </div>
      </div>
      <label class="row-chk"><input type="checkbox" id="lan_access" checked> Allow access from devices on your local LAN</label>
      <label class="row-chk"><input type="checkbox" id="auto_launch" checked> Start Phonos immediately after setup completes</label>
      <p class="ruo">Research Use Only (RUO): Computational predictions require independent wet-lab validation and are not for clinical or diagnostic use.</p>
      <button class="btn" id="startBtn" onclick="startInstall()">Install &amp; Launch Phonos</button>
    </div>
  </div>

  <div class="card" id="statusCard" style="display:none;">
    <h2 id="stepTitle">Starting setup...</h2>
    <div class="card-body">
      <div id="openLinks" class="links"></div>
      <pre id="logBox"></pre>
    </div>
  </div>
</div>
<script>
async function loadCheck(){
  const r = await fetch('/api/check');
  const d = await r.json();
  const el = document.getElementById('checks');
  el.innerHTML = [
    `<div class="chk"><b>Python 3.11+</b><span class="${d.python.ok?'ok':'warn'}">${d.python.version}</span></div>`,
    `<div class="chk"><b>Platform</b><span>${d.platform}</span></div>`,
    `<div class="chk"><b>Virtualenv &amp; RDKit</b><span class="${d.rdkit_ready?'ok':'warn'}">${d.rdkit_ready?'Ready':'Will install'}</span></div>`,
    `<div class="chk"><b>Docker (Optional)</b><span class="${d.docker.ok?'ok':'warn'}">${d.docker.version}</span></div>`
  ].join('');
}
async function startInstall(){
  document.getElementById('startBtn').disabled = true;
  document.getElementById('statusCard').style.display = 'block';
  const payload = {
    mode: document.getElementById('mode').value,
    lab_name: document.getElementById('lab_name').value,
    admin_user: document.getElementById('admin_user').value,
    admin_password: document.getElementById('admin_password').value,
    nvidia_api_key: document.getElementById('nvidia_api_key').value,
    lan_access: document.getElementById('lan_access').checked,
    auto_launch: document.getElementById('auto_launch').checked
  };
  await fetch('/api/install', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  pollStatus();
}
async function pollStatus(){
  const r = await fetch('/api/status');
  const s = await r.json();
  document.getElementById('stepTitle').textContent = s.step || 'Working…';
  const lb = document.getElementById('logBox');
  lb.textContent = (s.logs || []).join('\\n');
  lb.scrollTop = lb.scrollHeight;
  if (s.done) {
    document.getElementById('startBtn').disabled = false;
    if (s.ok && s.launched_url) {
      let html = `<a href="${s.launched_url}" target="_blank">Open Phonos (${s.launched_url})</a>`;
      if (s.lan_url) html += `<a href="${s.lan_url}" target="_blank" style="background:#27272a;color:#fafafa;">LAN: ${s.lan_url}</a>`;
      document.getElementById('openLinks').innerHTML = html;
    }
    loadCheck();
  } else {
    setTimeout(pollStatus, 900);
  }
}
loadCheck();
</script>
</body>
</html>
"""


class InstallerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/check":
            self._send_json(system_check())
            return
        if self.path == "/api/status":
            self._send_json(STATE.snapshot())
            return
        raw = INDEX_HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/install":
            self._send_json({"error": "not found"}, 404)
            return
        with STATE.lock:
            if STATE.running:
                self._send_json({"error": "Installation already running"}, 409)
                return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            opts = json.loads(body.decode("utf-8"))
        except Exception:
            opts = {}
        threading.Thread(target=_install_worker, args=(opts,), daemon=True).start()
        self._send_json({"started": True})


def main() -> int:
    parser = argparse.ArgumentParser(description="Phonos Browser GUI Installer")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), InstallerHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Phonos Browser GUI Installer running at {url} (Ctrl+C to quit)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping installer.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
