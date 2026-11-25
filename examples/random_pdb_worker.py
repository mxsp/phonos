#!/usr/bin/env python3
"""
Custom Model Worker: random PDB fetcher
Site → Custom Node → Site workflow
"""

import os
import random
import time
import requests
import sys

# Configuration

# Allow overriding via environment variable
BASE_URL = os.environ.get("PHONOS_API_URL", "https://phonos.fyreflysystems.com/api/bridge/v1")

# Small pool of stable PDB IDs for quick downloads.
PDB_POOL = [
    "1CRN", "1BNA", "1UBQ", "1A3N", "2PTC", "4HHB", "1GFL", "2DN2"
]


def fetch_pdb_text(pdb_id):
    url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        return response.text
    except Exception as e:
        print(f"Warning: Failed to download PDB {pdb_id}: {e}")
        return "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N"


def process_job(input_data):
    """
    Input: {"sequence": "example_text"} or {"sequence": "1CRN"}
    Output: dict containing PDB id and content
    """
    seq = str(input_data.get("sequence", "")).strip().upper()
    pdb_id = seq if len(seq) == 4 and seq.isalnum() else random.choice(PDB_POOL)

    pdb_text = fetch_pdb_text(pdb_id)

    return {
        "status": "completed",
        "pdb_id": pdb_id,
        "pdb_length": len(pdb_text),
        "pdb_text": pdb_text,
    }

# Use environment variable if set, otherwise fallback (though fallback likely invalid for local)
API_KEY = os.environ.get("PHONOS_API_KEY", "pk__1Ft-owmSxNlLN3pUWNiWTVazUCRPmGLW6nGJt2tQ5U")

def main():
    if not API_KEY:
        raise RuntimeError("Set PHONOS_API_KEY in your environment.")

    print(f"Starting random PDB worker...")
    print(f"Polling {BASE_URL}")
    print(f"Using API Key: {API_KEY[:10]}...")

    while True:
        try:
            response = requests.post(
                f"{BASE_URL}/jobs/poll",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"max_jobs": 1},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            jobs = data.get("jobs", [])

            if jobs:
                for job in jobs:
                    job_id = job["job_id"]
                    input_data = job.get("input_data", {})

                    print(f"\nReceived job {job_id}")
                    print(f"   Input: {input_data}")

                    result = process_job(input_data)

                    requests.post(
                        f"{BASE_URL}/jobs/{job_id}/result",
                        headers={"Authorization": f"Bearer {API_KEY}"},
                        json={"status": "completed", "result": result},
                        timeout=20,
                    )

                    print(f"Job {job_id} completed")
            else:
                sys.stdout.write(".")
                sys.stdout.flush()

            time.sleep(5)

        except KeyboardInterrupt:
            print("\nWorker stopped")
            break
        except Exception as exc:
            print(f"\nError: {exc}")
            if hasattr(exc, 'response') and exc.response is not None:
                print(f"Response: {exc.response.text}")
            # If server is down or key invalid, wait a bit
            time.sleep(10)


if __name__ == "__main__":
    main()