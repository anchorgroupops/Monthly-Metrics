#!/usr/bin/env bash
# Anchor Group Monthly Metrics — Cloudflare Tunnel installer.
#
# Publishes the dashboard at https://anchor.joelycannoli.com via Cloudflare
# Tunnel (no port-forwarding, automatic TLS at the Cloudflare edge).
#
# Prerequisites:
#   - The Pi has internet access (outbound 443 to Cloudflare).
#   - joelycannoli.com is on Cloudflare DNS.
#   - You can briefly open a URL in a browser to authorize the tunnel.
#
# Idempotent: safe to re-run. Skips steps that are already complete.

set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-anchor}"
HOSTNAME="${HOSTNAME_FQDN:-anchor.joelycannoli.com}"
LOCAL_SERVICE="${LOCAL_SERVICE:-http://127.0.0.1:5000}"

log() { printf "\n[tunnel] %s\n" "$*"; }

# 1. Install cloudflared --------------------------------------------------------
if ! command -v cloudflared >/dev/null 2>&1; then
  log "Installing cloudflared from Cloudflare's apt repo…"
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | \
    sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | \
    sudo tee /etc/apt/sources.list.d/cloudflared.list
  sudo apt-get update -qq
  sudo apt-get install -y cloudflared
fi

cloudflared --version

# 2. One-time login (interactive, opens a CF URL in a browser) -----------------
if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  log "Cloudflare login required (one-time)."
  echo "  cloudflared will print a URL. Open it in a browser, sign in, and"
  echo "  authorize the joelycannoli.com zone. Then come back here."
  echo
  cloudflared tunnel login
fi

# 3. Create the tunnel ----------------------------------------------------------
TUNNEL_UUID="$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2 == n {print $1}')"
if [ -z "$TUNNEL_UUID" ]; then
  log "Creating tunnel '$TUNNEL_NAME'…"
  cloudflared tunnel create "$TUNNEL_NAME"
  TUNNEL_UUID="$(cloudflared tunnel list | awk -v n="$TUNNEL_NAME" '$2 == n {print $1}')"
else
  log "Tunnel '$TUNNEL_NAME' already exists ($TUNNEL_UUID)."
fi

CRED_FILE="$HOME/.cloudflared/$TUNNEL_UUID.json"
if [ ! -f "$CRED_FILE" ]; then
  echo "ERROR: credentials file not found at $CRED_FILE" >&2
  exit 1
fi

# 4. Write the tunnel config ----------------------------------------------------
CONFIG="$HOME/.cloudflared/config.yml"
log "Writing $CONFIG"
cat > "$CONFIG" <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $CRED_FILE

ingress:
  - hostname: $HOSTNAME
    service: $LOCAL_SERVICE
  - service: http_status:404
EOF

# 5. DNS routing (creates the CNAME automatically via the CF API) --------------
log "Routing $HOSTNAME → tunnel '$TUNNEL_NAME'…"
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" || \
  log "DNS route may already exist; continuing."

# 6. Install as a systemd service ----------------------------------------------
if ! systemctl list-unit-files --no-legend | grep -q "^cloudflared.service"; then
  log "Installing cloudflared as a systemd service…"
  sudo cloudflared --config "$CONFIG" service install
fi

sudo systemctl enable --now cloudflared
sleep 2
sudo systemctl status cloudflared --no-pager --lines=5 || true

# 7. Done ----------------------------------------------------------------------
log "✓ Tunnel install complete."
echo
echo "  Hostname: https://$HOSTNAME"
echo "  Tunnel:   $TUNNEL_NAME ($TUNNEL_UUID)"
echo "  Forwards to: $LOCAL_SERVICE"
echo
echo "  Test:  curl -I https://$HOSTNAME/healthz"
echo "  Logs:  sudo journalctl -u cloudflared -f"
