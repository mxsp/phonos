# 5. Refactoring roadmap

Prioritized by **impact × effort**. Each item is independently shippable and
reversible. "Effort" is rough engineer-days.

## Priority matrix

| # | Item | Impact | Effort | Risk |
|---|---|---|---|---|
| P0 | Rotate exposed secrets + split `node_config` keys (doc 3.2) | 🔴 critical | S | low |
| P0 | UX & notification refactor — modals → right-side panels (§A) | 🟠 high | M | med |
| P1 | Extract shared JS primitives (`ui/*.js`) from `app.js` (doc 4.5) | 🟠 high | M | low |
| P1 | Consolidate 8+ toast systems onto one `Toast` (§B) | 🟠 high | M | low |
| P1 | Split `api_jobs.py` mega-functions via per-model registry (doc 4.2) | 🟠 high | M | med |
| P2 | Global `app` → Blueprints, module by module (doc 4.1) | 🟠 high | L | med |
| P2 | Split `production/database.py` + extract `SUPPORTED_MODELS` (doc 4.4) | 🟡 med | M | med |
| P2 | Reconcile drifted worker copies; delete `public_api.py` + empty stubs | 🟡 med | S | low |
| P3 | Relocate live web core to `apps/main-app` (doc 6) | 🟡 med | M | high |
| P3 | Git LFS / optimize large binaries (doc 3.5); de-dupe agent-rule docs | 🟢 low | S | low |
| P3 | Test tree: split `test_main.py` (2.6k LOC) into `tests/<domain>/` | 🟡 med | M | low |

---

## A. UX & Notification refactor (P0)

**Goal.** Eliminate pop-up dialogs, modals, and intrusive notifications. Move
workflows/forms/settings/contextual actions into **right-side panels/drawers**.
Reserve notifications for **critical events, errors, and security warnings**.

**Why it's tractable.** The app already has (1) a single `Modal` chokepoint
(app.js:2421, 44 `Modal.open` sites + `ConfirmDialog`) and (2) a fully-built but
**unused** right-side `Drawer` (`app-shell.js`, styled in `dashboard.css`). The
refactor is mostly *rewiring a chokepoint*, not editing hundreds of sites.

**Delivered in this pass (foundation):**
- `static/js/ui-panel.js` — a self-contained, dependency-free right-side
  **`Panel`** primitive that builds its own DOM (no template markup required),
  supports width presets, ESC/overlay close, focus trap, body-scroll lock, and a
  promise-based **`Panel.confirm()`** / **`Panel.form()`**. Namespaced
  `.ui-panel*` classes to avoid collisions.
- `static/css/ui-panel.css` — theme-aware styling reusing the existing design
  tokens.

**Migration steps (remaining, do with browser validation):**
1. **Route `Modal.open` through `Panel`.** Add `placement: 'drawer' | 'center'`
   to `Modal.open`; default forms/wizards to `'drawer'`. Because 44 sites call
   the one `Modal.open`, this converts the bulk with **zero call-site edits**.
   Keep `'center'` for genuinely modal moments (blocking disclaimers, one-time
   key reveal `sticky`).
2. **Kill native dialogs.** Replace the ~76 `confirm()`/`alert()`/`prompt()`
   calls with `await Panel.confirm(...)` / `Panel.form(...)`. `ConfirmDialog`
   already exists but has only 1 call site — make it (backed by `Panel`) the
   standard and convert app.js (14) then notebook.js (56). Each is a
   sync→async control-flow change, so convert per-file with tests.
3. **Fold hand-rolled modals** (9 in app.js: webhook/API-key reveal, batch
   detail; notebook `nb-modal` wizards) onto `Panel`.
4. **Notification policy.** Add a severity gate at the single `Toast.show`
   chokepoint: `error`/`warning`/security persist and are prominent; `info`/
   `success` become brief and low-profile (or route to the existing
   **notification center** `notifications-sidebar.js` instead of a toast). This
   honors "critical only" without touching 700+ call sites.

**Acceptance:** no `window.confirm/alert/prompt` in app JS; forms/settings open
as right drawers; only error/warning/security surface as toasts.

---

## B. Toast consolidation (P1)

Replace the per-page `showToast`/`showNotification` copies in `notebook.js`
(125), `phonos_viewer.js`, `viewer_qol.js`, `custom_models.js`, `boltz2.js`,
`openfold3.js`, `wolfram_ui.js` with the canonical `window.Toast`. Keep
`DesktopNotifications` (OS push) and `NotificationsSidebar` (inbox) as-is — they
are the legitimate persistent-notification surfaces.

---

## Suggested execution order

P0 secrets → P0 UX foundation (shipped) → P0/P1 UX wiring with browser tests →
P1 JS primitive extraction + toast consolidation → P1 `api_jobs` split →
P2 Blueprints + db/config split → P3 physical relocation + bloat + tests.
