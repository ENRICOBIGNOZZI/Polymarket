#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA required}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
SOURCE_DIR="${POLYMARKET_APP_DIR:-/home/$SERVICE_USER/polymarket}"
RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:-/home/$SERVICE_USER/polymarket-runtime}"
RUNTIME_CURRENT="$RUNTIME_ROOT/current"
TARGET_RUNTIME="$RUNTIME_ROOT/by-sha/$EXPECTED_SHA"
ARTIFACT_ROOT="${POLYMARKET_ARTIFACT_ROOT:-/home/$SERVICE_USER/polymarket-artifacts}"
RUN_ROOT="${PM_V7_RUN_ROOT:-/home/$SERVICE_USER/polymarket-runs/paper_v7_london}"
ARCHIVE_ROOT="${PM_V7_ARCHIVE_ROOT:-/home/$SERVICE_USER/polymarket-runs/paper_v7_london_archives}"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 78; }
[[ "$(uname -s)" == Linux ]] || { echo "London cutover requires Linux" >&2; exit 78; }
[[ -f "$TARGET_RUNTIME/deploy/london/runtime_sha" ]] || { echo "staged runtime bundle missing" >&2; exit 66; }
[[ "$(cat "$TARGET_RUNTIME/deploy/london/runtime_sha")" == "$EXPECTED_SHA" ]] || { echo "staged runtime bundle SHA mismatch" >&2; exit 66; }
[[ ! -e "$TARGET_RUNTIME/research" && ! -e "$TARGET_RUNTIME/tests" ]] || { echo "forbidden tree present in staged runtime" >&2; exit 66; }

# Fail closed before any service/runtime mutation unless the staged release is
# one native, single-owner crypto trigger-to-admission process. This verifier
# is deployment control-plane only; it is not a runtime dependency.
python3 "$TARGET_RUNTIME/ops/verify_v7_native_critical_path.py" \
  --policy "$TARGET_RUNTIME/config/v7_native_critical_path_policy.json" \
  --manifest "$TARGET_RUNTIME/config/v7_process_manifest.json"

python3 - "$ARTIFACT_ROOT/current/manifest.json" "$EXPECTED_SHA" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); sha=sys.argv[2]
if not p.is_file(): raise SystemExit('runtime artifact bundle missing')
v=json.loads(p.read_text())
assert v.get('schema')=='polymarket_v7_runtime_artifact_bundle_v1'
assert v.get('paper_only') is True and v.get('authenticated_execution') is False and v.get('real_order_submission') is False
assert v.get('target_model_sha')==sha
assert v.get('runtime_training') is False
print('artifact_gate=ready')
PY

# Capture the run root currently bound to systemd before rewriting it. The
# target may use a new per-SHA root, but unresolved native fills in the previous
# generation must still block a cross-SHA cutover.
PREVIOUS_RUN_ROOT=""
if systemctl cat polymarket-v7-paper.service >/dev/null 2>&1; then
  PREVIOUS_RUN_ROOT="$(systemctl show polymarket-v7-paper.service -p Environment --value \
    | tr ' ' '\\n' | sed -n 's/^PM_V7_RUN_ROOT=//p' | head -n 1)"
fi
if [[ -n "$PREVIOUS_RUN_ROOT" && "$PREVIOUS_RUN_ROOT" != /* ]]; then
  echo "previous London run root is not absolute" >&2
  exit 78
fi
ALLOW_NATIVE_CARRYOVER="${PM_V7_ALLOW_NATIVE_CARRYOVER:-0}"
[[ "$ALLOW_NATIVE_CARRYOVER" == 0 || "$ALLOW_NATIVE_CARRYOVER" == 1 ]] || {
  echo "PM_V7_ALLOW_NATIVE_CARRYOVER must be 0 or 1" >&2; exit 78;
}
if [[ "$ALLOW_NATIVE_CARRYOVER" == 1 && -n "$PREVIOUS_RUN_ROOT" && "$PREVIOUS_RUN_ROOT" != "$RUN_ROOT" ]]; then
  echo "native carryover requires a stable London run root" >&2
  exit 78
fi

# Stop old generation before archive; never copy its ledger into the new run.
sudo systemctl stop polymarket-v7-exporter.service polymarket-v7-paper.service >/dev/null 2>&1 || true
if [[ -n "$PREVIOUS_RUN_ROOT" && "$PREVIOUS_RUN_ROOT" != "$RUN_ROOT" && -e "$PREVIOUS_RUN_ROOT" ]]; then
  python3 "$SOURCE_DIR/scripts/v7_prepare_cutover_run_root.py" \
    --run-root "$PREVIOUS_RUN_ROOT" --archive-root "$ARCHIVE_ROOT" \
    --repository-root "$SOURCE_DIR" --target-sha "$EXPECTED_SHA"
fi
prepare_args=(
  --run-root "$RUN_ROOT" --archive-root "$ARCHIVE_ROOT"
  --repository-root "$SOURCE_DIR" --target-sha "$EXPECTED_SHA"
)
if [[ "$ALLOW_NATIVE_CARRYOVER" == 1 ]]; then
  prepare_args+=(--allow-native-carryover)
fi
python3 "$SOURCE_DIR/scripts/v7_prepare_cutover_run_root.py" "${prepare_args[@]}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$RUN_ROOT" "$RUN_ROOT/control"

# Atomic release pointer switch happens only after the model-artifact gate and old-generation archive.
ln -sfn "by-sha/$EXPECTED_SHA" "$RUNTIME_CURRENT"
[[ "$(cat "$RUNTIME_CURRENT/deploy/london/runtime_sha")" == "$EXPECTED_SHA" ]]
render_unit(){
  local source="$1" destination="$2"
  python3 - "$source" "$destination" "$SERVICE_USER" "$SERVICE_GROUP" "$RUNTIME_CURRENT" "$RUN_ROOT" "$EXPECTED_SHA" <<'PYUNIT'
import os,sys
from pathlib import Path
source,destination,user,group,app,run,sha=sys.argv[1:]
payload=Path(source).read_text()
for k,v in {'@SERVICE_USER@':user,'@SERVICE_GROUP@':group,'@APP_DIR@':app,'@RUN_ROOT@':run,'@EXPECTED_SHA@':sha}.items(): payload=payload.replace(k,v)
if '@' in payload: raise SystemExit('unrendered systemd marker')
t=Path(destination+'.tmp');t.write_text(payload);os.chmod(t,0o644);os.replace(t,destination)
PYUNIT
}
for name in polymarket-v7-paper.service polymarket-v7-exporter.service polymarket-v7-retention.service polymarket-v7-retention.timer; do
  tmp="$(mktemp)"; render_unit "$TARGET_RUNTIME/ops/systemd/$name.in" "$tmp"; sudo install -m 0644 "$tmp" "/etc/systemd/system/$name"; rm -f "$tmp"
done

# Render and activate the immutable monitoring control plane before trading.
# It consumes exporter snapshots asynchronously and is never a trigger-path dependency.
sudo env POLYMARKET_EXPECTED_SHA="$EXPECTED_SHA" POLYMARKET_SERVICE_USER="$SERVICE_USER" \
  POLYMARKET_RUNTIME_ROOT="$RUNTIME_ROOT" POLYMARKET_RUNTIME_DIR="$TARGET_RUNTIME" \
  bash "$TARGET_RUNTIME/ops/v7_london_install_monitoring.sh"
sudo systemctl daemon-reload
sudo systemctl enable prometheus.service prometheus-node-exporter.service grafana-server.service
sudo systemctl restart prometheus.service prometheus-node-exporter.service grafana-server.service

monitoring_ready=0
for _ in $(seq 1 "${POLYMARKET_MONITORING_HEALTH_ATTEMPTS:-60}"); do
  if curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1; then
    monitoring_ready=1; break
  fi
  sleep 1
done
[[ "$monitoring_ready" == 1 ]] || { echo "London monitoring control-plane health gate failed" >&2; exit 69; }

sudo systemctl enable --now polymarket-v7-paper.service polymarket-v7-exporter.service polymarket-v7-retention.timer

ready=0
for _ in $(seq 1 "${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-240}"); do
  if python3 - "$RUN_ROOT" "$EXPECTED_SHA" <<'PY' >/dev/null 2>&1
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]
r=json.loads((root/'control/runtime_status.json').read_text())
a=json.loads((root/'control/runtime_artifact_receipt.json').read_text())
p=json.loads((root/'control/runtime_resource_plan.json').read_text())
assert r.get('state')=='running' and r.get('model_sha')==sha
assert r.get('paper_only') is True and r.get('authenticated_execution') is False and r.get('real_order_submission') is False
assert set(r.get('economic_engines') or [])=={'CRYPTO_SETTLEMENT_ENGINE'}
assert a.get('target_model_sha')==sha and a.get('runtime_training') is False
assert p.get('runtime_training') is False and p.get('retrospective_analytics') is False
pid=int(r.get('pid') or 0); assert pid>0; os.kill(pid,0)
assert int(time.time())-int(r.get('timestamp') or 0)<=30
PY
  then
    if curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1; then ready=1; break; fi
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo "London PAPER runtime health gate failed" >&2; exit 70; }

# Prometheus must actually scrape the asynchronous V7 exporter. Do not infer
# monitoring truth from process liveness alone.
scrape_ready=0
for _ in $(seq 1 "${POLYMARKET_PROMETHEUS_SCRAPE_ATTEMPTS:-30}"); do
  if curl -fsS --get --data-urlencode 'query=up{job="polymarket-v7"}' \
      http://127.0.0.1:9090/api/v1/query | python3 -c 'import json,sys; v=json.load(sys.stdin); r=v.get("data",{}).get("result",[]); assert len(r)==1 and r[0]["value"][1]=="1"' >/dev/null 2>&1; then
    scrape_ready=1; break
  fi
  sleep 1
done
[[ "$scrape_ready" == 1 ]] || { echo "Prometheus is not scraping the V7 exporter" >&2; exit 72; }

# Explicitly prove no research/training process is resident on London.
if pgrep -af 'v7_(external_rich_train|external_residual_train|maker_durable_learning|pm_repricing_shadow|two_sided_complete_set_shadow|generate_economic_artifacts|profit_attribution|profit_report|economic_decision_report|lossless_data_compaction|permanent_evidence)\.py' >/tmp/polymarket-v7-forbidden-processes 2>/dev/null; then
  cat /tmp/polymarket-v7-forbidden-processes >&2
  rm -f /tmp/polymarket-v7-forbidden-processes
  echo "forbidden research process active on London" >&2
  exit 71
fi
rm -f /tmp/polymarket-v7-forbidden-processes
printf 'cutover_result=success\nsha=%s\nruntime=%s\nrun_root=%s\n' "$EXPECTED_SHA" "$RUNTIME_CURRENT" "$RUN_ROOT"
