# Legal, Scientific & Biosecurity Disclaimer

**PLEASE READ CAREFULLY BEFORE DEPLOYING OR USING PHONOS.**

## 1. Research Use Only (Not Medical, Clinical, or Diagnostic Advice)
Phonos is computational research orchestration software provided strictly **For Research Use Only (RUO)**.
- Phonos is **not** a medical device, in-vitro diagnostic (IVD), clinical decision-making system, or regulatory-validated instrument under EU MDR/IVDR, FDA 21 CFR, or any equivalent framework.
- All outputs produced through Phonos—including 3D protein/nucleic-acid structure predictions, pLDDT/PAE confidence metrics, protein-ligand docking poses, binding affinity estimates, *de novo* protein designs, DNA/RNA sequences, small-molecule SMILES, and AI Copilot responses—are **unverified probabilistic computational approximations**.
- Never use Phonos outputs to diagnose, treat, cure, or prevent any disease, or to administer any substance to humans or animals.

## 2. Wet-Lab Synthesis, Chemical Safety & Toxicity
Generated chemical structures (e.g., via `MolMIM`, `GenMol`, or custom endpoints) and engineered biological sequences (e.g., via `RFdiffusion`, `ProteinMPNN`, `Evo 2`, or custom endpoints) have **unknown toxicity, reactivity, immunogenicity, stability, and biological activity**.
- Any physical synthesis, expression, assay, or wet-lab handling of molecules or sequences designed with Phonos is performed **entirely at the user's and operating institution's sole risk and responsibility** under qualified laboratory safety oversight.
- Computational novelty does not imply freedom-to-operate; users are solely responsible for patent and intellectual-property clearance.

## 3. Biosecurity, Biosafety & Dual-Use Prohibition
You must **never** use Phonos or any connected model endpoint to:
- Design, mutate, enhance transmissibility/virulence, evade immune/diagnostic detection, or model regulated pathogens, Select Agents, biological toxins, bioweapons, or chemical weapons; or
- Violate national or international biosafety, biosecurity, gene-synthesis screening (e.g., IGSC / HHS frameworks), sanctions, or export-control laws (EU Dual-Use Regulation 2021/821, EAR, ITAR).

Operators of self-hosted Phonos instances are solely responsible for enforcing access controls, audit logging, and biosecurity screening appropriate for their jurisdiction and research institution.

## 4. Self-Hosted Data Governance & GDPR Controller Notice
Phonos is self-hosted software. **FyreFly Systems GbR / mxsp does not host, receive, store, or process any data from your self-hosted Phonos installation** (zero telemetry).
- The person or organization deploying Phonos is the sole **Data Controller** (under GDPR / BDSG or applicable privacy law) for all local user accounts, uploaded sequences/structures, notebooks, and logs.
- When you configure external cloud API keys (such as **NVIDIA NIM** `health.api.nvidia.com`, **Google Gemini**, **OpenAI**, **Anthropic**, **OpenRouter**, **IBM Quantum**, or custom HTTP endpoints), inputs sent to those models are transmitted directly from your Phonos instance to your chosen third-party API provider (which may process data outside the EU/EEA, e.g., in the United States) under **your own API account and data-processing agreements**.

## 5. Third-Party Model Licenses & Commercial Restrictions
Phonos acts as a client/orchestrator for external or user-hosted models. Connecting an API key or self-hosted container does **not** grant you a license to the underlying model weights or service. In particular:
- **MolMIM** (via NVIDIA BioNeMo / NIM) is governed by the **NVIDIA AI Foundation Models Community License** and is restricted to **non-commercial research and development** unless you hold a separate commercial license from NVIDIA.
- **NVIDIA NIM APIs** require each operator/user to supply their own API key and accept the NVIDIA NIM Terms of Service.
- See [THIRD_PARTY_LICENSES.md](./THIRD_PARTY_LICENSES.md) for model and library attributions.
