#!/usr/bin/env bash
#
# Phonos Production Deployment Script
# ------------------------------------
# Used by GitHub Actions CI/CD and can be run manually.
#
# Usage:
#   ./deploy/deploy.sh              # Full deploy (pull + deps + build + restart)
#   ./deploy/deploy.sh --pull-only  # Just pull latest code
#   ./deploy/deploy.sh --restart    # Just restart docker services
#   ./deploy/deploy.sh --status     # Show service status
#
set -euo pipefail

# Portable: DEPLOY_DIR defaults to the repo root (parent of this deploy/ dir).
# Override with PHONOS_DIR for non-standard layouts.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${PHONOS_DIR:-$(dirname "$SCRIPT_DIR")}"
COMPOSE_DIR="${PHONOS_COMPOSE_DIR:-${DEPLOY_DIR}/phonos-main}"
VENV_DIR="${DEPLOY_DIR}/venv"
LOG_FILE="${DEPLOY_DIR}/deploy/deploy.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

cd "$DEPLOY_DIR"

case "${1:-full}" in
  --pull-only)
    log "Pulling latest code..."
    git fetch origin main
    git reset --hard origin/main
    log "Pull complete."
    ;;

  --restart)
    log "Restarting Docker services..."
    cd "$COMPOSE_DIR"
    docker-compose up -d --build --remove-orphans
    docker-compose ps
    log "Restart complete."
    ;;

  --status)
    echo "=== Git Status ==="
    git log --oneline -3
    echo ""
    echo "=== Docker Services ==="
    cd "$COMPOSE_DIR"
    docker-compose ps
    echo ""
    echo "=== Disk Usage ==="
    df -h / | tail -1
    ;;

  full|"")
    log "=== Full Deploy Started ==="

    # 1. Pull latest
    log "Pulling latest code..."
    git fetch origin main
    git reset --hard origin/main

    # 2. Update Python deps if requirements changed
    if git diff HEAD~1 --name-only 2>/dev/null | grep -q "phonos-main/requirements.txt"; then
      log "Requirements changed, updating Python deps..."
      if [ -d "$VENV_DIR" ]; then
        source "$VENV_DIR/bin/activate"
      else
        python3 -m venv "$VENV_DIR"
        source "$VENV_DIR/bin/activate"
      fi
      pip install -q -r phonos-main/requirements.txt
    fi

    # 3. Rebuild frontend if changed
    if git diff HEAD~1 --name-only 2>/dev/null | grep -qE "^frontend/|^vite.config"; then
      log "Frontend changed, rebuilding..."
      npm install --production 2>/dev/null || true
      npm run build 2>/dev/null || log "No build script found"
    fi

    # 4. Restart Docker services
    log "Restarting Docker services..."
    cd "$COMPOSE_DIR"
    docker-compose pull 2>/dev/null || true
    docker-compose up -d --build --remove-orphans
    docker-compose ps

    log "=== Full Deploy Complete ==="
    ;;

  *)
    echo "Usage: $0 [--pull-only|--restart|--status|full]"
    exit 1
    ;;
esac
