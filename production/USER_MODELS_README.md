# User-Defined Models & Bridge API

**Quick Start Guide**

This system allows users to define their own computational models, host them on their own infrastructure, and connect them to the Phonos platform securely via the Bridge API.

> 📖 **For complete API reference, see [USER_MODELS_API_REFERENCE.md](USER_MODELS_API_REFERENCE.md)**

## Overview

User-defined models bridge the gap between Phonos's frontend and custom backend processing. Users can:
1. Define a model's interface (inputs, outputs, metadata).
2. Host a worker that listens for jobs or receives webhooks.
3. Process jobs on their own hardware (GPU clusters, local machines).
4. Return results to Phonos for display and storage.

### Documentation

| Document | Purpose | Audience |
|----------|---------|----------|
| **USER_MODELS_README.md** (this file) | Quick start guide | Beginners |
| **[USER_MODELS_API_REFERENCE.md](USER_MODELS_API_REFERENCE.md)** | Complete API documentation | Developers |
| **user_models_schema.sql** | Database schema | DBAs |
| **examples/user_model_worker_example.py** | Reference implementation | Developers |

## Key Concepts

### User Models (`user_models` table)
A definition of a computational tool.
- **Model ID**: Unique 16-character alphanumeric ID.
- **Input Schema**: JSON schema defining what inputs the model accepts.
- **Worker Endpoint**: Optional URL for webhook notifications when jobs are created.
- **Credit Cost**: How many credits a user pays to run this model.

### Bridge API (`user_model_api_keys` table)
Secure credentials for workers to authenticate with Phonos.
- **Worker Keys**: Used by the model hosting infrastructure to fetch jobs and post results.
- **Project Keys**: Can be used to access multiple models within a project.
- **Authentication**: Uses `Authorization: Bearer <API_KEY>` header.
- **Lifecycle**: Full key is only shown once at creation (API-key style handling).

### External Job Queue (`external_job_queue` table)
Tracks the state of jobs assigned to external workers.
- **Pending**: Job submitted, waiting for worker.
- **Processing**: Worker has claimed the job.
- **Completed/Failed**: Final states.

## API Endpoints

### Management (Frontend)
- `GET /api/user-models`: List models.
- `POST /api/user-models`: Create a new model.
- `PUT /api/user-models/<id>`: Update a model.
- `DELETE /api/user-models/<id>`: Delete a model.

### Bridge API (for Workers)
- `GET  /api/bridge/v1/health`: Check API health.
- `GET  /api/bridge/v1/worker/info`: Check authentication and authorized scope.
- `POST /api/bridge/v1/jobs/poll`: Poll for new pending jobs.
- `POST /api/bridge/v1/jobs/<job_id>/result`: Submit job results.

## Output Formats

Your model can return results in various formats. Set the `output_format` field when creating your model:

| Format | Use Case | Viewer |
|--------|----------|--------|
| `json` | Structured data, predictions | JSON viewer |
| `pdb` | Protein structures | 3D molecular viewer |
| `cif` / `mmcif` | Crystallographic data | 3D molecular viewer |
| `sdf` / `mol` / `mol2` | Small molecules | 3D molecular viewer |
| `txt` / `csv` | Text/tabular data | Text viewer |
| `png` / `jpg` | Images | Image viewer |
| `zip` | Multiple files | Download |

**Example:**
```json
{
  "model_name": "Protein Folder",
  "output_format": "pdb",  // Results shown in 3D viewer
  "input_schema": [ ... ]
}
```

See [USER_MODELS_API_REFERENCE.md](USER_MODELS_API_REFERENCE.md#output-formats) for complete format documentation.

## Input Schema Format

The input schema is a JSON list of field definitions:

```json
[
  {
    "name": "prompt",
    "label": "Prompt",
    "type": "text",
    "required": true,
    "placeholder": "Enter your prompt here..."
  },
  {
    "name": "seed",
    "label": "Random Seed",
    "type": "number",
    "default": 42
  }
]
```

**Supported Field Types:**
- `text` - Single-line text input
- `textarea` - Multi-line text input
- `number` - Numeric input
- `select` - Dropdown selection
- `checkbox` - Boolean toggle
- `file` - File upload

## Worker Implementation

See `phonos/examples/user_model_worker_example.py` for a complete reference implementation.

A minimal Python worker looks like this:

```python
import requests
import time
import os
import json
import hmac
import hashlib
from user_model_crypto import encrypt_data, decrypt_data

API_KEY = os.getenv('PHONOS_WORKER_KEY')
BASE_URL = "https://phonos.example.com"

def sign_payload(payload_dict):
    body = json.dumps(payload_dict, separators=(",", ":"))
    ts = int(time.time())
    sig = hmac.new(API_KEY.encode("utf-8"), f"{ts}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "X-Timestamp": str(ts),
        "X-Signature": sig,
    }
    return body, headers

def process_jobs():
    # 1. Poll for jobs (signed request)
    poll_body, poll_headers = sign_payload({"max_jobs": 1})
    resp = requests.post(f"{BASE_URL}/api/bridge/v1/jobs/poll", headers=poll_headers, data=poll_body)
    jobs = resp.json().get('jobs', [])
    
    for wrapped_job in jobs:
        # 2. Decrypt job payload when encryption is enabled for this key
        job = decrypt_data(wrapped_job['data'], API_KEY) if wrapped_job.get('encrypted') else wrapped_job
        job_id = job['job_id']
        print(f"Processing job {job_id}...")
        
        try:
            # 3. Run your model logic here...
            result = {"output": "Processed data"}

            # 4. Encrypt + sign result submission
            encrypted_result = encrypt_data({
                "status": "completed",
                "result": result
            }, API_KEY)
            submit_body, submit_headers = sign_payload({
                "encrypted": True,
                "data": encrypted_result
            })

            requests.post(
                f"{BASE_URL}/api/bridge/v1/jobs/{job_id}/result",
                headers=submit_headers,
                data=submit_body
            )
        except Exception as e:
            # Report failure
            encrypted_error = encrypt_data({
                "status": "failed",
                "error_message": str(e)
            }, API_KEY)
            error_body, error_headers = sign_payload({
                "encrypted": True,
                "data": encrypted_error
            })
            requests.post(
                f"{BASE_URL}/api/bridge/v1/jobs/{job_id}/result",
                headers=error_headers,
                data=error_body
            )

if __name__ == "__main__":
    while True:
        process_jobs()
        time.sleep(5)
```

## Result Submission Examples

### JSON Result
```python
result = {
    "status": "completed",
    "result": {
        "prediction": "positive",
        "confidence": 0.95
    },
    "metrics": {
        "runtime_seconds": 4.2
    }
}
```

### PDB Structure Result
```python
result = {
    "status": "completed",
    "result": {
        "structure_pdb": "ATOM      1  N   MET...",
        "confidence_scores": {
            "plddt": [95.2, 94.8, ...],
            "mean_plddt": 92.5
        }
    },
    "output_format": "pdb",
    "metrics": {
        "runtime_seconds": 45.2
    }
}
```

### Image Result (Base64 Encoded)
```python
import base64

with open("output.png", "rb") as f:
    image_data = base64.b64encode(f.read()).decode()

result = {
    "status": "completed",
    "result": {
        "image_base64": image_data,
        "width": 1920,
        "height": 1080
    },
    "output_format": "png"
}
```

### Failure Result
```python
result = {
    "status": "failed",
    "error_message": "CUDA out of memory",
    "error_type": "RuntimeError",
    "retry_recommended": false,
    "metrics": {
        "runtime_seconds": 5.2
    }
}
```

For more examples, see [USER_MODELS_API_REFERENCE.md](USER_MODELS_API_REFERENCE.md#requestresponse-examples).

## Security

- **API Keys**: Store securely. They are only shown once upon creation.
- **Encryption**: All worker keys are encrypted at rest using AES-256-GCM. Encryption-enabled worker keys use encrypted transport by default.
- **HTTPS**: Always use HTTPS for the Bridge API to protect the Bearer token and job data in transit.
- **Environment Variables**: Never hardcode API keys in your code. Use environment variables:
  ```python
  import os
  API_KEY = os.getenv('PHONOS_WORKER_KEY')
  ```

### Encrypted Communication

The Bridge API supports end-to-end encrypted communication using your API key as the encryption key. This provides an additional layer of security beyond HTTPS.

#### Encryption Features
- **AES-256-GCM** authenticated encryption for job data
- **HMAC-SHA256** request signing for integrity verification
- **PBKDF2** key derivation with 100,000 iterations
- **5-minute timestamp window** for replay attack protection
- **One-time key display** with hash-based verification and masked prefixes in UI

#### Using Encrypted Requests

```python
import json
import time
import hmac
import hashlib
import requests
from user_model_crypto import encrypt_data, sign_request

API_KEY = os.getenv('PHONOS_WORKER_KEY')
BASE_URL = "https://phonos.example.com"

def poll_jobs_encrypted():
    # Poll payload remains plaintext JSON, but signed
    payload = {"max_jobs": 1, "worker_id": "my-worker"}

    # Add request signature
    timestamp = int(time.time())
    body = json.dumps(payload, separators=(",", ":"))
    signature = sign_request(API_KEY, timestamp, body)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "X-Timestamp": str(timestamp),
        "X-Signature": signature
    }

    resp = requests.post(
        f"{BASE_URL}/api/bridge/v1/jobs/poll",
        headers=headers,
        data=body
    )

    result = resp.json()

    from user_model_crypto import decrypt_data
    jobs = []
    for wrapped_job in result.get("jobs", []):
        jobs.append(decrypt_data(wrapped_job["data"], API_KEY) if wrapped_job.get("encrypted") else wrapped_job)
    return jobs
```

#### Submitting Encrypted Results

```python
from user_model_crypto import encrypt_data, sign_request

def submit_encrypted_result(job_id, result_data):
    # Encrypt the result
    encrypted_result = encrypt_data({
        "status": "completed",
        "result": result_data,
        "metrics": {"runtime_seconds": 45.2}
    }, API_KEY)

    # Sign the request
    timestamp = int(time.time())
    body = json.dumps({"encrypted": True, "data": encrypted_result})
    signature = sign_request(API_KEY, timestamp, body)

    resp = requests.post(
        f"{BASE_URL}/api/bridge/v1/jobs/{job_id}/result",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "X-Timestamp": str(timestamp),
            "X-Signature": signature
        },
        data=body
    )
    return resp.json()
```

#### Python Encryption Helper

You can use the provided encryption module in your workers:

```python
# Copy from phonos/phonos_app/helpers/user_model_crypto.py
from user_model_crypto import (
    encrypt_data,
    decrypt_data,
    sign_request,
    verify_request,
    ModelMessage
)

# Using the ModelMessage helper class
msg = ModelMessage(API_KEY, encrypt=True)

# Pack data for transmission
packed = msg.pack({"status": "completed", "result": {...}})
# packed = {"timestamp": ..., "payload": {...}, "signature": "..."}

# Unpack and verify received data
data = msg.unpack(received_message)
```

## Testing & Verification

You can verify your connection to the platform using the provided utility script:

```bash
export PHONOS_API_URL="http://localhost:5000"
export PHONOS_WORKER_KEY="wk_..."
python3 phonos/examples/verify_user_model_connection.py
```
