#!/usr/bin/env python3
"""
Retry the tests that failed due to rate limiting
"""
import os
import json
import time
import requests
from typing import Dict, Any

NVIDIA_BOLTZ2_URL = os.getenv(
    "BOLTZ2_API_URL",
    "https://health.api.nvidia.com/v1/biology/mit/boltz2/predict"
)
NVIDIA_API_KEY = os.getenv(
    "NVIDIA_API_KEY",
    "nvapi-YOUR_API_KEY_HERE"
)

print("=" * 60)
print("RETRYING PREVIOUSLY FAILED TESTS")
print("=" * 60)
print("Waiting 5 seconds to avoid rate limit...")
time.sleep(5)

headers = {
    "Authorization": f"Bearer {NVIDIA_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

# Test 1: DNA sequence (previously failed due to rate limit)
print("\n=== Test 1: DNA Sequence ===")
try:
    payload = {
        "polymers": [{
            "id": "D",
            "molecule_type": "dna",
            "sequence": "ATCGATCGATCG"
        }],
        "recycling_steps": 1,
        "sampling_steps": 50
    }

    response = requests.post(
        url=NVIDIA_BOLTZ2_URL,
        headers=headers,
        json=payload,
        timeout=60
    )

    if response.status_code in [200, 202]:
        result = response.json() if response.status_code == 200 else {"status": "async"}
        print(f"✅ PASS - DNA sequence accepted")
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            print(f"   Confidence: {result.get('confidence_scores', [None])[0]}")
    elif response.status_code == 429:
        print(f"⚠️  RATE LIMITED - Try again later")
    else:
        print(f"❌ FAIL - HTTP {response.status_code}: {response.text[:200]}")
except Exception as e:
    print(f"❌ FAIL - {e}")

time.sleep(3)

# Test 2: RNA sequence (previously failed due to rate limit)
print("\n=== Test 2: RNA Sequence ===")
try:
    payload = {
        "polymers": [{
            "id": "R",
            "molecule_type": "rna",
            "sequence": "AUCGAUCGAUCG"
        }],
        "recycling_steps": 1,
        "sampling_steps": 50
    }

    response = requests.post(
        url=NVIDIA_BOLTZ2_URL,
        headers=headers,
        json=payload,
        timeout=60
    )

    if response.status_code in [200, 202]:
        result = response.json() if response.status_code == 200 else {"status": "async"}
        print(f"✅ PASS - RNA sequence accepted")
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            print(f"   Confidence: {result.get('confidence_scores', [None])[0]}")
    elif response.status_code == 429:
        print(f"⚠️  RATE LIMITED - Try again later")
    else:
        print(f"❌ FAIL - HTTP {response.status_code}: {response.text[:200]}")
except Exception as e:
    print(f"❌ FAIL - {e}")

time.sleep(3)

# Test 3: Multi-chain complex (previously failed due to rate limit)
print("\n=== Test 3: Multi-Chain Complex ===")
try:
    payload = {
        "polymers": [
            {
                "id": "A",
                "molecule_type": "protein",
                "sequence": "GIVEQCCTSICSLYQLENYCN"
            },
            {
                "id": "B",
                "molecule_type": "protein",
                "sequence": "FVNQHLCGSHLVEALYLVCGERGFFYTPKT"
            }
        ],
        "recycling_steps": 2,
        "sampling_steps": 50,
        "diffusion_samples": 1
    }

    response = requests.post(
        url=NVIDIA_BOLTZ2_URL,
        headers=headers,
        json=payload,
        timeout=60
    )

    if response.status_code in [200, 202]:
        result = response.json() if response.status_code == 200 else {"status": "async"}
        print(f"✅ PASS - Multi-chain complex accepted")
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            iptm = result.get('iptm_scores', [None])[0]
            print(f"   Confidence: {result.get('confidence_scores', [None])[0]}")
            print(f"   iPTM (interface): {iptm}")
    elif response.status_code == 429:
        print(f"⚠️  RATE LIMITED - Try again later")
    else:
        print(f"❌ FAIL - HTTP {response.status_code}: {response.text[:200]}")
except Exception as e:
    print(f"❌ FAIL - {e}")

print("\n" + "=" * 60)
print("RETRY COMPLETE")
print("=" * 60)
print("\nNote: If tests are still rate limited, wait a few minutes")
print("and run: python3 retry_failed_tests.py")
