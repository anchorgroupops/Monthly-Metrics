# HEARTBEAT — Monthly schedule setup

The Anchor Group Monthly Metrics pipeline is designed to run **at 09:00 on the
1st of each month** to draft the prior month's performance digest. The first
production run is **May 1, 2026**, covering April activity.

The HEARTBEAT script (`scripts/heartbeat.sh`) is intentionally read-only with
respect to email delivery — it researches the latest Zillow KPIs, scores the
prior month's data, and queues draft emails. **Admin approval is required**
via the dashboard before any email is sent.

## Pipeline

```
heartbeat.sh
 ├─ python main.py --mode research    # refresh thresholds.json
 └─ python main.py --mode draft       # score + queue draft emails

(admin)
 └─ python main.py --mode dashboard   # review + approve + send
```

## Install — cron (Linux/macOS)

```bash
# Edit your crontab
crontab -e

# Add (adjust the path):
0 9 1 * * /opt/Monthly-Metrics/scripts/heartbeat.sh
```

To keep secrets out of your crontab, drop them in `/opt/Monthly-Metrics/.env`:

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-…
FUB_API_KEY=…              # only if pulling live from FUB
ADMIN_PASSWORD=choose-something-strong
SMTP_USER=reports@anchorgroup.com
SMTP_PASSWORD=…
EMAIL_FROM=reports@anchorgroup.com
```

The script auto-loads `.env` if present.

## Install — systemd timer (server / Pi)

`/etc/systemd/system/anchor-heartbeat.service`:

```ini
[Unit]
Description=Anchor Monthly Metrics HEARTBEAT
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/Monthly-Metrics
EnvironmentFile=/opt/Monthly-Metrics/.env
ExecStart=/opt/Monthly-Metrics/scripts/heartbeat.sh
User=anchor
```

`/etc/systemd/system/anchor-heartbeat.timer`:

```ini
[Unit]
Description=Run Anchor HEARTBEAT at 09:00 on the 1st of each month

[Timer]
OnCalendar=*-*-01 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now anchor-heartbeat.timer
systemctl list-timers anchor-heartbeat.timer
```

## Logs

Each run writes to `logs/heartbeat-YYYYMMDD.log`. Tail the latest:

```bash
tail -f logs/heartbeat-$(date +%Y%m%d).log
```

## Manual test

```bash
./scripts/heartbeat.sh
python main.py --mode dashboard   # then approve + send from the UI
```
