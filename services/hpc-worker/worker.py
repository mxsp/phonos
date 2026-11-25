#!/usr/bin/env python3
"""Phonos HPC worker — runs on any (semi-external) machine, dials home.

Connects OUTBOUND to the Phonos API over HTTPS with a per-cluster bearer
token, long-polls for compute tasks, executes each in an isolated
subprocess (see task_runner.py), and posts results back. No inbound ports
are required on the worker machine, so it works behind NAT and firewalls.

Configuration (environment):
    PHONOS_HPC_URL          Phonos base URL, e.g. https://phonos.example.com
    PHONOS_HPC_TOKEN        Worker token from cluster creation (phc_...)
    PHONOS_HPC_TLS_VERIFY   'false' to allow self-signed certs (default true)
    PHONOS_HPC_LEASE_WAIT   long-poll seconds per lease call (default 20)
"""
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import time

import requests

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("hpc-worker")

BASE_URL = (os.environ.get("PHONOS_HPC_URL") or "").rstrip("/")
TOKEN = os.environ.get("PHONOS_HPC_TOKEN") or ""
TLS_VERIFY = (os.environ.get("PHONOS_HPC_TLS_VERIFY", "true").lower() != "false")
LEASE_WAIT = max(1, min(25, int(os.environ.get("PHONOS_HPC_LEASE_WAIT", "20"))))
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_runner.py")
ENVELOPE = "@PHONOS_HPC_RESULT@"

_running = True


def _headers():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _post(path, payload, timeout):
    resp = requests.post(f"{BASE_URL}{path}", json=payload,
                         headers=_headers(), timeout=timeout, verify=TLS_VERIFY)
    resp.raise_for_status()
    return resp.json()


def capabilities():
    caps = {"python": platform.python_version(), "host": platform.node()}
    for mod in ("numpy", "scipy", "pandas"):
        try:
            caps[mod] = __import__(mod).__version__
        except Exception:
            pass
    try:
        caps["cpus"] = os.cpu_count()
    except Exception:
        pass
    return caps


def register():
    backoff = 2
    while _running:
        try:
            info = _post("/api/hpc/worker/register",
                         {"capabilities": capabilities()}, timeout=15)
            log.info("Registered with cluster '%s'", info.get("cluster"))
            return
        except Exception as e:
            log.warning("Register failed (%s); retrying in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(60, backoff * 2)


def run_task(task):
    task_uuid = task["task_uuid"]
    timeout = int(task.get("timeout_seconds") or 300)
    log.info("Task %s leased (timeout %ss)", task_uuid, timeout)
    started = time.monotonic()
    outcome = {"status": "error", "error": "Worker error", "duration_ms": 0}
    try:
        proc = subprocess.run(
            [sys.executable, RUNNER],
            input=json.dumps({"code": task["code"], "args": task.get("args") or {}}),
            capture_output=True, text=True, timeout=timeout,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        envelope = None
        for line in reversed((proc.stdout or "").splitlines()):
            if line.startswith(ENVELOPE):
                envelope = json.loads(line[len(ENVELOPE):])
                break
        if envelope is None:
            outcome = {
                "status": "error",
                "error": ("Task produced no result envelope. "
                          f"stderr: {(proc.stderr or '')[:2000]}"),
                "duration_ms": duration_ms,
            }
        elif envelope.get("ok"):
            outcome = {"status": "done", "result": envelope.get("result"),
                       "stdout": envelope.get("stdout") or "",
                       "duration_ms": duration_ms}
        else:
            outcome = {"status": "error", "error": envelope.get("error") or "Task failed",
                       "stdout": envelope.get("stdout") or "",
                       "duration_ms": duration_ms}
    except subprocess.TimeoutExpired:
        outcome = {"status": "error",
                   "error": f"Task exceeded its {timeout}s timeout on the worker",
                   "duration_ms": int((time.monotonic() - started) * 1000)}
    except Exception as e:
        outcome = {"status": "error", "error": f"Worker exception: {e}",
                   "duration_ms": int((time.monotonic() - started) * 1000)}

    # Deliver the result with retries — never lose a finished computation to
    # a transient network blip.
    for attempt in range(5):
        try:
            _post(f"/api/hpc/worker/result/{task_uuid}", outcome, timeout=30)
            log.info("Task %s reported: %s (%sms)",
                     task_uuid, outcome["status"], outcome.get("duration_ms"))
            return
        except Exception as e:
            log.warning("Result delivery failed (%s); retry %d/5", e, attempt + 1)
            time.sleep(min(30, 2 ** attempt))
    log.error("Giving up delivering result for task %s", task_uuid)


def main():
    if not BASE_URL or not TOKEN:
        log.error("PHONOS_HPC_URL and PHONOS_HPC_TOKEN are required")
        sys.exit(1)
    if not TLS_VERIFY:
        log.warning("TLS certificate verification is DISABLED")

    def _stop(signum, frame):
        global _running
        log.info("Signal %s — shutting down after current task", signum)
        _running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    register()
    reregister_every = 60  # heartbeat via register even when idle
    last_register = time.monotonic()

    while _running:
        try:
            data = _post("/api/hpc/worker/lease", {"wait": LEASE_WAIT},
                         timeout=LEASE_WAIT + 15)
            task = data.get("task")
            if task:
                run_task(task)
            if time.monotonic() - last_register > reregister_every:
                _post("/api/hpc/worker/register",
                      {"capabilities": capabilities()}, timeout=15)
                last_register = time.monotonic()
        except Exception as e:
            log.warning("Lease loop error (%s); backing off 5s", e)
            time.sleep(5)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
