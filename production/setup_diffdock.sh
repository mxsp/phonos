#!/bin/bash

# DiffDock Setup Script for node3-diffdock.py
# This script installs all necessary dependencies for running DiffDock

set -e  # Exit on error

echo "=========================================="
echo "DiffDock Setup for Phonos Node3"
echo "=========================================="
echo

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check Python version
echo -e "${YELLOW}[1/6] Checking Python version...${NC}"
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $python_version"

# Install PyTorch with CUDA support (adjust based on your CUDA version)
echo -e "${YELLOW}[2/6] Installing PyTorch...${NC}"
if python3 -c "import torch" 2>/dev/null; then
    echo "PyTorch already installed"
    python3 -c "import torch; print(f'PyTorch version: {torch.__version__}')"
else
    echo "Installing PyTorch with CUDA 11.8 support..."
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
fi

# Install RDKit
echo -e "${YELLOW}[3/6] Installing RDKit...${NC}"
if python3 -c "import rdkit" 2>/dev/null; then
    echo "RDKit already installed"
else
    pip install rdkit
fi

# Install other required packages
echo -e "${YELLOW}[4/6] Installing additional dependencies...${NC}"
pip install biopython scipy numpy pandas

# Install ESMFold for protein folding
echo -e "${YELLOW}[5/6] Installing ESMFold...${NC}"
if python3 -c "import esm" 2>/dev/null; then
    echo "ESM already installed"
else
    pip install fair-esm
fi

# Clone and setup DiffDock
echo -e "${YELLOW}[6/6] Setting up DiffDock...${NC}"
DIFFDOCK_DIR="$HOME/DiffDock"

if [ -d "$DIFFDOCK_DIR" ]; then
    echo "DiffDock directory already exists at $DIFFDOCK_DIR"
else
    echo "Cloning DiffDock repository..."
    cd "$HOME"
    git clone https://github.com/gcorso/DiffDock.git
    cd DiffDock

    # Install DiffDock requirements
    if [ -f requirements.txt ]; then
        echo "Installing DiffDock requirements..."
        pip install -r requirements.txt
    fi

    # Download model weights
    echo "Downloading DiffDock model weights..."
    if [ ! -d "workdir" ]; then
        mkdir -p workdir
    fi

    # Note: You may need to download model weights manually
    # Follow instructions at: https://github.com/gcorso/DiffDock#running-diffdock-on-your-own-complexes
    echo "Note: You may need to download model weights manually."
    echo "Visit: https://github.com/gcorso/DiffDock#running-diffdock-on-your-own-complexes"
fi

echo
echo -e "${GREEN}=========================================="
echo "Setup Complete!"
echo "==========================================${NC}"
echo
echo "Next steps:"
echo "1. Download DiffDock model weights if not done automatically"
echo "2. Run the worker: python3 node3-diffdock.py"
echo
echo "For testing, you can use:"
echo "  - Protein: Provide a PDB file or amino acid sequence"
echo "  - Ligand: Provide SMILES string (e.g., 'CC(=O)OC1=CC=CC=C1C(=O)O' for aspirin)"
echo
