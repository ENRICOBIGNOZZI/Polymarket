#!/usr/bin/env bash
set -euo pipefail

BASE="$HOME/.cache/polymarket-grafana-london"
PROV="$BASE/config/provisioning"
DASH="$BASE/dashboards"
DATA="$BASE/data"
LOGS="$BASE/logs"
PLUGINS="$BASE/plugins"
CFG="$BASE/config/grafana.ini"
RAW="https://raw.githubusercontent.com/ENRICOBIGNOZZI/Polymarket/main"

mkdir -p "$PROV/dashboards" "$DASH" "$DATA" "$LOGS" "$PLUGINS"
test -f "$CFG"
test -f "$PROV/datasources/prometheus-v7.yml"

for name in   polymarket-v7.json   polymarket-v7-external-fair.json   polymarket-v7-latency.json   polymarket-v7-multi-crypto.json   polymarket-v7-pure-arb.json
do
  tmp="$(mktemp)"
  curl -fsSL "$RAW/monitoring/grafana/dashboards/$name" -o "$tmp"
  python3 - "$tmp" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))
assert isinstance(v.get("uid"),str) and v["uid"]
assert isinstance(v.get("panels"),list)
PY
  install -m 0644 "$tmp" "$DASH/$name"
  rm -f "$tmp"
done

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
rm -f "$PROV/dashboards/v7-pure-arb.yml"

pkill -TERM -f '/opt/homebrew/bin/grafana server' 2>/dev/null || true
for _ in $(seq 1 40); do
  pgrep -f '/opt/homebrew/bin/grafana server' >/dev/null 2>&1 || break
  sleep 0.25
done
pkill -KILL -f '/opt/homebrew/bin/grafana server' 2>/dev/null || true

nohup /opt/homebrew/bin/grafana server   --homepath=/opt/homebrew/opt/grafana/share/grafana   --config="$CFG"   "cfg:default.paths.data=$DATA"   "cfg:default.paths.logs=$LOGS"   "cfg:default.paths.plugins=$PLUGINS"   "cfg:default.paths.provisioning=$PROV"   >"$LOGS/grafana-stdout.log" 2>&1 </dev/null &

for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:3000/api/health >/tmp/mamma-grafana-health.json 2>/dev/null && break
  sleep 1
done
cat /tmp/mamma-grafana-health.json
grep -q '"database"' /tmp/mamma-grafana-health.json

search="$(curl -fsS 'http://127.0.0.1:3000/api/search?folderUIDs=afyms0c1xjabkf')"
python3 - "$search" <<'PY'
import json,sys
rows=json.loads(sys.argv[1])
uids={x.get("uid") for x in rows}
required={
 "polymarket-v7",
 "polymarket-v7-external-fair",
 "polymarket-v7-latency",
 "polymarket-v7-multi-crypto",
 "polymarket-v7-pure-arb",
}
missing=required-uids
assert not missing, (missing,rows)
print("MAMMA_DASHBOARDS_OK=1")
print("PURE_ARB_DASHBOARD_OK=1")
PY

result="$(curl -fsS --get --data-urlencode 'query=polymarket_pure_arb_up'   http://100.102.9.16:19091/api/v1/query)"
python3 - "$result" <<'PY'
import json,sys
v=json.loads(sys.argv[1])
rows=v.get("data",{}).get("result",[])
assert rows and any(float(x["value"][1])==1.0 for x in rows), v
print("PURE_ARB_UP_OK=1")
PY

echo "FINAL_MAMMA_GRAFANA_OK=1"
