# infrastructure/ — deploy & ops (target home)

Target home for everything that provisions, deploys, and supervises the system.
Being consolidated here incrementally.

## Already here / to move

| Concern | Lives today at | Notes |
|---|---|---|
| Worker k8s + docker manifests, deploy scripts | `deploy/` | Move to `infrastructure/deploy/` once the k8s `secret.yaml` handling is finalized (it is currently untracked but the path is referenced in `.gitignore`). |
| Web-app systemd unit | `/etc/systemd/system/phonos.service` (not in repo; a `.bak` exists in `$HOME`) | Should be tracked here as `infrastructure/systemd/phonos.service` so the deploy config is version-controlled. |
| Worker/ancillary systemd units | `phonos-new-workers.service`, `production/cleanup-stuck-jobs.service`, `phonos_watchdog/units/*` | Consolidate under `infrastructure/systemd/`. |
| Gunicorn config | `gunicorn_config.py` | Keep at the app root while the app root is the CWD; track a copy/symlink here. |
| Compose (notebook/wolfram build) | `docker-compose.notebook.yml` | Build contexts now point at `services/`. |

## Supervision

The self-healing supervisor lives at `phonos_watchdog/` (systemd:
`phonos-watchdog.service`). It health-checks Redis/Postgres/HTTP/MRS/workers and
restarts them via `systemctl`. Its unit files under `phonos_watchdog/units/`
are candidates to move here.

See [`docs/architecture/06-migration-strategy.md`](../docs/architecture/06-migration-strategy.md)
for sequencing. Nothing load-bearing has been physically relocated yet to avoid
disrupting the live deploy.
