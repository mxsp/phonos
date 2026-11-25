#!/bin/bash
# Setup script to schedule the stuck jobs cleanup as a cron job
#
# This script helps you set up a cron job to automatically clean up
# stuck Redis jobs every hour.
#
# Usage: bash setup_cleanup_cron.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHONOS_DIR="${PHONOS_DIR:-$(dirname "$SCRIPT_DIR")}"
# Prefer an explicit override, then the project venv, then PATH python3.
if [ -n "${PHONOS_PYTHON:-}" ] && [ -x "${PHONOS_PYTHON}" ]; then
    VENV_PYTHON="$PHONOS_PYTHON"
elif [ -x "${PHONOS_DIR}/venv/bin/python3" ]; then
    VENV_PYTHON="${PHONOS_DIR}/venv/bin/python3"
else
    VENV_PYTHON="$(command -v python3)"
fi
CLEANUP_SCRIPT="${SCRIPT_DIR}/cleanup_stuck_jobs.py"
LOG_FILE="${SCRIPT_DIR}/logs/cleanup_stuck_jobs.log"

# Ensure log directory exists
mkdir -p "${SCRIPT_DIR}/logs"

# Define the cron job command
# Runs every hour at minute 0
CRON_SCHEDULE="0 * * * *"
CRON_COMMAND="cd ${SCRIPT_DIR} && ${VENV_PYTHON} ${CLEANUP_SCRIPT} --hours 8 >> ${LOG_FILE} 2>&1"
CRON_JOB="${CRON_SCHEDULE} ${CRON_COMMAND}"

echo "========================================"
echo "Stuck Jobs Cleanup - Cron Setup"
echo "========================================"
echo ""
echo "This will set up a cron job to run the cleanup script:"
echo "  Script:   ${CLEANUP_SCRIPT}"
echo "  Schedule: Every hour (0 * * * *)"
echo "  Timeout:  8 hours"
echo "  Log file: ${LOG_FILE}"
echo ""

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -F "${CLEANUP_SCRIPT}" > /dev/null; then
    echo "WARNING: A cron job for this script already exists:"
    echo ""
    crontab -l | grep -F "${CLEANUP_SCRIPT}"
    echo ""
    read -p "Do you want to replace it? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi

    # Remove existing cron job
    crontab -l 2>/dev/null | grep -v -F "${CLEANUP_SCRIPT}" | crontab -
    echo "Removed existing cron job."
fi

# Add the new cron job
(crontab -l 2>/dev/null; echo "${CRON_JOB}") | crontab -

echo ""
echo "✓ Cron job successfully added!"
echo ""
echo "You can verify it with: crontab -l | grep cleanup_stuck_jobs"
echo "To remove it, run: crontab -e"
echo ""
echo "The cleanup will run every hour and log to:"
echo "  ${LOG_FILE}"
echo ""
echo "To test the script manually, run:"
echo "  cd ${SCRIPT_DIR}"
echo "  ${VENV_PYTHON} cleanup_stuck_jobs.py --dry-run"
echo ""
