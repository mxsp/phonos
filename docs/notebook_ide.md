# Notebook IDE Guide

This document describes the current Notebook IDE behavior and the APIs it depends on.

## Scope

The Notebook IDE is project-centric and includes:

- Notebooks
- Project files and job outputs
- Runtime (kernel) sessions
- Model catalog (platform + custom models)
- SDK documentation panel

IoT devices are intentionally excluded from the Notebook IDE explorer and model workflows.

## Frontend Components

Primary UI file:

- `static/js/notebook.js`

Main sections:

- Explorer panel: notebooks + project files + job outputs
- Models panel: `/api/notebooks/models`
- Docs panel: SDK quick references and model catalog
- Runtime panel: active sessions and controls

## Runtime / Kernel Behavior

### Session lifecycle

- `POST /api/notebooks/<id>/kernel/start` starts a new kernel or resumes an
  existing live session.
- Kernel status is tracked both in the database (`notebook_sessions`) and in
  an in-memory manager (`phonos_app/helpers/kernel_manager.py`).
- On reconnect after a server restart, the backend attempts to reattach to a
  still-running container and rebuild the ZMQ sockets.

### Execution queue

Code execution is serialized per session using a server-side lock so that
overlapping runs cannot update the wrong execution record. Each
`execute_request` is tagged with a unique `msg_id`; `execute_reply` updates
that exact row in `notebook_executions`.

### Output handling while UI is hidden

All output events (`notebook:output`, `notebook:execute_reply`) are buffered
per session (last 500 messages). When a client reconnects, buffered messages
are replayed so outputs produced while the tab was in the background or
refreshing are not lost.

### Wolfram Language runtime

- Wolfram cells use the persistent `/evaluate-session` endpoint of the
  Wolfram service, keyed by the notebook `session_id`.
- State (`=`, `:=`, `Module` definitions, etc.) persists across cells in the
  same session; prior cells are not replayed on every evaluation.
- The Wolfram kernel can be configured via `runtime_levels.config.kernel`
  (e.g. `wolframscript`) and `runtime_levels.config.service_url`.
- Interactive widgets (`Manipulate`, `Dynamic`) are rendered as static images
  by the service; full live interactivity is not supported in this version.

## Notebook APIs

### Project resources

- `GET /api/notebooks/project/<project_id>/resources`
- Returns notebook explorer data in a single response:
  - `notebooks[]`
  - `files[]` (uploads, outputs, artifacts)
  - `container_path` per file (for `/project/...` access inside kernel)

### Notebook model catalog

- `GET /api/notebooks/models?project_id=<id>`
- Returns:
  - `platform_models[]`
  - `custom_models[]`
  - `counts`

This route applies model visibility/override rules and project access checks.

### Notebook file management

- `GET /api/notebooks/<id>/files`
- `POST /api/notebooks/<id>/files`
- `POST /api/notebooks/<id>/files/move`
- `POST /api/notebooks/<id>/files/duplicate`
- `GET /api/notebooks/<id>/files/search?q=<query>&path=<path>`
- `GET /api/notebooks/<id>/files/exists?path=<path>`

All paths are validated to stay within the notebook workspace.

### Notebook history and management

- `GET /api/notebooks/<id>/history` — paginated commit list (`?limit=&offset=&action=&search=`)
- `POST /api/notebooks/<id>/history` — create a manual commit (checkpoint) with a message
- `GET /api/notebooks/<id>/history/<commit_hash>` — full cells snapshot for one commit
- `GET /api/notebooks/<id>/history/<commit_hash>/diff` — cell-level diff against its parent commit
- `GET /api/notebooks/<id>/history/<base_hash>/compare/<target_hash>` — diff between two arbitrary commits
- `GET /api/notebooks/<id>/history/<commit_hash>/ipynb` — export a commit as a `.ipynb` file
- `POST /api/notebooks/<id>/restore/<commit_hash>` — restore the notebook to a commit (creates a new `restore` commit; does not delete history)
- `GET /api/notebooks/<id>/sessions`
- `POST /api/notebooks/<id>/duplicate` — also seeds an initial `duplicate` history commit for the new notebook

### Import and file safety

- `POST /api/notebooks/import`
  - Requires `project_id`
  - Requires write access (`Project Manager` or `Project Contributor`)
- `POST /api/notebooks/<id>/files`
  - Rejects invalid traversal paths
- `POST /api/notebooks/<id>/project-files/copy`
  - Validates source and destination paths remain in allowed directories

## File Paths in Notebook Runtime

Files returned by the notebook resources API include `container_path`, which is the path to use from notebook code.

Examples:

- `/project/uploads/my-data.csv`
- `/project/jobs/1234/structure.pdb`

Use these paths directly in Python cells.

## Quantum Settings

A dedicated **Quantum Settings** section is available in both the Account
Settings page and the Notebook IDE. It controls the quantum execution
environment: simulator vs. real hardware, credential sources, backend
selection, and session configuration.

For the full reference see `docs/quantum.md`. Key API endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/quantum/settings` | Current settings + Runtime Status Panel data |
| `PUT /api/quantum/settings` | Save execution mode, cred source, backend, session config |
| `GET /api/quantum/credentials` | Credential metadata (never returns the API key) |
| `PUT /api/quantum/credentials` | Save personal IBM Quantum API key (encrypted at rest) |
| `DELETE /api/quantum/credentials` | Remove stored credentials |
| `POST /api/quantum/credentials/verify` | Test connection against IBM Quantum Runtime |
| `GET /api/quantum/backends` | Available simulator + hardware backends |

### Security

- API keys are AES-256-GCM encrypted at rest
- Keys are **never** returned from any API endpoint
- Log messages are sanitised to remove credential-bearing text
- Errors are wrapped with `_sanitize_message()` before reaching the frontend

### Example notebook

`examples/quantum_hello_world.ipynb` demonstrates a Bell-state circuit
running on the simulator, with instructions for switching to real hardware.

## Validation Checklist

Before shipping changes:

1. `ENCRYPTION_KEY="a-secure-test-key-for-dev-use-32" pytest tests/test_main.py -v`
2. `flake8 phonos_app/ production/ tests/ --select=E999,F821,F822 --exclude=venv,node_modules,.git,__pycache__`
