# Pi deployment cheat-sheet

End goal: dashboard reachable at `https://anchor.joelycannoli.com/metrics`,
nightly snapshots, monthly emails on the 1st.

## 1. Bootstrap the app

SSH into the Pi and run **one** of these:

```bash
# Once the branch is merged to main:
curl -fsSL https://raw.githubusercontent.com/anchorgroupops/Monthly-Metrics/main/deploy/bootstrap.sh | sudo bash

# While the branch is still open (current PR):
curl -fsSL https://raw.githubusercontent.com/anchorgroupops/Monthly-Metrics/claude/agent-dashboard-metrics-q9fTw/deploy/bootstrap.sh \
  | sudo BRANCH=claude/agent-dashboard-metrics-q9fTw bash
```

The script clones into `/opt/monthly-metrics-src`, then runs
`install-pi.sh` which:

- creates the `monthly-metrics` system user
- builds a venv at `/opt/monthly-metrics/venv`
- writes a placeholder `/etc/monthly-metrics.env` (mode 0640, root:monthly-metrics)
- initializes `data/metrics.db`
- installs and enables the four systemd units:
  - `monthly-metrics-web.service` (uvicorn on 127.0.0.1:8081)
  - `monthly-metrics-sync.timer` (daily 06:00)
  - `monthly-metrics-send.timer` (1st of month, 08:00)

It's idempotent — re-running on a future update just refreshes the source
tree and unit files.

## 2. Fill in secrets

```bash
sudo -e /etc/monthly-metrics.env
```

Required:

```
FUB_API_KEY=<your follow up boss key>
SMTP_USER=<gmail addr>
SMTP_PASSWORD=<gmail app password>
EMAIL_FROM=<gmail addr>
SECRET_KEY=<paste output of: openssl rand -hex 32>
```

`WEB_BASE_URL` and `WEB_BASE_PATH` are pre-filled for `anchor.joelycannoli.com/metrics`.

## 3. Drop in the agent roster

CSV at `/opt/monthly-metrics/config/agents.csv` with columns
`name,email,fub_agent_id,active`. Either paste it directly or:

```bash
# from a public Google Sheets share-link (anyone-with-link → viewer):
sudo curl -fsSL "https://docs.google.com/spreadsheets/d/<SHEET_ID>/export?format=csv" \
  -o /opt/monthly-metrics/config/agents.csv
sudo chown monthly-metrics:monthly-metrics /opt/monthly-metrics/config/agents.csv
```

## 4. Restart, sync, and verify locally

```bash
sudo systemctl restart monthly-metrics-web
sudo systemctl start  monthly-metrics-sync.service     # one-off populate
curl -s http://127.0.0.1:8081/metrics/healthz          # → "ok"
```

## 5. Wire `/metrics` into your existing public URL

You said `anchor.joelycannoli.com` is already serving things. Pick whichever
proxy you actually run there:

### Cloudflare Tunnel (most common with Pi + n8n)

```bash
sudo -e /etc/cloudflared/config.yml
```

Add a `path:` ingress rule **before** any catch-all entries. Snippet at
[`deploy/cloudflared.example.yml`](cloudflared.example.yml):

```yaml
ingress:
  - hostname: anchor.joelycannoli.com
    path: /metrics(/.*)?
    service: http://127.0.0.1:8081
  # … your existing rules …
  - service: http_status:404
```

Then `sudo systemctl restart cloudflared`.

### nginx

Drop [`deploy/nginx.example.conf`](nginx.example.conf)'s `location /metrics`
block into the existing `server { ... server_name anchor.joelycannoli.com; ... }`.
`sudo nginx -t && sudo systemctl reload nginx`.

### Caddy

Append the `handle /metrics*` block from
[`deploy/Caddyfile.example`](Caddyfile.example) to your site block.
`sudo systemctl reload caddy`.

## 6. Smoke-test

Open `https://anchor.joelycannoli.com/metrics` in a browser. Enter an agent's
email. They should get the magic-link email and land on their dashboard with
gauges and a (single-point) trend chart.

## 7. Backfill history for the trend lines (optional)

The trend charts won't have multi-month points until you've been running for
several months. To eyeball the UI immediately, seed fake history for one
agent:

```bash
sudo -u monthly-metrics /opt/monthly-metrics/venv/bin/python \
  -m src.storage --seed-history alex@example.com
```

## Logs

```bash
journalctl -u monthly-metrics-web -f          # live web service
journalctl -u monthly-metrics-sync.service    # last daily sync
journalctl -u monthly-metrics-send.service    # last monthly email run
```

## Updating later

```bash
sudo curl -fsSL https://raw.githubusercontent.com/anchorgroupops/Monthly-Metrics/main/deploy/bootstrap.sh | sudo bash
```

The bootstrap is idempotent: it pulls the latest, re-installs the units,
restarts the service. Your env file, DB, and roster CSV are preserved.
