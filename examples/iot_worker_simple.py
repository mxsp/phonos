#!/usr/bin/env python3
"""
Simple Phonos IoT Worker - Minimal Example
"""

import os
import time
import requests

API_KEY = os.environ.get("PHONOS_API_KEY", "dk_your_key_here")
API_URL = os.environ.get("PHONOS_API_URL", "http://localhost:5000")

def submit_data(data: dict):
    """Submit data to Phonos."""
    response = requests.post(
        f"{API_URL}/api/iot/v2/device/data",
        headers={
            "Content-Type": "application/json",
            "X-Device-Key": API_KEY
        },
        json={"data": data, "type": "reading"}
    )
    print(f"Status: {response.status_code}, Response: {response.json()}")
    return response.json()

# Example: Submit single reading
if __name__ == "__main__":
    # Submit test data
    result = submit_data({
        "temperature": 25.5,
        "humidity": 60,
        "status": "ok"
    })

    # Continuous loop example
    # while True:
    #     submit_data({"temperature": 25.5})
    #     time.sleep(30)
