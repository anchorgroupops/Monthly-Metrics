#!/usr/bin/env bash
# One-shot Pi installer.
#
# Run on the Pi as a single line:
#
#   curl -fsSL https://raw.githubusercontent.com/anchorgroupops/Monthly-Metrics/main/deploy/bootstrap.sh | sudo bash
#
# Or, before the branch is merged to main:
#
#   curl -fsSL https://raw.githubusercontent.com/anchorgroupops/Monthly-Metrics/claude/agent-dashboard-metrics-q9fTw/deploy/bootstrap.sh | sudo BRANCH=claude/agent-dashboard-metrics-q9fTw bash
#
# What it does:
#   1. apt-installs git + python3-venv if missing
#   2. Clones (or pulls) the repo into /opt/monthly-metrics-src
#   3. Runs deploy/install-pi.sh from that checkout
# After it finishes, edit /etc/monthly-metrics.env and drop in your roster CSV
# at /opt/monthly-metrics/config/agents.csv.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/anchorgroupops/Monthly-Metrics.git}"
BRANCH="${BRANCH:-main}"
SRC_DIR="${SRC_DIR:-/opt/monthly-metrics-src}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run with sudo." >&2
  exit 1
fi

echo "── Anchor Group Monthly Metrics — bootstrap ──"

if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  echo "[boot 1/3] Installing git, python3-venv, rsync"
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv python3-pip rsync
fi

if [[ -d "$SRC_DIR/.git" ]]; then
  echo "[boot 2/3] Updating existing checkout at $SRC_DIR"
  git -C "$SRC_DIR" fetch --quiet origin "$BRANCH"
  git -C "$SRC_DIR" checkout --quiet "$BRANCH"
  git -C "$SRC_DIR" reset --hard --quiet "origin/$BRANCH"
else
  echo "[boot 2/3] Cloning $REPO_URL ($BRANCH) → $SRC_DIR"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
fi

echo "[boot 3/3] Running deploy/install-pi.sh"
bash "$SRC_DIR/deploy/install-pi.sh"
