#!/usr/bin/env bash
# scripts/deploy.sh — pull main, install deps, migrate DB, restart, diagnose.
# Run from any host that has SSH access to pi@raspberrypi.

set -euo pipefail

PI_HOST="${PI_HOST:-pi@raspberrypi}"
APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"

echo "→ Deploying to ${PI_HOST}:${APP_DIR}"

ssh "$PI_HOST" bash -c "'
  set -euo pipefail
  cd $APP_DIR
  git fetch origin
  git checkout main
  git reset --hard origin/main
  .venv/bin/pip install --quiet -r requirements.txt
  .venv/bin/python main.py --mode migrate
  sudo systemctl restart anchor-dashboard
  scripts/diagnose.sh
'"

echo "→ Deploy complete."
