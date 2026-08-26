#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
LOCAL_BRANCH="${POLYMARKET_BRANCH:-main}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-deploy}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
STATUS_FILE="$STATE_DIR/autoupdate_status.env"
RUNTIME_HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-180}"
DEPLOY_LOCK_DIR="${POLYMARKET_DEPLOY_LOCK_DIR:-$CACHE_DIR/update.lock}"
DEPLOY_LOCK_WAIT_SECONDS="${POLYMARKET_DEPLOY_LOCK_WAIT_SECONDS:-900}"
DEPLOY_LOCK_STALE_SECONDS="${POLYMARKET_DEPLOY_LOCK_STALE_SECONDS:-3600}"
POLYMARKET_DEPLOY_LOCK_V1=1
DEPLOY_LOCK_HELD=0
DEPLOY_LOCK_TOKEN=""

log() { printf '[mac-deploy] %s\n' "$*"; }
fail() { printf '[mac-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

find_brew() {
  if command -v brew >/dev/null 2>&1; then command -v brew
  elif [[ -x /opt/homebrew/bin/brew ]]; then printf '%s\n' /opt/homebrew/bin/brew
  elif [[ -x /usr/local/bin/brew ]]; then printf '%s\n' /usr/local/bin/brew
  else return 1
  fi
}

release_deploy_lock() {
  [[ "$DEPLOY_LOCK_HELD" == "1" && -n "$DEPLOY_LOCK_TOKEN" ]] || return 0
  local recorded=""
  [[ -f "$DEPLOY_LOCK_DIR/owner.env" ]] && recorded="$(sed -n 's/^token=//p' "$DEPLOY_LOCK_DIR/owner.env" | head -n1)"
  if [[ "$recorded" == "$DEPLOY_LOCK_TOKEN" ]]; then rm -rf "$DEPLOY_LOCK_DIR"; fi
  DEPLOY_LOCK_HELD=0
}

acquire_deploy_lock() {
  mkdir -p "$CACHE_DIR"
  local now deadline acquired owner token
  now="$(date +%s)"; deadline=$((now + DEPLOY_LOCK_WAIT_SECONDS)); token="updater-$$-$now"
  while ! mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; do
    now="$(date +%s)"
    acquired="$(sed -n 's/^acquired_ts=//p' "$DEPLOY_LOCK_DIR/owner.env" 2>/dev/null | head -n1 || true)"
    owner="$(sed -n 's/^token=//p' "$DEPLOY_LOCK_DIR/owner.env" 2>/dev/null | head -n1 || true)"
    if [[ "$acquired" =~ ^[0-9]+$ ]] && (( now - acquired > DEPLOY_LOCK_STALE_SECONDS )); then
      log "Reclaiming stale deployment mutex token=${owner:-unknown}"
      rm -rf "$DEPLOY_LOCK_DIR"
      continue
    fi
    (( now < deadline )) || fail "deployment mutex busy token=${owner:-unknown}"
    sleep 2
  done
  DEPLOY_LOCK_TOKEN="$token"; DEPLOY_LOCK_HELD=1
  {
    printf 'token=%s\n' "$token"
    printf 'pid=%s\n' "$$"
    printf 'acquired_ts=%s\n' "$now"
    printf 'deploy_ref=%s\n' "$DEPLOY_REF"
  } > "$DEPLOY_LOCK_DIR/owner.env"
}

write_status() {
  local status="$1" head_sha="$2" validated_sha="$3" main_sha="$4" tmp
  mkdir -p "$STATE_DIR"
  tmp="$(mktemp "$STATE_DIR/autoupdate_status.XXXXXX")"
  {
    printf 'checked_ts=%s\n' "$(date +%s)"
    printf 'status=%s\n' "$status"
    printf 'head=%s\n' "$head_sha"
    printf 'origin=%s\n' "$validated_sha"
    printf 'deploy_ref=%s\n' "$DEPLOY_REF"
    printf 'validated=%s\n' "$validated_sha"
    printf 'origin_main=%s\n' "$main_sha"
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

champion_meta() {
  "$PYTHON_BIN" - "$APP_DIR/config/live_champion.json" <<'PY'
import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
assert int(m['version']) == 7, m
assert m['loop'] == 'scripts/paper_v7_loop.sh', m
assert m['config'] == 'config/paper_v7.json', m
assert m['run_root'] == 'runs/paper_v7_live', m
print(f"7\t{m['run_root']}\t{m['config']}\t{m['loop']}")
PY
}

request_runtime_handoff() {
  local target_sha="$1" meta version run_root_rel config_rel loop_rel marker tmp
  meta="$(champion_meta)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  marker="$APP_DIR/$run_root_rel/runtime_handoff.request"
  mkdir -p "$(dirname "$marker")"; tmp="$marker.tmp.$$"
  {
    printf 'target_sha=%s\n' "$target_sha"
    printf 'requested_ts=%s\n' "$(date +%s)"
    printf 'reason=validated_deploy_handoff\n'
  } > "$tmp"
  mv "$tmp" "$marker"
}

clear_runtime_handoff() {
  local meta version run_root_rel config_rel loop_rel
  meta="$(champion_meta)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  rm -f "$APP_DIR/$run_root_rel/runtime_handoff.request"
}

paper_runtime_healthy() {
  local meta version run_root_rel config_rel loop_rel root execution metrics grafana_search
  meta="$(champion_meta 2>/dev/null)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  [[ "$version" == "7" ]] || return 1
  root="$APP_DIR/$run_root_rel"; execution="$root/execution"
  "$PYTHON_BIN" - "$root" <<'PY'
import json,sys,time
from pathlib import Path
root=Path(sys.argv[1]); ex=root/'execution'; now=time.time()
sup=json.loads((root/'v7_supervisor.json').read_text())
exe=json.loads((ex/'v7_execution_supervisor.json').read_text())
runtime=json.loads((ex/'runtime_status.json').read_text())
proxy=json.loads((ex/'market_proxy_status.json').read_text())
allocator=json.loads((ex/'allocator_status.json').read_text())
assert sup['execution_alive'] is True and sup['shadow_alive'] is True
assert now-float(sup['timestamp']) <= 60
assert now-float(exe['timestamp']) <= 120 and exe['paper_only'] is True
assert runtime['schema'] == 'polymarket_v7_runtime_status_v1'
assert runtime['version'] == 7 and runtime['paper_only'] is True and runtime['authenticated_execution'] is False
assert float(runtime['drawdown']) <= 0.15 + 1e-12
assert proxy['schema'] == 'polymarket_v7_market_proxy_status_v1'
assert now-float(proxy['timestamp']) <= 180
assert int(allocator['models_expected']) == 5 and int(allocator['models_alive']) == 5
assert (ex/'strategy_status.csv').is_file()
PY
  curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 || return 1
  metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null)" || return 1
  grep -q '^polymarket_runtime_info{adapter="v7",run_root="paper_v7_live",version="v7"} 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_v7_runtime_info 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_runtime_pnl_usd ' <<<"$metrics" || return 1
  grep -q '^polymarket_allocator_state_present 1$' <<<"$metrics" || return 1
  grep -q '^polymarket_allocator_models_expected 5$' <<<"$metrics" || return 1
  grep -q '^polymarket_model_info{' <<<"$metrics" || return 1
  curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 || return 1
  curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1 || return 1
  grafana_search="$(curl -fsS http://127.0.0.1:3000/api/search 2>/dev/null)" || return 1
  grep -q 'polymarket-multi-strategy' <<<"$grafana_search" || return 1
}

full_runtime_healthy() { paper_runtime_healthy; }

wait_for_runtime_health() {
  local attempts="${1:-$RUNTIME_HEALTH_ATTEMPTS}" i
  for ((i=0; i<attempts; ++i)); do full_runtime_healthy && return 0; sleep 2; done
  return 1
}

capture_runtime_health_diagnostics() {
  local target_sha="$1"
  [[ -f "$APP_DIR/ops/capture_runtime_health_macos.sh" ]] && bash "$APP_DIR/ops/capture_runtime_health_macos.sh" "$target_sha" || true
}

[[ "$(uname -s)" == "Darwin" ]] || fail "This updater is for macOS only"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ -f "$APP_DIR/.server_bootstrapped_macos" ]] || fail "run ops/bootstrap_macos.sh interactively once first"
BREW_BIN="$(find_brew)" || fail "Homebrew is required"
[[ "$DEPLOY_LOCK_WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_LOCK_WAIT_SECONDS must be positive"
[[ "$DEPLOY_LOCK_STALE_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_LOCK_STALE_SECONDS must be positive"
acquire_deploy_lock
trap release_deploy_lock EXIT

BREW_PREFIX="$("$BREW_BIN" --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PKG_CONFIG_PATH="$("$BREW_BIN" --prefix curl)/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
PYTHON_BIN="$BREW_PREFIX/bin/python3"
[[ "$RUNTIME_HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be positive"

cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  if full_runtime_healthy; then write_status up_to_date "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"; exit 0; fi
  bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || { write_status unhealthy "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"; fail "runtime configuration repair failed"; }
  request_runtime_handoff "$NEW_SHA" || fail "could not request runtime-owner handoff"
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  if wait_for_runtime_health; then write_status repaired "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"; exit 0; fi
  capture_runtime_health_diagnostics "$NEW_SHA"
  write_status unhealthy "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  fail "automatic V7 runtime repair did not restore health"
fi

if git merge-base --is-ancestor "$NEW_SHA" "$OLD_SHA" 2>/dev/null; then
  write_status awaiting_validation "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  exit 0
fi

mkdir -p "$CACHE_DIR"
STAGE="$(mktemp -d "$CACHE_DIR/stage.XXXXXX")"; STAGE_SRC="$STAGE/src"; CONFIG_BACKUP="$STAGE/config-backup"
cleanup() {
  git -C "$APP_DIR" worktree remove --force "$STAGE_SRC" >/dev/null 2>&1 || true
  rm -rf "$STAGE"
  release_deploy_lock
}
trap cleanup EXIT

git -C "$APP_DIR" worktree add --detach "$STAGE_SRC" "$NEW_SHA" >/dev/null
cd "$STAGE_SRC"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
JOBS="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
cmake --build build --parallel "$JOBS"
ctest --test-dir build --output-on-failure
"$PYTHON_BIN" -m unittest tests/test_monitoring_v7_exporter.py tests/test_v7_unified_runtime.py tests/test_v7_point_in_time_archive_workflow.py -v
"$PYTHON_BIN" -m py_compile monitoring/exporter.py monitoring/exporter_v7.py monitoring/exporter_latest_v7.py scripts/v7_*.py
bash -n scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh ops/apply_runtime_config_macos.sh ops/capture_runtime_health_macos.sh
"$PYTHON_BIN" -m json.tool config/live_champion.json >/dev/null
"$PYTHON_BIN" -m json.tool config/paper_v7.json >/dev/null
"$PYTHON_BIN" - <<'PY'
import json
m=json.load(open('config/live_champion.json'))
assert int(m['version']) == 7
assert m['loop'] == 'scripts/paper_v7_loop.sh'
assert m['config'] == 'config/paper_v7.json'
assert m['run_root'] == 'runs/paper_v7_live'
PY

log "Candidate validation passed; staging production build"
cd "$APP_DIR"
git checkout "$LOCAL_BRANCH"
git reset --hard "$NEW_SHA"
rm -rf build.next
cmake -S . -B build.next -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
cmake --build build.next --parallel "$JOBS"

mkdir -p "$CONFIG_BACKUP/grafana/provisioning/datasources" "$CONFIG_BACKUP/grafana/provisioning/dashboards"
for rel in grafana.ini grafana/provisioning/datasources/prometheus.yml grafana/provisioning/dashboards/dashboards.yml; do
  if [[ -f "$STATE_DIR/$rel" ]]; then mkdir -p "$CONFIG_BACKUP/$(dirname "$rel")"; cp "$STATE_DIR/$rel" "$CONFIG_BACKUP/$rel"; fi
done

rollback() {
  local reason="$1"
  log "ROLLBACK: $reason"
  cd "$APP_DIR"; git reset --hard "$OLD_SHA" || true
  if [[ -d build.previous ]]; then rm -rf build; mv build.previous build; fi
  for rel in grafana.ini grafana/provisioning/datasources/prometheus.yml grafana/provisioning/dashboards/dashboards.yml; do
    rm -f "$STATE_DIR/$rel"
    if [[ -f "$CONFIG_BACKUP/$rel" ]]; then mkdir -p "$STATE_DIR/$(dirname "$rel")"; cp "$CONFIG_BACKUP/$rel" "$STATE_DIR/$rel"; fi
  done
  write_status rollback "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  clear_runtime_handoff || true
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  exit 1
}

rm -rf build.previous
[[ -d build ]] && mv build build.previous
mv build.next build
bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || rollback "runtime configuration failed"
request_runtime_handoff "$NEW_SHA" || rollback "could not request runtime-owner handoff"
sudo -n /usr/local/sbin/polymarket-service-control restart || rollback "service restart failed"
if ! wait_for_runtime_health; then capture_runtime_health_diagnostics "$NEW_SHA"; rollback "post-deploy V7 PAPER runtime health checks failed"; fi
FINAL_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"
[[ "$FINAL_SHA" == "$NEW_SHA" ]] || rollback "checkout moved during serialized deployment: actual=$FINAL_SHA expected=$NEW_SHA"
rm -rf build.previous
write_status deployed "$NEW_SHA" "$NEW_SHA" "$MAIN_SHA"
printf 'deployed_sha=%s\nvalidated_ref=%s\nmain_sha=%s\nprevious_sha=%s\nchampion_version=7\nchampion_run_root=runs/paper_v7_live\n' "$NEW_SHA" "$DEPLOY_REF" "$MAIN_SHA" "$OLD_SHA"
log "V7 deployment healthy"
