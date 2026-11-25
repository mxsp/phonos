#!/usr/bin/env python3
"""
run_esmfold.py
Standalone script to run ESMFold (facebook/esmfold_v1) on GPU and save a PDB.

Usage:
    # create a venv / conda env as you prefer, then:
    pip install --upgrade pip
    pip install "transformers>=4.30.0" torch  # make sure torch matches your CUDA toolkit
    pip install fair-esm               # provides ESM tooling if you want the original repo utilities
    # then run:
    python run_esmfold.py
"""

import os
import sys
import torch

# HuggingFace imports
from transformers import AutoTokenizer, EsmForProteinFolding

# utilities for converting model outputs -> PDB (taken/adapted from HF + ESM community examples)
from transformers.models.esm.openfold_utils.protein import to_pdb, Protein as OFProtein
from transformers.models.esm.openfold_utils.feats import atom14_to_atom37

def convert_outputs_to_pdb(outputs):
    """
    Convert model outputs (dict of tensors) to a list of PDB strings.
    This follows the community / HF approach: map atom14->atom37, build OFProtein,
    and call to_pdb() for each example in batch.
    """
    # outputs is expected to be a dict with keys like:
    # 'positions' (list/tuple with per-recycle), 'aatype', 'atom37_atom_exists', 'residue_index', 'plddt', 'chain_index' maybe
    # We use final positions: outputs["positions"][-1]
    final_atom_positions = atom14_to_atom37(outputs["positions"][-1], outputs)
    # move small tensors to cpu and numpy
    outputs_cpu = {k: v.to("cpu") for k, v in outputs.items()}
    final_atom_positions = final_atom_positions.to("cpu")
    final_atom_mask = outputs_cpu["atom37_atom_exists"]

    pdbs = []
    batch = outputs_cpu["aatype"].shape[0]
    for i in range(batch):
        aa = outputs_cpu["aatype"][i]
        pred_pos = final_atom_positions[i]
        mask = final_atom_mask[i]
        resid = outputs_cpu["residue_index"][i] + 1
        plddt = outputs_cpu["plddt"][i] if "plddt" in outputs_cpu else None

        pred = OFProtein(
            aatype=aa,
            atom_positions=pred_pos,
            atom_mask=mask,
            residue_index=resid,
            b_factors=plddt,
            chain_index=outputs_cpu.get("chain_index", None)[i] if "chain_index" in outputs_cpu else None,
        )
        pdbs.append(to_pdb(pred))
    return pdbs


def main():
    # Example sequence (ubiquitin-like short protein). Replace with your sequence.
    seq = (
        "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
    )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}", file=sys.stderr)

    if device == "cpu":
        print("WARNING: running on CPU will be very slow. For GPU run set CUDA_VISIBLE_DEVICES and have PyTorch+CUDA installed.", file=sys.stderr)

    # Try to reduce GPU memory by loading model with low_cpu_mem_usage where supported.
    print("Loading tokenizer & model (facebook/esmfold_v1). This will download ~several GB on first run.", file=sys.stderr)
    tokenizer = AutoTokenizer.from_pretrained("facebook/esmfold_v1")
    model = EsmForProteinFolding.from_pretrained("facebook/esmfold_v1", low_cpu_mem_usage=True)

    # Optional: use fp16 to save memory (works on modern GPUs). If you get errors, comment the .half() line.
    use_fp16 = True if device == "cuda" else False
    if use_fp16:
        try:
            model = model.half()
            print("Converted model to fp16 (half).", file=sys.stderr)
        except Exception as e:
            print("Could not convert to fp16, continuing in fp32:", e, file=sys.stderr)

    model = model.to(device)
    model.eval()

    # Tokenize (note: add_special_tokens=False for these models)
    tokenized = tokenizer([seq], return_tensors="pt", add_special_tokens=False)
    input_ids = tokenized["input_ids"].to(device)

    # Some models accept dict-style args; here EsmForProteinFolding expects input ids as 'input_ids'
    with torch.no_grad():
        print("Running model inference (this may take seconds->minutes depending on seq length and GPU).", file=sys.stderr)
        outputs = model(input_ids)
        # Many community examples return the outputs['structures' or similar], but HF returns a dict-like mapping.
        # The convert_outputs_to_pdb function expects a dict with keys used by openfold_utils.
        # If the model returns a wrapper object (BatchEncoding-like), inspect and adapt as needed.
        # For HF EsmForProteinFolding the forward returns a dict with needed tensors.
        if isinstance(outputs, tuple) or hasattr(outputs, "values"):
            # if it's a tuple (older TF config) or model returns a dict-like, try to get the dict
            if isinstance(outputs, tuple):
                outputs = outputs[0]
        # ensure tensors are present
        required = ["positions", "aatype", "atom37_atom_exists", "residue_index"]
        for r in required:
            if r not in outputs:
                print(f"ERROR: model output missing expected key '{r}'. Inspect 'outputs' contents.", file=sys.stderr)
                # print available keys for debugging
                print("Available output keys:", list(outputs.keys()), file=sys.stderr)
                return

        pdb_list = convert_outputs_to_pdb(outputs)

    # Save PDB(s) - for the single input we save one file
    out_dir = "esmfold_out"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "prediction_1.pdb")
    with open(out_path, "w") as f:
        f.write("".join(pdb_list[0]))
    print(f"Saved predicted PDB to: {out_path}")

    # Optionally print a small summary: pLDDT approximate info if present
    if "plddt" in outputs:
        plddt = outputs["plddt"].mean().item()
        print(f"Approx mean pLDDT: {plddt:.2f}")

if __name__ == "__main__":
    main()
