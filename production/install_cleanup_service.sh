#!/bin/bash
# Installation script for the stuck jobs cleanup systemd service
#
# This script installs and enables the cleanup service to run automatically
# on boot and every hour thereafter.
#
# Usage: sudo bash install_cleanup_service.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="${SCRIPT_DIR}/cleanup-stuck-jobs.service"
TIMER_FILE="${SCRIPT_DIR}/cleanup-stuck-jobs.timer"
SYSTEMD_DIR="/etc/systemd/system"

echo "========================================"
echo "Stuck Jobs Cleanup - Service Installation"
echo "========================================"
echo ""

# Create log directory
echo "Creating log directory..."
mkdir -p "${SCRIPT_DIR}/logs"
chown phonos:phonos "${SCRIPT_DIR}/logs"

# Copy service files
echo "Installing systemd service and timer..."
cp "${SERVICE_FILE}" "${SYSTEMD_DIR}/cleanup-stuck-jobs.service"
cp "${TIMER_FILE}" "${SYSTEMD_DIR}/cleanup-stuck-jobs.timer"

# Reload systemd
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable the timer (this will auto-start on boot)
echo "Enabling timer to start on boot..."
systemctl enable cleanup-stuck-jobs.timer

# Start the timer now
echo "Starting timer..."
systemctl start cleanup-stuck-jobs.timer

echo ""
echo "✓ Installation complete!"
echo ""
echo "Service status:"
systemctl status cleanup-stuck-jobs.timer --no-pager || true
echo ""
echo "Next scheduled run:"
systemctl list-timers cleanup-stuck-jobs.timer --no-pager || true
echo ""
echo "Useful commands:"
echo "  - Check timer status:     systemctl status cleanup-stuck-jobs.timer"
echo "  - Check service status:   systemctl status cleanup-stuck-jobs.service"
echo "  - View logs:              journalctl -u cleanup-stuck-jobs.service -f"
echo "  - Run manually now:       systemctl start cleanup-stuck-jobs.service"
echo "  - Stop timer:             systemctl stop cleanup-stuck-jobs.timer"
echo "  - Disable on boot:        systemctl disable cleanup-stuck-jobs.timer"
echo "  - View all scheduled runs: systemctl list-timers"
echo ""
