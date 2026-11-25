# phonos_watchdog

A self-contained **parallel self-healing supervisor** for the Phonos stack. It runs
as its own systemd service (`phonos-watchdog.service`) next to redis, postgres, the
Flask app, MRS and the workers. Every cycle it health-checks each tier, restarts
whatever is down, runs due periodic jobs, and publishes its status to Redis.

It deliberately imports **nothing** from `phonos` / `production` — it reads `.env`
directly — so it keeps working (and keeps healing) even when the app's own code or
its dependencies are broken.

## What it watches & heals

| Target | Health signal | Heal action |
|--------|---------------|-------------|
| `redis` | `PING` (with password from `.env`) | restart `redis6.service` |
| `postgres` | `SELECT 1` via `DATABASE_URL` | restart `postgresql.service` |
| `app` | HTTP `GET /` on :5000 returns < 500 (TCP fallback) | restart `phonos.service` |
| `mrs` | unit active **and** still draining heartbeats (not wedged) | restart `phonos-mrs.service` |
| `boltz2-worker-1` | fresh heartbeat in Redis `hash:worker_health` | restart `phonos-boltz2.service` |
| NIM workers (esmfold-re / proteinmpnn-re / evo2) | fresh heartbeat | **monitor-only** (unsupervised orphans — see below) |

### Safety mechanics
- **Dependency ordering** — datastores (tier 0) are healed before app/mrs (tier 1);
  a dependent that fails only because its dependency is down is *deferred*, not restarted.
- **Cooldown** — at most one restart per target every `RESTART_COOLDOWN` (60s).
- **Post-heal grace** — a just-restarted target is given `POST_HEAL_GRACE` (45s) to
  come up before it can be judged again.
- **Circuit breaker** — more than `MAX_RESTARTS` (3) within `RESTART_WINDOW` (10 min)
  trips the breaker: auto-heal stops and a `CRITICAL` is logged for manual attention.
- **Startup grace** — no healing in the first `STARTUP_GRACE` (30s) after the watchdog starts.

## Periodic tasks
Configured in `config.PERIODIC_TASKS`. Ships with an hourly incremental
`code-review-graph update --brief` over the phonos repo. Task failures are logged
but never crash the watchdog or trigger heals. First run is one interval after start.

## Operating it

```bash
# service
sudo systemctl status phonos-watchdog.service
sudo journalctl -u phonos-watchdog.service -f

# ad-hoc (no service needed)
PYTHONPATH=/opt/phonos python3.11 -m phonos_watchdog once     # one cycle, printed
PYTHONPATH=/opt/phonos python3.11 -m phonos_watchdog status   # last published status
PYTHONPATH=/opt/phonos python3.11 -m phonos_watchdog._selftest # heal-engine self-tests
```

Status is also published to Redis: key `watchdog:status` (JSON, 120s TTL) and
`watchdog:heartbeat`, so the app/UI can surface watchdog health later.

## Tuning
All knobs live at the top of `config.py` (intervals, flap-protection budgets, the
target list, expected workers, periodic tasks). The engine reads from there — no
need to touch `main.py` / `checks.py` / `healer.py` for routine changes.

### Making the NIM workers self-healing (esmfold-re / proteinmpnn-re / evo2)
These three are **monitor-only** because, as deployed today, there is no safe way
to restart them automatically:

- They are **unsupervised orphans** — launched by hand in SSH sessions (`ps` shows
  them in cgroup `user.slice/session-*.scope`, reparented to init). No systemd unit
  owns them.
- They are **not** managed by `phonos-workers.service` either. That unit runs
  `deploy_new_workers.py`, whose `NEW_WORKERS` only covers `alphafold2-re`,
  `alphafold2-re-multimer` and `boltz2-enhanced` — not these three. Its duplicate
  guard reads `production/new_workers_pids.json`, which is currently empty
  (`{"active_workers": {}}`), so restarting it would spawn a **duplicate
  boltz2-enhanced** and still not touch these workers.

So the watchdog alerts when they die but does not act. To make them self-healing,
put each under its own systemd unit (prepared in `phonos_watchdog/units/`), then a
crash → automatic restart by systemd *and* the watchdog.

**One-time cutover (per worker; do it during a quiet window):**
```bash
# 1. install the prepared unit
sudo cp phonos_watchdog/units/phonos-esmfold-re.service /etc/systemd/system/
sudo systemctl daemon-reload

# 2. stop the hand-started orphan (find it with: ps -ef | grep esmfold_re_worker)
kill <orphan-pid>

# 3. start + enable under systemd (survives reboot, Restart=always)
sudo systemctl enable --now phonos-esmfold-re.service
systemctl status phonos-esmfold-re.service     # confirm one healthy instance

# 4. tell the watchdog to heal it: in config.py set
#    ExpectedWorker(worker_id="esmfold-re-worker-1", unit="phonos-esmfold-re.service")
sudo systemctl restart phonos-watchdog.service
```
Repeat with `phonos-evo2.service` (evo2-worker-1) and `phonos-proteinmpnn-re.service`
(proteinmpnn-re-worker-1). Do them one at a time and verify a single instance each
(check `hash:worker_health` / `python3.11 -m phonos_watchdog status`) before moving on.
