#!/usr/bin/env python3
"""
Test script to debug ESMFold protein sequence to PDB conversion.
"""

import sys
import os
import traceback

# Add production directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Test importing the conversion function
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("node3_diffdock", "node3-diffdock.py")
    node3_diffdock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(node3_diffdock)
    sequence_to_pdb = node3_diffdock.sequence_to_pdb
    print("✓ Successfully imported sequence_to_pdb function")
except Exception as e:
    print(f"✗ Failed to import: {e}")
    traceback.print_exc()
    sys.exit(1)

# Test sequences
test_sequences = {
    "Tiny (20aa)": "MKFLKFSLLTAVLLSVVFAT",
    "Short (40aa)": "MTEYKLVVVGAGGVGKSALTIQLIQNHFVDEYDPTIED",
}

print("\n" + "="*60)
print("Testing ESMFold Sequence to PDB Conversion")
print("="*60 + "\n")

for name, sequence in test_sequences.items():
    print(f"\nTest: {name}")
    print(f"Sequence: {sequence}")
    print(f"Length: {len(sequence)} amino acids")

    try:
        print("  Attempting conversion...")
        pdb_string = sequence_to_pdb(sequence)

        if pdb_string:
            print(f"  ✓ SUCCESS - Generated PDB ({len(pdb_string)} characters)")
            print(f"  Preview (first 200 chars):")
            print("  " + pdb_string[:200].replace("\n", "\n  "))
        else:
            print(f"  ✗ FAILED - No PDB string returned (None)")

    except Exception as e:
        print(f"  ✗ ERROR: {e}")
        print("\n  Full traceback:")
        traceback.print_exc()

print("\n" + "="*60)
print("Test Complete")
print("="*60)
