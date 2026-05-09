#!/usr/bin/env bash
# Idempotent installer for the Anchor Group Monthly Metrics dashboard on a Pi.
#
# Usage:
#   sudo bash deploy/install-pi.sh
#
# What this does:
#   1. Creates the `monthly-metrics` system user
#   2. Installs the project into /opt/monthly-metrics with a Python venv
#   3. Creates /etc/monthly-metrics.env (fill it in by hand afterwards)
#   4. Initializes the SQLite database
#   5. Installs and starts the four systemd units (web + sync timer + send timer)
#
# Re-running the script is safe — it skips steps that are already done.
#
# Cloudflare Tunnel setup is NOT automated here; see deploy/cloudflared.example.yml
# for the (one-time, interactive) setup steps.

set -euo pipefail

readonly APP_USER="monthly-metrics"
readonly APP_DIR="/opt/monthly-metrics"
readonly ENV_FILE="/etc/monthly-metrics.env"
readonly REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run with sudo." >&2
  exit 1
fi

echo "── Anchor Group Monthly Metrics — Pi installer ──"

# 1. App user
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  echo "[1/5] Creating system user $APP_USER"
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
else
  echo "[1/5] User $APP_USER already exists"
fi

# 2. App directory + venv
mkdir -p "$APP_DIR"
echo "[2/5] Syncing repo to $APP_DIR"
rsync -a --delete \
  --exclude=".git" --exclude=".venv" --exclude="venv" --exclude="data" \
  "$REPO_DIR/" "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

if [[ ! -d "$APP_DIR/venv" ]]; then
  echo "      Creating venv"
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
echo "      Installing requirements"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# 3. Env file (placeholder — operator fills in)
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[3/5] Writing placeholder $ENV_FILE — edit before starting services"
  cat > "$ENV_FILE" <<'EOF'
# Anchor Group Monthly Metrics — runtime environment
# Fill in real values, then: sudo systemctl restart monthly-metrics-web

FUB_API_KEY=
ANTHROPIC_API_KEY=

# SMTP for monthly emails AND magic-link login
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
EMAIL_FROM=

# Public URL the dashboard is served at. Must include the sub-path if any.
WEB_BASE_URL=https://anchor.joelycannoli.com/metrics

# Mount point inside the URL space. MUST match the path portion of
# WEB_BASE_URL above (without the trailing slash). Leave blank to mount at /.
WEB_BASE_PATH=/metrics

# Generate with: openssl rand -hex 32
SECRET_KEY=

METRICS_DB_PATH=/opt/monthly-metrics/data/metrics.db
EOF
  chown root:"$APP_USER" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
else
  echo "[3/5] $ENV_FILE already exists — leaving in place"
fi

# 4. Initialize database
echo "[4/5] Initializing SQLite schema"
mkdir -p "$APP_DIR/data"
chown "$APP_USER:$APP_USER" "$APP_DIR/data"
sudo -u "$APP_USER" \
  "$APP_DIR/venv/bin/python" -m src.storage --init

# 5. systemd units
echo "[5/5] Installing systemd units"
install -m 0644 "$APP_DIR/deploy/monthly-metrics-web.service"   /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/monthly-metrics-sync.service"  /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/monthly-metrics-sync.timer"    /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/monthly-metrics-send.service"  /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/monthly-metrics-send.timer"    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now monthly-metrics-web.service
systemctl enable --now monthly-metrics-sync.timer
systemctl enable --now monthly-metrics-send.timer

echo
echo "Install complete. Next steps:"
echo "  1. Edit $ENV_FILE with real credentials, including a strong SECRET_KEY"
echo "     (e.g. openssl rand -hex 32) and the public WEB_BASE_URL."
echo "  2. Drop the agent roster CSV at $APP_DIR/config/agents.csv"
echo "     (columns: name,email,fub_agent_id,active)."
echo "  3. Restart the web service: sudo systemctl restart monthly-metrics-web"
echo "  4. Set up Cloudflare Tunnel — see $APP_DIR/deploy/cloudflared.example.yml"
echo
echo "Logs: journalctl -u monthly-metrics-web -f"
