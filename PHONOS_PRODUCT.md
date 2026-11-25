# Phonos — Open Molecular Discovery Workbench

Phonos is a self-hosted computational biology and molecular discovery platform unifying **3D structure prediction**, **molecular docking**, **de novo protein & small-molecule design**, **collaborative Python/Bioinformatics Notebooks**, **interactive Mol\* 3D analysis**, **custom BYOM (Bring Your Own Model) endpoints**, and an integrated **AI Discovery Copilot**.

> **Research Use Only (RUO):** Computational outputs are probabilistic estimates intended strictly for scientific research and education—never for clinical, diagnostic, or medical decision-making. See [DISCLAIMER.md](./DISCLAIMER.md) and [LICENSE](./LICENSE).

---

## Supported Bio-AI Models & Custom Endpoints

| Capability | Built-In Integrations (NVIDIA NIM / Self-Hosted) | Custom Endpoints (BYOM) |
|---|---|---|
| **Structure Prediction** | Boltz-2, OpenFold3 *(+ local ESMFold / AlphaFold2 workers)* | Direct HTTP REST, Self-Hosted NIM, ColabFold, HuggingFace |
| **Molecular Docking** | DiffDock | Custom Vina / DiffDock / EquiBind REST servers |
| **Protein Design** | RFdiffusion, ProteinMPNN | Custom PyTorch / FastAPI backbone & sequence servers |
| **Small-Molecule Generation** | GenMol, MolMIM *(non-commercial R&D)* | Custom RDKit / SMILES generative endpoints |
| **Genomics & DNA/RNA** | Evo 2 | Custom sequence foundation models & OpenAI-compatible APIs |

---

## Core Capabilities

- **Unified Project & Pipeline Workbench**: Organize sequences, PDB/mmCIF/SDF files, multi-step discovery flows, and batch screening runs with role-based lab access (`admin`, `scientist`, `operator`, `viewer` + per-project `Manager`, `Contributor`, `Viewer`).
- **Interactive 3D Molecular Viewer (Mol\*)**: Confidence (pLDDT) coloring, binding-pocket & ligand focus, distance/contact analysis, side-by-side structure comparison, publication-ready renders, and live 3D snapshot sharing with Phonos Copilot.
- **Built-In Notebook IDE**: Execute Python, Biopython, RDKit, and `phonos_sdk` workflows directly in a zero-Docker local kernel or isolated container kernel, with inline 3D structure previews and Copilot code assistance.
- **Bring Your Own Model (BYOM)**: Connect your own local GPU servers, FastAPI microservices, HuggingFace endpoints, OpenAI-compatible servers, or self-hosted NVIDIA NIM containers with encrypted API keys, schema builders, and 1-click connection testing.
- **Discovery Copilot (Agent Lab)**: Context-aware scientific assistant with 70+ typed tools, multi-provider streaming (Gemini, Anthropic, OpenAI, OpenRouter, Ollama, LM Studio, vLLM), discovery routines, interactive artifacts, and tool sandbox testing.
