#!/usr/bin/env bash
# Production launcher for the Anchor Monthly Metrics dashboard.
# Starts gunicorn bound to localhost — Cloudflare Tunnel terminates TLS
# and forwards traffic to 127.0.0.1:5050.
#
# Env vars are loaded by the systemd unit's EnvironmentFile=. We deliberately
# do NOT `source .env` here — bash would interpret any $(…), backticks, or
# unquoted whitespace in .env as commands, which has caused crash loops in
# practice. systemd's parser is stricter and safer.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

export DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-production}"
GUNICORN="$REPO_DIR/.venv/bin/gunicorn"

if [ ! -x "$GUNICORN" ]; then
  echo "ERROR: gunicorn not found at $GUNICORN. Run scripts/install.sh." >&2
  exit 1
fi

# 2 workers × 4 threads is comfortable on a Pi 4. Tune via env.
WORKERS="${GUNICORN_WORKERS:-2}"
THREADS="${GUNICORN_THREADS:-4}"
BIND="${GUNICORN_BIND:-127.0.0.1:5050}"

exec "$GUNICORN" \
  --workers "$WORKERS" \
  --threads "$THREADS" \
  --bind "$BIND" \
  --access-logfile - \
  --error-logfile - \
  wsgi:application
