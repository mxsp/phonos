"""Classical HPC compute clusters for Phonos notebooks (experimental).

Named clusters of containerized workers — running semi-externally on any
machine that can reach the Phonos API — execute Python compute tasks with
numpy/scipy/pandas available. Mirrors the quantum SDK ergonomics:

    import phonos

    phonos.hpc.clusters()                     # what's available + online?

    c = phonos.hpc.cluster('cluster1')        # explicit handle
    task = c.compute('a + b', a=1, b=2)       # submit; returns HpcTask
    task.result()                             # block until done -> 3

    # attribute sugar, both styles:
    phonos.hpc.cluster1.compute('np.linalg.norm(v)', v=[3, 4]).result()
    phonos.hpc_cluster1.compute('a * b', a=6, b=7).result()

Task semantics:
- ``code`` is Python source executed on the worker; keyword arguments become
  variables in the task namespace; ``np`` (numpy) is pre-imported.
- The value of the LAST expression (or an explicit ``result = ...``
  assignment) is returned. Results must be JSON-representable — numpy arrays
  and scalars are converted automatically, anything else falls back to
  ``repr()``.
- Tasks queue server-side; workers pull them over an authenticated outbound
  HTTPS connection (no inbound ports on the worker machine).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class HpcError(RuntimeError):
    """Raised when an HPC task fails or the cluster is unavailable."""


def _api(method: str, endpoint: str, data: Optional[dict] = None,
         params: Optional[dict] = None, timeout: float = 60.0):
    from phonos import _api_request, PhonosError  # lazy: avoid circular import
    try:
        return _api_request(method, endpoint, data=data, params=params, timeout=timeout)
    except PhonosError as e:
        raise HpcError(str(e)) from None


class HpcTask:
    """Handle to a submitted compute task."""

    def __init__(self, task_uuid: str, cluster: str = ''):
        self.task_uuid = task_uuid
        self.cluster = cluster
        self._final: Optional[dict] = None

    def _fetch(self) -> dict:
        if self._final is not None:
            return self._final
        data = _api('GET', f'/api/hpc/tasks/{self.task_uuid}') or {}
        if data.get('status') in ('done', 'error', 'cancelled'):
            self._final = data
        return data

    def status(self) -> str:
        """'queued' | 'running' | 'done' | 'error' | 'cancelled'."""
        return self._fetch().get('status', 'unknown')

    def done(self) -> bool:
        return self.status() in ('done', 'error', 'cancelled')

    def stdout(self) -> str:
        """Text printed by the task (available once finished)."""
        return self._fetch().get('stdout') or ''

    def cancel(self) -> bool:
        try:
            _api('POST', f'/api/hpc/tasks/{self.task_uuid}/cancel')
            return True
        except HpcError:
            return False

    def result(self, timeout: float = 600.0, poll_interval: float = 1.0) -> Any:
        """Block until the task finishes; return its result value.

        Raises :class:`HpcError` on task failure, cancellation, or timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            data = self._fetch()
            status = data.get('status')
            if status == 'done':
                out = data.get('stdout')
                if out:
                    print(out, end='' if out.endswith('\n') else '\n')
                return data.get('result')
            if status == 'error':
                raise HpcError(data.get('error') or 'HPC task failed')
            if status == 'cancelled':
                raise HpcError('HPC task was cancelled')
            if time.monotonic() >= deadline:
                raise HpcError(
                    f"Task {self.task_uuid} did not finish within {timeout}s "
                    f"(status: {status}). It keeps running server-side — "
                    f"re-attach with phonos.hpc.task('{self.task_uuid}')."
                )
            time.sleep(poll_interval)

    def __repr__(self):
        try:
            st = self.status()
        except Exception:
            st = 'unknown'
        return f"HpcTask(id='{self.task_uuid}', cluster='{self.cluster}', status='{st}')"


class HpcCluster:
    """Handle to a named HPC cluster: ``phonos.hpc.cluster('cluster1')``."""

    def __init__(self, name: str):
        self.name = name

    def compute(self, code: str, timeout: int = 300, **args) -> HpcTask:
        """Submit Python ``code`` for execution on this cluster.

        Keyword arguments become variables in the task namespace::

            task = cluster.compute('a + b', a=1, b=2)
            task.result()   # -> 3
        """
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty Python source string")
        data = _api('POST', f'/api/hpc/clusters/{self.name}/tasks', data={
            'code': code,
            'args': args,
            'timeout': timeout,
        })
        if not data.get('cluster_online', True):
            print(f"Note: cluster '{self.name}' has no worker online right now — "
                  f"the task is queued and runs when one connects.")
        return HpcTask(data['task_uuid'], cluster=self.name)

    def status(self) -> Dict[str, Any]:
        """Live info for this cluster (online, queue depth, capabilities)."""
        for c in clusters():
            if c.get('name') == self.name:
                return c
        raise HpcError(f"Unknown HPC cluster '{self.name}'")

    def __repr__(self):
        try:
            st = self.status()
            state = 'online' if st.get('online') else 'offline'
            return (f"HpcCluster(name='{self.name}', {state}, "
                    f"queued={st.get('queued', 0)}, running={st.get('running', 0)})")
        except Exception:
            return f"HpcCluster(name='{self.name}')"


def clusters() -> List[Dict[str, Any]]:
    """Available HPC clusters with online status and queue depth."""
    data = _api('GET', '/api/hpc/clusters') or {}
    return data.get('clusters', [])


def cluster(name: str) -> HpcCluster:
    """Handle for a named cluster (does not verify existence until used)."""
    return HpcCluster(str(name))


def task(task_uuid: str) -> HpcTask:
    """Re-attach to a previously submitted task by its id."""
    return HpcTask(task_uuid)


def help() -> str:  # noqa: A001 - deliberate SDK surface name
    return __doc__ or ''


def __getattr__(name: str):
    """Attribute sugar: ``phonos.hpc.cluster1`` -> HpcCluster('cluster1')."""
    if name.startswith('_'):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return HpcCluster(name)
