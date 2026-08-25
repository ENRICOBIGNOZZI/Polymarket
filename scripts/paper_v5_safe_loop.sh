#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

CONFIG="${1:-config/paper_v5.json}"
RUN_ROOT="${2:-runs/paper_v5_live}"
mkdir -p "$RUN_ROOT"

read -r GENERIC_SCAN_ONLY STALE_SECONDS GRACE_SECONDS REPORT_INTERVAL_SECONDS <<EOF
$(python3 - "$CONFIG" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
config = json.loads(path.read_text(encoding="utf-8"))
operability = config.get("multi_strategy", {}).get("operability", {})
scan_only = bool(operability.get("generic_children_scan_only", False))
stale = float(operability.get("child_stale_seconds", 600.0))
grace = float(operability.get("watchdog_grace_seconds", 180.0))
report = float(operability.get("report_interval_seconds", 30.0))
if not scan_only:
    raise SystemExit("multi_strategy.operability.generic_children_scan_only must be true")
if stale <= 0 or grace < 0 or report <= 0:
    raise SystemExit("invalid V5 operability timing")
print("true", stale, grace, report)
PY
)
EOF

GENERATED_LOOP="$RUN_ROOT/paper_v5_loop.safe.generated.sh"
python3 - "$GENERATED_LOOP" <<'PY'
from pathlib import Path
import os
import sys

target = Path(sys.argv[1])
source_path = Path("scripts/paper_v5_loop.sh")
source = source_path.read_text(encoding="utf-8")
needle = "python3 scripts/multi_strategy_paper.py"
count = source.count(needle)
if count < 2:
    raise SystemExit(f"expected at least two allocator launch sites, found {count}")
source = source.replace(needle, "python3 scripts/multi_strategy_paper_safe.py")
temporary = target.with_suffix(target.suffix + ".tmp")
temporary.write_text(source, encoding="utf-8")
os.replace(temporary, target)
PY
chmod 700 "$GENERATED_LOOP"

MAIN_PID=0
WATCHDOG_PID=0
REPORT_PID=0
WATCHDOG_RESTARTS=0
REPORT_RESTARTS=0

append_event() {
  local component="$1"
  local event="$2"
  local count="$3"
  local path="$RUN_ROOT/safe_loop_events.csv"
  if [[ ! -s "$path" ]]; then
    printf 'timestamp,component,event,restart_count\n' > "$path"
  fi
  printf '%s,%s,%s,%s\n' "$(date +%s)" "$component" "$event" "$count" >> "$path"
}

start_watchdog() {
  python3 scripts/v5_stale_watchdog.py \
    --run-root "$RUN_ROOT" \
    --stale-seconds "$STALE_SECONDS" \
    --grace-seconds "$GRACE_SECONDS" \
    --interval-seconds 5 \
    >> "$RUN_ROOT/stale_watchdog.log" 2>&1 &
  WATCHDOG_PID=$!
}

start_report() {
  python3 scripts/model_operability_report.py \
    --config "$CONFIG" \
    --run-root "$RUN_ROOT" \
    --window-seconds 3600 \
    --stale-seconds "$STALE_SECONDS" \
    --interval-seconds "$REPORT_INTERVAL_SECONDS" \
    --loop >> "$RUN_ROOT/model_operability.log" 2>&1 &
  REPORT_PID=$!
}

cleanup() {
  trap - EXIT INT TERM
  for pid in "$REPORT_PID" "$WATCHDOG_PID" "$MAIN_PID"; do
    if (( pid > 0 )); then kill "$pid" 2>/dev/null || true; fi
  done
  for pid in "$REPORT_PID" "$WATCHDOG_PID" "$MAIN_PID"; do
    if (( pid > 0 )); then wait "$pid" 2>/dev/null || true; fi
  done
}

trap cleanup EXIT INT TERM

bash "$GENERATED_LOOP" "$CONFIG" "$RUN_ROOT" >> "$RUN_ROOT/safe_loop_main.log" 2>&1 &
MAIN_PID=$!
start_watchdog
start_report
append_event main start 0
append_event watchdog start 0
append_event report start 0

while true; do
  if ! kill -0 "$MAIN_PID" 2>/dev/null; then
    set +e
    wait "$MAIN_PID"
    rc=$?
    set -e
    append_event main exit "$rc"
    exit "$rc"
  fi

  if ! kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    wait "$WATCHDOG_PID" 2>/dev/null || true
    WATCHDOG_RESTARTS=$((WATCHDOG_RESTARTS + 1))
    append_event watchdog restart "$WATCHDOG_RESTARTS"
    sleep 1
    start_watchdog
  fi

  if ! kill -0 "$REPORT_PID" 2>/dev/null; then
    wait "$REPORT_PID" 2>/dev/null || true
    REPORT_RESTARTS=$((REPORT_RESTARTS + 1))
    append_event report restart "$REPORT_RESTARTS"
    sleep 1
    start_report
  fi

  sleep 2
done
