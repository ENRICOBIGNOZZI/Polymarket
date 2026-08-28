#!/usr/bin/env bash
# Canonical local/CI verification for the single PAPER-only V7 system.
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPOSITORY_ROOT"

BUILD_ROOT="${V7_VERIFY_BUILD_ROOT:-$REPOSITORY_ROOT/build-verify-v7}"
RELEASE_BUILD="$BUILD_ROOT/Release"
DEBUG_BUILD="$BUILD_ROOT/Debug"
JOBS="${V7_VERIFY_JOBS:-2}"
LATENCY_SAMPLES="${V7_VERIFY_LATENCY_SAMPLES:-200000}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "verify_v7: missing required command: $1" >&2
    exit 2
  }
}

for command_name in git python3 cmake c++ pkg-config; do
  require_command "$command_name"
done

python3 - <<'PY'
import json
from pathlib import Path

for root in (Path("config"), Path("monitoring")):
    for path in sorted(root.rglob("*.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"verify_v7: invalid JSON {path}: {exc}")
print("verify_v7: config/json validation passed")
PY

python3 -m py_compile scripts/v7_*.py monitoring/exporter_v7.py \
  monitoring/exporter_v7_fillability.py monitoring/v7_ledger_metrics.py \
  monitoring/v7_maker_fillability.py monitoring/v7_maker_fillability_exact.py
bash -n scripts/paper_v7_execution_loop.sh
bash -n ops/update_server_v7.sh
bash -n scripts/verify_v7.sh

# Fail on high-confidence credential material in tracked text.  This is local,
# deterministic and does not print a matching secret into logs.
python3 - <<'PY'
import re
import subprocess
from pathlib import Path

patterns = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
)
tracked = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
bad = []
for raw in tracked:
    if not raw:
        continue
    path = Path(raw.decode("utf-8"))
    try:
        payload = path.read_bytes()
    except OSError:
        continue
    if any(pattern.search(payload) for pattern in patterns):
        bad.append(path.as_posix())
if bad:
    raise SystemExit("verify_v7: possible tracked credential material: " + ", ".join(sorted(bad)))
print("verify_v7: tracked-secret scan passed")
PY

python3 - <<'PY'
import re
from pathlib import Path

unpinned = []
for path in sorted(Path(".github/workflows").glob("*.yml")):
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if not match or match.group(1).startswith("./"):
            continue
        reference = match.group(1)
        if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference):
            unpinned.append(f"{path}:{line_number}:{reference}")
if unpinned:
    raise SystemExit("verify_v7: GitHub Actions must use exact commit SHAs: " + ", ".join(unpinned))
print("verify_v7: GitHub Action pin validation passed")
PY

python3 tests/test_no_legacy_runtime.py
python3 tests/test_v7_single_writer_contract.py
python3 -m unittest discover -s tests -p 'test_v7_*manifest.py'
python3 scripts/v7_convergence_audit.py --repository-root . >/dev/null

cmake -S . -B "$RELEASE_BUILD" -DCMAKE_BUILD_TYPE=Release
cmake --build "$RELEASE_BUILD" --parallel "$JOBS"
ctest --test-dir "$RELEASE_BUILD" --output-on-failure

cmake -S . -B "$DEBUG_BUILD" -DCMAKE_BUILD_TYPE=Debug
cmake --build "$DEBUG_BUILD" --parallel "$JOBS"
ctest --test-dir "$DEBUG_BUILD" --output-on-failure

# Keep these named groups explicit even though CTest also discovers them.  The
# command's contract remains readable and failures are attributable by domain.
python3 -m unittest discover -s tests -p 'test_v7_*.py'
python3 -m unittest discover -s tests -p 'test_monitoring_v7_*.py'

"$RELEASE_BUILD/polymarket_v7_maker_replay_bench" "$LATENCY_SAMPLES" \
  > "$RELEASE_BUILD/v7-latency-benchmark.json"
python3 scripts/v7_latency_gate.py \
  --config config/v7_latency_slo.json \
  --benchmark "$RELEASE_BUILD/v7-latency-benchmark.json" \
  --output "$RELEASE_BUILD/v7-latency-gate.json"

binary_args=()
while IFS= read -r binary; do
  binary_args+=(--binary "$binary")
done < <(find "$RELEASE_BUILD" -maxdepth 1 -type f -perm -111 -name 'polymarket_v7_*' | sort)
if ((${#binary_args[@]} == 0)); then
  echo "verify_v7: no canonical V7 binaries found" >&2
  exit 2
fi
python3 scripts/v7_build_manifest.py create \
  --output "$RELEASE_BUILD/build_manifest.json" \
  --repository-root "$REPOSITORY_ROOT" \
  --code-sha "$(git rev-parse HEAD)" \
  --timestamp "$(git show -s --format=%cI HEAD)" \
  --build-type Release \
  "${binary_args[@]}"
python3 scripts/v7_build_manifest.py validate "$RELEASE_BUILD/build_manifest.json" >/dev/null

if [[ "${V7_VERIFY_SANITIZERS:-0}" == "1" ]]; then
  SANITIZER_BUILD="$BUILD_ROOT/ASan-UBSan"
  cmake -S . -B "$SANITIZER_BUILD" -DCMAKE_BUILD_TYPE=Debug \
    -DCMAKE_CXX_FLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer" \
    -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address,undefined"
  cmake --build "$SANITIZER_BUILD" --parallel "$JOBS"
  ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 \
    ctest --test-dir "$SANITIZER_BUILD" --output-on-failure
fi

if [[ "${V7_VERIFY_TSAN:-0}" == "1" ]]; then
  TSAN_BUILD="$BUILD_ROOT/TSan"
  cmake -S . -B "$TSAN_BUILD" -DCMAKE_BUILD_TYPE=Debug \
    -DCMAKE_CXX_FLAGS="-fsanitize=thread -fno-omit-frame-pointer" \
    -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=thread"
  cmake --build "$TSAN_BUILD" --parallel "$JOBS"
  TSAN_OPTIONS=halt_on_error=1 ctest --test-dir "$TSAN_BUILD" --output-on-failure
fi

echo "verify_v7: PASS exact_sha=$(git rev-parse HEAD)"
