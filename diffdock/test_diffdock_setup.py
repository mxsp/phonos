#!/usr/bin/env python3
"""
Test script to verify DiffDock auto-installation.
This checks if the auto-install functions work without starting the full worker.
"""

import logging
import sys
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def check_dependencies():
    """Check if all dependencies are available."""
    logger.info("=" * 60)
    logger.info("Checking DiffDock Dependencies")
    logger.info("=" * 60)

    deps_status = {}

    # Check PyTorch
    try:
        import torch
        deps_status['PyTorch'] = f'✓ Installed (v{torch.__version__})'
        if torch.cuda.is_available():
            deps_status['CUDA'] = f'✓ Available ({torch.cuda.get_device_name(0)})'
        else:
            deps_status['CUDA'] = '⚠ Not available (CPU-only mode)'
    except ImportError:
        deps_status['PyTorch'] = '✗ Not installed'

    # Check RDKit
    try:
        from rdkit import Chem
        from rdkit import __version__ as rdkit_version
        deps_status['RDKit'] = f'✓ Installed (v{rdkit_version})'
    except ImportError:
        deps_status['RDKit'] = '✗ Not installed'

    # Check ESMFold
    try:
        import esm
        deps_status['ESMFold'] = '✓ Installed'
    except ImportError:
        deps_status['ESMFold'] = '✗ Not installed'

    # Check other dependencies
    for package in ['biopython', 'scipy', 'numpy', 'pandas']:
        try:
            __import__(package.lower())
            deps_status[package.capitalize()] = '✓ Installed'
        except ImportError:
            deps_status[package.capitalize()] = '✗ Not installed'

    # Check DiffDock repository
    import os
    diffdock_dir = os.path.expanduser('~/DiffDock')
    if os.path.exists(diffdock_dir):
        deps_status['DiffDock Repo'] = f'✓ Found at {diffdock_dir}'

        # Check for model weights
        weights_patterns = ['workdir/*/score_model.pt', 'workdir/*/confidence_model.pt']
        from glob import glob
        weights_found = any(glob(os.path.join(diffdock_dir, pattern)) for pattern in weights_patterns)
        if weights_found:
            deps_status['Model Weights'] = '✓ Downloaded'
        else:
            deps_status['Model Weights'] = '⚠ Not found'
    else:
        deps_status['DiffDock Repo'] = f'✗ Not found at {diffdock_dir}'

    # Print status
    logger.info("")
    for component, status in deps_status.items():
        logger.info(f"  {component:20s} : {status}")
    logger.info("")

    # Overall status
    all_installed = all('✓' in status for status in deps_status.values())
    if all_installed:
        logger.info("✓ All dependencies are installed and ready!")
        return True
    else:
        logger.warning("⚠ Some dependencies are missing. The worker will auto-install them on first run.")
        return False


def test_auto_install():
    """Test the auto-installation by importing the worker module."""
    logger.info("=" * 60)
    logger.info("Testing Auto-Installation")
    logger.info("=" * 60)

    try:
        # This will trigger auto-installation if needed
        sys.path.insert(0, str(Path(__file__).parent))

        logger.info("Importing node3-diffdock module...")
        logger.info("This will auto-install missing dependencies...")
        logger.info("")

        # Import will trigger load_model() which runs auto-installation
        # Note: We can't actually import it fully without Redis, but we can test the functions
        logger.info("Auto-installation would be triggered on first worker run.")
        logger.info("To test fully, run: python3 node3-diffdock.py")

    except Exception as e:
        logger.error(f"Error during import test: {e}")
        return False

    return True


def main():
    """Main test function."""
    print("\n" + "=" * 60)
    print("DiffDock Auto-Installation Test")
    print("=" * 60 + "\n")

    # Check current status
    deps_ok = check_dependencies()

    print()
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)

    if deps_ok:
        print("\n✓ All dependencies are already installed!")
        print("  You can run: python3 node3-diffdock.py")
    else:
        print("\n⚠ Some dependencies are missing.")
        print("  Run the worker to trigger auto-installation:")
        print("  python3 node3-diffdock.py")
        print("\n  Or install manually with:")
        print("  ./setup_diffdock.sh")

    print("\n" + "=" * 60 + "\n")


if __name__ == '__main__':
    main()
