#!/usr/bin/env python3
"""
Encrypted Worker Example for Phonos Custom Models

This example demonstrates how to implement a worker that uses
end-to-end encryption for secure job processing.

Features:
- AES-256-GCM encrypted job data
- HMAC-SHA256 request signing
- PBKDF2 key derivation
- Automatic encryption/decryption

Requirements:
    pip install requests cryptography

Usage:
    export PHONOS_API_KEY="mk_your_api_key_here"
    export PHONOS_API_URL="https://phonos.example.com"
    python encrypted_worker_example.py

For production use, copy the user_model_crypto module to your worker:
    cp phonos/phonos_app/helpers/user_model_crypto.py ./
"""

import os
import sys
import json
import time
import hmac
import hashlib
import secrets
import base64
import logging
from typing import Dict, Any, Optional, Union, Tuple

# Try to import cryptography for AES-256-GCM
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    print("Warning: cryptography library not installed. Using fallback encryption.")
    print("Install with: pip install cryptography")

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
API_KEY = os.environ.get('PHONOS_API_KEY')
BASE_URL = os.environ.get('PHONOS_API_URL', 'http://localhost:5000')
POLL_INTERVAL = 5  # seconds


# =============================================================================
# ENCRYPTION UTILITIES (Standalone version of user_model_crypto)
# =============================================================================

def derive_encryption_key(api_key: str, salt: bytes = None) -> Tuple[bytes, bytes]:
    """Derive a 256-bit encryption key from the API key using PBKDF2."""
    if salt is None:
        salt = secrets.token_bytes(16)

    key = hashlib.pbkdf2_hmac(
        'sha256',
        api_key.encode('utf-8'),
        salt,
        iterations=100000,
        dklen=32
    )
    return key, salt


def encrypt_data(data: Union[Dict[str, Any], str, bytes], api_key: str) -> str:
    """
    Encrypt data using AES-256-GCM.

    Args:
        data: Data to encrypt (dict, str, or bytes)
        api_key: The API key for encryption

    Returns:
        Base64-encoded encrypted payload
    """
    # Serialize data if needed
    if isinstance(data, dict):
        plaintext = json.dumps(data, separators=(',', ':')).encode('utf-8')
    elif isinstance(data, str):
        plaintext = data.encode('utf-8')
    else:
        plaintext = data

    if HAS_CRYPTOGRAPHY:
        # AES-256-GCM encryption
        salt = secrets.token_bytes(16)
        key, _ = derive_encryption_key(api_key, salt)
        nonce = secrets.token_bytes(12)

        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        # Pack: version(1) + salt(16) + nonce(12) + ciphertext
        version = b'\x01'
        packed = version + salt + nonce + ciphertext
    else:
        # Fallback XOR encryption
        key_bytes = hashlib.sha256(api_key.encode('utf-8')).digest()
        key_stream = (key_bytes * ((len(plaintext) // 32) + 1))[:len(plaintext)]
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, key_stream))

        mac = hmac.new(key_bytes, ciphertext, hashlib.sha256).digest()
        packed = b'\x00' + mac + ciphertext

    return base64.b64encode(packed).decode('ascii')


def decrypt_data(encrypted: str, api_key: str) -> Union[Dict[str, Any], bytes]:
    """
    Decrypt data using AES-256-GCM.

    Args:
        encrypted: Base64-encoded encrypted payload
        api_key: The API key for decryption

    Returns:
        Decrypted data (parsed as JSON if possible)
    """
    packed = base64.b64decode(encrypted)
    version = packed[0]

    if version == 1 and HAS_CRYPTOGRAPHY:
        # AES-256-GCM decryption
        salt = packed[1:17]
        nonce = packed[17:29]
        ciphertext = packed[29:]

        key, _ = derive_encryption_key(api_key, salt)
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    elif version == 0:
        # Fallback XOR decryption
        mac = packed[1:33]
        ciphertext = packed[33:]

        key_bytes = hashlib.sha256(api_key.encode('utf-8')).digest()
        expected_mac = hmac.new(key_bytes, ciphertext, hashlib.sha256).digest()

        if not hmac.compare_digest(mac, expected_mac):
            raise ValueError("HMAC verification failed")

        key_stream = (key_bytes * ((len(ciphertext) // 32) + 1))[:len(ciphertext)]
        plaintext = bytes(a ^ b for a, b in zip(ciphertext, key_stream))
    else:
        raise ValueError(f"Unsupported encryption version: {version}")

    # Try to parse as JSON
    try:
        return json.loads(plaintext.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return plaintext


def sign_request(api_key: str, timestamp: int, payload: str) -> str:
    """Create HMAC-SHA256 signature for request."""
    message = f"{timestamp}.{payload}"
    return hmac.new(
        api_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


# =============================================================================
# WORKER CLASS
# =============================================================================

class EncryptedWorker:
    """Worker that communicates with Phonos using encrypted payloads."""

    def __init__(self, api_key: str, base_url: str, worker_id: str = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.worker_id = worker_id or f"encrypted-worker-{secrets.token_hex(4)}"
        self.session = requests.Session()
        self.use_encryption = True

    def _make_request(self, method: str, endpoint: str, data: dict = None,
                      encrypt_request: bool = True, want_encrypted_response: bool = True) -> dict:
        """Make an authenticated, optionally encrypted request."""
        url = f"{self.base_url}{endpoint}"
        timestamp = int(time.time())

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Request encrypted response
        if want_encrypted_response and self.use_encryption:
            headers["X-Encryption"] = "true"

        # Prepare body
        if data:
            if encrypt_request and self.use_encryption:
                encrypted_data = encrypt_data(data, self.api_key)
                body = {"encrypted": True, "data": encrypted_data}
            else:
                body = data

            body_str = json.dumps(body)

            # Add signature
            signature = sign_request(self.api_key, timestamp, body_str)
            headers["X-Timestamp"] = str(timestamp)
            headers["X-Signature"] = signature
        else:
            body = None
            body_str = ""

        # Make request
        if method.upper() == "GET":
            response = self.session.get(url, headers=headers)
        elif method.upper() == "POST":
            response = self.session.post(url, headers=headers, json=body)
        else:
            raise ValueError(f"Unsupported method: {method}")

        response.raise_for_status()
        result = response.json()

        # Decrypt response if encrypted
        if result.get("encrypted") and result.get("data"):
            return decrypt_data(result["data"], self.api_key)

        return result

    def check_health(self) -> dict:
        """Check API health."""
        url = f"{self.base_url}/api/external/v1/health"
        response = self.session.get(url)
        return response.json()

    def get_worker_info(self) -> dict:
        """Get worker authentication info and stats."""
        return self._make_request("GET", "/api/external/v1/worker/info",
                                   want_encrypted_response=False)

    def poll_jobs(self, max_jobs: int = 1, model: str = None) -> list:
        """
        Poll for pending jobs using encrypted communication.

        Args:
            max_jobs: Maximum number of jobs to fetch
            model: Optional model filter

        Returns:
            List of jobs to process
        """
        data = {
            "worker_id": self.worker_id,
            "max_jobs": max_jobs
        }
        if model:
            data["model"] = model

        result = self._make_request("POST", "/api/external/v1/jobs/poll", data)
        return result.get("jobs", [])

    def submit_result(self, job_id: str, status: str = "completed",
                      result: dict = None, error_message: str = None,
                      metrics: dict = None) -> dict:
        """
        Submit job result using encrypted communication.

        Args:
            job_id: The job UUID
            status: "completed" or "failed"
            result: Result data (for completed jobs)
            error_message: Error message (for failed jobs)
            metrics: Optional metrics (runtime_seconds, memory_used_mb, etc.)

        Returns:
            API response
        """
        data = {"status": status}

        if result:
            data["result"] = result
        if error_message:
            data["error_message"] = error_message
        if metrics:
            data["metrics"] = metrics

        return self._make_request(
            "POST",
            f"/api/external/v1/jobs/{job_id}/result",
            data,
            encrypt_request=True,
            want_encrypted_response=False
        )

    def send_heartbeat(self, job_id: str, progress: int = None,
                       message: str = None) -> dict:
        """Send heartbeat for a running job."""
        data = {}
        if progress is not None:
            data["progress"] = progress
        if message:
            data["message"] = message

        return self._make_request(
            "POST",
            f"/api/external/v1/jobs/{job_id}/heartbeat",
            data if data else None,
            encrypt_request=False,
            want_encrypted_response=False
        )


# =============================================================================
# EXAMPLE PROCESSING FUNCTION
# =============================================================================

def process_job(job: dict) -> tuple:
    """
    Process a single job.

    Replace this with your actual processing logic.

    Args:
        job: Job data from the API

    Returns:
        Tuple of (success: bool, result: dict or error_message: str)
    """
    job_id = job.get("job_id")
    model = job.get("model")
    input_data = job.get("input_data", {})
    sequence = job.get("sequence", "")

    logger.info(f"Processing job {job_id} for model {model}")
    logger.info(f"Input sequence length: {len(sequence)}")

    # Simulate processing
    start_time = time.time()

    try:
        # YOUR PROCESSING LOGIC HERE
        # Example: just echo back the input with some transformation
        result = {
            "output": f"Processed by encrypted worker",
            "input_length": len(sequence),
            "model": model,
            "encrypted": True
        }

        # Simulate some work
        time.sleep(2)

        runtime = time.time() - start_time
        metrics = {
            "runtime_seconds": runtime,
            "encryption_overhead_ms": 5  # Approximate
        }

        return True, result, metrics

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        return False, str(e), {"runtime_seconds": time.time() - start_time}


# =============================================================================
# MAIN WORKER LOOP
# =============================================================================

def main():
    """Main worker loop."""
    if not API_KEY:
        print("Error: PHONOS_API_KEY environment variable not set")
        print("Usage: PHONOS_API_KEY=mk_... python encrypted_worker_example.py")
        sys.exit(1)

    worker = EncryptedWorker(API_KEY, BASE_URL)

    # Check health
    health = worker.check_health()
    logger.info(f"API Health: {health.get('status')}")

    # Get worker info
    try:
        info = worker.get_worker_info()
        logger.info(f"Authenticated as user {info.get('user_id')}")
        logger.info(f"Pending jobs: {info.get('stats', {}).get('pending_jobs', 0)}")
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        sys.exit(1)

    logger.info(f"Starting encrypted worker {worker.worker_id}")
    logger.info(f"Encryption: {'AES-256-GCM' if HAS_CRYPTOGRAPHY else 'XOR fallback'}")
    logger.info(f"Polling interval: {POLL_INTERVAL}s")

    while True:
        try:
            # Poll for jobs with encryption
            jobs = worker.poll_jobs(max_jobs=1)

            if jobs:
                for job in jobs:
                    job_id = job["job_id"]
                    logger.info(f"Received encrypted job: {job_id}")

                    # Process the job
                    success, result_or_error, metrics = process_job(job)

                    # Submit result with encryption
                    if success:
                        response = worker.submit_result(
                            job_id=job_id,
                            status="completed",
                            result=result_or_error,
                            metrics=metrics
                        )
                    else:
                        response = worker.submit_result(
                            job_id=job_id,
                            status="failed",
                            error_message=result_or_error,
                            metrics=metrics
                        )

                    logger.info(f"Job {job_id} result submitted: {response.get('status')}")

            else:
                logger.debug("No pending jobs")

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error: {e}")
        except Exception as e:
            logger.error(f"Error in worker loop: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
