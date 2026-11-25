# apps/ — deployable applications (target home)

This directory is the **target** home for the two long-running applications in
the system. It is currently a **scaffold**: the code still lives at the repo
root because both are wired into a live production deployment whose paths are
baked into systemd units, `PYTHONPATH`, nginx, and `wsgi.py`. Moving them is a
*coordinated deploy change*, not a file move — see
[`docs/architecture/06-migration-strategy.md`](../docs/architecture/06-migration-strategy.md).

| App (target) | Lives today at | What it is |
|---|---|---|
| `apps/main-app` | `phonos_app/`, `production/`, `static/`, `templates/`, `wsgi.py`, `gunicorn_config.py` | The Flask + Socket.IO web application served by gunicorn (eventlet). |
| `apps/worker`   | `phonos_worker.py`, `production/*_worker.py`, `production/mrs*.py`, `node1-esm/`, `diffdock/` | The job/model workers and the MRS router that dispatch and execute compute jobs via Redis. |

## Why the core is not moved yet

`phonos_app/app.py` derives `template_folder`/`static_folder` from
`PROJECT_ROOT = dirname(dirname(app.py))` (the repo root today). The live
systemd unit sets `WorkingDirectory=/…/phonos`,
`PYTHONPATH=/…/phonos:/…/phonos/production`, and launches `wsgi:application`.
Relocating the tree requires updating all of those **and** verifying nginx's
`/static` handling — a change that must land in a maintenance window with a boot
smoke-test, not a blind push. The migration doc gives the exact, ordered recipe.
