#!/usr/bin/env bash
# scripts/smoke.sh — exercise public surface of the live dashboard.
# Run from the workstation after each deploy.

set -euo pipefail

URL="${URL:-https://metrics.joelycannoli.com}"

echo "→ healthz JSON…"
HEALTH=$(curl -fsS "$URL/healthz")
echo "$HEALTH" | python -m json.tool
OK=$(echo "$HEALTH" | python -c "import sys,json;print(json.load(sys.stdin)['ok'])")
[ "$OK" = "True" ] || { echo "healthz reports unhealthy"; exit 1; }

echo "→ login form renders…"
curl -fsS -o /dev/null "$URL/login"

echo "→ root redirects…"
CODE=$(curl -fsS -o /dev/null -w "%{http_code}" "$URL/" || true)
[ "$CODE" = "302" ] || [ "$CODE" = "303" ] || { echo "root: $CODE"; exit 1; }

echo "→ Smoke test PASSED."
