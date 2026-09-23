#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:?POLYMARKET_APP_DIR is required}"
RUN_ROOT="${PM_V7_RUN_ROOT:?PM_V7_RUN_ROOT is required}"
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
STATUS="$RUN_ROOT/control/supervisor_status.json"

# MACOS_SHARED_MONOTONIC_PREFLIGHT
# Python before 3.10 uses a process-local monotonic epoch on macOS. Fair-value
# validity timestamps cross worker boundaries and require the shared epoch.
python3 - <<'PY'
import sys
if sys.platform == "darwin" and sys.version_info < (3, 10):
    print("V7 blocked: macOS requires Python >=3.10 for cross-process monotonic timestamps; configure the service PATH to the validated interpreter", file=sys.stderr)
    raise SystemExit(78)
PY
# END_MACOS_SHARED_MONOTONIC_PREFLIGHT


[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact 40-character SHA required" >&2; exit 78; }
[[ "$(cat "$APP_DIR/deploy/london/runtime_sha" 2>/dev/null || true)" == "$EXPECTED_SHA" ]] || { echo "staged runtime SHA drift" >&2; exit 78; }

# London keeps PID 1 / ordinary system work on a housekeeping affinity. The
# PAPER supervisor must first reopen the service process to every online CPU;
# the runtime resource planner then deterministically pins HOT/COLLECTOR/
# CONTROL/LATENCY classes. Without this step, the planner only sees PID 1's
# inherited housekeeping mask and falsely concludes that the 8-core host has
# too few CPUs.
if [[ "$(uname -s)" == "Linux" ]] && command -v taskset >/dev/null 2>&1     && [[ -r /sys/devices/system/cpu/online ]]; then
  online_cpus="$(tr -d '[:space:]' </sys/devices/system/cpu/online)"
  [[ "$online_cpus" =~ ^[0-9,-]+$ ]] || {
    echo "invalid online CPU set: $online_cpus" >&2
    exit 78
  }
  taskset -pc "$online_cpus" "$" >/dev/null || {
    echo "cannot expand PAPER service CPU affinity to online set: $online_cpus" >&2
    exit 78
  }
fi

# A true safety quarantine remains operator-controlled. Restart-budget exhaustion
# is different: the supervisor stays fail-closed through its bounded cooldown and
# retries automatically when the exact-SHA window expires.
if [[ -f "$STATUS" ]]; then
  state="$(python3 - "$STATUS" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1],encoding='utf-8')).get('state',''))
except Exception: print('')
PY
)"
  case "$state" in
    quarantined) exit 0 ;;
  esac
fi

exec python3 "$APP_DIR/ops/v7_runtime_supervisor.py" \
  --repository-root "$APP_DIR" \
  --run-root "$RUN_ROOT" \
  --policy "$APP_DIR/config/v7_runtime_supervision.json" \
  --expected-sha "$EXPECTED_SHA"
