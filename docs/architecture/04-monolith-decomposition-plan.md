# 4. Monolith decomposition plan

## Context

Two monoliths dominate: **`static/js/app.js` (50,417 LOC)** on the frontend and
the **global-`app` route layer** on the backend, plus a handful of 2–5k-line
files. None can be split by moving lines alone — they are held together by
inline `onclick` handlers (frontend) and a single shared `app` object (backend).
This plan gives the *seams* and a chokepoint-first sequence.

---

## 4.1 Backend: from global `app` to Blueprints

**Problem.** ~50 route modules do `@app.route(...)` against one global `app`
imported from `phonos_app.app`. Import order matters, circular imports are
worked around with `importlib` and function-level imports, and `public_api.py`
had to be commented out to avoid duplicate registration. This is the root
coupling.

**Target.** Each domain becomes a Flask **Blueprint** (`<domain>_bp`) registered
by an explicit factory. `api_notebooks.py` already proves the pattern
(`notebook_bp`, `register_notebook_routes(app)`).

**Approach (incremental, no big bang):**
1. Introduce a real `create_app()` that returns a fresh app and calls a list of
   `register_*` functions — the aggregation that today lives as import
   side-effects in `routes/__init__.py`.
2. Convert modules **one at a time**: wrap each `api_<domain>.py`'s routes in a
   `<domain>_bp = Blueprint(...)`, replace `@app.route` → `@<domain>_bp.route`,
   export `register_<domain>_routes(app)`. Keep the old star-import working via a
   shim until all are converted.
3. Delete the `importlib` gymnastics once no route registers at import time.
4. Retire `public_api.py` after confirming `/v1/*` has no live consumers.

**Sequencing** (lowest-risk first): `api_tags`, `api_activity`, `api_tutorial`
(small, few deps) → `api_search`, `api_analytics` → the big ones last.

---

## 4.2 `api_jobs.py` (4,832 LOC, only 13 routes → mostly two mega-functions)

| Seam | Lines (approx) | Extract to |
|---|---|---|
| `view_output(job_id)` per-model output rendering (`_extract_plddt_from_mmcif`, openfold3/proteinmpnn/boltz branches) | 1259–3151 (~1.9k) | `api_job_outputs.py` + `helpers/output_renderers/<model>.py` |
| `_handle_job_submission()` validation + per-model-type dispatch (`_extract_multimer_sequences`, `_looks_like_pdb`, `_resolve_sequence_molecule_type`, `_load_sequence_from_file`, type-0/2/3/5/12/openfold3 branches) | 3151–4783 (~1.6k) | `api_job_submission.py` + `helpers/job_dispatch/<model>.py` |
| project archive/upload/download (`archive_project`, `download_project`, `upload_project`) | — | belongs with `api_projects.py` |
| batch submit (`submit_batch_jobs`) | — | `api_batches.py` (already a module) |
| job lifecycle (`get_job_logs`, `retry_job`, `duplicate_job_config`, `delete_job`, `estimate_job_cost`) | — | keep as slim `api_jobs.py` |

The two mega-functions are the highest-value backend refactor: a per-model
**registry** (`RENDERERS[model_type]`, `DISPATCHERS[model_type]`) replaces the
giant `if/elif` ladders and makes adding a model a one-file change.

---

## 4.3 `api_notebooks.py` (3,624 LOC, Blueprint, 46 routes)

Split the Blueprint into sub-modules mounted under the same `url_prefix`:
- **kernel/session** — `/kernel/*`, `/sessions*` (lifecycle, interrupt, restart)
- **notebook files** — `/<id>/files`, `/directory`, `/rename`, `/bulk-*`
- **config** — templates, runtimes, preferences, executions
- **wolfram** — `/wolfram/credentials|evaluate|bridge` (self-contained block)

---

## 4.4 `production/database.py` (3,013 LOC) and `production/config.py` (2,332 LOC)

- **database.py** mixes three concerns: the **background/worker pool**, the
  **schema/DDL** (`_create_schema` 1366–2842 + migrations), and the SQLite test
  compat. Split into `platform/db/pool.py`, `platform/db/schema.py`,
  `platform/db/migrations/`, `platform/db/sqlite_compat.py`. The request pool
  (`helpers/database.py`) stays but imports the shared schema.
- **config.py** is ~1.8k LOC of `SUPPORTED_MODELS` metadata. Extract the catalog
  to `platform/config/models_catalog.py` (or a versioned YAML/JSON loaded at
  boot) so `Config` is a thin settings object and the catalog can be edited
  without touching config logic.

---

## 4.5 Frontend: carving `app.js` (50,417 LOC)

**Hard constraint.** Hundreds of generated-HTML `onclick="App.foo()"` /
`Views.foo()` handlers hard-require `window.App` / `window.Views` to keep
existing global names. **Do not** move `App`/`Views` out of the global file
without rewriting every handler.

**What is safe to extract now** (each self-contained, <250 LOC, clear boundary):
`Toast`, `ToastEnhanced`, `Modal`, `ConfirmDialog`, `API`, `ClipboardHelper`,
and the dead `Drawer` from `app-shell.js`. Extract these into a small set of
`static/js/ui/*.js` files loaded before `app.js`, keeping the same `window.*`
globals. This is the beachhead for the UX refactor (doc 5).

**What stays for now:** the `Views` (~28.9k LOC) and `App` (~15.2k LOC)
god-objects. Reduce them opportunistically by (a) moving each feature's
render/handlers into the already-separate per-feature files
(`boltz2.js`, `openfold3.js`, `databases.js`, `flows_module.js`, …) that already
exist, and (b) replacing inline `onclick` with delegated event listeners so the
global names can eventually be dropped. Treat this as a long-tail effort, not a
milestone gate.

**Ordering principle:** extract the *shared primitives* first (they unblock the
UX refactor and the toast/modal consolidation), then peel features off the edges
of `Views`/`App` into their existing per-feature modules.
