#!/usr/bin/env python3
"""
Hello-world custom model worker for Phonos Bridge API.

This worker polls for jobs and submits a deterministic dummy PDB result.
It is intended for local/server smoke testing with a non-encrypted worker key.

Usage:
    export PHONOS_API_URL="http://localhost:5000"
    export PHONOS_API_KEY="wk_..."
    export WORKER_ID="hello-world-pdb-worker"
    export RUN_ONCE="1"  # Optional: process one polling cycle then exit
    python examples/hello_world_dummy_pdb_worker.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import requests


BASE_URL = os.environ.get("PHONOS_API_URL", "http://localhost:5000").rstrip("/")
API_KEY = os.environ.get("PHONOS_API_KEY", "").strip()
WORKER_ID = os.environ.get("WORKER_ID", "hello-world-pdb-worker").strip()
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
RUN_ONCE = os.environ.get("RUN_ONCE", "0") == "1"
MODEL_ID_RAW = os.environ.get("PHONOS_MODEL_ID", "").strip()


def _resolve_model_id():
    if not MODEL_ID_RAW:
        return None
    try:
        return int(MODEL_ID_RAW)
    except ValueError:
        return MODEL_ID_RAW


def generate_dummy_pdb(sequence: str) -> str:
    sequence_snippet = (sequence or "HELLO_WORLD").strip()[:64]
    date_fragment = datetime.utcnow().strftime("%d-%b-%y").upper()
    return (
        f"HEADER    HELLO WORLD DUMMY PDB                 {date_fragment}\n"
        "TITLE     PHONOS HELLO WORLD CUSTOM MODEL\n"
        f"REMARK   1 INPUT SEQUENCE: {sequence_snippet}\n"
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1       1.246   2.390   0.000  1.00  0.00           O\n"
        "TER\n"
        "END\n"
    )


def poll_jobs(session: requests.Session, model_id):
    payload = {
        "worker_id": WORKER_ID,
        "max_jobs": 1,
    }
    if model_id is not None:
        payload["model_id"] = model_id

    response = session.post(f"{BASE_URL}/api/bridge/v1/jobs/poll", json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("jobs", [])


def submit_result(session: requests.Session, job_id: str, result_payload: dict) -> bool:
    response = session.post(
        f"{BASE_URL}/api/bridge/v1/jobs/{job_id}/result",
        json=result_payload,
        timeout=30,
    )
    if response.ok:
        return True

    print(f"Result submission failed for {job_id}: {response.status_code} {response.text}")
    return False


def process_job(session: requests.Session, job: dict) -> bool:
    if job.get("encrypted"):
        print(
            "Received encrypted job payload. "
            "Use a worker key with encryption disabled for this hello-world worker."
        )
        return False

    job_id = job.get("job_id")
    input_data = job.get("input_data") or {}
    sequence = str(input_data.get("sequence", "HELLO_WORLD"))

    dummy_pdb = generate_dummy_pdb(sequence)
    result_payload = {
        "status": "completed",
        "result": {
            "structure_pdb": dummy_pdb,
            "confidence_scores": {"plddt": [100.0, 99.9, 99.8, 99.7]},
            "message": "Hello world dummy model response",
        },
        "metrics": {"runtime_seconds": 0.01},
    }
    return submit_result(session, job_id, result_payload)


def main() -> int:
    if not API_KEY:
        print("PHONOS_API_KEY is required.")
        return 1

    model_id = _resolve_model_id()
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
    )

    print(f"Starting hello-world dummy PDB worker against {BASE_URL}")
    if model_id is not None:
        print(f"Filtering by model_id={model_id}")

    processed = 0
    while True:
        try:
            jobs = poll_jobs(session, model_id)
            if not jobs:
                print("No pending jobs.")
                if RUN_ONCE:
                    break
                time.sleep(POLL_INTERVAL)
                continue

            for job in jobs:
                job_id = job.get("job_id", "<unknown>")
                print(f"Processing job {job_id}")
                if process_job(session, job):
                    processed += 1
                    print(f"Completed job {job_id}")
                else:
                    print(f"Failed job {job_id}")

            if RUN_ONCE:
                break
        except KeyboardInterrupt:
            print("Worker stopped.")
            break
        except Exception as exc:
            print(f"Worker error: {exc}")
            if RUN_ONCE:
                break
            time.sleep(POLL_INTERVAL)

    print(f"Worker finished. Processed {processed} job(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
