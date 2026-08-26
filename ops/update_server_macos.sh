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

log(){ printf '[mac-deploy-v7] %s\n' "$*"; }
fail(){ printf '[mac-deploy-v7] ERROR: %s\n' "$*" >&2; exit 1; }

find_brew() {
  if command -v brew >/dev/null 2>&1; then command -v brew
  elif [[ -x /opt/homebrew/bin/brew ]]; then printf '%s\n' /opt/homebrew/bin/brew
  elif [[ -x /usr/local/bin/brew ]]; then printf '%s\n' /usr/local/bin/brew
  else return 1
  fi
}

release_deploy_lock() {
  if [[ "$DEPLOY_LOCK_HELD" != "1" || -z "$DEPLOY_LOCK_TOKEN" ]]; then return 0; fi
  local recorded=""
  [[ -f "$DEPLOY_LOCK_DIR/owner.env" ]] && recorded="$(sed -n 's/^token=//p' "$DEPLOY_LOCK_DIR/owner.env" | head -n 1)"
  if [[ "$recorded" == "$DEPLOY_LOCK_TOKEN" ]]; then rm -rf "$DEPLOY_LOCK_DIR"; fi
  DEPLOY_LOCK_HELD=0
}

acquire_deploy_lock() {
  mkdir -p "$CACHE_DIR"
  local now deadline acquired owner token
  now="$(date +%s)"; deadline=$((now + DEPLOY_LOCK_WAIT_SECONDS)); token="v7-updater-$$-$now"
  while ! mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; do
    now="$(date +%s)"
    acquired="$(sed -n 's/^acquired_ts=//p' "$DEPLOY_LOCK_DIR/owner.env" 2>/dev/null | head -n 1 || true)"
    owner="$(sed -n 's/^token=//p' "$DEPLOY_LOCK_DIR/owner.env" 2>/dev/null | head -n 1 || true)"
    if [[ "$acquired" =~ ^[0-9]+$ ]] && (( now - acquired > DEPLOY_LOCK_STALE_SECONDS )); then
      log "Reclaiming stale deployment mutex token=${owner:-unknown}"
      rm -rf "$DEPLOY_LOCK_DIR"
      continue
    fi
    (( now < deadline )) || fail "deployment mutex busy token=${owner:-unknown}"
    sleep 2
  done
  DEPLOY_LOCK_TOKEN="$token"; DEPLOY_LOCK_HELD=1
  printf 'token=%s\npid=%s\nacquired_ts=%s\ndeploy_ref=%s\n' "$token" "$$" "$now" "$DEPLOY_REF" > "$DEPLOY_LOCK_DIR/owner.env"
}

write_status() {
  local status="$1" head_sha="$2" validated_sha="$3" main_sha="$4"
  mkdir -p "$STATE_DIR"
  local tmp="$(mktemp "$STATE_DIR/autoupdate_status.XXXXXX")"
  printf 'checked_ts=%s\nstatus=%s\nhead=%s\norigin=%s\ndeploy_ref=%s\nvalidated=%s\norigin_main=%s\n' \
    "$(date +%s)" "$status" "$head_sha" "$validated_sha" "$DEPLOY_REF" "$validated_sha" "$main_sha" > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

champion_meta() {
  "$PYTHON_BIN" - "$APP_DIR/config/live_champion.json" <<'PY'
import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
print(f"{m['version']}\t{m['run_root']}\t{m['config']}\t{m['loop']}\t{str(m.get('paper_only')).lower()}\t{str(m.get('authenticated_execution')).lower()}")
PY
}

require_v7_manifest() {
  local meta version run_root config loop paper auth
  meta="$(champion_meta)" || return 1
  IFS=$'\t' read -r version run_root config loop paper auth <<<"$meta"
  [[ "$version" == "7" ]] || return 1
  [[ "$run_root" == "runs/paper_v7_live" ]] || return 1
  [[ "$config" == "config/paper_v7.json" ]] || return 1
  [[ "$loop" == "scripts/paper_v7_loop.sh" ]] || return 1
  [[ "$paper" == "true" && "$auth" == "false" ]] || return 1
}

paper_runtime_healthy() {
  require_v7_manifest || return 1
  "$PYTHON_BIN" - "$APP_DIR/runs/paper_v7_live" <<'PY'
import json,sys,time
from pathlib import Path
root=Path(sys.argv[1])
supervisor=json.loads((root/'v7_supervisor.json').read_text())
runtime=json.loads((root/'execution'/'runtime_status.json').read_text())
allocator=json.loads((root/'execution'/'allocator_status.json').read_text())
assert supervisor.get('execution_alive') is True, supervisor
assert supervisor.get('shadow_alive') is True, supervisor
assert runtime.get('version') == 7, runtime
assert runtime.get('paper_only') is True, runtime
assert runtime.get('authenticated_execution') is False, runtime
assert float(runtime.get('drawdown', 1.0)) <= 0.15 + 1e-12, runtime
assert time.time()-float(runtime['timestamp']) <= 180, runtime
assert int(allocator.get('models_expected', 0)) == 5, allocator
assert int(allocator.get('models_alive', 0)) == 5, allocator
PY
}

full_runtime_healthy() {
  local metrics grafana_search
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
  grep -q 'polymarket-v7-paper' <<<"$grafana_search" || return 1
  paper_runtime_healthy
}

wait_for_runtime_health() {
  local attempts="${1:-$RUNTIME_HEALTH_ATTEMPTS}" i
  for ((i=0; i<attempts; ++i)); do
    if full_runtime_healthy; then return 0; fi
    sleep 2
  done
  return 1
}

capture_runtime_health_diagnostics() {
  local target_sha="$1"
  if [[ -f "$APP_DIR/ops/capture_runtime_health_macos.sh" ]]; then
    bash "$APP_DIR/ops/capture_runtime_health_macos.sh" "$target_sha" || true
  fi
}

validate_checkout() {
  require_v7_manifest || return 1
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
  local jobs="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
  cmake --build build --parallel "$jobs"
  ctest --test-dir build --output-on-failure
  "$PYTHON_BIN" -m unittest tests/test_monitoring_v7_exporter.py tests/test_grafana_v7_contract.py -v
  "$PYTHON_BIN" -m py_compile monitoring/exporter.py monitoring/exporter_v7.py scripts/v7_*.py
  bash -n scripts/paper_v7_loop.sh scripts/paper_v7_execution_loop.sh ops/apply_runtime_config_macos.sh
  "$PYTHON_BIN" -m json.tool config/paper_v7.json >/dev/null
  "$PYTHON_BIN" -m json.tool monitoring/grafana/dashboards/polymarket-v7.json >/dev/null
}

rollback() {
  local reason="$1"
  log "Rolling back to $OLD_SHA: $reason"
  git reset --hard "$OLD_SHA"
  bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || true
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  wait_for_runtime_health 90 || true
  write_status rollback "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  fail "$reason"
}

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS only"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ -f "$APP_DIR/.server_bootstrapped_macos" ]] || fail "run ops/bootstrap_macos.sh once first"
[[ "$RUNTIME_HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be a positive integer"
[[ "$DEPLOY_LOCK_WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_LOCK_WAIT_SECONDS must be a positive integer"
[[ "$DEPLOY_LOCK_STALE_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_LOCK_STALE_SECONDS must be a positive integer"
BREW_BIN="$(find_brew)" || fail "Homebrew is required"
BREW_PREFIX="$("$BREW_BIN" --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PKG_CONFIG_PATH="$("$BREW_BIN" --prefix curl)/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
PYTHON_BIN="$BREW_PREFIX/bin/python3"

acquire_deploy_lock
trap release_deploy_lock EXIT
cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA" || fail "$DEPLOY_REF is not an ancestor of $LOCAL_BRANCH"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  if full_runtime_healthy; then
    write_status up_to_date "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    exit 0
  fi
  log "Same SHA but V7 runtime unhealthy; reapplying runtime config before restart"
  bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || fail "runtime configuration repair failed"
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  if wait_for_runtime_health; then
    write_status repaired "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    log "Runtime and Grafana configuration repaired"
    exit 0
  fi
  capture_runtime_health_diagnostics "$NEW_SHA"
  write_status unhealthy "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  fail "same-SHA V7 runtime repair did not restore health"
fi

if git merge-base --is-ancestor "$NEW_SHA" "$OLD_SHA" 2>/dev/null; then
  write_status awaiting_validation "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  log "Current checkout is ahead of paper-validated; waiting for exact-SHA validation"
  exit 0
fi

sudo -n /usr/local/sbin/polymarket-service-control stop || true
git checkout "$LOCAL_BRANCH"
git reset --hard "$NEW_SHA"
if ! validate_checkout; then rollback "candidate V7 deterministic validation failed"; fi
if ! bash "$APP_DIR/ops/apply_runtime_config_macos.sh"; then rollback "candidate V7 runtime configuration failed"; fi
sudo -n /usr/local/sbin/polymarket-service-control restart || rollback "candidate V7 service restart failed"
if ! wait_for_runtime_health; then
  capture_runtime_health_diagnostics "$NEW_SHA"
  rollback "post-deploy V7 runtime health checks failed"
fi

write_status deployed "$NEW_SHA" "$NEW_SHA" "$MAIN_SHA"
log "Deployed healthy V7 paper-validated revision $NEW_SHA"
