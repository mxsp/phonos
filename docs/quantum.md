# Quantum Settings — Reference

## Overview

Phonos provides a dedicated **Quantum Settings** section that lets you
configure your quantum execution environment without modifying notebook
code. You can toggle between a local simulator and real IBM quantum
hardware, choose credential sources, and select specific backends.

### Where to find it

| Surface | How to access |
|---------|---------------|
| User Account Settings | `/profile_settings` → **Quantum Settings** card |
| Notebook — Account panel | Right sidebar → user icon → **IBM Quantum Account** |
| Notebook — Settings panel | Left sidebar → gear icon → **Quantum** section |

### Architecture

```
Job Submitted
     │
     ▼
Check User Settings
     │
     ▼
Use Personal Credentials?
     │
 ┌───┴───┐
 │ Yes   │ No
 ▼       ▼
User     Platform
Key      Managed Key
 │        │
 └───┬────┘
     ▼
Select Mode
     │
 ┌───┴─────────┐
 │ Simulator   │
 │ Hardware    │
 └───┬─────────┘
     ▼
Execute Job
```

---

## Execution Mode

Controls where quantum workloads run.

| Mode | Description | When to use |
|------|-------------|-------------|
| **Simulator** | Local Aer simulator | Development, testing, learning. Instant startup, no queue, no hardware limits. |
| **Real Hardware** | Actual IBM quantum computer | Research, validation, production. Requires valid credentials and a reachable backend. |

### Simulator backends

- **Aer Simulator** — Local Qiskit Aer simulator (default)
- **Local Testing Mode** — Lightweight simulation for quick tests

### Hardware backends

- `ibm_fez` (127 qubits)
- `ibm_marrakesh` (127 qubits)
- `ibm_kingston` (127 qubits)

These rotate — the platform fetches the live list from IBM Cloud when
credentials are available.

---

## Credential Provider

Choose the source of your IBM Quantum credentials.

### Platform Managed (default)

The platform's own IBM Quantum API key is used. Configured server-side
in `.env` via `IBM_QUANTUM_API_KEY` and `IBM_QUANTUM_INSTANCE`. No user
action required.

```
User Jobs
     │
     ▼
Platform Quantum Account
     │
     ▼
IBM Quantum Runtime
```

### Personal IBM Quantum Credentials

Bring your own IBM Quantum API key. Stored AES-256-GCM encrypted at rest
and **never** exposed to the frontend, logs, or API responses.

**Security guarantees:**
- Never displayed after save (frontend shows `**************`)
- Never returned to frontend APIs (`GET /api/quantum/credentials` returns
  only `has_key: bool` + non-secret metadata)
- Encrypted at rest (AES-256-GCM, keyed by host `ENCRYPTION_KEY`)
- Encrypted in transit (TLS)
- Stored in per-user isolated DB rows

| Field | Required | Description |
|-------|----------|-------------|
| API Key | Yes | IBM Quantum API key from [IBM Quantum](https://quantum.ibm.com/) |
| Instance / CRN | No | IBM Cloud resource name (e.g. `crn:v1:bluemix:public:...`) |
| Preferred Backend | No | Specific backend to use (default: `automatic`) |

---

## Backend Selection

| Option | Description |
|--------|-------------|
| **Automatic** (default) | Least-busy backend is selected at job time |
| `ibm_fez` | 127-qubit IBM Quantum backend |
| `ibm_marrakesh` | 127-qubit IBM Quantum backend |
| `ibm_kingston` | 127-qubit IBM Quantum backend |

In **simulator** mode, `Automatic` resolves to `AerSimulator`.

---

## Session Configuration

Advanced settings for IBM Runtime sessions.

| Setting | Default | Range | Description |
|---------|---------|-------|-------------|
| Auto Start Session | Disabled | On/Off | Create a Runtime session lazily. Recommended: off (session created on first job). |
| Session Timeout | 60 s | 30–300 s | Maximum session idle time before automatic teardown. Platform recommendation: 60 s. |

---

## Runtime Status Panel

Displayed in the Quantum Settings card on the **Account Settings** page.

| Field | Description |
|-------|-------------|
| Execution Mode | Current mode (`Simulator` / `Real Hardware`) |
| Credential Source | `Platform Managed` / `Personal` / `None` |
| Backend | Selected backend (e.g. `Automatic`, `ibm_fez`) |
| Runtime Session | `Active` / `Inactive` |
| Time Remaining | Seconds left in the current session (if active) |

---

## Defaults

New users start with:

| Setting | Default |
|---------|---------|
| Execution Mode | `Simulator` |
| Credential Source | `Platform Managed` |
| Backend | `Automatic` |
| Auto Start Session | `Disabled` |
| Session Timeout | `60 s` |

These defaults minimise hardware usage while allowing immediate
experimentation.

---

## Security Requirements

The system must **never** expose:

- IBM API Keys
- Platform Credentials
- Secret Tokens
- Service Credentials

through:

- Notebook output
- Browser console (`console.log`)
- Frontend API responses
- Logs (all errors sanitised to remove credential-bearing text)
- Exceptions (wrapped with `_sanitize_message()`)
- Debug messages

### Credential Isolation

Each user's configuration is fully isolated:

```
User A              User B
 └ Quantum Settings └ Quantum Settings
  └ API Key (enc)    └ API Key (enc)
```

No shared personal credentials. `ON DELETE CASCADE` ensures deleted users'
secrets are purged.

---

## API Reference

All endpoints are under `/api/quantum/` and require authentication.

### Settings

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/quantum/settings` | Return settings + Runtime Status Panel data |
| `PUT` | `/api/quantum/settings` | Save execution mode, cred source, backend, session config |

**`PUT /api/quantum/settings` body:**

```json
{
  "execution_mode": "simulator|hardware",
  "credential_source": "platform|personal",
  "preferred_backend": "automatic|ibm_fez|ibm_marrakesh|ibm_kingston",
  "auto_start_session": false,
  "session_timeout_sec": 60
}
```

### Credentials

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/quantum/credentials` | Metadata only (`has_key`, instance, backend, timestamps) |
| `PUT` | `/api/quantum/credentials` | Save personal IBM API key (encrypted, never echoed) |
| `DELETE` | `/api/quantum/credentials` | Remove stored credentials |
| `POST` | `/api/quantum/credentials/verify` | Test connection against IBM Quantum Runtime |

**`PUT /api/quantum/credentials` body:**

```json
{
  "api_key": "your-ibm-quantum-api-key",
  "instance_crn": "crn:v1:bluemix:...",
  "preferred_backend": "ibm_fez"
}
```

**Response** (does not echo the key):

```json
{ "success": true }
```

### Status & Discovery

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/quantum/status` | Runtime Status Panel snapshot |
| `GET` | `/api/quantum/backends` | Available simulator + hardware backends |

---

## Getting Started

1. **Open the Hello World notebook** — `examples/quantum_hello_world.ipynb`
   (import via Notebook IDE → Import → select the file)

2. **Run on simulator** — the default settings run on the local Aer simulator.
   Just execute the cells.

3. **Switch to real hardware** — Open Quantum Settings, change Execution Mode
   to "Real Quantum Hardware", and save. Re-run the notebook.

4. **Bring your own key** — Switch Credential Provider to "Personal IBM
   Quantum Credentials" and enter your API key from
   [IBM Quantum](https://quantum.ibm.com/).
