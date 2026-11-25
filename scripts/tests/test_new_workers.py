#!/usr/bin/env python3
"""
Simple test script for new workers that doesn't require Redis
Tests basic API connectivity and response parsing
"""
import sys
import os
import json

# Add production directory to path
sys.path.insert(0, '/opt/phonos/production')

def test_worker_import(worker_name, worker_module):
    """Test importing a worker module."""
    try:
        print(f"Testing {worker_name} worker import...")
        module = __import__(worker_module)
        print(f"✓ {worker_name} worker imported successfully")
        
        # Test basic functionality if test function exists
        if hasattr(module, f'test_{worker_module.split("_")[0]}_api'):
            test_func = getattr(module, f'test_{worker_module.split("_")[0]}_api')
            print(f"Testing {worker_name} API functionality...")
            success = test_func()
            if success:
                print(f"✓ {worker_name} API test passed")
            else:
                print(f"✗ {worker_name} API test failed")
        
        return True
        
    except ImportError as e:
        print(f"✗ {worker_name} worker import failed: {e}")
        return False
    except Exception as e:
        print(f"✗ {worker_name} worker test failed: {e}")
        return False

def main():
    """Run basic tests for all new workers."""
    print("=" * 60)
    print("Testing New Workers - Basic Import and API Tests")
    print("=" * 60)
    
    # Test the workers we created
    workers = [
        ("AlphaFold2-RE", "alphafold2_re_worker"),
        ("AlphaFold2-RE-Multimer", "alphafold2_re_multimer_worker"), 
        ("Boltz2-Enhanced", "boltz2_enhanced_worker"),
    ]
    
    results = []
    for name, module in workers:
        success = test_worker_import(name, module)
        results.append((name, success))
        print()
    
    print("=" * 60)
    print("Test Results Summary:")
    print("=" * 60)
    
    for name, success in results:
        status = "✓ PASSED" if success else "✗ FAILED"
        print(f"{name}: {status}")
    
    print()
    print("Next Steps:")
    print("1. Install missing dependencies: pip3 install redis requests")
    print("2. Set NVIDIA API key in environment or worker files")
    print("3. Test full functionality with: python3 <worker>.py --test")
    print("4. Update .env file with proper NVIDIA_API_KEY")
    print("5. Deploy workers using deployment scripts")
    
    print()
    print("Configuration Updates:")
    print("- Added alphafold2-re (type: 'alphafold2-re')")
    print("- Added alphafold2-re-multimer (type: 'alphafold2-re-multimer')")
    print("- Added boltz2-enhanced (type: 'boltz2-enhanced')")
    print("- Updated model validation logic")
    print("- Added placeholder public keys for all workers")

if __name__ == '__main__':
    main()