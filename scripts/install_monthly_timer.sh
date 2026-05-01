#!/usr/bin/env bash
# Install the anchor-monthly systemd timer so the FUB pull + draft pipeline
# fires automatically on the 1st of each month at 09:00 local. Idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SVC_SRC="$REPO_DIR/systemd/anchor-monthly.service"
TIMER_SRC="$REPO_DIR/systemd/anchor-monthly.timer"
SVC_DST="/etc/systemd/system/anchor-monthly.service"
TIMER_DST="/etc/systemd/system/anchor-monthly.timer"
RUN_USER="${RUN_USER:-$USER}"

log() { printf "\n[timer] %s\n" "$*"; }

for f in "$SVC_SRC" "$TIMER_SRC"; do
  if [ ! -f "$f" ]; then
    echo "ERROR: $f not found." >&2
    exit 1
  fi
done

# heartbeat.sh is committed executable; no chmod needed at install time.

log "Installing $SVC_DST (running as '$RUN_USER')"
sudo install -m 0644 "$SVC_SRC" "$SVC_DST"
sudo sed -i "/^\[Service\]$/a User=$RUN_USER" "$SVC_DST"

log "Installing $TIMER_DST"
sudo install -m 0644 "$TIMER_SRC" "$TIMER_DST"

sudo systemctl daemon-reload
sudo systemctl enable --now anchor-monthly.timer

log "Timer status:"
sudo systemctl status anchor-monthly.timer --no-pager --lines=4 || true
echo
log "Next firings:"
sudo systemctl list-timers anchor-monthly.timer --no-pager || true

echo
echo "  Run now (manual):  sudo systemctl start anchor-monthly.service"
echo "  Tail journal:      sudo journalctl -u anchor-monthly -f"
echo "  Disable:           sudo systemctl disable --now anchor-monthly.timer"
