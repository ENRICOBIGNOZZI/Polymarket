#!/usr/bin/env bash
set -euo pipefail
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA required}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
SOURCE_DIR="${POLYMARKET_APP_DIR:-/home/$SERVICE_USER/polymarket}"
RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:-/home/$SERVICE_USER/polymarket-runtime}"
TARGET="$RUNTIME_ROOT/by-sha/$EXPECTED_SHA"
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 78; }
[[ "$(uname -s)" == Linux ]] || { echo "London stage requires Linux" >&2; exit 78; }
python3 -c 'import numpy' >/dev/null 2>&1 || { echo "python3 numpy required for London verification stage" >&2; exit 78; }
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
GIT_SOURCE=(git -c "safe.directory=$SOURCE_DIR" -C "$SOURCE_DIR")
[[ -e "$SOURCE_DIR/.git" && "$("${GIT_SOURCE[@]}" rev-parse --is-inside-work-tree 2>/dev/null)" == true ]] || { echo "source checkout missing" >&2; exit 66; }
[[ "$("${GIT_SOURCE[@]}" rev-parse HEAD)" == "$EXPECTED_SHA" ]] || { echo "source SHA mismatch" >&2; exit 66; }
[[ -z "$("${GIT_SOURCE[@]}" status --porcelain)" ]] || { echo "dirty source checkout" >&2; exit 66; }
mkdir -p "$RUNTIME_ROOT/by-sha"
# Full verification build is staging-only; it never runs on the trading path.
rm -rf "$SOURCE_DIR/build-verify" "$SOURCE_DIR/build-runtime"
cmake -S "$SOURCE_DIR" -B "$SOURCE_DIR/build-verify" -GNinja -DCMAKE_BUILD_TYPE=Release
verify_build_log="$SOURCE_DIR/build-verify/london-stage-build.log"
if ! cmake --build "$SOURCE_DIR/build-verify" --parallel "${POLYMARKET_BUILD_JOBS:-2}" >"$verify_build_log" 2>&1; then
  echo "London verification build failed" >&2
  tail -n 160 "$verify_build_log" >&2 || true
  exit 8
fi
verify_test_log="$SOURCE_DIR/build-verify/london-stage-ctest.log"
if ! ctest --test-dir "$SOURCE_DIR/build-verify" --output-on-failure >"$verify_test_log" 2>&1; then
  echo "London verification ctest failed" >&2
  failed_list="$SOURCE_DIR/build-verify/Testing/Temporary/LastTestsFailed.log"
  if [[ -s "$failed_list" ]]; then
    echo "London failed tests:" >&2
    cat "$failed_list" >&2
  fi
  ctest --test-dir "$SOURCE_DIR/build-verify" --rerun-failed --output-on-failure >&2 || true
  exit 8
fi
cmake -S "$SOURCE_DIR" -B "$SOURCE_DIR/build-runtime" -GNinja -DCMAKE_BUILD_TYPE=Release -DPM_LONDON_RUNTIME_ONLY=ON -DBUILD_TESTING=OFF
runtime_build_log="$SOURCE_DIR/build-runtime/london-runtime-build.log"
if ! cmake --build "$SOURCE_DIR/build-runtime" --parallel "${POLYMARKET_BUILD_JOBS:-2}" >"$runtime_build_log" 2>&1; then
  echo "London runtime-only build failed" >&2
  tail -n 160 "$runtime_build_log" >&2 || true
  exit 8
fi
tmp="$TARGET.tmp.$$"; rm -rf "$tmp"
python3 "$SOURCE_DIR/ops/build_london_runtime_bundle.py" --repository-root "$SOURCE_DIR" \
  --build-dir "$SOURCE_DIR/build-runtime" --expected-sha "$EXPECTED_SHA" --output "$tmp"
[[ "$(cat "$tmp/deploy/london/runtime_sha")" == "$EXPECTED_SHA" ]]
[[ ! -e "$tmp/research" && ! -e "$tmp/tests" ]]
rm -rf "$TARGET"; mv "$tmp" "$TARGET"
printf 'stage_result=success\nsha=%s\nruntime_dir=%s\n' "$EXPECTED_SHA" "$TARGET"
