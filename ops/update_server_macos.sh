#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
LOCAL_BRANCH="${POLYMARKET_BRANCH:-main}"
DEPLOY_REF="${POLYMARKET_DEPLOY_REF:-paper-validated}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-deploy}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
STATUS_FILE="$STATE_DIR/autoupdate_status.env"
RUNTIME_HEALTH_ATTEMPTS="${POLYMARKET_RUNTIME_HEALTH_ATTEMPTS:-180}"

log() { printf '[mac-deploy] %s\n' "$*"; }
fail() { printf '[mac-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

find_brew() {
  if command -v brew >/dev/null 2>&1; then
    command -v brew
  elif [[ -x /opt/homebrew/bin/brew ]]; then
    printf '%s\n' /opt/homebrew/bin/brew
  elif [[ -x /usr/local/bin/brew ]]; then
    printf '%s\n' /usr/local/bin/brew
  else
    return 1
  fi
}

write_status() {
  local status="$1" head_sha="$2" validated_sha="$3" main_sha="$4"
  mkdir -p "$STATE_DIR"
  local tmp
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
import json
import sys
from pathlib import Path
m = json.loads(Path(sys.argv[1]).read_text())
print(f"{m['version']}\t{m['run_root']}\t{m['config']}\t{m['loop']}")
PY
}

paper_runtime_healthy() {
  local meta version run_root_rel config_rel loop_rel supervisor
  meta="$(champion_meta 2>/dev/null)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  [[ "$version" =~ ^[0-9]+$ ]] || return 1
  supervisor="$APP_DIR/$run_root_rel/runtime_supervisor.csv"
  [[ -s "$supervisor" ]] || return 1
  "$PYTHON_BIN" - "$supervisor" "$version" "$APP_DIR/$run_root_rel" <<'PY'
import csv
import json
import math
import sys
import time
from pathlib import Path
supervisor = Path(sys.argv[1])
version = int(sys.argv[2])
run_root = Path(sys.argv[3])
with supervisor.open(newline='', encoding='utf-8') as handle:
    rows = list(csv.DictReader(handle))
assert rows, 'empty runtime supervisor'
row = rows[-1]
assert row.get('recorder_alive') == '1', row
assert row.get('broker_alive') == '1', row
assert row.get('allocator_alive' if version >= 5 else 'terminal_alive') == '1', row
assert time.time() - float(row['timestamp']) <= 60, row
if version >= 5:
    allocator = json.loads((run_root / 'allocator_status.json').read_text())
    with (run_root / 'strategy_status.csv').open(newline='', encoding='utf-8') as handle:
        strategies = list(csv.DictReader(handle))
    assert allocator.get('paper_only') is True, allocator
    assert int(allocator.get('models_expected', 0)) == 5, allocator
    assert int(allocator.get('models_alive', 0)) == 5, allocator
    assert {item.get('name') for item in strategies} == {'micro', 'pca', 'graph', 'semantic', 'external'}, strategies
    total = float(allocator.get('reserve_fraction', 0.0)) + sum(float(item['capital_fraction']) for item in strategies)
    assert math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9), (total, strategies)
    stale = {
        str(item.get('name')): float(item['status_age_seconds'])
        for item in strategies
        if float(item['status_age_seconds']) > 120
    }
    assert not stale, f'stale strategy status: {stale}'
PY
}

full_runtime_healthy() {
  local meta version run_root_rel config_rel loop_rel run_name metrics grafana_search
  meta="$(champion_meta 2>/dev/null)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  run_name="$(basename "$run_root_rel")"
  curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 || return 1
  metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null)" || return 1
  grep -q "^polymarket_runtime_info{adapter=\"v$version\",run_root=\"$run_name\",version=\"v$version\"} 1$" <<<"$metrics" || return 1
  grep -q '^polymarket_runtime_pnl_usd ' <<<"$metrics" || return 1
  if (( version >= 5 )); then
    grep -q '^polymarket_allocator_state_present 1$' <<<"$metrics" || return 1
    grep -q '^polymarket_allocator_models_expected 5$' <<<"$metrics" || return 1
    grep -q '^polymarket_model_info{' <<<"$metrics" || return 1
  fi
  curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 || return 1
  curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1 || return 1
  grafana_search="$(curl -fsS http://127.0.0.1:3000/api/search 2>/dev/null)" || return 1
  if (( version >= 5 )); then
    grep -q 'polymarket-multi-strategy-v5' <<<"$grafana_search" || return 1
  fi
  paper_runtime_healthy
}

wait_for_runtime_health() {
  local attempts="${1:-$RUNTIME_HEALTH_ATTEMPTS}"
  local i
  for ((i=0; i<attempts; ++i)); do
    if full_runtime_healthy; then
      return 0
    fi
    sleep 2
  done
  return 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "This updater is for macOS only"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ -f "$APP_DIR/.server_bootstrapped_macos" ]] || fail "run ops/bootstrap_macos.sh interactively once first"
BREW_BIN="$(find_brew)" || fail "Homebrew is required (checked PATH, /opt/homebrew/bin/brew, /usr/local/bin/brew)"

BREW_PREFIX="$("$BREW_BIN" --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PKG_CONFIG_PATH="$("$BREW_BIN" --prefix curl)/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
PYTHON_BIN="$BREW_PREFIX/bin/python3"
[[ "$RUNTIME_HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be a positive integer"

cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  if full_runtime_healthy; then
    write_status up_to_date "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    log "Already deployed and healthy at validated commit $NEW_SHA"
    exit 0
  fi
  log "Validated code is current but runtime is unhealthy; reapplying runtime configuration before service repair"
  if ! bash "$APP_DIR/ops/apply_runtime_config_macos.sh"; then
    write_status unhealthy "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    fail "validated code is current but runtime configuration repair failed"
  fi
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  if wait_for_runtime_health; then
    write_status repaired "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    log "Runtime and Grafana configuration repaired at validated commit $NEW_SHA"
    exit 0
  fi
  write_status unhealthy "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  fail "validated code is current and automatic configuration/service repair did not restore health"
fi

if git merge-base --is-ancestor "$NEW_SHA" "$OLD_SHA" 2>/dev/null; then
  write_status awaiting_validation "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  log "Current checkout is ahead of validated ref; waiting for live-smoke validation"
  exit 0
fi

mkdir -p "$CACHE_DIR"
STAGE="$(mktemp -d "$CACHE_DIR/stage.XXXXXX")"
STAGE_SRC="$STAGE/src"
CONFIG_BACKUP="$STAGE/config-backup"
cleanup() {
  git -C "$APP_DIR" worktree remove --force "$STAGE_SRC" >/dev/null 2>&1 || true
  rm -rf "$STAGE"
}
trap cleanup EXIT

log "Validating candidate $NEW_SHA from $DEPLOY_REF in isolated worktree"
git -C "$APP_DIR" worktree add --detach "$STAGE_SRC" "$NEW_SHA" >/dev/null
cd "$STAGE_SRC"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
JOBS="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
cmake --build build --parallel "$JOBS"
ctest --test-dir build --output-on-failure
"$PYTHON_BIN" -m unittest \
  tests/test_monitoring_exporter.py tests/test_monitoring_v4_exporter.py \
  tests/test_monitoring_latest_exporter.py tests/test_monitoring_v5_exporter.py \
  tests/test_grafana_fast_paper_contract.py tests/test_grafana_multi_strategy_contract.py \
  tests/test_multi_strategy_paper.py -v
"$PYTHON_BIN" -m py_compile \
  monitoring/exporter.py monitoring/exporter_v4.py monitoring/exporter_v5.py monitoring/exporter_latest.py \
  scripts/multi_strategy_paper.py scripts/build_v4_intents.py scripts/merge_v4_intents.py \
  scripts/walk_forward_v4.py scripts/tiny_live_pilot.py
bash -n scripts/paper_latest_loop.sh scripts/paper_v5_loop.sh ops/apply_runtime_config_macos.sh
"$PYTHON_BIN" -m json.tool config/paper_v5.json >/dev/null
"$PYTHON_BIN" -m json.tool monitoring/grafana/dashboards/polymarket-multi-strategy.json >/dev/null

log "Candidate validation passed; staging production build"
cd "$APP_DIR"
git checkout "$LOCAL_BRANCH"
git reset --hard "$NEW_SHA"
rm -rf build.next
cmake -S . -B build.next -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
cmake --build build.next --parallel "$JOBS"

log "Snapshotting runtime config"
mkdir -p "$CONFIG_BACKUP/grafana/provisioning/datasources" \
  "$CONFIG_BACKUP/grafana/provisioning/dashboards"
for rel in \
  grafana.ini \
  grafana/provisioning/datasources/prometheus.yml \
  grafana/provisioning/dashboards/dashboards.yml; do
  if [[ -f "$STATE_DIR/$rel" ]]; then
    mkdir -p "$CONFIG_BACKUP/$(dirname "$rel")"
    cp "$STATE_DIR/$rel" "$CONFIG_BACKUP/$rel"
  fi
done

rollback() {
  local reason="$1"
  log "ROLLBACK: $reason"
  cd "$APP_DIR"
  git reset --hard "$OLD_SHA" || true
  if [[ -d build.previous ]]; then
    rm -rf build
    mv build.previous build
  fi
  for rel in \
    grafana.ini \
    grafana/provisioning/datasources/prometheus.yml \
    grafana/provisioning/dashboards/dashboards.yml; do
    rm -f "$STATE_DIR/$rel"
    if [[ -f "$CONFIG_BACKUP/$rel" ]]; then
      mkdir -p "$STATE_DIR/$(dirname "$rel")"
      cp "$CONFIG_BACKUP/$rel" "$STATE_DIR/$rel"
    fi
  done
  write_status rollback "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  exit 1
}

rm -rf build.previous
if [[ -d build ]]; then mv build build.previous; fi
mv build.next build

log "Applying V5-aware runtime configuration"
bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || rollback "runtime configuration failed"

log "Restarting manifest-selected paper services"
sudo -n /usr/local/sbin/polymarket-service-control restart || rollback "service restart failed"

log "Waiting for production health (up to $((RUNTIME_HEALTH_ATTEMPTS * 2)) seconds for cold-start model refresh)"
if ! wait_for_runtime_health; then
  rollback "post-deploy paper runtime health checks failed"
fi

rm -rf build.previous
write_status deployed "$NEW_SHA" "$NEW_SHA" "$MAIN_SHA"
meta="$(champion_meta)"
IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
printf 'deployed_sha=%s\n' "$NEW_SHA"
printf 'validated_ref=%s\n' "$DEPLOY_REF"
printf 'main_sha=%s\n' "$MAIN_SHA"
printf 'previous_sha=%s\n' "$OLD_SHA"
printf 'champion_version=%s\n' "$version"
printf 'champion_run_root=%s\n' "$run_root_rel"
log "Deployment healthy"
