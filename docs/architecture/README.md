# Phonos — Architecture Review

A staff-level architecture review of the Phonos monolith: what it is today, what
it should become, and how to get there without disrupting the live deployment.

Produced 2026-07-24. Grounded in a full read of `phonos_app/`, `production/`,
`static/`, `templates/`, and the deploy tooling.

## Deliverables

| # | Doc | What it answers |
|---|---|---|
| 1 | [01-proposed-structure.md](./01-proposed-structure.md) | The target repository layout (`apps/`, `services/`, `packages/`, `infrastructure/`, `docs/`) and what maps where. |
| 2 | [02-knowledge-graph.md](./02-knowledge-graph.md) | Applications, services, modules, data flows, and internal/external dependencies — as diagrams. |
| 3 | [03-dead-code-and-cleanup-report.md](./03-dead-code-and-cleanup-report.md) | Dead code, duplicate trees, committed secrets, stale deps — evidence + what was actioned. |
| 4 | [04-monolith-decomposition-plan.md](./04-monolith-decomposition-plan.md) | How to split the oversized modules (`app.js` 50k LOC, `api_jobs.py`, `production/database.py`, …). |
| 5 | [05-refactoring-roadmap.md](./05-refactoring-roadmap.md) | Prioritized backlog by impact × effort, including the UX/notification refactor. |
| 6 | [06-migration-strategy.md](./06-migration-strategy.md) | Minimal-disruption sequencing, with the exact recipe to relocate the live web core. |

## The system in one paragraph

Phonos is a **single-process Flask + Socket.IO monolith** (gunicorn/eventlet)
that brokers scientific-compute jobs (protein folding, docking, quantum, HPC) to
a fleet of **out-of-process workers** over Redis, plus a **Notebook IDE** backed
by dockerized kernel/Wolfram services. The frontend is **framework-less vanilla
JS** — no bundler, no modules — dominated by one 50k-line `app.js`. The backend
attaches ~50 route modules to a single global `app` object (only the notebook
API uses a Blueprint). Two Postgres connection pools (request vs. background)
share one schema owned by `production/database.py`.

## Guiding principles for this review

1. **Do no harm to production.** The live app is served from repo-root paths
   wired into systemd/nginx. Structural moves that touch those paths are staged,
   not blind-pushed.
2. **Chokepoints over sweeps.** Prefer changes at shared seams (`Modal`, `Toast`,
   the route aggregator, the app factory) that fix many call sites at once over
   editing hundreds of sites.
3. **Reversibility.** Every step is a `git mv` / additive file / config toggle
   that can be reverted in one commit.
