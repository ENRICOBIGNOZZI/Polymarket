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
APP_UPDATER_LOCK_AWARE=0

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

release_deploy_lock() {
  if [[ "$DEPLOY_LOCK_HELD" != "1" || -z "$DEPLOY_LOCK_TOKEN" ]]; then
    return 0
  fi
  local recorded=""
  recorded="$(sed -n 's/^token=//p' "$DEPLOY_LOCK_DIR/owner.env" 2>/dev/null | head -n 1 || true)"
  if [[ "$recorded" == "$DEPLOY_LOCK_TOKEN" ]]; then
    rm -rf "$DEPLOY_LOCK_DIR"
    log "Released deployment mutex token=$DEPLOY_LOCK_TOKEN"
  fi
  DEPLOY_LOCK_HELD=0
}

acquire_deploy_lock() {
  mkdir -p "$CACHE_DIR"
  local now deadline acquired owner
  now="$(date +%s)"
  deadline=$((now + DEPLOY_LOCK_WAIT_SECONDS))
  DEPLOY_LOCK_TOKEN="updater-$$-$now"
  while ! mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; do
    now="$(date +%s)"
    acquired="$(sed -n 's/^acquired_ts=//p' "$DEPLOY_LOCK_DIR/owner.env" 2>/dev/null | head -n 1 || true)"
    owner="$(sed -n 's/^token=//p' "$DEPLOY_LOCK_DIR/owner.env" 2>/dev/null | head -n 1 || true)"
    if [[ "$acquired" =~ ^[0-9]+$ ]] && (( now - acquired > DEPLOY_LOCK_STALE_SECONDS )); then
      log "Reclaiming stale deployment mutex token=${owner:-unknown} age_seconds=$((now-acquired))"
      rm -rf "$DEPLOY_LOCK_DIR"
      continue
    fi
    if (( now >= deadline )); then
      fail "deployment mutex busy token=${owner:-unknown}; refusing overlapping checkout mutation"
    fi
    sleep 2
  done
  DEPLOY_LOCK_HELD=1
  {
    printf 'token=%s\n' "$DEPLOY_LOCK_TOKEN"
    printf 'pid=%s\n' "$$"
    printf 'acquired_ts=%s\n' "$now"
    printf 'deploy_ref=%s\n' "$DEPLOY_REF"
  } > "$DEPLOY_LOCK_DIR/owner.env"
  log "Acquired deployment mutex token=$DEPLOY_LOCK_TOKEN"
}

wait_for_legacy_updater() {
  [[ "$APP_UPDATER_LOCK_AWARE" == "0" ]] || return 0
  local deadline now pids pid other
  deadline=$(( $(date +%s) + DEPLOY_LOCK_WAIT_SECONDS ))
  while :; do
    other=""
    pids="$(/usr/bin/pgrep -f "$APP_DIR/ops/update_server_macos.sh" 2>/dev/null || true)"
    for pid in $pids; do
      [[ "$pid" == "$$" ]] && continue
      other="${other}${other:+,}$pid"
    done
    [[ -z "$other" ]] && return 0
    now="$(date +%s)"
    if (( now >= deadline )); then
      fail "legacy pre-mutex updater still running pid=${other}; refusing concurrent checkout mutation"
    fi
    log "Waiting for pre-mutex launchd updater pid=${other} to finish"
    sleep 2
  done
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
from pathlib import Path, PurePosixPath

m = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
version = m.get("version")
if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
    raise SystemExit("invalid champion version")
values = [str(m.get(key, "")) for key in ("run_root", "config", "loop")]
for value in values:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit("invalid champion path")
print("\t".join((str(version), *values)))
PY
}

dashboard_uid() {
  "$PYTHON_BIN" - "$APP_DIR/config/project_context.json" <<'PY'
import json, sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(p["grafana"]["dashboard_uid"])
PY
}

request_runtime_handoff() {
  local target_sha="$1" meta version run_root_rel config_rel loop_rel marker tmp
  meta="$(champion_meta)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  [[ "$run_root_rel" =~ ^runs/[A-Za-z0-9._/-]+$ ]] || return 1
  marker="$APP_DIR/$run_root_rel/runtime_handoff.request"
  mkdir -p "$(dirname "$marker")"
  tmp="$marker.tmp.$$"
  {
    printf 'target_sha=%s\n' "$target_sha"
    printf 'requested_ts=%s\n' "$(date +%s)"
    printf 'reason=validated_deploy_handoff\n'
  } > "$tmp"
  mv "$tmp" "$marker"
  log "Requested runtime-owner handoff for $run_root_rel"
}

clear_runtime_handoff() {
  local meta version run_root_rel config_rel loop_rel
  meta="$(champion_meta)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  rm -f "$APP_DIR/$run_root_rel/runtime_handoff.request"
}

paper_runtime_healthy() {
  local meta version run_root_rel config_rel loop_rel
  meta="$(champion_meta 2>/dev/null)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"

  if (( version == 5 )); then
    "$PYTHON_BIN" "$APP_DIR/scripts/v5_runtime_readiness.py" \
      --run-root "$APP_DIR/$run_root_rel" \
      --supervisor-max-age 60 \
      --allocator-max-age 30 \
      --model-output-max-age 120 \
      --startup-grace 600 >/dev/null
    return
  fi

  "$PYTHON_BIN" "$APP_DIR/scripts/runtime_contract_health.py" \
    --manifest "$APP_DIR/config/live_champion.json" \
    --repository-root "$APP_DIR" \
    --max-age-seconds 180 >/dev/null
}

full_runtime_healthy() {
  local meta version run_root_rel config_rel loop_rel run_name metrics uid
  meta="$(champion_meta 2>/dev/null)" || return 1
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  run_name="$(basename "$run_root_rel")"
  uid="$(dashboard_uid 2>/dev/null)" || return 1

  curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 || return 1
  metrics="$(curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null)" || return 1
  grep -Eq "^polymarket_runtime_info\\{adapter=\"[^\"]+\",run_root=\"$run_name\",version=\"v$version\"\\} 1$" <<<"$metrics" || return 1
  grep -q '^polymarket_runtime_pnl_usd ' <<<"$metrics" || return 1
  grep -q '^polymarket_runtime_equity_usd ' <<<"$metrics" || return 1
  if (( version >= 6 )); then
    grep -q '^polymarket_runtime_contract_present 1$' <<<"$metrics" || return 1
  fi
  curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 || return 1
  curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1 || return 1
  curl -fsS "http://127.0.0.1:3000/api/dashboards/uid/$uid" >/dev/null 2>&1 || return 1
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

capture_runtime_health_diagnostics() {
  local target_sha="$1"
  if [[ -f "$APP_DIR/ops/capture_runtime_health_macos.sh" ]]; then
    bash "$APP_DIR/ops/capture_runtime_health_macos.sh" "$target_sha" || log "candidate health diagnostics collector failed"
  else
    log "candidate health diagnostics collector missing"
  fi
}

validate_candidate() {
  local source="$1" version run_root_rel config_rel loop_rel meta jobs
  meta="$("$PYTHON_BIN" - "$source/config/live_champion.json" <<'PY'
import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"{int(m['version'])}\t{m['run_root']}\t{m['config']}\t{m['loop']}")
PY
)"
  IFS=$'\t' read -r version run_root_rel config_rel loop_rel <<<"$meta"
  [[ "$version" =~ ^[1-9][0-9]*$ ]] || return 1
  [[ -f "$source/$config_rel" && -f "$source/$loop_rel" ]] || return 1

  cd "$source"
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
  jobs="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
  cmake --build build --parallel "$jobs"
  ctest --test-dir build --output-on-failure
  "$PYTHON_BIN" -m unittest \
    tests/test_monitoring_exporter.py \
    tests/test_monitoring_latest_exporter.py \
    tests/test_runtime_contract_health.py \
    tests/test_grafana_multi_strategy_contract.py -v
  "$PYTHON_BIN" -m py_compile \
    monitoring/exporter.py monitoring/exporter_latest.py \
    scripts/runtime_contract_health.py
  bash -n scripts/paper_latest_loop.sh "$loop_rel" \
    ops/apply_runtime_config_macos.sh ops/capture_runtime_health_macos.sh
  "$PYTHON_BIN" -m json.tool "$config_rel" >/dev/null
  "$PYTHON_BIN" -m json.tool config/live_champion.json >/dev/null
  "$PYTHON_BIN" -m json.tool monitoring/grafana/dashboards/polymarket-multi-strategy.json >/dev/null
}

[[ "$(uname -s)" == "Darwin" ]] || fail "This updater is for macOS only"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ -f "$APP_DIR/.server_bootstrapped_macos" ]] || fail "run ops/bootstrap_macos.sh interactively once first"
BREW_BIN="$(find_brew)" || fail "Homebrew is required (checked PATH, /opt/homebrew/bin/brew, /usr/local/bin/brew)"
[[ "$DEPLOY_LOCK_WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_LOCK_WAIT_SECONDS must be a positive integer"
[[ "$DEPLOY_LOCK_STALE_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_DEPLOY_LOCK_STALE_SECONDS must be a positive integer"
[[ "$RUNTIME_HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS must be a positive integer"
if grep -Eq '^POLYMARKET_DEPLOY_LOCK_V[0-9]+=1$' "$APP_DIR/ops/update_server_macos.sh" 2>/dev/null; then
  APP_UPDATER_LOCK_AWARE=1
fi

acquire_deploy_lock
trap release_deploy_lock EXIT INT TERM
wait_for_legacy_updater

BREW_PREFIX="$("$BREW_BIN" --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PKG_CONFIG_PATH="$("$BREW_BIN" --prefix curl)/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
PYTHON_BIN="$BREW_PREFIX/bin/python3"

cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$LOCAL_BRANCH" "$DEPLOY_REF"
MAIN_SHA="$(git rev-parse "origin/$LOCAL_BRANCH")"
NEW_SHA="$(git rev-parse "origin/$DEPLOY_REF")"
git merge-base --is-ancestor "$NEW_SHA" "$MAIN_SHA" || fail "$DEPLOY_REF ($NEW_SHA) is not an ancestor of $LOCAL_BRANCH ($MAIN_SHA)"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  if full_runtime_healthy; then
    write_status up_to_date "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    log "Already deployed and healthy at validated commit $NEW_SHA"
    exit 0
  fi
  log "Validated code is current but runtime is unhealthy; repairing monitoring/runtime configuration"
  bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || {
    write_status unhealthy "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    fail "runtime configuration repair failed"
  }
  request_runtime_handoff "$NEW_SHA" || fail "could not request runtime-owner handoff"
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  if wait_for_runtime_health; then
    write_status repaired "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
    log "Runtime and Grafana configuration repaired at validated commit $NEW_SHA"
    exit 0
  fi
  capture_runtime_health_diagnostics "$NEW_SHA"
  write_status unhealthy "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  fail "automatic repair did not restore runtime health"
fi

if git merge-base --is-ancestor "$NEW_SHA" "$OLD_SHA" 2>/dev/null; then
  write_status awaiting_validation "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  log "Current checkout is ahead of validated ref; waiting for validation"
  exit 0
fi

mkdir -p "$CACHE_DIR"
STAGE="$(mktemp -d "$CACHE_DIR/stage.XXXXXX")"
STAGE_SRC="$STAGE/src"
CONFIG_BACKUP="$STAGE/state-backup"
cleanup() {
  git -C "$APP_DIR" worktree remove --force "$STAGE_SRC" >/dev/null 2>&1 || true
  rm -rf "$STAGE"
  release_deploy_lock
}
trap cleanup EXIT INT TERM

log "Validating candidate $NEW_SHA in isolated worktree"
git -C "$APP_DIR" worktree add --detach "$STAGE_SRC" "$NEW_SHA" >/dev/null
validate_candidate "$STAGE_SRC"

log "Candidate validation passed; staging production build"
wait_for_legacy_updater
JOBS="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
cd "$APP_DIR"
git checkout "$LOCAL_BRANCH"
git reset --hard "$NEW_SHA"
rm -rf build.next
cmake -S . -B build.next -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
cmake --build build.next --parallel "$JOBS"

mkdir -p "$CONFIG_BACKUP"
if [[ -f "$STATE_DIR/grafana.ini" ]]; then
  cp "$STATE_DIR/grafana.ini" "$CONFIG_BACKUP/grafana.ini"
fi
if [[ -d "$STATE_DIR/grafana" ]]; then
  cp -R "$STATE_DIR/grafana" "$CONFIG_BACKUP/grafana"
fi

rollback() {
  local reason="$1"
  log "ROLLBACK: $reason"
  cd "$APP_DIR"
  git reset --hard "$OLD_SHA" || true
  if [[ -d build.previous ]]; then
    rm -rf build
    mv build.previous build
  fi
  rm -f "$STATE_DIR/grafana.ini"
  rm -rf "$STATE_DIR/grafana"
  if [[ -f "$CONFIG_BACKUP/grafana.ini" ]]; then
    cp "$CONFIG_BACKUP/grafana.ini" "$STATE_DIR/grafana.ini"
  fi
  if [[ -d "$CONFIG_BACKUP/grafana" ]]; then
    cp -R "$CONFIG_BACKUP/grafana" "$STATE_DIR/grafana"
  fi
  write_status rollback "$OLD_SHA" "$NEW_SHA" "$MAIN_SHA"
  clear_runtime_handoff || true
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  exit 1
}

rm -rf build.previous
if [[ -d build ]]; then mv build build.previous; fi
mv build.next build

log "Applying version-neutral runtime/Grafana configuration"
bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || rollback "runtime configuration failed"

log "Restarting manifest-selected PAPER services"
request_runtime_handoff "$NEW_SHA" || rollback "could not request runtime-owner handoff"
sudo -n /usr/local/sbin/polymarket-service-control restart || rollback "service restart failed"

log "Waiting for generic runtime contract health"
if ! wait_for_runtime_health; then
  capture_runtime_health_diagnostics "$NEW_SHA"
  rollback "post-deploy runtime contract health checks failed"
fi

FINAL_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"
if [[ "$FINAL_SHA" != "$NEW_SHA" ]]; then
  rollback "checkout moved during serialized deployment: actual=$FINAL_SHA expected=$NEW_SHA"
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
