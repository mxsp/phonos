#!/usr/bin/env python3
"""
Phonos External API v1 - Python Test Suite

This script tests all endpoints of the External API v1.

Usage:
    python test_external_api_v1.py --api-key YOUR_API_KEY [--base-url http://localhost:5000]

Requirements:
    pip install requests
"""

import argparse
import json
import sys
import time
from typing import Any, Dict, Optional
from dataclasses import dataclass
from enum import Enum

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests")
    sys.exit(1)


class TestResult(Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class TestStats:
    passed: int = 0
    failed: int = 0
    skipped: int = 0


class PhonosAPITester:
    """Test suite for Phonos External API v1."""

    def __init__(self, api_key: str, base_url: str = "http://localhost:5000"):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.api_prefix = "/api/external/v1"
        self.stats = TestStats()
        self.test_project_id: Optional[int] = None
        self.test_job_id: Optional[str] = None

    def _get_url(self, endpoint: str) -> str:
        """Build full URL for endpoint."""
        return f"{self.base_url}{self.api_prefix}{endpoint}"

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers with auth."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def _request(self, method: str, endpoint: str, data: Optional[Dict] = None,
                 expected_status: int = 200) -> Dict[str, Any]:
        """Make API request and return response."""
        url = self._get_url(endpoint)
        headers = self._get_headers()

        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=30)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=data, timeout=30)
            elif method == "PUT":
                response = requests.put(url, headers=headers, json=data, timeout=30)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers, timeout=30)
            else:
                raise ValueError(f"Unknown method: {method}")

            return {
                "status_code": response.status_code,
                "data": response.json() if response.text else {},
                "success": response.status_code == expected_status
            }
        except requests.RequestException as e:
            return {
                "status_code": 0,
                "data": {"error": str(e)},
                "success": False
            }
        except json.JSONDecodeError:
            return {
                "status_code": response.status_code,
                "data": {"raw": response.text},
                "success": False
            }

    def _log_result(self, test_name: str, result: TestResult, details: str = ""):
        """Log test result."""
        colors = {
            TestResult.PASSED: "\033[92m",  # Green
            TestResult.FAILED: "\033[91m",  # Red
            TestResult.SKIPPED: "\033[93m"  # Yellow
        }
        reset = "\033[0m"

        if result == TestResult.PASSED:
            self.stats.passed += 1
        elif result == TestResult.FAILED:
            self.stats.failed += 1
        else:
            self.stats.skipped += 1

        status = f"{colors[result]}[{result.value}]{reset}"
        print(f"  {status} {test_name}")
        if details and result == TestResult.FAILED:
            print(f"         Details: {details}")

    def _log_section(self, title: str):
        """Log test section header."""
        print(f"\n{'=' * 60}")
        print(f" {title}")
        print('=' * 60)

    # ==========================================================================
    # Test Methods
    # ==========================================================================

    def test_api_info(self):
        """Test API info endpoint (no auth required)."""
        self._log_section("1. API Info")

        # Test /info without auth
        url = f"{self.base_url}{self.api_prefix}/info"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200 and "api" in response.json():
                self._log_result("GET /info (no auth)", TestResult.PASSED)
            else:
                self._log_result("GET /info (no auth)", TestResult.FAILED)
        except Exception as e:
            self._log_result("GET /info (no auth)", TestResult.FAILED, str(e))

    def test_user_endpoints(self):
        """Test user and account endpoints."""
        self._log_section("2. User & Account Endpoints")

        # Test /me
        result = self._request("GET", "/me")
        if result["success"] and result["data"].get("status") == "success":
            self._log_result("GET /me", TestResult.PASSED)
            user = result["data"].get("user", {})
            if user.get("id") and user.get("username"):
                self._log_result("GET /me - has user data", TestResult.PASSED)
            else:
                self._log_result("GET /me - has user data", TestResult.FAILED)
        else:
            self._log_result("GET /me", TestResult.FAILED, str(result["data"]))

        # Test /usage
        result = self._request("GET", "/usage")
        if result["success"] and result["data"].get("status") == "success":
            self._log_result("GET /usage", TestResult.PASSED)
        else:
            self._log_result("GET /usage", TestResult.FAILED)

        # Test /usage with period
        result = self._request("GET", "/usage?period=week")
        if result["success"]:
            self._log_result("GET /usage?period=week", TestResult.PASSED)
        else:
            self._log_result("GET /usage?period=week", TestResult.FAILED)

        # Test /credits
        result = self._request("GET", "/credits")
        if result["success"] and result["data"].get("status") == "success":
            self._log_result("GET /credits", TestResult.PASSED)
        else:
            self._log_result("GET /credits", TestResult.FAILED)

    def test_models_endpoints(self):
        """Test models endpoints."""
        self._log_section("3. Models Endpoints")

        # Test /models
        result = self._request("GET", "/models")
        if result["success"] and result["data"].get("models"):
            self._log_result("GET /models", TestResult.PASSED)
            model_count = len(result["data"]["models"])
            print(f"         Found {model_count} models")
        else:
            self._log_result("GET /models", TestResult.FAILED)

        # Test /models/esmfold
        result = self._request("GET", "/models/esmfold")
        if result["success"] and result["data"].get("model"):
            self._log_result("GET /models/esmfold", TestResult.PASSED)
        else:
            self._log_result("GET /models/esmfold", TestResult.FAILED)

        # Test invalid model
        result = self._request("GET", "/models/invalid_model", expected_status=404)
        if result["status_code"] == 404:
            self._log_result("GET /models/invalid (404)", TestResult.PASSED)
        else:
            self._log_result("GET /models/invalid (404)", TestResult.FAILED)

    def test_projects_endpoints(self):
        """Test projects endpoints."""
        self._log_section("4. Projects Endpoints")

        # Test GET /projects
        result = self._request("GET", "/projects")
        if result["success"] and "projects" in result["data"]:
            self._log_result("GET /projects", TestResult.PASSED)
        else:
            self._log_result("GET /projects", TestResult.FAILED)

        # Test POST /projects (create)
        project_data = {
            "name": "API Test Project",
            "description": "Created via Python test",
            "is_private": True
        }
        result = self._request("POST", "/projects", project_data, expected_status=201)
        if result["status_code"] == 201 and result["data"].get("project"):
            self._log_result("POST /projects (create)", TestResult.PASSED)
            self.test_project_id = result["data"]["project"].get("id")
            print(f"         Created project ID: {self.test_project_id}")
        else:
            self._log_result("POST /projects (create)", TestResult.FAILED, str(result["data"]))

        if self.test_project_id:
            # Test GET /projects/:id
            result = self._request("GET", f"/projects/{self.test_project_id}")
            if result["success"] and result["data"].get("project"):
                self._log_result(f"GET /projects/{self.test_project_id}", TestResult.PASSED)
            else:
                self._log_result(f"GET /projects/{self.test_project_id}", TestResult.FAILED)

            # Test PUT /projects/:id
            result = self._request("PUT", f"/projects/{self.test_project_id}",
                                   {"description": "Updated via Python test"})
            if result["success"]:
                self._log_result(f"PUT /projects/{self.test_project_id}", TestResult.PASSED)
            else:
                self._log_result(f"PUT /projects/{self.test_project_id}", TestResult.FAILED)

            # Test GET /projects/:id/members
            result = self._request("GET", f"/projects/{self.test_project_id}/members")
            if result["success"]:
                self._log_result(f"GET /projects/{self.test_project_id}/members", TestResult.PASSED)
            else:
                self._log_result(f"GET /projects/{self.test_project_id}/members", TestResult.FAILED)

            # Test GET /projects/:id/jobs
            result = self._request("GET", f"/projects/{self.test_project_id}/jobs")
            if result["success"]:
                self._log_result(f"GET /projects/{self.test_project_id}/jobs", TestResult.PASSED)
            else:
                self._log_result(f"GET /projects/{self.test_project_id}/jobs", TestResult.FAILED)

            # Test GET /projects/:id/files
            result = self._request("GET", f"/projects/{self.test_project_id}/files")
            if result["success"]:
                self._log_result(f"GET /projects/{self.test_project_id}/files", TestResult.PASSED)
            else:
                self._log_result(f"GET /projects/{self.test_project_id}/files", TestResult.FAILED)

    def test_jobs_endpoints(self):
        """Test jobs endpoints."""
        self._log_section("5. Jobs Endpoints")

        # Test GET /jobs
        result = self._request("GET", "/jobs")
        if result["success"] and "jobs" in result["data"]:
            self._log_result("GET /jobs", TestResult.PASSED)
        else:
            self._log_result("GET /jobs", TestResult.FAILED)

        # Test GET /jobs with filters
        result = self._request("GET", "/jobs?limit=5&status=completed")
        if result["success"]:
            self._log_result("GET /jobs?limit=5&status=completed", TestResult.PASSED)
        else:
            self._log_result("GET /jobs?limit=5&status=completed", TestResult.FAILED)

        # Test POST /jobs (submit)
        job_data = {
            "model": "esmfold",
            "sequence": "MKFLILLFNILCLFPVLAADNHGVTETK",
            "name": "API Test Job"
        }
        if self.test_project_id:
            job_data["project_id"] = self.test_project_id

        result = self._request("POST", "/jobs", job_data, expected_status=201)
        if result["status_code"] == 201 and result["data"].get("job"):
            self._log_result("POST /jobs (submit)", TestResult.PASSED)
            self.test_job_id = result["data"]["job"].get("job_id")
            print(f"         Created job ID: {self.test_job_id}")
        else:
            self._log_result("POST /jobs (submit)", TestResult.FAILED, str(result["data"]))

        if self.test_job_id:
            # Test GET /jobs/:id
            result = self._request("GET", f"/jobs/{self.test_job_id}")
            if result["success"] and result["data"].get("job"):
                self._log_result(f"GET /jobs/{self.test_job_id}", TestResult.PASSED)
            else:
                self._log_result(f"GET /jobs/{self.test_job_id}", TestResult.FAILED)

            # Test GET /jobs/:id/status
            result = self._request("GET", f"/jobs/{self.test_job_id}/status")
            if result["success"] and "job_status" in result["data"]:
                self._log_result(f"GET /jobs/{self.test_job_id}/status", TestResult.PASSED)
            else:
                self._log_result(f"GET /jobs/{self.test_job_id}/status", TestResult.FAILED)

            # Test POST /jobs/:id/cancel
            result = self._request("POST", f"/jobs/{self.test_job_id}/cancel")
            if result["success"] or result["data"].get("error") == "cannot_cancel":
                self._log_result(f"POST /jobs/{self.test_job_id}/cancel", TestResult.PASSED)
            else:
                self._log_result(f"POST /jobs/{self.test_job_id}/cancel", TestResult.FAILED)

        # Test batch submission
        batch_data = {
            "jobs": [
                {"model": "esmfold", "sequence": "MKFLIL", "name": "Batch 1"},
                {"model": "esmfold", "sequence": "MKFLIL", "name": "Batch 2"}
            ]
        }
        result = self._request("POST", "/jobs/batch", batch_data, expected_status=201)
        if result["status_code"] == 201:
            self._log_result("POST /jobs/batch", TestResult.PASSED)
            submitted = result["data"].get("submitted", 0)
            print(f"         Submitted {submitted} batch jobs")
        else:
            self._log_result("POST /jobs/batch", TestResult.FAILED)

    def test_files_endpoints(self):
        """Test files endpoints."""
        self._log_section("6. Files Endpoints")

        # Test GET /files
        result = self._request("GET", "/files")
        if result["success"] and "files" in result["data"]:
            self._log_result("GET /files", TestResult.PASSED)
        else:
            self._log_result("GET /files", TestResult.FAILED)

        # Skip upload test (requires multipart form)
        self._log_result("POST /files/upload", TestResult.SKIPPED, "Requires multipart form")

    def test_user_models_endpoints(self):
        """Test user models endpoints."""
        self._log_section("7. User Models Endpoints")

        # Test GET /user-models
        result = self._request("GET", "/user-models")
        if result["success"] and "models" in result["data"]:
            self._log_result("GET /user-models", TestResult.PASSED)
        else:
            self._log_result("GET /user-models", TestResult.FAILED)

    def test_search_endpoint(self):
        """Test search endpoint."""
        self._log_section("8. Search Endpoint")

        # Test GET /search
        result = self._request("GET", "/search?q=test")
        if result["success"] and "results" in result["data"]:
            self._log_result("GET /search?q=test", TestResult.PASSED)
        else:
            self._log_result("GET /search?q=test", TestResult.FAILED)

        # Test with type filter
        result = self._request("GET", "/search?q=protein&type=jobs")
        if result["success"]:
            self._log_result("GET /search?q=protein&type=jobs", TestResult.PASSED)
        else:
            self._log_result("GET /search?q=protein&type=jobs", TestResult.FAILED)

    def test_keys_endpoints(self):
        """Test API keys endpoints."""
        self._log_section("9. API Keys Endpoints")

        # Test GET /keys
        result = self._request("GET", "/keys")
        if result["success"] and "keys" in result["data"]:
            self._log_result("GET /keys", TestResult.PASSED)
        else:
            self._log_result("GET /keys", TestResult.FAILED)

        # Skip create/delete to avoid modifying keys
        self._log_result("POST /keys", TestResult.SKIPPED, "Skipped to avoid creating test keys")
        self._log_result("DELETE /keys/:id", TestResult.SKIPPED, "Skipped to avoid deleting keys")

    def test_error_handling(self):
        """Test error handling."""
        self._log_section("10. Error Handling")

        # Test invalid endpoint
        url = f"{self.base_url}{self.api_prefix}/nonexistent"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            if response.status_code == 404:
                self._log_result("Invalid endpoint returns 404", TestResult.PASSED)
            else:
                self._log_result("Invalid endpoint returns 404", TestResult.FAILED,
                                 f"Got {response.status_code}")
        except Exception as e:
            self._log_result("Invalid endpoint returns 404", TestResult.FAILED, str(e))

        # Test without auth
        url = f"{self.base_url}{self.api_prefix}/me"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 401:
                self._log_result("Missing auth returns 401", TestResult.PASSED)
            else:
                self._log_result("Missing auth returns 401", TestResult.FAILED,
                                 f"Got {response.status_code}")
        except Exception as e:
            self._log_result("Missing auth returns 401", TestResult.FAILED, str(e))

        # Test invalid auth
        url = f"{self.base_url}{self.api_prefix}/me"
        headers = {"Authorization": "Bearer invalid_key_12345"}
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 401:
                self._log_result("Invalid auth returns 401", TestResult.PASSED)
            else:
                self._log_result("Invalid auth returns 401", TestResult.FAILED,
                                 f"Got {response.status_code}")
        except Exception as e:
            self._log_result("Invalid auth returns 401", TestResult.FAILED, str(e))

    def cleanup(self):
        """Clean up test data."""
        self._log_section("11. Cleanup")

        if self.test_job_id:
            result = self._request("DELETE", f"/jobs/{self.test_job_id}")
            if result["success"]:
                self._log_result(f"Delete test job {self.test_job_id}", TestResult.PASSED)
            else:
                self._log_result(f"Delete test job {self.test_job_id}", TestResult.SKIPPED)

        if self.test_project_id:
            result = self._request("DELETE", f"/projects/{self.test_project_id}")
            if result["success"]:
                self._log_result(f"Delete test project {self.test_project_id}", TestResult.PASSED)
            else:
                self._log_result(f"Delete test project {self.test_project_id}", TestResult.SKIPPED)

    def run_all_tests(self):
        """Run all tests."""
        print("\n" + "=" * 60)
        print(" Phonos External API v1 - Python Test Suite")
        print("=" * 60)
        print(f"\nBase URL: {self.base_url}")
        print(f"API Key:  {self.api_key[:15]}...")

        try:
            self.test_api_info()
            self.test_user_endpoints()
            self.test_models_endpoints()
            self.test_projects_endpoints()
            self.test_jobs_endpoints()
            self.test_files_endpoints()
            self.test_user_models_endpoints()
            self.test_search_endpoint()
            self.test_keys_endpoints()
            self.test_error_handling()
            self.cleanup()
        except KeyboardInterrupt:
            print("\n\nTest interrupted by user.")

        self._print_summary()

    def _print_summary(self):
        """Print test summary."""
        print("\n" + "=" * 60)
        print(" Test Summary")
        print("=" * 60)
        print(f"\n  \033[92mPassed:\033[0m  {self.stats.passed}")
        print(f"  \033[91mFailed:\033[0m  {self.stats.failed}")
        print(f"  \033[93mSkipped:\033[0m {self.stats.skipped}")

        total = self.stats.passed + self.stats.failed
        if total > 0:
            success_rate = (self.stats.passed / total) * 100
            print(f"\n  Success Rate: {success_rate:.1f}%")

        print()

        if self.stats.failed == 0:
            print("\033[92mAll tests passed!\033[0m")
            return 0
        else:
            print("\033[91mSome tests failed.\033[0m")
            return 1


def main():
    parser = argparse.ArgumentParser(
        description="Test Phonos External API v1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_external_api_v1.py --api-key pk_abc123...
  python test_external_api_v1.py --api-key pk_abc123... --base-url https://phonos.example.com

To get an API key:
  1. Log into Phonos
  2. Go to Profile Settings > API Keys
  3. Create a new API key
        """
    )
    parser.add_argument("--api-key", "-k", required=True, help="Your Phonos API key")
    parser.add_argument("--base-url", "-u", default="http://localhost:5000",
                        help="Base URL of Phonos server (default: http://localhost:5000)")

    args = parser.parse_args()

    tester = PhonosAPITester(args.api_key, args.base_url)
    exit_code = tester.run_all_tests()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
