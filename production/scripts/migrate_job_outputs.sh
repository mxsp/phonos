#!/bin/bash
# Migrate existing job outputs to new storage location
# This script is non-destructive - original files are preserved

set -euo pipefail

# Portable: derive the repo root and let SOURCE/DEST be overridden via env.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHONOS_DIR="${PHONOS_DIR:-$(dirname "$(dirname "$SCRIPT_DIR")")}"
RUN_USER="${PHONOS_USER:-${SUDO_USER:-$(id -un)}}"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"

# Old location (fallback storage) -> new location (primary storage).
SOURCE="${MIGRATE_SOURCE:-${JOB_OUTPUT_DIR_FALLBACK:-${PHONOS_DIR}/job_outputs}}"
DEST="${MIGRATE_DEST:-${JOB_OUTPUT_DIR:-${PHONOS_DIR}/job_outputs}}"
LOG_DIR="${PHONOS_LOG_DIR:-${PHONOS_DIR}/logs}"
LOG_FILE="${LOG_DIR}/migration.log"

# Create log directory if needed
if [ ! -d "$LOG_DIR" ]; then
    mkdir -p "$LOG_DIR" 2>/dev/null || { sudo mkdir -p "$LOG_DIR"; sudo chown "$RUN_USER:$RUN_GROUP" "$LOG_DIR"; }
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=========================================="
log "Starting job output migration"
log "Source: $SOURCE"
log "Destination: $DEST"
log "=========================================="

# Check if source exists and has content
if [ ! -d "$SOURCE" ]; then
    log "Source directory does not exist: $SOURCE"
    log "Nothing to migrate."
    exit 0
fi

SOURCE_COUNT=$(find "$SOURCE" -type f 2>/dev/null | wc -l)
if [ "$SOURCE_COUNT" -eq 0 ]; then
    log "Source directory is empty. Nothing to migrate."
    exit 0
fi

log "Found ${SOURCE_COUNT} files to migrate"

# Ensure destination exists
mkdir -p "$DEST"

# Get source size before migration
SOURCE_SIZE=$(du -sb "$SOURCE" 2>/dev/null | cut -f1 || echo "0")
SOURCE_SIZE_HR=$(du -sh "$SOURCE" 2>/dev/null | cut -f1 || echo "0")
log "Source size: ${SOURCE_SIZE_HR} (${SOURCE_SIZE} bytes)"

# Check destination disk space
DEST_FREE=$(df -B1 "$DEST" | tail -1 | awk '{print $4}')
if [ "$SOURCE_SIZE" -gt "$DEST_FREE" ]; then
    log "ERROR: Not enough space on destination!"
    log "  Required: ${SOURCE_SIZE_HR}"
    log "  Available: $(numfmt --to=iec $DEST_FREE 2>/dev/null || echo $DEST_FREE)"
    exit 1
fi

# Use rsync for safe copy with progress
log "Starting rsync migration..."
log "----------------------------------------"

if rsync -avh --progress --stats "$SOURCE/" "$DEST/" 2>&1 | tee -a "$LOG_FILE"; then
    log "----------------------------------------"
    log "Rsync completed successfully"
else
    log "ERROR: Rsync failed!"
    exit 1
fi

# Verify migration
log "----------------------------------------"
log "Verifying migration..."

DEST_SIZE=$(du -sb "$DEST" 2>/dev/null | cut -f1 || echo "0")
DEST_COUNT=$(find "$DEST" -type f 2>/dev/null | wc -l)

log "Source: ${SOURCE_COUNT} files, ${SOURCE_SIZE} bytes"
log "Destination: ${DEST_COUNT} files, ${DEST_SIZE} bytes"

# Allow small difference due to filesystem overhead
DIFF=$((SOURCE_SIZE - DEST_SIZE))
if [ "$DIFF" -lt 0 ]; then
    DIFF=$((DIFF * -1))
fi

THRESHOLD=$((SOURCE_SIZE / 100))  # 1% tolerance
if [ "$DIFF" -le "$THRESHOLD" ] && [ "$SOURCE_COUNT" -eq "$DEST_COUNT" ]; then
    log "Migration verified successfully!"
    log "  Files match: ${SOURCE_COUNT} = ${DEST_COUNT}"
    log "  Size difference: ${DIFF} bytes (within tolerance)"
else
    log "WARNING: Size or file count mismatch!"
    log "  File count: ${SOURCE_COUNT} vs ${DEST_COUNT}"
    log "  Size difference: ${DIFF} bytes"
    log "Please verify manually before removing source files."
fi

log "=========================================="
log "Migration complete"
log ""
log "IMPORTANT: Original files at ${SOURCE} have been preserved."
log "After verifying the migration, you can optionally remove them with:"
log "  rm -rf ${SOURCE}/*"
log ""
log "Or keep them as a backup/fallback location."
log "=========================================="
