# Phonos — Self-Hosted Molecular Discovery, 3D Structure & Bio-AI Workbench

**Phonos** is a local-first computational biology, protein structure prediction, molecular docking, Python notebook, and AI Copilot platform designed for research laboratories and open-source bio-AI engineering.

- **Zero-SMTP Local Account Manager (`username` + `password`)** — Instant browser First-Run Setup Wizard (`/setup`), Admin Account Manager (`/admin/accounts`), or CLI (`phonos accounts`), combining **4 Global Lab Roles** (`Admin`, `Scientist`, `Operator`, `Viewer`) with per-project RBAC (`Project Manager`, `Project Contributor`, `Project Viewer`).
- **Always ships with both isolated modes (modeled on [Gimel](https://github.com/mxsp/gimel)):**
  - **Playground (`:5040`)** — Isolated SQLite sandbox pre-seeded with a folded 3D mini-protein structure (`pLDDT` confidence B-factors) and starter RDKit/3D Python notebook (`--reset` anytime).
  - **Production (`:5041`)** — Clean laboratory database (SQLite out-of-the-box or PostgreSQL 16 + Redis 7), encrypted API key vault, and localnet guard (`PHONOS_LOCALNET_ONLY=1`).
- **Bring Your Own Model (BYOM) & Custom Endpoints** — Connect custom models over `direct_http`, `nvidia_nim` (self-hosted or cloud), `openai_compat`, `huggingface`, or `webhook_bridge` (`/custom-models`), plus encrypted personal/instance API keys (`Settings → Model Providers`).
- **Zero-Docker Stateful Python Notebook & Mol\* 3D Viewer** — Built-in local Python kernel (`show_smiles`, `show_structure`, Matplotlib, RDKit, BioPython) with optional Docker sandbox isolation, plus interactive 3D Mol\* confidence coloring, Ångström distance ruler (`M`), coordinate export (`D`), and 1-click Copilot structure inspection (`A`).
- **Discovery Copilot (`/agent`)** — Supports local keyless LLMs (`Ollama`, `LM Studio`, `vLLM`) and cloud routers (`Anthropic`, `OpenRouter`, `Gemini`, `OpenAI`, `Groq`) with RDKit ADMET/Lipinski/QED profiling, 3D `pLDDT` confidence auditing, saturation mutagenesis scanning, and custom BYOM job execution.
- **Built-in GitHub Release Updater** — Check and apply SHA-256 verified updates from `mxsp/phonos` in **Admin → Account Manager → Software Updates** or via `phonos update`.

> **Research Use Only (RUO):** Phonos and all computational outputs are strictly for **non-clinical research and educational use** — never for clinical diagnosis, patient treatment, or unvalidated wet-lab synthesis. See [DISCLAIMER.md](./DISCLAIMER.md) and [THIRD_PARTY_LICENSES.md](./THIRD_PARTY_LICENSES.md) (note: upstream NVIDIA MolMIM endpoints are restricted to non-commercial R&D).  
> **License:** Source-available for **non-commercial academic, personal, and research use** under the **Phonos Non-Commercial Source License v1.0** — see [LICENSE](./LICENSE).

---

## One-Time Setup & Quick Start

### Option 1: Guided Browser GUI Installer (Easiest — Zero Dependencies)

Launch the local setup wizard (`http://127.0.0.1:8098`) using only Python 3.11+ standard library — checks prerequisites, creates `.venv`, installs RDKit & BioPython, configures your first Admin account, and launches Phonos:

```bash
git clone https://github.com/mxsp/phonos.git
cd phonos
python3 scripts/install-ui.py
```

*(On Windows, double-click `start-windows.bat` or run `python scripts\install-ui.py`.)*

### Option 2: CLI One-Time Setup Script (Linux / macOS / Windows)

**Linux / macOS:**
```bash
git clone https://github.com/mxsp/phonos.git
cd phonos
chmod +x scripts/setup.sh
./scripts/setup.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/mxsp/phonos.git
cd phonos
.\scripts\setup.ps1
```

Then start Phonos:
```bash
.venv/bin/phonos playground                      # Demo sandbox on http://127.0.0.1:5040
.venv/bin/phonos production                      # Clean lab instance on http://127.0.0.1:5041
.venv/bin/phonos both                            # Run Playground (:5040) + Production (:5041) side by side
```

### Option 3: Docker Compose (Playground + Production + PostgreSQL + Redis + MRS)

```bash
cp .env.example .env
# Edit .env to set POSTGRES_PASSWORD (SECRET_KEY & ENCRYPTION_KEY auto-persist if left blank)
docker compose up -d --build
```

- **Playground Sandbox:** `http://127.0.0.1:5040/login` (`docker logs phonos-playground` prints the generated demo login)
- **Production Lab:** `http://127.0.0.1:5041/setup` (PostgreSQL 16 + Redis 7 + ZeroMQ MRS broker)

### Option 4: Linux Standalone Binary (`phonos-linux-x64.tar.gz`)

Download `phonos-linux-x64.tar.gz` directly from [GitHub Releases](https://github.com/mxsp/phonos/releases) (or build locally via `./packaging/build-exe.sh`):

```bash
tar -xzf phonos-linux-x64.tar.gz
./phonos --mode playground     # Sandbox on http://127.0.0.1:5040
./phonos --mode production     # Production Lab on http://127.0.0.1:5041
```

*(Note: Official pre-built binaries currently target Linux x64; on Windows, use Option 1 `python scripts\install-ui.py` or Option 2 `.\scripts\setup.ps1`.)*

---

## Two-Layer Role Architecture (Lab Roles × Project Roles)

Phonos pairs Gimel-style global **Lab Roles** with granular per-project assignments:

| Global Lab Role | Create Top-Level Projects | Compute / Notebook Execution | Project Visibility |
| :--- | :---: | :---: | :--- |
| **Admin** (`admin`) | Yes | Full + Provider Vault + Updater | Clean personal sidebar by default + **All Lab Projects** (`?scope=all_lab`) override |
| **Scientist** (`scientist` / `staff`) | Yes | Full (built-in + BYOM + notebooks) | Own projects + Default Lab Team + assigned projects |
| **Operator** (`operator` / `worker`) | Assigned projects only | Runs jobs inside Contributor/Manager projects | Default Lab Team + assigned projects |
| **Viewer** (`viewer` / `guest`) | No | **Read-only ceiling** (0 compute/kernels) | Inspects 3D structures, notebooks, and results in shared projects |

### CLI Account Manager & GitHub Updater

```bash
# Manage local username + password accounts
phonos accounts list
phonos accounts add alice --role scientist --lab-team-role contributor
phonos accounts passwd alice
phonos accounts role alice admin
phonos accounts disable alice | enable alice | unlock alice | delete alice

# Check & apply SHA-256 verified updates from GitHub Releases (mxsp/phonos)
phonos update --check
phonos update --apply
```

---

## Bring Your Own Model (BYOM) & Custom Endpoints

Register any internal or self-hosted bio-AI endpoint in **`/custom-models`** using 5 execution protocols:

1. **`direct_http`** — Direct synchronous REST JSON call to your microservice (supports `.pdb`, `.cif`, `.sdf`, `.mol2`, `.fasta`, `.csv`, `.json` outputs).
2. **`nvidia_nim`** — Self-hosted NVIDIA BioNeMo NIM container or custom cloud NIM endpoint.
3. **`openai_compat`** — vLLM, Ollama, LM Studio, or OpenRouter `/v1/chat/completions` scientific model endpoints.
4. **`huggingface`** — HuggingFace Inference Endpoints / TGI.
5. **`webhook_bridge`** — Asynchronous queue + external worker pull (`X-Worker-Key`) for long-running GPU clusters.

Run the included zero-dependency reference server to test end-to-end custom 3D structure generation locally in 10 seconds:

```bash
python3 examples/custom_model_server.py --port 8095
```

Then register `http://127.0.0.1:8095/predict` (`direct_http`) in **`/custom-models`** and click **Test Live Endpoint** — or ask **Discovery Copilot** (`/agent`) to run your custom model directly!

---

## Repository Layout

```text
phonos_app/           Flask + Socket.IO web application, RBAC, Notebook kernel & Copilot
  agent/              Discovery Copilot (11 LLM presets incl. Ollama/vLLM, ADMET, pLDDT & BYOM tools)
  helpers/            Account manager, encrypted provider vault, BYOM HTTP runner, local notebook kernel, updater
  routes/             REST APIs (/api/accounts, /api/updates, /api/settings/providers, /api/user-models, ...)
  cli.py              Unified `phonos` CLI (playground, production, accounts, setup, update, install-ui)
  launcher.py         Dual-mode Playground (:5040) & Production (:5041) supervisor
production/           Database schema/migrations, ZeroMQ MRS broker, NIM workers & auto-provisioned local RSA keys
phonos_lib/           Python helper library for notebooks (`get_project`, `ModelManager`, `FileManager`)
static/ & templates/  Mol* 3D viewer (`phonos_viewer.js`), Notebook IDE, Copilot UI, Account Manager & Landing Page
examples/             Reference `custom_model_server.py` BYOM FastAPI/HTTP starter
scripts/              Zero-dependency `install-ui.py` GUI wizard, `setup.sh` (Linux/macOS) & `setup.ps1` (Windows)
packaging/            PyInstaller spec, Linux binary builder & Windows Inno Setup (`Phonos-Setup-x64.exe`)
tests/                Unit & integration test suites (`pytest`)
```

---

## Security, Legal & Clean Open-Source Push Note

- [License (Phonos Non-Commercial Source License v1.0)](./LICENSE)
- [Research-Use-Only & Biosecurity Disclaimer](./DISCLAIMER.md)
- [Third-Party & Upstream Model Licenses](./THIRD_PARTY_LICENSES.md)
- [Security Policy](./SECURITY.md) · [Contributing Guide](./CONTRIBUTING.md)

> **Tip before pushing to a new public GitHub repository:** If migrating from an older internal Git repository whose commit history contained legacy keys or merged trees, initialize a clean single-commit public history before pushing to `https://github.com/mxsp/phonos`:
> ```bash
> git checkout --orphan oss-release
> git add -A
> git commit -m "Initial public release of Phonos v1.0.0"
> ```
