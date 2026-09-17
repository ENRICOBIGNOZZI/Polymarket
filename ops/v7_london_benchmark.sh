#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
MODE="${1:-smoke}"
OUTPUT_DIR="${POLYMARKET_BENCHMARK_DIR:-$HOME/polymarket-benchmarks}"
CONFIG="$APP_DIR/config/v7_london_az_shootout.json"
PROBE="$APP_DIR/build/polymarket_v7_latency_probe"
LOCK_FILE="${POLYMARKET_BENCHMARK_LOCK_FILE:-/tmp/polymarket-v7-london-benchmark.lock}"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git -C "$APP_DIR" rev-parse HEAD)" == "$EXPECTED_SHA" ]]
[[ -x "$PROBE" ]]
[[ -f "$CONFIG" ]]
command -v flock >/dev/null 2>&1 || { echo "flock is required" >&2; exit 78; }
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another London benchmark already owns this host: $LOCK_FILE" >&2
  exit 75
fi

TOKEN="$(curl --noproxy 169.254.169.254 --connect-timeout 2 --max-time 5 -fsS -X PUT 'http://169.254.169.254/latest/api/token' \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600')"
imds(){ curl --noproxy 169.254.169.254 --connect-timeout 2 --max-time 5 -fsS -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
REGION="$(imds placement/region)"
AZ_NAME="$(imds placement/availability-zone)"
AZ_ID="$(imds placement/availability-zone-id)"
INSTANCE_ID="$(imds instance-id)"
INSTANCE_TYPE="$(imds instance-type)"

# AZ letters belong to the caller's AWS account. Physical IDs are stable.
python3 "$APP_DIR/scripts/v7_london_identity.py" --config "$CONFIG" \
  --region "$REGION" --zone-name "$AZ_NAME" --zone-id "$AZ_ID"

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
python3 - "$manifest_path" "$EXPECTED_SHA" "$AZ_NAME" "$AZ_ID" "$INSTANCE_ID" "$INSTANCE_TYPE" "$probe_path" "$LOCK_FILE" <<'PY'
import json,socket,sys,time
from pathlib import Path
path,sha,az_name,az_id,instance_id,instance_type,probe,lock_file=sys.argv[1:]
value={
  'schema':'polymarket_v7_london_host_benchmark_manifest_v1',
  'timestamp':int(time.time()), 'hostname':socket.gethostname(),
  'code_sha':sha, 'zone_name':az_name, 'zone_id':az_id,
  'instance_id':instance_id, 'instance_type':instance_type,
  'probe_path':probe, 'paper_only':True,
  'authenticated_execution':False, 'real_order_submission':False,
  'measures_authenticated_order_path':False,
  'zone_name_scope':'THIS_AWS_ACCOUNT_ONLY',
  'selection_scope':'PUBLIC_HTTPS_PROBE_ONLY',
  'single_owner_lock':True,
  'benchmark_lock_file':lock_file,
}
Path(path).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n',encoding='utf-8')
PY
printf 'probe=%s\nmanifest=%s\n' "$probe_path" "$manifest_path"
