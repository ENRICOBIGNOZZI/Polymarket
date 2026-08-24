#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SUPERVISOR_CONFIG="${PORTFOLIO_SUPERVISOR_CONFIG:-$ROOT/config/portfolio_supervisor.json}"
SYSTEM_RUN_DIR="${PREDICTION_SYSTEM_RUN_DIR:-$ROOT/runs/system}"
START_ALPHA="${PREDICTION_SYSTEM_START_ALPHA:-1}"
START_CROSS="${PREDICTION_SYSTEM_START_CROSS:-1}"
mkdir -p "$SYSTEM_RUN_DIR" "$ROOT/runs/supervisor" "$ROOT/runs/cross_venue"

supervisor_pid=0
alpha_pid=0
cross_pid=0
supervisor_restarts=0
alpha_restarts=0
cross_restarts=0

start_supervisor() {
  python3 "$ROOT/scripts/portfolio_supervisor.py" --config "$SUPERVISOR_CONFIG" --loop \
    >> "$SYSTEM_RUN_DIR/supervisor.log" 2>&1 &
  supervisor_pid=$!
}

start_alpha() {
  if [[ "$START_ALPHA" != "1" ]]; then return; fi
  bash "$ROOT/scripts/paper_latest_loop.sh" \
    >> "$SYSTEM_RUN_DIR/alpha.log" 2>&1 &
  alpha_pid=$!
}

start_cross() {
  if [[ "$START_CROSS" != "1" ]]; then return; fi
  bash "$ROOT/scripts/cross_venue_loop.sh" \
    >> "$SYSTEM_RUN_DIR/cross_venue.log" 2>&1 &
  cross_pid=$!
}

append_event() {
  local component="$1" event="$2" count="$3"
  local path="$SYSTEM_RUN_DIR/events.csv"
  [[ -s "$path" ]] || printf 'timestamp,component,event,restart_count\n' > "$path"
  printf '%s,%s,%s,%s\n' "$(date +%s)" "$component" "$event" "$count" >> "$path"
}

write_status() {
  local supervisor_alive=0 alpha_alive=0 cross_alive=0
  (( supervisor_pid > 0 )) && kill -0 "$supervisor_pid" 2>/dev/null && supervisor_alive=1
  [[ "$START_ALPHA" != "1" ]] || { (( alpha_pid > 0 )) && kill -0 "$alpha_pid" 2>/dev/null && alpha_alive=1; }
  [[ "$START_CROSS" != "1" ]] || { (( cross_pid > 0 )) && kill -0 "$cross_pid" 2>/dev/null && cross_alive=1; }
  local tmp="$SYSTEM_RUN_DIR/runtime_planes.csv.tmp"
  printf 'timestamp,supervisor_alive,alpha_enabled,alpha_alive,cross_enabled,cross_alive,supervisor_restarts,alpha_restarts,cross_restarts,supervisor_pid,alpha_pid,cross_pid\n' > "$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$(date +%s)" "$supervisor_alive" "$START_ALPHA" "$alpha_alive" "$START_CROSS" "$cross_alive" \
    "$supervisor_restarts" "$alpha_restarts" "$cross_restarts" \
    "$supervisor_pid" "$alpha_pid" "$cross_pid" >> "$tmp"
  mv "$tmp" "$SYSTEM_RUN_DIR/runtime_planes.csv"
}

cleanup() {
  for pid in "$cross_pid" "$alpha_pid" "$supervisor_pid"; do
    if (( pid > 0 )); then kill "$pid" 2>/dev/null || true; fi
  done
  for pid in "$cross_pid" "$alpha_pid" "$supervisor_pid"; do
    if (( pid > 0 )); then wait "$pid" 2>/dev/null || true; fi
  done
}

shutdown() {
  trap - EXIT INT TERM
  cleanup
  exit 0
}

trap cleanup EXIT
trap shutdown INT TERM

start_supervisor
append_event supervisor start 0
for _ in $(seq 1 50); do
  [[ -s "$ROOT/runs/supervisor/capital_limits.json" ]] && break
  kill -0 "$supervisor_pid" 2>/dev/null || break
  sleep 0.1
done
start_alpha
[[ "$START_ALPHA" == "1" ]] && append_event alpha start 0
start_cross
[[ "$START_CROSS" == "1" ]] && append_event cross_venue start 0
write_status

while true; do
  if ! kill -0 "$supervisor_pid" 2>/dev/null; then
    wait "$supervisor_pid" 2>/dev/null || true
    supervisor_restarts=$((supervisor_restarts + 1))
    append_event supervisor restart "$supervisor_restarts"
    sleep 1
    start_supervisor
  fi
  if [[ "$START_ALPHA" == "1" ]] && ! kill -0 "$alpha_pid" 2>/dev/null; then
    wait "$alpha_pid" 2>/dev/null || true
    alpha_restarts=$((alpha_restarts + 1))
    append_event alpha restart "$alpha_restarts"
    sleep 1
    start_alpha
  fi
  if [[ "$START_CROSS" == "1" ]] && ! kill -0 "$cross_pid" 2>/dev/null; then
    wait "$cross_pid" 2>/dev/null || true
    cross_restarts=$((cross_restarts + 1))
    append_event cross_venue restart "$cross_restarts"
    sleep 1
    start_cross
  fi
  write_status
  sleep 5
done
