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

echo "=== DATASOURCE ==="
cat "$PROV/datasources/prometheus-v7.yml" 2>/dev/null || true

echo "=== TAILSCALE PEER 100.102.9.16 ==="
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[[ -x "$TS" ]] || TS="$(command -v tailscale || true)"
if [[ -n "$TS" ]]; then
  "$TS" status --json > /tmp/v7-ts-status.json 2>/dev/null || true
  python3 - <<'PY'
import json
from pathlib import Path
p=Path('/tmp/v7-ts-status.json')
if not p.is_file():
    print('TAILSCALE_STATUS_UNAVAILABLE=1')
    raise SystemExit(0)
v=json.load(open(p))
hits=[]
for peer in (v.get('Peer') or {}).values():
    ips=[str(x) for x in (peer.get('TailscaleIPs') or [])]
    if '100.102.9.16' in ips:
        hits.append({k:peer.get(k) for k in (
            'HostName','DNSName','TailscaleIPs','Online','Active','Expired','Tags'
        )})
print(json.dumps(hits,sort_keys=True))
PY
else
  echo "TAILSCALE_CLI_MISSING=1"
fi

echo "=== PROMETHEUS 19091 ==="
curl -sv --max-time 8 http://100.102.9.16:19091/-/healthy -o /tmp/v7-prom-health 2>&1 || true
cat /tmp/v7-prom-health 2>/dev/null || true
echo
curl -sv --max-time 8 --get \
  --data-urlencode 'query=polymarket_pure_arb_up' \
  http://100.102.9.16:19091/api/v1/query \
  -o /tmp/v7-prom-query 2>&1 || true
cat /tmp/v7-prom-query 2>/dev/null || true
echo
