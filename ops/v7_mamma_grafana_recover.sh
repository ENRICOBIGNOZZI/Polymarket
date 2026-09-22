#!/usr/bin/env bash
set -euo pipefail

BASE="$HOME/.cache/polymarket-grafana-london"
PROV="$BASE/config/provisioning"
DASH="$BASE/dashboards"
LOGS="$BASE/logs"
PLUGINS="$BASE/plugins"
DATA="$BASE/data"
CFG="$BASE/config/grafana.ini"

test -f "$DASH/polymarket-v7-pure-arb.json"
mkdir -p "$PROV/dashboards" "$DASH" "$LOGS" "$PLUGINS" "$DATA"

cat > "$PROV/dashboards/v7.yml" <<YAML
apiVersion: 1
providers:
  - name: Polymarket V7 Crypto
    orgId: 1
    folder: Polymarket London
    folderUid: afyms0c1xjabkf
    type: file
    disableDeletion: false
    allowUiUpdates: false
    updateIntervalSeconds: 5
    options:
      path: $DASH
      foldersFromFilesStructure: false
YAML

grep -q 'folder: Polymarket London' "$PROV/dashboards/v7.yml"
grep -q 'folderUid: afyms0c1xjabkf' "$PROV/dashboards/v7.yml"
grep -q "path: $DASH" "$PROV/dashboards/v7.yml"
rm -f "$PROV/dashboards/v7-pure-arb.yml"

pkill -TERM -f '/opt/homebrew/bin/grafana server' 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! pgrep -f '/opt/homebrew/bin/grafana server' >/dev/null 2>&1; then break; fi
  sleep 0.25
done
pkill -KILL -f '/opt/homebrew/bin/grafana server' 2>/dev/null || true

mkdir -p "$DATA" "$LOGS" "$PLUGINS"
nohup /opt/homebrew/bin/grafana server \
  --homepath=/opt/homebrew/opt/grafana/share/grafana \
  --config="$CFG" \
  "cfg:default.paths.data=$DATA" \
  "cfg:default.paths.logs=$LOGS" \
  "cfg:default.paths.plugins=$PLUGINS" \
  "cfg:default.paths.provisioning=$PROV" \
  >"$LOGS/grafana-stdout.log" 2>&1 </dev/null &

for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:3000/api/health >/tmp/grafana-health.json 2>/dev/null; then
    break
  fi
  sleep 1
done

cat /tmp/grafana-health.json
curl -fsS http://127.0.0.1:3000/api/search?query=Pure%20Arbitrage > /tmp/grafana-search.json
curl -fsS http://127.0.0.1:3000/api/dashboards/uid/polymarket-v7-pure-arb > /tmp/grafana-uid.json

python3 - <<'PY'
import json
search=json.load(open('/tmp/grafana-search.json'))
uid=json.load(open('/tmp/grafana-uid.json'))
assert any(x.get('uid')=='polymarket-v7-pure-arb' for x in search), search
meta=uid.get('meta') or {}
dash=uid.get('dashboard') or {}
assert dash.get('uid')=='polymarket-v7-pure-arb', dash
print('MAMMA_GRAFANA_PURE_ARB_OK=1')
print('TITLE='+str(dash.get('title')))
print('FOLDER_UID='+str(meta.get('folderUid')))
print('FOLDER_TITLE='+str(meta.get('folderTitle')))
print('URL='+str(meta.get('url')))
PY

TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
if [[ -x "$TS" ]]; then
  "$TS" serve status || true
fi

curl -k -sS -I --max-time 5 \
  https://mamma-portfolio.tail1bae85.ts.net/d/polymarket-v7-pure-arb/polymarket-v7-crypto-pure-arbitrage-paper \
  | head -12 || true
