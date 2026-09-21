#!/usr/bin/env bash
set -euo pipefail

BASE="$HOME/.cache/polymarket-grafana-london"
PROV="$BASE/config/provisioning"

echo "=== PROCESS ==="
pgrep -af grafana || true

echo "=== PROVIDERS ==="
for p in "$PROV/dashboards/"*.yml "$PROV/dashboards/"*.yaml; do
  [[ -f "$p" ]] || continue
  echo "--- $p"
  cat "$p"
done

echo "=== PURE ARB FILES ==="
find "$BASE" -type f -name 'polymarket-v7-pure-arb.json' -print 2>/dev/null || true

echo "=== API SEARCH ==="
curl -sS -w '\nHTTP_SEARCH=%{http_code}\n'   'http://127.0.0.1:3000/api/search?query=Pure%20Arbitrage' || true

echo "=== UID LOOKUP ==="
curl -sS -w '\nHTTP_UID=%{http_code}\n'   'http://127.0.0.1:3000/api/dashboards/uid/polymarket-v7-pure-arb' || true

echo "=== HEALTH ==="
curl -sS -w '\nHTTP_HEALTH=%{http_code}\n' http://127.0.0.1:3000/api/health || true

echo "=== LOG TAIL ==="
for p in "$BASE/logs/"*.log; do
  [[ -f "$p" ]] || continue
  echo "--- $p"
  tail -n 400 "$p" | grep -Ei 'provision|dashboard|folder|pure|error|warn' || true
done

echo "=== SERVICE CONTROL ==="
sudo -n /usr/local/sbin/polymarket-service-control status || true
