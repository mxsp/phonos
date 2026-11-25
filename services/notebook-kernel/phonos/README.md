# Phonos SDK for Notebooks

Python library for accessing AI models, submitting jobs, and downloading outputs
from inside the Phonos Notebook IDE.

## Installation

The SDK is pre-installed in the notebook kernel environment (`/opt/phonos_sdk/phonos`).

## Quick Start

```python
import phonos

# Browse models
phonos.list_models()             # all catalog models
phonos.list_online_models()      # only models currently online
phonos.get_model('boltz2')       # full details (parameters, credit cost)

# Submit a job
job = phonos.boltz2.run(sequence='MKTIIALSYIFCLVAD')
# legacy form still works:
job = phonos.boltz2(sequence='MKTIIALSYIFCLVAD')

# Wait + download
job.wait(progress=True)
phonos.download_output(job, 'structure.cif')
phonos.list_outputs(job)         # all artifacts (for DiffDock multi-pose etc.)

# User / project context
phonos.get_credits()             # current credit balance
phonos.list_projects()           # projects you have access to
phonos.list_jobs(limit=20)       # recent jobs
```

## Model aliases

Every model is exposed under both a snake_case and a hyphenated attribute so
that the convenience form works regardless of style:

| canonical id           | phonos.<name>.run(...)                            |
|------------------------|---------------------------------------------------|
| `esmfold`              | `phonos.esmfold`                                  |
| `esmfold-re`           | `phonos.esmfold_re`, `phonos['esmfold-re']`       |
| `alphafold2-re`        | `phonos.alphafold2_re`                            |
| `alphafold2-re-multimer` | `phonos.alphafold2_re_multimer`                 |
| `proteinmpnn-re`       | `phonos.proteinmpnn_re`                           |
| `diffdock-re`          | `phonos.diffdock_re`                              |
| `boltz`, `boltz1`, `boltz2` | `phonos.boltz`, `phonos.boltz1`, `phonos.boltz2` |
| `evo`, `evo2`          | `phonos.evo`, `phonos.evo2`                       |
| `molmim`, `genmol`     | `phonos.molmim`, `phonos.genmol`                  |
| `openfold3`            | `phonos.openfold3`                                |
| `msa-search`           | `phonos.msa_search`, `phonos['msa-search']`       |

## API Reference

### Model discovery
- `list_models(refresh=False)` → `List[ModelInfo]`
- `list_online_models(refresh=False)` → `List[ModelInfo]`
- `get_model(name_or_id, refresh=False)` → `ModelInfo`
- `models()` → `Dict[str, ModelHandle]` (every registered handle)

### User / project
- `get_user_info()` → dict with `credits`, `plan`, `project_count`, …
- `get_credits()` → int or `None`
- `list_projects()` → `List[ProjectInfo]`
- `get_project_info()` → current kernel's project context

### Job submission
- `phonos.<model>.run(input, **kwargs)` → `Job`
- `submit_job(model, **kwargs)` → `Job`
- `batch_submit(model, items, batch_mode=True)` → `List[Job]`

### Job tracking
- `get_job(job_id)` → `Job`
- `get_job_info(job_or_id)` → `Job`
- `list_jobs(limit=50, status=None)` → `List[Job]`
- `cancel_job(job_id)` → `bool`
- `get_job_logs(job_id, limit=200)` → `List[dict]`

### Outputs
- `list_outputs(job_or_id)` → `List[Artifact]` (filename, size, format, url)
- `download_output(job_or_id, output_path=None)` → local path
- `download_artifact(job_or_id, artifact_name, output_path=None)` → local path

### Workspace files
- `list_project_files(path='/workspace')`
- `read_project_file(filename, path='/workspace')`
- `write_project_file(filename, content, path='/workspace')`

### `ModelHandle` extras
- `phonos.<model>.info(refresh=False)` → `ModelInfo`
- `phonos.<model>.help()` → multi-line help string with parameters
- `phonos.<model>(**kwargs)` → callable form (legacy)

### `Job` extras
- `job.wait(timeout=None, poll_interval=1.0, progress=False)` → bool
- `job.refresh()` → self
- `job.cancel()`, `job.logs()`, `job.list_artifacts(refresh=True)`
- `job.download(output_path=None, artifact=None)` → local path

## Error handling

The SDK raises `phonos.PhonosError` (a subclass of `RuntimeError`) for any
server-side failure, including authentication, validation, 4xx, 5xx, and
network errors. The server's `message` is included in the exception text.
