# User Models API Reference

**Complete reference for user-defined models and the Bridge API**

---

## Table of Contents
1. [Overview](#overview)
2. [Output Formats](#output-formats)
3. [API Endpoints](#api-endpoints)
4. [Request/Response Examples](#requestresponse-examples)
5. [Error Handling](#error-handling)
6. [Best Practices](#best-practices)

---

## Overview

The User Models system allows you to define custom computational models and integrate them with Phonos using the Bridge API. Your worker polls for jobs, processes them on your infrastructure, and returns results in various formats.

### Key Features
- **Flexible Output Formats**: JSON, PDB, CIF, SDF, MOL2, and custom formats
- **Secure Authentication**: Bearer token authentication with API keys
- **Job Management**: Poll for jobs, submit results, handle failures
- **Encryption**: Automatic encryption/decryption of job data
- **Monitoring**: Track usage, runtime metrics, and errors

---

## Output Formats

### Supported Formats

The `output_format` field in your model definition determines how results are returned and displayed.

| Format | Extension | Use Case | Content Type | Display Method |
|--------|-----------|----------|--------------|----------------|
| `json` | `.json` | Generic structured data | `application/json` | JSON viewer, charts |
| `pdb` | `.pdb` | Protein structures | `chemical/x-pdb` | 3D molecular viewer |
| `cif` | `.cif` | Crystallographic data | `chemical/x-cif` | 3D molecular viewer |
| `mmcif` | `.mmcif` | Macromolecular structures | `chemical/x-mmcif` | 3D molecular viewer |
| `sdf` | `.sdf` | Small molecules | `chemical/x-mdl-sdfile` | 3D molecular viewer |
| `mol2` | `.mol2` | Molecular structures | `chemical/x-mol2` | 3D molecular viewer |
| `mol` | `.mol` | Chemical structures | `chemical/x-mdl-molfile` | 3D molecular viewer |
| `txt` | `.txt` | Plain text | `text/plain` | Text viewer |
| `csv` | `.csv` | Tabular data | `text/csv` | Table viewer |
| `png` | `.png` | Images | `image/png` | Image viewer |
| `pdf` | `.pdf` | Documents | `application/pdf` | PDF viewer |
| `zip` | `.zip` | Archives | `application/zip` | Download link |

### Format Selection

Choose your output format when creating a model:

```json
{
  "model_name": "Protein Folder",
  "output_format": "pdb",
  "input_schema": [ ... ]
}
```

Or specify it dynamically in the job result:

```json
{
  "status": "completed",
  "result": {
    "output_format": "cif",  // Override model default
    "data": "..."
  }
}
```

---

## API Endpoints

### Base URL
```
Production: https://phonos.bio/api/bridge/v1
Development: http://localhost:5000/api/bridge/v1
```

### Authentication
All Bridge API endpoints require authentication via Bearer token:

```http
Authorization: Bearer pk_abc123...
```

---

### 1. Health Check

**Endpoint:** `GET /health`

**Description:** Check if the Bridge API is operational

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0",
  "timestamp": "2026-01-06T12:00:00Z"
}
```

---

### 2. Worker Info

**Endpoint:** `GET /worker/info`

**Description:** Verify your API key and check authorization scope

**Response:**
```json
{
  "status": "success",
  "worker_info": {
    "key_id": "pk_abc123",
    "user_id": 42,
    "authorized_models": [123, 456],
    "authorized_projects": [789],
    "rate_limit": 60,
    "permissions": ["poll_jobs", "submit_results"]
  }
}
```

**Error Response (401 Unauthorized):**
```json
{
  "status": "error",
  "message": "Invalid or missing API key"
}
```

---

### 3. Poll for Jobs

**Endpoint:** `POST /jobs/poll`

**Description:** Retrieve pending jobs from the queue

**Request Body:**
```json
{
  "model_id": 123,           // Optional: filter by specific model
  "project_id": 456,         // Optional: filter by project
  "worker_id": "worker-gpu-1", // Optional: identifier for your worker
  "max_jobs": 5              // Number of jobs to fetch (max: 10)
}
```

**Response (Success):**
```json
{
  "status": "success",
  "jobs": [
    {
      "job_id": "550e8400-e29b-41d4-a716-446655440000",
      "model_id": 123,
      "input_data": {
        "sequence": "MKTAYIAKQRQISFVKSHF",
        "temperature": 0.7
      },
      "custom_params": {
        "use_gpu": true,
        "num_recycles": 3
      },
      "timeout_seconds": 3600,
      "output_format": "pdb"
    }
  ],
  "count": 1
}
```

**Response (No Jobs):**
```json
{
  "status": "success",
  "jobs": [],
  "count": 0
}
```

**Error Response (403 Forbidden):**
```json
{
  "status": "error",
  "message": "API key not authorized for this model"
}
```

---

### 4. Submit Job Result

**Endpoint:** `POST /jobs/<job_id>/result`

**Description:** Submit results for a completed or failed job

#### 4.1 Success Result (JSON Format)

**Request:**
```json
{
  "status": "completed",
  "result": {
    "predictions": [
      {"class": "positive", "confidence": 0.95},
      {"class": "negative", "confidence": 0.05}
    ],
    "metadata": {
      "model_version": "v2.0",
      "processing_time": 4.2
    }
  },
  "metrics": {
    "runtime_seconds": 4.5,
    "memory_used_mb": 512,
    "gpu_utilization": 0.85
  }
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Result submitted successfully",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

#### 4.2 Success Result (PDB Format)

**Request:**
```json
{
  "status": "completed",
  "result": {
    "structure_pdb": "HEADER    PREDICTED STRUCTURE\nATOM      1  N   MET A   1      10.000  10.000  10.000...",
    "confidence_scores": {
      "plddt": [95.2, 94.8, 93.5, ...]
    }
  },
  "output_format": "pdb",
  "metrics": {
    "runtime_seconds": 45.2,
    "memory_used_mb": 2048
  }
}
```

#### 4.3 Success Result (Binary File)

For binary files (images, archives), use base64 encoding:

**Request:**
```json
{
  "status": "completed",
  "result": {
    "output_file_base64": "iVBORw0KGgoAAAANSUhEUgAA...",
    "filename": "result.png",
    "description": "Protein structure visualization"
  },
  "output_format": "png",
  "metrics": {
    "runtime_seconds": 12.3
  }
}
```

#### 4.4 Success Result (Multiple Files)

**Request:**
```json
{
  "status": "completed",
  "result": {
    "primary_output": "structure_data_here",
    "artifacts": [
      {
        "filename": "rank_1.pdb",
        "content": "ATOM...",
        "type": "pdb"
      },
      {
        "filename": "confidence.json",
        "content": "{\"plddt\": [...]}",
        "type": "json"
      }
    ]
  },
  "output_format": "pdb",
  "metrics": {
    "runtime_seconds": 67.8
  }
}
```

#### 4.5 Failure Result

**Request:**
```json
{
  "status": "failed",
  "error_message": "CUDA out of memory: tried to allocate 2.00 GiB",
  "error_type": "RuntimeError",
  "traceback": "Traceback (most recent call last):\n  File ...",
  "retry_recommended": false,
  "metrics": {
    "runtime_seconds": 5.2,
    "memory_used_mb": 8192
  }
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Failure status recorded",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## Request/Response Examples

### Example 1: Simple Text Processing Model

**Model Definition:**
```json
{
  "model_name": "Text Summarizer",
  "model_type": "custom",
  "output_format": "json",
  "input_schema": [
    {
      "name": "text",
      "type": "textarea",
      "label": "Input Text",
      "required": true
    },
    {
      "name": "max_length",
      "type": "number",
      "label": "Maximum Summary Length",
      "default": 100
    }
  ]
}
```

**Job Polling Response:**
```json
{
  "status": "success",
  "jobs": [
    {
      "job_id": "abc-123",
      "model_id": 10,
      "input_data": {
        "text": "Long article text here...",
        "max_length": 150
      },
      "output_format": "json"
    }
  ]
}
```

**Result Submission:**
```json
{
  "status": "completed",
  "result": {
    "summary": "Concise summary of the article...",
    "word_count": 145,
    "keywords": ["science", "research", "discovery"]
  },
  "metrics": {
    "runtime_seconds": 2.3,
    "tokens_processed": 5420
  }
}
```

---

### Example 2: Protein Structure Prediction

**Model Definition:**
```json
{
  "model_name": "AlphaFold Runner",
  "model_type": "protein_folding",
  "output_format": "pdb",
  "input_schema": [
    {
      "name": "sequence",
      "type": "textarea",
      "label": "Protein Sequence",
      "required": true,
      "validation": "^[ACDEFGHIKLMNPQRSTVWY]+$"
    },
    {
      "name": "num_models",
      "type": "number",
      "label": "Number of Models",
      "default": 5,
      "min": 1,
      "max": 5
    }
  ]
}
```

**Job Polling Response:**
```json
{
  "status": "success",
  "jobs": [
    {
      "job_id": "def-456",
      "model_id": 20,
      "input_data": {
        "sequence": "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEK",
        "num_models": 5
      },
      "custom_params": {
        "use_templates": false,
        "max_recycles": 3
      },
      "timeout_seconds": 7200,
      "output_format": "pdb"
    }
  ]
}
```

**Result Submission:**
```json
{
  "status": "completed",
  "result": {
    "structure_pdb": "HEADER    ALPHAFOLD MONOMER V2.0 PREDICTION\nTITLE     ALPHAFOLD MONOMER V2.0 PREDICTION FOR UNKNOWN PROTEIN\nREMARK   1\nREMARK   1 REFERENCE 1\nREMARK   1  AUTH   JUMPER,EVANS,PRITZEL...\nATOM      1  N   MET A   1      12.345  23.456  34.567  1.00 95.23           N\nATOM      2  CA  MET A   1      13.456  24.567  35.678  1.00 94.87           C\n...",
    "confidence_scores": {
      "plddt": [95.23, 94.87, 93.45, ...],
      "mean_plddt": 92.5,
      "ptm": 0.87
    },
    "ranked_models": [
      {
        "rank": 1,
        "model_name": "model_1",
        "plddt": 92.5,
        "filename": "ranked_0.pdb"
      }
    ]
  },
  "output_format": "pdb",
  "metrics": {
    "runtime_seconds": 245.7,
    "memory_used_mb": 4096,
    "gpu_utilization": 0.92
  }
}
```

---

### Example 3: Image Generation Model

**Model Definition:**
```json
{
  "model_name": "Protein Visualizer",
  "model_type": "visualization",
  "output_format": "png",
  "input_schema": [
    {
      "name": "pdb_file",
      "type": "file",
      "label": "PDB Structure",
      "required": true,
      "accept": ".pdb"
    },
    {
      "name": "view_angle",
      "type": "select",
      "label": "View Angle",
      "options": ["front", "top", "side"],
      "default": "front"
    }
  ]
}
```

**Job Polling Response:**
```json
{
  "status": "success",
  "jobs": [
    {
      "job_id": "ghi-789",
      "model_id": 30,
      "input_data": {
        "pdb_file": "ATOM...",  // PDB content
        "view_angle": "front"
      },
      "output_format": "png"
    }
  ]
}
```

**Result Submission (Base64 Encoded):**
```json
{
  "status": "completed",
  "result": {
    "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAUA...",
    "width": 1920,
    "height": 1080,
    "dpi": 300
  },
  "output_format": "png",
  "metrics": {
    "runtime_seconds": 8.4,
    "rendering_engine": "PyMOL"
  }
}
```

---

### Example 4: Molecular Docking

**Model Definition:**
```json
{
  "model_name": "DiffDock Custom",
  "model_type": "docking",
  "output_format": "sdf",
  "input_schema": [
    {
      "name": "protein_pdb",
      "type": "file",
      "label": "Protein Structure",
      "required": true
    },
    {
      "name": "ligand_smiles",
      "type": "text",
      "label": "Ligand SMILES",
      "required": true
    },
    {
      "name": "num_poses",
      "type": "number",
      "label": "Number of Poses",
      "default": 10
    }
  ]
}
```

**Result Submission (Multiple Poses):**
```json
{
  "status": "completed",
  "result": {
    "top_pose": "SDF_DATA_FOR_RANK_1...",
    "poses": [
      {
        "rank": 1,
        "score": -8.5,
        "sdf_data": "SDF_CONTENT...",
        "confidence": 0.95
      },
      {
        "rank": 2,
        "score": -7.8,
        "sdf_data": "SDF_CONTENT...",
        "confidence": 0.87
      }
    ],
    "binding_site": {
      "residues": ["ASP123", "TYR456"],
      "center": [12.3, 45.6, 78.9]
    }
  },
  "output_format": "sdf",
  "metrics": {
    "runtime_seconds": 120.5,
    "total_samples": 1000,
    "mean_confidence": 0.78
  }
}
```

---

## Error Handling

### Error Response Format

All errors follow a consistent format:

```json
{
  "status": "error",
  "message": "Human-readable error message",
  "error_code": "ERROR_CODE",  // Optional
  "details": {  // Optional additional context
    "field": "value"
  }
}
```

### Common Error Codes

| HTTP Status | Error Code | Description | Solution |
|-------------|------------|-------------|----------|
| 401 | `INVALID_API_KEY` | API key is invalid or missing | Check Authorization header |
| 403 | `UNAUTHORIZED_MODEL` | API key not authorized for this model | Use correct API key |
| 403 | `UNAUTHORIZED_PROJECT` | API key not authorized for this project | Check project permissions |
| 404 | `JOB_NOT_FOUND` | Job ID doesn't exist | Verify job_id is correct |
| 400 | `INVALID_REQUEST` | Request body is malformed | Check JSON syntax |
| 400 | `MISSING_REQUIRED_FIELD` | Required field is missing | Include all required fields |
| 409 | `JOB_ALREADY_COMPLETED` | Result already submitted | Check job status |
| 429 | `RATE_LIMIT_EXCEEDED` | Too many requests | Slow down polling frequency |
| 500 | `INTERNAL_ERROR` | Server error | Contact support |
| 503 | `SERVICE_UNAVAILABLE` | Service temporarily down | Retry later |

### Error Handling Examples

#### Invalid API Key
**Request:**
```http
GET /api/bridge/v1/worker/info
Authorization: Bearer invalid_key
```

**Response (401):**
```json
{
  "status": "error",
  "message": "Invalid or missing API key",
  "error_code": "INVALID_API_KEY"
}
```

#### Job Not Found
**Request:**
```http
POST /api/bridge/v1/jobs/nonexistent-job-id/result
```

**Response (404):**
```json
{
  "status": "error",
  "message": "Job not found in external queue",
  "error_code": "JOB_NOT_FOUND",
  "details": {
    "job_id": "nonexistent-job-id"
  }
}
```

#### Rate Limit Exceeded
**Response (429):**
```json
{
  "status": "error",
  "message": "Rate limit exceeded",
  "error_code": "RATE_LIMIT_EXCEEDED",
  "details": {
    "limit": 60,
    "window": "1 minute",
    "retry_after_seconds": 45
  }
}
```

---

## Best Practices

### 1. Polling Strategy

**Good Practice:**
```python
import time
import requests

def poll_with_exponential_backoff():
    base_delay = 5
    max_delay = 60
    current_delay = base_delay

    while True:
        jobs = poll_for_jobs()

        if jobs:
            # Jobs available, process them
            for job in jobs:
                process_job(job)
            current_delay = base_delay  # Reset delay
        else:
            # No jobs, wait with backoff
            time.sleep(current_delay)
            current_delay = min(current_delay * 1.5, max_delay)
```

**Bad Practice:**
```python
while True:
    poll_for_jobs()  # Aggressive polling wastes resources
```

### 2. Error Recovery

**Good Practice:**
```python
def submit_result_with_retry(job_id, result, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = submit_result(job_id, result)
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                log_error(f"Failed after {max_retries} attempts: {e}")
                return False
            time.sleep(2 ** attempt)  # Exponential backoff
    return False
```

### 3. Result Validation

**Good Practice:**
```python
def validate_and_submit(job_id, result):
    # Validate output format matches model definition
    if not validate_output_format(result):
        submit_failure(job_id, "Invalid output format")
        return

    # Check file size limits
    if get_result_size(result) > MAX_SIZE:
        submit_failure(job_id, "Result too large")
        return

    submit_result(job_id, result)
```

### 4. Metrics Tracking

**Good Practice:**
```python
import time
import psutil

def track_job_execution(job_function):
    start_time = time.time()
    start_memory = psutil.virtual_memory().used / (1024 ** 2)

    result = job_function()

    end_time = time.time()
    end_memory = psutil.virtual_memory().used / (1024 ** 2)

    metrics = {
        "runtime_seconds": end_time - start_time,
        "memory_used_mb": max(0, end_memory - start_memory),
        "cpu_percent": psutil.cpu_percent(),
        "timestamp": datetime.now().isoformat()
    }

    return result, metrics
```

### 5. Security

**Good Practices:**
- Store API keys in environment variables, never in code
- Use HTTPS for all API requests
- Rotate API keys regularly
- Implement IP whitelisting if possible
- Log all API requests for audit trail

**Example:**
```python
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv('PHONOS_API_KEY')
BASE_URL = os.getenv('PHONOS_API_URL', 'https://phonos.bio')

# Never do this:
# API_KEY = "pk_abc123..."  # ❌ Hardcoded secret
```

### 6. Graceful Shutdown

**Good Practice:**
```python
import signal
import sys

shutdown_requested = False

def signal_handler(sig, frame):
    global shutdown_requested
    print("Shutdown requested, finishing current job...")
    shutdown_requested = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def main_loop():
    while not shutdown_requested:
        jobs = poll_for_jobs()
        for job in jobs:
            if shutdown_requested:
                # Re-queue job for another worker
                requeue_job(job)
                break
            process_job(job)
```

---

## Additional Resources

- **Example Worker:** `/examples/user_model_worker_example.py`
- **Connection Tester:** `/examples/verify_user_model_connection.py`
- **Schema:** `/production/user_models_schema.sql`
- **Quick Start:** `/production/USER_MODELS_README.md`

---

## Support

For issues or questions:
- Check logs: `sudo journalctl -u phonos -f`
- Review database: `psql -U user -d phonos_db`
- Test connection: `python examples/verify_user_model_connection.py`

**API Version:** v1
**Last Updated:** 2026-01-06
