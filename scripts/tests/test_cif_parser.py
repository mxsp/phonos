
import os

def extract_plddt_from_cif(cif_path):
    print(f"Parsing {cif_path}...")
    with open(cif_path, 'r') as f:
        lines = f.readlines()

    plddt_scores = []
    
    # Strategy 1: Look for _ma_qa_metric_local (ModelCIF standard)
    # _ma_qa_metric_local.metric_value is the score
    # _ma_qa_metric_local.metric_id usually links to _ma_qa_metric.id (1 = pLDDT)
    
    in_qa_metric_loop = False
    qa_metric_indices = {}
    qa_scores = []
    
    for i, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        
        if line.startswith('loop_'):
            # Check if this loop is the QA metric loop
            # We look ahead to see the headers
            is_qa_loop = False
            headers = []
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith('_'):
                header = lines[j].strip()
                headers.append(header)
                if '_ma_qa_metric_local.metric_value' in header:
                    is_qa_loop = True
                j += 1
            
            if is_qa_loop:
                # Map headers to indices
                qa_metric_indices = {h.split('.')[-1]: k for k, h in enumerate(headers)}
                # Process data lines
                k = j
                while k < len(lines) and not lines[k].strip().startswith('#') and not lines[k].strip().startswith('loop_') and not lines[k].strip().startswith('_'):
                    parts = lines[k].strip().split()
                    if len(parts) >= len(headers):
                        try:
                            val_idx = qa_metric_indices.get('metric_value')
                            type_idx = qa_metric_indices.get('metric_id')
                            
                            # Only if metric_id is 1 (pLDDT) or not specified (assume pLDDT if only one metric)
                            # In this file: 1 pLDDT 'Predicted lddt'
                            if val_idx is not None:
                                score = float(parts[val_idx])
                                qa_scores.append(score)
                        except (ValueError, IndexError):
                            pass
                    k += 1
                break # Found it, stop looking

    if qa_scores:
        print(f"Found {len(qa_scores)} scores in _ma_qa_metric_local")
        return qa_scores

    # Strategy 2: Fallback to ATOM records if Strategy 1 failed
    print("Fallback to ATOM records...")
    
    # First, map the _atom_site columns
    atom_site_indices = {}
    in_atom_site_loop = False
    atom_lines_start = -1
    
    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith('loop_'):
            # Check for atom_site loop
            j = i + 1
            headers = []
            is_atom_loop = False
            while j < len(lines) and lines[j].strip().startswith('_'):
                if '_atom_site.' in lines[j]:
                    is_atom_loop = True
                    headers.append(lines[j].strip().split('.')[-1]) # just the name
                j += 1
            
            if is_atom_loop:
                atom_site_indices = {name: k for k, name in enumerate(headers)}
                atom_lines_start = j
                break
    
    if not atom_site_indices:
        print("Could not find _atom_site loop")
        return []

    print(f"Atom site indices: {atom_site_indices}")
    
    b_factor_idx = atom_site_indices.get('B_iso_or_equiv')
    atom_name_idx = atom_site_indices.get('label_atom_id')
    group_pdb_idx = atom_site_indices.get('group_PDB')
    
    if b_factor_idx is None:
        print("No B_iso_or_equiv column found")
        return []

    scores = []
    seen_residues = set() # (chain, res_num)

    current_lines = lines[atom_lines_start:]
    for line in current_lines:
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('loop_'):
            if line.startswith('#'): continue # skip comments
            if line.startswith('loop_'): break # stop if new loop
            continue
            
        parts = line.split()
        # Simple split might fail if there are quoted strings with spaces, but for atoms usually ok
        # Ideally use a proper tokenizer, but let's assume standard CIF for atoms
        
        try:
            # Check if it's an ATOM record (not HETATM usually for pLDDT, but check)
            if group_pdb_idx is not None and parts[group_pdb_idx] != 'ATOM':
                continue
                
            # Only CA atoms
            if atom_name_idx is not None and parts[atom_name_idx] != 'CA':
                continue
                
            score = float(parts[b_factor_idx])
            scores.append(score)
            
        except (ValueError, IndexError):
            continue
            
    return scores

# Test
scores = extract_plddt_from_cif('boltz2_test_structure.cif')
print(f"Extracted {len(scores)} scores: {scores[:10]}...")
