# 1. Proposed repository structure

## Context

The repo grew organically into ~694 tracked files with several overlapping
concerns at the root: the web app (`phonos_app/`), the backend/data layer and
workers (`production/`), the served frontend (`static/`, `templates/`), three
dockerized services (`production/docker/*`), two standalone client packages
(`pypi/`, `cli_tool/`), deploy manifests (`deploy/`), and ~15 loose markdown/
scratch files at the root. There is no separation-of-concerns boundary a new
contributor can rely on, and two legacy mirror trees (`phonos-main/`,
`production/node3-diffdock.py`) actively mislead.

The target below adopts the conventional monorepo shape (`apps/`, `services/`,
`packages/`, `infrastructure/`, `docs/`) adapted to a Python/Flask reality
(there is no JS build system, so no `packages/ui` / `packages/types`).

## Target layout

```
phonos/
├── apps/
│   ├── main-app/              # the Flask + Socket.IO web app (gunicorn)
│   │   ├── phonos_app/        #   app factory, routes/, helpers/, socket handlers
│   │   ├── static/            #   served JS/CSS/img
│   │   ├── templates/         #   Jinja templates
│   │   ├── wsgi.py            #   gunicorn entrypoint
│   │   └── gunicorn_config.py
│   └── worker/                # job/model workers + MRS router
│       ├── mrs/               #   production/mrs*.py
│       ├── workers/           #   production/*_worker.py, node1-esm/, diffdock/
│       └── phonos_worker.py   #   user-hosted bridge worker
│
├── services/                  # ✅ DONE — independently-deployable containers
│   ├── notebook-kernel/
│   ├── wolfram-service/
│   └── hpc-worker/
│
├── packages/                  # ✅ DONE — standalone, publishable, non-runtime
│   ├── sdk/                   #   phonos_sdk  (was pypi/)
│   └── cli/                   #   phonos_cli  (was cli_tool/)
│
├── platform/                  # shared backend libraries (extract from production/)
│   ├── db/                    #   schema + both connection pools
│   ├── config/                #   Config + SUPPORTED_MODELS catalog
│   └── domain/                #   models, sequence_utils, structure_analysis, paths
│
├── infrastructure/            # 🚧 SCAFFOLD — deploy & ops
│   ├── deploy/                #   (was deploy/)  k8s + worker docker + scripts
│   └── systemd/               #   phonos.service, worker units, watchdog units
│
├── docs/                      # ✅ consolidated docs (this review lives here)
│   └── architecture/
└── scripts/                   # ✅ maintenance/one-off scripts (was root scatter)
```

Legend: ✅ done in this pass · 🚧 scaffolded (README + plan, code not yet moved).

## Mapping: today → target

| Today | Target | Status |
|---|---|---|
| `pypi/` | `packages/sdk/` | ✅ moved |
| `cli_tool/` | `packages/cli/` | ✅ moved |
| `production/docker/notebook-kernel` | `services/notebook-kernel/` | ✅ moved |
| `production/docker/wolfram-service` | `services/wolfram-service/` | ✅ moved |
| `production/docker/hpc-worker` | `services/hpc-worker/` | ✅ moved |
| `scan_i18n.py`, `update_user_credits.py`, `export_docs_to_obsidian.py` (root) | `scripts/` | ✅ moved |
| `AD.md`, `agend.md`, `PERMISSION_AND_SECURITY_FIXES.md` (root) | `docs/` | ✅ moved |
| `phonos-main/` (empty mirror) | — | ✅ deleted |
| `test_files/test.smi` | — | ✅ deleted |
| `phonos_app/` + `static/` + `templates/` + `wsgi.py` | `apps/main-app/` | 🚧 staged (see doc 6) |
| `production/*_worker.py`, `node1-esm/`, `diffdock/` | `apps/worker/` | 🚧 staged |
| `production/database.py`, `production/config.py`, `helpers/*` shared | `platform/` | 🚧 staged (see doc 4) |
| `deploy/` | `infrastructure/deploy/` | 🚧 staged |
| `/etc/systemd/system/phonos.service` | `infrastructure/systemd/` | 🚧 track it |

## Naming & ownership conventions

- **One responsibility per top-level dir.** `apps/*` run; `services/*` are
  containers with a network contract; `packages/*` are published; `platform/*`
  is imported shared code; `infrastructure/*` provisions; `docs/*` explains.
- **Route modules** stay `api_<domain>.py` but migrate from flat `@app.route`
  registration to Flask **Blueprints** named `<domain>_bp` (see doc 4). The
  notebook API already does this (`notebook_bp`) — use it as the template.
- **Ownership boundaries** are the Blueprint/package folders. A `CODEOWNERS`
  file should map each `apps/main-app/phonos_app/routes/<domain>` and each
  `services/*` to an owner once the team grows.
- **Tests** mirror the source tree (`tests/<domain>/`) instead of one 2.6k-line
  `test_main.py`.
