"""Health-check primitives. Each returns a CheckResult(ok, detail).

Checks must be cheap, bounded by short timeouts, and never raise — a check that
blows up is treated as "unhealthy" with the exception text as the detail.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import redis

from . import config
from .env import Settings


@dataclass
class CheckResult:
    ok: bool
    detail: str = ""


def systemd_is_active(unit: str) -> bool:
    """True iff `systemctl is-active <unit>` reports active."""
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() == "active"
    except (subprocess.SubprocessError, OSError):
        return False


class Checker:
    """Holds shared, lazily-(re)created connections for the check functions."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._redis: Optional[redis.Redis] = None

    # --- Redis ---------------------------------------------------------------
    def redis_client(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.Redis(
                host=self.s.redis_host,
                port=self.s.redis_port,
                db=self.s.redis_db,
                password=self.s.redis_password,
                ssl=self.s.redis_ssl,
                socket_connect_timeout=5,
                socket_timeout=5,
                decode_responses=True,
            )
        return self._redis

    def check_redis(self) -> CheckResult:
        try:
            pong = self.redis_client().ping()
            return CheckResult(bool(pong), "PONG" if pong else "no PONG")
        except Exception as exc:  # noqa: BLE001 - any failure == down
            self._redis = None  # force reconnect next time
            return CheckResult(False, f"redis ping failed: {exc}")

    # --- Postgres ------------------------------------------------------------
    def check_postgres(self) -> CheckResult:
        if not self.s.database_url:
            return CheckResult(False, "DATABASE_URL not set")
        conn = None
        try:
            import psycopg2  # imported lazily so a missing driver degrades gracefully
            conn = psycopg2.connect(self.s.database_url, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return CheckResult(True, "SELECT 1 ok")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(False, f"postgres query failed: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # --- App HTTP ------------------------------------------------------------
    def check_http(self, url: str) -> CheckResult:
        """Any HTTP response < 500 means the app is serving (it has no /health,
        and `/` returns 200). A 5xx or no response counts as down."""
        try:
            import requests
            resp = requests.get(url, timeout=8, allow_redirects=False)
            ok = resp.status_code < 500
            return CheckResult(ok, f"HTTP {resp.status_code}")
        except Exception:  # noqa: BLE001 - fall back to a raw TCP connect
            return self._tcp_probe(url)

    @staticmethod
    def _tcp_probe(url: str) -> CheckResult:
        host, port = "127.0.0.1", 5000
        try:
            # crude parse of http://host:port/...
            rest = url.split("://", 1)[-1].split("/", 1)[0]
            if ":" in rest:
                host, port_s = rest.split(":", 1)
                port = int(port_s)
            else:
                host = rest
        except Exception:  # noqa: BLE001
            pass
        try:
            with socket.create_connection((host, port), timeout=5):
                return CheckResult(True, f"TCP {host}:{port} open")
        except OSError as exc:
            return CheckResult(False, f"TCP {host}:{port} closed: {exc}")

    # --- Worker / MRS heartbeats --------------------------------------------
    def _worker_health(self) -> dict[str, dict]:
        """Return {worker_id: health_dict} from the MRS-maintained Redis hash."""
        raw = self.redis_client().hgetall(config.WORKER_HEALTH_HASH)
        out: dict[str, dict] = {}
        for wid, blob in raw.items():
            try:
                out[wid] = json.loads(blob)
            except (TypeError, ValueError):
                continue
        return out

    def check_worker(self, worker_id: str) -> CheckResult:
        try:
            health = self._worker_health().get(worker_id)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(False, f"cannot read worker health: {exc}")
        if not health:
            return CheckResult(False, "no heartbeat recorded")
        age = time.time() - float(health.get("last_seen", 0))
        if age > config.WORKER_STALE_SECONDS:
            return CheckResult(False, f"stale heartbeat ({age:.0f}s old)")
        return CheckResult(True, f"heartbeat {age:.0f}s ago")

    def check_mrs(self) -> CheckResult:
        """MRS healthy = unit active AND it is still processing heartbeats.

        If the unit is active but the freshest heartbeat across the whole fleet
        is very old, MRS is wedged and a restart is warranted.
        """
        if not systemd_is_active("phonos-mrs.service"):
            return CheckResult(False, "unit not active")
        try:
            health = self._worker_health()
        except Exception as exc:  # noqa: BLE001
            return CheckResult(False, f"cannot read worker health: {exc}")
        if not health:
            # No workers known yet — can't conclude MRS is wedged. Unit is up.
            return CheckResult(True, "active (no workers registered)")
        freshest = min(
            (time.time() - float(h.get("last_seen", 0)) for h in health.values()),
            default=1e9,
        )
        if freshest > config.MRS_WEDGE_SECONDS:
            return CheckResult(
                False, f"wedged: freshest heartbeat {freshest:.0f}s old"
            )
        return CheckResult(True, f"active, freshest heartbeat {freshest:.0f}s ago")
