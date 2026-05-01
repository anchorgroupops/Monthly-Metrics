#!/usr/bin/env bash
# Anchor Group Monthly Metrics — HEARTBEAT
#
# Runs the monthly FUB pull → research → draft pipeline. Designed for the
# anchor-monthly.timer systemd unit, but safe to run manually any time.
# Does NOT send emails — admin must approve drafts via the dashboard.
#
# Failure handling
# ----------------
# Pull and draft failures alert the admin via SMTP and exit non-zero so
# systemd marks the unit failed (and the dashboard's pull-status badge shows
# the error). Research failures are logged but non-fatal — the previous
# thresholds.json is still useful.
#
# Required env vars (loaded from /opt/Monthly-Metrics/.env via systemd):
#   ANTHROPIC_API_KEY   — for monthly KPI research
#   FUB_API_KEY         — required for the pull step
#   ADMIN_EMAIL         — where alerts go (falls back to EMAIL_FROM)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

# Load .env when running by hand. systemd already loads it via EnvironmentFile=
# but this keeps manual `bash scripts/heartbeat.sh` invocations working too.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

mkdir -p logs
LOG_FILE="logs/heartbeat-$(date +%Y%m%d).log"
PYTHON="${PYTHON:-${REPO_DIR}/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

# Tee everything from here on into the log file.
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=== HEARTBEAT $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Send admin alert. Subject via env var (avoids quoting hell), body via stdin.
alert() {
  ALERT_SUBJECT="$1" "$PYTHON" - <<'PYEOF' || true
import os, sys
from src.notifier import notify_admin_failure
notify_admin_failure(os.environ["ALERT_SUBJECT"], sys.stdin.read())
PYEOF
}

echo
echo "[1/3] Pulling FUB metrics…"
if ! "$PYTHON" main.py --mode pull; then
  echo "ERROR: pull step failed."
  tail -80 "$LOG_FILE" | alert "Anchor heartbeat: FUB pull failed"
  exit 1
fi

echo
echo "[2/3] Refreshing KPI thresholds…"
if ! "$PYTHON" main.py --mode research; then
  echo "WARN: research step failed; continuing with previous thresholds.json."
fi

echo
echo "[3/3] Building draft emails…"
if ! "$PYTHON" main.py --mode draft; then
  echo "ERROR: draft step failed."
  tail -80 "$LOG_FILE" | alert "Anchor heartbeat: draft step failed"
  exit 1
fi

echo
echo "Done. Approve drafts via the dashboard:"
echo "  https://anchor.joelycannoli.com/"
