#!/usr/bin/env bash
# Install the Anchor Dashboard systemd service so gunicorn runs 24/7.
# Idempotent: safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
UNIT_SRC="$REPO_DIR/systemd/anchor-dashboard.service"
UNIT_DST="/etc/systemd/system/anchor-dashboard.service"
RUN_USER="${RUN_USER:-$USER}"

log() { printf "\n[svc] %s\n" "$*"; }

if [ ! -f "$UNIT_SRC" ]; then
  echo "ERROR: $UNIT_SRC not found." >&2
  exit 1
fi

if [ ! -x "$REPO_DIR/scripts/serve.sh" ]; then
  echo "ERROR: scripts/serve.sh not executable. Run scripts/install.sh first." >&2
  exit 1
fi

if [ ! -x "$REPO_DIR/.venv/bin/gunicorn" ]; then
  echo "ERROR: gunicorn not installed. Run scripts/install.sh first." >&2
  exit 1
fi

log "Installing $UNIT_DST (running as user '$RUN_USER')"
sudo install -m 0644 "$UNIT_SRC" "$UNIT_DST"
# Inject the User= line — the source unit deliberately omits it.
sudo sed -i "/^\[Service\]$/a User=$RUN_USER" "$UNIT_DST"

sudo systemctl daemon-reload
sudo systemctl enable --now anchor-dashboard

sleep 2
sudo systemctl status anchor-dashboard --no-pager --lines=10 || true

log "✓ Dashboard service installed."
echo
echo "  Status:   sudo systemctl status anchor-dashboard"
echo "  Restart:  sudo systemctl restart anchor-dashboard"
echo "  Logs:     sudo journalctl -u anchor-dashboard -f"
