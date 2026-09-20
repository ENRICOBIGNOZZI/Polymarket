#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA required}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
SOURCE_DIR="${POLYMARKET_APP_DIR:-/home/$SERVICE_USER/polymarket}"
RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:-/home/$SERVICE_USER/polymarket-runtime}"
RUNTIME_CURRENT="$RUNTIME_ROOT/current"
TARGET_RUNTIME="$RUNTIME_ROOT/by-sha/$EXPECTED_SHA"
ARTIFACT_ROOT="${POLYMARKET_ARTIFACT_ROOT:-/home/$SERVICE_USER/polymarket-artifacts}"
TARGET_ARTIFACT="$ARTIFACT_ROOT/by-sha/$EXPECTED_SHA"
ARTIFACT_CURRENT="$ARTIFACT_ROOT/current"
RUN_ROOT="${PM_V7_RUN_ROOT:-/home/$SERVICE_USER/polymarket-runs/paper_v7_london}"
ARCHIVE_ROOT="${PM_V7_ARCHIVE_ROOT:-/home/$SERVICE_USER/polymarket-runs/paper_v7_london_archives}"

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 78; }
[[ "$(uname -s)" == Linux ]] || { echo "London cutover requires Linux" >&2; exit 78; }
LOCK_FILE="${POLYMARKET_LONDON_DEPLOY_LOCK_FILE:-/home/$SERVICE_USER/.cache/polymarket-v7-london-deploy.lock}"
LOCK_DIR="$(dirname "$LOCK_FILE")"
command -v flock >/dev/null 2>&1 || { echo "flock is required for London deployment serialization" >&2; exit 78; }
if [[ "$(id -u)" == 0 ]]; then
  SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$LOCK_DIR"
  touch "$LOCK_FILE"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$LOCK_FILE"
else
  mkdir -p "$LOCK_DIR"
  touch "$LOCK_FILE"
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another London stage/cutover already owns this host" >&2
  exit 73
fi
[[ -f "$TARGET_RUNTIME/deploy/london/runtime_sha" ]] || { echo "staged runtime bundle missing" >&2; exit 66; }
[[ "$(cat "$TARGET_RUNTIME/deploy/london/runtime_sha")" == "$EXPECTED_SHA" ]] || { echo "staged runtime bundle SHA mismatch" >&2; exit 66; }
[[ ! -e "$TARGET_RUNTIME/research" && ! -e "$TARGET_RUNTIME/tests" ]] || { echo "forbidden tree present in staged runtime" >&2; exit 66; }

# Fail closed before any service/runtime mutation unless the staged release is
# one native, single-owner crypto trigger-to-admission process. This verifier
# is deployment control-plane only; it is not a runtime dependency.
python3 "$TARGET_RUNTIME/ops/verify_v7_native_critical_path.py" \
  --policy "$TARGET_RUNTIME/config/v7_native_critical_path_policy.json" \
  --manifest "$TARGET_RUNTIME/config/v7_process_manifest.json"

python3 - "$TARGET_ARTIFACT/manifest.json" "$EXPECTED_SHA" <<'PY'
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

PROBABILITY_MODEL_SOURCE="${POLYMARKET_PROBABILITY_MODEL_SOURCE:-}"
if [[ -n "$PROBABILITY_MODEL_SOURCE" ]]; then
  [[ "$PROBABILITY_MODEL_SOURCE" == /* && -f "$PROBABILITY_MODEL_SOURCE" && ! -L "$PROBABILITY_MODEL_SOURCE" ]] || {
    echo "probability model source must be an absolute regular file" >&2; exit 78;
  }
  python3 - "$PROBABILITY_MODEL_SOURCE" "$EXPECTED_SHA" <<'PYPROB'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);sha=sys.argv[2]
v=json.loads(p.read_text(encoding='utf-8'))
assert v.get('schema')=='v7_probability_logit_candidate_v1'
assert v.get('code_sha')==sha
assert v.get('paper_only') is True
assert v.get('authenticated_execution') is False
assert v.get('real_order_submission') is False
assert int(v.get('test_duration_seconds') or 0)==7200
assert v.get('forward_calibrated') is False
assert not (v.get('excluded_assets') or [])
assert not (v.get('asset_shadow_overrides') or [])
print('probability_model_gate=ready')
PYPROB
fi

# Check the same exact-SHA CI contract as runtime BEFORE stopping healthy
# services or moving any run directory. A failed/pending check must leave the
# old generation collecting data; it must not spend the new restart budget.
CI_REPOSITORY="${PM_V7_CI_REPOSITORY:-ENRICOBIGNOZZI/Polymarket}"
CI_PREFLIGHT_DIR="$RUNTIME_ROOT/deployment-receipts"
mkdir -p "$CI_PREFLIGHT_DIR"
CI_PREFLIGHT_RECEIPT="$CI_PREFLIGHT_DIR/ci-$EXPECTED_SHA-$(date -u +%Y%m%dT%H%M%S)-$$.json"
python3 "$TARGET_RUNTIME/scripts/v7_exact_sha_ci_gate.py" \
  --repository "$CI_REPOSITORY" --sha "$EXPECTED_SHA" \
  --output "$CI_PREFLIGHT_RECEIPT"

# Capture the run root currently bound to systemd before rewriting it. The
# target may use a new per-SHA root, but unresolved native fills in the previous
# generation must still block a cross-SHA cutover.
PREVIOUS_RUN_ROOT=""
if systemctl cat polymarket-v7-paper.service >/dev/null 2>&1; then
  PREVIOUS_RUN_ROOT="$(systemctl show polymarket-v7-paper.service -p Environment --value | \
    python3 -c 'import shlex,sys; items=shlex.split(sys.stdin.read()); print(next((x.split("=",1)[1] for x in items if x.startswith("PM_V7_RUN_ROOT=")), ""))')"
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
# Prove the PAPER service cgroup is actually quiescent before treating the
# manager status file as historical. KillMode=mixed keeps all runtime children
# in the service cgroup, so inactive+MainPID=0 (and an empty cgroup when it
# still exists) is the authoritative liveness proof.
sudo systemctl stop polymarket-v7-paper.service >/dev/null 2>&1 || true
paper_state="$(systemctl show polymarket-v7-paper.service -p ActiveState --value 2>/dev/null || true)"
paper_main_pid="$(systemctl show polymarket-v7-paper.service -p MainPID --value 2>/dev/null || true)"
paper_cgroup="$(systemctl show polymarket-v7-paper.service -p ControlGroup --value 2>/dev/null || true)"
[[ "$paper_state" == inactive || "$paper_state" == failed ]] || {
  echo "prior PAPER service did not quiesce: state=$paper_state" >&2; exit 69;
}
[[ "$paper_main_pid" =~ ^[0-9]+$ && "$paper_main_pid" == 0 ]] || {
  echo "prior PAPER service MainPID still active: $paper_main_pid" >&2; exit 69;
}
if [[ -n "$paper_cgroup" && -r "/sys/fs/cgroup$paper_cgroup/cgroup.procs" && -s "/sys/fs/cgroup$paper_cgroup/cgroup.procs" ]]; then
  echo "prior PAPER service cgroup still has processes" >&2
  exit 69
fi
sudo systemctl stop polymarket-v7-exporter.service >/dev/null 2>&1 || true

# A clean systemd stop can leave the last manager snapshot with non-zero PIDs.
# Normalize only after the service/cgroup liveness proof above; never use this
# as a substitute for stopping a live process.
if [[ -n "$PREVIOUS_RUN_ROOT" ]]; then
  native_status="$PREVIOUS_RUN_ROOT/control/native_engine_manager_status.json"
  if [[ -e "$native_status" ]]; then
    [[ -f "$native_status" && ! -L "$native_status" ]] || {
      echo "unsafe prior native manager status path" >&2; exit 78;
    }
    python3 - "$native_status" <<'PYNATIVEQUIESCE'
import json, os, sys
from pathlib import Path
p=Path(sys.argv[1])
v=json.loads(p.read_text(encoding="utf-8"))
if (
    v.get("schema") != "polymarket_v7_native_engine_manager_status_v1"
    or v.get("paper_only") is not True
    or v.get("authenticated_execution") is not False
    or v.get("real_order_submission") is not False
    or v.get("real_capital_at_risk") is not False
    or v.get("single_native_portfolio_owner") is not True
):
    raise SystemExit("prior native manager status safety contract invalid")
v["state"]="STOPPED"
v["active_worker_count"]=0
v["engine_pid"]=0
v["engine_pids"]=[]
workers=v.get("workers")
if isinstance(workers,list):
    for row in workers:
        if isinstance(row,dict):
            row["pid"]=0
            if "state" in row:
                row["state"]="STOPPED"
st=p.stat()
tmp=p.with_name(p.name+".cutover-quiesced.tmp")
tmp.write_text(json.dumps(v,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
os.chmod(tmp, st.st_mode & 0o777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp,p)
PYNATIVEQUIESCE
  fi
fi

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

PROBABILITY_ENV="$RUN_ROOT/control/probability-model.env"
rm -f "$PROBABILITY_ENV"
if [[ -n "$PROBABILITY_MODEL_SOURCE" ]]; then
  PROBABILITY_MODEL_TARGET="$TARGET_ARTIFACT/probability_model.json"
  install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600     "$PROBABILITY_MODEL_SOURCE" "$PROBABILITY_MODEL_TARGET"
  tmp_probability_env="$PROBABILITY_ENV.tmp.$$"
  {
    printf 'PM_V7_PROBABILITY_MODEL=%s\n' "$PROBABILITY_MODEL_TARGET"
    printf 'PM_V7_PROBABILITY_EVALUATION_SECONDS=7200\n'
  } > "$tmp_probability_env"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$tmp_probability_env"
  chmod 0600 "$tmp_probability_env"
  mv "$tmp_probability_env" "$PROBABILITY_ENV"
fi

# Shell redirections are opened by this root control-plane shell before sudo
# changes the Python process user. Pre-create every shared log with the runtime
# owner, or the later service restart cannot append under UMask=0077.
for runtime_log in "$RUN_ROOT/legacy_native_claims.log" "$RUN_ROOT/legacy_native_reconciliation.log"; do
  [[ ! -L "$runtime_log" ]] || { echo "unsafe legacy runtime log symlink: $runtime_log" >&2; exit 78; }
  if [[ -e "$runtime_log" ]]; then
    [[ -f "$runtime_log" ]] || { echo "unsafe legacy runtime log type: $runtime_log" >&2; exit 78; }
    chown "$SERVICE_USER:$SERVICE_GROUP" "$runtime_log"
    chmod 0600 "$runtime_log"
  else
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 /dev/null "$runtime_log"
  fi
done

# Reconcile historical PAPER native claims in the cutover control plane, before
# the current run's ledger writer starts. Anything unresolved remains reserved
# by the final registry and cannot vanish across SHA generations.
sudo -u "$SERVICE_USER" python3 "$TARGET_RUNTIME/scripts/v7_legacy_native_claims.py" \
  --current-run-root "$RUN_ROOT" --scan-parent "$(dirname "$RUN_ROOT")" \
  --target-sha "$EXPECTED_SHA" --output "$RUN_ROOT/control/legacy_native_claims.json" \
  >> "$RUN_ROOT/legacy_native_claims.log" 2>&1
sudo -u "$SERVICE_USER" python3 "$TARGET_RUNTIME/scripts/v7_legacy_native_reconciler.py" \
  --registry "$RUN_ROOT/control/legacy_native_claims.json" --repository-root "$TARGET_RUNTIME" \
  --target-sha "$EXPECTED_SHA" --output "$RUN_ROOT/control/legacy_native_reconciliation.json" \
  >> "$RUN_ROOT/legacy_native_reconciliation.log" 2>&1 || true
sudo -u "$SERVICE_USER" python3 "$TARGET_RUNTIME/scripts/v7_legacy_native_claims.py" \
  --current-run-root "$RUN_ROOT" --scan-parent "$(dirname "$RUN_ROOT")" \
  --target-sha "$EXPECTED_SHA" --output "$RUN_ROOT/control/legacy_native_claims.json" \
  >> "$RUN_ROOT/legacy_native_claims.log" 2>&1

# Atomic release pointer switch happens only after the model-artifact gate, old-generation archive, and legacy native claim reconciliation/reservation.
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
assert r.get('economic_new_risk_ready') is False
assert r.get('authorized_alpha_actions') in (None, [])
assert a.get('target_model_sha')==sha and a.get('runtime_training') is False
assert p.get('runtime_training') is False and p.get('retrospective_analytics') is False
pid=int(r.get('pid') or 0); assert pid>0; os.kill(pid,0)
assert int(time.time())-int(r.get('timestamp') or 0)<=30
PY
  then
    if metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null)"; then
      core_metrics_ready=1
      for expected_metric in         'polymarket_v7_execution_alive 1'         'polymarket_v7_single_writer_ok 1'         'polymarket_v7_exact_sha_ok 1'         'polymarket_v7_paper_only_contract_ok 1'         'polymarket_v7_authenticated_execution_disabled 1'         'polymarket_v7_native_engine_mode 1'         'polymarket_v7_economic_new_risk_ready 0'; do
        grep -Fxq "$expected_metric" <<<"$metrics" || { core_metrics_ready=0; break; }
      done
      if [[ "$core_metrics_ready" == 1 ]]; then
        ready=1
        break
      fi
    fi
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo "London PAPER core runtime health gate failed" >&2; exit 70; }

# Full exporter health deliberately includes data-plane completeness (30/60 PM
# book coverage, all external assets, retention, etc.). Those are observable
# readiness conditions, not permission to keep the PAPER core stopped. New risk
# remains fail-closed above. Preserve the full health result as deployment
# evidence without conflating it with core process/safety liveness.
exporter_health_payload="$(curl -sS http://127.0.0.1:9108/healthz 2>/dev/null || true)"
printf 'exporter_full_health=%s\n' "$exporter_health_payload"

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

# `current` is a convenience pointer for research/control-plane consumers only.
# The service itself is pinned to by-sha/$EXPECTED_SHA, so moving this pointer
# can never invalidate or prevent restart of an older active generation.
ln -sfn "by-sha/$EXPECTED_SHA" "$ARTIFACT_CURRENT"
[[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["target_model_sha"])' "$ARTIFACT_CURRENT/manifest.json")" == "$EXPECTED_SHA" ]]
printf 'cutover_result=success\nsha=%s\nruntime=%s\nrun_root=%s\n' "$EXPECTED_SHA" "$RUNTIME_CURRENT" "$RUN_ROOT"
