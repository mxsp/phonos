<!--
  PHONOS — AD.md
  Consumer-facing product page for GitHub / company website
-->

<div align="center">

```
██████╗ ██╗  ██╗ ██████╗ ███╗   ██╗ ██████╗ ███████╗
██╔══██╗██║  ██║██╔═══██╗████╗  ██║██╔═══██╗██╔════╝
██████╔╝███████║██║   ██║██╔██╗ ██║██║   ██║███████╗
██╔═══╝ ██╔══██║██║   ██║██║╚██╗██║██║   ██║╚════██║
██║     ██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝███████║
╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝
```

### The AI-Native Platform for Computational Biology

**Run state-of-the-art structure prediction, design DNA sequences, visualize molecules in 3D, and collaborate with your team — all in one place.**

---

![Platform](https://img.shields.io/badge/Platform-Web%20%7C%20API-10b981?style=for-the-badge)
![Models](https://img.shields.io/badge/AI%20Models-Boltz2%20%7C%20Evo2%20%7C%20Custom-10b981?style=for-the-badge)
![Security](https://img.shields.io/badge/Security-AES--256--GCM-10b981?style=for-the-badge)
![Version](https://img.shields.io/badge/Version-3.2.0-10b981?style=for-the-badge)

</div>

---

## What is Phonos?

Phonos is a **cloud research platform** built for scientists, engineers, and drug discovery teams who need GPU-scale computation without the infrastructure headache.

Submit a protein sequence. Get back a 3D structure. Visualize it in your browser. Share it with your team. All without touching a terminal.

```
  Your Research                    Phonos                         Results
  ──────────                       ──────                         ───────

  ┌─────────────┐    REST API    ┌──────────────────┐    3D PDB  ┌──────────────┐
  │  Protein    │ ─────────────► │  AI Job Queue    │ ─────────► │  Structure   │
  │  Sequence   │                │  (GPU Cluster)   │            │  Viewer      │
  └─────────────┘                └──────────────────┘            └──────────────┘
  ┌─────────────┐   Drag & Drop  ┌──────────────────┐   Metrics  ┌──────────────┐
  │  DNA / RNA  │ ─────────────► │  Batch Processor │ ─────────► │  Confidence  │
  │  Sequences  │                │  (Parallel Jobs) │            │  Dashboard   │
  └─────────────┘                └──────────────────┘            └──────────────┘
  ┌─────────────┐   Bridge API   ┌──────────────────┐   JSON/PDB ┌──────────────┐
  │  Your Own   │ ─────────────► │  Custom Model    │ ─────────► │  Any Output  │
  │  Model      │                │  Bridge          │            │  Format      │
  └─────────────┘                └──────────────────┘            └──────────────┘
```

---

## Core Capabilities

### 1. Structure Prediction — Powered by Boltz2

Run cutting-edge protein, DNA, and RNA structure prediction backed by **NVIDIA GPU infrastructure**, directly from your browser.

```
  Input Tab         Parameters          3D Output
  ─────────         ──────────          ─────────
  [Protein ]        Recycling:  ●───4   ╔═══════════════╗
  [DNA     ]        Sampling:   ●──200  ║               ║
  [RNA     ]        Samples:    ●───1   ║   ~ ~ ~ ~ ~   ║
                    Potentials: [ON ]   ║  ~ Helix α ~  ║
  Sequence:                            ║   ~ ~ ~ ~ ~   ║
  MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWERVMGDGERQFSTLKSTVEAIWAGIKATEAAVSEEFGLAPFLPDQIHFVHSQELLSRYPDLDAKGRERAIAKDLGAVFLVGIGGKLSDGHRHDVRAPDYDDWSTPSELGHAGLNGDILVWNPVLEDAFELSSMGIRVDADTLKHQLALTGDEDRLELEWHQALLRGEMPQTIGGGIGQSRLTMLLLQLPHIGQVQAGVWPAAVRESVPSLL

  [Predict Structure] ──────────────────────────────────► Confidence: 94.2
                                                          pTM: 0.91 | iPTM: 0.88
```

**Supported molecule types:**
- Protein sequences (all 20 standard amino acids)
- DNA sequences (A, C, G, T)
- RNA sequences (A, C, G, U)
- Ligand binding (ATP, GTP, NAD, HEME, custom SMILES)

---

### 2. DNA Sequence Design — Evo2

Generate novel DNA sequences for research and synthetic biology applications using **Evo2**, accessed through NVIDIA's API.

```
  Prompt / Seed Sequence
  ┌─────────────────────────────────────────────────────┐
  │  ATGCGTAACTTGGCCATCGATCGTAGCTAGCGATCGTAGCTAGCG...  │
  └─────────────────────────────────────────────────────┘
              │
              ▼  Evo2 (NVIDIA API)
  ┌─────────────────────────────────────────────────────┐
  │  Generated Output                                   │
  │  ATGCGTAACTTGGCCATCGATCGTAGCTAGCGATCGTAGCTAGCG...  │
  │  .....[continuation]......TGAAGTCAGCTGATCGATCG     │
  └─────────────────────────────────────────────────────┘
```

---

### 3. Custom Model Bridge — Bring Your Own Compute

Connect any computational model — running on your own GPUs, HPC cluster, or cloud — to the Phonos platform via the **Bridge API**.

```
  Your Infrastructure                 Phonos Platform
  ─────────────────                   ───────────────

  ┌─────────────────────┐             ┌─────────────────────┐
  │  Your GPU Server    │             │  Bridge API          │
  │  ┌───────────────┐  │  poll jobs  │  ┌───────────────┐  │
  │  │  Worker       │ ◄├─────────────┤─ │  Job Queue    │  │
  │  │  (Python/Any) │  │             │  └───────────────┘  │
  │  │               │  │  submit     │  ┌───────────────┐  │
  │  │  your_model() │ ──┼────────────►  │  Results DB   │  │
  │  └───────────────┘  │  results    │  └───────────────┘  │
  └─────────────────────┘             └─────────────────────┘
         AES-256-GCM encrypted payloads ─────────────────────►
         HMAC-SHA256 request signing ────────────────────────►
```

**Output formats your model can return:**

| Format | Use Case | Auto-Viewer |
|--------|----------|-------------|
| `pdb` / `cif` | Protein & crystal structures | 3D molecular viewer |
| `sdf` / `mol` | Small molecules & ligands | 3D molecular viewer |
| `json` | Predictions, classifications | Interactive table |
| `png` / `jpg` | Plots, heatmaps, images | Image viewer |
| `csv` / `txt` | Tabular data, sequences | Text viewer |
| `zip` | Multi-file results | Download bundle |

---

### 4. Interactive 3D Molecular Viewer

Every job result opens directly in a **WebGL-powered 3D viewer** — no software to install.

```
  ╔══════════════════════════════════════════════════════════╗
  ║  3D Structure Viewer                        [Screenshot]  ║
  ╠══════════════════════════════════════════════════════════╣
  ║                                                          ║
  ║          ╭──╮      ╭╮                                    ║
  ║        ╭─╯  ╰──╮  ╭╯╰──╮                               ║
  ║       ─╯        ╰──╯    ╰─╮                             ║
  ║      ╭──╮  ╭───────╮  ╭──╯                              ║
  ║      ╰──╰──╯       ╰──╯  Ligand: ◉ ATP                 ║
  ║                                                          ║
  ╠══════════════════════════════════════════════════════════╣
  ║  Style: [Cartoon] [Stick] [Sphere]   Color: [Spectrum]  ║
  ║  Quality: [Low] [Med] [High]         Surface: [VDW]     ║
  ║  Atoms: 3,204   Residues: 412   Chains: 2               ║
  ╚══════════════════════════════════════════════════════════╝
```

**Viewer features:**
- Rotate, zoom, and pan with mouse or touch
- Cartoon, stick, sphere, and surface rendering modes
- Automatic ligand detection and Ball & Stick display
- Spectral and chain-based coloring
- Atom selection and hover tooltips
- One-click screenshot export

---

### 5. Collaborative Notebook IDE

Write Python directly in the browser with a **Jupyter-compatible Notebook IDE** built for bioinformatics workflows.

```
  ┌─────────────────────────────────────────────────────┐
  │  notebook.ipynb                         [Run All]   │
  ├─────────────────────────────────────────────────────┤
  │  [▶] In [1]:                                        │
  │      import phonos                                   │
  │      seq = "MKTAYIAKQRQ..."                         │
  │      job = phonos.submit("boltz2", sequence=seq)    │
  │                                                     │
  │  Out[1]: Job ID: job_4f8a2c1d — Queued              │
  ├─────────────────────────────────────────────────────┤
  │  [▶] In [2]:                                        │
  │      result = job.wait()                            │
  │      phonos.view(result.pdb)                        │
  │                                                     │
  │  Out[2]: [Opens 3D Viewer inline]                   │
  └─────────────────────────────────────────────────────┘
```

- Smart autocomplete for Phonos library functions and common bioinformatics patterns
- Real-time collaboration: see teammates' cursors live
- Auto-save on every edit and before tab close
- Full keyboard shortcut suite (`Ctrl+Enter` to run, `Shift+Enter` to run & advance)

---

### 6. Batch Job Processing

Submit hundreds of sequences in one go with **parallel batch execution**.

```
  Upload CSV / FASTA                 Batch Progress
  ─────────────────                  ──────────────

  sequences.fasta                    Submitted: 120 jobs
  ┌──────────────────┐               ██████████████░░░░░ 72%
  │ >seq_001         │
  │ MKTAYIAKQRQ...   │               ✓ Completed:  86
  │ >seq_002         │               ⟳ Running:    12
  │ AGIVKEGDLSQ...   │               ○ Queued:     22
  │ >seq_003         │
  │ PQITLWKRPIV...   │               [Download All Results]
  │  ... 117 more    │               [Export Summary CSV]
  └──────────────────┘
```

- Auto-batches large queues in groups of 3 for optimal throughput
- Live status dashboard per sequence
- Bulk result download as ZIP
- CSV summary export with confidence scores

---

### 7. Project & Team Workspace

Organize your research into **Projects** and collaborate with your team in real time.

```
  My Lab
  ├── Projects
  │   ├── 🔬 Drug Discovery — Kinase Inhibitors
  │   │   ├── Files (47)
  │   │   ├── Jobs (213) — 12 running
  │   │   └── Team: @alice @bob @charlie
  │   ├── 🧬 mRNA Vaccine Target Screening
  │   │   ├── Files (8)
  │   │   └── Jobs (31)
  │   └── 🔩 Custom Docking Model (Bridge API)
  │       └── Jobs (5)
  └── Data Lake
      ├── /sequences/   (1.2 GB)
      ├── /structures/  (4.7 GB)
      └── /outputs/     (8.1 GB)
```

**Collaboration features:**
- Role-based access: Owner, Manager, Contributor, Viewer
- Project-level file storage with folder organization
- Shared job history and results
- @mention teammates in project messages
- Real-time activity feed

---

### 8. Data Lake & File Manager

A **built-in file system** for all your research data — sequences, structures, datasets, outputs.

```
  File Browser
  ┌─────────────────────────────────────────────────────┐
  │  /projects/kinase-inhibitors/structures/            │
  ├──────────────────────────────┬──────────┬───────────┤
  │  Name                        │  Size    │  Type     │
  ├──────────────────────────────┼──────────┼───────────┤
  │  ◉ abl1_boltz2.pdb           │  248 KB  │  Structure│
  │  ◉ egfr_predicted.cif        │  1.1 MB  │  Structure│
  │  ◉ atp_ligand.sdf            │  4.2 KB  │  Molecule │
  │  ◉ batch_results_001.zip     │  18.3 MB │  Archive  │
  │  📁 docking_runs/            │  2.4 GB  │  Folder   │
  └──────────────────────────────┴──────────┴───────────┘
  [Upload] [New Folder] [Download Selected] [Run Job on Selected]
```

- Drag-and-drop upload
- Direct "Run Job" on any file
- Tag files for quick filtering
- Storage usage tracking per project and per team

---

### 9. Enterprise-Grade Security

Every layer of Phonos is built with security at its core.

```
  Data Flow Security
  ──────────────────

  Browser ──[ HTTPS/TLS ]──► API Server ──[ AES-256-GCM ]──► Bridge Workers
     │                           │                                   │
     │                     CSRF tokens                        HMAC-SHA256
     │                     Session auth                       Request signing
     │                     Login required                     5-min replay window
     │
     └── Encryption keys stored in browser IndexedDB (never transmitted)
```

| Protection Layer | Technology |
|-----------------|------------|
| Transport | HTTPS / TLS everywhere |
| Payload encryption | AES-256-GCM (authenticated) |
| Key derivation | PBKDF2 — 100,000 iterations |
| Request integrity | HMAC-SHA256 signing |
| Replay prevention | 5-minute timestamp window |
| API keys at rest | AES-256-GCM encrypted in database |
| Session security | CSRF protection on all mutations |

---

### 10. Analytics & Usage Dashboard

Track compute spending, job success rates, and team activity across all projects.

```
  Analytics — Last 30 Days
  ┌────────────────────────────────────────────────────┐
  │  Jobs Submitted   │  Success Rate  │  Compute Used │
  │      1,247        │     96.4%      │   4,821 min   │
  ├────────────────────────────────────────────────────┤
  │  Credits Used     │  Storage       │  Team Members │
  │     2,180 / 5,000 │   14.3 GB      │      8        │
  ├────────────────────────────────────────────────────┤
  │  Jobs by Model                                     │
  │  Boltz2  ████████████████████ 68%                  │
  │  Evo2    ████████ 21%                              │
  │  Custom  ████ 11%                                  │
  └────────────────────────────────────────────────────┘
```

---

## Workflow: From Sequence to Structure in 3 Steps

```
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │   STEP 1           STEP 2              STEP 3               │
  │                                                              │
  │  [Paste Seq]  ──►  [Submit Job]  ──►  [View 3D + Download] │
  │                                                              │
  │   ~30 seconds      Queued → GPU       pLDDT confidence      │
  │   to configure     processing         score displayed        │
  │                    1-5 minutes        PDB/CIF download       │
  │                                       Share with team        │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

---

## Who Uses Phonos?

```
  ┌────────────────────────┐   ┌────────────────────────┐   ┌────────────────────────┐
  │   Drug Discovery       │   │   Academic Research     │   │   Biotech Startups     │
  │   Teams                │   │   Labs                  │   │                        │
  │                        │   │                         │   │                        │
  │  • Screen thousands    │   │  • Publish-ready 3D     │   │  • Deploy proprietary  │
  │    of targets fast     │   │    structure figures    │   │    models via Bridge   │
  │  • Ligand binding      │   │  • Collaborative        │   │    API                 │
  │    prediction          │   │    notebooks            │   │  • Pay-per-use, no     │
  │  • Team collaboration  │   │  • Free tier available  │   │    infra overhead      │
  │    across sites        │   │                         │   │                        │
  └────────────────────────┘   └────────────────────────┘   └────────────────────────┘
```

---

## Platform Architecture at a Glance

```
  ╔═══════════════════════════════════════════════════════════════╗
  ║                       PHONOS PLATFORM                        ║
  ╠══════════════╦═══════════════════╦══════════════════════════ ╣
  ║   Frontend   ║    Backend        ║    Compute Layer          ║
  ║              ║                   ║                           ║
  ║  React SPA   ║  Flask (Python)   ║  NVIDIA NVCF (Boltz2)    ║
  ║  WebGL 3D    ║  PostgreSQL DB    ║  NVIDIA API  (Evo2)      ║
  ║  CodeMirror  ║  Redis Queue      ║  Custom Bridge Workers   ║
  ║  Socket.IO   ║  REST + WS API    ║  Your GPU / HPC          ║
  ╚══════════════╩═══════════════════╩══════════════════════════ ╝
```

---

## Bridge API — Quick Integration

Connect your model to Phonos in under 50 lines of Python:

```python
import requests, time, os

API_KEY = os.getenv("PHONOS_API_KEY")   # pk_...
BASE    = os.getenv("PHONOS_URL")        # https://your-phonos-instance.com

def run():
    headers = {"Authorization": f"Bearer {API_KEY}"}

    # 1. Poll for pending jobs
    jobs = requests.post(f"{BASE}/api/bridge/v1/jobs/poll",
                         headers=headers, json={"max_jobs": 1}).json().get("jobs", [])

    for job in jobs:
        try:
            # 2. Run your model
            output = your_model(job["inputs"])

            # 3. Return results — shown automatically in Phonos UI
            requests.post(f"{BASE}/api/bridge/v1/jobs/{job['job_id']}/result",
                          headers=headers,
                          json={"status": "completed", "result": output, "output_format": "pdb"})
        except Exception as e:
            requests.post(f"{BASE}/api/bridge/v1/jobs/{job['job_id']}/result",
                          headers=headers,
                          json={"status": "failed", "error_message": str(e)})

while True:
    run()
    time.sleep(5)
```

---

## Plans

| Feature | Free | Research | Enterprise |
|---------|------|----------|------------|
| Structure predictions | 10 / month | Unlimited | Unlimited |
| Batch jobs | — | Up to 500 | Unlimited |
| Custom Bridge models | — | 3 models | Unlimited |
| 3D Viewer | Included | Included | Included |
| Notebook IDE | Included | Included | Included |
| Team collaboration | 1 user | Up to 10 | Unlimited |
| Storage | 1 GB | 50 GB | Custom |
| API access | — | Included | Included |
| End-to-end encryption | — | Included | Included |
| Priority GPU queue | — | — | Included |
| SLA / Support | Community | Email | Dedicated |

---

## Getting Started

```bash
# Option 1 — Cloud (no setup)
# Sign up at your Phonos instance and start submitting jobs immediately.

# Option 2 — Self-hosted
git clone https://github.com/your-org/phonos.git
cd phonos
cp .env.example .env          # Add your NVIDIA API keys, DB credentials
docker-compose up -d          # Starts web + worker + database
# Open http://localhost:5000
```

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Quick Start](production/QUICK_START.md) | Get up and running in 5 minutes |
| [Bridge API Reference](production/USER_MODELS_API_REFERENCE.md) | Full API spec for custom models |
| [Bridge API Quick Reference](production/USER_MODELS_QUICK_REFERENCE.md) | One-page cheat sheet |
| [Custom Model README](production/USER_MODELS_README.md) | Worker implementation guide |
| [Deploy Guide](deploy/DEPLOY.md) | Self-hosted deployment instructions |

---

<div align="center">

---

**Phonos** — Computational biology at the speed of thought.

*Built with Flask · PostgreSQL · Redis · NVIDIA GPU · WebGL · Socket.IO*

</div>
