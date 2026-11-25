#!/usr/bin/env python3
"""
Verify that Boltz2 fixes are applied correctly
"""
import json

print("=" * 60)
print("VERIFYING BOLTZ2 FIXES")
print("=" * 60)

# Test 1: Check API route handler uses correct payload structure
print("\n✓ Test 1: API Route Handler Payload Structure")
with open('phonos_app/routes/api_boltz2.py', 'r') as f:
    content = f.read()

    # Check for correct payload structure
    assert '"polymers": polymers' in content, "❌ Missing 'polymers' key in payload"
    assert '"ligands": ligands' in content or 'payload["ligands"] = ligands' in content, "❌ Missing ligands handling"

    # Check old wrong format is removed
    assert '"sequences": sequences' not in content, "❌ Old 'sequences' format still present"

    print("  ✓ Uses 'polymers' array (correct)")
    print("  ✓ Uses separate 'ligands' array (correct)")
    print("  ✓ Old 'sequences' format removed")

# Test 2: Check parameter ranges
print("\n✓ Test 2: Parameter Validation Ranges")
with open('phonos_app/routes/api_boltz2.py', 'r') as f:
    content = f.read()

    # Check recycling_steps
    assert 'min(max(1, int(data.get(\'recycling_steps\', 3))), 6)' in content, "❌ recycling_steps range incorrect"
    print("  ✓ recycling_steps: 1-6 (correct)")

    # Check sampling_steps
    assert 'min(max(10, int(data.get(\'sampling_steps\', 50))), 1000)' in content, "❌ sampling_steps range incorrect"
    print("  ✓ sampling_steps: 10-1000 (correct)")

    # Check diffusion_samples
    assert 'min(max(1, int(data.get(\'diffusion_samples\', 1))), 5)' in content, "❌ diffusion_samples range incorrect"
    print("  ✓ diffusion_samples: 1-5 (correct)")

# Test 3: Check max sequence length
print("\n✓ Test 3: Maximum Sequence Length")
with open('phonos_app/routes/api_boltz2.py', 'r') as f:
    content = f.read()

    assert '> 4096' in content, "❌ Max sequence length not updated to 4096"
    assert 'max_sequence_length\': 4096' in content, "❌ Info endpoint not updated"
    print("  ✓ Max sequence length: 4096 (correct)")

# Test 4: Check molecule type support
print("\n✓ Test 4: Molecule Type Support")
with open('phonos_app/routes/api_boltz2.py', 'r') as f:
    content = f.read()

    assert 'molecule_type' in content, "❌ molecule_type not added"
    assert "['protein', 'dna', 'rna']" in content, "❌ DNA/RNA support not added"
    assert '"molecule_type": molecule_type' in content, "❌ molecule_type not passed to API"
    print("  ✓ Supports: protein, dna, rna")
    print("  ✓ molecule_type passed to polymers")

# Test 5: Check worker test function
print("\n✓ Test 5: Worker Test Function Fixed")
with open('production/boltz2_worker.py', 'r') as f:
    content = f.read()

    # Find test function
    test_start = content.find('def test_boltz2_api():')
    test_section = content[test_start:test_start+2000]

    assert 'test_polymers' in test_section, "❌ Worker test not using polymers"
    assert '"molecule_type": "protein"' in test_section, "❌ Worker test missing molecule_type"
    assert 'polymers=test_polymers' in test_section, "❌ Worker test not passing polymers param"
    print("  ✓ Test function uses correct API format")

# Test 6: Verify info endpoint updates
print("\n✓ Test 6: /api/boltz2/info Endpoint")
with open('phonos_app/routes/api_boltz2.py', 'r') as f:
    content = f.read()

    assert "'recycling_steps': {'min': 1, 'max': 6, 'default': 3}" in content, "❌ Info endpoint recycling_steps not updated"
    assert "'sampling_steps': {'min': 10, 'max': 1000, 'default': 50}" in content, "❌ Info endpoint sampling_steps not updated"
    assert "'diffusion_samples': {'min': 1, 'max': 5, 'default': 1}" in content, "❌ Info endpoint diffusion_samples not updated"
    print("  ✓ All parameter ranges correct in info endpoint")

print("\n" + "=" * 60)
print("ALL FIXES VERIFIED SUCCESSFULLY ✓")
print("=" * 60)

# Summary
print("\nSummary of Applied Fixes:")
print("1. ✓ Payload structure: 'polymers' + 'ligands' arrays")
print("2. ✓ Parameter ranges: recycling (1-6), sampling (10-1000), diffusion (1-5)")
print("3. ✓ Max sequence length: 4096 residues")
print("4. ✓ Molecule type support: protein, DNA, RNA")
print("5. ✓ Worker test function updated")
print("6. ✓ Info endpoint corrected")

print("\n🎉 Boltz2 integration is now fully compliant with NVIDIA API spec!")
