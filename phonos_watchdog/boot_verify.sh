#!/usr/bin/env bash
# Post-boot health snapshot. Run by phonos-boot-verify.service ~60s into boot
# (and can be run by hand any time). Appends a delimited report to the log.
LOG=/opt/phonos/logs/boot_verify.log
UNITS=(redis6 postgresql phonos phonos-mrs phonos-boltz2 phonos-workers \
       phonos-watchdog phonos-esmfold-re phonos-evo2 phonos-proteinmpnn-re)
{
  echo "================ BOOT VERIFY $(date -Is) ================"
  echo "kernel/uptime: $(uptime -p 2>/dev/null) | booted: $(uptime -s 2>/dev/null)"
  echo "--- systemctl is-active / is-enabled ---"
  for u in "${UNITS[@]}"; do
    printf "  %-26s active=%-10s enabled=%s\n" "$u" \
      "$(systemctl is-active "$u.service" 2>&1)" \
      "$(systemctl is-enabled "$u.service" 2>&1)"
  done
  echo "--- failed units (should be none) ---"
  systemctl --failed --no-legend 2>&1 | sed 's/^/  /'
  echo "--- worker processes ---"
  ps -eo pid,etime,cmd 2>/dev/null | grep -E "_worker\.py|boltz2_worker|deploy_new_workers" \
    | grep -v grep | sed 's/^/  /'
  echo "--- watchdog status ---"
  PYTHONPATH=/opt/phonos /usr/bin/python3.11 -m phonos_watchdog status 2>&1 | sed 's/^/  /'
  echo
} >> "$LOG" 2>&1
