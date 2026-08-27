#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
EXPECTED_SHA="${EXPECTED_VALIDATED_SHA:-${POLYMARKET_EXPECTED_SHA:-}}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"
MAIN_REF="${POLYMARKET_MAIN_REF:-main}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-v7-deploy}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
LOCK_DIR="$CACHE_DIR/update-v7.lock"
STATUS_FILE="$STATE_DIR/v7_deploy_status.env"
HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-180}"

log(){ printf '[v7-deploy] %s\n' "$*"; }
fail(){ printf '[v7-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "EXPECTED_VALIDATED_SHA must be the exact 40-char validated SHA"
[[ "$DEPLOY_REF" == "paper-validated" ]] || fail "V7 deploy ref must remain paper-validated"
[[ "$MAIN_REF" == "main" ]] || fail "V7 canonical integration ref must remain main"
[[ "$HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be positive"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"

mkdir -p "$CACHE_DIR" "$STATE_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then fail "another V7 deployment owns $LOCK_DIR"; fi
candidate=""
cleanup(){
  if [[ -n "$candidate" && -d "$candidate" ]]; then git -C "$APP_DIR" worktree remove --force "$candidate" >/dev/null 2>&1 || true; fi
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

write_status(){
  local state="$1" detail="${2:-}" tmp="$STATUS_FILE.tmp.$$"
  {
    printf 'timestamp=%s\n' "$(date +%s)"
    printf 'state=%s\n' "$state"
    printf 'expected_sha=%s\n' "$EXPECTED_SHA"
    printf 'server_head=%s\n' "$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo missing)"
    printf 'detail=%s\n' "$detail"
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

production_run_root(){ printf '%s\n' "$APP_DIR/runs/paper_v7_live"; }

production_pid(){
  local status="$(production_run_root)/control/runtime_status.json"
  python3 - "$status" <<'PY' 2>/dev/null || true
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
if p.is_file():
    try:
        v=json.loads(p.read_text()); pid=int(v.get('pid') or 0)
        if pid>0: print(pid)
    except Exception: pass
PY
}

stop_production_runtime(){
  local pid="$(production_pid)"
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null; then
    log "Stopping production V7 pid=$pid only"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 300); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    kill -0 "$pid" 2>/dev/null && fail "production V7 pid=$pid did not drain"
  fi
}

assert_no_legacy_writer(){
  local hits
  hits="$(pgrep -af 'scripts/paper_.*_loop\.sh|scripts/paper_latest_loop\.sh' 2>/dev/null | grep -v 'scripts/paper_v7_execution_loop.sh' || true)"
  [[ -z "$hits" ]] || fail "superseded PAPER writer is still alive: $hits"
}

monitoring_contract(){
  local root="$1"
  python3 - "$root" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); m=json.loads((root/'monitoring/v7_monitoring_manifest.json').read_text())
assert m.get('schema')=='polymarket_v7_monitoring_manifest_v2'
assert m.get('version')==7 and m.get('paper_only') is True and m.get('authenticated_execution') is False
for rel in ('monitoring/exporter_v7.py','monitoring/v7_ledger_metrics.py','monitoring/v7_alerts.yml','monitoring/grafana/dashboards/polymarket-v7.json'):
    assert (root/rel).is_file(), rel
print(m['grafana']['dashboard_uid'])
PY
}

build_current_checkout(){
  local brew_prefix=""
  if command -v brew >/dev/null 2>&1; then brew_prefix="$(brew --prefix)"; fi
  cmake -S "$APP_DIR" -B "$APP_DIR/build" -DCMAKE_BUILD_TYPE=Release ${brew_prefix:+-DCMAKE_PREFIX_PATH="$brew_prefix"}
  cmake --build "$APP_DIR/build" --parallel "${POLYMARKET_BUILD_JOBS:-2}"
}

prevalidate_candidate(){
  candidate="$(mktemp -d "$CACHE_DIR/candidate.${EXPECTED_SHA:0:12}.XXXXXX")"
  rmdir "$candidate"
  git -C "$APP_DIR" worktree add --detach "$candidate" "$EXPECTED_SHA" >/dev/null
  log "Validating exact V7 candidate $EXPECTED_SHA before active-checkout mutation"
  (
    cd "$candidate"
    python3 scripts/v7_cutover_contract.py --repository-root . --expected-head "$EXPECTED_SHA" >/dev/null
    monitoring_contract "$candidate" >/dev/null
    local brew_prefix=""
    if command -v brew >/dev/null 2>&1; then brew_prefix="$(brew --prefix)"; fi
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release ${brew_prefix:+-DCMAKE_PREFIX_PATH="$brew_prefix"}
    cmake --build build --parallel "${POLYMARKET_BUILD_JOBS:-2}"
    ctest --test-dir build --output-on-failure
    python3 -m py_compile scripts/v7_cutover_contract.py scripts/v7_execution_ledger.py scripts/v7_ledger_spool.py scripts/v7_canonical_economics.py scripts/v7_portfolio_guard.py monitoring/exporter_v7.py monitoring/v7_ledger_metrics.py
    bash -n scripts/paper_v7_execution_loop.sh ops/update_server_v7.sh
    python3 -m json.tool config/live_champion.json >/dev/null
    python3 -m json.tool config/paper_v7.json >/dev/null
    python3 -m json.tool monitoring/v7_monitoring_manifest.json >/dev/null
    python3 -m json.tool monitoring/grafana/dashboards/polymarket-v7.json >/dev/null
  )
  git -C "$APP_DIR" worktree remove --force "$candidate" >/dev/null
  candidate=""
}

start_production_runtime(){
  local run_root="$(production_run_root)"
  mkdir -p "$run_root"
  nohup env \
    PM_V7_CONFIG=config/paper_v7.json \
    PM_V7_RUN_ROOT="$run_root" \
    PM_TRADE_RECORDER="$APP_DIR/build/polymarket_trade_recorder" \
    bash "$APP_DIR/scripts/paper_v7_execution_loop.sh" \
    >>"$run_root/deploy-runtime.log" 2>&1 </dev/null &
  local pid=$!
  log "Started production V7 pid=$pid"
  sleep 2
  kill -0 "$pid" 2>/dev/null || { tail -n 200 "$run_root/deploy-runtime.log" >&2 || true; fail "production V7 exited during startup"; }
}

stop_owned_monitoring(){
  local name pidfile pid
  for name in exporter prometheus grafana; do
    pidfile="$STATE_DIR/${name}-v7.pid"
    if [[ -f "$pidfile" ]]; then
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      fi
      rm -f "$pidfile"
    fi
  done
}

start_monitoring(){
  local run_root="$(production_run_root)"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    POLYMARKET_APP_DIR="$APP_DIR" POLYMARKET_STATE_DIR="$STATE_DIR" bash "$APP_DIR/ops/apply_v7_monitoring_config_macos.sh" >/dev/null
    if command -v brew >/dev/null 2>&1; then
      brew services stop prometheus >/dev/null 2>&1 || true
      brew services stop grafana >/dev/null 2>&1 || true
    fi
  fi
  stop_owned_monitoring

  nohup python3 "$APP_DIR/monitoring/exporter_v7.py" --run-root "$run_root" --repository-root "$APP_DIR" --host 127.0.0.1 --port 9108 \
    >>"$run_root/monitoring-exporter.log" 2>&1 </dev/null & echo $! > "$STATE_DIR/exporter-v7.pid"

  if command -v prometheus >/dev/null 2>&1; then
    mkdir -p "$STATE_DIR/prometheus-data"
    nohup prometheus --config.file="$STATE_DIR/prometheus-v7.yml" --web.listen-address=127.0.0.1:9090 --storage.tsdb.path="$STATE_DIR/prometheus-data" \
      >>"$run_root/prometheus-v7.log" 2>&1 </dev/null & echo $! > "$STATE_DIR/prometheus-v7.pid"
  fi

  if command -v grafana >/dev/null 2>&1; then
    local grafana_home="${POLYMARKET_GRAFANA_HOME:-}"
    if [[ -z "$grafana_home" && "$(uname -s)" == "Darwin" && $(command -v brew) ]]; then grafana_home="$(brew --prefix grafana)/share/grafana"; fi
    [[ -n "$grafana_home" ]] || grafana_home="/usr/share/grafana"
    mkdir -p "$STATE_DIR/grafana/data" "$STATE_DIR/grafana/log" "$STATE_DIR/grafana/plugins"
    nohup env \
      GF_SERVER_HTTP_ADDR=127.0.0.1 GF_SERVER_HTTP_PORT=3000 \
      GF_AUTH_ANONYMOUS_ENABLED=true GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
      GF_PATHS_PROVISIONING="$STATE_DIR/grafana/provisioning" \
      GF_PATHS_DATA="$STATE_DIR/grafana/data" GF_PATHS_LOGS="$STATE_DIR/grafana/log" GF_PATHS_PLUGINS="$STATE_DIR/grafana/plugins" \
      grafana server --homepath "$grafana_home" \
      >>"$run_root/grafana-v7.log" 2>&1 </dev/null & echo $! > "$STATE_DIR/grafana-v7.pid"
  fi
}

runtime_health(){
  local run_root="$(production_run_root)" uid="$1"
  python3 - "$run_root" "$EXPECTED_SHA" <<'PY'
import csv,json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]; now=int(time.time())
required=[root/'control/runtime_status.json',root/'control/portfolio_state.json',root/'control/allocations/manifest.json',root/'graph_rv/status.json',root/'canonical_economics.json',root/'ledger/execution.jsonl',root/'trade_tape.csv']
assert all(p.exists() for p in required), [str(p) for p in required if not p.exists()]
runtime=json.loads((root/'control/runtime_status.json').read_text()); portfolio=json.loads((root/'control/portfolio_state.json').read_text()); graph=json.loads((root/'graph_rv/status.json').read_text()); economics=json.loads((root/'canonical_economics.json').read_text())
assert runtime.get('version')==7 and runtime.get('model_sha')==sha and runtime.get('paper_only') is True and runtime.get('authenticated_execution') is False and runtime.get('real_order_submission') is False
pid=int(runtime.get('pid') or 0); assert pid>0; os.kill(pid,0)
assert now-int(runtime.get('timestamp') or 0)<=180
assert portfolio.get('paper_only') is True and portfolio.get('authenticated_execution') is False and portfolio.get('killed') is False and float(portfolio.get('drawdown',1))<.15
assert now-int(portfolio.get('timestamp') or 0)<=30
assert graph.get('paper_only') is True and graph.get('authenticated_execution') is False and now-int(graph.get('timestamp') or 0)<=180
assert economics.get('paper_only') is True and economics.get('authenticated_execution') is False and economics.get('expected_model_sha')==sha
with (root/'trade_tape.csv').open(newline='',encoding='utf-8') as h: rows=list(csv.DictReader(h))
assert rows and max(int(float(r.get('received_ms') or 0)) for r in rows)>0
PY
  curl -fsS http://127.0.0.1:9108/healthz >/dev/null
  local metrics="$(curl -fsS http://127.0.0.1:9108/metrics)"
  grep -q '^polymarket_v7_runtime_info 1$' <<<"$metrics"
  grep -q '^polymarket_v7_execution_alive 1$' <<<"$metrics"
  grep -q '^polymarket_v7_paper_only_contract_ok 1$' <<<"$metrics"
  grep -q '^polymarket_v7_authenticated_execution_disabled 1$' <<<"$metrics"
  grep -q '^polymarket_v7_ledger_valid 1$' <<<"$metrics"
  curl -fsS http://127.0.0.1:9090/-/ready >/dev/null
  curl -fsS http://127.0.0.1:3000/api/health >/dev/null
  curl -fsS "http://127.0.0.1:3000/api/dashboards/uid/$uid" >/dev/null
}

cd "$APP_DIR"
write_status validating "fetching canonical refs"
git fetch --no-tags origin "$MAIN_REF" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$MAIN_REF")"
VALIDATED_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]] || fail "origin/main $MAIN_SHA != exact validated SHA $EXPECTED_SHA"
[[ "$VALIDATED_SHA" == "$EXPECTED_SHA" ]] || fail "origin/paper-validated $VALIDATED_SHA != exact validated SHA $EXPECTED_SHA"
prevalidate_candidate
assert_no_legacy_writer

OLD_SHA="$(git rev-parse HEAD)"
if [[ "$OLD_SHA" != "$EXPECTED_SHA" ]]; then
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] || fail "tracked server checkout is dirty"
  stop_production_runtime
  git checkout --detach "$EXPECTED_SHA"
  git reset --hard "$EXPECTED_SHA"
else
  stop_production_runtime
fi

python3 scripts/v7_cutover_contract.py --repository-root "$APP_DIR" --expected-head "$EXPECTED_SHA" >/dev/null
DASHBOARD_UID="$(monitoring_contract "$APP_DIR")"
build_current_checkout
start_production_runtime
start_monitoring

healthy=0
for _ in $(seq 1 "$HEALTH_ATTEMPTS"); do
  if runtime_health "$DASHBOARD_UID" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done
if [[ "$healthy" != 1 ]]; then
  write_status failed "post-deploy canonical V7 health did not converge"
  runtime_health "$DASHBOARD_UID" || true
  tail -n 200 "$(production_run_root)/deploy-runtime.log" >&2 || true
  fail "canonical V7 runtime/monitoring health failed"
fi

assert_no_legacy_writer
[[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]] || fail "server checkout drifted after deployment"
printf '%s\n' "$EXPECTED_SHA" > "$(production_run_root)/control/deployed_sha"
write_status healthy "canonical V7 PAPER runtime and monitoring healthy"
log "V7 deployed exact SHA $EXPECTED_SHA"
