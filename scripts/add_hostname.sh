#!/usr/bin/env bash
# Add an additional hostname to the existing Anchor Cloudflare Tunnel.
#
# Routes a new <hostname> → 127.0.0.1:5000 alongside whatever's already in
# /etc/cloudflared/config.yml. Both hostnames will hit the same dashboard.
#
# Idempotent: re-running with the same hostname is a no-op.
#
# Prereq: scripts/install_tunnel.sh has run successfully (creates the tunnel,
# writes ~/.cloudflared/cert.pem, and writes /etc/cloudflared/config.yml).
#
# Usage:
#   scripts/add_hostname.sh metrics.joelycannoli.com
#   LOCAL_SERVICE=http://127.0.0.1:5000 scripts/add_hostname.sh foo.example.com

set -euo pipefail

NEW_HOST="${1:-}"
[ -n "$NEW_HOST" ] || { echo "usage: $0 <hostname>" >&2; exit 1; }

TUNNEL_NAME="${TUNNEL_NAME:-anchor}"
LOCAL_SERVICE="${LOCAL_SERVICE:-http://127.0.0.1:5050}"
SYS_CONFIG=/etc/cloudflared/config.yml

log()  { printf "\n[hostname] %s\n" "$*"; }
fail() { printf "\n[hostname] ERROR: %s\n" "$*" >&2; exit 1; }

# Sanity checks
[ -f "$SYS_CONFIG" ] || fail "$SYS_CONFIG not found. Run scripts/install_tunnel.sh first."
command -v cloudflared >/dev/null 2>&1 || fail "cloudflared not installed."
[ -f "$HOME/.cloudflared/cert.pem" ] || \
  fail "$HOME/.cloudflared/cert.pem missing — Cloudflare auth not done. Run scripts/install_tunnel.sh."

# 1. Idempotency: skip config update if hostname already present
if sudo grep -q "hostname: $NEW_HOST" "$SYS_CONFIG"; then
  log "$NEW_HOST already in $SYS_CONFIG — skipping config update."
else
  log "Adding $NEW_HOST → $LOCAL_SERVICE to $SYS_CONFIG"
  # Insert the new ingress rule directly above the catch-all (http_status:404)
  # line so it's evaluated before the fallback.
  sudo sed -i "s|^  - service: http_status:404\$|  - hostname: $NEW_HOST\n    service: $LOCAL_SERVICE\n  - service: http_status:404|" "$SYS_CONFIG"
fi

# 2. Create CNAME via Cloudflare API (cloudflared has the auth token)
log "Routing $NEW_HOST → tunnel '$TUNNEL_NAME'…"
if ! ROUTE_OUT="$(cloudflared tunnel route dns "$TUNNEL_NAME" "$NEW_HOST" 2>&1)"; then
  if echo "$ROUTE_OUT" | grep -qiE "already exists|already configured|already in use"; then
    log "DNS route for $NEW_HOST already exists — continuing."
  else
    echo "$ROUTE_OUT" >&2
    fail "Failed to create DNS route for $NEW_HOST.
       Common causes:
         • cert.pem doesn't have access to that zone
         • a conflicting DNS record already points $NEW_HOST somewhere else
           → remove it from the Cloudflare dashboard, then re-run"
  fi
else
  echo "$ROUTE_OUT"
fi

# 3. Restart cloudflared so the new ingress rule loads
log "Restarting cloudflared…"
sudo systemctl restart cloudflared
sleep 3

# 4. Verify DNS resolves
log "Verifying DNS for $NEW_HOST…"
if getent hosts "$NEW_HOST" >/dev/null 2>&1; then
  log "✓ $NEW_HOST resolves: $(getent hosts "$NEW_HOST" | head -1 | awk '{print $1}')"
else
  log "⚠ DNS not resolving yet from this Pi — usually appears within a few seconds."
fi

log "✓ Done. $NEW_HOST is now mapped to the same dashboard as anchor.joelycannoli.com."
echo
echo "  Test:  curl -I https://$NEW_HOST/healthz"
echo
