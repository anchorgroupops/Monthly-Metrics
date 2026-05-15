#!/usr/bin/env bash
# scripts/harden_pi.sh — idempotent Pi OS hardening.
# Configures: unattended-upgrades, fail2ban, SSH key-only auth.
# Pass --dry-run to print actions without applying.

set -euo pipefail

DRY="${1:-}"
run() {
    if [ "$DRY" = "--dry-run" ]; then
        echo "[dry-run] $*"
    else
        eval "$*"
    fi
}

echo "→ Installing unattended-upgrades + fail2ban…"
run "sudo apt-get update -qq"
run "sudo apt-get install -y unattended-upgrades fail2ban"

echo "→ Configuring unattended-upgrades for security only…"
run "sudo dpkg-reconfigure -fnoninteractive unattended-upgrades"

echo "→ Verifying SSH key auth works before disabling password auth…"
KEY_COUNT=$(grep -cE '^[a-z0-9-]+ ' "$HOME/.ssh/authorized_keys" 2>/dev/null || echo 0)
if [ "$KEY_COUNT" -lt 1 ]; then
    echo "  ABORT: ~/.ssh/authorized_keys has no key entries — refusing to disable password auth."
    exit 1
fi

echo "→ Disabling SSH password auth…"
run "sudo sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config"
run "sudo sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config"
run "sudo systemctl restart sshd"

echo "→ Enabling fail2ban for sshd…"
run "sudo systemctl enable --now fail2ban"

echo "→ Done. Verify on next login that key auth still works."
