#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
MODE="${1:-smoke}"
OUTPUT_DIR="${POLYMARKET_BENCHMARK_DIR:-$HOME/polymarket-benchmarks}"
CONFIG="$APP_DIR/config/v7_london_az_shootout.json"
PROBE="$APP_DIR/build/polymarket_v7_latency_probe"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git -C "$APP_DIR" rev-parse HEAD)" == "$EXPECTED_SHA" ]]
[[ -x "$PROBE" ]]
[[ -f "$CONFIG" ]]

TOKEN="$(curl -fsS -X PUT 'http://169.254.169.254/latest/api/token' \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600')"
imds(){ curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
AZ_NAME="$(imds placement/availability-zone)"
AZ_ID="$(imds placement/availability-zone-id)"
INSTANCE_ID="$(imds instance-id)"
INSTANCE_TYPE="$(imds instance-type)"

python3 - "$CONFIG" "$AZ_NAME" "$AZ_ID" <<'PY'
import json,sys
config=json.load(open(sys.argv[1],encoding='utf-8'))
name,zone_id=sys.argv[2:]
expected={row['zone_name']:row['zone_id'] for row in config['zones']}
if name not in expected or expected[name] != zone_id:
    raise SystemExit(f'AZ mapping mismatch: observed {name}/{zone_id}, expected={expected.get(name)}')
PY

case "$MODE" in
  smoke)
    SAMPLES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["benchmark"]["smoke_samples"])' "$CONFIG")"
    INTERVAL_MS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["benchmark"]["smoke_interval_ms"])' "$CONFIG")"
    ;;
  formal)
    SAMPLES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["benchmark"]["formal_samples"])' "$CONFIG")"
    INTERVAL_MS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["benchmark"]["formal_interval_ms"])' "$CONFIG")"
    ;;
  *) echo "usage: $0 [smoke|formal]" >&2; exit 64 ;;
esac

mkdir -p "$OUTPUT_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
probe_path="$OUTPUT_DIR/${MODE}.${AZ_ID}.${stamp}.json"
manifest_path="$OUTPUT_DIR/${MODE}.${AZ_ID}.${stamp}.host.json"

"$PROBE" --region "$AZ_ID" --exact-code-sha "$EXPECTED_SHA" \
  --samples "$SAMPLES" --interval-ms "$INTERVAL_MS" > "$probe_path"
python3 - "$manifest_path" "$EXPECTED_SHA" "$AZ_NAME" "$AZ_ID" "$INSTANCE_ID" "$INSTANCE_TYPE" "$probe_path" <<'PY'
import json,socket,sys,time
from pathlib import Path
path,sha,az_name,az_id,instance_id,instance_type,probe=sys.argv[1:]
value={
  'schema':'polymarket_v7_london_host_benchmark_manifest_v1',
  'timestamp':int(time.time()), 'hostname':socket.gethostname(),
  'code_sha':sha, 'zone_name':az_name, 'zone_id':az_id,
  'instance_id':instance_id, 'instance_type':instance_type,
  'probe_path':probe, 'paper_only':True,
  'authenticated_execution':False, 'real_order_submission':False,
  'measures_authenticated_order_path':False,
}
Path(path).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n',encoding='utf-8')
PY
printf 'probe=%s\nmanifest=%s\n' "$probe_path" "$manifest_path"
