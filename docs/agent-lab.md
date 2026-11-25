# Phonos Copilot — architecture and tool catalog

Phonos Copilot is the Phonos equivalent of the **Gimel Agent Lab**: a
context-aware assistant that reads the live application through a typed tool
catalog and streams its answer into the UI. It was built after studying the
Gimel implementation; this document records what that implementation does, what
was reused, and how the Phonos version is wired.

## 1. What the Gimel Agent Lab does

The lab (`~/Desktop/Gimel/agentlab`) is a standalone FastAPI process on port
8008 that shares Gimel's accounts, roles and database. It is a plugin, not a
second application:

- `app.py` replaces Gimel's scheduler with an idle loop, registers the plugin,
  and mounts the built React UI at `/lab` and `/voice`.
- `router.py` exposes `/api/agent/*`; `runner.py` owns one assistant turn.
- `tools.py` (≈4,500 lines) is a registry of typed tools with roles, write
  flags, reversibility and a change log; `analysis_tools.py` and `twin_tools.py`
  add read-only analysis and factory-twin tools.
- `llm.py` is a small OpenAI-compatible streaming client with tool-call
  assembly; `providers.py` stores endpoints/keys and caches model lists.
- `page_context.py` sanitises what the browser reports (route, open entity,
  selection, view settings, a few visible facts). It is passed to the model as
  **data**, never instructions.
- Tool calls run in a loop up to a configured step limit. Every write is
  recorded in `agent_changes` and can be reverted compare-and-set; inventory
  and price reversals write compensating history rows instead of deleting.
- SSE events: `start`, `delta`, `reasoning`, `assistant`, `tool_call`,
  `tool_result`, `error`, `done`.
- The UI is a React pane with chat, tool catalog/testing, changes, stats,
  prompts and settings. The main portal embeds the same chat as a right-hand
  sidebar (Ctrl+Shift+A).

Key design rules worth keeping:

1. Model-facing tool descriptions are English; user-facing summaries are the
   product language.
2. Internal facts come only from tools; the model is told not to invent ids,
   prices, counts or statuses.
3. Web pages, file contents, tool results and page context are untrusted data.
4. Writes are explicit, role-checked, logged and reversible where possible.
5. The provider is OpenAI-compatible, so Gemini, OpenCode Go, OpenAI and local
   Ollama/LM Studio all work without a second client.

## 2. What Phonos Copilot reuses

Phonos is Flask + vanilla JS, so the transport and UI are different, but the
contract is the same.

| Gimel concept | Phonos implementation |
| --- | --- |
| FastAPI router | Flask blueprint in `phonos_app/agent/router.py` |
| SQLAlchemy models | additive `phonos_agent_*` tables, created lazily |
| OpenAI streaming client | stdlib `urllib` client in `agent/llm.py` |
| Tool registry | `agent/tools.py` + `agent/bio_tools.py` |
| Page context | `agent/page_context.py` + `static/js/agent-context.js` |
| SSE chat | `POST /api/agent/chats/<id>/messages` |
| Dedicated lab page | `/agent` (`templates/agent.html`) |
| Portal sidebar | right-hand panel mounted by `static/js/agent.js` |
| Change log | not needed yet: every domain tool is read-only; job submission is a separate, user-confirmed action |

The assistant is additive. The rest of Phonos never imports it. If route
registration fails, the application still starts.

## 3. Package layout

```
phonos_app/agent/
  __init__.py       register_agent_routes()
  db.py             tables, row helpers, independent DB connection for SSE
  settings.py       provider endpoint/key/model, env precedence, model cache
  page_context.py   sanitise and render the current view
  tools.py          core catalog: projects, jobs, files, notebooks, ...
  bio_tools.py      model-centric tools: schemas, validation, I/O, run drafts
  llm.py            OpenAI-compatible streaming + tool-call assembly
  runner.py         one turn: prompt, tool loop, persistence, SSE events
  store.py          chats, messages, history, rewind, usage statistics
  viewer_context.py 3D/sequence snapshots (Redis + memory fallback, per user, 1 h TTL)
  locks.py         cross-worker chat lock and cancel flag (Redis + memory fallback)
  router.py         page, JSON API, SSE, settings, tools, viewer snapshot, job confirmation
static/js/agent.js          panel + dedicated page, live voice
static/js/agent-context.js  page + 3D/sequence probe
static/css/agent.css        both surfaces
templates/agent.html        dedicated page
tests/test_agent_lab.py     schema, tools, context, SSE, tool loop
```

Tables (only these, all prefixed):

- `phonos_agent_chats`
- `phonos_agent_messages`
- `phonos_agent_settings`

No existing table is renamed or altered.

## 4. Request flow

1. The page reports context through `PhonosAgentContext.collect()`.
2. `POST /api/agent/chats/<id>/messages` sanitises it with
   `page_context.sanitize()`.
3. `runner.run_stream()` writes the user message, builds the system prompt
   (identity, user/plan, current view, rules, tool count) and streams the
   provider.
4. Each tool call is executed against the same user scope: project access is
   checked with `get_project_assignment`/team membership, and admin status is
   read from the session user.
5. Tool results are truncated, stored as `tool` messages, and fed back to the
   model. The loop stops at `max_steps` (default 8, cap 20).
6. The browser receives SSE events and renders text, tool cards, errors and
   client actions. A `confirm_job` action opens a modal; nothing is queued
   until the user clicks **Run job**.
7. Chat storage keeps provider-reported usage only; the Usage tab aggregates it
   without estimating prices.

The SSE handler uses a dedicated DB connection (`independent_db`) because the
request-scoped connection can be closed when the streaming response starts.
`stream_with_context` keeps the request context for user/chat data, while the
stream connection is closed when the generator ends.

## 5. Context awareness

### Live 3D / sequence snapshot

The Mol* viewer and the sequence panel are part of the context, not a separate
feature. `agent-context.js` reads `Views.molecularVisualizerState` (and
`App.projectVisState`), extracts the current structure text, per-chain
sequences, selected residues, ligand and analysis, and posts the snapshot to
`PUT /api/agent/viewer-context`. The snapshot is per user, capped at 8 MB,
stored in Redis (shared by all workers, compressed) with an in-process fallback when Redis is absent; never logged, expires after one hour.

The chat carries a small summary (name, format, chain lengths, selection,
ligand). The full PDB/mmCIF and sequences are available to the model through
`read_viewer_structure`, `viewer_sequence`, `viewer_selection` and
`find_in_viewer_structure`. This is the “full access if selected” path: no
megabytes in every prompt, but the assistant can read the entire selected
object when it needs to.

The Copilot context bar above the composer shows what is attached (`3D ·
demo.pdb · A(412) · ligand ATP · 3 selected · full data attached`). The Context
tab has an attach/detach switch, a refresh button and a structure summary.

### Page probe


`agent-context.js` sends a deliberately small slice:

- `route`: `Views.current` on the SPA, or `from` on `/agent`
- `title`: page title / project name
- `entity`: open project, job or file
- `selection`: checked or selected rows
- `settings`: active tab, visible filters and search values
- `facts`: up to ten short visible statements (stat cards, row count, empty
  state, breadcrumb)

The server enforces text length, key names, control-character removal and a
4 KB cap. The prompt says to resolve “this project/job/file” against it and to
verify numbers with tools anyway.

The entity is also a tool default: `list_jobs`, `get_project`, `read_file_text`,
`propose_job` and similar tools use the open project/file when the id is
omitted.

## 6. Tool catalog (67 tools)

The catalog is grouped in the UI and exposed by `GET /api/agent/tools` with
JSON schemas. The UI can run any tool manually in the Tools tab.

**Context / account** — `get_page_context`, `get_me`, `get_usage`

**Projects** — `list_projects`, `get_project`, `get_project_members`,
`get_project_activity`, `get_project_stats`, `search_projects`

**Jobs** — `list_jobs`, `get_job`, `get_job_logs`, `get_job_artifacts`,
`read_job_output`, `get_job_queue`, `get_job_timeline`

**Bio models** — `list_models`, `get_model`, `get_model_runtime`,
`recommend_model`, `model_input_schema`, `match_models_to_input`,
`model_compatibility_matrix`, `check_model_ready`, `estimate_job_cost`

**Bio analysis** — `sequence_analysis`, `translate_sequence`,
`reverse_complement`, `find_orfs`, `structure_summary`,
`extract_structure_sequence`, `structure_ligands`, `validate_ligand`

**Live 3D / sequence** — `get_viewer_context`, `read_viewer_structure`,
`viewer_sequence`, `viewer_selection`, `find_in_viewer_structure`

**Running models** — `propose_job` (confirmation draft), `propose_pipeline`
(plan only), `summarize_job_result`

**Files / notebooks / pipelines / flows** — `list_files`, `search_files`,
`get_file`, `read_file_text`, `list_notebooks`, `get_notebook`,
`list_notebook_runs`, `list_pipelines`, `get_pipeline`, `list_flows`,
`get_flow`

**Teams / activity / support** — `list_teams`, `get_team`, `get_activity`,
`get_notifications`, `list_tickets`, `get_ticket`

**Search / actions** — `global_search`, `calculate`, `open_project`,
`open_job`, `open_file`, `open_view`, `prepare_job`

**Web (optional)** — `web_search`, `fetch_url`

The model tools use the same sources as the product:

- `Config.SUPPORTED_MODELS` for the catalog and credit cost
- `helpers.model_io` for payload kinds, format detection, FASTA/structure
  parsing and the compatibility matrix
- `helpers.sequence_utils` for validation, composition, translation, GC,
  molecular weight, pI and ORFs
- RDKit (already a Phonos dependency) for ligand validation
- `helpers.paths` for safe job-output and project-file reads

## 7. Running a real bio-model job

`propose_job` validates the model, project, sequence/file, required custom
fields and credit balance, then returns a `confirm_job` client action with a
draft. The chat renders a modal with model, project, input summary, cost and
balance. On **Run job**, the browser calls:

```
POST /api/agent/jobs   { "draft": { ... } }
```

The endpoint re-checks project access and delegates to the existing
`routes.api_jobs._handle_job_submission()`. That means the assistant does not
reimplement role checks, model availability, adaptive cost, output paths,
Redis queueing or socket notifications. The result is a normal job in the Jobs
tab and in `job_logs`.

`prepare_job` remains the non-writing alternative: it opens the project’s job
wizard with the model and sequence prefilled and clears nothing.

**Confirmation and auto-run.** The confirmation modal lets the user change the
project, shows the model, cost, input summary and any used files, then submits
through the normal job path. A per-chat **Auto-run** toggle (default off) grants
permission to skip the second click: only then does `propose_job` return a
signed, short-lived token, and `POST /api/agent/jobs` accepts an automatic run
only with a token matching the user, chat and exact draft. The chat then polls
`GET /api/agent/jobs/<job_id>` and renders status, input file, artifacts and
output files in a run card. A matching submitted/running job suppresses
auto-run and shows a duplicate warning; the user can still confirm manually.

## 8. Configuration

Environment (wins over database):

```
GEMINI_API_KEY=...                 # default provider, OpenAI-compatible endpoint
PHONOS_AGENT_API_KEY=...           # generic key for any endpoint
PHONOS_AGENT_BASE_URL=...
PHONOS_AGENT_MODELS_URL=...
PHONOS_AGENT_MODEL=...
```

Admin settings (Assistant → Settings):

- **Fyreflysystems Models**: an OpenCode-compatible provider preset. Its API key is read from `OPENCODE_API_KEY`/`FYREFLY_MODELS_API_KEY` on the server, is never displayed and is never stored from the settings form. If the model endpoint is unreachable, a built-in OpenCode model list is offered.

- base URL, models URL, model, sealed API key
- temperature, max steps, timeout, history length
- web search on/off
- additional system instruction

Secrets are sealed with `helpers.data_crypto` when `PHONOS_DATA_KEYS` is set;
the key is never returned. The environment always wins for the active key.
Provider presets include Google Gemini, **Fyreflysystems Models**, OpenCode Go,
OpenAI and local Ollama/LM Studio.

Default endpoint: Google Gemini's OpenAI-compatible API
(`https://generativelanguage.googleapis.com/v1beta/openai`). Presets for
OpenCode Go, OpenAI and local Ollama/LM Studio are included. The model list is
cached for ten minutes.

## 9. UI surfaces

- **Dedicated page `/agent`**: left chat sidebar, main tabs (Chat, Models,
  Tools, Context, Usage, Settings), right context rail.
- **Right-hand panel** in the dashboard: launcher, Ctrl+Shift+A, tabs (Chat,
  Models, Chats, Tools, Context, Usage, Settings), expand-to-page button.
- **Navigation**: Tools → Copilot opens the panel; the header has a Copilot
  button; the page can be opened directly.
- **Models tab**: active bio models with accepted payloads, cost, readiness,
  input schema and an “Ask Copilot” prompt.
- **Runs tab**: recent jobs with status, model, project, cost and a live output
  card (input file, artifacts, structure/sequence output).
- **Model picker** in the composer: switch the model for the current chat; the
  choice is stored on the chat.
- **Auto-run** toggle in the composer: off by default; on makes Copilot submit
  a proposed job after a short visible countdown, with a signed token and a
  server-side check of the chat permission.
- **Tools tab**: grouped catalog with filters (All/Models/Bio/Run/Read/Actions/
  Web), schema details and a manual test form.
- **Job modal**: confirmation before any real submission, with a project
  selector, used files, cost, balance and a live output card after submission.
- **Live voice**: microphone button uses the browser SpeechRecognition API for
  hands-free dictation and sends the final transcript; answers can be spoken
  with the browser SpeechSynthesis voice. A per-answer “Read aloud” link is
  always available. Browser speech may use the browser vendor’s service, so
  the UI warns against confidential dictation.

## 10. Security boundaries

- No shell, arbitrary HTTP or arbitrary SQL from the model.
- Tools are allow-listed and typed; arguments are parsed and validated.
- Every project/file/job read passes the same membership checks as the UI.
- Web fetch blocks loopback/private/link-local addresses, credentials in URLs,
  non-standard ports and redirects to blocked hosts; no proxy bypass.
- Page context and tool results are framed as data in the system prompt.
- No API key, password or token is exposed to the model or the browser.
- CSRF protection on every mutating endpoint.
- Chat content is per user; deleted chats are soft-deleted for the audit trail.
- Job submission is a separate, user-confirmed action, not a model write tool.

## 11. Testing and deployment

```
cd ~/Desktop/phonos
ENCRYPTION_KEY="a-secure-test-key-for-dev-use-32" \
  ./venv/bin/python -m pytest tests/test_agent_lab.py -q
node --check static/js/agent.js
node --check static/js/agent-context.js
```

Production deploy goes through the existing dev/prod split:

```
~/bin/fyrefly-sync --only phonos          # dry run
~/bin/fyrefly-sync --only phonos --apply  # owner-approved
```

The deploy script ships only git-tracked files, never `.env`, and restarts
`phonos-web`, `phonos-mrs` and `phonos-workers.target`. The assistant needs no
new Python package.

## 12. Known limits

- Tool outputs are truncated to keep prompts bounded; large PDBs and FASTA
  files return summaries or the first requested characters. The live 3D
  snapshot bypasses prompt size by staying on the server and is read on demand.
- `propose_job` covers sequence-input and file-input models, but the file must
  already be a project file; it does not upload new files.
- Pipelines are planned, not submitted by the assistant; the existing pipeline
  UI remains the place to confirm multi-stage runs.
- The agent has no background scheduler and no automatic retries.
- If `GEMINI_API_KEY` is absent and no admin key is stored, the assistant
  answers with a clear configuration error instead of guessing.
