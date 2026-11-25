#!/usr/bin/env bash
# Start Phonos locally (defaults to Production mode via unified launcher, or Gunicorn if PHONOS_USE_GUNICORN=1).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHONOS_DIR="${PHONOS_DIR:-$SCRIPT_DIR}"

cd "$PHONOS_DIR"
mkdir -p logs job_outputs production/user_uploads .data

export PYTHONPATH="$PHONOS_DIR:$PHONOS_DIR/production${PYTHONPATH:+:$PYTHONPATH}"

if [ -x "$PHONOS_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PHONOS_DIR/.venv/bin/python"
elif [ -x "$PHONOS_DIR/venv/bin/python" ]; then
    PYTHON_BIN="$PHONOS_DIR/venv/bin/python"
else
    PYTHON_BIN="${PHONOS_PYTHON:-$(command -v python3.12 || command -v python3.11 || command -v python3)}"
fi

if [ "${PHONOS_USE_GUNICORN:-0}" = "1" ]; then
    exec "$PYTHON_BIN" -m gunicorn \
        --config gunicorn_config.py \
        --pythonpath "$PHONOS_DIR,$PHONOS_DIR/production" \
        wsgi:application
fi

exec "$PYTHON_BIN" -m phonos_app.launcher "$@"
