"""User-tweakable configuration: what to watch, how to heal, what to schedule.

Everything an operator is likely to adjust lives here. The engine
(``main.py`` / ``healer.py`` / ``checks.py``) reads from these structures and
should not need editing for routine changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .env import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Loop / timing
# ---------------------------------------------------------------------------
CHECK_INTERVAL = 20          # seconds between health-check cycles
STARTUP_GRACE = 30           # don't heal anything for this long after watchdog start
STATUS_TTL = 120             # TTL (s) on the status key we publish to Redis

# ---------------------------------------------------------------------------
# Flap protection / circuit breaker (per target)
# ---------------------------------------------------------------------------
MAX_RESTARTS = 3             # max restarts of one target within RESTART_WINDOW
RESTART_WINDOW = 600         # rolling window (s) for the restart budget
RESTART_COOLDOWN = 60        # min seconds between restart attempts for a target
POST_HEAL_GRACE = 45         # after a restart, give the target this long to recover
                             # before it can be judged unhealthy / re-healed again

# ---------------------------------------------------------------------------
# Worker liveness
# ---------------------------------------------------------------------------
# MRS records each worker's last heartbeat in this Redis hash.
WORKER_HEALTH_HASH = "hash:worker_health"
# A worker is "stale" if its last_seen is older than this. The app's own worker
# timeout is 90s; we allow a generous multiple so we only act on real outages.
WORKER_STALE_SECONDS = 240
# If MRS is up but the *freshest* worker heartbeat across the whole fleet is older
# than this, MRS itself is likely wedged (not draining the heartbeat channel).
MRS_WEDGE_SECONDS = 360

# Redis key the watchdog publishes its own status under.
STATUS_KEY = "watchdog:status"
HEARTBEAT_KEY = "watchdog:heartbeat"


@dataclass
class Target:
    """A thing we monitor and (optionally) heal by restarting a systemd unit."""

    name: str                       # logical name, used in status + logs
    kind: str                       # "redis" | "postgres" | "http" | "mrs" | "unit"
    tier: int                       # lower tiers are healed first (dependencies)
    unit: Optional[str] = None      # systemd unit to restart to heal it (None = alert only)
    depends_on: List[str] = field(default_factory=list)
    # kind-specific extra:
    url: Optional[str] = None       # for kind == "http"


@dataclass
class ExpectedWorker:
    """A worker we expect to be heartbeating, and how to heal it if it isn't."""

    worker_id: str
    # systemd unit that owns this worker. None -> monitor + alert only (we will
    # NOT auto-restart it, because the safe restart action is unknown).
    unit: Optional[str] = None


# ---------------------------------------------------------------------------
# Service targets, ordered by dependency tier.
#   tier 0: foundational datastores (everything depends on them)
#   tier 1: the app control plane (Flask app, MRS router)
# Workers are handled separately (see EXPECTED_WORKERS) since liveness comes
# from Redis heartbeats rather than just "is the unit running".
# ---------------------------------------------------------------------------
TARGETS: List[Target] = [
    Target(name="redis", kind="redis", tier=0, unit="redis6.service"),
    Target(name="postgres", kind="postgres", tier=0, unit="postgresql.service"),
    Target(
        name="app",
        kind="http",
        tier=1,
        unit="phonos.service",
        url=None,  # filled from Settings.app_url at runtime
        depends_on=["redis", "postgres"],
    ),
    Target(
        name="mrs",
        kind="mrs",
        tier=1,
        unit="phonos-mrs.service",
        depends_on=["redis", "postgres"],
    ),
]

# ---------------------------------------------------------------------------
# Workers expected to be heartbeating.
#
# boltz2-worker-1 is cleanly owned by phonos-boltz2.service (cgroup confirms it),
# so we auto-heal it by restarting that unit.
#
# The three NIM workers below (esmfold-re / proteinmpnn-re / evo2) are currently
# UNSUPERVISED orphans — they were launched by hand in SSH sessions (ps shows
# them in cgroup user.slice/session-*.scope, reparented to init) and NO systemd
# unit manages them. They are NOT handled by phonos-workers.service either: that
# unit runs deploy_new_workers.py, whose NEW_WORKERS only covers alphafold2-re /
# alphafold2-re-multimer / boltz2-enhanced, and whose duplicate-guard reads a PID
# file that is currently empty ({"active_workers": {}}) — so restarting it would
# spawn a DUPLICATE boltz2-enhanced and still not touch these three.
#
# Therefore there is no safe auto-restart for them today and they are MONITOR-ONLY
# (unit=None): the watchdog alerts when they die but takes no action. To make them
# self-healing, install the prepared units in phonos_watchdog/units/ and follow the
# one-time cutover in README.md, then set `unit=` to the matching service.
# ---------------------------------------------------------------------------
# Every worker below now has a dedicated systemd unit (installed + enabled from
# phonos_watchdog/units/), so each autostarts on boot with Restart=always.
#
# BUT until the next reboot (or a manual cutover) the *currently running*
# instances are still hand-started orphans, not the systemd units. If the
# watchdog restarted a unit while its orphan twin were still alive we'd get two
# processes sharing one worker-id. So worker auto-heal is gated on the flag below:
# leave it False until no orphans remain (a clean reboot guarantees this), then
# set it True and restart the watchdog for full heartbeat-staleness healing.
WORKERS_ARE_SYSTEMD_MANAGED = False


def _worker_unit(unit: str) -> str:
    return unit if WORKERS_ARE_SYSTEMD_MANAGED else None


EXPECTED_WORKERS: List[ExpectedWorker] = [
    # boltz2-worker-1 is already the systemd unit (no orphan), so always healable.
    ExpectedWorker("boltz2-worker-1", "phonos-boltz2.service"),
    ExpectedWorker("esmfold-re-worker-1", _worker_unit("phonos-esmfold-re.service")),
    ExpectedWorker("proteinmpnn-re-worker-1", _worker_unit("phonos-proteinmpnn-re.service")),
    ExpectedWorker("evo2-worker-1", _worker_unit("phonos-evo2.service")),
    ExpectedWorker("molmim-worker-1", _worker_unit("phonos-molmim.service")),
    ExpectedWorker("openfold3-worker-1", _worker_unit("phonos-openfold3.service")),
    ExpectedWorker("genmol-worker-1", _worker_unit("phonos-genmol.service")),
    ExpectedWorker("diffdock-re-worker-1", _worker_unit("phonos-diffdock-re.service")),
    # NOT monitored here:
    #  * rfdiffusion-worker-1 runs (phonos-rfdiffusion.service, enabled for boot) but
    #    its heartbeats are never recorded in hash:worker_health — MRS only records
    #    SIGNED heartbeats and rfdiffusion's aren't verified (pre-existing key/signing
    #    issue). The watchdog has no reliable liveness signal for it, so heartbeat
    #    monitoring would just show it permanently DOWN / heal-loop. It still
    #    autostarts + crash-restarts via systemd Restart=always.
    #  * node-<uuid> (type-12) is a REMOTE publisher node heartbeating into this Redis
    #    (no local process) — not started by this host.
]


@dataclass
class PeriodicTask:
    """A command run on a fixed interval, independent of health checks.

    Failures are logged but never crash the watchdog or trigger heals.
    """

    name: str
    interval: int                   # seconds between runs
    command: List[str]
    cwd: Optional[str] = None
    timeout: int = 1800             # hard kill after this many seconds
    enabled: bool = True
    run_on_start: bool = False      # run once shortly after startup?


# code-review-graph lives in its own venv at ~/code-review-graph.
_CRG_BIN = "/opt/phonos/code-review-graph/.venv/bin/code-review-graph"

PERIODIC_TASKS: List[PeriodicTask] = [
    PeriodicTask(
        name="code-review-graph-update",
        interval=3600,                       # hourly incremental graph update
        command=[_CRG_BIN, "update", "--brief"],
        cwd=str(PROJECT_ROOT),               # the phonos git repo
        timeout=1800,
        enabled=True,
        run_on_start=False,
    ),
]
