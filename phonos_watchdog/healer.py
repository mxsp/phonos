"""Self-healing via `sudo systemctl restart`, with flap protection.

Per target we enforce:
  * a cooldown between restart attempts,
  * a post-heal grace period (don't re-judge a unit while it's still coming up),
  * a restart budget (MAX_RESTARTS within RESTART_WINDOW) backed by a circuit
    breaker — once tripped we stop restarting and escalate to a CRITICAL log,
    until the window clears.
"""
from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict

from . import config

log = logging.getLogger("phonos_watchdog.healer")


@dataclass
class TargetState:
    restarts: Deque[float] = field(default_factory=deque)   # timestamps in window
    last_attempt: float = 0.0
    last_outcome: str = ""
    circuit_open: bool = False
    grace_until: float = 0.0        # don't evaluate/heal before this time


class HealOutcome:
    HEALED = "restarted"
    COOLDOWN = "cooldown"
    GRACE = "grace"
    CIRCUIT_OPEN = "circuit_open"
    NO_UNIT = "alert_only"
    FAILED = "restart_failed"


class Healer:
    def __init__(self) -> None:
        self._state: Dict[str, TargetState] = {}

    def state(self, name: str) -> TargetState:
        return self._state.setdefault(name, TargetState())

    def in_grace(self, name: str, now: float = None) -> bool:
        """True while a recently-healed target is still allowed to come up."""
        now = now if now is not None else time.time()
        return now < self.state(name).grace_until

    def _prune_window(self, st: TargetState, now: float) -> None:
        while st.restarts and now - st.restarts[0] > config.RESTART_WINDOW:
            st.restarts.popleft()

    def heal(self, name: str, unit: str) -> str:
        """Attempt to heal `name` by restarting `unit`. Returns a HealOutcome."""
        now = time.time()
        st = self.state(name)

        if not unit:
            st.last_outcome = HealOutcome.NO_UNIT
            log.warning("[%s] unhealthy and monitor-only (no unit to restart)", name)
            return HealOutcome.NO_UNIT

        if now - st.last_attempt < config.RESTART_COOLDOWN:
            return HealOutcome.COOLDOWN

        self._prune_window(st, now)
        if len(st.restarts) >= config.MAX_RESTARTS:
            if not st.circuit_open:
                st.circuit_open = True
                log.critical(
                    "[%s] CIRCUIT OPEN: %d restarts of %s within %ds did not stick "
                    "— giving up auto-heal until the window clears. MANUAL ACTION NEEDED.",
                    name, len(st.restarts), unit, config.RESTART_WINDOW,
                )
            st.last_outcome = HealOutcome.CIRCUIT_OPEN
            return HealOutcome.CIRCUIT_OPEN

        # Attempt the restart.
        st.last_attempt = now
        log.warning("[%s] HEALING — sudo systemctl restart %s", name, unit)
        try:
            res = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", unit],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            st.last_outcome = HealOutcome.FAILED
            log.error("[%s] restart of %s errored: %s", name, unit, exc)
            return HealOutcome.FAILED

        if res.returncode != 0:
            st.last_outcome = HealOutcome.FAILED
            log.error(
                "[%s] `systemctl restart %s` exited %d: %s",
                name, unit, res.returncode, (res.stderr or "").strip(),
            )
            # Count the failed attempt against the budget too, so we don't hammer.
            st.restarts.append(now)
            return HealOutcome.FAILED

        st.restarts.append(now)
        st.circuit_open = False
        st.grace_until = now + config.POST_HEAL_GRACE
        st.last_outcome = HealOutcome.HEALED
        log.warning(
            "[%s] restarted %s (%d/%d in window); grace %ds",
            name, unit, len(st.restarts), config.MAX_RESTARTS, config.POST_HEAL_GRACE,
        )
        return HealOutcome.HEALED

    def snapshot(self, name: str) -> dict:
        """Serializable per-target heal state, for the published status."""
        st = self.state(name)
        self._prune_window(st, time.time())
        return {
            "restarts_in_window": len(st.restarts),
            "last_attempt": st.last_attempt,
            "last_outcome": st.last_outcome,
            "circuit_open": st.circuit_open,
            "grace_until": st.grace_until,
        }
