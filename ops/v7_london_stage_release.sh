#!/usr/bin/env bash
set -euo pipefail
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA required}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
SOURCE_DIR="${POLYMARKET_APP_DIR:-/home/$SERVICE_USER/polymarket}"
RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:-/home/$SERVICE_USER/polymarket-runtime}"
TARGET="$RUNTIME_ROOT/by-sha/$EXPECTED_SHA"
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 78; }
[[ "$(uname -s)" == Linux ]] || { echo "London stage requires Linux" >&2; exit 78; }
[[ -d "$SOURCE_DIR/.git" ]] || { echo "source checkout missing" >&2; exit 66; }
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$EXPECTED_SHA" ]] || { echo "source SHA mismatch" >&2; exit 66; }
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || { echo "dirty source checkout" >&2; exit 66; }
mkdir -p "$RUNTIME_ROOT/by-sha"
# Full verification build is staging-only; it never runs on the trading path.
rm -rf "$SOURCE_DIR/build-verify" "$SOURCE_DIR/build-runtime"
cmake -S "$SOURCE_DIR" -B "$SOURCE_DIR/build-verify" -GNinja -DCMAKE_BUILD_TYPE=Release
cmake --build "$SOURCE_DIR/build-verify" --parallel "${POLYMARKET_BUILD_JOBS:-2}"
ctest --test-dir "$SOURCE_DIR/build-verify" --output-on-failure
cmake -S "$SOURCE_DIR" -B "$SOURCE_DIR/build-runtime" -GNinja -DCMAKE_BUILD_TYPE=Release -DPM_LONDON_RUNTIME_ONLY=ON -DBUILD_TESTING=OFF
cmake --build "$SOURCE_DIR/build-runtime" --parallel "${POLYMARKET_BUILD_JOBS:-2}"
tmp="$TARGET.tmp.$$"; rm -rf "$tmp"
python3 "$SOURCE_DIR/ops/build_london_runtime_bundle.py" --repository-root "$SOURCE_DIR" \
  --build-dir "$SOURCE_DIR/build-runtime" --expected-sha "$EXPECTED_SHA" --output "$tmp"
[[ "$(cat "$tmp/deploy/london/runtime_sha")" == "$EXPECTED_SHA" ]]
[[ ! -e "$tmp/research" && ! -e "$tmp/tests" ]]
rm -rf "$TARGET"; mv "$tmp" "$TARGET"
printf 'stage_result=success\nsha=%s\nruntime_dir=%s\n' "$EXPECTED_SHA" "$TARGET"
