# User Models API - Quick Reference Card

**One-page cheat sheet for the Bridge API**

---

## Authentication

```http
Authorization: Bearer wk_abc123...
X-Timestamp: 1710600000
X-Signature: hmac_sha256(timestamp + "." + raw_json_body)
```

---

## Endpoints

```
Base URL: https://phonos.bio/api/bridge/v1
```

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Check API health |
| `GET` | `/worker/info` | Verify authentication |
| `POST` | `/jobs/poll` | Get pending jobs |
| `POST` | `/jobs/<job_id>/result` | Submit result |

---

## Poll for Jobs

**Request:**
```json
POST /jobs/poll
{
  "max_jobs": 1,
  "model_id": 123
}
```

**Response:**
```json
{
  "status": "success",
  "jobs": [
    {
      "encrypted": true,
      "data": "BASE64_ENCRYPTED_PAYLOAD"
    }
  ],
  "signature_required": true
}
```

---

## Submit Result

### Success (JSON)
```json
POST /jobs/abc-123/result
{
  "encrypted": true,
  "data": "BASE64_ENCRYPTED_PAYLOAD"
}
```

### Success (PDB)
```json
{
  "encrypted": true,
  "data": "BASE64_ENCRYPTED_PAYLOAD"
}
```

### Success (Binary/Image)
```json
{
  "encrypted": true,
  "data": "BASE64_ENCRYPTED_PAYLOAD"
}
```

### Failure
```json
{
  "encrypted": true,
  "data": "BASE64_ENCRYPTED_PAYLOAD"
}
```

---

## Output Formats

| Format | Use Case | Viewer |
|--------|----------|--------|
| `json` | Data, predictions | JSON viewer |
| `pdb` | Proteins | 3D viewer |
| `cif` | Crystals | 3D viewer |
| `sdf` | Molecules | 3D viewer |
| `png` | Images | Image viewer |
| `txt` | Text | Text viewer |
| `zip` | Archives | Download |

---

## Python Worker Template

```python
import requests
import time
import os
import json
import hmac
import hashlib
from user_model_crypto import encrypt_data, decrypt_data

API_KEY = os.getenv('PHONOS_WORKER_KEY')
BASE_URL = "https://phonos.bio"

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

while True:
    # Poll for jobs
    poll_body, poll_headers = sign_payload({"max_jobs": 1})
    resp = requests.post(f"{BASE_URL}/api/bridge/v1/jobs/poll", headers=poll_headers, data=poll_body)
    jobs = resp.json().get('jobs', [])

    for wrapped_job in jobs:
        job = decrypt_data(wrapped_job["data"], API_KEY) if wrapped_job.get("encrypted") else wrapped_job
        job_id = job['job_id']
        try:
            # Process job
            result = process(job['input_data'])

            encrypted = encrypt_data({"status": "completed", "result": result}, API_KEY)
            submit_body, submit_headers = sign_payload({"encrypted": True, "data": encrypted})
            requests.post(f"{BASE_URL}/api/bridge/v1/jobs/{job_id}/result", headers=submit_headers, data=submit_body)
        except Exception as e:
            # Submit failure
            encrypted = encrypt_data({"status": "failed", "error_message": str(e)}, API_KEY)
            fail_body, fail_headers = sign_payload({"encrypted": True, "data": encrypted})
            requests.post(f"{BASE_URL}/api/bridge/v1/jobs/{job_id}/result", headers=fail_headers, data=fail_body)

    time.sleep(5)  # Wait before next poll
```

---

## Error Codes

| HTTP | Code | Meaning |
|------|------|---------|
| 401 | `INVALID_API_KEY` | Check auth header |
| 403 | `UNAUTHORIZED_MODEL` | Wrong API key |
| 404 | `JOB_NOT_FOUND` | Invalid job_id |
| 429 | `RATE_LIMIT_EXCEEDED` | Slow down |
| 500 | `INTERNAL_ERROR` | Contact support |

---

## Best Practices

✅ **DO:**
- Use exponential backoff when polling
- Store API keys in environment variables
- Include metrics (runtime, memory)
- Validate outputs before submitting
- Use HTTPS for all requests
- Handle errors gracefully

❌ **DON'T:**
- Poll aggressively (<5s intervals)
- Hardcode API keys
- Submit invalid formats
- Ignore timeouts
- Skip error handling

---

## Links

- 📖 [Full API Reference](USER_MODELS_API_REFERENCE.md)
- 🚀 [Quick Start Guide](USER_MODELS_README.md)
- 💾 [Database Schema](user_models_schema.sql)
- 📝 [Example Worker](../examples/user_model_worker_example.py)
