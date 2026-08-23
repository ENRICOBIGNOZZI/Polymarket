#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
BRANCH="${POLYMARKET_BRANCH:-main}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-deploy}"
STATE_DIR="${POLYMARKET_STATE_DIR:-$HOME/.config/polymarket}"
STATUS_FILE="$STATE_DIR/autoupdate_status.env"

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
  local status="$1" head_sha="$2" origin_sha="$3"
  mkdir -p "$STATE_DIR"
  local tmp
  tmp="$(mktemp "$STATE_DIR/autoupdate_status.XXXXXX")"
  {
    printf 'checked_ts=%s\n' "$(date +%s)"
    printf 'status=%s\n' "$status"
    printf 'head=%s\n' "$head_sha"
    printf 'origin=%s\n' "$origin_sha"
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

[[ "$(uname -s)" == "Darwin" ]] || fail "This updater is for macOS only"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ -f "$APP_DIR/.server_bootstrapped_macos" ]] || fail "run ops/bootstrap_macos.sh interactively once first"
BREW_BIN="$(find_brew)" || fail "Homebrew is required (checked PATH, /opt/homebrew/bin/brew, /usr/local/bin/brew)"

BREW_PREFIX="$("$BREW_BIN" --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PKG_CONFIG_PATH="$("$BREW_BIN" --prefix curl)/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
PYTHON_BIN="$BREW_PREFIX/bin/python3"

cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$BRANCH"
NEW_SHA="$(git rev-parse "origin/$BRANCH")"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  write_status up_to_date "$OLD_SHA" "$NEW_SHA"
  log "Already deployed at $NEW_SHA"
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

log "Validating candidate $NEW_SHA in isolated worktree"
git -C "$APP_DIR" worktree add --detach "$STAGE_SRC" "$NEW_SHA" >/dev/null
cd "$STAGE_SRC"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
JOBS="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 2)"
cmake --build build --parallel "$JOBS"
ctest --test-dir build --output-on-failure
"$PYTHON_BIN" -m unittest \
  tests/test_monitoring_exporter.py tests/test_monitoring_v4_exporter.py tests/test_monitoring_latest_exporter.py -v
"$PYTHON_BIN" -m py_compile \
  monitoring/exporter.py monitoring/exporter_v4.py monitoring/exporter_latest.py \
  scripts/build_v4_intents.py scripts/merge_v4_intents.py scripts/walk_forward_v4.py scripts/tiny_live_pilot.py
bash -n scripts/paper_latest_loop.sh scripts/paper_v4_once.sh scripts/paper_v4_loop.sh \
  ops/apply_runtime_config_macos.sh

log "Candidate validation passed; staging production build"
cd "$APP_DIR"
git checkout "$BRANCH"
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
  write_status rollback "$OLD_SHA" "$NEW_SHA"
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  exit 1
}

rm -rf build.previous
if [[ -d build ]]; then mv build build.previous; fi
mv build.next build

log "Applying versioned runtime configuration"
bash "$APP_DIR/ops/apply_runtime_config_macos.sh" || rollback "runtime configuration failed"

log "Restarting latest-runtime services"
sudo -n /usr/local/sbin/polymarket-service-control restart || rollback "service restart failed"

log "Waiting for production health"
healthy=0
for _ in {1..45}; do
  if curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null | grep -q '^polymarket_runtime_info' && \
     curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:3000/api/search >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
[[ "$healthy" -eq 1 ]] || rollback "post-deploy health checks failed"

rm -rf build.previous
write_status deployed "$NEW_SHA" "$NEW_SHA"
printf 'deployed_sha=%s\n' "$NEW_SHA"
printf 'previous_sha=%s\n' "$OLD_SHA"
log "Deployment healthy"
