#!/bin/bash
# Shell wrapper for orphaned files cleanup script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHONOS_DIR="$(dirname "$SCRIPT_DIR")"

# Activate virtual environment if it exists
if [ -f "$PHONOS_DIR/../venv/bin/activate" ]; then
    source "$PHONOS_DIR/../venv/bin/activate"
fi

# Run the cleanup script
python3 "$PHONOS_DIR/cleanup_orphaned_files.py" "$@"
