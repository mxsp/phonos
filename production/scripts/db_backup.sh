#!/bin/bash
# PostgreSQL Database Backup Script for Phonos
# Runs every 6 hours via systemd timer
# Backs up to all available disks for redundancy

set -euo pipefail

# Portable: derive the repo root from this script's location (production/scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHONOS_DIR="${PHONOS_DIR:-$(dirname "$(dirname "$SCRIPT_DIR")")}"
RUN_USER="${PHONOS_USER:-${SUDO_USER:-$(id -un)}}"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"

# Configuration (all overridable via environment)
DB_NAME="${DB_NAME:-phonos}"
DB_USER="${DB_USER:-phonos_user}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILENAME="phonos_backup_${TIMESTAMP}.dump"

# Backup destinations (will back up to all available). Defaults to a
# PROJECT_ROOT-relative dir; set BACKUP_DIRS (os.pathsep / comma separated, e.g.
# "/mnt/storage/backups/postgres:$HOME/phonos/backups/postgres") to add disks.
if [ -n "${BACKUP_DIRS:-}" ]; then
    IFS=':,' read -r -a BACKUP_DIRS <<< "${BACKUP_DIRS}"
else
    BACKUP_DIRS=("${PHONOS_DIR}/backups/postgres")
fi

# Logging
LOG_DIR="${PHONOS_LOG_DIR:-${PHONOS_DIR}/logs}"
LOG_FILE="${LOG_DIR}/db_backup.log"

# Create log directory if needed
if [ ! -d "$LOG_DIR" ]; then
    mkdir -p "$LOG_DIR" 2>/dev/null || { sudo mkdir -p "$LOG_DIR"; sudo chown "$RUN_USER:$RUN_GROUP" "$LOG_DIR"; }
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=========================================="
log "Starting PostgreSQL backup..."

# Create backup directories
for dir in "${BACKUP_DIRS[@]}"; do
    mkdir -p "$dir" 2>/dev/null || true
done

# Create temporary backup
TEMP_BACKUP="/tmp/${BACKUP_FILENAME}"

# Perform backup with custom format (supports parallel restore)
log "Creating backup of database '${DB_NAME}'..."

if PGPASSWORD="${PGPASSWORD:-}" pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --format=custom \
    --compress=9 \
    --verbose \
    -f "$TEMP_BACKUP" 2>&1 | tee -a "$LOG_FILE"; then

    BACKUP_SIZE=$(du -h "$TEMP_BACKUP" | cut -f1)
    log "Backup created successfully: ${BACKUP_SIZE}"

    # Copy to all backup destinations
    SUCCESS_COUNT=0
    FAILED_DIRS=()

    for dir in "${BACKUP_DIRS[@]}"; do
        if [ -d "$dir" ] && [ -w "$dir" ]; then
            DEST_FILE="${dir}/${BACKUP_FILENAME}"
            if cp "$TEMP_BACKUP" "$DEST_FILE"; then
                log "Copied backup to: ${DEST_FILE}"
                ((SUCCESS_COUNT++))

                # Also create a symlink to latest backup
                LATEST_LINK="${dir}/latest.dump"
                ln -sf "$BACKUP_FILENAME" "$LATEST_LINK"

                # Clean up old backups (keep last RETENTION_DAYS days)
                OLD_COUNT=$(find "$dir" -name "phonos_backup_*.dump" -mtime +${RETENTION_DAYS} 2>/dev/null | wc -l)
                if [ "$OLD_COUNT" -gt 0 ]; then
                    find "$dir" -name "phonos_backup_*.dump" -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true
                    log "Cleaned ${OLD_COUNT} old backup(s) from ${dir}"
                fi
            else
                log "ERROR: Failed to copy backup to ${dir}"
                FAILED_DIRS+=("$dir")
            fi
        else
            log "WARNING: Backup directory not available or not writable: ${dir}"
            FAILED_DIRS+=("$dir")
        fi
    done

    # Remove temp backup
    rm -f "$TEMP_BACKUP"

    if [ $SUCCESS_COUNT -eq 0 ]; then
        log "CRITICAL: Backup failed - no destinations available!"
        exit 1
    fi

    log "Backup completed successfully to ${SUCCESS_COUNT} destination(s)"

    if [ ${#FAILED_DIRS[@]} -gt 0 ]; then
        log "WARNING: Failed destinations: ${FAILED_DIRS[*]}"
    fi

else
    log "ERROR: pg_dump failed!"
    rm -f "$TEMP_BACKUP"
    exit 1
fi

# Generate backup health report
log "----------------------------------------"
log "Backup Health Report:"
for dir in "${BACKUP_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        COUNT=$(find "$dir" -name "phonos_backup_*.dump" 2>/dev/null | wc -l)
        SIZE=$(du -sh "$dir" 2>/dev/null | cut -f1 || echo "N/A")
        LATEST=$(ls -t "${dir}"/phonos_backup_*.dump 2>/dev/null | head -1 | xargs basename 2>/dev/null || echo "none")
        log "  ${dir}:"
        log "    - Backups: ${COUNT}"
        log "    - Total size: ${SIZE}"
        log "    - Latest: ${LATEST}"
    else
        log "  ${dir}: NOT AVAILABLE"
    fi
done

# Check disk space on backup destinations
log "----------------------------------------"
log "Disk Space Report:"
for dir in "${BACKUP_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        DISK_INFO=$(df -h "$dir" 2>/dev/null | tail -1)
        log "  ${dir}: ${DISK_INFO}"
    fi
done

log "=========================================="
log "Backup process complete"
