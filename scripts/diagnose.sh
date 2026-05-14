#!/usr/bin/env bash
# Anchor Group Monthly Metrics — deployment diagnostic.
#
# Runs every check that matters for the Pi → Cloudflare → browser path and
# prints a green/red status line for each. Safe to run any time.
#
#   scripts/diagnose.sh                       # checks metrics.joelycannoli.com
#   HOSTNAME_FQDN=metrics.foo.com scripts/diagnose.sh

set -u  # don't `set -e` — we want every check to run even if one fails

HOSTNAME="${HOSTNAME_FQDN:-metrics.joelycannoli.com}"
TUNNEL_NAME="${TUNNEL_NAME:-anchor}"
LOCAL_BIND="${LOCAL_BIND:-127.0.0.1:5050}"

PASS="\033[32m✓\033[0m"
WARN="\033[33m⚠\033[0m"
FAIL="\033[31m✗\033[0m"

ok()   { printf "  $PASS %s\n" "$*"; }
warn() { printf "  $WARN %s\n" "$*"; }
bad()  { printf "  $FAIL %s\n" "$*"; }
hdr()  { printf "\n\033[1m%s\033[0m\n" "$*"; }

# ── 1. Local dashboard ───────────────────────────────────────────────────────
hdr "1. Dashboard service (anchor-dashboard)"

if systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^anchor-dashboard.service"; then
  ok "systemd unit installed"
  STATE="$(systemctl is-active anchor-dashboard 2>/dev/null || true)"
  SUBSTATE="$(systemctl show -p SubState --value anchor-dashboard 2>/dev/null || true)"
  case "$STATE" in
    active)
      if [ "$SUBSTATE" = "running" ]; then
        ok "service is active (running)"
      else
        warn "service active but SubState=$SUBSTATE"
      fi
      ;;
    activating)
      bad "service is stuck in 'activating' state (SubState=$SUBSTATE) — likely crash-looping"
      echo "    last 15 journal lines:"
      sudo journalctl -u anchor-dashboard -n 15 --no-pager 2>/dev/null \
        | sed 's/^/      /'
      ;;
    *)
      bad "service is NOT active (state=$STATE, sub=$SUBSTATE)"
      echo "    last 15 journal lines:"
      sudo journalctl -u anchor-dashboard -n 15 --no-pager 2>/dev/null \
        | sed 's/^/      /'
      ;;
  esac
else
  bad "systemd unit NOT installed"
  echo "    → run: scripts/install_dashboard_service.sh"
fi

# ── 2. Local healthz ─────────────────────────────────────────────────────────
hdr "2. Local healthz ($LOCAL_BIND)"

if curl -fsS -m 3 -o /dev/null "http://$LOCAL_BIND/healthz" 2>/dev/null; then
  ok "http://$LOCAL_BIND/healthz returns 200"
else
  bad "http://$LOCAL_BIND/healthz unreachable"
  # If the service is active but healthz still fails, gunicorn started but
  # workers are crashing (import error / bind failure / etc.) — tail the
  # journal so the actual stack trace is right here.
  if systemctl is-active --quiet anchor-dashboard 2>/dev/null; then
    echo "    service is up but not responding — recent journal lines:"
    sudo journalctl -u anchor-dashboard -n 25 --no-pager 2>/dev/null \
      | sed 's/^/      /'
  else
    echo "    → sudo journalctl -u anchor-dashboard -n 30"
  fi
fi

# ── 3. cloudflared ───────────────────────────────────────────────────────────
hdr "3. Cloudflare Tunnel (cloudflared)"

if command -v cloudflared >/dev/null 2>&1; then
  ok "cloudflared installed ($(cloudflared --version 2>&1 | head -1))"
else
  bad "cloudflared NOT installed"
  echo "    → run: scripts/install_tunnel.sh"
fi

if [ -f "$HOME/.cloudflared/cert.pem" ]; then
  ok "cert.pem present (~/.cloudflared/cert.pem)"
else
  bad "cert.pem MISSING — Cloudflare login never completed"
  echo "    → run: scripts/install_tunnel.sh   (and complete the browser flow)"
fi

if command -v cloudflared >/dev/null 2>&1; then
  TUNNEL_UUID="$(cloudflared tunnel list --output json 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(next((t['id'] for t in d if t.get('name')==sys.argv[1]),''))" "$TUNNEL_NAME" 2>/dev/null)"
  if [ -n "$TUNNEL_UUID" ]; then
    ok "tunnel '$TUNNEL_NAME' exists ($TUNNEL_UUID)"
    if [ -f "$HOME/.cloudflared/$TUNNEL_UUID.json" ]; then
      ok "credentials file present"
    else
      bad "credentials file MISSING at ~/.cloudflared/$TUNNEL_UUID.json"
    fi
  else
    bad "tunnel '$TUNNEL_NAME' does not exist"
    echo "    → run: scripts/install_tunnel.sh"
  fi
fi

if [ -f "$HOME/.cloudflared/config.yml" ]; then
  ok "config.yml present (~/.cloudflared/config.yml)"
else
  bad "config.yml MISSING at ~/.cloudflared/config.yml"
fi

# `cloudflared service install` runs the daemon out of /etc/cloudflared/.
# Verify the system-wide config matches the user-side config, otherwise the
# running service is using a stale tunnel UUID (this is a real failure mode
# we hit during a re-install).
if [ -f /etc/cloudflared/config.yml ]; then
  SYS_UUID="$(awk '/^tunnel:/ {print $2; exit}' /etc/cloudflared/config.yml 2>/dev/null)"
  USER_UUID="$(awk '/^tunnel:/ {print $2; exit}' "$HOME/.cloudflared/config.yml" 2>/dev/null)"
  if [ -n "$SYS_UUID" ] && [ -n "$USER_UUID" ] && [ "$SYS_UUID" = "$USER_UUID" ]; then
    ok "/etc/cloudflared/config.yml matches user config (tunnel $SYS_UUID)"
  elif [ -n "$SYS_UUID" ]; then
    bad "/etc/cloudflared/config.yml references a DIFFERENT tunnel UUID ($SYS_UUID)"
    echo "    The systemd service is running with stale config."
    echo "    → run: scripts/install_tunnel.sh   (re-syncs /etc/cloudflared)"
  fi
fi

if systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^cloudflared.service"; then
  ok "cloudflared systemd unit installed"
  if systemctl is-active --quiet cloudflared; then
    ok "cloudflared service is active"
  else
    bad "cloudflared service NOT active"
    echo "    → sudo systemctl status cloudflared"
    echo "    → sudo journalctl -u cloudflared -n 30"
  fi
else
  bad "cloudflared systemd unit NOT installed"
  echo "    → run: scripts/install_tunnel.sh"
fi

# Active edge connections from this Pi to Cloudflare's POPs.
if command -v cloudflared >/dev/null 2>&1 && [ -n "${TUNNEL_UUID:-}" ]; then
  CONN_COUNT="$(cloudflared tunnel info --output json "$TUNNEL_NAME" 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('conns',[])))" 2>/dev/null || echo 0)"
  if [ "$CONN_COUNT" -gt 0 ] 2>/dev/null; then
    ok "tunnel has $CONN_COUNT active edge connection(s)"
  else
    bad "tunnel has 0 active edge connections"
    echo "    → outbound 443 to Cloudflare may be blocked, or service hasn't started"
  fi
fi

# ── 4. DNS resolution (the user's actual symptom) ────────────────────────────
hdr "4. DNS resolution for $HOSTNAME"

if getent hosts "$HOSTNAME" >/dev/null 2>&1; then
  RESOLVED="$(getent hosts "$HOSTNAME" | head -1 | awk '{print $1}')"
  ok "$HOSTNAME resolves to $RESOLVED"
else
  bad "$HOSTNAME does NOT resolve"
  echo "    This is what the browser sees as ERR_NAME_NOT_RESOLVED."
  echo "    Most likely cause: the Cloudflare CNAME was never created."
  echo "    → run: scripts/install_tunnel.sh"
  echo "    → or check: https://dash.cloudflare.com → joelycannoli.com → DNS"
fi

# ── 5. End-to-end healthz over HTTPS ─────────────────────────────────────────
hdr "5. End-to-end: https://$HOSTNAME/healthz"

if HTTP_OUT="$(curl -fsS -m 8 -o /dev/null -w '%{http_code}' "https://$HOSTNAME/healthz" 2>&1)"; then
  if [ "$HTTP_OUT" = "200" ]; then
    ok "https://$HOSTNAME/healthz → 200"
  else
    warn "https://$HOSTNAME/healthz → $HTTP_OUT"
  fi
else
  bad "https://$HOSTNAME/healthz unreachable"
  echo "    → if DNS resolved above, check tunnel status (section 3)"
  echo "    → if status is 502: dashboard is down (section 1)"
  echo "    → if status is 530: tunnel is down (section 3)"
fi

echo
