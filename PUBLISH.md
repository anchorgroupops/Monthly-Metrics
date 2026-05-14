# Publishing the dashboard at metrics.joelycannoli.com

This runbook assumes:

- The Pi has the repo at `/opt/Monthly-Metrics`.
- `scripts/install.sh` already ran successfully (`.venv` exists, `.env` filled in, pytest green).
- joelycannoli.com is on Cloudflare DNS (free plan is fine).

The architecture:

```
internet  →  Cloudflare edge (TLS terminates here)
              ↓
          Cloudflare Tunnel (cloudflared service on the Pi, outbound only)
              ↓
          gunicorn @ 127.0.0.1:5050  (anchor-dashboard.service)
              ↓
          Flask + SQLite at /opt/Monthly-Metrics/data/metrics.db
```

No inbound port forwarding. No public IP needed. No manual TLS certs.

## 1. Install the long-running dashboard service

```bash
cd /opt/Monthly-Metrics
scripts/install_dashboard_service.sh
```

This creates and starts `anchor-dashboard.service`. Verify:

```bash
sudo systemctl status anchor-dashboard
curl -I http://127.0.0.1:5050/healthz   # expect HTTP/1.1 200 OK
```

## 2. Install the Cloudflare Tunnel

```bash
cd /opt/Monthly-Metrics
scripts/install_tunnel.sh
```

Mid-script, `cloudflared tunnel login` will print a URL like:

```
Please open the following URL and log in with your Cloudflare account:
https://dash.cloudflare.com/argotunnel?...
```

**On your laptop**, open that URL in a browser, sign in to Cloudflare, click your domain (`joelycannoli.com`), and click **Authorize**. Control returns to the SSH session automatically once Cloudflare confirms.

The script then:

1. Creates a tunnel named `anchor`.
2. Writes `~/.cloudflared/config.yml` mapping `metrics.joelycannoli.com → http://127.0.0.1:5050`.
3. Creates a CNAME via the Cloudflare API.
4. Installs `cloudflared` as a systemd service and starts it.

To use a different subdomain, run with an override:

```bash
HOSTNAME_FQDN=metrics.joelycannoli.com scripts/install_tunnel.sh
```

## 3. Verify from any device with internet

From your laptop / phone:

```bash
curl -I https://metrics.joelycannoli.com/healthz
# HTTP/2 200

curl -I https://metrics.joelycannoli.com/login
# HTTP/2 200
# set-cookie: ...; Secure; HttpOnly; SameSite=Lax
```

In a browser at `https://metrics.joelycannoli.com`:

1. Login form renders with a green padlock (Cloudflare's TLS cert).
2. Sign in with `ADMIN_PASSWORD` from `.env`.
3. Upload, draft, approve, send all behave the same as local-only.

Brute-force test: type a wrong password 5 times in a row — the 5th attempt returns "Too many failed attempts" (HTTP 429) and stays locked for 15 minutes.

## 4. Day-to-day operations

```bash
# Tail dashboard logs
sudo journalctl -u anchor-dashboard -f

# Tail tunnel logs
sudo journalctl -u cloudflared -f

# Restart after a deploy
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart anchor-dashboard
```

The HEARTBEAT cron entry is unchanged — it still runs `scripts/heartbeat.sh` at 09:00 on the 1st of each month, queueing drafts for approval. You then approve them through the public URL instead of an SSH tunnel.

## 5. Troubleshooting

**One-shot diagnostic (always start here)**
```bash
scripts/diagnose.sh
```
Prints a green/red checklist for every step from the dashboard service through Cloudflare DNS to end-to-end HTTPS. The first red line is your problem.

**`ERR_NAME_NOT_RESOLVED` / "site can't be reached" in browser**
DNS isn't pointing at the tunnel. The CNAME in Cloudflare was never created (or was deleted). Re-run:
```bash
scripts/install_tunnel.sh
```
The hardened script fails loudly now if the DNS step doesn't succeed — read the error it prints. Most common cause: the browser auth flow picked the wrong Cloudflare account or didn't authorize the `joelycannoli.com` zone. Fix:
```bash
rm ~/.cloudflared/cert.pem
scripts/install_tunnel.sh   # re-do the browser flow, pick joelycannoli.com
```

**`502 Bad Gateway` from metrics.joelycannoli.com**
DNS works, tunnel works, but the dashboard service isn't running. `sudo systemctl status anchor-dashboard`.

**`530` from metrics.joelycannoli.com**
Cloudflare can't reach the tunnel. `sudo systemctl status cloudflared`. If down, `sudo systemctl restart cloudflared`.

**`CSRF token missing` after login**
You're on an old browser tab opened before the dashboard restart. Hard refresh (Ctrl+Shift+R / Cmd+Shift+R).

**`429 Too Many Requests` on login**
Brute-force lockout triggered. Wait 15 minutes, or restart the dashboard to flush the in-memory limiter: `sudo systemctl restart anchor-dashboard`.

**Pi rebooted, dashboard not responding**
Both services have `Restart=on-failure` and are `enabled` — they should come back automatically. If not:
```bash
sudo systemctl start anchor-dashboard cloudflared
```

## 6. Security notes

- The Pi never exposes any inbound port. All connections are outbound-initiated by `cloudflared`.
- TLS is handled at the Cloudflare edge with their universal SSL cert.
- The dashboard binds to `127.0.0.1` only — even on the local network, the only path in is via the tunnel.
- Sessions are signed with `FLASK_SECRET_KEY` (auto-generated by `scripts/install.sh`).
- Login uses a single `ADMIN_PASSWORD`. If you suspect it leaked: regenerate via
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(24))"
  ```
  Edit `.env`, then `sudo systemctl restart anchor-dashboard`.
- For an extra layer, enable [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/applications/) on the hostname — Cloudflare handles email-OTP / Google SSO at the edge before any traffic hits the Pi. No app code changes needed.
