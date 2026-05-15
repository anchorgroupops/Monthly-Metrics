#!/usr/bin/env bash
# scripts/disk_check.sh — alert when SD card usage > THRESHOLD%.
# Schedule via crontab: 0 * * * * /opt/Monthly-Metrics/scripts/disk_check.sh

set -euo pipefail

THRESHOLD="${THRESHOLD:-85}"
APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"

USED=$(df -P "$APP_DIR" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')

if [ "$USED" -ge "$THRESHOLD" ]; then
    cd "$APP_DIR"
    .venv/bin/python -c "
from src.notifier import notify_admin_failure
notify_admin_failure(
    'Anchor Group: disk usage at ${USED}%',
    'The Pi at ${APP_DIR} is at ${USED}% disk usage (threshold ${THRESHOLD}%).\n\n'
    'Run df -h on the Pi and prune old logs/backups.'
)
"
fi
