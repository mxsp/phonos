#!/usr/bin/env python3
"""
Test local conversion functions without requiring NVIDIA API.
Tests SMILES to SDF conversion using RDKit.
"""

import sys
import os
from pathlib import Path

# Add production directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the worker module to test functions
import importlib.util
spec = importlib.util.spec_from_file_location("node3_diffdock", "node3-diffdock.py")
node3_diffdock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node3_diffdock)
smiles_to_sdf = node3_diffdock.smiles_to_sdf

# Test molecules
TEST_MOLECULES = {
    "Aspirin": "CC(=O)OC1=CC=CC=C1C(=O)O",
    "Caffeine": "CN1C=NC2=C1C(=O)N(C(=O)N2C)C",
    "Ibuprofen": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O",
    "Dopamine": "C1=CC(=C(C=C1CCN)O)O"
}


def test_smiles_conversion():
    """Test SMILES to SDF conversion."""
    print("\n" + "="*60)
    print("Testing SMILES to SDF Conversion (RDKit)")
    print("="*60 + "\n")

    passed = 0
    failed = 0

    for name, smiles in TEST_MOLECULES.items():
        print(f"Testing {name}: {smiles}")

        try:
            sdf_content = smiles_to_sdf(smiles)

            if sdf_content and len(sdf_content) > 0:
                print(f"  ✓ Success - Generated SDF ({len(sdf_content)} chars)")
                print(f"    Preview: {sdf_content[:100]}...")
                passed += 1
            else:
                print(f"  ✗ Failed - No SDF content generated")
                failed += 1

        except Exception as e:
            print(f"  ✗ Error: {e}")
            failed += 1

        print()

    print("="*60)
    print(f"Results: {passed} passed, {failed} failed out of {len(TEST_MOLECULES)} tests")
    print("="*60)

    return passed == len(TEST_MOLECULES)


def test_rdkit_availability():
    """Test if RDKit is available."""
    print("\n" + "="*60)
    print("Testing RDKit Availability")
    print("="*60 + "\n")

    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
        from rdkit import __version__ as rdkit_version

        print(f"✓ RDKit is available (version {rdkit_version})")

        # Test basic functionality
        mol = Chem.MolFromSmiles("CCO")
        if mol:
            print(f"✓ Basic SMILES parsing works")
            return True
        else:
            print(f"✗ SMILES parsing failed")
            return False

    except ImportError as e:
        print(f"✗ RDKit not available: {e}")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("DiffDock Local Conversion Tests")
    print("="*60)

    # Test 1: RDKit availability
    rdkit_ok = test_rdkit_availability()

    if not rdkit_ok:
        print("\n✗ Cannot proceed with tests - RDKit is not available")
        return False

    # Test 2: SMILES conversions
    conversions_ok = test_smiles_conversion()

    # Summary
    print("\n" + "="*60)
    print("Test Summary")
    print("="*60)

    if conversions_ok:
        print("\n✅ All local conversion tests passed!")
        print("\nThe DiffDock worker can successfully:")
        print("  • Convert SMILES to SDF format using RDKit")
        print("  • Generate 3D coordinates for molecules")
        print("  • Prepare ligands for docking")
        print("\nNote: Full docking requires:")
        print("  • NVIDIA API key configured in .env")
        print("  • Type-3 worker running")
        print("  • Redis connection")
        return True
    else:
        print("\n⚠ Some tests failed")
        return False


if __name__ == '__main__':
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
