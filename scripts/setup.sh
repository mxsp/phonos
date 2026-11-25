#!/usr/bin/env bash
# Phonos One-Time Setup Script (Linux / macOS)
#
# Creates Python virtualenv (.venv), installs Phonos + RDKit + BioPython + CLI,
# initializes isolated Playground (:5040) & Production (:5041) data folders,
# and optionally creates the first Production Admin account (username + password).
#
# Usage:
#   ./scripts/setup.sh
#   ./scripts/setup.sh --mode production --admin-user admin --admin-password "MyStrongPassword"
#   ./scripts/setup.sh --mode playground --non-interactive

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE=""
ADMIN_USER=""
ADMIN_PASSWORD=""
LAB_NAME="Phonos Lab"
NON_INTERACTIVE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --admin-user) ADMIN_USER="${2:-}"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="${2:-}"; shift 2 ;;
    --lab-name) LAB_NAME="${2:-Phonos Lab}"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

echo "===================================================================="
echo "  Phonos — One-Time Setup (Playground :5040 + Production Lab :5041)"
echo "===================================================================="

python_ok() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if ! python_ok; then
  echo "Python 3.11+ with venv is required. Attempting package manager install..." >&2
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    brew install python@3.12 || true
  elif command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv python3-pip libxrender1 libxext6 || true
  fi
fi

python_ok || { echo "Error: python3 (3.11+) with venv is required." >&2; exit 1; }

cd "$ROOT"

VENV_DIR=".venv"
if [[ ! -d ".venv" && -d "venv" ]]; then
  VENV_DIR="venv"
fi

echo "[1/3] Setting up Python virtual environment ($VENV_DIR)..."
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -e . --quiet

echo "[2/3] Initializing isolated Playground sandbox (:5040)..."
"$VENV_DIR/bin/phonos" setup --mode playground

if [[ -z "$MODE" ]]; then
  if [[ "$NON_INTERACTIVE" -eq 1 || ! -t 0 ]]; then
    MODE="both"
  else
    echo ""
    echo "Configure Production Admin account now? (Leave empty to set up in the browser at /setup)"
    read -r -p "Admin username (leave empty to skip): " ADMIN_USER || true
    if [[ -n "$ADMIN_USER" ]]; then
      read -r -s -p "Admin password (min. 8 chars): " ADMIN_PASSWORD || true
      echo ""
    fi
    MODE="both"
  fi
fi

if [[ "$MODE" == "production" || "$MODE" == "both" ]]; then
  echo "[3/3] Initializing Production lab environment (:5041)..."
  if [[ -n "$ADMIN_USER" && -n "$ADMIN_PASSWORD" ]]; then
    "$VENV_DIR/bin/phonos" setup --mode production --lab-name "$LAB_NAME" --admin-user "$ADMIN_USER" --admin-password "$ADMIN_PASSWORD"
  else
    "$VENV_DIR/bin/phonos" setup --mode production --lab-name "$LAB_NAME"
  fi
else
  echo "[3/3] Playground-only setup complete."
fi

echo ""
echo "Setup complete! Start Phonos with:"
echo "  Playground (Demo, :5040):      $VENV_DIR/bin/phonos playground"
echo "  Production (Clean, :5041):     $VENV_DIR/bin/phonos production"
echo "  Both side by side:             $VENV_DIR/bin/phonos both"
echo "  Manage Lab Accounts:           $VENV_DIR/bin/phonos accounts list"
echo "  Check for GitHub updates:      $VENV_DIR/bin/phonos update --check"
