#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:?POLYMARKET_APP_DIR is required}"
RUN_ROOT="${PM_V7_RUN_ROOT:?PM_V7_RUN_ROOT is required}"
EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
STATUS="$RUN_ROOT/control/supervisor_status.json"

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
