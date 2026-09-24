#!/usr/bin/env bash
set -euo pipefail
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA required}"
SERVICE_USER="${POLYMARKET_SERVICE_USER:-enrico}"
SOURCE_DIR="${POLYMARKET_APP_DIR:-/home/$SERVICE_USER/polymarket}"
RUNTIME_ROOT="${POLYMARKET_RUNTIME_ROOT:-/home/$SERVICE_USER/polymarket-runtime}"
TARGET="$RUNTIME_ROOT/by-sha/$EXPECTED_SHA"
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 78; }
[[ "$(uname -s)" == Linux ]] || { echo "London stage requires Linux" >&2; exit 78; }
REUSE_EXACT_SHA_CI="${POLYMARKET_REUSE_EXACT_SHA_CI:-0}"
CI_REPOSITORY="${PM_V7_CI_REPOSITORY:-ENRICOBIGNOZZI/Polymarket}"
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
rm -rf "$SOURCE_DIR/build-verify" "$SOURCE_DIR/build-runtime"

# Fast path: the exact immutable code SHA has already passed the full GitHub
# release matrix. Re-prove those check-runs from London, then avoid rebuilding
# and re-running the same 343-test suite a second time on the deployment host.
# The fallback remains the original full local verification.
if [[ "$REUSE_EXACT_SHA_CI" == 1 ]]; then
  CI_RECEIPT_DIR="$RUNTIME_ROOT/ci-receipts"
  mkdir -p "$CI_RECEIPT_DIR"
  python3 "$SOURCE_DIR/scripts/v7_exact_sha_ci_gate.py" \
    --repository "$CI_REPOSITORY" --sha "$EXPECTED_SHA" \
    --required-check ci-v7-Release \
    --required-check ci-v7-Debug \
    --required-check sanitizer-v7 \
    --required-check security-audit-v7 \
    --required-check london-runtime-boundary-v7 \
    --required-check monitoring-v7 \
    --required-check single-writer-v7 \
    --output "$CI_RECEIPT_DIR/$EXPECTED_SHA.json"
  printf 'london_stage_verification=REUSED_EXACT_SHA_CI\n'
else
  python3 -c 'import numpy' >/dev/null 2>&1 || { echo "python3 numpy required for London verification stage" >&2; exit 78; }
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
  printf 'london_stage_verification=FULL_LOCAL_CI\n'
fi

# by-sha is immutable. A retry of the same exact SHA must never remove or
# rewrite the release currently referenced by systemd/current.
if [[ -e "$TARGET" || -L "$TARGET" ]]; then
  [[ -d "$TARGET" && ! -L "$TARGET" ]] || { echo "existing exact-SHA runtime target is unsafe" >&2; exit 66; }
  python3 - "$TARGET/runtime_bundle_receipt.json" "$TARGET/deploy/london/runtime_sha" "$EXPECTED_SHA" <<'PYREUSE'
import json,sys
receipt_path,sha_path,expected=sys.argv[1:]
v=json.load(open(receipt_path,encoding='utf-8'))
assert v.get('schema')=='polymarket_v7_london_runtime_bundle_receipt_v1'
assert v.get('runtime_sha')==expected
assert v.get('paper_only') is True
assert v.get('authenticated_execution') is False
assert v.get('real_order_submission') is False
assert v.get('research_tree_present') is False
assert v.get('source_tree_verified') is True
assert open(sha_path,encoding='utf-8').read().strip()==expected
PYREUSE
  for rel in ops/v7_service_entrypoint.sh ops/v7_runtime_supervisor.py ops/systemd/polymarket-v7-paper.service.in; do
    cmp -s "$TARGET/$rel" "$SOURCE_DIR/$rel" || {
      echo "existing exact-SHA runtime target differs from immutable source: $rel" >&2
      exit 66
    }
  done
  printf 'stage_result=reused_immutable_exact_sha\nsha=%s\nruntime_dir=%s\n' "$EXPECTED_SHA" "$TARGET"
  exit 0
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
if [[ -e "$TARGET" || -L "$TARGET" ]]; then
  echo "immutable exact-SHA runtime target appeared during staging" >&2
  rm -rf "$tmp"
  exit 66
fi
mv "$tmp" "$TARGET"
printf 'stage_result=success\nsha=%s\nruntime_dir=%s\n' "$EXPECTED_SHA" "$TARGET"
