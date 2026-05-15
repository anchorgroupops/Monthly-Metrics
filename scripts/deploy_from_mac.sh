#!/usr/bin/env bash
# Deploy Monthly-Metrics to the Pi from the Mac via Tailscale SSH.
# Usage: ./scripts/deploy_from_mac.sh
set -euo pipefail

PI_HOST="${PI_HOST:-100.109.22.118}"
PI_USER="${PI_USER:-joelycannoli}"
REMOTE_DIR="/opt/Monthly-Metrics"
REPO_URL="https://github.com/anchorgroupops/Monthly-Metrics.git"
HOSTNAME_FQDN="metrics.joelycannoli.com"

echo "=== Deploying Monthly-Metrics to Pi ($PI_HOST) ==="

# 1. Clone or pull the repo on the Pi
ssh "$PI_USER@$PI_HOST" bash -s <<'REMOTE'
set -euo pipefail
REMOTE_DIR="/opt/Monthly-Metrics"
REPO_URL="https://github.com/anchorgroupops/Monthly-Metrics.git"

if [ -d "$REMOTE_DIR/.git" ]; then
    echo "Repo exists, pulling latest..."
    cd "$REMOTE_DIR" && git pull origin main
else
    echo "Cloning repo..."
    sudo mkdir -p "$REMOTE_DIR"
    sudo chown "$USER:$USER" "$REMOTE_DIR"
    git clone "$REPO_URL" "$REMOTE_DIR"
fi

# 2. Run install script
cd "$REMOTE_DIR"
chmod +x scripts/install.sh
./scripts/install.sh

# 3. Install systemd units
chmod +x scripts/install_monthly_timer.sh scripts/install_dashboard_service.sh
./scripts/install_monthly_timer.sh
./scripts/install_dashboard_service.sh

# 4. Install Cloudflare tunnel (if not already set up)
if [ -f scripts/install_tunnel.sh ]; then
    chmod +x scripts/install_tunnel.sh
    ./scripts/install_tunnel.sh
fi

# 5. Start the dashboard
sudo systemctl daemon-reload
sudo systemctl enable anchor-dashboard.service
sudo systemctl restart anchor-dashboard.service
sudo systemctl enable anchor-monthly.timer
sudo systemctl start anchor-monthly.timer

echo ""
echo "=== Deployment complete ==="
echo "Dashboard: https://metrics.joelycannoli.com"
echo "Healthz:   https://metrics.joelycannoli.com/healthz"
echo ""
echo "Run 'scripts/diagnose.sh' on the Pi to verify everything."
REMOTE

echo "Done. Dashboard should be live at https://$HOSTNAME_FQDN"
