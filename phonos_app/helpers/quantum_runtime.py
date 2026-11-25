"""Quantum circuit execution dispatcher — simulator or real IBM hardware.

This module implements the Runtime Selection Logic from the Quantum Settings
spec:

    Job Submitted → Check User Settings → Use Personal Credentials?
      → (Yes) User Key / (No) Platform Managed Key
      → Select Mode (Simulator / Hardware)
      → Execute Job

**Security**: plaintext API keys are used locally to create the IBM Runtime
service object and are immediately discarded; they are never logged,
serialized, returned from API endpoints, or included in exception messages.

**Note on availability**: qiskit-aer and qiskit-ibm-runtime are imported
lazily so the app can start without them installed locally (tests, lint).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from phonos_app.helpers.quantum_user_creds import (
    resolve_credentials,
    get_settings,
    DEFAULT_SESSION_TIMEOUT,
    MIN_SESSION_TIMEOUT,
    MAX_SESSION_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Lazily-imported module handles
_QISKIT = None
_AER = None
_IBM_RUNTIME = None


def _import_qiskit():
    global _QISKIT
    if _QISKIT is None:
        import qiskit as _QISKIT
    return _QISKIT


def _import_aer():
    global _AER
    if _AER is None:
        from qiskit_aer import AerSimulator as _AER
    return _AER


def _import_ibm_runtime():
    global _IBM_RUNTIME
    if _IBM_RUNTIME is None:
        from qiskit_ibm_runtime import QiskitRuntimeService, Session, SamplerV2
        _IBM_RUNTIME = (QiskitRuntimeService, Session, SamplerV2)
    return _IBM_RUNTIME


def _least_busy_backend(service):
    """Least-busy operational hardware backend, computed manually.

    ``QiskitRuntimeService.least_busy`` raises "No backend matches the
    criteria" on some runtime/API version combinations even when
    ``service.backends()`` lists operational devices (seen on 0.44.x).
    """
    best = None
    best_pending = None
    for b in service.backends():
        try:
            if getattr(b, "simulator", False):
                continue
            st = b.status()
            if not getattr(st, "operational", True):
                continue
            pending = int(getattr(st, "pending_jobs", 1 << 30))
        except Exception:
            continue
        if best is None or pending < best_pending:
            best, best_pending = b, pending
    if best is None:
        raise RuntimeError("No operational IBM backend is available for this account/instance.")
    return best


def connect_runtime_service(api_key: str, instance: Optional[str] = None):
    """Open a QiskitRuntimeService connection, tolerating channel renames.

    qiskit-ibm-runtime renamed the channel across versions
    ('ibm_cloud' → 'ibm_quantum_platform'); try both so the platform works
    with whichever library version is installed.
    """
    QiskitRuntimeService = _import_ibm_runtime()[0]
    last_error = None
    for channel in ("ibm_quantum_platform", "ibm_cloud"):
        try:
            return QiskitRuntimeService(channel=channel, token=api_key, instance=instance)
        except Exception as e:  # invalid channel name raises immediately
            last_error = e
            if "channel" not in str(e).lower():
                raise
    raise last_error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class QuantumExecutionResult:
    """Result of a quantum circuit execution."""
    def __init__(self, counts: dict, shots: int, backend: str, mode: str,
                 session_state: str, time_remaining: Optional[int] = None,
                 error: Optional[str] = None):
        self.counts = counts
        self.shots = shots
        self.backend = backend
        self.mode = mode
        self.session_state = session_state
        self.time_remaining = time_remaining
        self.error = error

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "shots": self.shots,
            "backend": self.backend,
            "mode": self.mode,
            "session_state": self.session_state,
            "time_remaining": self.time_remaining,
            "error": self.error,
        }


def execute_circuit(circuit, user_id: int, shots: int = 1024) -> QuantumExecutionResult:
    """Execute a quantum circuit for a given user, respecting their settings.

    ``circuit`` — any object with a ``measure_all_inplace()`` method or a
    plain ``QuantumCircuit`` from ``qiskit``.

    The resolution flow follows the spec flowchart:
    ``resolve_credentials`` → ``select_backend`` → execute.
    """
    qk = _import_qiskit()
    aer = _import_aer()

    settings = get_settings(user_id)
    mode = settings["execution_mode"]
    pref_backend = settings["preferred_backend"]
    auto_session = settings["auto_start_session"]
    timeout = max(MIN_SESSION_TIMEOUT, min(MAX_SESSION_TIMEOUT,
                                           settings["session_timeout_sec"]))

    # --- Resolve credentials ---
    api_key, source, instance, resolved_backend = resolve_credentials(user_id)
    if source == "none":
        return QuantumExecutionResult(
            counts={}, shots=0, backend=pref_backend, mode=mode,
            session_state="Inactive",
            error="No credentials available. Configure personal IBM key or check platform env."
        )

    selected_backend = resolved_backend or pref_backend or "automatic"

    if mode == "simulator":
        return _execute_simulator(circuit, qk, aer, selected_backend, shots)
    else:
        return _execute_hardware(circuit, qk, api_key, instance, selected_backend,
                                  shots, source, auto_session, timeout)


def get_runtime_status(user_id: int) -> dict:
    """Return the Runtime Status Panel data for a user."""
    settings = get_settings(user_id)
    _, source, _, resolved_backend = resolve_credentials(user_id)

    if source == "none":
        cred_source = "None"
    elif source == "platform":
        cred_source = "Platform Managed"
    else:
        cred_source = "Personal"

    backend = resolved_backend or settings["preferred_backend"] or "automatic"

    # Session is currently inactive (we don't persist session handles)
    status = {
        "execution_mode": settings["execution_mode"].capitalize(),
        "credential_source": cred_source,
        "backend": backend,
        "runtime_session": "Inactive",
        "time_remaining": None,
    }
    return status


# Live backend list cache: querying IBM takes seconds and the queue numbers
# only need to be fresh-ish. Keyed by credential fingerprint so switching
# between personal and platform credentials doesn't serve stale data.
_BACKENDS_CACHE: dict = {"key": None, "at": 0.0, "data": None}
# Ask IBM at most once every 5 minutes — queue depth and paused/maintenance
# states don't change faster than that in practice.
_BACKENDS_CACHE_TTL = 300.0

_STATIC_BACKENDS = {
    "simulator": [
        {"id": "aer_simulator", "label": "Aer Simulator"},
        {"id": "local_testing", "label": "Local Testing Mode"},
    ],
    "hardware": [
        {"id": "ibm_fez", "label": "IBM fez"},
        {"id": "ibm_marrakesh", "label": "IBM marrakesh"},
        {"id": "ibm_kingston", "label": "IBM kingston"},
    ],
    "live": False,
}


def get_available_backends(user_id: Optional[int] = None, refresh: bool = False) -> dict:
    """Backends available to the user, with live IBM data when possible.

    When the user's credentials resolve, the hardware list is fetched live
    from IBM and each entry carries ``num_qubits``, ``pending_jobs`` (queue
    depth), and ``operational`` so the Settings UI can render an informed
    picker (sorted least-busy first). Results are cached for
    ``_BACKENDS_CACHE_TTL`` seconds. Without credentials — or if IBM is
    unreachable — the static fallback list is returned with ``live: False``.
    """
    import time as _time

    api_key = instance = None
    if user_id is not None:
        try:
            api_key, _source, instance, _pref = resolve_credentials(user_id)
        except Exception as e:
            logger.warning("Backend list: could not resolve credentials: %s", e)

    if not api_key:
        return dict(_STATIC_BACKENDS)

    cache_key = (api_key[:12], instance)
    now = _time.monotonic()
    if (not refresh and _BACKENDS_CACHE["data"] is not None
            and _BACKENDS_CACHE["key"] == cache_key
            and now - _BACKENDS_CACHE["at"] < _BACKENDS_CACHE_TTL):
        return _BACKENDS_CACHE["data"]

    try:
        service = connect_runtime_service(api_key, instance)
        hardware = []
        for b in service.backends():
            entry = {"id": b.name, "label": f"IBM {b.name.removeprefix('ibm_')}"}
            try:
                entry["num_qubits"] = int(b.num_qubits)
            except Exception:
                pass
            try:
                st = b.status()
                entry["pending_jobs"] = int(getattr(st, "pending_jobs", -1))
                entry["operational"] = bool(getattr(st, "operational", True))
                # IBM's dashboard shows "paused"/"maintenance" for backends
                # that are operational but not executing jobs — surface it so
                # Phonos matches what the IBM site displays.
                msg = str(getattr(st, "status_msg", "") or "").strip().lower()
                if msg:
                    entry["status_msg"] = msg
            except Exception:
                pass
            hardware.append(entry)
        hardware.sort(key=lambda e: e.get("pending_jobs", 1 << 30))
        data = {
            "simulator": _STATIC_BACKENDS["simulator"],
            "hardware": hardware or _STATIC_BACKENDS["hardware"],
            "live": bool(hardware),
        }
        _BACKENDS_CACHE.update(key=cache_key, at=now, data=data)
        return data
    except Exception as e:
        logger.warning("Live backend query failed, using static list: %s", e)
        return dict(_STATIC_BACKENDS)


# ---------------------------------------------------------------------------
# Internal dispatchers
# ---------------------------------------------------------------------------

def _execute_simulator(circuit, qk, aer, selected_backend: str, shots: int) -> QuantumExecutionResult:
    """Execute the circuit on the local Aer simulator."""
    try:
        backend = aer()
        compiled = qk.transpile(circuit, backend)
        result = backend.run(compiled, shots=shots).result()
        counts = result.get_counts()
        return QuantumExecutionResult(
            counts=counts, shots=shots,
            backend=selected_backend if selected_backend != "automatic" else "aer_simulator",
            mode="Simulator",
            session_state="Inactive",
        )
    except Exception as e:
        logger.error("Simulator execution failed: %s", e)
        return QuantumExecutionResult(
            counts={}, shots=0, backend=selected_backend, mode="Simulator",
            session_state="Inactive",
            error=f"Simulator error: {_sanitize_message(e)}",
        )


def _execute_hardware(circuit, qk, api_key: str, instance: Optional[str],
                      selected_backend: str, shots: int, source: str,
                      auto_session: bool, timeout: int) -> QuantumExecutionResult:
    """Execute the circuit on real IBM Quantum hardware via SamplerV2.

    Uses the modern IBM Runtime primitives API: circuits are transpiled with
    a preset pass manager for the chosen backend and dispatched through
    ``SamplerV2`` (``backend.run`` was removed from IBM Runtime backends).
    When ``auto_session`` is enabled the sampler runs inside a bounded
    Runtime session; otherwise it uses per-job mode.
    """
    _, Session, SamplerV2 = _import_ibm_runtime()

    try:
        service = connect_runtime_service(api_key, instance)

        if selected_backend != "automatic":
            backend = service.backend(selected_backend)
        else:
            backend = _least_busy_backend(service)

        backend_name = backend.name

        from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
        # Pin the built-in translator: some IBM backends advertise a default
        # translation plugin ('ibm_dynamic_circuits') that isn't registered in
        # every qiskit/qiskit-ibm-runtime combination and raises TranspilerError.
        pm = generate_preset_pass_manager(
            backend=backend, optimization_level=1, translation_method="translator")
        isa_circuit = pm.run(circuit)

        if auto_session:
            with Session(backend=backend, max_time=timeout) as session:
                sampler = SamplerV2(mode=session)
                job = sampler.run([isa_circuit], shots=shots)
                result = job.result()
        else:
            sampler = SamplerV2(mode=backend)
            job = sampler.run([isa_circuit], shots=shots)
            result = job.result()

        counts = _extract_counts(result, circuit)
        return QuantumExecutionResult(
            counts=counts, shots=shots,
            backend=backend_name,
            mode="Real Hardware",
            session_state="Active" if auto_session else "Inactive",
            time_remaining=timeout if auto_session else None,
        )
    except Exception as e:
        logger.error("Hardware execution failed: %s", e)
        return QuantumExecutionResult(
            counts={}, shots=0, backend=selected_backend,
            mode="Real Hardware",
            session_state="Error",
            error=f"Hardware execution error: {_sanitize_message(e)}",
        )


def _extract_counts(result, circuit=None) -> dict:
    """Extract counts from a qiskit result object.

    Handles ``PrimitiveResult`` (SamplerV2), legacy ``SamplerResult``
    (quasi_dists), and ``AerResult`` (get_counts).
    """
    # SamplerV2 PrimitiveResult: result[0].data.<creg>.get_counts()
    try:
        pub = result[0]
        data = pub.data
        # Take the first classical register's BitArray (usually 'meas' or 'c')
        for field in getattr(data, "keys", lambda: [])() or vars(data).keys():
            bits = getattr(data, field, None)
            if hasattr(bits, "get_counts"):
                return bits.get_counts()
    except (TypeError, IndexError, AttributeError):
        pass
    if hasattr(result, "quasi_dists"):
        # Legacy SamplerResult — use first quasi-dist, format keys with the
        # circuit's classical bit width when known.
        qd = result.quasi_dists[0]
        if not qd:
            return {}
        width = getattr(circuit, "num_clbits", 0) or max(qd).bit_length() or 1
        return {f"{k:0{width}b}": round(float(v), 4) for k, v in qd.items()}
    if hasattr(result, "get_counts"):
        return result.get_counts()
    return {}


def _sanitize_message(exc: Exception) -> str:
    """Strip any credential-bearing content from an exception message.

    The sanitized message is safe to return to the frontend or log.
    Only the first line of the message is used; any credential-like
    tokens are replaced with ``<redacted>``.
    """
    msg = str(exc).split("\n")[0].strip() if str(exc) else type(exc).__name__
    # Remove anything that looks like an API key (alphanumeric with dashes/underscores,
    # 32+ chars or containing common key patterns)
    import re
    msg = re.sub(r'[A-Za-z0-9_-]{32,}', '<redacted>', msg)
    msg = re.sub(r'token[=:]\s*\S+', 'token=<redacted>', msg, flags=re.IGNORECASE)
    return msg[:200]