# 6. Migration strategy

Principle: **the running app must never break between commits.** Every step is
either additive, a `git mv` of non-runtime code, or a config change validated
before the deploy that depends on it.

## Phase 0 — done in this pass ✅
- Moved non-runtime trees (`packages/`, `services/`, `scripts/`, `docs/`).
- Untracked committed secrets + user data; hardened `.gitignore`.
- Deleted empty/dead artifacts.
- Scaffolded `apps/`, `infrastructure/`, `platform/` (READMEs + plan).
- Shipped the UX `Panel` foundation (additive, opt-in — no behavior change).
- Updated the two moved references (`docker-compose.notebook.yml` build
  contexts, `.gitignore` hpc-worker path).

## Phase 1 — secrets & safety (do first, no downtime)
1. Rotate `REDIS_PASSWORD` + `ENCRYPTION_KEY` and the GitHub PAT (doc 3.2).
2. Code change: split `private_key` out of the 11 `production/node_config_*.json`
   into gitignored `*.pem` loaded at runtime (mirror `node1-esm` node identity).
3. Optional: `git filter-repo` to purge the secret blobs from history; coordinate
   the force-push + collaborator re-clone.

## Phase 2 — backend modularization (additive, per module)
- Introduce `create_app()` + explicit `register_*` list (doc 4.1).
- Convert route modules to Blueprints **one at a time**, smallest first, keeping
  a shim so the old aggregator still works until the last one lands.
- Split `api_jobs.py` mega-functions behind per-model registries (doc 4.2).
- Extract `platform/db/*` and `platform/config/models_catalog.py` (doc 4.4),
  leaving thin re-export shims at the old import paths so nothing else changes.
- **Gate:** `pytest` green after each module; the request path is unchanged.

## Phase 3 — frontend modularization (additive)
- Extract shared primitives to `static/js/ui/*.js` loaded before `app.js`,
  preserving `window.*` globals (doc 4.5).
- Wire the UX `Panel` refactor with browser validation (doc 5 §A).
- Consolidate toasts (doc 5 §B).

## Phase 4 — relocate the live web core (the high-risk step) 🚧

This is the only step that touches deploy-critical paths. **Do it in a
maintenance window**, not a blind push.

Exact recipe:
1. `git mv phonos_app static templates wsgi.py gunicorn_config.py apps/main-app/`
   (and the top-level `config.py`/`database.py`/`encryption_util.py` shims).
2. `phonos_app/app.py`: `PROJECT_ROOT` currently = `dirname(dirname(app.py))`.
   After the move that still resolves to `apps/main-app/`, so **also move
   `static/` and `templates/` into `apps/main-app/`** (step 1 does) — no code
   change needed if they move together. Verify `static_folder`/`template_folder`
   resolve.
3. Update the **systemd unit** (`/etc/systemd/system/phonos.service`):
   `WorkingDirectory=/…/phonos/apps/main-app`,
   `PYTHONPATH=/…/phonos/apps/main-app:/…/phonos/apps/main-app/production` (and
   move/relink `production/` under `apps/main-app/` or keep it in `platform/`
   with updated `PYTHONPATH`). Track this unit in `infrastructure/systemd/`.
4. **nginx**: confirm whether it serves `/static` directly (`alias
   /…/phonos/static/`). If so, update the alias to the new path; if it proxies
   all to gunicorn, nothing to do. This is the one thing not visible in-repo —
   check `/etc/nginx/` before the window.
5. Update `start-phonos.sh` (drop the broken `phonos-main` indirection) and
   `deploy/deploy.sh` / `DEPLOY.md` references.
6. **Validate before restart:** run the test suite, then a boot smoke-test that
   imports the app under the test env (the existing `tests/conftest.py` provides
   an in-memory Redis + SQLite path, so `create_app()` can be imported without
   touching prod DB/Redis) and asserts the route map is non-empty.
7. `systemctl daemon-reload && systemctl restart phonos`; watch
   `logs/gunicorn_error.log`; roll back by reverting the unit + the `git mv`.

## Phase 5 — polish
Worker relocation to `apps/worker/` (update the two worker systemd units'
`WorkingDirectory`), large-binary handling (LFS), doc de-duplication, and
splitting `test_main.py` into `tests/<domain>/`.

## Rollback
Every phase is a single revert: Phase 0/2/3 are `git revert`; Phase 4 is
`git revert` of the `git mv` **plus** restoring the previous systemd unit and
nginx alias. Keep the pre-move unit file saved (a `.bak` already exists in
`$HOME`).
