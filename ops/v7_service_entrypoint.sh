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
[[ "$(git -C "$APP_DIR" rev-parse HEAD)" == "$EXPECTED_SHA" ]] || { echo "checkout SHA drift" >&2; exit 78; }

# Native service managers must not turn an explicitly quarantined state into an
# unbounded restart loop.  Exit successfully until an operator reconciles and
# removes the quarantine status through the controlled deployment path.
if [[ -f "$STATUS" ]]; then
  state="$(python3 - "$STATUS" <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1],encoding='utf-8')).get('state',''))
except Exception: print('')
PY
)"
  case "$state" in
    quarantined|restart_budget_exhausted) exit 0 ;;
  esac
fi

exec python3 "$APP_DIR/ops/v7_runtime_supervisor.py" \
  --repository-root "$APP_DIR" \
  --run-root "$RUN_ROOT" \
  --policy "$APP_DIR/config/v7_runtime_supervision.json" \
  --expected-sha "$EXPECTED_SHA"
