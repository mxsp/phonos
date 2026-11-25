"""
Phonos SDK for Notebooks - AI Model Access Library

Quick start in a notebook cell::

    import phonos

    # List available models
    phonos.list_models()                # all catalog models
    phonos.list_online_models()         # only currently online
    phonos.get_model('boltz2')          # details (parameters, credit cost, ...)

    # Submit a job (two equivalent forms)
    job = phonos.boltz2.run(sequence='MKTIIALSYIFCLVAD')
    job = phonos.submit_job('boltz2', sequence='MKTIIALSYIFCLVAD')
    job.wait()                          # block until done; True on success

    # Download outputs
    out = phonos.download_output(job)               # primary output file
    phonos.download_artifact(job, 'structure.cif')  # named artifact
    files = phonos.list_outputs(job)                # all artifacts

    # Inspect
    info = phonos.get_job_info(job)
    print(info.status, info.credits_used)

    # User / project / credits
    me   = phonos.get_user_info()
    proj = phonos.list_projects()

Per-model facade ``phonos.<short_name>.run(input, **kwargs)`` is the uniform
entry point. Hyphenated models (``esmfold-re``, ``alphafold2-re``, ...) are
also reachable via snake_case aliases (``phonos.esmfold_re.run(...)``).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import requests


# ----------------------------------------------------------------------------
# Configuration / auth context (populated by the kernel manager)
# ----------------------------------------------------------------------------

_BASE_URL = os.environ.get('PHONOS_API_URL', 'http://localhost:5000').rstrip('/')
_PROJECT_ID = os.environ.get('NOTEBOOK_PROJECT_ID')
_SESSION_TOKEN = (
    os.environ.get('NOTEBOOK_SESSION_ID')
    or os.environ.get('NOTEBOOK_SESSION_TOKEN')
)


# ----------------------------------------------------------------------------
# Enums / dataclasses
# ----------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING = 'pending'
    SUBMITTED = 'submitted'
    RUNNING = 'running'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)

    @property
    def is_success(self) -> bool:
        return self is JobStatus.COMPLETED


@dataclass
class ModelInfo:
    """Public description of a catalog model."""
    id: str
    name: str
    short_name: str
    description: str
    task: str
    input_modes: List[str]
    output_extension: str
    available: bool
    is_active: bool
    credit_cost: int
    outline_color: str
    node_id: Optional[str] = None
    badge: Optional[str] = None
    badge_color: Optional[str] = None
    custom_fields: List[Dict[str, Any]] = field(default_factory=list)
    rate_limit: Optional[Dict[str, Any]] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def parameters(self) -> List[Dict[str, Any]]:
        """User-facing parameters derived from ``custom_fields`` (may be empty)."""
        return list(self.custom_fields or [])

    @property
    def required_parameters(self) -> List[str]:
        return [f['name'] for f in self.custom_fields if f.get('required')]

    def get_parameter(self, name: str) -> Optional[Dict[str, Any]]:
        for f in self.custom_fields or []:
            if f.get('name') == name:
                return f
        return None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ModelInfo':
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            short_name=data.get('short_name', ''),
            description=data.get('description', ''),
            task=data.get('task', ''),
            input_modes=list(data.get('input_modes') or []),
            output_extension=data.get('output_extension', ''),
            available=bool(data.get('available', True)),
            is_active=bool(data.get('is_active', True)),
            credit_cost=int(data.get('credit_cost') or 0),
            outline_color=data.get('outline_color', 'gray'),
            node_id=data.get('node_id'),
            badge=data.get('badge'),
            badge_color=data.get('badge_color'),
            custom_fields=list(data.get('custom_fields') or []),
            rate_limit=data.get('rate_limit'),
            raw=data,
        )


@dataclass
class Artifact:
    filename: str
    display_name: str
    size: int
    format: str
    type: str = 'output'
    url: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Artifact':
        return cls(
            filename=data.get('filename', ''),
            display_name=data.get('display_name', data.get('filename', '')),
            size=int(data.get('size') or 0),
            format=data.get('format', ''),
            type=data.get('type', 'output'),
            url=data.get('url'),
        )


@dataclass
class Job:
    job_id: str
    model: str
    name: Optional[str]
    status: str
    created_at: datetime
    updated_at: Optional[datetime]
    message: Optional[str]
    execution_time: Optional[float]
    output_file: Optional[str]
    output_file_size: Optional[int]
    sequence: Optional[str]
    input_file_id: Optional[str]
    project_id: str
    credits_used: Optional[int] = None
    custom_params: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Artifact] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    # ---- convenience: behaves like a string job_id so old code still works ----
    def __str__(self) -> str:        # pragma: no cover
        return self.job_id

    @property
    def status_enum(self) -> JobStatus:
        try:
            return JobStatus(self.status)
        except ValueError:
            return JobStatus.PENDING

    @property
    def is_complete(self) -> bool:
        return self.status_enum.is_terminal

    @property
    def is_success(self) -> bool:
        return self.status_enum.is_success

    @property
    def is_failed(self) -> bool:
        return self.status_enum is JobStatus.FAILED

    # ---- mutation ----

    def refresh(self) -> 'Job':
        """Re-fetch this job's state from the server (mutates and returns self)."""
        updated = get_job(self.job_id)
        self.status = updated.status
        self.updated_at = updated.updated_at
        self.message = updated.message
        self.execution_time = updated.execution_time
        self.output_file = updated.output_file
        self.output_file_size = updated.output_file_size
        self.credits_used = updated.credits_used
        return self

    # ---- waiting ----

    def wait(
        self,
        timeout: Optional[float] = None,
        poll_interval: float = 1.0,
        progress: bool = False,
    ) -> bool:
        """Block until the job is complete. Returns True on success.

        ``progress`` prints a single-line status ticker while polling.
        """
        start = time.time()
        while not self.is_complete:
            if timeout is not None and (time.time() - start) > timeout:
                raise TimeoutError(
                    f"Job {self.job_id} did not complete within {timeout}s "
                    f"(status={self.status})"
                )
            if progress:
                elapsed = int(time.time() - start)
                print(
                    f"\r  [{self.status:<11}] {elapsed:>5}s elapsed "
                    f"\u2014 job {self.job_id[:8]}",
                    end='', flush=True,
                )
            time.sleep(poll_interval)
            try:
                self.refresh()
            except RuntimeError:
                # transient; keep polling
                pass
        if progress:
            print()
        return self.is_success

    def result(self, timeout: float = 300.0, poll_interval: float = 1.0) -> Dict[str, Any]:
        """Backward-compatible dict result (also useful for serialisation)."""
        self.wait(timeout=timeout, poll_interval=poll_interval)
        return {
            'success': self.is_success,
            'job_id': self.job_id,
            'status': self.status,
            'message': self.message,
            'output_file': self.output_file,
            'output_file_size': self.output_file_size,
            'execution_time': self.execution_time,
            'credits_used': self.credits_used,
            'artifacts': [a.__dict__ for a in self.artifacts],
        }

    # ---- operations on this job ----

    def cancel(self) -> bool:
        return cancel_job(self.job_id)

    def logs(self) -> List[Dict[str, Any]]:
        return get_job_logs(self.job_id)

    def download(
        self,
        output_path: Optional[str] = None,
        artifact: Optional[str] = None,
    ) -> str:
        """Download a job artifact to disk.

        ``artifact`` selects which file to fetch; defaults to the primary
        ``output_file``. Returns the local filesystem path written.
        """
        if artifact is None:
            return download_output(self.job_id, output_path=output_path)
        return download_artifact(self.job_id, artifact, output_path=output_path)

    def list_artifacts(self, refresh: bool = True) -> List[Artifact]:
        if refresh or not self.artifacts:
            self.artifacts = list_outputs(self.job_id)
        return list(self.artifacts)

    # ---- display ----

    def __repr__(self) -> str:
        return f"Job(job_id='{self.job_id}', model='{self.model}', status='{self.status}')"

    def _repr_html_(self) -> str:    # pragma: no cover
        status_color = {
            'pending': '#f59e0b', 'submitted': '#f59e0b', 'running': '#3b82f6',
            'processing': '#3b82f6', 'completed': '#22c55e',
            'failed': '#ef4444', 'cancelled': '#6b7280',
        }.get(self.status, '#6b7280')
        return (
            f"<div style='border:1px solid #3f3f46;border-radius:8px;padding:10px;"
            f"background:#18181b;color:#f4f4f5;font-family:monospace;'>"
            f"<strong>{self.model}</strong> "
            f"<span style='color:#71717a;'>({self.status})</span><br>"
            f"<span style='color:#a1a1aa;font-size:0.85em;'>"
            f"job_id: {self.job_id}<br>"
            f"created: {self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else 'n/a'}"
            f"</span></div>"
        )


# ----------------------------------------------------------------------------
# Per-model registry: maps friendly names -> canonical ids and primary inputs
# ----------------------------------------------------------------------------

# (short_name -> (canonical_model_id, primary_input_param, description))
# Canonical model table (derived from SUPPORTED_MODELS in production/config.py).
# Each model id maps to (canonical_id, primary_input, description). Aliases
# (short_name, snake_case, hyphenated) point to the same canonical id so that
# every spelling resolves identically.
# Canonical model table (derived from SUPPORTED_MODELS in production/config.py).
# Each model id maps to (canonical_id, primary_input, description). Aliases
# (short_name, snake_case, hyphenated) point to the same canonical id so that
# every spelling resolves identically.
_MODEL_REGISTRY: Dict[str, Tuple[str, str, str]] = {
    # ESMFold (canonical id type-0; SDK sends 'esmfold' which is aliased)
    'esmfold':              ('type-0', 'sequence', 'Predict 3D protein structure from an amino-acid sequence using ESMFold.'),
    'esmfold_re':           ('esmfold-re', 'sequence', 'ESMFold-RE: structure prediction with refinement.'),
    'esmfold-re':           ('esmfold-re', 'sequence', 'ESMFold-RE: structure prediction with refinement.'),
    # AlphaFold2 / multimer
    'alphafold2':           ('type-2', 'sequence', 'AlphaFold2 monomer structure prediction.'),
    'alphafold2_re':        ('alphafold2-re', 'sequence', 'AlphaFold2-RE structure prediction with MSA.'),
    'alphafold2-re':        ('alphafold2-re', 'sequence', 'AlphaFold2-RE structure prediction with MSA.'),
    'alphafold2_re_multimer': ('alphafold2-re-multimer', 'sequences', 'AlphaFold2-RE multimer complex prediction.'),
    'alphafold2-re-multimer': ('alphafold2-re-multimer', 'sequences', 'AlphaFold2-RE multimer complex prediction.'),
    # DiffDock / DiffDock-RE
    'diffdock':             ('type-3', 'ligand_smiles', 'DiffDock molecular docking (protein + ligand SMILES/SDF).'),
    'diffdock_re':          ('diffdock-re', 'ligand_smiles', 'DiffDock-RE molecular docking with refinement.'),
    'diffdock-re':          ('diffdock-re', 'ligand_smiles', 'DiffDock-RE molecular docking with refinement.'),
    # RFdiffusion (canonical id type-12; aliased to 'rfdiffusion')
    'rfdiffusion':          ('type-12', 'contigs', 'De novo protein backbone design with RFdiffusion.'),
    # ProteinMPNN / ProteinMPNN-RE
    'proteinmpnn':          ('type-4', 'sequence', 'ProteinMPNN inverse folding / sequence design.'),
    'proteinmpnn_re':       ('proteinmpnn-re', 'sequence', 'ProteinMPNN-RE inverse folding with refinement.'),
    'proteinmpnn-re':       ('proteinmpnn-re', 'sequence', 'ProteinMPNN-RE inverse folding with refinement.'),
    # Boltz2 (canonical id type-9)
    'boltz2':               ('type-9', 'sequences', 'Boltz-2 protein complex structure prediction.'),
    'boltz2_enhanced':      ('boltz2-enhanced', 'sequences', 'Boltz-2+ with extra features.'),
    'boltz2-enhanced':      ('boltz2-enhanced', 'sequences', 'Boltz-2+ with extra features.'),
    # Evo2 (canonical id evo2; type-8 is a duplicate alias)
    'evo2':                 ('evo2', 'sequence', 'Evo-2 genomic foundation model.'),
    # MolMIM, GenMol
    'molmim':               ('molmim', 'smiles', 'MolMIM molecular generation.'),
    'genmol':               ('genmol', 'smiles', 'GenMol generative chemistry.'),
    # OpenFold3
    'openfold3':            ('openfold3', 'sequences', 'OpenFold3 complex structure prediction.'),
    # MSA search
    'msa_search':           ('msa-search', 'sequence', 'MSA homology search.'),
    'msa-search':           ('msa-search', 'sequence', 'MSA homology search.'),
}


def _resolve_model_id(name_or_id: str) -> Tuple[str, str, str]:
    """Map a friendly short name or canonical id to (id, primary_input, description).

    Lookup is case-insensitive. Falls back to using the raw string as the id if
    it isn't in the local registry (the server will validate).
    """
    if not name_or_id:
        raise ValueError("Model name is required")
    key = name_or_id.lower()
    if key in _MODEL_REGISTRY:
        return _MODEL_REGISTRY[key]
    for k, v in _MODEL_REGISTRY.items():
        if v[0] == name_or_id:
            return v
    # Unknown: best-effort guess of primary input from the catalog (server-side)
    return (name_or_id, 'input', f"Model '{name_or_id}' (not in local registry).")


# ----------------------------------------------------------------------------
# Catalog cache (populated lazily from /api/available_models or the notebook
# catalog endpoint, whichever the running kernel can reach)
# ----------------------------------------------------------------------------

_catalog_cache: Optional[List[ModelInfo]] = None
_catalog_by_id: Dict[str, ModelInfo] = {}
_catalog_by_short: Dict[str, ModelInfo] = {}


def _refresh_catalog(force: bool = False) -> List[ModelInfo]:
    """Pull the catalog of models and cache it. Returns the list."""
    global _catalog_cache
    if _catalog_cache is not None and not force:
        return _catalog_cache

    payload = _api_request('GET', '/api/available_models')
    raw_models = payload if isinstance(payload, list) else payload.get('models', [])
    models = [ModelInfo.from_dict(m) for m in raw_models]

    _catalog_cache = models
    _catalog_by_id.clear()
    _catalog_by_short.clear()
    for m in models:
        if m.id:
            _catalog_by_id[m.id] = m
        if m.short_name:
            _catalog_by_short[m.short_name.lower()] = m
    return models


def _find_in_catalog(name_or_id: str) -> Optional[ModelInfo]:
    if not _catalog_cache:
        try:
            _refresh_catalog()
        except Exception:
            return None
    key = name_or_id.lower()
    if key in _catalog_by_short:
        return _catalog_by_short[key]
    if name_or_id in _catalog_by_id:
        return _catalog_by_id[name_or_id]
    return None


# ----------------------------------------------------------------------------
# ModelHandle: per-model facade exposed as ``phonos.<short_name>``
# ----------------------------------------------------------------------------

class ModelHandle:
    """Uniform per-model facade.

    Usage::

        phonos.esmfold.run(sequence='MKTAY...')
        phonos.boltz2.run(sequence='MKTAY...', ligand_input_type='smiles', ...)
        phonos.diffdock.run({'protein_pdb_id': '4hhb', 'ligand_smiles': 'CCO'})
    """

    def __init__(
        self,
        model_id: str,
        short_name: Optional[str] = None,
        primary_input: Optional[str] = None,
        description: str = '',
    ):
        self.id = model_id
        self.short_name = short_name or model_id
        self.primary_input = primary_input or 'input'
        self.description = description

    # legacy callable form: phonos.esmfold(sequence=...)
    def __call__(self, **kwargs) -> Job:
        return submit_job(model=self.id, **kwargs)

    def __repr__(self) -> str:    # pragma: no cover
        return f"ModelHandle(id='{self.id}', short_name='{self.short_name}')"

    def _repr_html_(self) -> str:    # pragma: no cover
        return (
            f"<div style='border:1px solid #3f3f46;border-radius:6px;padding:8px;"
            f"margin:4px 0;background:#18181b;color:#f4f4f5;font-family:monospace;'>"
            f"<strong>phonos.{self.short_name}</strong> "
            f"<span style='color:#71717a;'>({self.id})</span>"
            f"<div style='color:#a1a1aa;font-size:0.85em;'>{self.description}</div>"
            f"</div>"
        )

    # ---- the uniform entry point ----

    def run(self, input: Union[str, Dict, List, None] = None, **kwargs) -> Job:
        """Submit a job to this model.

        ``input`` accepts:
          * ``str``  -> the model's primary input (sequence, SMILES, ...).
                        If the model's primary input is list-valued (e.g.
                        ``sequences`` for boltz2/openfold3), a single string
                        is auto-wrapped into a one-element list for you.
          * ``dict`` -> merged into the job payload
          * ``list`` -> a list-valued primary input (e.g. sequences)
          * ``None`` -> fall back to ``**kwargs`` only

        ``**kwargs`` may include any model-specific custom parameter. If the
        primary input is already provided via ``kwargs`` (e.g. ``sequence=``
        or ``sequences=``), the ``input`` argument is ignored.
        """
        payload: Dict[str, Any] = {}
        if input is None:
            pass
        elif isinstance(input, str):
            # Auto-wrap a single string into a one-element list when the
            # primary input is list-valued (e.g. boltz2.sequences, openfold3
            # multimer). This means `phonos.boltz2.run("MKTAY...")` works
            # the same as `phonos.boltz2.run(sequences=["MKTAY..."])`.
            if self.primary_input.endswith('s') and self.primary_input not in kwargs:
                payload[self.primary_input] = [input]
            else:
                payload[self.primary_input] = input
        elif isinstance(input, dict):
            payload.update(input)
        elif isinstance(input, (list, tuple)):
            payload[self.primary_input] = list(input)
        else:
            payload[self.primary_input] = input
        # Also accept kwargs that match the primary input name (e.g.
        # ``phonos.boltz2.run(sequence="X")`` stores it as ``sequence``,
        # but the worker reads ``sequences`` for multimer models). Mirror
        # the value into the canonical list form when relevant.
        payload.update(kwargs)
        if self.primary_input == 'sequences' and 'sequence' in payload and 'sequences' not in payload:
            seq_val = payload.pop('sequence')
            if isinstance(seq_val, str):
                payload['sequences'] = [seq_val]
            elif isinstance(seq_val, (list, tuple)):
                payload['sequences'] = list(seq_val)

        # Soft validation against catalog (raises ValueError before any network call)
        info = _find_in_catalog(self.id)
        if info is not None:
            missing = [p for p in info.required_parameters if p not in payload and p != self.primary_input]
            if missing:
                raise ValueError(
                    f"Model '{self.id}' requires parameter(s): {', '.join(missing)}. "
                    f"Pass them as kwargs, e.g. phonos.{self.short_name}.run({missing[0]}=...)."
                )

        return submit_job(model=self.id, **payload)

    # ---- introspection helpers ----

    def info(self, refresh: bool = False) -> ModelInfo:
        """Return the live :class:`ModelInfo` for this model from the catalog."""
        if refresh:
            _refresh_catalog(force=True)
        info = _find_in_catalog(self.id)
        if info is None:
            raise LookupError(
                f"Model '{self.id}' was not found in /api/available_models. "
                f"It may be offline or require a different project context."
            )
        return info

    def help(self) -> str:
        """Return a short help string describing this model and its parameters."""
        lines = [
            f"phonos.{self.short_name}.run(input, **kwargs)",
            f"  model id       : {self.id}",
            f"  primary input  : {self.primary_input}",
            f"  description    : {self.description or '(see phonos.list_models())'}",
            f"  example        : phonos.{self.short_name}.run('<your {self.primary_input}>')",
        ]
        try:
            info = self.info()
            lines.append(f"  credit cost    : {info.credit_cost}")
            if info.parameters:
                lines.append("  parameters     :")
                for p in info.parameters:
                    req = 'required' if p.get('required') else 'optional'
                    default = p.get('default')
                    default_repr = '' if default in (None, '', []) else f" (default={default!r})"
                    ptype = p.get('type', 'any')
                    lines.append(
                        f"      - {p['name']:<24} {ptype:<10} {req}{default_repr}"
                    )
        except Exception:
            pass
        return '\n'.join(lines)


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

class PhonosError(RuntimeError):
    """Raised for any non-transport SDK error (HTTP, validation, server)."""


def _auth_headers() -> Dict[str, str]:
    if not _SESSION_TOKEN:
        raise PhonosError(
            "Not running in a notebook context. "
            "NOTEBOOK_SESSION_TOKEN / NOTEBOOK_SESSION_ID environment variable is required."
        )
    return {
        'X-Notebook-Session': _SESSION_TOKEN,
        'X-Project-ID': str(_PROJECT_ID) if _PROJECT_ID else '',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def _api_request(
    method: str,
    endpoint: str,
    data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
    stream: bool = False,
) -> Any:
    """Authenticated JSON request to the Phonos backend."""
    headers = _auth_headers()
    url = f"{_BASE_URL}{endpoint}"
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=data,
            params=params,
            timeout=timeout,
            stream=stream,
        )
    except requests.exceptions.ConnectionError as e:
        raise PhonosError(
            f"Could not connect to Phonos API at {_BASE_URL}. "
            f"Make sure the Phonos server is running. ({e})"
        )
    except requests.exceptions.Timeout as e:
        raise PhonosError(f"Request to {url} timed out after {timeout}s ({e})")

    if response.status_code == 204:
        return None

    # Try JSON for everything (even errors)
    parsed: Any = None
    try:
        parsed = response.json()
    except Exception:
        parsed = None

    if response.ok:
        return parsed

    # Build a rich error message
    msg = ''
    if isinstance(parsed, dict):
        msg = parsed.get('message') or parsed.get('error') or ''
    if not msg:
        msg = response.text or ''
    raise PhonosError(
        f"{method} {endpoint} failed: {response.status_code} {response.reason}. {msg}".strip()
    )


def _api_download(
    endpoint: str,
    output_path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 120.0,
) -> str:
    """Stream an authenticated download to disk; return the local path."""
    headers = _auth_headers()
    url = f"{_BASE_URL}{endpoint}"
    response = requests.get(url, headers=headers, params=params, timeout=timeout, stream=True)
    if not response.ok:
        msg = ''
        try:
            j = response.json()
            msg = j.get('message') or j.get('error') or ''
        except Exception:
            msg = response.text or ''
        raise PhonosError(
            f"GET {endpoint} failed: {response.status_code} {response.reason}. {msg}".strip()
        )
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
    return output_path


# ----------------------------------------------------------------------------
# Model discovery
# ----------------------------------------------------------------------------

def list_models(refresh: bool = False) -> List[ModelInfo]:
    """List all catalog models."""
    return list(_refresh_catalog(force=refresh))


def list_online_models(refresh: bool = False) -> List[ModelInfo]:
    """Only models currently marked as available AND active."""
    return [m for m in list_models(refresh=refresh) if m.available and m.is_active]


def get_model(name_or_id: str, refresh: bool = False) -> ModelInfo:
    """Look up a single model by id, short_name, or alias.

    Raises :class:`LookupError` if not found.
    """
    if refresh:
        _refresh_catalog(force=True)
    info = _find_in_catalog(name_or_id)
    if info is not None:
        return info
    # try aliases (esmfold_re -> esmfold-re etc.)
    canonical, _, _ = _resolve_model_id(name_or_id)
    info = _find_in_catalog(canonical)
    if info is not None:
        return info
    raise LookupError(
        f"No model named '{name_or_id}'. Try phonos.list_models() to see available models."
    )


# ----------------------------------------------------------------------------
# Project / user discovery
# ----------------------------------------------------------------------------

@dataclass
class ProjectInfo:
    id: int
    name: str
    role: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ProjectInfo':
        return cls(
            id=int(d.get('id') or d.get('project_id') or 0),
            name=d.get('name', ''),
            role=d.get('role'),
            raw=d,
        )


def get_project_info() -> Dict[str, Any]:
    """Return the current kernel's project context (set by the kernel manager)."""
    return {
        'project_id': _PROJECT_ID,
        'api_url': _BASE_URL,
        'has_session': bool(_SESSION_TOKEN),
        'runtime_level': os.environ.get('RUNTIME_LEVEL', 'basic'),
    }


def list_projects(refresh: bool = False) -> List[ProjectInfo]:
    """List projects the current user has access to."""
    data = _api_request('GET', '/api/projects')
    items = data if isinstance(data, list) else data.get('projects', [])
    return [ProjectInfo.from_dict(p) for p in items]


def get_user_info() -> Dict[str, Any]:
    """Return the current user's profile (credits, plan, storage, job counts)."""
    return _api_request('GET', '/api/user') or {}


def get_credits() -> Optional[int]:
    """Convenience: current credit balance, or None if unknown."""
    info = get_user_info()
    if 'credits' in info:
        try:
            return int(info['credits'])
        except (TypeError, ValueError):
            return None
    return None


# ----------------------------------------------------------------------------
# Jobs: submit, inspect, list, cancel
# ----------------------------------------------------------------------------

def _build_submit_payload(model: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Strip None values and pass through whatever the user gave us."""
    return {k: v for k, v in kwargs.items() if v is not None}


def submit_job(model: str, name: Optional[str] = None, **kwargs) -> Job:
    """Submit a job to ``model`` (id or short_name)."""
    payload: Dict[str, Any] = {'model': model, 'source': 'notebook'}
    if name:
        payload['name'] = name
    payload.update(_build_submit_payload(model, kwargs))

    if not _PROJECT_ID:
        raise PhonosError(
            "No project is associated with this notebook kernel "
            "(NOTEBOOK_PROJECT_ID is unset). Open or select a project "
            "before submitting jobs."
        )

    endpoint = f'/api/projects/{_PROJECT_ID}/jobs'
    response = _api_request('POST', endpoint, data=payload)

    return Job(
        job_id=response.get('job_id', ''),
        model=model,
        name=name,
        status=response.get('status', 'pending'),
        created_at=_parse_iso(response.get('created_at')) or datetime.now(),
        updated_at=_parse_iso(response.get('updated_at')),
        message=response.get('message'),
        execution_time=response.get('execution_time'),
        output_file=response.get('output_file'),
        output_file_size=response.get('output_file_size'),
        sequence=kwargs.get('sequence') or kwargs.get('protein_sequence'),
        input_file_id=response.get('input_file_id'),
        project_id=str(_PROJECT_ID),
        credits_used=response.get('credits_used'),
        custom_params=kwargs.get('custom_params') or {},
        raw=response,
    )


def batch_submit(
    model: str,
    items: List[Dict[str, Any]],
    batch_mode: bool = True,
    name: Optional[str] = None,
) -> List[Job]:
    """Submit a batch of jobs in one request.

    ``items`` is a list of dicts; each becomes a job unless ``batch_mode=True``,
    in which case the worker treats them as multiple inputs of a single job.

    Returns the list of newly created :class:`Job` objects (one per item when
    ``batch_mode=False``, otherwise a single-element list).
    """
    if not items:
        return []
    if not _PROJECT_ID:
        raise PhonosError(
            "No project is associated with this notebook kernel "
            "(NOTEBOOK_PROJECT_ID is unset)."
        )
    payload: Dict[str, Any] = {
        'model': model,
        'project_id': _PROJECT_ID,
        'items': items,
        'batch_mode': batch_mode,
        'source': 'notebook',
    }
    if name:
        payload['name'] = name
    response = _api_request('POST', '/api/batch/jobs', data=payload)
    if not isinstance(response, dict):
        return []
    jobs_data = response.get('jobs') or []
    return [
        Job(
            job_id=j.get('job_id', ''),
            model=model,
            name=name,
            status=j.get('status', 'pending'),
            created_at=_parse_iso(j.get('created_at')) or datetime.now(),
            updated_at=_parse_iso(j.get('updated_at')),
            message=j.get('message'),
            execution_time=j.get('execution_time'),
            output_file=j.get('output_file'),
            output_file_size=j.get('output_file_size'),
            sequence=(j.get('sequence') if isinstance(j, dict) else None),
            input_file_id=j.get('input_file_id'),
            project_id=str(_PROJECT_ID),
            raw=j,
        )
        for j in jobs_data
    ]


def get_job(job_id: str) -> Job:
    """Fetch the latest state for a job."""
    if not job_id:
        raise ValueError("job_id is required")
    response = _api_request('GET', f'/api/jobs/{job_id}')
    return _job_from_api(response, fallback_id=job_id)


def get_job_info(job: Union[str, Job]) -> Job:
    """Idempotent: accept either a job_id or a Job; return a fresh Job."""
    jid = job.job_id if isinstance(job, Job) else job
    return get_job(jid)


def list_jobs(limit: int = 50, status: Optional[str] = None) -> List[Job]:
    """List recent jobs in the current project (newest first)."""
    params: Dict[str, Any] = {'limit': int(limit)}
    if status:
        params['status'] = status
    if _PROJECT_ID:
        response = _api_request('GET', f'/api/projects/{_PROJECT_ID}/jobs', params=params)
    else:
        response = _api_request('GET', '/api/jobs', params=params)
    items = response if isinstance(response, list) else response.get('jobs', [])
    return [_job_from_api(j) for j in items]


def cancel_job(job_id: str) -> bool:
    """Cancel a running/pending job."""
    if not job_id:
        return False
    try:
        response = _api_request('POST', f'/api/jobs/{job_id}/cancel')
    except PhonosError:
        return False
    if isinstance(response, dict):
        return response.get('status') == 'success' or 'cancel' in (response.get('message', '') or '').lower()
    return False


def get_job_logs(job_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Fetch execution log entries for a job."""
    params = {'limit': int(limit)}
    response = _api_request('GET', f'/api/jobs/{job_id}/logs', params=params)
    if isinstance(response, dict):
        return response.get('logs') or []
    if isinstance(response, list):
        return response
    return []


# ----------------------------------------------------------------------------
# Outputs: download primary output or any artifact
# ----------------------------------------------------------------------------

def list_outputs(job: Union[str, Job]) -> List[Artifact]:
    """List every artifact (output file) produced by a job."""
    jid = job.job_id if isinstance(job, Job) else job
    response = _api_request('GET', f'/api/jobs/{jid}/artifacts')
    if not isinstance(response, dict):
        return []
    base = (_BASE_URL or '').rstrip('/')
    out: List[Artifact] = []
    for a in response.get('artifacts', []) or []:
        filename = a.get('filename', '')
        art = Artifact.from_dict(a)
        # Build an absolute download URL for convenience
        if filename:
            art.url = f"{base}/api/jobs/{jid}/artifacts/{requests.utils.quote(filename)}/content"
        out.append(art)
    return out


def _resolve_output_path(job: Union[str, Job], output_path: Optional[str]) -> str:
    if output_path:
        return output_path
    info = get_job_info(job) if isinstance(job, str) else job
    fname = info.output_file
    if fname:
        # output_file is often "/api/.../..." or just a filename
        return os.path.basename(fname.split('?')[0]) or 'output'
    return 'output'


def download_output(
    job: Union[str, Job],
    output_path: Optional[str] = None,
) -> str:
    """Download a job's primary output file. Returns the local path.

    Uses the server's `/api/jobs/<id>/output_content` endpoint which handles
    path resolution, decompression, Boltz2 CIF-to-PDB conversion, and
    permission checks — rather than naively fetching the absolute filesystem
    path that ``Job.output_file`` happens to contain.
    """
    jid = job.job_id if isinstance(job, Job) else job
    info = get_job_info(jid)
    if not info.output_file and not info.artifacts:
        raise PhonosError(f"Job {jid} has no output file (status={info.status})")

    # Determine a sensible on-disk filename
    target = output_path
    if not target:
        # Prefer display_name -> basename of output_file -> 'output'
        if info.output_file:
            target = os.path.basename(info.output_file.split('?')[0]) or None
        if not target:
            target = f"{jid}.out"

    # Prefer the resolved output_content endpoint; fall back to raw file
    # via the artifact endpoint if the job has named artifacts.
    return _api_download(f'/api/jobs/{jid}/output_content', target)


def download_artifact(
    job: Union[str, Job],
    artifact_name: str,
    output_path: Optional[str] = None,
) -> str:
    """Download a specific artifact (output file) by name. Returns the local path."""
    jid = job.job_id if isinstance(job, Job) else job
    target = output_path or artifact_name
    endpoint = f'/api/jobs/{jid}/artifacts/{requests.utils.quote(artifact_name)}/content'
    return _api_download(endpoint, target)


# ----------------------------------------------------------------------------
# Files (notebook-side helpers)
# ----------------------------------------------------------------------------

def list_project_files(path: str = '/workspace') -> List[Dict[str, Any]]:
    """List files in the notebook workspace (``/workspace`` by default)."""
    import stat
    results: List[Dict[str, Any]] = []
    try:
        entries = sorted(os.listdir(path))
    except FileNotFoundError:
        return []
    for entry in entries:
        full = os.path.join(path, entry)
        try:
            st = os.stat(full)
            results.append({
                'name': entry,
                'path': full,
                'size': st.st_size,
                'is_dir': stat.S_ISDIR(st.st_mode),
                'modified_at': datetime.fromtimestamp(st.st_mtime).isoformat(),
            })
        except OSError:
            results.append({'name': entry, 'path': full, 'size': 0, 'is_dir': False, 'modified_at': ''})
    return results


def read_project_file(filename: str, path: str = '/workspace') -> str:
    """Read a file from the notebook workspace."""
    for base in (path, '/workspace', '/project'):
        filepath = os.path.join(base, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
    raise FileNotFoundError(f"File not found: {filename}")


def write_project_file(filename: str, content: str, path: str = '/workspace') -> str:
    """Write a file into the notebook workspace."""
    filepath = os.path.join(path, filename)
    os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    return filepath


# ----------------------------------------------------------------------------
# Per-model facade: phonos.<short_name>.run(...)
# ----------------------------------------------------------------------------

# Bind every registered model as both snake_case and hyphenated attribute.
# Pre-populate the global namespace so `phonos.esmfold.run(...)` etc. works.

_handles: Dict[str, ModelHandle] = {}

for _alias, (_canonical, _primary, _desc) in _MODEL_REGISTRY.items():
    _handle = ModelHandle(_canonical, short_name=_alias.replace('-', '_'), primary_input=_primary, description=_desc)
    _handles[_alias] = _handle
    # Bind every alias as a top-level module attribute (Python identifiers only).
    globals()[_alias.replace('-', '_')] = _handle


def models() -> Dict[str, ModelHandle]:
    """Return the live registry of per-model handles (snake_case + hyphenated)."""
    return dict(_handles)


# Legacy callable forms (phonos.esmfold(sequence=...), phonos.boltz2(...), ...)
# are now provided by ModelHandle.__call__ above. Existing code that uses them
# keeps working: the handle's __call__ forwards kwargs straight to submit_job.
#
# If you want the *exact* previous defaults (e.g. boltz2 used to wrap a string
# as a single-element list of sequences automatically), prefer:
#     phonos.boltz2.run(sequence='MKTAY...')
# or pass a list explicitly:
#     phonos.boltz2.run(['MKTAY...'])

# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------

def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


def _job_from_api(data: Dict[str, Any], fallback_id: Optional[str] = None) -> Job:
    if not isinstance(data, dict):
        raise PhonosError(f"Unexpected job payload: {data!r}")
    credits_used = data.get('credits_used')
    custom_params = data.get('custom_params') or {}
    return Job(
        job_id=data.get('job_id') or fallback_id or '',
        model=data.get('model', ''),
        name=data.get('name'),
        status=data.get('status', 'pending'),
        created_at=_parse_iso(data.get('created_at')) or datetime.now(),
        updated_at=_parse_iso(data.get('updated_at')),
        message=data.get('message'),
        execution_time=data.get('execution_time'),
        output_file=data.get('output_file'),
        output_file_size=data.get('output_file_size'),
        sequence=data.get('sequence'),
        input_file_id=data.get('input_file_id'),
        project_id=str(data.get('project_id') or _PROJECT_ID or ''),
        credits_used=credits_used,
        custom_params=custom_params,
        raw=data,
    )


# ============================================================================
# Documentation & in-depth help
# ============================================================================
#
# Public surface for documentation:
#
#   phonos.help()                        -- one-page SDK overview
#   phonos.help(topic)                   -- detailed help for a feature or model
#   phonos.guide()                       -- full SDK walkthrough (same as phonos.help())
#   phonos.docs(topic)                   -- structured Doc object (programmatic access)
#   phonos.api()                         -- full programmatic API reference (returns a dict)
#   phonos.<model>.help()                -- quick parameter list
#   phonos.<model>.reference()           -- in-depth reference (with examples)
#   phonos.<model>.examples              -- list of curated code snippets
#
# Topics understood by help()/docs():
#
#   'overview'           -- one-page SDK cheat sheet (same as help())
#   'guide'              -- full SDK walkthrough
#   'api'                -- programmatic API reference
#
#   'wait'               -- Job.wait(timeout, poll_interval, progress)
#   'download'           -- download_output, download_artifact, Job.download
#   'outputs'            -- list_outputs, Artifact dataclass
#   'logs'               -- get_job_logs, Job.logs()
#   'cancel'             -- cancel_job, Job.cancel()
#   'batch'              -- batch_submit
#   'discovery'          -- list_models, list_online_models, get_model, models()
#   'projects'           -- list_projects, get_project_info
#   'user'               -- get_user_info, get_credits
#   'workspace'          -- list/read/write_project_file
#   'errors'             -- PhonosError, troubleshooting
#
#   '<model>'            -- detailed help for a specific model
# ============================================================================


# ----------------------------------------------------------------------------
# Curated per-model reference data (used by phonos.help() / phonos.docs())
# ----------------------------------------------------------------------------

_MODEL_REFERENCE: Dict[str, Dict[str, Any]] = {
    'esmfold': {
        'summary': 'Predict a 3D protein structure from an amino-acid sequence using Meta ESMFold.',
        'when_to_use': 'Single-chain protein structure prediction. Fastest option when no MSA is required.',
        'inputs': {'sequence': {'type': 'str', 'required': True,
            'description': 'Amino-acid sequence (uppercase recommended). 20-400 residues give best results.'}},
        'advanced_inputs': {
            'num_recycles': {'type': 'int', 'default': 4, 'range': '0-20',
                'description': 'Number of recycling iterations. Higher = better accuracy, slower.'},
            'output_format': {'type': "['pdb','cif','both']", 'default': 'pdb',
                'description': 'File format for the predicted structure.'},
        },
        'output': 'A PDB or mmCIF structure file.',
        'output_examples': ['structure.pdb', 'structure.cif'],
        'tips': ['Long sequences (>1000 residues) can take minutes and may OOM the GPU.',
                 'For very small peptides (<30 residues) results are usually noisy.'],
        'see_also': ['wait', 'download', 'logs'],
    },
    'esmfold-re': {
        'summary': 'Refined ESMFold prediction. Same model, hosted endpoint with extra validation.',
        'when_to_use': 'When you want a quick, reliable single-chain prediction without setting up a local GPU.',
        'inputs': {'sequence': {'type': 'str', 'required': True,
            'description': 'Amino-acid sequence.'}},
        'advanced_inputs': {
            'num_recycles': {'type': 'int', 'default': 4, 'range': '0-20',
                'description': 'Recycling iterations.'},
            'output_format': {'type': "['pdb','cif','both']", 'default': 'pdb',
                'description': 'File format.'},
        },
        'output': 'One PDB or CIF file.',
        'output_examples': ['<job_id>.pdb'],
        'tips': ['Refined worker applies MSA masking only when needed.'],
        'see_also': ['wait', 'download'],
    },
    'alphafold2': {
        'summary': 'AlphaFold2 monomer prediction with MSA.',
        'when_to_use': 'Gold-standard single-chain prediction when you have a few minutes to spare.',
        'inputs': {'sequence': {'type': 'str', 'required': True, 'description': 'Amino-acid sequence.'}},
        'advanced_inputs': {
            'msa_mode': {'type': "['mmseqs2','colabfold','precomputed']", 'default': 'mmseqs2',
                'description': 'MSA search strategy.'},
            'num_recycles': {'type': 'int', 'default': 3, 'description': 'Recycling iterations.'},
            'use_templates': {'type': 'bool', 'default': False, 'description': 'Use structural templates.'},
            'amber_relax': {'type': 'bool', 'default': False, 'description': 'AMBER relaxation (slow).'},
        },
        'output': 'PDB structure + PAE plot PNG.',
        'output_examples': ['<job_id>.pdb', 'pae.png'],
        'tips': ['MSA search adds 1-5 minutes depending on database size.',
                 'Disable amber_relax for fast prototyping.'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'alphafold2-re': {
        'summary': 'AlphaFold2 with MSA search and refinement parameters.',
        'when_to_use': 'Hosted AlphaFold2 with more knobs than the standard worker.',
        'inputs': {'sequence': {'type': 'str', 'required': True, 'description': 'Amino-acid sequence.'}},
        'advanced_inputs': {
            'msa_mode': {'type': "['mmseqs2','colabfold','precomputed']", 'default': 'mmseqs2',
                'description': 'MSA search strategy.'},
            'num_recycles': {'type': 'int', 'default': 3, 'description': 'Recycling iterations.'},
        },
        'output': 'PDB structure + PAE plot.',
        'output_examples': ['<job_id>.pdb'],
        'see_also': ['wait', 'download'],
    },
    'alphafold2-re-multimer': {
        'summary': 'AlphaFold2 multimer: predict protein complexes from multiple chains.',
        'when_to_use': 'Multi-chain complexes (receptors, antibodies, designed binders).',
        'inputs': {'sequences': {'type': 'List[str]', 'required': True,
            'description': 'One amino-acid sequence per chain, in order. '
                           'A single string is auto-wrapped to a one-element list.'}},
        'advanced_inputs': {
            'msa_mode': {'type': "['mmseqs2','colabfold']", 'default': 'mmseqs2',
                'description': 'MSA search strategy.'},
            'num_recycles': {'type': 'int', 'default': 3, 'description': 'Recycling iterations.'},
            'pairing_strategy': {'type': "['all_vs_all','paired']", 'default': 'paired',
                'description': 'How to pair chains in the MSA.'},
        },
        'output': 'PDB structure with all chains.',
        'output_examples': ['<job_id>.pdb'],
        'tips': ['Order of sequences = chain order in the output PDB.'],
        'see_also': ['wait', 'download'],
    },
    'rfdiffusion': {
        'summary': 'De novo protein backbone design with RFdiffusion.',
        'when_to_use': 'Designing novel protein scaffolds, binders, or symmetric assemblies.',
        'inputs': {'contigs': {'type': 'str', 'required': True,
            'description': 'Contig string, e.g. "60-120" or "40-60/0 40-60".'}},
        'advanced_inputs': {
            'sequence': {'type': 'str', 'default': '', 'description': 'Optional motif to scaffold around.'},
            'hotspot_residue': {'type': 'str', 'default': '',
                'description': 'Comma-separated hotspot residues, e.g. "40:A,55:A".'},
            'diffuse_templates': {'type': 'bool', 'default': False, 'description': 'Diffuse from a template.'},
            'num_designs': {'type': 'int', 'default': 1, 'range': '1-50',
                'description': 'How many independent designs to generate.'},
            'diffusion_steps': {'type': 'int', 'default': 50, 'range': '10-1000',
                'description': 'Number of denoising steps.'},
        },
        'output': 'Multiple PDB files (one per design).',
        'output_examples': ['design_0.pdb', 'design_1.pdb', '...'],
        'tips': ['More designs = more chances of finding a good fold (try 8+).',
                 'For motif scaffolding, set hotspot_residue to pin the motif.'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'proteinmpnn': {
        'summary': 'Inverse folding: design sequences for a fixed backbone.',
        'when_to_use': 'Given a PDB backbone, propose sequences that fold to it.',
        'inputs': {'pdb_file': {'type': 'str', 'required': True,
            'description': 'PDB file path in the project workspace, or pre-uploaded file id.'}},
        'advanced_inputs': {
            'sequence': {'type': 'str', 'default': '',
                'description': 'Positions to keep fixed (1-letter codes; non-positions use "/").'},
            'temperature': {'type': 'float', 'default': 0.1, 'range': '0.0-1.0',
                'description': 'Sampling temperature. Higher = more diverse.'},
            'num_sequences': {'type': 'int', 'default': 1, 'range': '1-100',
                'description': 'How many sequences to propose.'},
        },
        'output': 'FASTA file with designed sequences.',
        'output_examples': ['<job_id>.fasta'],
        'tips': ['temperature=0.1 picks the highest-probability amino acid.',
                 'temperature=0.3 gives diverse but still-foldable sequences.'],
        'see_also': ['wait', 'download'],
    },
    'proteinmpnn-re': {
        'summary': 'Inverse folding with refinement: ProteinMPNN + ESMFold re-prediction.',
        'when_to_use': 'When you want MPNN designs filtered by predicted foldability.',
        'inputs': {'pdb_file': {'type': 'str', 'required': True, 'description': 'PDB file path.'}},
        'advanced_inputs': {
            'sequence': {'type': 'str', 'default': '', 'description': 'Positions to fix.'},
            'temperature': {'type': 'float', 'default': 0.1, 'description': 'Sampling temperature.'},
            'num_sequences': {'type': 'int', 'default': 8, 'description': 'How many to generate.'},
            'refine_cycles': {'type': 'int', 'default': 3, 'description': 'Refinement iterations.'},
            'plddt_threshold': {'type': 'float', 'default': 0.7,
                'description': 'Drop sequences whose refolded pLDDT falls below this.'},
        },
        'output': 'FASTA + per-sequence pLDDT scores.',
        'output_examples': ['<job_id>.fasta', '<job_id>_plddt.json'],
        'tips': ['Default 8 sequences + 3 refine cycles usually gives 1-3 high-confidence designs.'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'diffdock': {
        'summary': 'DiffDock: diffusion-based molecular docking.',
        'when_to_use': 'Predict binding poses of a small molecule against a protein.',
        'inputs': {
            'protein_file': {'type': 'str', 'required': False,
                'description': 'Project-relative path to a .pdb file (alternative: protein_pdb_id).'},
            'protein_pdb_id': {'type': 'str', 'required': False,
                'description': 'Four-letter PDB id, e.g. "4hhb" (fetched from RCSB).'},
            'ligand_smiles': {'type': 'str', 'required': False,
                'description': 'SMILES string for the ligand (alternative: ligand_file).'},
            'ligand_file': {'type': 'str', 'required': False,
                'description': 'Project-relative path to an SDF/MOL2/SMILES file.'},
        },
        'advanced_inputs': {
            'num_poses': {'type': 'int', 'default': 10, 'range': '1-40',
                'description': 'How many poses to return.'},
            'time_divisions': {'type': 'int', 'default': 20, 'range': '1-100',
                'description': 'Diffusion time steps.'},
            'steps': {'type': 'int', 'default': 18, 'range': '1-100', 'description': 'Diffusion steps.'},
            'save_trajectory': {'type': 'bool', 'default': False, 'description': 'Save trajectory frames.'},
        },
        'output': 'Top-N docked poses as SDF, plus confidence scores.',
        'output_examples': ['rank_1.sdf', 'rank_2.sdf', 'confidence.csv'],
        'tips': ['Use protein_pdb_id for known structures (no upload needed).',
                 'SMILES is fastest; SDF lets you specify stereochemistry explicitly.',
                 'Poses are ranked by confidence -- rank_1.sdf is the best pose.'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'diffdock-re': {
        'summary': 'DiffDock with refinement: better accuracy on difficult ligands.',
        'when_to_use': 'When standard DiffDock gives low-confidence poses.',
        'inputs': {
            'protein': {'type': 'str', 'required': True, 'description': 'Project-relative PDB path.'},
            'ligand': {'type': 'str', 'required': True,
                'description': 'Ligand in SDF, MOL2, or SMILES format.'},
            'ligand_file_type': {'type': "['sdf','mol2','smiles']", 'default': 'sdf',
                'description': 'Format of the ligand input.'},
        },
        'advanced_inputs': {
            'num_poses': {'type': 'int', 'default': 10, 'range': '1-20', 'description': 'How many poses.'},
            'time_divisions': {'type': 'int', 'default': 20, 'description': 'Time steps.'},
            'steps': {'type': 'int', 'default': 18, 'description': 'Diffusion steps.'},
            'save_trajectory': {'type': 'bool', 'default': False, 'description': 'Save frames.'},
            'is_staged': {'type': 'bool', 'default': False, 'description': 'Staged diffusion.'},
            'no_final_step_noise': {'type': 'bool', 'default': False,
                'description': 'Skip noise at the final step.'},
        },
        'output': 'Refined docked poses + confidence.',
        'output_examples': ['rank_1.sdf', 'confidence.csv'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'evo2': {
        'summary': 'Evo-2: genomic foundation model for DNA sequence design and prediction.',
        'when_to_use': 'Design, predict, or evolve DNA/RNA sequences. Supports masked evolution, '
                       'generation, and likelihood scoring for regulatory elements and coding regions.',
        'inputs': {
            'sequence': {'type': 'str', 'required': True,
                'description': 'DNA sequence (A/C/G/T). Lowercase is accepted. '
                               'For protein-mode use the corresponding amino-acid alphabet.'},
        },
        'advanced_inputs': {
            'mask_rate': {'type': 'float', 'default': 0.3, 'range': '0.0-1.0',
                'description': 'Fraction of tokens to mask for evolution.'},
            'num_iterations': {'type': 'int', 'default': 2, 'range': '1-20',
                'description': 'Number of masked evolution passes.'},
            'temperature': {'type': 'float', 'default': 1.0, 'range': '0.1-5.0',
                'description': 'Sampling temperature for masked tokens.'},
        },
        'output': 'Evolved sequence(s) as FASTA or JSON.',
        'output_examples': ['<job_id>.fasta'],
        'tips': ['mask_rate=0.3 with 2-4 iterations works well for most design tasks.',
                 'Use lower temperature (0.5-0.8) for more conservative evolution.'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'boltz': {
        'summary': 'Boltz complex structure prediction (legacy).',
        'when_to_use': 'Use boltz2 for new work; boltz exists for legacy compatibility.',
        'inputs': {'sequences': {'type': 'List[str]', 'required': True,
            'description': 'One sequence per chain.'}},
        'advanced_inputs': {},
        'output': 'PDB structure.',
        'output_examples': ['<job_id>.pdb'],
        'tips': ['Prefer boltz2 for affinity predictions and updated parameters.'],
        'see_also': ['wait', 'download'],
    },
    'boltz1': {
        'summary': 'Boltz-1 complex structure prediction.',
        'when_to_use': 'Older generation than boltz2 -- kept for reproducibility.',
        'inputs': {'sequences': {'type': 'List[str]', 'required': True,
            'description': 'One sequence per chain.'}},
        'advanced_inputs': {
            'recycling_steps': {'type': 'int', 'default': 3, 'description': 'Refinement passes.'},
            'sampling_steps': {'type': 'int', 'default': 50, 'description': 'Diffusion denoising steps.'},
        },
        'output': 'PDB structure.',
        'output_examples': ['<job_id>.pdb'],
        'see_also': ['wait', 'download'],
    },
    'boltz2': {
        'summary': 'Boltz-2: state-of-the-art biomolecular structure prediction with ligand support and affinity scoring.',
        'when_to_use': 'Predict protein or protein+ligand structures with optional binding affinity.',
        'inputs': {
            'sequence': {'type': 'str', 'required': False,
                'description': 'Amino-acid sequence. Auto-wrapped to ["..."] for multimer mode.'},
            'sequences': {'type': 'List[str]', 'required': False,
                'description': 'List of sequences for multimer mode.'},
        },
        'advanced_inputs': {
            'molecule_type': {'type': "['protein','dna','rna']", 'default': 'protein',
                'description': 'Polymer type for the sequence.'},
            'cyclic': {'type': 'bool', 'default': False, 'description': 'Treat the sequence as cyclic.'},
            'ligand_input_type': {'type': "['none','smiles','ccd']", 'default': 'none',
                'description': 'Whether to add a small-molecule ligand.'},
            'ligand_smiles': {'type': 'str', 'default': '',
                'description': 'SMILES for the ligand (when ligand_input_type=smiles).'},
            'ligand_ccd': {'type': 'str', 'default': '',
                'description': 'CCD code such as ATP, GTP, NAD (when ligand_input_type=ccd).'},
            'predict_affinity': {'type': 'bool', 'default': False,
                'description': 'Compute predicted binding affinity (only one ligand per request).'},
            'recycling_steps': {'type': 'int', 'default': 3, 'range': '1-6',
                'description': 'Refinement iterations.'},
            'sampling_steps': {'type': 'int', 'default': 50, 'range': '10-1000',
                'description': 'Diffusion denoising steps.'},
            'diffusion_samples': {'type': 'int', 'default': 1, 'range': '1-5',
                'description': 'Number of independent samples.'},
            'step_scale': {'type': 'float', 'default': 1.2, 'range': '0.5-5.0',
                'description': 'Diffusion step size.'},
            'without_potentials': {'type': 'bool', 'default': False,
                'description': 'Skip physics potentials (faster but less accurate).'},
        },
        'output': 'CIF structure + optional affinity JSON.',
        'output_examples': ['<job_id>.cif', '<job_id>_affinity.json'],
        'tips': ['For affinity, set predict_affinity=True and use one ligand.',
                 'diffusion_samples > 1 gives an ensemble (each sample is a possible fold).'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'boltz2-enhanced': {
        'summary': 'Boltz2-Enhanced: extra sampling, ensemble generation, advanced knobs.',
        'when_to_use': 'Production runs that need the best Boltz-2 can offer.',
        'inputs': {
            'sequence': {'type': 'str', 'required': False, 'description': 'Amino-acid sequence.'},
            'sequences': {'type': 'List[str]', 'required': False, 'description': 'List of sequences (multimer).'},
        },
        'advanced_inputs': {
            'molecule_type': {'type': "['protein','dna','rna']", 'default': 'protein', 'description': 'Polymer type.'},
            'cyclic': {'type': 'bool', 'default': False, 'description': 'Cyclic sequence.'},
            'ligand_input_type': {'type': "['none','smiles','ccd']", 'default': 'none', 'description': 'Ligand type.'},
            'ligand_smiles': {'type': 'str', 'default': '', 'description': 'Ligand SMILES.'},
            'ligand_ccd': {'type': 'str', 'default': '', 'description': 'Ligand CCD code.'},
            'predict_affinity': {'type': 'bool', 'default': False, 'description': 'Predict binding affinity.'},
            'recycling_steps': {'type': 'int', 'default': 3, 'range': '1-6', 'description': 'Refinement iterations.'},
            'sampling_steps': {'type': 'int', 'default': 200, 'range': '10-1000', 'description': 'Denoising steps.'},
            'diffusion_samples': {'type': 'int', 'default': 5, 'range': '1-25', 'description': 'Ensemble size.'},
            'step_scale': {'type': 'float', 'default': 1.2, 'description': 'Step size.'},
            'without_potentials': {'type': 'bool', 'default': False, 'description': 'Skip physics potentials.'},
            'write_full_pae': {'type': 'bool', 'default': False, 'description': 'Write the full PAE matrix.'},
            'affinity_mw_correction': {'type': 'bool', 'default': False,
                'description': 'Apply MW correction to affinity.'},
        },
        'output': 'CIF ensemble + PAE matrix + optional affinity.',
        'output_examples': ['<job_id>.cif', '<job_id>_pae.json.gz'],
        'tips': ['High sampling_steps (>200) + diffusion_samples>3 gives the best ensembles.'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'openfold3': {
        'summary': 'OpenFold3: next-generation biomolecular structure prediction.',
        'when_to_use': 'Multi-modal complexes (protein + DNA + RNA + ligand) with state-of-the-art accuracy.',
        'inputs': {
            'sequence': {'type': 'str', 'required': False, 'description': 'Single amino-acid sequence.'},
            'sequences': {'type': 'List[str]', 'required': False,
                'description': 'List of sequences (one per chain) for multimers. '
                               'Auto-converted to chains=[{sequence, id: A, B, ...}] server-side.'},
        },
        'advanced_inputs': {
            'molecule_type': {'type': "['protein','dna','rna']", 'default': 'protein', 'description': 'Polymer type.'},
            'ligand_smiles': {'type': 'str', 'default': '', 'description': 'SMILES ligand.'},
            'ligand_ccd': {'type': 'str', 'default': '', 'description': 'CCD ligand.'},
            'msa_input': {'type': 'str', 'default': '',
                'description': 'Pre-computed MSA in A3M format (skips MSA search).'},
            'num_samples': {'type': 'int', 'default': 1, 'description': 'Number of samples.'},
            'output_format': {'type': "['pdb','cif']", 'default': 'pdb', 'description': 'Structure format.'},
        },
        'output': 'PDB/CIF structure.',
        'output_examples': ['<job_id>.pdb'],
        'tips': ['For mixed protein+DNA+ligand, use chains=[{...}, {...}] explicitly.'],
        'see_also': ['wait', 'download', 'outputs'],
    },
    'molmim': {
        'summary': 'MolMIM: targeted molecular generation with property optimisation.',
        'when_to_use': 'Generate SMILES variants of a seed molecule targeting a property (LogP, QED, etc.).',
        'inputs': {'smiles': {'type': 'str', 'required': True, 'description': 'Seed SMILES.'}},
        'advanced_inputs': {
            'target_logp': {'type': 'float', 'default': None, 'description': 'Target octanol-water partition.'},
            'target_qed': {'type': 'float', 'default': None, 'description': 'Target drug-likeness (0-1).'},
            'target_sas': {'type': 'float', 'default': None, 'description': 'Target synthetic accessibility.'},
            'num_molecules': {'type': 'int', 'default': 32, 'description': 'How many to generate.'},
        },
        'output': 'SMI file with one SMILES per line + property scores.',
        'output_examples': ['<job_id>.smi'],
        'tips': ['Set at least one target_* parameter.'],
        'see_also': ['wait', 'download'],
    },
    'genmol': {
        'summary': 'GenMol: masked-diffusion molecule generation from SAFE fragments.',
        'when_to_use': 'Generate valid, drug-like molecules from a SMILES seed or masked fragment.',
        'inputs': {'smiles': {'type': 'str', 'required': True,
            'description': 'Seed SMILES. Use mask tokens like [*] to scaffold around.'}},
        'advanced_inputs': {
            'num_molecules': {'type': 'int', 'default': 32, 'description': 'How many to generate.'},
            'ranking': {'type': "['qed','logp']", 'default': 'qed', 'description': 'Score used to rank.'},
            'temperature': {'type': 'float', 'default': 1.0, 'description': 'Sampling temperature.'},
        },
        'output': 'JSON with ranked molecules and scores.',
        'output_examples': ['<job_id>.json'],
        'see_also': ['wait', 'download'],
    },
    'msa-search': {
        'summary': 'MSA Search: homology search for an input sequence.',
        'when_to_use': 'Pre-compute an MSA once, then pass it to AlphaFold2 or similar.',
        'inputs': {'sequence': {'type': 'str', 'required': True, 'description': 'Amino-acid sequence.'}},
        'advanced_inputs': {
            'database': {'type': "['uniref100','uniref30','colabfold_env']", 'default': 'uniref100',
                'description': 'MSA database.'},
            'max_seqs': {'type': 'int', 'default': 5000, 'description': 'Max sequences to keep.'},
        },
        'output': 'A3M alignment file (ColabFold format).',
        'output_examples': ['<job_id>.a3m'],
        'tips': ['A3M files are reusable -- save them and pass to alphafold2-re via msa_input.'],
        'see_also': ['wait', 'download'],
    },
}


# ----------------------------------------------------------------------------
# Per-feature reference docs (used by phonos.help('wait'), phonos.docs('wait'))
# ----------------------------------------------------------------------------

_FEATURE_REFERENCE: Dict[str, Dict[str, Any]] = {
    'wait': {
        'title': 'Waiting for jobs to finish',
        'summary': 'Block until a Job reaches a terminal state. The single most-used SDK method.',
        'sections': [
            ('Job.wait(timeout, poll_interval, progress)', [
                'job.wait()                                     # block forever, no progress',
                'job.wait(timeout=600)                          # raise TimeoutError after 10 min',
                'job.wait(poll_interval=2.0)                    # slow polling (default 1.0s)',
                'job.wait(progress=True)                        # single-line ticker on stdout',
                'job.wait(timeout=300, progress=True)           # combined',
            ]),
            ('Returns', [
                'True  -- if status == "completed"',
                'False -- if status == "failed" or "cancelled"',
                'Raises TimeoutError -- if timeout exceeded while still pending/running',
            ]),
            ('Related Job properties', [
                'job.status          -- raw status string',
                'job.status_enum     -- JobStatus enum',
                'job.is_complete     -- True iff status is terminal',
                'job.is_success      -- True iff status == "completed"',
                'job.is_failed       -- True iff status == "failed"',
                'job.refresh()       -- re-fetch state from server (mutates self)',
                'job.execution_time  -- seconds (populated when complete)',
                'job.message         -- error or status message (populated when complete)',
            ]),
            ('Polling vs websockets', [
                'The SDK polls /api/jobs/<id> every poll_interval seconds. There is no',
                'real-time push today, so a long-running job may stay in "running"',
                'for minutes before flipping to "completed" on the next poll.',
            ]),
        ],
        'see_also': ['download', 'outputs', 'logs', 'cancel'],
    },
    'download': {
        'title': 'Downloading outputs',
        'summary': 'Three ways to fetch a job\'s output files: primary output, named artifact, or via Job.download().',
        'sections': [
            ('phonos.download_output(job_or_id, output_path=None)', [
                'phonos.download_output(job)                  # primary output, current dir',
                'phonos.download_output(job, "out.pdb")      # rename',
                'phonos.download_output(job_id, "/workspace/structures/out.pdb")',
                '',
                'Uses GET /api/jobs/<id>/output_content which handles:',
                '  * path resolution (the stored output_file may be an absolute filesystem path)',
                '  * gzip decompression for .gz artifacts',
                '  * Boltz2 CIF -> PDB conversion',
                '  * permission check (project role OR job creator)',
            ]),
            ('phonos.download_artifact(job_or_id, artifact_name, output_path=None)', [
                'phonos.download_artifact(job, "rank_1.sdf")',
                'phonos.download_artifact(job, "rank_2.sdf", "/workspace/dock/rank2.sdf")',
                '',
                'Used when a job produces multiple output files (DiffDock multi-pose,',
                'RFdiffusion multi-design, Boltz2 ensemble). List artifacts first with',
                'phonos.list_outputs(job) to see what is available.',
            ]),
            ('job.download(output_path=None, artifact=None)', [
                'job.download()                                 # same as download_output(job)',
                'job.download(artifact="rank_1.sdf")             # same as download_artifact',
                'job.download("/workspace/structures/out.pdb")   # explicit path',
            ]),
            ('Where do files go by default?', [
                'When output_path is omitted, the file is saved into the current working',
                'directory of the notebook kernel (usually /workspace).',
                'Pass an absolute path to control location.',
            ]),
        ],
        'see_also': ['wait', 'outputs'],
    },
    'outputs': {
        'title': 'Outputs and artifacts',
        'summary': 'Every job produces one or more output files. The SDK exposes them via list_outputs and download_artifact.',
        'sections': [
            ('phonos.list_outputs(job_or_id) -> List[Artifact]', [
                'artifacts = phonos.list_outputs(job)',
                'for a in artifacts:',
                '    print(a.filename, a.size, a.format, a.url)',
                '',
                'Also available as:',
                'job.list_artifacts()  # populates job.artifacts and returns it',
            ]),
            ('Artifact dataclass', [
                'filename       -- name on disk',
                'display_name   -- human-readable name',
                'size           -- bytes (int)',
                'format         -- file extension without dot',
                'type           -- "output", "artifact", etc.',
                'url            -- absolute URL for inspection',
            ]),
            ('Multi-artifact workers', [
                '* DiffDock       -> rank_1.sdf, rank_2.sdf, ..., confidence.csv',
                '* RFdiffusion    -> design_0.pdb, design_1.pdb, ...',
                '* Boltz2 Enhanced-> <job_id>.cif, <job_id>_pae.json.gz',
                '* ProteinMPNN-RE -> <job_id>.fasta, <job_id>_plddt.json',
            ]),
        ],
        'see_also': ['download', 'wait'],
    },
    'logs': {
        'title': 'Execution logs',
        'summary': 'Workers emit structured log entries as the job runs. Fetch them for debugging failed or slow jobs.',
        'sections': [
            ('phonos.get_job_logs(job_id, limit=200) -> List[dict]', [
                'logs = phonos.get_job_logs(job.job_id)',
                'for entry in logs[-10:]:  # last 10',
                '    print(entry["timestamp"], entry["status"], entry["message"])',
            ]),
            ('job.logs() -> List[dict]', [
                'Same as phonos.get_job_logs(self.job_id).',
            ]),
            ('Log entry shape', [
                '{',
                '  "timestamp": "2025-..."',
                '  "status":    "running" | "completed" | "failed" | ...',
                '  "message":   "human-readable progress or error message",',
                '  ... other fields depending on the worker',
                '}',
            ]),
        ],
        'see_also': ['wait', 'cancel', 'errors'],
    },
    'cancel': {
        'title': 'Cancelling jobs',
        'summary': 'Best-effort cancellation. Already-running workers may finish their current step before exiting.',
        'sections': [
            ('phonos.cancel_job(job_id) -> bool', [
                'ok = phonos.cancel_job(job.job_id)',
                'if ok: print("cancelled")',
            ]),
            ('job.cancel() -> bool', [
                'ok = job.cancel()',
            ]),
            ('Notes', [
                '* Cancellation is cooperative. The worker checks the cancel flag between',
                '  sampling steps. Long diffusion jobs may take up to a few minutes to stop.',
                '* A cancelled job ends with status="cancelled". job.is_success will be False,',
                '  job.is_failed will be False.',
                '* You do not get a credit refund for cancelled jobs.',
            ]),
        ],
        'see_also': ['wait', 'logs'],
    },
    'batch': {
        'title': 'Batch submission',
        'summary': 'Submit many similar jobs at once with batch_submit, or iterate in Python with submit_job.',
        'sections': [
            ('phonos.batch_submit(model, items, batch_mode=True) -> List[Job]', [
                'items = [{"sequence": s} for s in long_list_of_sequences]',
                'jobs = phonos.batch_submit("esmfold", items)',
                'for j in jobs:',
                '    j.wait()',
                '',
                'With batch_mode=False each item becomes a separate Job.',
                'With batch_mode=True  the worker treats them as inputs of a single Job.',
            ]),
            ('Manual loop', [
                'for s in sequences:',
                '    job = phonos.boltz2.run(sequence=s)',
                '    jobs.append(job)',
                'for j in jobs:',
                '    j.wait()',
            ]),
            ('Credits and rate limits', [
                'Each job consumes one credit_cost unit. Check phonos.get_credits() before',
                'launching a big batch. Some models enforce a per-user rate limit (e.g.',
                '20 ESMFold runs/day); the SDK surfaces "rate limit exceeded" as a 400.',
            ]),
        ],
        'see_also': ['wait', 'download', 'user'],
    },
    'discovery': {
        'title': 'Model discovery',
        'summary': 'Find out which models are available, online, and what parameters they accept.',
        'sections': [
            ('phonos.list_models(refresh=False) -> List[ModelInfo]', [
                'for m in phonos.list_models():',
                '    print(m.id, m.name, m.available, m.credit_cost)',
                '',
                'Cached after the first call; pass refresh=True to re-fetch.',
            ]),
            ('phonos.list_online_models(refresh=False) -> List[ModelInfo]', [
                'Just the models currently online AND active.',
                '[m.short_name for m in phonos.list_online_models()]',
            ]),
            ('phonos.get_model(name_or_id, refresh=False) -> ModelInfo', [
                'm = phonos.get_model("boltz2")',
                'print(m.description, m.parameters, m.required_parameters)',
            ]),
            ('phonos.models() -> Dict[str, ModelHandle]', [
                'Every registered handle: phonos.esmfold, phonos.boltz2, etc.',
                'Also accessible by alias: phonos.models()["esmfold-re"].',
            ]),
            ('ModelInfo dataclass', [
                'id, name, short_name, description, task, input_modes,',
                'output_extension, available, is_active, credit_cost,',
                'outline_color, node_id, badge, badge_color,',
                'parameters (alias for custom_fields), required_parameters,',
                'raw (full dict from API)',
            ]),
        ],
        'see_also': ['user', 'projects'],
    },
    'projects': {
        'title': 'Project discovery',
        'summary': 'A kernel is associated with one project (NOTEBOOK_PROJECT_ID). Use these helpers to switch or inspect.',
        'sections': [
            ('phonos.get_project_info() -> dict', [
                '{',
                '  "project_id": 3,',
                '  "api_url": "http://172.18.0.1:5000",',
                '  "has_session": True,',
                '  "runtime_level": "basic"',
                '}',
            ]),
            ('phonos.list_projects(refresh=False) -> List[ProjectInfo]', [
                'for p in phonos.list_projects():',
                '    print(p.id, p.name, p.role)',
            ]),
            ('Switching projects', [
                'The project is set when the kernel starts (by the kernel manager).',
                'Restart the kernel with a different project selected in the UI to',
                'switch contexts -- the SDK is stateless beyond reading NOTEBOOK_PROJECT_ID.',
            ]),
        ],
        'see_also': ['user', 'discovery'],
    },
    'user': {
        'title': 'User / credits / account',
        'summary': 'Inspect your account, credit balance, and active job counts.',
        'sections': [
            ('phonos.get_user_info() -> dict', [
                '{',
                '  "id": 1, "username": "alice", "email": "...",',
                '  "credits": 240, "plan": "pro", "storage_used": 12345678,',
                '  "storage_quota": 1073741824,',
                '  "active_job_count": 2, "completed_job_count": 47,',
                '  "failed_job_count": 3, ...',
                '}',
            ]),
            ('phonos.get_credits() -> int', [
                'credits = phonos.get_credits()',
                'if credits is not None and credits < 5:',
                '    print("low credits -- top up soon")',
            ]),
        ],
        'see_also': ['projects', 'batch'],
    },
    'workspace': {
        'title': 'Notebook workspace files',
        'summary': 'Local filesystem helpers (read/write/list) for the kernel container. No API calls.',
        'sections': [
            ('phonos.list_project_files(path="/workspace") -> List[dict]', [
                'for f in phonos.list_project_files("/workspace"):',
                '    print(f["name"], f["size"], f["is_dir"])',
            ]),
            ('phonos.read_project_file(filename, path="/workspace") -> str', [
                'text = phonos.read_project_file("notes.txt", "/workspace")',
            ]),
            ('phonos.write_project_file(filename, content, path="/workspace") -> str', [
                'path = phonos.write_project_file("out.fasta", fasta_text, "/workspace")',
                'print(path)',
            ]),
            ('Available paths', [
                '* /workspace  -- default notebook workspace (mapped to a host directory)',
                '* /project    -- project-scoped files',
                '* anywhere else mounted into the kernel container',
            ]),
        ],
        'see_also': ['download'],
    },
    'errors': {
        'title': 'Errors and troubleshooting',
        'summary': 'Every PhonosError has a precise message. Read it carefully -- it almost always tells you exactly what to fix.',
        'sections': [
            ('Exception', [
                'phonos.PhonosError(RuntimeError) -- raised by every phonos.* call on failure.',
                'Server responses with non-2xx status are converted into PhonosError with the',
                'server\'s message field preserved.',
                '',
                'Error format:',
                '  POST /api/projects/3/jobs failed: 400 BAD REQUEST. protein sequence is required for this model.',
                '   ^method      ^endpoint                          ^status ^reason   ^server message',
            ]),
            ('Common cases', [
                '"Authentication required." (401)',
                '  The notebook session token expired. Re-open or restart the kernel.',
                '',
                '"No project is associated with this notebook kernel"',
                '  The kernel was started without a project selected. Open or select a project.',
                '',
                '"Unknown model requested." (400)',
                '  The model string did not resolve. Pass a model id from phonos.list_models().',
                '',
                '"protein sequence is required for this model." (400)',
                '  Missing primary input. For multimer models you can pass sequences=[...] or',
                '  a single string (auto-wrapped to a one-element list).',
                '',
                '"Insufficient credits." (402)',
                '  Check phonos.get_credits() and top up your plan.',
                '',
                '"rate limit exceeded" (429)',
                '  You sent too many jobs in a short window. Wait and retry.',
                '',
                '"Error: \'str\' object has no attribute \'get\'" -- worker bug',
                '  Old OpenFold3 jobs were stored with sequences=[str,...]. The server now',
                '  auto-converts these to chains=[{sequence, id}, ...].',
            ]),
            ('Inspecting a failed job', [
                'job = phonos.get_job_info(job_id)',
                'print(job.status, job.message)',
                'for entry in phonos.get_job_logs(job_id):',
                '    print(entry)',
            ]),
            ('Network / connection', [
                'Connection refused, NameResolutionError, Timeout -> the SDK wraps these into',
                'PhonosError with a helpful message. Check phonos.get_project_info()["api_url"],',
                'the Phonos server status, and your firewall.',
            ]),
        ],
        'see_also': ['wait', 'logs'],
    },
}


# ----------------------------------------------------------------------------
# Curated code examples per model (used by phonos.<model>.examples and .reference())
# ----------------------------------------------------------------------------

_MODEL_EXAMPLES: Dict[str, List[Dict[str, str]]] = {
    'esmfold': [
        {'title': 'Predict a single protein',
         'code': "# Basic ESMFold prediction\nimport phonos\n\njob = phonos.esmfold.run('MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSH')\njob.wait()\nphonos.download_output(job, 'structure.pdb')\n"},
        {'title': 'Predict and measure execution time',
         'code': "import phonos, time\n\nt0 = time.time()\njob = phonos.esmfold.run('MKTAYIAKQRQISFVKSHRSRQHARE', num_recycles=8, output_format='cif')\nok = job.wait()\nprint(f'ok={ok}  elapsed={time.time()-t0:.1f}s  credits_used={job.credits_used}')\n"},
        {'title': 'Batch a list of sequences',
         'code': "import phonos\n\nseqs = ['MKTAYIAKQRQISFVKSHRSRQHARE', 'MVLSPADKTNVKAAWGKVGAHAGEYGAE', 'MQTLKDFFAAYLEM']\njobs = phonos.batch_submit('esmfold', [{'sequence': s} for s in seqs])\nfor j in jobs:\n    j.wait()\n    print(j.job_id, j.status)\n"},
    ],
    'esmfold-re': [
        {'title': 'Quick refined prediction',
         'code': "import phonos\njob = phonos.esmfold_re.run(sequence='MKTAYIAKQRQISFVKSHRSRQHARE', num_recycles=6)\njob.wait(progress=True)\n"},
        {'title': 'Inspect result + credits',
         'code': "import phonos\njob = phonos.esmfold_re.run('MKTAYIAKQRQISFVKSHRSRQHARE')\njob.wait()\nprint('status:', job.status, 'credits:', job.credits_used, 'time:', job.execution_time)\nphonos.download_output(job)\n"},
    ],
    'alphafold2': [
        {'title': 'Standard AlphaFold2 with MSA',
         'code': "import phonos\njob = phonos.alphafold2.run(sequence='MKKLVLAISLVGLLFISHCQAQEKFTF...', msa_mode='mmseqs2')\njob.wait(progress=True)\n"},
        {'title': 'Use a pre-computed MSA (much faster)',
         'code': "import phonos\njob = phonos.alphafold2.run(\n    sequence='MKKLVLAISLVGLLFISHCQAQEKFTF...',\n    msa_mode='precomputed',\n    custom_params={'msa_input': open('/workspace/my_msa.a3m').read()},\n)\njob.wait()\n"},
    ],
    'alphafold2-re': [
        {'title': 'Hosted AlphaFold2',
         'code': "import phonos\njob = phonos.alphafold2_re.run(sequence='MQTLKDFFAAYLEM...', msa_mode='mmseqs2')\njob.wait()\n"},
    ],
    'alphafold2-re-multimer': [
        {'title': 'Multi-chain complex (chain A + chain B)',
         'code': "import phonos\n\nsequences = ['MQTLKDFFAAYLEM', 'GSHMSLKDFFAAY']\njob = phonos.alphafold2_re_multimer.run(sequences=sequences)\njob.wait()\nphonos.download_output(job, 'complex.pdb')\n"},
        {'title': 'Use chains= for full per-chain control',
         'code': "import phonos\n\njob = phonos.alphafold2_re_multimer.run(\n    sequences=['MQTLKDFFAAYLEM', 'GSHMSLKDFFAAY'],\n    custom_params={'chains': [\n        {'sequence': 'MQTLKDFFAAYLEM', 'id': 'A', 'molecule_type': 'protein'},\n        {'sequence': 'GSHMSLKDFFAAY',  'id': 'B', 'molecule_type': 'protein'},\n    ]},\n)\njob.wait()\n"},
    ],
    'rfdiffusion': [
        {'title': 'De novo design (length range)',
         'code': "import phonos\njob = phonos.rfdiffusion.run(contigs='60-120', num_designs=8)\njob.wait(progress=True)\n\nfor a in phonos.list_outputs(job):\n    print(a.filename, a.size, a.format)\n"},
        {'title': 'Motif scaffolding around hotspots',
         'code': "import phonos\n\njob = phonos.rfdiffusion.run(\n    sequence='ACPFGH...',\n    contigs='40-60/0 40-60',\n    hotspot_residue='40:A,55:A',\n    num_designs=16,\n)\njob.wait(progress=True)\nfor a in phonos.list_outputs(job):\n    phonos.download_artifact(job, a.filename)\n"},
    ],
    'proteinmpnn': [
        {'title': 'Design sequences for a backbone',
         'code': "import phonos\njob = phonos.proteinmpnn.run(sequence='MQTLKDFFAAY...', pdb_file='backbone.pdb', temperature=0.1)\njob.wait()\nphonos.download_output(job, 'designs.fasta')\n"},
    ],
    'proteinmpnn-re': [
        {'title': 'MPNN + ESMFold refinement',
         'code': "import phonos\njob = phonos.proteinmpnn_re.run(\n    sequence='MQTLKDFFAAY...', pdb_file='backbone.pdb',\n    num_sequences=16, refine_cycles=3, plddt_threshold=0.7,\n)\njob.wait()\n"},
    ],
    'diffdock': [
        {'title': 'Dock aspirin to haemoglobin (PDB id)',
         'code': "import phonos\njob = phonos.diffdock.run(\n    protein_pdb_id='4hhb',\n    ligand_smiles='CC(=O)OC1=CC=CC=C1C(=O)O',\n    num_poses=20,\n)\njob.wait(progress=True)\nfor a in phonos.list_outputs(job):\n    phonos.download_artifact(job, a.filename)\n"},
        {'title': 'Dock an SDF ligand against an uploaded protein',
         'code': "import phonos\njob = phonos.diffdock.run(protein_file='protein.pdb', ligand_file='ligand.sdf', num_poses=10)\njob.wait()\n"},
    ],
    'diffdock-re': [
        {'title': 'Refined DiffDock with extra parameters',
         'code': "import phonos\njob = phonos.diffdock_re.run(\n    protein='protein.pdb', ligand='CC(=O)OC1=CC=CC=C1C(=O)O',\n    ligand_file_type='smiles', num_poses=20, steps=30, save_trajectory=True,\n)\njob.wait(progress=True)\n"},
    ],
    'evo2': [
        {'title': 'Masked-token evolution',
         'code': "import phonos\n\njob = phonos.evo2.run(\n    sequence='ATGCGTACGTAGCTAGC...',\n    mask_rate=0.3,\n    num_iterations=3,\n    temperature=1.0,\n)\njob.wait(progress=True)\nphonos.download_output(job, 'evolved.fasta')\n"},
        {'title': 'Low-temperature conservative design',
         'code': "import phonos\njob = phonos.evo2.run(sequence='ATGCGTACGTAGCTAGC...', mask_rate=0.2, num_iterations=2, temperature=0.5)\njob.wait()\n"},
    ],
    'boltz2': [
        {'title': 'Predict a single protein (Boltz-2)',
         'code': "import phonos\n\njob = phonos.boltz2.run('GIVEQCCTSICSLYQLENYCN')\njob.wait()\nphonos.download_output(job, 'structure.cif')\n"},
        {'title': 'Protein + ligand (CCD) with affinity',
         'code': "import phonos\n\njob = phonos.boltz2.run(\n    sequence='GIVEQCCTSICSLYQLENYCN',\n    ligand_input_type='ccd', ligand_ccd='ATP',\n    predict_affinity=True, recycling_steps=3,\n)\njob.wait(progress=True)\nprint(phonos.list_outputs(job))\n"},
        {'title': 'Protein + custom SMILES ligand',
         'code': "import phonos\njob = phonos.boltz2.run(\n    sequence='MQTLKDFFAAYLEM...',\n    ligand_input_type='smiles',\n    ligand_smiles='CC(=O)OC1=CC=CC=C1C(=O)O',  # aspirin\n    predict_affinity=True,\n)\njob.wait()\n"},
        {'title': 'Multimer (multiple chains)',
         'code': "import phonos\n\njob = phonos.boltz2.run(\n    sequences=['MQTLKDFFAAYLEM', 'GSHMSLKDFFAAY'],\n    recycling_steps=3,\n)\njob.wait(progress=True)\n"},
    ],
    'boltz2-enhanced': [
        {'title': 'Production ensemble run',
         'code': "import phonos\n\njob = phonos.boltz2_enhanced.run(\n    sequence='GIVEQCCTSICSLYQLENYCN',\n    ligand_input_type='ccd', ligand_ccd='ATP',\n    predict_affinity=True,\n    sampling_steps=300, diffusion_samples=5,\n    write_full_pae=True,\n)\njob.wait(progress=True)\n"},
    ],
    'openfold3': [
        {'title': 'Single protein (monomer mode)',
         'code': "import phonos\njob = phonos.openfold3.run('MQTLKDFFAAYLEM')\njob.wait()\n"},
        {'title': 'Multi-chain complex',
         'code': "import phonos\njob = phonos.openfold3.run(sequences=['MQTLKDFFAAYLEM', 'GSHMSLKDFFAAY'])\njob.wait()\n"},
        {'title': 'Mixed protein + ligand (CCD)',
         'code': "import phonos\njob = phonos.openfold3.run(\n    sequence='MQTLKDFFAAYLEM',\n    ligand_ccd='ATP',\n    custom_params={'ligand_input_type': 'ccd', 'ligand_id': 'L'},\n)\njob.wait(progress=True)\n"},
    ],
    'molmim': [
        {'title': 'Generate drug-like variants of a seed',
         'code': "import phonos\njob = phonos.molmim.run(\n    smiles='CC(=O)OC1=CC=CC=C1C(=O)O',  # aspirin\n    target_logp=2.5, target_qed=0.8, num_molecules=64,\n)\njob.wait()\nphonos.download_output(job, 'aspirin_variants.smi')\n"},
    ],
    'genmol': [
        {'title': 'Generate molecules from a SAFE mask',
         'code': "import phonos\njob = phonos.genmol.run(\n    smiles='[*]C(=O)N[C@@H]([*])C',  # masked scaffold\n    num_molecules=64, ranking='qed',\n)\njob.wait()\n"},
    ],
    'msa-search': [
        {'title': 'Pre-compute MSA for later reuse',
         'code': "import phonos\njob = phonos.msa_search.run(sequence='MVLSPADKTNVKAAWGKVGAHAGEYGAE...', database='uniref100')\njob.wait()\nphonos.download_output(job, 'query.a3m')\n"},
    ],
}


# ----------------------------------------------------------------------------
# Doc object + public help surface
# ----------------------------------------------------------------------------


@dataclass
class Doc:
    """Structured documentation object returned by phonos.docs().

    Attributes:
        topic    -- the help topic that produced this doc (e.g. 'boltz2', 'wait').
        title    -- human-readable title.
        summary  -- one-line summary.
        body     -- full text body (same as phonos.help(topic)).
        sections -- list of (heading, [lines]) tuples for programmatic access.
        see_also -- list of related topic names.
    """
    topic: str
    title: str
    summary: str
    body: str
    sections: List[Tuple[str, List[str]]] = field(default_factory=list)
    see_also: List[str] = field(default_factory=list)

    def _repr_html_(self) -> str:    # pragma: no cover
        body_html = (self.body or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        body_html = body_html.replace('\n', '<br>')
        return (
            f"<div style='border:1px solid #3f3f46;border-radius:8px;padding:12px;"
            f"background:#18181b;color:#f4f4f5;font-family:monospace;'>"
            f"<div style='font-size:14px;font-weight:600;color:#f4f4f5'>{self.title}</div>"
            f"<div style='font-size:12px;color:#a1a1aa;margin:4px 0 8px 0'>{self.summary}</div>"
            f"<div style='font-size:12px;line-height:1.5'>{body_html}</div>"
            f"</div>"
        )

    def __repr__(self) -> str:    # pragma: no cover
        return f"Doc(topic={self.topic!r}, title={self.title!r})"


def _get_reference(model_id: str) -> Optional[Dict[str, Any]]:
    if not model_id:
        return None
    return _MODEL_REFERENCE.get(model_id.lower()) or _MODEL_REFERENCE.get(model_id)


def _get_examples(model_id: str) -> List[Dict[str, str]]:
    if not model_id:
        return []
    return _MODEL_EXAMPLES.get(model_id.lower()) or _MODEL_EXAMPLES.get(model_id) or []


def _get_feature(topic: str) -> Optional[Dict[str, Any]]:
    if not topic:
        return None
    return _FEATURE_REFERENCE.get(topic.lower()) or _FEATURE_REFERENCE.get(topic)


def _format_help_block(model_id: str, ref: Dict[str, Any],
                       examples: List[Dict[str, str]],
                       short_name: Optional[str] = None) -> str:
    lines: List[str] = []
    if not short_name:
        short_name = ref.get('summary', '').split(':', 1)[0].strip()
    lines.append('=' * 78)
    lines.append(f"  phonos.{short_name}   (canonical id: {model_id})")
    lines.append('=' * 78)
    if ref.get('summary'):
        lines.append(ref['summary'])
    if ref.get('when_to_use'):
        lines.append(''); lines.append('When to use:')
        lines.append('  ' + ref['when_to_use'])
    if ref.get('inputs'):
        lines.append(''); lines.append('Inputs:')
        for name, spec in ref['inputs'].items():
            req = ' (required)' if spec.get('required') else ''
            lines.append(f"  - {name}: {spec.get('type', 'any')}{req}")
            if spec.get('description'):
                lines.append('      ' + spec['description'])
    if ref.get('advanced_inputs'):
        lines.append(''); lines.append('Advanced inputs:')
        for name, spec in ref['advanced_inputs'].items():
            bits = [spec.get('type', 'any')]
            if 'default' in spec and spec['default'] not in (None, ''):
                bits.append(f"default={spec['default']!r}")
            if 'range' in spec:
                bits.append(spec['range'])
            lines.append(f"  - {name}: {' '.join(bits)}")
            if spec.get('description'):
                lines.append('      ' + spec['description'])
    if ref.get('output'):
        lines.append(''); lines.append('Output:')
        lines.append('  ' + ref['output'])
        if ref.get('output_examples'):
            lines.append('  Typical filenames: ' + ', '.join(ref['output_examples']))
    if ref.get('tips'):
        lines.append(''); lines.append('Tips:')
        for tip in ref['tips']:
            lines.append('  * ' + tip)
    if examples:
        lines.append(''); lines.append('Examples:')
        for i, ex in enumerate(examples, 1):
            lines.append(''); lines.append(f"  [{i}] {ex['title']}")
            lines.append('  ' + ('-' * 70))
            for code_line in ex['code'].rstrip().splitlines():
                lines.append('    ' + code_line)
    if ref.get('see_also'):
        lines.append(''); lines.append('See also:')
        for t in ref['see_also']:
            lines.append(f"  phonos.help('{t}')   -- {_FEATURE_REFERENCE.get(t, {}).get('title', t)}")
    return '\n'.join(lines)


def _format_feature_block(key: str, ref: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('=' * 78)
    lines.append(f"  phonos.help('{key}')   {ref.get('title', key)}")
    lines.append('=' * 78)
    if ref.get('summary'):
        lines.append(ref['summary'])
    for heading, content_lines in ref.get('sections', []):
        lines.append(''); lines.append(heading)
        lines.append('-' * len(heading))
        lines.extend(content_lines)
    if ref.get('see_also'):
        lines.append(''); lines.append('See also:')
        for t in ref['see_also']:
            other = _FEATURE_REFERENCE.get(t, {})
            label = other.get('title', t)
            lines.append(f"  phonos.help('{t}')   -- {label}")
    return '\n'.join(lines)


# Topic routing -- which feature key handles which alias
_FEATURE_ALIASES: Dict[str, str] = {
    'sdk': 'overview',
    'guide': 'overview',
    'overview': 'overview',
    'cheatsheet': 'overview',

    'wait': 'wait', 'waiting': 'wait', 'job.wait': 'wait',

    'download': 'download', 'downloads': 'download', 'job.download': 'download',
    'outputs': 'outputs', 'output': 'outputs', 'artifacts': 'outputs',
    'list_outputs': 'outputs',

    'logs': 'logs', 'log': 'logs', 'job.logs': 'logs',
    'cancel': 'cancel', 'cancellation': 'cancel', 'job.cancel': 'cancel',

    'batch': 'batch', 'batches': 'batch', 'batch_submit': 'batch',

    'discovery': 'discovery', 'models': 'discovery', 'list_models': 'discovery',
    'list_online_models': 'discovery', 'get_model': 'discovery',

    'projects': 'projects', 'list_projects': 'projects', 'project': 'projects',
    'get_project_info': 'projects',

    'user': 'user', 'credits': 'user', 'get_user_info': 'user', 'get_credits': 'user',
    'account': 'user',

    'workspace': 'workspace', 'files': 'workspace', 'project_files': 'workspace',
    'read_project_file': 'workspace', 'write_project_file': 'workspace',

    'errors': 'errors', 'troubleshooting': 'errors', 'phonoserror': 'errors',
    'error': 'errors', 'exceptions': 'errors',
}


def _resolve_topic(topic: str) -> Optional[Tuple[str, str, str]]:
    """Return (kind, key, body) where kind is 'overview' | 'feature' | 'model'.

    body is the formatted text. Returns None if the topic is unknown.
    """
    if not topic:
        return None
    t = topic.strip().lower()

    # Feature aliases
    fkey = _FEATURE_ALIASES.get(t)
    if fkey:
        if fkey == 'overview':
            return ('overview', 'overview', guide())
        ref = _get_feature(fkey)
        if ref is not None:
            return ('feature', fkey, _format_feature_block(fkey, ref))

    # Direct feature lookup (e.g. 'wait' explicitly)
    ref = _get_feature(t)
    if ref is not None:
        return ('feature', t, _format_feature_block(t, ref))

    # Model lookup
    canonical, _, _ = _resolve_model_id(t)
    handle = _handles.get(t.replace('-', '_')) or _handles.get(t)
    short_name = handle.short_name if handle else None
    mref = _get_reference(canonical) or _get_reference(t) or _get_reference(short_name or '')
    examples = _get_examples(canonical) or _get_examples(t) or _get_examples(short_name or '')
    if mref is not None:
        return ('model', canonical, _format_help_block(canonical, mref, examples, short_name=short_name))

    # Fall back to ModelHandle.help() if a handle exists
    if handle:
        return ('model', canonical, handle.help())

    return None


def guide() -> str:
    """Return the one-page SDK overview / full walkthrough cheat sheet.

    Equivalent to phonos.help() with no arguments.
    """
    return (__doc__ or "Phonos SDK").strip()


def help(topic: Optional[str] = None) -> str:
    """Print detailed help for a feature, model, or the SDK in general.

    Usage:
        phonos.help()                  -- one-page SDK cheat sheet
        phonos.help('overview')        -- same
        phonos.help('guide')           -- full SDK walkthrough
        phonos.help('api')             -- programmatic API reference

        Feature topics:
            phonos.help('wait')        -- Job.wait() and polling
            phonos.help('download')    -- download_output / download_artifact
            phonos.help('outputs')     -- outputs / artifacts
            phonos.help('logs')        -- get_job_logs
            phonos.help('cancel')      -- cancel_job / Job.cancel
            phonos.help('batch')       -- batch_submit
            phonos.help('discovery')   -- list_models / get_model
            phonos.help('projects')    -- list_projects / get_project_info
            phonos.help('user')        -- get_user_info / get_credits
            phonos.help('workspace')   -- read/write/list project files
            phonos.help('errors')      -- PhonosError + troubleshooting

        Per-model:
            phonos.help('boltz2')      -- detailed help for one model
            phonos.help('esmfold-re')  -- hyphenated aliases work
            phonos.boltz2.reference()  -- same content via the handle
    """
    if not topic:
        return guide()
    resolved = _resolve_topic(str(topic).strip().lower())
    if resolved is not None:
        return resolved[2]
    return (
        f"No help topic matches '{topic}'.\\n\\n"
        "Try one of:\\n"
        "  phonos.help()           -- full overview\\n"
        "  phonos.help('api')      -- programmatic reference\\n"
        "  phonos.help('wait')     -- Job.wait() and polling\\n"
        "  phonos.help('download') -- download_output / download_artifact\\n"
        "  phonos.help('outputs')  -- outputs / artifacts\\n"
        "  phonos.help('logs')     -- get_job_logs\\n"
        "  phonos.help('cancel')   -- cancel_job\\n"
        "  phonos.help('batch')    -- batch_submit\\n"
        "  phonos.help('discovery')-- list_models / get_model\\n"
        "  phonos.help('projects') -- list_projects\\n"
        "  phonos.help('user')     -- get_user_info / get_credits\\n"
        "  phonos.help('workspace')-- file helpers\\n"
        "  phonos.help('errors')   -- troubleshooting\\n"
        "  phonos.help('<model>')  -- e.g. phonos.help('boltz2')\\n"
    )


def docs(topic: Optional[str] = None) -> Doc:
    """Return a structured :class:`Doc` object for ``topic``.

    The Doc has a body (same as ``phonos.help(topic)``) plus structured
    sections and a ``see_also`` list of related topic names. Useful for
    building UIs or notebooks that render documentation.
    """
    if not topic:
        resolved = ('overview', 'overview', guide())
    else:
        resolved = _resolve_topic(str(topic).strip().lower())
        if resolved is None:
            return Doc(
                topic=str(topic),
                title=f'Unknown topic: {topic}',
                summary='No documentation matches this topic.',
                body=help(topic),
                sections=[],
                see_also=[],
            )

    kind, key, body = resolved
    if kind == 'feature':
        ref = _get_feature(key) or {}
        title = ref.get('title', key)
        summary = ref.get('summary', '')
        return Doc(
            topic=key,
            title=title,
            summary=summary,
            body=body,
            sections=list(ref.get('sections', [])),
            see_also=list(ref.get('see_also', [])),
        )
    if kind == 'model':
        ref = _get_reference(key) or {}
        return Doc(
            topic=key,
            title=f"phonos.{ref.get('summary', key).split(':',1)[0].strip()} ({key})",
            summary=ref.get('summary', ''),
            body=body,
            sections=[],
            see_also=list(ref.get('see_also', [])),
        )
    return Doc(topic='overview', title='Phonos SDK overview', summary='', body=body)


def api() -> Dict[str, Any]:
    """Return a structured programmatic reference for every public function.

    Each entry includes signature, docstring (truncated), short description,
    return type, and the topic key for ``phonos.help(...)``.
    """
    import inspect as _inspect
    items: List[Dict[str, Any]] = []

    def _entry(fn, topic=None, kind='function'):
        sig = None
        try:
            sig = str(_inspect.signature(fn))
        except Exception:
            sig = '(?)'
        doc = (_inspect.getdoc(fn) or '').strip().splitlines()
        summary = doc[0] if doc else ''
        items.append({
            'name': fn.__name__,
            'kind': kind,
            'topic': topic,
            'signature': f"{fn.__name__}{sig}",
            'summary': summary,
        })

    # Discovery
    _entry(list_models, 'discovery')
    _entry(list_online_models, 'discovery')
    _entry(get_model, 'discovery')
    _entry(models, 'discovery')

    # Projects / user
    _entry(list_projects, 'projects')
    _entry(get_project_info, 'projects')
    _entry(get_user_info, 'user')
    _entry(get_credits, 'user')

    # Jobs
    _entry(submit_job, 'batch')
    _entry(batch_submit, 'batch')
    _entry(get_job, 'logs')
    _entry(get_job_info, 'logs')
    _entry(list_jobs, 'logs')
    _entry(cancel_job, 'cancel')
    _entry(get_job_logs, 'logs')

    # Outputs
    _entry(download_output, 'download')
    _entry(download_artifact, 'download')
    _entry(list_outputs, 'outputs')

    # Workspace
    _entry(list_project_files, 'workspace')
    _entry(read_project_file, 'workspace')
    _entry(write_project_file, 'workspace')

    # Help / docs
    _entry(help, 'overview')
    _entry(docs, 'overview')
    _entry(guide, 'overview')
    _entry(api, 'overview')

    # Per-model handles
    handle_items = []
    for alias, handle in sorted(_handles.items()):
        handle_items.append({
            'name': alias,
            'kind': 'model',
            'topic': alias,
            'short_name': handle.short_name,
            'canonical_id': handle.id,
            'primary_input': handle.primary_input,
            'description': handle.description,
            'help': f"phonos.help('{alias}')",
            'reference': f"phonos.{handle.short_name}.reference()",
            'examples': f"phonos.{handle.short_name}.examples",
        })

    return {
        'functions': items,
        'models': handle_items,
        'topics': sorted(set(
            list(_FEATURE_REFERENCE.keys())
            + list(_MODEL_REFERENCE.keys())
            + ['overview', 'guide', 'api']
        )),
        'features': sorted(_FEATURE_REFERENCE.keys()),
    }


# ----------------------------------------------------------------------------
# Public surface
# ----------------------------------------------------------------------------

# Runtime bridges (experimental): phonos.wolfram (Python↔Wolfram session
# bridge), phonos.quantum (Quantum Settings-aware execution helpers), and
# phonos.hpc (classical compute clusters).
# Imported last so their lazy references back into this package resolve.
from . import wolfram  # noqa: E402
from . import quantum  # noqa: E402
from . import hpc  # noqa: E402

__version__ = '2.2.0'


def __getattr__(name: str):
    """Dynamic attribute lookups on the package.

    - Hyphenated model ids (``phonos.esmfold-re``): Python identifiers cannot
      contain hyphens, so snake_case is canonical and hyphenated names are
      resolved here on demand to the underlying :class:`ModelHandle`.
    - HPC cluster sugar (``phonos.hpc_cluster1``): any ``hpc_<name>``
      attribute resolves to ``phonos.hpc.cluster('<name>')`` so users can
      write ``phonos.hpc_cluster1.compute('a + b', a=1, b=2)``.
    - Chimera lang bridge (``phonos.lang``, ``phonos.r``): imported lazily so
      notebooks that never touch C/C++/Rust/R cells don't load rpy2.
    """
    if name in ('lang', 'r'):
        from . import lang as _lang
        return _lang.r if name == 'r' else _lang
    if name.startswith('hpc_') and len(name) > 4:
        return hpc.cluster(name[4:])
    if '-' in name:
        handle = globals().get('_handles', {}).get(name)
        if handle is not None:
            return handle
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # enums / dataclasses
    'JobStatus', 'ModelInfo', 'Artifact', 'ProjectInfo', 'Job', 'ModelHandle', 'PhonosError',
    # discovery
    'list_models', 'list_online_models', 'get_model', 'models',
    'get_project_info', 'list_projects', 'get_user_info', 'get_credits',
    # jobs
    'submit_job', 'batch_submit', 'get_job', 'get_job_info', 'list_jobs',
    'cancel_job', 'get_job_logs',
    # outputs
    'download_output', 'download_artifact', 'list_outputs',
    # files
    'list_project_files', 'read_project_file', 'write_project_file',
    # runtime bridges (experimental)
    'wolfram', 'quantum',
    # docs / help
    'help', 'guide',
] + list(_handles.keys())
