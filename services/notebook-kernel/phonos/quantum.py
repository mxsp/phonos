"""Quantum execution helpers for Phonos notebooks.

Honours the user's **Quantum Settings** (execution mode, backend,
credentials) which the platform injects into the kernel environment:

- ``PHONOS_QUANTUM_MODE``   — ``simulator`` (default) or ``hardware``
- ``PHONOS_QUANTUM_BACKEND`` — preferred backend name or ``automatic``
- ``IBM_QUANTUM_API_KEY`` / ``IBM_QUANTUM_INSTANCE`` — resolved credentials
  (personal or platform-managed, per the user's credential source setting)

Quick reference::

    import phonos
    from qiskit import QuantumCircuit

    qc = QuantumCircuit(2, 2)
    qc.h(0); qc.cx(0, 1); qc.measure([0, 1], [0, 1])

    # --- sampling ---------------------------------------------------------
    counts = phonos.quantum.run(qc, shots=1024)        # settings decide where
    counts = phonos.quantum.run(qc, mode='simulator')  # force local Aer
    counts = phonos.quantum.run(qc, mode='noisy')      # Aer + real-device noise model
    counts = phonos.quantum.run(qc, mode='hardware',   # force IBM hardware
                                dynamical_decoupling=True, twirling=True)

    # --- async hardware jobs (submit now, fetch later) ---------------------
    job = phonos.quantum.submit(qc, shots=4096)        # returns immediately
    job.status()                                       # 'QUEUED' / 'RUNNING' / 'DONE'
    counts = job.result()                              # blocks until done
    phonos.quantum.jobs()                              # jobs submitted from this notebook
    job = phonos.quantum.job('abc123')                 # re-attach after restart

    # --- expectation values (EstimatorV2) -----------------------------------
    ev = phonos.quantum.expval(circ, 'ZZ')             # exact (statevector)
    ev = phonos.quantum.expval(circ, 'ZZ', mode='hardware', resilience_level=1)

    # --- quantum chemistry ---------------------------------------------------
    res = phonos.quantum.vqe('H 0 0 0; H 0 0 0.735')   # ground-state energy (Hartree)

    phonos.quantum.mode()          # 'simulator' | 'hardware'
    phonos.quantum.backends()      # available IBM backends w/ queue + errors
    phonos.quantum.backend()       # resolved backend object
    phonos.quantum.service()       # QiskitRuntimeService (hardware)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Union

# Where async job handles are persisted so they survive kernel restarts and
# can be re-attached from a later session (the workspace dir is mounted rw).
_JOBS_FILE = os.environ.get('PHONOS_QUANTUM_JOBS_FILE', '/workspace/.phonos_quantum_jobs.json')

# Cache of noise-model simulators keyed by backend name (building one fetches
# live calibration data from IBM, ~seconds).
_NOISY_SIM_CACHE: Dict[str, Any] = {}


def mode() -> str:
    """The execution mode from the user's Quantum Settings."""
    value = (os.environ.get('PHONOS_QUANTUM_MODE') or 'simulator').lower()
    return 'hardware' if value == 'hardware' else 'simulator'


def backend_name() -> str:
    """The preferred backend name from the user's Quantum Settings."""
    return os.environ.get('PHONOS_QUANTUM_BACKEND') or 'automatic'


def service():
    """An authenticated ``QiskitRuntimeService``.

    Uses the account saved at kernel startup; falls back to explicit
    credentials from the environment. Tolerates the channel rename across
    qiskit-ibm-runtime versions.
    """
    from qiskit_ibm_runtime import QiskitRuntimeService

    try:
        return QiskitRuntimeService()
    except Exception:
        pass

    token = os.environ.get('IBM_QUANTUM_API_KEY')
    if not token:
        raise RuntimeError(
            "No IBM Quantum credentials available. Configure them in "
            "Quantum Settings (Account Settings or the Notebook sidebar)."
        )
    instance = os.environ.get('IBM_QUANTUM_INSTANCE') or None
    last_error: Optional[Exception] = None
    for channel in ('ibm_quantum_platform', 'ibm_cloud'):
        try:
            return QiskitRuntimeService(channel=channel, token=token, instance=instance)
        except Exception as e:
            last_error = e
            if 'channel' not in str(e).lower():
                raise
    raise last_error  # type: ignore[misc]


def _least_busy(svc):
    """Least-busy operational hardware backend, computed manually.

    ``QiskitRuntimeService.least_busy`` raises "No backend matches the
    criteria" on some runtime/API version combinations even when
    ``svc.backends()`` lists operational devices (seen on 0.44.x), so we do
    the obvious thing ourselves: filter operational non-simulators and pick
    the shortest queue.
    """
    best = None
    best_key = None
    for b in svc.backends():
        try:
            if getattr(b, 'simulator', False):
                continue
            st = b.status()
            if not getattr(st, 'operational', True):
                continue
            pending = int(getattr(st, 'pending_jobs', 1 << 30))
            # Prefer devices whose queue is actually executing ('active') over
            # paused/maintenance ones — matches the IBM dashboard state. A
            # paused device still accepts jobs (they wait until it resumes),
            # so it remains a valid fallback when nothing is active.
            msg = str(getattr(st, 'status_msg', '') or '').strip().lower()
            key = (0 if msg in ('', 'active') else 1, pending)
        except Exception:
            continue
        if best is None or key < best_key:
            best, best_key = b, key
    if best is None:
        raise RuntimeError(
            "No operational IBM backend is available for this account/instance."
        )
    return best


def backend(name: Optional[str] = None, run_mode: Optional[str] = None):
    """Resolve the execution backend per the user's settings.

    Simulator mode returns a local ``AerSimulator``; ``noisy`` returns an
    ``AerSimulator`` configured with the real device's current noise model;
    hardware mode returns the named IBM backend (or the least busy
    operational one).
    """
    effective_mode = (run_mode or mode())
    if effective_mode == 'noisy':
        return _noisy_simulator(name)
    if effective_mode != 'hardware':
        from qiskit_aer import AerSimulator
        return AerSimulator()

    svc = service()
    target = name or backend_name()
    if target and target != 'automatic':
        return svc.backend(target)
    return _least_busy(svc)


def _noisy_simulator(name: Optional[str] = None):
    """Local Aer simulator carrying a real backend's calibration noise model.

    Free "hardware rehearsal": gate errors, readout errors, and T1/T2 from
    the device's current calibration, with no queue and no quota. Requires
    IBM credentials (to fetch the calibration data).
    """
    from qiskit_aer import AerSimulator

    svc = service()
    target = name or backend_name()
    if not target or target == 'automatic':
        target = _least_busy(svc).name
    if target in _NOISY_SIM_CACHE:
        return _NOISY_SIM_CACHE[target]
    real = svc.backend(target)
    sim = AerSimulator.from_backend(real)
    _NOISY_SIM_CACHE[target] = sim
    return sim


def _apply_sampler_mitigation(sampler, dynamical_decoupling: bool, twirling: bool):
    """Best-effort enable of SamplerV2 error-suppression options."""
    try:
        if dynamical_decoupling:
            sampler.options.dynamical_decoupling.enable = True
            sampler.options.dynamical_decoupling.sequence_type = 'XY4'
        if twirling:
            sampler.options.twirling.enable_gates = True
            sampler.options.twirling.enable_measure = True
    except Exception as e:  # options tree varies across runtime versions
        print(f"Warning: could not set mitigation options: {e}")


def run(circuit, shots: int = 1024, mode: Optional[str] = None,
        backend_name: Optional[str] = None,
        dynamical_decoupling: bool = False,
        twirling: bool = False) -> Dict[str, int]:
    """Run a circuit where the user's Quantum Settings say it should run.

    Returns the measurement counts dict. ``mode`` (``simulator`` / ``noisy``
    / ``hardware``) and ``backend_name`` override the saved settings for this
    call only. ``dynamical_decoupling`` and ``twirling`` enable error
    suppression on the hardware path.
    """
    from qiskit import transpile

    effective_mode = mode if mode in ('simulator', 'noisy', 'hardware') else globals()['mode']()
    target = backend(name=backend_name, run_mode=effective_mode)

    if effective_mode != 'hardware':
        compiled = transpile(circuit, target)
        result = target.run(compiled, shots=shots).result()
        return result.get_counts()

    # Hardware path: modern IBM Runtime primitives.
    from qiskit_ibm_runtime import SamplerV2

    target_name = getattr(target, 'name', '')
    if callable(target_name):  # BackendV1 compat
        target_name = target_name()

    isa_circuit = _isa(circuit, target)
    sampler = SamplerV2(mode=target)
    _apply_sampler_mitigation(sampler, dynamical_decoupling, twirling)
    job = sampler.run([isa_circuit], shots=shots)
    print(f"Submitted to {target_name} (job {job.job_id()}), waiting for result...")
    _remember_job(job.job_id(), str(target_name), shots, blocking=True)
    result = job.result()
    return _counts_from_pub(result[0])


def _isa(circuit, target):
    """Transpile a circuit to the backend's ISA.

    Pins the built-in translator: some IBM backends advertise a default
    translation plugin ('ibm_dynamic_circuits') that isn't registered in
    every qiskit/qiskit-ibm-runtime combination, which raises
    TranspilerError. 'translator' is always available and correct for the
    static circuits used here.
    """
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
    pm = generate_preset_pass_manager(
        backend=target, optimization_level=1, translation_method="translator")
    return pm.run(circuit)


def _counts_from_pub(pub_result) -> Dict[str, int]:
    """Extract a counts dict from a SamplerV2 pub result."""
    data = pub_result.data
    for field in vars(data).keys():
        bits = getattr(data, field, None)
        if hasattr(bits, 'get_counts'):
            return bits.get_counts()
    raise RuntimeError("Could not extract counts from the sampler result")


# ---------------------------------------------------------------------------
# Async hardware jobs — submit now, fetch the result whenever (even from a
# later kernel session; handles are persisted in the notebook workspace).
# ---------------------------------------------------------------------------

class QuantumJob:
    """Handle to an IBM Runtime job submitted from a Phonos notebook.

    ``status()`` / ``done()`` poll without blocking; ``result()`` blocks
    until the job completes and returns the counts dict; ``cancel()``
    withdraws a queued job.
    """

    def __init__(self, job_id: str, backend: str = '', runtime_job=None):
        self.job_id = job_id
        self.backend = backend
        self._job = runtime_job
        self._counts: Optional[Dict[str, int]] = None

    def _attach(self):
        if self._job is None:
            self._job = service().job(self.job_id)
            try:
                self.backend = self.backend or self._job.backend().name
            except Exception:
                pass
        return self._job

    def status(self) -> str:
        """Live job status: QUEUED / RUNNING / DONE / ERROR / CANCELLED."""
        st = self._attach().status()
        return getattr(st, 'name', None) or str(st)

    def done(self) -> bool:
        return self.status() in ('DONE', 'ERROR', 'CANCELLED')

    def cancel(self) -> bool:
        try:
            self._attach().cancel()
            return True
        except Exception as e:
            print(f"Cancel failed: {e}")
            return False

    def result(self, timeout: Optional[float] = None) -> Dict[str, int]:
        """Block until the job completes; returns the counts dict."""
        if self._counts is not None:
            return self._counts
        result = self._attach().result(timeout=timeout)
        self._counts = _counts_from_pub(result[0])
        return self._counts

    def __repr__(self):
        try:
            st = self.status()
        except Exception:
            st = 'UNKNOWN'
        return f"QuantumJob(id='{self.job_id}', backend='{self.backend}', status='{st}')"


class _LocalJob(QuantumJob):
    """Completed pseudo-job wrapping a local simulator run, so code written
    against ``submit()`` works identically in simulator mode."""

    def __init__(self, counts: Dict[str, int], backend: str):
        super().__init__(job_id=f"local-{int(time.time()*1000)}", backend=backend)
        self._counts = counts

    def status(self) -> str:
        return 'DONE'

    def done(self) -> bool:
        return True

    def result(self, timeout: Optional[float] = None) -> Dict[str, int]:
        return self._counts or {}


def _remember_job(job_id: str, backend: str, shots: int, blocking: bool = False):
    """Append a job record to the workspace jobs file (best-effort)."""
    try:
        records = []
        if os.path.exists(_JOBS_FILE):
            with open(_JOBS_FILE) as f:
                records = json.load(f) or []
        records = [r for r in records if r.get('job_id') != job_id]
        records.append({
            'job_id': job_id,
            'backend': backend,
            'shots': shots,
            'blocking': blocking,
            'submitted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        })
        with open(_JOBS_FILE, 'w') as f:
            json.dump(records[-100:], f, indent=1)
    except Exception:
        pass  # persistence is convenience, never fail the run for it


def submit(circuit, shots: int = 1024, mode: Optional[str] = None,
           backend_name: Optional[str] = None,
           dynamical_decoupling: bool = False,
           twirling: bool = False) -> QuantumJob:
    """Submit a circuit and return immediately with a :class:`QuantumJob`.

    On hardware this does NOT wait for the queue — fetch the counts later
    with ``job.result()``, check progress with ``job.status()``, or re-attach
    from a future session via ``phonos.quantum.job(job_id)``. In simulator /
    noisy modes the run executes locally right away and the returned job is
    already complete (same code works everywhere).
    """
    effective_mode = mode if mode in ('simulator', 'noisy', 'hardware') else globals()['mode']()

    if effective_mode != 'hardware':
        counts = run(circuit, shots=shots, mode=effective_mode, backend_name=backend_name)
        return _LocalJob(counts, backend=effective_mode)

    from qiskit_ibm_runtime import SamplerV2

    target = backend(name=backend_name, run_mode='hardware')
    target_name = getattr(target, 'name', '')
    if callable(target_name):
        target_name = target_name()

    isa_circuit = _isa(circuit, target)
    sampler = SamplerV2(mode=target)
    _apply_sampler_mitigation(sampler, dynamical_decoupling, twirling)
    rt_job = sampler.run([isa_circuit], shots=shots)
    job_id = rt_job.job_id()
    _remember_job(job_id, str(target_name), shots)
    print(f"Submitted to {target_name}: job {job_id} (use phonos.quantum.job('{job_id}') to re-attach)")
    return QuantumJob(job_id, backend=str(target_name), runtime_job=rt_job)


def job(job_id: str) -> QuantumJob:
    """Re-attach to a previously submitted hardware job by its ID."""
    return QuantumJob(job_id)


def jobs() -> List[Dict[str, Any]]:
    """Jobs submitted from this notebook's workspace (most recent last).

    Records persist across kernel restarts. Use ``phonos.quantum.job(id)``
    on any entry to re-attach and fetch its result.
    """
    try:
        if os.path.exists(_JOBS_FILE):
            with open(_JOBS_FILE) as f:
                return json.load(f) or []
    except Exception as e:
        print(f"Could not read jobs file: {e}")
    return []


# ---------------------------------------------------------------------------
# Expectation values — EstimatorV2 across simulator / noisy / hardware.
# ---------------------------------------------------------------------------

def _as_observable(observable):
    """Normalize str / list / SparsePauliOp inputs to SparsePauliOp."""
    from qiskit.quantum_info import SparsePauliOp
    if isinstance(observable, SparsePauliOp):
        return observable
    if isinstance(observable, str):
        return SparsePauliOp(observable)
    if isinstance(observable, (list, tuple)):
        # [("ZZ", 0.5), ("XI", -1.0)] or ["ZZ", "XX"]
        if observable and isinstance(observable[0], (list, tuple)):
            return SparsePauliOp.from_list(list(observable))
        return SparsePauliOp(list(observable))
    return SparsePauliOp(observable)


def expval(circuit, observable, mode: Optional[str] = None,
           backend_name: Optional[str] = None,
           shots: int = 4096,
           resilience_level: Optional[int] = None,
           zne: bool = False,
           dynamical_decoupling: bool = False,
           twirling: bool = False) -> float:
    """Expectation value ⟨ψ|O|ψ⟩ of ``observable`` for the circuit's state.

    ``observable`` may be a Pauli string ("ZZ"), a list of (pauli, coeff)
    pairs, or a ``SparsePauliOp``. Simulator mode computes the exact value
    from the statevector; ``noisy`` estimates it under the device noise
    model; ``hardware`` runs IBM Runtime EstimatorV2 with optional error
    mitigation (``resilience_level`` 0–2, ``zne``) and suppression
    (``dynamical_decoupling``, ``twirling``).
    """
    obs = _as_observable(observable)
    effective_mode = mode if mode in ('simulator', 'noisy', 'hardware') else globals()['mode']()

    # The estimator path needs a measurement-free circuit.
    circ = circuit.remove_final_measurements(inplace=False)

    if effective_mode == 'simulator':
        from qiskit.primitives import StatevectorEstimator
        est = StatevectorEstimator()
        res = est.run([(circ, obs)]).result()
        return float(res[0].data.evs)

    if effective_mode == 'noisy':
        from qiskit import transpile
        from qiskit_aer.primitives import EstimatorV2 as AerEstimatorV2
        sim = _noisy_simulator(backend_name)
        compiled = transpile(circ, sim)
        mapped = obs.apply_layout(compiled.layout) if compiled.layout else obs
        est = AerEstimatorV2.from_backend(sim) if hasattr(AerEstimatorV2, 'from_backend') else AerEstimatorV2()
        res = est.run([(compiled, mapped)]).result()
        return float(res[0].data.evs)

    # Hardware: IBM Runtime EstimatorV2 on ISA circuit + layout-mapped obs.
    from qiskit_ibm_runtime import EstimatorV2

    target = backend(name=backend_name, run_mode='hardware')
    isa_circuit = _isa(circ, target)
    mapped = obs.apply_layout(isa_circuit.layout) if isa_circuit.layout else obs

    est = EstimatorV2(mode=target)
    try:
        est.options.default_shots = shots
        if resilience_level is not None:
            est.options.resilience_level = int(resilience_level)
        if zne:
            est.options.resilience.zne_mitigation = True
        if dynamical_decoupling:
            est.options.dynamical_decoupling.enable = True
        if twirling:
            est.options.twirling.enable_gates = True
    except Exception as e:
        print(f"Warning: could not set estimator options: {e}")

    ej = est.run([(isa_circuit, mapped)])
    print(f"Estimator job {ej.job_id()} submitted, waiting for result...")
    res = ej.result()
    return float(res[0].data.evs)


def backends() -> List[Dict[str, Any]]:
    """Available IBM backends with live queue depth and calibration summary."""
    svc = service()
    out: List[Dict[str, Any]] = []
    for b in svc.backends():
        info: Dict[str, Any] = {'name': b.name}
        try:
            info['num_qubits'] = b.num_qubits
        except Exception:
            pass
        try:
            st = b.status()
            info['operational'] = bool(getattr(st, 'operational', True))
            info['pending_jobs'] = int(getattr(st, 'pending_jobs', -1))
            msg = str(getattr(st, 'status_msg', '') or '').strip().lower()
            if msg:
                info['status_msg'] = msg  # 'active' | 'paused' | 'maintenance' | ...
        except Exception:
            pass
        try:
            props = b.properties()
            if props:
                import statistics
                ro = [props.readout_error(q) for q in range(min(b.num_qubits, 16))]
                info['median_readout_error'] = round(statistics.median(ro), 5)
        except Exception:
            pass
        out.append(info)
    out.sort(key=lambda i: i.get('pending_jobs', 1 << 30))
    return out


# ---------------------------------------------------------------------------
# Quantum chemistry — molecular ground-state energy via VQE.
# ---------------------------------------------------------------------------

def vqe(atom: str = 'H 0 0 0; H 0 0 0.735', basis: str = 'sto3g',
        charge: int = 0, spin: int = 0,
        maxiter: int = 200,
        optimizer: str = 'COBYLA',
        verbose: bool = True) -> Dict[str, Any]:
    """Compute a molecule's electronic ground-state energy with VQE.

    Builds the molecular Hamiltonian with PySCF, maps it to qubits
    (Jordan–Wigner), prepares a Hartree–Fock + UCCSD ansatz, and minimizes
    the energy with a classical optimizer against the exact statevector
    estimator. For small systems the exact (FCI) energy is also computed by
    diagonalization for comparison.

    Args:
        atom:   molecule geometry in PySCF format, e.g. ``'H 0 0 0; H 0 0 0.735'``
                (Ångström) or ``'Li 0 0 0; H 0 0 1.6'``.
        basis:  basis set (default ``sto3g``).
        charge / spin: molecular charge and 2S (alpha-beta electron count).
        maxiter: classical optimizer iteration cap.
        optimizer: any scipy.optimize method name (COBYLA, SLSQP, ...).

    Returns a dict with ``total_energy`` (Hartree), ``electronic_energy``,
    ``nuclear_repulsion``, ``hf_energy``, ``exact_energy`` (when feasible),
    ``num_qubits``, ``iterations``, and the optimal parameters.
    """
    try:
        from qiskit_nature.second_q.drivers import PySCFDriver
        from qiskit_nature.second_q.mappers import JordanWignerMapper
        from qiskit_nature.second_q.circuit.library import HartreeFock, UCCSD
    except ImportError:
        raise RuntimeError(
            "Quantum chemistry packages are not installed in this kernel "
            "(qiskit-nature + pyscf). Restart the kernel on an updated "
            "runtime image, or ask an admin to rebuild phonos/notebook-kernel."
        )
    import numpy as np
    from scipy.optimize import minimize
    from qiskit.primitives import StatevectorEstimator

    t0 = time.monotonic()
    driver = PySCFDriver(atom=atom, basis=basis, charge=charge, spin=spin)
    problem = driver.run()
    mapper = JordanWignerMapper()
    hamiltonian = mapper.map(problem.hamiltonian.second_q_op())
    nuclear_repulsion = float(problem.nuclear_repulsion_energy or 0.0)

    hf_state = HartreeFock(problem.num_spatial_orbitals, problem.num_particles, mapper)
    ansatz = UCCSD(problem.num_spatial_orbitals, problem.num_particles, mapper,
                   initial_state=hf_state)

    estimator = StatevectorEstimator()
    iterations = {'n': 0}

    def energy(params):
        iterations['n'] += 1
        res = estimator.run([(ansatz, hamiltonian, params)]).result()
        e = float(res[0].data.evs)
        if verbose and iterations['n'] % 10 == 0:
            print(f"  iter {iterations['n']:>4}: E = {e + nuclear_repulsion:.8f} Ha")
        return e

    x0 = np.zeros(ansatz.num_parameters)
    if verbose:
        print(f"VQE: {ansatz.num_qubits} qubits, {ansatz.num_parameters} parameters "
              f"({optimizer}, maxiter={maxiter})")
    opt = minimize(energy, x0, method=optimizer, options={'maxiter': maxiter})

    electronic = float(opt.fun)
    total = electronic + nuclear_repulsion

    exact_total = None
    if hamiltonian.num_qubits <= 12:
        try:
            eigvals = np.linalg.eigvalsh(hamiltonian.to_matrix())
            exact_total = float(eigvals[0]) + nuclear_repulsion
        except Exception:
            pass

    hf_energy = None
    try:
        hf_energy = float(problem.reference_energy)
    except Exception:
        pass

    result = {
        'total_energy': total,
        'electronic_energy': electronic,
        'nuclear_repulsion': nuclear_repulsion,
        'hf_energy': hf_energy,
        'exact_energy': exact_total,
        'num_qubits': int(hamiltonian.num_qubits),
        'num_parameters': int(ansatz.num_parameters),
        'iterations': iterations['n'],
        'converged': bool(getattr(opt, 'success', False)),
        'optimal_params': [float(v) for v in opt.x],
        'seconds': round(time.monotonic() - t0, 2),
        'hamiltonian': hamiltonian,
        'ansatz': ansatz,
    }
    if verbose:
        print(f"VQE total energy   : {total:.8f} Ha")
        if exact_total is not None:
            print(f"Exact (FCI) energy : {exact_total:.8f} Ha   "
                  f"(error {abs(total - exact_total):.2e} Ha)")
        if hf_energy is not None:
            print(f"Hartree-Fock energy: {hf_energy:.8f} Ha")
        print(f"({result['num_qubits']} qubits, {iterations['n']} evaluations, {result['seconds']}s)")
    return result


def status() -> Dict[str, Any]:
    """Summary of the effective quantum execution configuration."""
    return {
        'mode': mode(),
        'backend': backend_name(),
        'has_credentials': bool(os.environ.get('IBM_QUANTUM_API_KEY')),
        'instance': os.environ.get('IBM_QUANTUM_INSTANCE') or None,
    }


def help() -> str:  # noqa: A001 - deliberate SDK surface name
    return __doc__ or ''
