#!/usr/bin/env bash
# scripts/healthz_check.sh — alert if public dashboard is unreachable.
# Schedule via crontab on the Pi: 0 */6 * * * /opt/Monthly-Metrics/scripts/healthz_check.sh

set -euo pipefail

URL="${URL:-https://metrics.joelycannoli.com/healthz}"
APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"

if ! curl -fsS --max-time 15 "$URL" >/dev/null; then
    cd "$APP_DIR"
    .venv/bin/python -c "
from src.notifier import notify_admin_failure
notify_admin_failure(
    'Anchor Group: healthz unreachable',
    'The dashboard at ${URL} did not respond within 15s. Check the Pi:\n\n'
    '  sudo systemctl status anchor-dashboard cloudflared\n'
    '  scripts/diagnose.sh'
)
"
fi
