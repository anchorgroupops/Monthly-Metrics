#!/usr/bin/env bash
# Anchor Group Monthly Metrics — one-shot Pi deployment.
#
# Pulls the latest code, refreshes the venv, restarts the dashboard service,
# ensures the configured hostnames are mapped to the tunnel, and runs the
# diagnostic. Idempotent — re-run anytime to bring the Pi back to "fully
# deployed" state.
#
# Usage:
#   cd /opt/Monthly-Metrics && scripts/deploy.sh
#   HOSTNAMES="metrics.joelycannoli.com anchor.joelycannoli.com" scripts/deploy.sh
#
# Prereq (one-time): scripts/install_tunnel.sh has run successfully and the
# Cloudflare browser auth completed (~/.cloudflared/cert.pem exists).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

# Default hostnames: metrics is canonical, anchor is the legacy alias that
# 301-redirects to metrics inside the app.
HOSTNAMES="${HOSTNAMES:-metrics.joelycannoli.com anchor.joelycannoli.com}"

log()  { printf "\n[deploy] %s\n" "$*"; }
warn() { printf "\n[deploy] ⚠ %s\n" "$*" >&2; }

# 1. Pull latest code -----------------------------------------------------------
log "1/5 Pulling latest code…"
git pull --ff-only

# 2. Install / refresh dashboard service (auto-bootstraps install.sh) ----------
log "2/5 Ensuring dashboard service is installed + dependencies refreshed…"
bash scripts/install_dashboard_service.sh

# 3. Restart dashboard so the new code is live --------------------------------
log "3/5 Restarting anchor-dashboard…"
sudo systemctl restart anchor-dashboard
sleep 5

# 4. Map every desired hostname to the existing tunnel -------------------------
log "4/5 Ensuring tunnel hostnames are configured…"
if [ -f /etc/cloudflared/config.yml ]; then
  for host in $HOSTNAMES; do
    log "  → $host"
    bash scripts/add_hostname.sh "$host" || \
      warn "add_hostname.sh failed for $host — see output above"
  done
else
  warn "Tunnel not installed yet — skipping hostname setup."
  warn "  Run scripts/install_tunnel.sh once (needs browser auth), then re-run deploy.sh."
fi

# 5. Diagnose ------------------------------------------------------------------
log "5/5 Running diagnostics…"
bash scripts/diagnose.sh

log "✓ Deploy complete."
echo
echo "  Test in browser: https://metrics.joelycannoli.com"
echo "  (anchor.joelycannoli.com will 301 → metrics.joelycannoli.com)"
echo
