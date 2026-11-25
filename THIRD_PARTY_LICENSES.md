# Third-Party Licenses & Model Attributions

Phonos builds on open-source scientific libraries, web frameworks, and external bio-AI model integrations. Full license texts for Python and JavaScript dependencies ship inside their respective package distributions.

---

## 1. Python Runtime Dependencies
- **Flask, Werkzeug, Jinja2, Click, ItsDangerous** — BSD-3-Clause
- **Flask-SocketIO, python-socketio, python-engineio, Flask-Compress, Flask-Mail** — MIT
- **Eventlet, Gunicorn** — MIT
- **PostgreSQL Driver (`psycopg2-binary`), `psycogreen`** — LGPL-3.0 (with OpenSSL/linking exception) / BSD
- **Redis (`redis-py`), PyJWT, Bleach, Requests** — MIT / Apache-2.0
- **Cryptography, bcrypt, Authlib** — Apache-2.0 / BSD-3-Clause
- **PyCryptodome** — BSD-2-Clause / Public Domain
- **APScheduler, Pillow, python-dotenv** — MIT / HPND-PIL / BSD-3-Clause
- **Biopython** — Biopython License Agreement / BSD-3-Clause
- **RDKit** — BSD-3-Clause
- **NumPy, Pandas, SciPy** — BSD-3-Clause
- **Qiskit, Qiskit Aer, Qiskit IBM Runtime** — Apache-2.0
- **PyZMQ** — BSD-3-Clause / LGPL-3.0
- **Stripe Python SDK** *(optional billing integration)* — MIT

## 2. Optional Extras (Not Required or Bundled by Default)
- **OpenBabel (`openbabel-wheel`)** — **GPL-2.0-only**. *Not installed by default in Phonos builds, containers, or binaries.* RDKit (BSD-3-Clause) handles standard chemical informatics. Operators who manually install OpenBabel in their own environment must comply with GPL-2.0 if redistributing combined binaries.
- **MaxMind GeoIP2 (`geoip2` + `GeoLite2-Country.mmdb`)** — Code is Apache-2.0; `.mmdb` databases are **not included** in this repository and require the operator's own MaxMind account/license if geoblocking is enabled.
- **Wolfram Engine (`services/wolfram-service`)** — Optional separate microservice pulling `wolframresearch/wolframengine`; governed by the proprietary **Wolfram Engine Software License Agreement** (personal/non-production developer terms unless licensed from Wolfram Research).
- **PyWebView** *(optional native desktop window wrapper)* — BSD-3-Clause.

## 3. Frontend & Visualization Libraries
- **Mol\* (`pdbe-molstar`)** — MIT License (PDBe / RCSB / CEITEC Mol\* contributors)
- **BlueprintJS (`@blueprintjs/core`, `@blueprintjs/icons`)** — Apache-2.0
- **CodeMirror 5** — MIT License
- **xterm.js** — MIT License
- **Chart.js** — MIT License
- **Socket.IO Client, Marked** — MIT License
- **DOMPurify** — Apache-2.0 / MPL-2.0
- **MediaPipe Tasks / Hands** *(experimental gesture viewer)* — Apache-2.0
- **Font Awesome Free** — CC BY 4.0 (Icons) / SIL OFL 1.1 (Fonts) / MIT (CSS)
- **Inter & IBM Plex Mono Fonts** — SIL Open Font License 1.1

---

## 4. Scientific Bio-AI Models (External API / Self-Hosted Worker Integrations)

Phonos does **not** redistribute third-party AI model weights in this repository. When invoking models via NVIDIA NIM or self-hosted endpoints, the respective upstream model licenses and terms apply:

| Model | Upstream Authors | Upstream Code / Weight License | Notes |
|---|---|---|---|
| **Boltz-2** | MIT CSAIL (Passaro, Wohlwend et al.) | MIT License | Biomolecular complex & affinity prediction |
| **OpenFold3** | OpenFold Consortium / AlQuraishi Lab | Apache-2.0 | Biomolecular structure prediction |
| **DiffDock** | MIT CSAIL (Corso et al.) | MIT License | Diffusion-based molecular blind docking |
| **RFdiffusion** | Institute for Protein Design / RosettaCommons | BSD-3-Clause | *De novo* protein backbone generation |
| **ProteinMPNN** | Institute for Protein Design (Dauparas et al.) | MIT License | Structure-conditioned sequence design |
| **Evo 2** | Arc Institute / NVIDIA / Stanford | Apache-2.0 | Genomic DNA/RNA foundation model |
| **GenMol** | NVIDIA Corporation | NVIDIA Open Model License | Fragment-based molecule generation |
| **MolMIM** | NVIDIA Corporation | **NVIDIA AI Foundation Models Community License** | **Non-Commercial R&D Only** (unless covered by NVIDIA Enterprise license) |
| **ESMFold** *(self-hosted)* | Meta FAIR (Lin et al.) | Apache-2.0 (code) / CC BY 4.0 (weights) | Retired on hosted NIM; optional local worker |
| **AlphaFold2** *(self-hosted)* | Google DeepMind (Jumper et al.) | Apache-2.0 (code) / CC BY 4.0 (AF2 params) | Retired on hosted NIM; note AF3 weights carry separate non-commercial terms |
