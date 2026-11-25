"""Phonos watchdog — a self-contained, parallel self-healing supervisor.

Runs alongside redis / postgres / phonos / mrs / workers. Each cycle it health-
checks every tier, restarts (via sudo systemctl) whatever is down — honouring
dependency order, cooldowns and a circuit breaker — runs due periodic tasks
(e.g. the code-review-graph update), and publishes its status to Redis.

CLI:
    python -m phonos_watchdog run      # main loop (used by the systemd unit)
    python -m phonos_watchdog once     # one cycle, print results, exit
    python -m phonos_watchdog status   # print the last published status
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from typing import Dict, List

from . import config
from .checks import Checker
from .env import Settings
from .healer import HealOutcome, Healer
from .tasks import TaskRunner

log = logging.getLogger("phonos_watchdog")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


class Watchdog:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.checker = Checker(self.settings)
        self.healer = Healer()
        self.startup = time.time()
        self.tasks = TaskRunner(config.PERIODIC_TASKS, self.startup)
        self._stop = False
        # Resolve the app target's URL from settings.
        self.targets = config.TARGETS
        for t in self.targets:
            if t.kind == "http" and not t.url:
                t.url = self.settings.app_url
        self._last_status: dict = {}

    # --- one health-check cycle ---------------------------------------------
    def _check_target(self, target: config.Target):
        if target.kind == "redis":
            return self.checker.check_redis()
        if target.kind == "postgres":
            return self.checker.check_postgres()
        if target.kind == "http":
            return self.checker.check_http(target.url or self.settings.app_url)
        if target.kind == "mrs":
            return self.checker.check_mrs()
        if target.kind == "unit":
            from .checks import systemd_is_active
            ok = systemd_is_active(target.unit or "")
            return type("R", (), {"ok": ok, "detail": "active" if ok else "inactive"})()
        return type("R", (), {"ok": False, "detail": "unknown kind"})()

    def run_cycle(self) -> dict:
        now = time.time()
        in_startup_grace = now < self.startup + config.STARTUP_GRACE
        services: Dict[str, dict] = {}
        unhealthy: set[str] = set()        # names that are down or recovering
        report_lines: List[str] = []

        for target in sorted(self.targets, key=lambda t: t.tier):
            res = self._check_target(target)
            action = "ok"

            if not res.ok:
                unhealthy.add(target.name)
                blocked_by = [d for d in target.depends_on if d in unhealthy]
                if in_startup_grace:
                    action = "startup_grace"
                elif blocked_by:
                    action = f"deferred (waiting on {', '.join(blocked_by)})"
                elif self.healer.in_grace(target.name, now):
                    action = "recovering (post-heal grace)"
                else:
                    action = self.healer.heal(target.name, target.unit)
            else:
                # A target still inside post-heal grace counts as "settling".
                if self.healer.in_grace(target.name, now):
                    unhealthy.discard(target.name)

            services[target.name] = {
                "ok": bool(res.ok),
                "detail": res.detail,
                "tier": target.tier,
                "unit": target.unit,
                "action": action,
                "heal": self.healer.snapshot(target.name),
            }
            report_lines.append(
                f"{target.name}={'OK' if res.ok else 'DOWN'}"
                + ("" if res.ok or action == "ok" else f"[{action}]")
            )

        workers = self._check_workers(now, unhealthy, in_startup_grace, report_lines)

        # Periodic tasks (code-review-graph, …) run regardless of service health.
        self.tasks.tick()

        status = {
            "ts": now,
            "pid": os.getpid(),
            "startup": self.startup,
            "uptime": now - self.startup,
            "services": services,
            "workers": workers,
            "tasks": self.tasks.snapshot(),
        }
        self._last_status = status
        self._publish(status)
        log.info("cycle: %s", "  ".join(report_lines))
        return status

    def _check_workers(self, now, unhealthy, in_startup_grace, report_lines) -> dict:
        workers: Dict[str, dict] = {}
        # Workers depend on redis (to read heartbeats) and a healthy MRS (to write
        # them). If either is down/recovering, defer worker judgement entirely.
        deps_down = [d for d in ("redis", "mrs") if d in unhealthy]
        restarted_units: set[str] = set()

        for ew in config.EXPECTED_WORKERS:
            res = self.checker.check_worker(ew.worker_id)
            action = "ok"
            if not res.ok:
                if in_startup_grace:
                    action = "startup_grace"
                elif deps_down:
                    action = f"deferred (waiting on {', '.join(deps_down)})"
                elif ew.unit and ew.unit in restarted_units:
                    action = "covered (unit already restarted this cycle)"
                elif ew.unit and self.healer.in_grace(ew.unit, now):
                    action = "recovering (post-heal grace)"
                else:
                    # Key the healer state by UNIT so multiple workers sharing a
                    # unit share one restart budget and we restart it only once.
                    action = self.healer.heal(ew.worker_id, ew.unit)
                    if ew.unit and action == HealOutcome.HEALED:
                        restarted_units.add(ew.unit)
            workers[ew.worker_id] = {
                "ok": bool(res.ok),
                "detail": res.detail,
                "unit": ew.unit,
                "action": action,
                "heal": self.healer.snapshot(ew.worker_id),
            }
            if not res.ok:
                report_lines.append(
                    f"worker:{ew.worker_id}=DOWN"
                    + ("" if action == "ok" else f"[{action}]")
                )
        return workers

    # --- status publication --------------------------------------------------
    def _publish(self, status: dict) -> None:
        try:
            r = self.checker.redis_client()
            payload = json.dumps(status, default=str)
            r.set(config.STATUS_KEY, payload, ex=config.STATUS_TTL)
            r.set(config.HEARTBEAT_KEY, str(status["ts"]), ex=config.STATUS_TTL)
        except Exception as exc:  # noqa: BLE001 - redis may be the thing that's down
            log.debug("could not publish status to redis: %s", exc)

    # --- loop ----------------------------------------------------------------
    def _handle_signal(self, signum, _frame) -> None:
        log.info("received signal %s, shutting down", signum)
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        log.info(
            "phonos-watchdog starting (pid=%s); %d service targets, %d workers, "
            "%d periodic tasks; check interval %ds, startup grace %ds",
            os.getpid(), len(self.targets), len(config.EXPECTED_WORKERS),
            len([t for t in config.PERIODIC_TASKS if t.enabled]),
            config.CHECK_INTERVAL, config.STARTUP_GRACE,
        )
        while not self._stop:
            cycle_start = time.time()
            try:
                self.run_cycle()
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("unexpected error in watchdog cycle")
            # Sleep the remainder of the interval, waking promptly on a signal.
            elapsed = time.time() - cycle_start
            sleep_for = max(1.0, config.CHECK_INTERVAL - elapsed)
            slept = 0.0
            while slept < sleep_for and not self._stop:
                time.sleep(min(1.0, sleep_for - slept))
                slept += 1.0
        log.info("phonos-watchdog stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_status(status: dict) -> None:
    if not status:
        print("no status available")
        return
    age = time.time() - float(status.get("ts", 0))
    print(f"watchdog pid={status.get('pid')} uptime={status.get('uptime', 0):.0f}s "
          f"status_age={age:.0f}s")
    print("services:")
    for name, s in status.get("services", {}).items():
        flag = "OK " if s["ok"] else "DOWN"
        print(f"  [{flag}] {name:<10} {s['detail']}  -> {s['action']}")
    print("workers:")
    for name, s in status.get("workers", {}).items():
        flag = "OK " if s["ok"] else "DOWN"
        print(f"  [{flag}] {name:<28} {s['detail']}  -> {s['action']}")
    print("tasks:")
    for name, t in status.get("tasks", {}).items():
        if t["runs"]:
            when = f"{time.time() - t['last_run']:.0f}s ago"
        else:
            when = "not yet run"
        print(f"  {name}: {t['last_status']} (runs={t['runs']}, last={when})")


def main(argv: List[str] | None = None) -> int:
    _setup_logging()
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "run"

    if cmd == "run":
        Watchdog().run()
        return 0
    if cmd == "once":
        status = Watchdog().run_cycle()
        _print_status(status)
        return 0
    if cmd == "status":
        wd = Watchdog()
        try:
            raw = wd.checker.redis_client().get(config.STATUS_KEY)
            _print_status(json.loads(raw) if raw else {})
        except Exception as exc:  # noqa: BLE001
            print(f"could not read status from redis: {exc}")
            return 1
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
