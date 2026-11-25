#!/usr/bin/env bash
# Portable Gunicorn launcher for Phonos (Linux / macOS).
# Derives the project root automatically; override with PHONOS_DIR or GUNICORN_BIND.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHONOS_DIR="${PHONOS_DIR:-$SCRIPT_DIR}"
APP_DIR="${PHONOS_APP_DIR:-$PHONOS_DIR}"

cd "$APP_DIR"
mkdir -p logs job_outputs production/user_uploads .data

export PYTHONPATH="$APP_DIR:$APP_DIR/production${PYTHONPATH:+:$PYTHONPATH}"

if [ -x "$PHONOS_DIR/.venv/bin/gunicorn" ]; then
    GUNICORN="$PHONOS_DIR/.venv/bin/gunicorn"
elif [ -x "$PHONOS_DIR/venv/bin/gunicorn" ]; then
    GUNICORN="$PHONOS_DIR/venv/bin/gunicorn"
else
    GUNICORN="$(command -v gunicorn)"
fi

exec "$GUNICORN" --config gunicorn_config.py wsgi:application
