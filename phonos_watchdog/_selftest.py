"""Self-test for the heal engine. Touches ONLY a disposable dummy unit and uses
mocked checks for orchestration logic — never the live services.

Run: PYTHONPATH=/opt/phonos python3.11 -m phonos_watchdog._selftest
"""
from __future__ import annotations

import subprocess
import time

from . import config
from .healer import HealOutcome, Healer

DUMMY = "phonos-watchdog-selftest.service"


def _active(unit: str) -> str:
    return subprocess.run(["systemctl", "is-active", unit],
                          capture_output=True, text=True).stdout.strip()


def test_real_restart() -> None:
    print("\n== TEST 1: real heal of disposable dummy unit ==")
    subprocess.run(["sudo", "-n", "systemctl", "stop", DUMMY], check=True)
    print(f"  after stop:    {DUMMY} = {_active(DUMMY)}")
    assert _active(DUMMY) == "inactive", "dummy should be stopped"

    h = Healer()
    outcome = h.heal("dummy", DUMMY)
    time.sleep(1)
    print(f"  heal outcome:  {outcome}")
    print(f"  after heal:    {DUMMY} = {_active(DUMMY)}")
    assert outcome == HealOutcome.HEALED
    assert _active(DUMMY) == "active", "watchdog heal should have restarted it"

    # Immediate second heal must be blocked by cooldown.
    outcome2 = h.heal("dummy", DUMMY)
    print(f"  2nd heal (cooldown): {outcome2}")
    assert outcome2 == HealOutcome.COOLDOWN
    print("  PASS")


def test_circuit_breaker() -> None:
    print("\n== TEST 2: circuit breaker trips after MAX_RESTARTS ==")
    h = Healer()
    # Force the restart budget to look exhausted without real restarts.
    st = h.state("x")
    now = time.time()
    for _ in range(config.MAX_RESTARTS):
        st.restarts.append(now)
    st.last_attempt = 0  # bypass cooldown
    outcome = h.heal("x", "anything.service")
    print(f"  outcome with full budget: {outcome}")
    assert outcome == HealOutcome.CIRCUIT_OPEN
    assert st.circuit_open
    print("  PASS")


def test_dependency_ordering() -> None:
    print("\n== TEST 3: dependents are deferred while a dependency is down ==")
    from .checks import CheckResult
    from .main import Watchdog

    wd = Watchdog()
    wd.startup = 0  # skip startup grace

    # Realistic outage: redis is down, which makes app + mrs fail their own
    # checks too (postgres happens to be fine). Heals are intercepted.
    healed: list[str] = []
    down = {"redis", "app", "mrs"}

    def fake_check(target):
        return CheckResult(False, "mock down") if target.name in down \
            else CheckResult(True, "mock up")

    def fake_heal(name, unit):
        healed.append(name)
        return HealOutcome.HEALED

    wd._check_target = fake_check
    wd.healer.heal = fake_heal
    wd.checker.check_worker = lambda wid: CheckResult(True, "mock up")
    wd.tasks.tick = lambda: None
    wd._publish = lambda status: None

    status = wd.run_cycle()
    print(f"  healed this cycle: {healed}")
    print(f"  app action:  {status['services']['app']['action']}")
    print(f"  mrs action:  {status['services']['mrs']['action']}")
    # Only redis should be healed; app/mrs deferred because redis is its dependency.
    assert healed == ["redis"], f"expected only redis healed, got {healed}"
    assert "deferred" in status["services"]["app"]["action"]
    assert "deferred" in status["services"]["mrs"]["action"]
    print("  PASS")


if __name__ == "__main__":
    test_real_restart()
    test_circuit_breaker()
    test_dependency_ordering()
    print("\nALL SELF-TESTS PASSED")
