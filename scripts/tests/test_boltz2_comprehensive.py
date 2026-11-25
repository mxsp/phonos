#!/usr/bin/env python3
"""
Comprehensive Boltz2 Model Testing Suite
Tests NVIDIA Boltz2 API integration for protein structure prediction
"""
import os
import sys
import json
import time
import requests
from typing import Dict, Any, List
from pathlib import Path

# NVIDIA API Configuration
NVIDIA_BOLTZ2_URL = os.getenv(
    "BOLTZ2_API_URL",
    "https://health.api.nvidia.com/v1/biology/mit/boltz2/predict"
)
NVIDIA_STATUS_URL = os.getenv(
    "BOLTZ2_STATUS_URL",
    "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{task_id}"
)
NVIDIA_API_KEY = os.getenv(
    "NVIDIA_API_KEY",
    "nvapi-YOUR_API_KEY_HERE"
)

# Test results tracking
test_results = []
NVCF_POLL_SECONDS = 300
NVCF_POLL_INTERVAL = 2


def log_test(name: str, status: str, details: str = "", response_data: Any = None):
    """Log test result."""
    result = {
        "test": name,
        "status": status,
        "details": details,
        "timestamp": time.time()
    }
    if response_data:
        result["response_data"] = response_data
    test_results.append(result)

    status_icon = "✓" if status == "PASS" else "✗" if status == "FAIL" else "⚠"
    print(f"{status_icon} {name}: {status}")
    if details:
        print(f"  └─ {details}")


def poll_nvcf_status(task_id: str) -> Dict[str, Any]:
    """Poll NVCF status endpoint until task completes."""
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Accept": "application/json"
    }

    status_url = NVIDIA_STATUS_URL.format(task_id=task_id)
    start_time = time.time()

    print(f"  └─ Polling task {task_id}...")

    while time.time() - start_time < NVCF_POLL_SECONDS:
        try:
            response = requests.get(status_url, headers=headers, timeout=30)

            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return response.json()
                else:
                    return {"raw_content": response.text[:5000]}

            elif response.status_code == 202:
                elapsed = time.time() - start_time
                print(f"  └─ Still processing... ({elapsed:.1f}s elapsed)")
                time.sleep(NVCF_POLL_INTERVAL)
                continue

            elif response.status_code == 404:
                raise Exception(f"Task {task_id} not found")

            else:
                raise Exception(f"Status check error: {response.status_code} - {response.text[:500]}")

        except requests.exceptions.Timeout:
            print(f"  └─ Timeout during polling, retrying...")
            time.sleep(NVCF_POLL_INTERVAL)
            continue

    raise Exception(f"Task {task_id} timed out after {NVCF_POLL_SECONDS} seconds")


def test_api_connectivity():
    """Test 1: API endpoint connectivity and authentication."""
    print("\n=== Test 1: API Connectivity & Authentication ===")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Test with minimal valid payload
    payload = {
        "polymers": [{
            "molecule_type": "protein",
            "sequence": "MKFLKFSLLTAVLLSVVFAFSSCGDDDDTGYLPPSQAIQDLLKRMKV"  # Short test sequence
        }],
        "recycling_steps": 1,
        "sampling_steps": 10,
        "diffusion_samples": 1
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code in [200, 202]:
            log_test(
                "API Connectivity",
                "PASS",
                f"Status: {response.status_code}, Response time: {response.elapsed.total_seconds():.2f}s"
            )

            # Handle async response
            if response.status_code == 202:
                nvcf_reqid = response.headers.get("NVCF-REQID")
                log_test(
                    "Async Task Creation",
                    "PASS",
                    f"Task ID: {nvcf_reqid}"
                )
                return True
            else:
                result = response.json()
                log_test(
                    "Immediate Response",
                    "PASS",
                    f"Returned {len(result.get('structures', []))} structure(s)"
                )
                return True
        else:
            log_test(
                "API Connectivity",
                "FAIL",
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
            return False

    except requests.exceptions.Timeout:
        log_test("API Connectivity", "FAIL", "Request timeout")
        return False
    except Exception as e:
        log_test("API Connectivity", "FAIL", str(e))
        return False


def test_protein_prediction():
    """Test 2: Basic protein structure prediction."""
    print("\n=== Test 2: Protein Structure Prediction ===")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Test with insulin chain A
    payload = {
        "polymers": [{
            "id": "A",
            "molecule_type": "protein",
            "sequence": "GIVEQCCTSICSLYQLENYCN"
        }],
        "recycling_steps": 2,
        "sampling_steps": 50,
        "diffusion_samples": 1
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        result = None

        if response.status_code == 200:
            result = response.json()
        elif response.status_code == 202:
            nvcf_reqid = response.headers.get("NVCF-REQID")
            result = poll_nvcf_status(nvcf_reqid)
        else:
            log_test(
                "Protein Prediction",
                "FAIL",
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
            return False

        # Validate response structure
        if not result:
            log_test("Protein Prediction", "FAIL", "No result returned")
            return False

        structures = result.get('structures', [])
        confidence_scores = result.get('confidence_scores', [])

        if not structures:
            log_test("Protein Prediction", "FAIL", "No structures in response")
            return False

        log_test(
            "Protein Prediction",
            "PASS",
            f"Generated {len(structures)} structure(s), Confidence: {confidence_scores[0] if confidence_scores else 'N/A'}",
            response_data={
                "structure_count": len(structures),
                "confidence_scores": confidence_scores,
                "ptm_scores": result.get('ptm_scores', []),
                "iptm_scores": result.get('iptm_scores', [])
            }
        )

        # Save structure for inspection
        if structures:
            output_file = Path("test_output_protein.cif")
            output_file.write_text(structures[0].get('structure', ''))
            print(f"  └─ Structure saved to: {output_file}")

        return True

    except Exception as e:
        log_test("Protein Prediction", "FAIL", str(e))
        return False


def test_protein_ligand_binding():
    """Test 3: Protein-ligand binding prediction."""
    print("\n=== Test 3: Protein-Ligand Binding Prediction ===")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Test with protein + ATP ligand
    payload = {
        "polymers": [{
            "id": "A",
            "molecule_type": "protein",
            "sequence": "GIVEQCCTSICSLYQLENYCN"
        }],
        "ligands": [{
            "id": "L1",
            "ccd": "ATP"
        }],
        "recycling_steps": 2,
        "sampling_steps": 50,
        "diffusion_samples": 1
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        result = None

        if response.status_code == 200:
            result = response.json()
        elif response.status_code == 202:
            nvcf_reqid = response.headers.get("NVCF-REQID")
            result = poll_nvcf_status(nvcf_reqid)
        else:
            log_test(
                "Protein-Ligand Binding",
                "FAIL",
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
            return False

        if not result:
            log_test("Protein-Ligand Binding", "FAIL", "No result returned")
            return False

        structures = result.get('structures', [])

        if not structures:
            log_test("Protein-Ligand Binding", "FAIL", "No structures in response")
            return False

        log_test(
            "Protein-Ligand Binding",
            "PASS",
            f"Generated complex structure with ligand",
            response_data={
                "structure_count": len(structures),
                "has_ligand": True
            }
        )

        # Save structure
        if structures:
            output_file = Path("test_output_ligand_complex.cif")
            output_file.write_text(structures[0].get('structure', ''))
            print(f"  └─ Complex structure saved to: {output_file}")

        return True

    except Exception as e:
        log_test("Protein-Ligand Binding", "FAIL", str(e))
        return False


def test_smiles_ligand():
    """Test 4: Ligand binding with SMILES notation."""
    print("\n=== Test 4: SMILES Ligand Input ===")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Test with aspirin SMILES
    payload = {
        "polymers": [{
            "id": "A",
            "molecule_type": "protein",
            "sequence": "MKFLKFSLLTAVLLSVVFAFSSCGDDDDTGYLPPSQAIQDLLKRMKV"
        }],
        "ligands": [{
            "id": "ASP",
            "smiles": "CC(=O)OC1=CC=CC=C1C(=O)O"  # Aspirin
        }],
        "recycling_steps": 1,
        "sampling_steps": 50,
        "diffusion_samples": 1
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        result = None

        if response.status_code == 200:
            result = response.json()
        elif response.status_code == 202:
            nvcf_reqid = response.headers.get("NVCF-REQID")
            result = poll_nvcf_status(nvcf_reqid)
        else:
            log_test(
                "SMILES Ligand",
                "FAIL",
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
            return False

        if not result or not result.get('structures'):
            log_test("SMILES Ligand", "FAIL", "No structures returned")
            return False

        log_test(
            "SMILES Ligand",
            "PASS",
            "Successfully predicted structure with SMILES ligand"
        )

        return True

    except Exception as e:
        log_test("SMILES Ligand", "FAIL", str(e))
        return False


def test_parameter_validation():
    """Test 5: Parameter validation and edge cases."""
    print("\n=== Test 5: Parameter Validation ===")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Test 5.1: Invalid recycling_steps (should be 1-6)
    print("\n  Test 5.1: Invalid recycling_steps")
    payload = {
        "polymers": [{
            "molecule_type": "protein",
            "sequence": "MKFLKFSLLTAVLLSVVFAFSSCGDDDDTGYLPPSQAIQDLLKRMKV"
        }],
        "recycling_steps": 10,  # Invalid: max is 6
        "sampling_steps": 50
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 422:
            log_test(
                "Invalid Recycling Steps",
                "PASS",
                "Correctly rejected invalid recycling_steps"
            )
        else:
            log_test(
                "Invalid Recycling Steps",
                "WARN",
                f"Expected 422, got {response.status_code}"
            )
    except Exception as e:
        log_test("Invalid Recycling Steps", "FAIL", str(e))

    # Test 5.2: Invalid sequence (non-amino acid characters)
    print("\n  Test 5.2: Invalid sequence characters")
    payload = {
        "polymers": [{
            "molecule_type": "protein",
            "sequence": "MKFLX123INVALID"  # Contains numbers
        }],
        "recycling_steps": 3,
        "sampling_steps": 50
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 422:
            log_test(
                "Invalid Sequence",
                "PASS",
                "Correctly rejected invalid sequence"
            )
        else:
            log_test(
                "Invalid Sequence",
                "WARN",
                f"Expected 422, got {response.status_code}"
            )
    except Exception as e:
        log_test("Invalid Sequence", "FAIL", str(e))

    # Test 5.3: Empty sequence
    print("\n  Test 5.3: Empty sequence")
    payload = {
        "polymers": [{
            "molecule_type": "protein",
            "sequence": ""
        }],
        "recycling_steps": 3,
        "sampling_steps": 50
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 422:
            log_test(
                "Empty Sequence",
                "PASS",
                "Correctly rejected empty sequence"
            )
        else:
            log_test(
                "Empty Sequence",
                "WARN",
                f"Expected 422, got {response.status_code}"
            )
    except Exception as e:
        log_test("Empty Sequence", "FAIL", str(e))

    # Test 5.4: Too many polymers (max 12)
    print("\n  Test 5.4: Too many polymers")
    payload = {
        "polymers": [
            {"molecule_type": "protein", "sequence": "MKFL"} for _ in range(13)
        ],
        "recycling_steps": 1,
        "sampling_steps": 10
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 422:
            log_test(
                "Too Many Polymers",
                "PASS",
                "Correctly rejected >12 polymers"
            )
        else:
            log_test(
                "Too Many Polymers",
                "WARN",
                f"Expected 422, got {response.status_code}"
            )
    except Exception as e:
        log_test("Too Many Polymers", "FAIL", str(e))


def test_dna_rna_support():
    """Test 6: DNA and RNA molecule types."""
    print("\n=== Test 6: DNA/RNA Support ===")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Test 6.1: DNA sequence
    print("\n  Test 6.1: DNA sequence")
    payload = {
        "polymers": [{
            "id": "D",
            "molecule_type": "dna",
            "sequence": "ATCGATCGATCG"
        }],
        "recycling_steps": 1,
        "sampling_steps": 50
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code in [200, 202]:
            log_test(
                "DNA Sequence",
                "PASS",
                "DNA sequence accepted"
            )
        else:
            log_test(
                "DNA Sequence",
                "FAIL",
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
    except Exception as e:
        log_test("DNA Sequence", "FAIL", str(e))

    # Test 6.2: RNA sequence
    print("\n  Test 6.2: RNA sequence")
    payload = {
        "polymers": [{
            "id": "R",
            "molecule_type": "rna",
            "sequence": "AUCGAUCGAUCG"
        }],
        "recycling_steps": 1,
        "sampling_steps": 50
    }

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code in [200, 202]:
            log_test(
                "RNA Sequence",
                "PASS",
                "RNA sequence accepted"
            )
        else:
            log_test(
                "RNA Sequence",
                "FAIL",
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
    except Exception as e:
        log_test("RNA Sequence", "FAIL", str(e))


def test_multichain_complex():
    """Test 7: Multi-chain protein complex."""
    print("\n=== Test 7: Multi-Chain Complex ===")

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Test with 2-chain complex
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

    try:
        response = requests.post(
            url=NVIDIA_BOLTZ2_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        result = None

        if response.status_code == 200:
            result = response.json()
        elif response.status_code == 202:
            nvcf_reqid = response.headers.get("NVCF-REQID")
            result = poll_nvcf_status(nvcf_reqid)
        else:
            log_test(
                "Multi-Chain Complex",
                "FAIL",
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
            return False

        if not result or not result.get('structures'):
            log_test("Multi-Chain Complex", "FAIL", "No structures returned")
            return False

        iptm_scores = result.get('iptm_scores', [])

        log_test(
            "Multi-Chain Complex",
            "PASS",
            f"Multi-chain complex predicted, iPTM: {iptm_scores[0] if iptm_scores else 'N/A'}",
            response_data={
                "chain_count": 2,
                "iptm_scores": iptm_scores
            }
        )

        return True

    except Exception as e:
        log_test("Multi-Chain Complex", "FAIL", str(e))
        return False


def generate_report():
    """Generate comprehensive test report."""
    print("\n" + "=" * 60)
    print("BOLTZ2 TESTING SUMMARY")
    print("=" * 60)

    total_tests = len(test_results)
    passed = sum(1 for t in test_results if t['status'] == 'PASS')
    failed = sum(1 for t in test_results if t['status'] == 'FAIL')
    warnings = sum(1 for t in test_results if t['status'] == 'WARN')

    print(f"\nTotal Tests: {total_tests}")
    print(f"✓ Passed: {passed}")
    print(f"✗ Failed: {failed}")
    print(f"⚠ Warnings: {warnings}")

    if failed > 0:
        print("\n--- Failed Tests ---")
        for result in test_results:
            if result['status'] == 'FAIL':
                print(f"  ✗ {result['test']}: {result['details']}")

    if warnings > 0:
        print("\n--- Warnings ---")
        for result in test_results:
            if result['status'] == 'WARN':
                print(f"  ⚠ {result['test']}: {result['details']}")

    # Save full report
    report_file = Path("boltz2_test_report.json")
    report_file.write_text(json.dumps({
        "summary": {
            "total": total_tests,
            "passed": passed,
            "failed": failed,
            "warnings": warnings,
            "timestamp": time.time()
        },
        "tests": test_results
    }, indent=2))

    print(f"\n📄 Full report saved to: {report_file}")

    return failed == 0


def main():
    """Run all tests."""
    print("=" * 60)
    print("BOLTZ2 COMPREHENSIVE TESTING SUITE")
    print("=" * 60)
    print(f"API URL: {NVIDIA_BOLTZ2_URL}")
    print(f"API Key: {NVIDIA_API_KEY[:20]}...")
    print("=" * 60)

    # Run all test suites
    test_api_connectivity()
    test_protein_prediction()
    test_protein_ligand_binding()
    test_smiles_ligand()
    test_parameter_validation()
    test_dna_rna_support()
    test_multichain_complex()

    # Generate final report
    success = generate_report()

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
