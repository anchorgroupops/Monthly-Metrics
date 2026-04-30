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
# Fails loudly on any unexpected error — does NOT mask DNS or auth failures.

set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-anchor}"
HOSTNAME="${HOSTNAME_FQDN:-anchor.joelycannoli.com}"
LOCAL_SERVICE="${LOCAL_SERVICE:-http://127.0.0.1:5000}"

log()  { printf "\n[tunnel] %s\n" "$*"; }
fail() { printf "\n[tunnel] ERROR: %s\n" "$*" >&2; exit 1; }

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
  [ -f "$HOME/.cloudflared/cert.pem" ] || \
    fail "cert.pem not found at $HOME/.cloudflared/cert.pem after login.
       The browser flow probably didn't complete. Re-run this script."
fi

# 3. Find or create the tunnel -------------------------------------------------
# Use --output json for reliable parsing (works on cloudflared >= 2022.x).
tunnel_uuid_for() {
  local name="$1"
  cloudflared tunnel list --output json 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(next((t['id'] for t in d if t.get('name')==sys.argv[1]),''))" "$name"
}

TUNNEL_UUID="$(tunnel_uuid_for "$TUNNEL_NAME")"
if [ -z "$TUNNEL_UUID" ]; then
  log "Creating tunnel '$TUNNEL_NAME'…"
  cloudflared tunnel create "$TUNNEL_NAME"
  TUNNEL_UUID="$(tunnel_uuid_for "$TUNNEL_NAME")"
  [ -n "$TUNNEL_UUID" ] || fail "Tunnel '$TUNNEL_NAME' created but UUID lookup failed."
else
  log "Tunnel '$TUNNEL_NAME' already exists ($TUNNEL_UUID)."
fi

CRED_FILE="$HOME/.cloudflared/$TUNNEL_UUID.json"
[ -f "$CRED_FILE" ] || fail "credentials file not found at $CRED_FILE"

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

# 5. DNS routing — fail LOUDLY if this doesn't work ----------------------------
log "Routing $HOSTNAME → tunnel '$TUNNEL_NAME'…"
if ! ROUTE_OUT="$(cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" 2>&1)"; then
  # Re-running on an existing route returns a specific error — that's OK.
  if echo "$ROUTE_OUT" | grep -qiE "already exists|already in use|record .* already configured"; then
    log "DNS route already exists for $HOSTNAME — continuing."
  else
    echo "$ROUTE_OUT" >&2
    fail "Failed to create DNS route for $HOSTNAME.
       Common causes:
         • cert.pem doesn't have access to the joelycannoli.com zone
           → delete ~/.cloudflared/cert.pem and re-run; pick the right zone in the browser
         • a conflicting DNS record for $HOSTNAME exists in Cloudflare
           → remove it from the Cloudflare dashboard, then re-run
         • outbound 443 to Cloudflare is blocked"
  fi
else
  echo "$ROUTE_OUT"
fi

# 6. Install (or re-sync) the systemd service ---------------------------------
# `cloudflared service install` COPIES ~/.cloudflared/config.yml and credentials
# to /etc/cloudflared/. Subsequent edits to ~/.cloudflared/config.yml do NOT
# propagate. So on every run we re-sync /etc/cloudflared with the current
# config + credentials, then restart the service.
if ! systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^cloudflared.service"; then
  log "Installing cloudflared as a systemd service…"
  sudo cloudflared --config "$CONFIG" service install
fi

# Re-sync /etc/cloudflared on every run so a re-installed tunnel actually
# takes effect. Write the system config with /etc/cloudflared paths so it
# resolves under the systemd service (which doesn't use the user's $HOME).
log "Re-syncing /etc/cloudflared with current config + credentials…"
sudo mkdir -p /etc/cloudflared
SYS_CRED="/etc/cloudflared/$(basename "$CRED_FILE")"
sudo install -m 0600 "$CRED_FILE" "$SYS_CRED"
sudo tee /etc/cloudflared/config.yml >/dev/null <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $SYS_CRED

ingress:
  - hostname: $HOSTNAME
    service: $LOCAL_SERVICE
  - service: http_status:404
EOF

sudo systemctl enable cloudflared
sudo systemctl restart cloudflared
sleep 3
sudo systemctl status cloudflared --no-pager --lines=8 || true

# 7. Verify the tunnel is actually up ------------------------------------------
log "Verifying tunnel state…"
TUNNEL_INFO="$(cloudflared tunnel info --output json "$TUNNEL_NAME" 2>/dev/null || echo '{}')"
CONN_COUNT="$(echo "$TUNNEL_INFO" | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('conns',[])))" 2>/dev/null || echo 0)"

if [ "$CONN_COUNT" = "0" ]; then
  log "⚠ Tunnel has 0 active connections to Cloudflare yet."
  log "  Wait 10–30 seconds and run: cloudflared tunnel info $TUNNEL_NAME"
else
  log "✓ Tunnel has $CONN_COUNT active connection(s) to Cloudflare."
fi

# 8. Verify DNS resolves -------------------------------------------------------
log "Verifying DNS for $HOSTNAME…"
if getent hosts "$HOSTNAME" >/dev/null 2>&1; then
  log "✓ DNS resolves: $(getent hosts "$HOSTNAME" | head -1)"
else
  log "⚠ DNS for $HOSTNAME does not resolve yet from this Pi."
  log "  Cloudflare-proxied records usually appear within a few seconds."
  log "  If still failing after 1 minute, check the Cloudflare DNS dashboard for $HOSTNAME."
fi

# 9. Done ----------------------------------------------------------------------
log "✓ Tunnel install complete."
echo
echo "  Hostname:    https://$HOSTNAME"
echo "  Tunnel:      $TUNNEL_NAME ($TUNNEL_UUID)"
echo "  Forwards to: $LOCAL_SERVICE"
echo
echo "  Test:      curl -I https://$HOSTNAME/healthz"
echo "  Logs:      sudo journalctl -u cloudflared -f"
echo "  Diagnose:  scripts/diagnose.sh"
