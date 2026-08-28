#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_DIR="${POLYMARKET_LOCAL_LIVE_DIR:-$HOME/.local/share/polymarket-v7-live}"
STATE_DIR="${POLYMARKET_LOCAL_STATE_DIR:-$HOME/.config/polymarket-local-v7}"
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:-}"
INSTALL_DEPS="${POLYMARKET_LOCAL_INSTALL_DEPS:-0}"

log(){ printf '[v7-local] %s\n' "$*"; }
fail(){ printf '[v7-local] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "local fallback requires macOS"
[[ -d "$ROOT/.git" ]] || fail "$ROOT is not the primary git checkout"
[[ "$LIVE_DIR" != "$ROOT" ]] || fail "local live checkout must be isolated from the development checkout"
[[ "$INSTALL_DEPS" == "0" || "$INSTALL_DEPS" == "1" ]] || fail "POLYMARKET_LOCAL_INSTALL_DEPS must be 0 or 1"

for cmd in git python3 curl; do
  command -v "$cmd" >/dev/null 2>&1 || fail "missing required command: $cmd"
done

if ! command -v brew >/dev/null 2>&1; then
  fail "Homebrew is required for the macOS V7 build/monitoring stack"
fi

missing=""
for cmd in cmake pkg-config prometheus grafana; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    missing="$missing $cmd"
  fi
done
if [[ -n "$missing" ]]; then
  if [[ "$INSTALL_DEPS" == "1" ]]; then
    log "Installing missing local dependencies with Homebrew:$missing"
    brew install cmake pkg-config prometheus grafana
  else
    fail "missing local dependencies:$missing (set POLYMARKET_LOCAL_INSTALL_DEPS=1 to install with Homebrew)"
  fi
fi

log "Fetching canonical V7 refs"
git -C "$ROOT" fetch --no-tags origin main paper-validated
MAIN_SHA="$(git -C "$ROOT" rev-parse origin/main)"
VALIDATED_SHA="$(git -C "$ROOT" rev-parse origin/paper-validated)"
[[ "$MAIN_SHA" == "$VALIDATED_SHA" ]] || fail "main=$MAIN_SHA differs from paper-validated=$VALIDATED_SHA"

if [[ -z "$EXPECTED_SHA" ]]; then EXPECTED_SHA="$VALIDATED_SHA"; fi
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "POLYMARKET_EXPECTED_SHA must be an exact 40-char SHA"
[[ "$EXPECTED_SHA" == "$MAIN_SHA" ]] || fail "requested SHA is not current exact validated main"

REMOTE_URL="$(git -C "$ROOT" remote get-url origin)"
mkdir -p "$(dirname "$LIVE_DIR")" "$STATE_DIR"
if [[ ! -d "$LIVE_DIR/.git" ]]; then
  [[ ! -e "$LIVE_DIR" ]] || fail "$LIVE_DIR exists but is not an isolated git clone"
  log "Creating isolated local live checkout at $LIVE_DIR"
  git clone --no-hardlinks "$ROOT" "$LIVE_DIR"
fi
[[ -d "$LIVE_DIR/.git" ]] || fail "$LIVE_DIR is not a standalone git checkout"
[[ -z "$(git -C "$LIVE_DIR" status --porcelain --untracked-files=no)" ]] || fail "isolated live checkout has tracked local changes"
git -C "$LIVE_DIR" remote set-url origin "$REMOTE_URL"
git -C "$LIVE_DIR" fetch --no-tags origin main paper-validated
[[ "$(git -C "$LIVE_DIR" rev-parse origin/main)" == "$EXPECTED_SHA" ]] || fail "local clone origin/main is not expected SHA"
[[ "$(git -C "$LIVE_DIR" rev-parse origin/paper-validated)" == "$EXPECTED_SHA" ]] || fail "local clone origin/paper-validated is not expected SHA"

UPDATER="$(mktemp "${TMPDIR:-/tmp}/polymarket-v7-local-updater.XXXXXX")"
cleanup(){ rm -f "$UPDATER"; }
trap cleanup EXIT INT TERM
git -C "$LIVE_DIR" show "$EXPECTED_SHA:ops/update_server_v7.sh" > "$UPDATER"
chmod 700 "$UPDATER"

log "Starting exact-SHA V7 PAPER runtime locally"
POLYMARKET_APP_DIR="$LIVE_DIR" \
POLYMARKET_STATE_DIR="$STATE_DIR" \
EXPECTED_VALIDATED_SHA="$EXPECTED_SHA" \
POLYMARKET_DEPLOY_REF=paper-validated \
POLYMARKET_MAIN_REF=main \
bash "$UPDATER"

curl -fsS http://127.0.0.1:9108/healthz >/dev/null || fail "local exporter is unhealthy"
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null || fail "local Prometheus is unhealthy"
curl -fsS http://127.0.0.1:3000/api/health >/dev/null || fail "local Grafana is unhealthy"

RUN_ROOT="$LIVE_DIR/runs/paper_v7_live"
python3 - "$RUN_ROOT" "$EXPECTED_SHA" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]
runtime=json.loads((root/'control/runtime_status.json').read_text(encoding='utf-8'))
assert runtime.get('version') == 7
assert runtime.get('model_sha') == sha
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False
maker=json.loads((root/'micro_maker/state.json').read_text(encoding='utf-8'))
assert maker.get('paper_only') is True
assert maker.get('authenticated_execution') is False
assert maker.get('real_order_submission') is False
assert maker.get('model_sha') == sha
PY

log "Local V7 Maker PAPER runtime is healthy on exact SHA $EXPECTED_SHA"
printf 'grafana=http://127.0.0.1:3000/d/polymarket-v7\n'
printf 'run_root=%s\n' "$RUN_ROOT"
printf 'live_checkout=%s\n' "$LIVE_DIR"
printf 'state_dir=%s\n' "$STATE_DIR"
