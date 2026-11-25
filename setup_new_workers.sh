#!/bin/bash
"""
Setup script for new workers
Installs services, creates configurations, and sets up monitoring
"""

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Portable: PHONOS_DIR defaults to this script's location and the service user
# defaults to whoever invoked the script (the real user even under sudo).
PHONOS_DIR="${PHONOS_DIR:-$SCRIPT_DIR}"
PROD_DIR="$PHONOS_DIR/production"
USER="${PHONOS_USER:-${SUDO_USER:-$(id -un)}}"
GROUP="$(id -gn "$USER" 2>/dev/null || echo "$USER")"

echo "🚀 Setting up new NVIDIA API workers..."
echo "======================================"

# Check dependencies
echo "📦 Checking dependencies..."
missing_deps=()

# Check Python packages
if ! python3 -c "import redis" 2>/dev/null; then
    missing_deps+=("python3-redis")
fi

if ! python3 -c "import requests" 2>/dev/null; then
    missing_deps+=("python3-requests")
fi

if [ ${#missing_deps[@]} -gt 0 ]; then
    echo "⚠ Missing dependencies: ${missing_deps[*]}"
    echo "Installing with pip3..."
    
    # Install system packages
    sudo apt update
    sudo apt install -y python3-pip python3-redis python3-requests
else
    echo "✓ All Python dependencies satisfied"
fi

# Check NVIDIA API key
if [ -z "$NVIDIA_API_KEY" ]; then
    if [ -f "$PHONOS_DIR/.env" ]; then
        source "$PHONOS_DIR/.env"
    fi
    
    if [ -z "$NVIDIA_API_KEY" ]; then
        echo "⚠ NVIDIA_API_KEY not set!"
        echo "Please set it in ~/.bashrc or $PHONOS_DIR/.env"
        echo "Example: export NVIDIA_API_KEY='nvapi-your-key-here'"
        exit 1
    fi
else
    echo "✓ NVIDIA API key found"
fi

# Create necessary directories
echo "📁 Creating directories..."
mkdir -p "$PROD_DIR/logs"
mkdir -p "$PHONOS_DIR/logs"
mkdir -p "$PHONOS_DIR/backups"
mkdir -p "$PHONOS_DIR/data"

# Set permissions
chown -R "$USER:$GROUP" "$PHONOS_DIR"
chmod -R 755 "$PROD_DIR"

# Install systemd service, rendering the template for this host (user + paths).
echo "🔧 Installing systemd service..."
PYTHON_BIN="${PHONOS_PYTHON:-$(command -v python3.11 || command -v python3)}"
sudo sed \
    -e "s|__PHONOS_USER__|$USER|g" \
    -e "s|__PHONOS_GROUP__|$GROUP|g" \
    -e "s|__PHONOS_DIR__|$PHONOS_DIR|g" \
    -e "s|__PHONOS_PYTHON__|$PYTHON_BIN|g" \
    "$PHONOS_DIR/phonos-new-workers.service" \
    | sudo tee /etc/systemd/system/phonos-new-workers.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable phonos-new-workers.service

# Create log rotation config
echo "📋 Setting up log rotation..."
sudo tee /etc/logrotate.d/phonos-workers > /dev/null <<EOF
$PROD_DIR/logs/*_worker*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    create 644 $USER $USER
    postrotate
        systemctl reload phonos-new-workers
}
EOF

# Create monitoring script (self-locating so it stays portable)
echo "📊 Creating monitoring script..."
cat > "$PHONOS_DIR/monitor_workers.sh" << 'EOF'
#!/bin/bash
MONITOR_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/production"
STATUS_FILE="$MONITOR_DIR/new_workers_status.json"
ALERT_THRESHOLD=3  # Alert after 3 restarts

check_worker_health() {
    if [ ! -f "$STATUS_FILE" ]; then
        return
    fi
    
    local timestamp=\$(jq -r '.timestamp' "$STATUS_FILE" 2>/dev/null || echo "0")
    local current_time=\$(date +%s)
    local age=$((current_time - timestamp))
    
    # Check status file age (alert if > 5 minutes old)
    if [ $age -gt 300 ]; then
        echo "⚠ Worker status file is stale ($age seconds old)"
        return 1
    fi
    
    # Check for excessive restarts
    jq -r '.workers[] | select(.restarts > '$ALERT_THRESHOLD') | .name' "$STATUS_FILE" 2>/dev/null | while read worker; do
        echo "🚨 ALERT: $worker has restarted > $ALERT_THRESHOLD times"
        # Send alert (you can integrate email, Slack, etc.)
    done
}

# Run health check
check_worker_health
EOF

chmod +x "$PHONOS_DIR/monitor_workers.sh"

# Create cron job for monitoring
echo "⏰ Setting up monitoring cron job..."
(crontab -l 2>/dev/null; echo "") | grep -v "monitor_workers" | crontab -
echo "*/5 * * * * $USER $PHONOS_DIR/monitor_workers.sh" | crontab -

echo "✓ Monitoring cron job added (checks every 5 minutes)"

# Update PATH for workers
echo "🛠 Updating system PATH..."
USER_HOME="$(eval echo "~$USER")"
if [ -f "$USER_HOME/.bashrc" ] && ! grep -q "$PROD_DIR" "$USER_HOME/.bashrc"; then
    echo "export PATH=\"\$PATH:$PROD_DIR\"" >> "$USER_HOME/.bashrc"
    echo "✓ Added production directory to PATH"
fi

# Create quick access scripts
echo "⚡ Creating convenience scripts..."

# Start workers script
cat > "$PHONOS_DIR/start_workers.sh" << 'EOF'
#!/bin/bash
echo "🚀 Starting new workers..."
cd "$(dirname "$(readlink -f "$0")")"
python3 scripts/utils/deploy_new_workers.py start "$@"
EOF

# Stop workers script
cat > "$PHONOS_DIR/stop_workers.sh" << 'EOF'
#!/bin/bash
echo "🛑 Stopping new workers..."
cd "$(dirname "$(readlink -f "$0")")"
python3 scripts/utils/deploy_new_workers.py stop "$@"
EOF

# Status script
cat > "$PHONOS_DIR/worker_status.sh" << 'EOF'
#!/bin/bash
echo "📊 Worker status..."
cd "$(dirname "$(readlink -f "$0")")"
python3 scripts/utils/deploy_new_workers.py status "$@"
EOF

# Restart script
cat > "$PHONOS_DIR/restart_workers.sh" << 'EOF'
#!/bin/bash
echo "🔄 Restarting workers..."
cd "$(dirname "$(readlink -f "$0")")"
python3 scripts/utils/deploy_new_workers.py stop
sleep 5
python3 scripts/utils/deploy_new_workers.py deploy "$@"
EOF

chmod +x "$PHONOS_DIR"/*.sh

echo "✓ Created convenience scripts:"
echo "  start_workers.sh - Start workers"
echo "  stop_workers.sh  - Stop workers" 
echo "  restart_workers.sh - Restart workers"
echo "  worker_status.sh - Show status"

echo ""
echo "🎯 Setup complete!"
echo "======================================"
echo "Available commands:"
echo "  sudo systemctl start phonos-new-workers    # Start service"
echo "  sudo systemctl stop phonos-new-workers     # Stop service"
echo "  sudo systemctl status phonos-new-workers  # Check service status"
echo "  sudo journalctl -u phonos-new-workers -f  # View logs"
echo ""
echo "Or use scripts directly:"
echo "  ~/phonos/start_workers.sh                 # Start manually"
echo "  ~/phonos/stop_workers.sh                  # Stop manually"
echo "  ~/phonos/worker_status.sh                # Check status"
echo "  ~/phonos/restart_workers.sh               # Restart manually"
echo ""
echo "🔧 Configuration files in: $PROD_DIR"
echo "📊 Logs in: $PROD_DIR/logs"
echo "📈 Monitoring active: cron job every 5 minutes"
