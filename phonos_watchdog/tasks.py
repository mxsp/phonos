"""Periodic background tasks (e.g. the hourly code-review-graph update).

These run on their own cadence, alongside the health loop. A task that fails or
times out is logged but never affects healing or crashes the watchdog.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List

from .config import PeriodicTask

log = logging.getLogger("phonos_watchdog.tasks")


@dataclass
class TaskState:
    last_run: float = 0.0
    last_status: str = "never run"
    last_rc: int = None
    runs: int = 0


class TaskRunner:
    def __init__(self, tasks: List[PeriodicTask], startup: float) -> None:
        self.tasks = [t for t in tasks if t.enabled]
        self._state: Dict[str, TaskState] = {t.name: TaskState() for t in self.tasks}
        for t in self.tasks:
            # run_on_start tasks fire on the first tick (last_run pushed a full
            # interval into the past); all others wait one interval after startup
            # so a watchdog restart doesn't re-trigger heavy jobs immediately.
            self._state[t.name].last_run = (
                startup - t.interval if t.run_on_start else startup
            )

    def due(self, task: PeriodicTask, now: float) -> bool:
        return now - self._state[task.name].last_run >= task.interval

    def tick(self) -> None:
        """Run any tasks whose interval has elapsed. Called each health cycle."""
        now = time.time()
        for task in self.tasks:
            if self.due(task, now):
                self._run(task)

    def _run(self, task: PeriodicTask) -> None:
        st = self._state[task.name]
        st.last_run = time.time()
        st.runs += 1
        log.info("[task:%s] running: %s", task.name, " ".join(task.command))
        try:
            res = subprocess.run(
                task.command,
                cwd=task.cwd,
                capture_output=True, text=True, timeout=task.timeout,
            )
            st.last_rc = res.returncode
            if res.returncode == 0:
                st.last_status = "ok"
                log.info("[task:%s] completed ok", task.name)
            else:
                st.last_status = f"exit {res.returncode}"
                log.error(
                    "[task:%s] exit %d: %s",
                    task.name, res.returncode,
                    (res.stderr or res.stdout or "").strip()[-500:],
                )
        except subprocess.TimeoutExpired:
            st.last_status = "timeout"
            st.last_rc = None
            log.error("[task:%s] timed out after %ds", task.name, task.timeout)
        except (subprocess.SubprocessError, OSError) as exc:
            st.last_status = f"error: {exc}"
            st.last_rc = None
            log.error("[task:%s] failed to launch: %s", task.name, exc)

    def snapshot(self) -> dict:
        return {
            name: {
                "last_run": st.last_run,
                "last_status": st.last_status,
                "last_rc": st.last_rc,
                "runs": st.runs,
            }
            for name, st in self._state.items()
        }
