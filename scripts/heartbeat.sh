#!/usr/bin/env bash
# Anchor Group Monthly Metrics — HEARTBEAT
#
# Runs the monthly research + draft pipeline. Designed for cron / systemd timer.
# Does NOT send emails — admin must approve drafts via the dashboard.
#
# Suggested cron entry (09:00 on the 1st of each month):
#   0 9 1 * * /path/to/Monthly-Metrics/scripts/heartbeat.sh
#
# Required env vars (set in your crontab or systemd unit):
#   ANTHROPIC_API_KEY   — for monthly KPI research
#   FUB_API_KEY         — only if pulling from FUB live
#   ADMIN_PASSWORD      — for the dashboard login

set -euo pipefail

# Resolve the repo root from the script location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

# Load .env if it exists (admin-friendly: keep secrets out of crontab).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

mkdir -p logs

LOG_FILE="logs/heartbeat-$(date +%Y%m%d).log"
PYTHON="${PYTHON:-python3}"

{
  echo "=== HEARTBEAT $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

  echo
  echo "[1/2] Refreshing KPI registry…"
  $PYTHON main.py --mode research || {
    echo "WARN: research failed; continuing with previous thresholds.json"
  }

  echo
  echo "[2/2] Building draft emails for prior month…"
  $PYTHON main.py --mode draft

  echo
  echo "Done. Approve drafts via the dashboard:"
  echo "  $PYTHON main.py --mode dashboard"
} | tee -a "$LOG_FILE"
