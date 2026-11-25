# packages/

Self-contained, independently-versioned Python packages that are **not** part
of the web-app runtime. Each has its own `pyproject.toml`/`setup.py` and can be
built and published on its own.

| Package | Path | What it is |
|---|---|---|
| `phonos_sdk` | [`packages/sdk`](./sdk) | Public Python SDK used inside notebook kernels and by external integrators (quantum/HPC/jobs clients). Formerly `pypi/`. |
| `phonos_cli` | [`packages/cli`](./cli) | Command-line client (`cli.py`, `client.py`). Formerly `cli_tool/`. |

## Why they live here

Neither package is imported by `phonos_app/` or `production/` at runtime
(verified: `grep -rn "import pypi\|import cli_tool"` returns nothing in the app).
Keeping them out of the app tree makes the release boundary explicit and lets
them be pip-installed / published without dragging in the server.

## Working on them

Build/test each package from its own directory using its own manifest; they do
not participate in the server's `requirements.txt`.
