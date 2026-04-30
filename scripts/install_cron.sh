#!/usr/bin/env bash
# Install a cron entry that runs heartbeat.sh at 09:00 on the 1st of each month.
# Idempotent: safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEARTBEAT="$SCRIPT_DIR/heartbeat.sh"

if [ ! -x "$HEARTBEAT" ]; then
  echo "ERROR: $HEARTBEAT is not executable. Run scripts/install.sh first." >&2
  exit 1
fi

CRONLINE="0 9 1 * * $HEARTBEAT"

if crontab -l 2>/dev/null | grep -Fq "$HEARTBEAT"; then
  echo "Cron entry already present:"
  crontab -l | grep -F "$HEARTBEAT"
  exit 0
fi

(crontab -l 2>/dev/null; echo "$CRONLINE") | crontab -

echo "Installed cron entry:"
echo "  $CRONLINE"
echo
echo "Next scheduled run: 09:00 on the 1st of next month."
echo "View entries:  crontab -l"
echo "Remove entry:  crontab -e   (delete the line manually)"
