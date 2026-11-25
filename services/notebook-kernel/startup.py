# Phonos Notebook Startup Script
# This runs automatically when a kernel starts

import os
import sys
import warnings
import json as _json
import time as _time
from datetime import datetime as _datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass as _dataclass

# Suppress common warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

# Add workspace and project to path
sys.path.insert(0, '/workspace')
sys.path.insert(0, '/project')

# Set working directory
os.chdir('/workspace')

# Configure matplotlib for inline display in Jupyter
import matplotlib
matplotlib.use('module://matplotlib_inline.backend_inline')
import matplotlib.pyplot as plt

# Configure IPython for inline display
try:
    from IPython import get_ipython
    ipython = get_ipython()
    if ipython is not None:
        ipython.run_line_magic('matplotlib', 'inline')
        from matplotlib_inline.backend_inline import set_matplotlib_formats
        set_matplotlib_formats('png', 'svg')
except Exception:
    matplotlib.use('Agg')
    pass

# Set a nice default style
try:
    plt.style.use('seaborn-v0_8-darkgrid')
except:
    plt.style.use('ggplot')

# Configure pandas display options
import pandas as pd
pd.set_option('display.max_columns', 50)
pd.set_option('display.max_rows', 100)
pd.set_option('display.width', None)
pd.set_option('display.max_colwidth', 100)

# Import common libraries
import numpy as np

# Phonos SDK - AI Models access
# Inline implementation for immediate availability without Docker rebuild
_BASE_URL = os.environ.get('PHONOS_API_URL', 'http://localhost:5000')
_PROJECT_ID = os.environ.get('NOTEBOOK_PROJECT_ID')
_SESSION_TOKEN = os.environ.get('NOTEBOOK_SESSION_ID') or os.environ.get('NOTEBOOK_SESSION_TOKEN')

# Primary input field per model — used by the new ``phonos.<model>.run(value)``
# shorthand so users can write ``phonos.esmfold.run('MVLSPAD...')`` instead of
# ``phonos.esmfold(sequence='MVLSPAD...')``.
_PRIMARY_INPUT = {
    'esmfold': 'sequence',
    'esmfold-re': 'sequence',
    'alphafold2': 'sequence',
    'alphafold2-re': 'sequence',
    'alphafold2-re-multimer': 'sequences',
    'rfdiffusion': 'contigs',
    'proteinmpnn': 'sequence',
    'proteinmpnn-re': 'sequence',
    'diffdock': 'ligand_smiles',
    'diffdock-re': 'ligand_smiles',
    'boltz': 'sequences',
    'boltz1': 'sequences',
    'boltz2': 'sequences',
    'evo': 'sequence',
    'evo2': 'sequence',
    'molmim': 'smiles',
    'genmol': 'smiles',
    'openfold3': 'sequences',
    'msa-search': 'sequence',
}


class _ModelHandle:
    """Uniform per-model entry point used as ``phonos.<model>.run(input, **kwargs)``.

    Backwards-compatible: ``phonos.<model>(**kwargs)`` still works.
    """
    __slots__ = ('id', 'short_name', 'primary_input', 'description')

    def __init__(self, model_id: str, primary_input: str, description: str = ''):
        self.id = model_id
        self.short_name = model_id
        self.primary_input = primary_input
        self.description = description

    def __call__(self, **kwargs) -> 'Job':
        return submit_job(model=self.id, **kwargs)

    def __repr__(self):
        return f"ModelHandle(id='{self.id}', short_name='{self.short_name}')"

    def run(self, input=None, **kwargs) -> 'Job':
        if input is None:
            pass
        elif isinstance(input, str):
            kwargs[self.primary_input] = input
        elif isinstance(input, dict):
            kwargs.update(input)
        elif isinstance(input, (list, tuple)):
            kwargs[self.primary_input] = list(input)
        else:
            kwargs[self.primary_input] = input
        return submit_job(model=self.id, **kwargs)

    def help(self) -> str:
        return (
            f"phonos.{self.short_name}.run(input, **kwargs)\n"
            f"  primary input   : {self.primary_input}\n"
            f"  example         : phonos.{self.short_name}.run('<your {self.primary_input}>')"
        )

def _api_request(method: str, endpoint: str, data: Dict = None, params: Dict = None) -> Dict:
    """Make authenticated API request to Phonos backend."""
    import requests
    if not _SESSION_TOKEN:
        raise RuntimeError("Not running in a notebook context (NOTEBOOK_SESSION_TOKEN not set)")

    headers = {
        'X-Notebook-Session': _SESSION_TOKEN,
        'X-Project-ID': str(_PROJECT_ID) if _PROJECT_ID else '',
        'Content-Type': 'application/json'
    }

    url = f"{_BASE_URL}{endpoint}"
    try:
        response = requests.request(method=method, url=url, headers=headers, json=data, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        raise RuntimeError(f"API request failed: {e}")

@_dataclass
class Job:
    job_id: str
    model: str
    name: Optional[str]
    status: str
    created_at: _datetime
    updated_at: Optional[_datetime]
    message: Optional[str]
    execution_time: Optional[float]
    output_file: Optional[str]
    output_file_size: Optional[int]
    sequence: Optional[str]
    input_file_id: Optional[str]
    project_id: str

    @property
    def is_complete(self) -> bool:
        return self.status in ['completed', 'failed', 'cancelled']

    @property
    def is_success(self) -> bool:
        return self.status == 'completed'

    def result(self, timeout: float = 300.0, poll_interval: float = 1.0) -> Dict:
        start_time = _time.time()
        while not self.is_complete:
            if _time.time() - start_time > timeout:
                raise TimeoutError(f"Job {self.job_id} did not complete within {timeout}s")
            _time.sleep(poll_interval)
            self.refresh()

        if self.status == 'completed':
            return {'success': True, 'job_id': self.job_id, 'output_file': self.output_file, 'execution_time': self.execution_time}
        return {'success': False, 'job_id': self.job_id, 'status': self.status, 'message': self.message}

    def refresh(self) -> 'Job':
        updated = get_job(self.job_id)
        self.status = updated.status
        self.updated_at = updated.updated_at
        self.message = updated.message
        self.execution_time = updated.execution_time
        self.output_file = updated.output_file
        self.output_file_size = updated.output_file_size
        return self

    def wait(self, poll_interval: float = 1.0) -> bool:
        self.result(timeout=float('inf'), poll_interval=poll_interval)
        return self.is_success

    def cancel(self) -> bool:
        try:
            response = _api_request('POST', f'/api/jobs/{self.job_id}/cancel')
            return response.get('status') == 'success'
        except:
            return False

    def logs(self) -> List[Dict]:
        return get_job_logs(self.job_id)

    def __repr__(self):
        return f"Job(job_id='{self.job_id}', model='{self.model}', status='{self.status}')"

def list_models() -> List[Dict]:
    """List all available AI models."""
    response = _api_request('GET', '/api/available_models')
    return response if isinstance(response, list) else response.get('models', [])

def list_online_models() -> List[Dict]:
    """List only models currently marked as available/online."""
    return [m for m in list_models() if m.get('available', True)]

def get_model(model_id: str) -> Optional[Dict]:
    """Get details of a specific model."""
    models = list_models()
    for m in models:
        if m.get('id') == model_id or m.get('short_name', '').lower() == model_id.lower():
            return m
    return None

def list_project_files(path: str = '/workspace') -> List[Dict]:
    """List files in the notebook workspace."""
    import os, stat
    from datetime import datetime
    results = []
    try:
        entries = os.listdir(path)
    except FileNotFoundError:
        return []
    for entry in sorted(entries):
        full = os.path.join(path, entry)
        try:
            st = os.stat(full)
            results.append({
                'name': entry, 'size': st.st_size,
                'is_dir': stat.S_ISDIR(st.st_mode),
                'modified_at': datetime.fromtimestamp(st.st_mtime).isoformat()
            })
        except OSError:
            results.append({'name': entry, 'size': 0, 'is_dir': False, 'modified_at': ''})
    return results

def read_project_file(filename: str, path: str = '/workspace') -> str:
    """Read a file from the notebook workspace."""
    import os
    for base in [path, '/workspace', '/project']:
        filepath = os.path.join(base, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            with open(filepath, 'r') as f:
                return f.read()
    raise FileNotFoundError(f"File not found: {filename}")

def write_project_file(filename: str, content: str, path: str = '/workspace') -> str:
    """Write a file to the notebook workspace."""
    import os
    filepath = os.path.join(path, filename)
    with open(filepath, 'w') as f:
        f.write(content)
    return filepath

def submit_job(model: str, name: str = None, **kwargs) -> Job:
    """Submit a job to an AI model."""
    payload = {'model': model, 'source': 'notebook'}
    if name:
        payload['name'] = name
    payload.update(kwargs)
    if _PROJECT_ID:
        endpoint = f'/api/projects/{_PROJECT_ID}/jobs'
    else:
        endpoint = '/api/jobs'
    try:
        response = _api_request('POST', endpoint, data=payload)
    except Exception:
        if _PROJECT_ID:
            response = _api_request('POST', '/api/jobs', data=payload)
        else:
            raise
    return Job(
        job_id=response.get('job_id', ''), model=model, name=name, status='pending',
        created_at=_datetime.now(), updated_at=None, message=response.get('message'),
        execution_time=None, output_file=None, output_file_size=None,
        sequence=kwargs.get('sequence'), input_file_id=None, project_id=_PROJECT_ID or ''
    )

def get_job(job_id: str) -> Job:
    """Get job details by ID."""
    response = _api_request('GET', f'/api/jobs/{job_id}')
    created_at = response.get('created_at')
    if isinstance(created_at, str):
        try:
            created_at = _datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        except ValueError:
            created_at = _datetime.now()
    return Job(
        job_id=response.get('job_id', job_id), model=response.get('model', ''), name=response.get('name'),
        status=response.get('status', 'pending'), created_at=created_at,
        updated_at=_datetime.fromisoformat(response['updated_at'].replace('Z', '+00:00')) if isinstance(response.get('updated_at'), str) else None,
        message=response.get('message'), execution_time=response.get('execution_time'),
        output_file=response.get('output_file'), output_file_size=response.get('output_file_size'),
        sequence=response.get('sequence'), input_file_id=response.get('input_file_id'),
        project_id=response.get('project_id', _PROJECT_ID or '')
    )

def list_jobs(limit: int = 50) -> List[Job]:
    """List all jobs for the current project."""
    if _PROJECT_ID:
        response = _api_request('GET', f'/api/projects/{_PROJECT_ID}/jobs', params={'limit': limit})
    else:
        response = _api_request('GET', '/api/jobs', params={'limit': limit})
    jobs = []
    for j in response:
        created_at = j.get('created_at')
        if isinstance(created_at, str):
            try:
                created_at = _datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            except ValueError:
                created_at = _datetime.now()
        jobs.append(Job(
            job_id=j.get('job_id', ''), model=j.get('model', ''), name=j.get('name'),
            status=j.get('status', 'pending'), created_at=created_at,
            updated_at=_datetime.fromisoformat(j['updated_at'].replace('Z', '+00:00')) if isinstance(j.get('updated_at'), str) else None,
            message=j.get('message'), execution_time=j.get('execution_time'),
            output_file=j.get('output_file'), output_file_size=j.get('output_file_size'),
            sequence=j.get('sequence'), input_file_id=j.get('input_file_id'),
            project_id=j.get('project_id', _PROJECT_ID or '')
        ))
    return jobs

def cancel_job(job_id: str) -> bool:
    """Cancel a running job."""
    try:
        response = _api_request('POST', f'/api/jobs/{job_id}/cancel')
        return response.get('status') == 'success'
    except:
        return False

def get_job_logs(job_id: str) -> List[Dict]:
    """Get execution logs for a job."""
    response = _api_request('GET', f'/api/jobs/{job_id}/logs')
    return response.get('logs', [])

def download_output(job_id: str, output_path: str = None) -> str:
    """Download job output file."""
    import requests
    job = get_job(job_id)
    if not job.output_file:
        raise ValueError(f"Job {job_id} has no output file")
    output_url = f"{_BASE_URL}{job.output_file}"
    headers = {'X-Notebook-Session': _SESSION_TOKEN, 'X-Project-ID': str(_PROJECT_ID) if _PROJECT_ID else ''}
    response = requests.get(output_url, headers=headers, timeout=60)
    response.raise_for_status()
    filename = output_path or job.output_file.split('/')[-1]
    with open(filename, 'wb') as f:
        f.write(response.content)
    return filename

def esmfold(sequence: str, num_recycles: int = 4, output_format: str = 'pdb') -> Job:
    """Predict protein structure using ESMFold."""
    return submit_job(model='esmfold', sequence=sequence, num_recycles=num_recycles, output_format=output_format)

def diffdock(protein_file: str = None, protein_pdb_id: str = None, protein_uniprot_id: str = None, ligand_smiles: str = None, ligand_file: str = None) -> Job:
    """Run molecular docking with DiffDock."""
    payload = {}
    if protein_file:
        payload.update({'protein_input_type': 'file', 'protein_file': protein_file})
    elif protein_pdb_id:
        payload.update({'protein_input_type': 'pdb', 'protein_pdb_id': protein_pdb_id})
    elif protein_uniprot_id:
        payload.update({'protein_input_type': 'uniprot', 'protein_uniprot_id': protein_uniprot_id})
    if ligand_smiles:
        payload.update({'ligand_input_type': 'smiles', 'ligand_smiles': ligand_smiles})
    elif ligand_file:
        payload.update({'ligand_input_type': 'file', 'ligand_file': ligand_file})
    return submit_job(model='diffdock', **payload)

def rfdiffusion(sequence: str = None, contigs: str = None, hotspot_residue: str = None, diffuse_templates: bool = False, num_designs: int = 1) -> Job:
    """Design proteins using RFdiffusion."""
    payload = {'num_designs': num_designs, 'diffuse_templates': diffuse_templates}
    if sequence:
        payload['sequence'] = sequence
    if contigs:
        payload['contigs'] = contigs
    if hotspot_residue:
        payload['hotspot_residue'] = hotspot_residue
    return submit_job(model='rfdiffusion', **payload)

def boltz2(sequences: List[str], species: str = 'human', without_potentials: bool = False) -> Job:
    """Predict protein complexes using Boltz-2."""
    return submit_job(model='boltz2', sequences=sequences, species=species, without_potentials=without_potentials)

def evo2(sequence: str, mask_rate: float = 0.3, num_iterations: int = 1) -> Job:
    """Evolve sequences using Evo-2."""
    return submit_job(model='evo2', sequence=sequence, mask_rate=mask_rate, num_iterations=num_iterations)

# Try to import the full SDK package, fallback to inline version
try:
    import phonos as _phonos_pkg
    _phonos = _phonos_pkg
    print("Phonos SDK (full package) loaded successfully")
except ImportError:
    _inline_fns = {
        'list_models': list_models, 'get_model': get_model, 'submit_job': submit_job,
        'get_job': get_job, 'list_jobs': list_jobs, 'cancel_job': cancel_job,
        'get_job_logs': get_job_logs, 'download_output': download_output,
        'esmfold': esmfold, 'diffdock': diffdock, 'rfdiffusion': rfdiffusion,
        'boltz2': boltz2, 'evo2': evo2,
        'list_project_files': list_project_files, 'read_project_file': read_project_file,
        'write_project_file': write_project_file,
        'list_online_models': list_online_models,
    }
    # Per-model handles so phonos.<model>.run(input, **kwargs) works inline too.
    for _model_id, _primary in _PRIMARY_INPUT.items():
        _inline_fns[_model_id] = _ModelHandle(_model_id, _primary)
    _phonos = type('phonos', (), _inline_fns)()
    # Register in sys.modules so 'import phonos' works
    import sys
    sys.modules['phonos'] = _phonos
    print("Phonos SDK (inline) loaded successfully")

# Make phonos available in notebook user namespace
try:
    from IPython import get_ipython
    ipython = get_ipython()
    if ipython is not None:
        ipython.user_ns['phonos'] = _phonos
    else:
        globals()['phonos'] = _phonos
except ImportError:
    globals()['phonos'] = _phonos

# Helper functions
def save_figure(fig, filename, dpi=150):
    """Save matplotlib figure to workspace"""
    filepath = os.path.join('/workspace', filename)
    fig.savefig(filepath, dpi=dpi, bbox_inches='tight', facecolor='white')
    return filepath

def list_files(path='/workspace'):
    """List files in a directory"""
    return os.listdir(path)

def read_file(filename, path='/workspace'):
    """Read a file from workspace or project"""
    for base in [path, '/workspace', '/project']:
        filepath = os.path.join(base, filename)
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return f.read()
    raise FileNotFoundError(f"File not found: {filename}")

# Auto-configure IBM Quantum account from environment variables.
# The channel name differs across qiskit-ibm-runtime versions
# ('ibm_cloud' vs 'ibm_quantum_platform') — try both.
_api_key = os.environ.get('IBM_QUANTUM_API_KEY')
if _api_key:
    try:
        from qiskit_ibm_runtime import QiskitRuntimeService
        _instance = os.environ.get('IBM_QUANTUM_INSTANCE')
        _saved = False
        _last_err = None
        for _channel in ('ibm_quantum_platform', 'ibm_cloud'):
            try:
                QiskitRuntimeService.save_account(
                    token=_api_key,
                    instance=_instance,
                    channel=_channel,
                    overwrite=True,
                    set_as_default=True
                )
                _saved = True
                break
            except Exception as _ch_err:
                _last_err = _ch_err
                if 'channel' not in str(_ch_err).lower():
                    raise
        if not _saved and _last_err:
            raise _last_err
        _backend = os.environ.get('IBM_QUANTUM_DEFAULT_BACKEND')
        print(f"IBM Quantum{': ' + _backend if _backend else ''} account configured")
    except Exception as _e:
        print(f"IBM Quantum account setup skipped: {_e}")

# Print welcome message
print("=" * 60)
print("Phonos Notebook Environment")
print("=" * 60)
print(f"Python: {sys.version.split()[0]}")
print(f"NumPy: {np.__version__}")
print(f"Pandas: {pd.__version__}")
print(f"Matplotlib: {matplotlib.__version__}")
print("-" * 60)
print("Workspace: /workspace")
print("Project files: /project (read-only)")
print("SDK: import phonos  # AI Models access")
print("=" * 60)
