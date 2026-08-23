#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
BRANCH="${POLYMARKET_BRANCH:-main}"
CACHE_DIR="${POLYMARKET_DEPLOY_CACHE:-$HOME/.cache/polymarket-deploy}"

log() { printf '[mac-deploy] %s\n' "$*"; }
fail() { printf '[mac-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "This updater is for macOS only"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"
[[ -f "$APP_DIR/.server_bootstrapped_macos" ]] || fail "run ops/bootstrap_macos.sh interactively once first"
command -v brew >/dev/null 2>&1 || fail "Homebrew is required"

BREW_PREFIX="$(brew --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PKG_CONFIG_PATH="$(brew --prefix curl)/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
PYTHON_BIN="$BREW_PREFIX/bin/python3"

cd "$APP_DIR"
OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$BRANCH"
NEW_SHA="$(git rev-parse "origin/$BRANCH")"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
  log "Already deployed at $NEW_SHA"
  exit 0
fi

mkdir -p "$CACHE_DIR"
STAGE="$(mktemp -d "$CACHE_DIR/stage.XXXXXX")"
STAGE_SRC="$STAGE/src"
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
bash -n scripts/paper_latest_loop.sh scripts/paper_v4_once.sh scripts/paper_v4_loop.sh

log "Candidate validation passed; staging production build"
cd "$APP_DIR"
git checkout "$BRANCH"
git reset --hard "$NEW_SHA"
rm -rf build.next
cmake -S . -B build.next -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$BREW_PREFIX"
cmake --build build.next --parallel "$JOBS"

rollback() {
  local reason="$1"
  log "ROLLBACK: $reason"
  cd "$APP_DIR"
  git reset --hard "$OLD_SHA" || true
  if [[ -d build.previous ]]; then
    rm -rf build
    mv build.previous build
  fi
  sudo -n /usr/local/sbin/polymarket-service-control restart || true
  exit 1
}

rm -rf build.previous
if [[ -d build ]]; then mv build build.previous; fi
mv build.next build

log "Restarting latest-runtime services"
sudo -n /usr/local/sbin/polymarket-service-control restart || rollback "service restart failed"

log "Waiting for production health"
healthy=0
for _ in {1..45}; do
  if curl -fsS http://127.0.0.1:9108/healthz >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:9108/metrics 2>/dev/null | grep -q '^polymarket_runtime_info' && \
     curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1 && \
     curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
[[ "$healthy" -eq 1 ]] || rollback "post-deploy health checks failed"

rm -rf build.previous
printf 'deployed_sha=%s\n' "$NEW_SHA"
printf 'previous_sha=%s\n' "$OLD_SHA"
log "Deployment healthy"
