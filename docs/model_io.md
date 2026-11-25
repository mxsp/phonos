# Model I/O chaining — outputs as inputs

Every model on Phonos emits files, and almost every one of those files is a
valid input for some *other* model:

| Produced by | File | Feeds |
|---|---|---|
| ProteinMPNN-RE, RFDiffusion | `.fasta` (designed sequences) | ESMFold, AlphaFold2, Boltz2, OpenFold3 |
| ESMFold, AlphaFold2, Boltz2, OpenFold3 | `.pdb` / `.cif` | ProteinMPNN-RE, RFDiffusion, DiffDock |
| DiffDock-RE | `.sdf` (docked pose) | DiffDock, MolMIM, GenMol |
| MolMIM, GenMol | `.smi` / `.json` | DiffDock (ligand), Boltz2 (ligand) |
| Evo2 | `.json` / `.fasta` (nucleotides) | Evo2 |
| MSA-Search | `.a3m` | folding models (query sequence) |

Rather than hard-coding that table in the UI, one module decides it:
`phonos_app/helpers/model_io.py`. The UI and the REST API both go through it,
so a chain that works in the browser works identically from a script.

## Concepts

A file is described by the **payload kinds** it contains:

| Kind | Meaning | Delivered as |
|---|---|---|
| `protein_sequence` | amino-acid sequence | pasted into the sequence field |
| `nucleotide_sequence` | DNA/RNA sequence | pasted into the sequence field |
| `structure` | PDB / mmCIF coordinates | picked from project files |
| `ligand` | SDF / MOL / MOL2 | picked from project files |
| `smiles` | SMILES string | pasted into the ligand field |
| `msa` | alignment (A3M / Stockholm) | picked from project files |

A file can carry several kinds at once: a predicted `.pdb` is both a
`structure` (dock it) and a `protein_sequence` (re-fold a chain with a
different model), so both sets of targets are offered.

Classification uses the extension *and* the content. A `.txt` holding FASTA is
treated as sequences; a `.fasta` of nucleotides is offered to Evo2 and not to
ESMFold. Where the two disagree, content wins.

## API

All three endpoints require an authenticated session and return the same
`payload` / `targets` shape.

### `GET /api/model-io/matrix`

The full compatibility table — kinds, models, and which extensions map to what.
Use this to build pipelines programmatically without duplicating the table.

```json
{
  "status": "success",
  "kinds":  [{"id": "protein_sequence", "label": "Protein sequence", "delivery": "sequence"}, ...],
  "models": [{"id": "esmfold-re", "accepts": ["protein_sequence"], "output_extension": ".pdb", "available": true}, ...],
  "extensions": {"structure": ["cif", "ent", "mmcif", "pdb", "pdb1"], ...}
}
```

### `GET /api/jobs/<job_id>/artifacts/<artifact_name>/as-input`

Classify one job output and list the models that can consume it.

```json
{
  "status": "success",
  "source":  {"type": "job_artifact", "job_id": "...", "project_id": 42,
              "produced_by": "proteinmpnn-re", "download_url": "/download_job_artifact/..."},
  "payload": {"format": "fasta", "kinds": ["protein_sequence"], "needs_file": false,
              "sequences": [{"name": "design_1", "sequence": "MKT...", "kind": "protein_sequence", "length": 128}],
              "smiles": [], "ligand_codes": []},
  "targets": [{"id": "esmfold-re", "short_name": "ESMFold-RE", "task": "protein_folding",
               "matched_kinds": ["protein_sequence"], "delivery": "sequence", "available": true}],
  "unavailable_targets": []
}
```

### `GET /api/files/<file_id>/as-input`

The same, for a project file — an upload or a registered job output. Job
outputs and uploads are interchangeable here on purpose: a caller does not need
to know where a file came from.

### `POST /api/model-io/classify`

Classify a snippet without storing it first. Useful while building an
integration:

```bash
curl -sS -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"filename": "designs.fasta", "content": ">d1\nMKTAYIAKQRQ\n"}' \
  https://phonos.fyreflysystems.de/api/model-io/classify
```

## Adding a model

One entry in `MODEL_INPUT_KINDS` (`phonos_app/helpers/model_io.py`):

```python
MODEL_INPUT_KINDS = {
    ...
    'my-new-model': (KIND_PROTEIN, KIND_SMILES),
}
```

That is the whole change. The picker, the target lists and the API matrix all
read from this table, so no UI code needs touching. `tests/test_model_io.py`
asserts that every declared kind is a real kind with a delivery channel, so a
typo fails CI rather than silently hiding the model.

## Adding a format

Add the extension to the relevant set (`FASTA_EXTENSIONS`,
`STRUCTURE_EXTENSIONS`, `LIGAND_EXTENSIONS`, `SMILES_EXTENSIONS`,
`MSA_EXTENSIONS`) and, if it needs parsing, a branch in `classify_payload`.
Keep the parser pure — the module has no Flask, database or filesystem
dependency, which is what makes it cheap to test.

## Frontend

`static/js/model_io.js` exposes `window.PhonosModelIO`:

```js
PhonosModelIO.openForArtifact(jobId, artifactName);  // job output
PhonosModelIO.openForFile(fileId);                   // project file
```

Markup can also opt in declaratively — preferred, because a filename must never
be interpolated into a JS string literal inside an HTML attribute:

```html
<button class="mio-trigger" data-mio-job="{{ job.job_id }}" data-mio-artifact="{{ filename }}">…</button>
<button class="mio-trigger" data-mio-file="{{ file.id }}">…</button>
```

The handoff to the job wizard goes through `sessionStorage`
(`phonos:modelIoPrefill`) because it can start on the standalone output page and
finish in the SPA. `App.consumeModelIoPrefill()` drains it when the target
project renders; entries older than 10 minutes are discarded as stale.
