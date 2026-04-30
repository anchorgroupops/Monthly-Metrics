#!/usr/bin/env bash
# Anchor Group Monthly Metrics — one-shot installer.
#
# Idempotent. Safe to re-run any time. Designed for Raspberry Pi OS or
# any Debian/Ubuntu host. Run from anywhere — it cd's into the repo root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

log() { printf "\n[install] %s\n" "$*"; }

# 1. System packages -----------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import venv" 2>/dev/null; then
  log "Installing python3 + venv via apt…"
  sudo apt-get update -qq
  sudo apt-get install -y python3 python3-venv python3-pip
fi

# 2. Virtualenv ----------------------------------------------------------------
if [ ! -d .venv ]; then
  log "Creating .venv/"
  python3 -m venv .venv
fi

log "Installing Python dependencies…"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# 3. .env scaffold -------------------------------------------------------------
if [ ! -f .env ]; then
  log "Copying .env.example → .env"
  cp .env.example .env
fi

# Generate FLASK_SECRET_KEY if blank
if grep -qE '^FLASK_SECRET_KEY=$' .env; then
  KEY=$(.venv/bin/python -c "import secrets; print(secrets.token_hex(32))")
  # Use a different sed delimiter to avoid escaping issues.
  sed -i.bak "s|^FLASK_SECRET_KEY=$|FLASK_SECRET_KEY=$KEY|" .env && rm -f .env.bak
  log "Generated FLASK_SECRET_KEY."
fi

# Generate ADMIN_PASSWORD if blank, and print it once
if grep -qE '^ADMIN_PASSWORD=$' .env; then
  PW=$(.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(24))")
  sed -i.bak "s|^ADMIN_PASSWORD=$|ADMIN_PASSWORD=$PW|" .env && rm -f .env.bak
  log "Generated ADMIN_PASSWORD: $PW"
  log "  ↑ save this — you'll use it to log into the dashboard."
fi

chmod 600 .env

# 4. Runtime dirs --------------------------------------------------------------
mkdir -p logs data
chmod +x scripts/heartbeat.sh

# 5. Sanity test ---------------------------------------------------------------
log "Running pytest…"
.venv/bin/python -m pytest tests/ -q

# 6. Status report -------------------------------------------------------------
log "✓ Install complete."
echo
echo "  Repo:   $REPO_DIR"
echo "  Python: $REPO_DIR/.venv/bin/python"
echo "  Env:    $REPO_DIR/.env  (chmod 600)"
echo

MISSING=()
for var in ANTHROPIC_API_KEY SMTP_USER SMTP_PASSWORD EMAIL_FROM; do
  if grep -qE "^${var}=$" .env; then
    MISSING+=("$var")
  fi
done

if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "  ⚠ Missing values in .env (edit before running heartbeat):"
  for v in "${MISSING[@]}"; do echo "      - $v"; done
  echo
  echo "  Then run:  scripts/install_cron.sh"
else
  echo "  All required secrets are set. Next:  scripts/install_cron.sh"
fi
