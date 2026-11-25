# Stuck Redis Jobs Cleanup Script

This script automatically identifies and removes stuck jobs from the Redis `ACTIVE_JOBS_HASH` that have been running for more than 8 hours without completing.

## Files

- `cleanup_stuck_jobs.py` - Main cleanup script
- `cleanup-stuck-jobs.service` - Systemd service definition
- `cleanup-stuck-jobs.timer` - Systemd timer for periodic execution
- `install_cleanup_service.sh` - Script to install and enable systemd service
- `setup_cleanup_cron.sh` - Alternative: Helper script to set up automated cleanup via cron
- `logs/cleanup_stuck_jobs.log` - Log file for automated runs (created automatically)

## Quick Start

### Manual Cleanup (Dry Run)

To see what would be deleted without actually deleting:

```bash
cd /opt/phonos/production
/opt/phonos/venv/bin/python3 cleanup_stuck_jobs.py --dry-run
```

### Manual Cleanup (Actually Delete)

To delete stuck jobs older than 8 hours:

```bash
cd /opt/phonos/production
/opt/phonos/venv/bin/python3 cleanup_stuck_jobs.py --hours 8
```

### Automated Cleanup (Systemd Service - Recommended)

**This is the recommended method for production systems.**

To install and enable automatic cleanup that runs:
- Every hour
- 5 minutes after boot
- Automatically on system restart

```bash
cd /opt/phonos/production
sudo bash install_cleanup_service.sh
```

The service will:
- ✓ Start automatically on boot
- ✓ Run every hour
- ✓ Persist across reboots
- ✓ Log to both systemd journal and log file
- ✓ Catch missed runs if the system was down

### Alternative: Automated Cleanup (Cron Job)

If you prefer cron instead of systemd:

```bash
cd /opt/phonos/production
bash setup_cleanup_cron.sh
```

This will create a cron job that runs every hour and logs output to `logs/cleanup_stuck_jobs.log`.

## Usage Options

```
cleanup_stuck_jobs.py [-h] [--dry-run] [--hours HOURS] [--json]

Options:
  -h, --help     Show help message
  --dry-run      Show what would be deleted without actually deleting
  --hours HOURS  Number of hours after which a job is considered stuck (default: 8)
  --json         Output results in JSON format
```

## Examples

### Check for jobs stuck longer than 4 hours (dry run)
```bash
/opt/phonos/venv/bin/python3 cleanup_stuck_jobs.py --dry-run --hours 4
```

### Delete jobs stuck longer than 12 hours
```bash
/opt/phonos/venv/bin/python3 cleanup_stuck_jobs.py --hours 12
```

### Get JSON output for monitoring/automation
```bash
/opt/phonos/venv/bin/python3 cleanup_stuck_jobs.py --json --hours 8
```

## How It Works

1. **Connects to Redis** using the same configuration as your production system
2. **Scans ACTIVE_JOBS_HASH** to find all currently active jobs
3. **Checks start_time** for each job and calculates elapsed time
4. **Identifies stuck jobs** that have been running longer than the timeout threshold
5. **Deletes stuck jobs** from the Redis hash (or reports them in dry-run mode)
6. **Logs details** about deleted jobs including job ID, model type, and runtime

## Output

The script provides a summary showing:
- Total active jobs in Redis
- Number of stuck jobs found
- Number of jobs deleted (or would be deleted in dry-run)
- Detailed information about each stuck job:
  - Job ID
  - Model type
  - Current status
  - How long it's been running
  - Start timestamp

Example output:
```
============================================================
CLEANUP SUMMARY
============================================================
Total active jobs:     10
Stuck jobs found:      2
Jobs deleted:          2
Errors encountered:    0
============================================================

Stuck jobs details:
  - Job ID: 091cddab-ad32-4191-a80d-b43e6180bc90
    Model: type-7, Status: dispatched
    Running for: 9.45 hours
    Started at: 2025-11-30T11:00:00+00:00

  - Job ID: 952f2426-40c1-49d5-bf83-881145dfaa53
    Model: type-5, Status: dispatched
    Running for: 10.22 hours
    Started at: 2025-11-30T10:30:00+00:00
```

## Monitoring

### For Systemd Service

**Check timer status:**
```bash
systemctl status cleanup-stuck-jobs.timer
```

**Check when next run is scheduled:**
```bash
systemctl list-timers cleanup-stuck-jobs.timer
```

**View service execution logs (live):**
```bash
journalctl -u cleanup-stuck-jobs.service -f
```

**View recent service logs:**
```bash
journalctl -u cleanup-stuck-jobs.service -n 100
```

**Run cleanup manually now:**
```bash
sudo systemctl start cleanup-stuck-jobs.service
```

**Stop automatic cleanup:**
```bash
sudo systemctl stop cleanup-stuck-jobs.timer
```

**Disable autostart on boot:**
```bash
sudo systemctl disable cleanup-stuck-jobs.timer
```

**Re-enable autostart:**
```bash
sudo systemctl enable cleanup-stuck-jobs.timer
sudo systemctl start cleanup-stuck-jobs.timer
```

### For Cron Job

**Check cron job status:**
```bash
crontab -l | grep cleanup_stuck_jobs
```

**Remove cron job:**
```bash
crontab -e
# Then delete the line containing cleanup_stuck_jobs.py
```

### General Monitoring

**View cleanup logs (file):**
```bash
tail -f /opt/phonos/production/logs/cleanup_stuck_jobs.log
```

## Safety Features

- **Dry-run mode** - Test before making changes
- **Detailed logging** - All deletions are logged with timestamps
- **Error handling** - Gracefully handles Redis connection issues and malformed data
- **Configurable timeout** - Adjust the threshold based on your needs
- **Non-destructive** - Only removes entries from ACTIVE_JOBS_HASH, doesn't affect job results or database

## Integration with Existing System

This script integrates with your existing job monitoring system:

- Uses the same Redis connection configuration from `config.py`
- Reads from the same `ACTIVE_JOBS_HASH` used by the MRS (Management & Routing Service)
- Complements the existing `monitor_jobs()` function which has a 1-hour timeout
- Provides a manual/scheduled cleanup for jobs that somehow escape the 1-hour monitor

## Troubleshooting

### Redis connection errors
Ensure Redis is running and accessible with the configured credentials in `config.py`

### Permission errors
Make sure the script is executable: `chmod +x cleanup_stuck_jobs.py`

### Module not found errors
Use the virtual environment Python: `/opt/phonos/venv/bin/python3`

### Systemd service not running
Check timer status: `systemctl status cleanup-stuck-jobs.timer`
Check service status: `systemctl status cleanup-stuck-jobs.service`
View service logs: `journalctl -u cleanup-stuck-jobs.service -n 50`
Verify timer is enabled: `systemctl is-enabled cleanup-stuck-jobs.timer`

### Cron job not running
Check cron service: `systemctl status cron` or `service cron status`
Check syslog for cron errors: `grep CRON /var/log/syslog`

### Service installed but not running on boot
Ensure timer is enabled: `sudo systemctl enable cleanup-stuck-jobs.timer`
Check for systemd errors: `journalctl -xe`
