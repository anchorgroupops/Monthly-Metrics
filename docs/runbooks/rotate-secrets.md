# Rotating secrets

Each secret has a different blast radius and recovery procedure. Run the
relevant section, then `scripts/deploy.sh` to roll the change.

## ANTHROPIC_API_KEY

Used by `--mode research` only. Rotation is non-blocking: thresholds.json
is updated monthly; missing the key just means the next monthly fire skips
threshold refresh.

1. Generate a new key in the Anthropic console.
2. Update `H:\AI\Secrets\.env.master.private`.
3. Update `/opt/Monthly-Metrics/.env` (derivative).
4. `sudo systemctl restart anchor-dashboard`. Verify next monthly run.

## SMTP_PASSWORD

Blast radius: monthly digest delivery + admin alerts.

1. Rotate at the SMTP provider.
2. Update both `.env` files (master + Pi).
3. `sudo systemctl restart anchor-dashboard`.
4. Trigger a test alert:
   ```bash
   .venv/bin/python -c "from src.notifier import notify_admin_failure; notify_admin_failure('test', 'rotation test')"
   ```
   (note: dedup window may suppress for 30min; touch `data/.last-alert` then `rm` it to reset)

## ADMIN_PASSWORD

Blast radius: dashboard login. Brute-force lockout protects against quick
re-auth attempts but a leaked password should still rotate.

1. `python -c "import secrets; print(secrets.token_urlsafe(24))"`
2. Update `/opt/Monthly-Metrics/.env` (do NOT commit).
3. `sudo systemctl restart anchor-dashboard`.

## FLASK_SECRET_KEY

Blast radius: existing sessions invalidated. Rotate if you suspect leak
of the key itself (server-side only — unlikely outside a host compromise).

1. `python -c "import secrets; print(secrets.token_hex(32))"`
2. Update `/opt/Monthly-Metrics/.env`.
3. `sudo systemctl restart anchor-dashboard`. All admins re-login.

## FUB_API_KEY

Blast radius: monthly pulls. Manual `scripts/diagnose.sh` after rotation.

1. Rotate in FUB account → Developer settings.
2. Update both `.env` files.
3. Restart dashboard. Trigger a manual pull from the dashboard "Pull Now"
   button to verify auth is still good.

## SSH key compromise (host-level)

If you suspect Pi SSH access was compromised:

1. From a trusted machine, generate a new key + push it via the existing
   trusted session: `ssh-copy-id pi@raspberrypi`.
2. SSH in with the new key, edit `~/.ssh/authorized_keys`, remove every
   line that isn't the new key.
3. Run `scripts/harden_pi.sh` (disables password auth, enables fail2ban).
4. Check `sudo last -i | head -30` and `sudo lastb -i | head -30` for the
   compromised IP. Cross-reference `/var/log/auth.log` for `Accepted`
   entries.
5. Rotate ADMIN_PASSWORD and FLASK_SECRET_KEY (above) — assume those
   were exposed too if anyone had shell access.
