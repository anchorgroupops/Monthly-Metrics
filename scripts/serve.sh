#!/usr/bin/env bash
# Production launcher for the Anchor Monthly Metrics dashboard.
# Starts gunicorn bound to localhost — Cloudflare Tunnel terminates TLS
# and forwards traffic to 127.0.0.1:5000.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

# Load .env if it exists (Pi-friendly: keeps secrets out of systemd units).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

export DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-production}"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
GUNICORN="$REPO_DIR/.venv/bin/gunicorn"

if [ ! -x "$GUNICORN" ]; then
  echo "ERROR: gunicorn not found at $GUNICORN. Run scripts/install.sh." >&2
  exit 1
fi

# 2 workers × 4 threads is comfortable on a Pi 4. Tune via env.
WORKERS="${GUNICORN_WORKERS:-2}"
THREADS="${GUNICORN_THREADS:-4}"
BIND="${GUNICORN_BIND:-127.0.0.1:5000}"

exec "$GUNICORN" \
  --workers "$WORKERS" \
  --threads "$THREADS" \
  --bind "$BIND" \
  --access-logfile - \
  --error-logfile - \
  wsgi:application
