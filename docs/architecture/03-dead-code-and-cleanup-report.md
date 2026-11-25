# 3. Dead-code & cleanup report

Evidence-backed inventory of dead code, duplicate trees, committed secrets, and
stale artifacts — with what was **actioned in this pass** vs. **deferred** (with
reason). Risky items were not touched without a clear, reversible path.

## 3.1 Actioned in this pass ✅

### Duplicate / dead trees removed
| Item | Evidence | Action |
|---|---|---|
| `phonos-main/` | Empty stub (0 tracked files, only empty dirs); memory's "drifted mirror" is now a shell; referenced only by a stale docker-compose deploy flow | Deleted from disk |
| `test_files/test.smi` | 3-byte tracked artifact, no references | `git rm` |
| `err.txt` (root) | 0 bytes, untracked scratch | Removed |
| root scratch scripts | `scan_i18n.py`, `update_user_credits.py`, `export_docs_to_obsidian.py` at repo root | `git mv` → `scripts/` |
| duplicate `update_user_credits.py` | Root version (app-DB-layer, parameterized) collided with an existing `scripts/` admin-top-up variant | Kept both, root → `scripts/update_user_credits_by_id.py` |

### Reorg moves (see doc 1)
`pypi/` → `packages/sdk/`, `cli_tool/` → `packages/cli/`,
`production/docker/*` → `services/*`, stale root markdown → `docs/`.

### Committed secrets — untracked (files kept on disk)
| File | Risk | Action |
|---|---|---|
| `diffdock/.env` | Real `SECRET_KEY` + `REDIS_PASSWORD` + `ENCRYPTION_KEY` (Fernet-shaped) | `git rm --cached` (now matched by `.env` ignore rule) |
| `node1-esm/.env` | **Byte-identical** Redis/Fernet secrets to `diffdock/.env` (committed twice) | `git rm --cached` |
| `deploy/workers/kubernetes/secret.yaml` | `REDIS_PASSWORD` + `ENCRYPTION_KEY` in `stringData` | `git rm --cached` |
| `production/user_uploads/profile_pics/*` (5 files, up to 1.5 MB) | Real user images committed despite the `production/user_uploads/` ignore rule; privacy + bloat | `git rm --cached` |

## 3.2 ⚠️ Requires owner action — cannot be done safely by tooling

These are **already exposed in git history**; untracking only stops *future*
tracking. They must be **rotated**, then optionally purged from history.

1. **Rotate the shared worker secrets** now exposed in history:
   `REDIS_PASSWORD` + `ENCRYPTION_KEY` (from `diffdock/.env` / `node1-esm/.env` /
   `deploy/.../secret.yaml`). These gate real infra.
2. **The GitHub PAT in the remote URL.** `origin` stores a token as the URL
   username in plaintext (`.git/config`), and tokens were pasted into tooling
   sessions. Rotate the PAT and re-add via SSH or a credential helper instead of
   an inline URL.
3. **`private_key` in 11 tracked `production/node_config_*.json`.** These embed
   worker private keys directly in tracked JSON. As the existing `.gitignore`
   note explains, the fix is a **code change** — split the key out to a
   gitignored `*.pem` loaded at runtime (mirror `node1-esm`'s
   `node_utils._persist_identity`/`_load_secure_text`) — *before* rotating.
   Not a `.gitignore` edit. Tracked as a roadmap item (doc 5).
4. **History purge (optional but recommended)** with `git filter-repo` for the
   secret files above, coordinated with a force-push and collaborator re-clone.

## 3.3 Dead / disabled code (deferred — needs a decision, not a delete)

| Item | Evidence | Recommendation |
|---|---|---|
| `public_api.py` (~2.6k LOC, 24 `/v1/*` routes) | Commented out of `routes/__init__.py` "to avoid duplicate registration"; superseded by `api_external_v1.py` | Confirm no external client depends on `/v1/*`, then delete. Do **not** silently drop — audit access logs first. |
| `production/node3-diffdock.py` | 0 bytes; the real worker is `diffdock/node3-diffdock.py` (590 LOC) | Delete the empty stub; make `diffdock/` the single source of truth. |
| `production/node1-esm.py` (604 LOC) vs `node1-esm/node1-esm.py` (671 LOC) | Drifted duplicate; top-level is the packaged deployable | Reconcile to one source of truth (keep `node1-esm/`). |
| `ToastEnhanced` (app.js:1805) | Legacy toast, ~2 references | Fold into the toast consolidation (doc 5). |
| `window.DrawerInstance` (app-shell.js) | Fully-built right drawer, **0 call sites** | **Don't delete — activate it** as the UX-refactor foundation. |
| collab socket handlers (`socket_handlers.py`) | `@socketio.on` registered only at import; sole importer is a *function-level* import in `api_users.get_online_users` | Verify collab/WebRTC events actually register at startup; if not, register explicitly in `app.py` like the notebook handlers. |
| Duplicate CSS blocks | `.modal-overlay`/`.modal` defined twice in `app.css` (≈1895 and ≈10047/10893) | De-dupe during the CSS pass. |

## 3.4 Dependencies

- **Python (`requirements.txt`)**: fully pinned, transcribed from live prod
  (2026-07-21). Import-signal check found **no obviously unused top-level deps**
  and none missing (`openai` is used in `crispr_gpt/llm.py`; `Bio`/`jwt` are the
  import names for biopython/PyJWT). `qiskit-nature`/`pyscf` correctly live only
  in the notebook-kernel image. **No action needed.**
- **Node (`package.json` / `package-lock.json`)**: only deps are AI-agent CLIs
  (`@anthropic-ai/claude-code`, `opencode-ai`) — **not** app runtime; there is no
  bundler and no `node_modules/`. Recommend moving these to a developer-tooling
  manifest outside the app manifest, or documenting clearly that the app ships
  hand-authored JS. (Left in place this pass — low risk, needs an owner call.)

## 3.5 Large tracked binaries (bloat — deferred)

`data/GeoLite2-Country.mmdb` (7 MB), `static/img/{register,landing,login}-visual.png`
(~11 MB combined, unoptimized), 6 Inter `.ttf` (~0.3 MB each). Candidates for
Git LFS or deploy-time fetch + image optimization. Not touched (functional).

## 3.6 Docs sprawl

Four **byte-identical** agent-rule files (`CLAUDE.md`, `GEMINI.md`,
`.cursorrules`, `.windsurfrules`, md5 `85785109…`) — kept in place because each
tool reads its own path, but they should be generated from one canonical source.
`AD.md`, `agenda.md`, `PERMISSION_AND_SECURITY_FIXES.md` moved into `docs/`.
`SECURITY_HARDENING.md`, `TODO.md`, `PHONOS_PRODUCT.md`, `NB_IMPROVEMENTS_TODO.md`
left at root as active docs. `production/*.md` guides can move to `docs/` later.
