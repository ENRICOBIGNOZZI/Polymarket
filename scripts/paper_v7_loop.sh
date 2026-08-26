#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG="${1:-config/paper_v7.json}"
RUN_ROOT="${2:-runs/paper_v7_live}"
EXECUTION_ROOT="$RUN_ROOT/execution"
SHADOW_ROOT="$RUN_ROOT/shadow"
mkdir -p "$EXECUTION_ROOT" "$SHADOW_ROOT"

execution_pid=0
shadow_pid=0

start_execution(){
  POLYMARKET_RUNTIME_PARENT_PID="$$" \
    bash scripts/paper_v7_execution_loop.sh "$CONFIG" "$EXECUTION_ROOT" \
    >>"$RUN_ROOT/execution_supervisor.log" 2>&1 &
  execution_pid=$!
}

start_shadow(){
  python3 scripts/v7_shadow_loop.py \
    --paper-config "$CONFIG" \
    --frequency-config config/v7_frequency_matrix.json \
    --run-root "$RUN_ROOT" \
    >>"$RUN_ROOT/shadow_supervisor.log" 2>&1 &
  shadow_pid=$!
}

cleanup(){
  local pid
  for pid in "$shadow_pid" "$execution_pid"; do
    if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$shadow_pid" "$execution_pid"; do
    if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}

shutdown(){
  trap - EXIT INT TERM
  cleanup
  exit 0
}

trap cleanup EXIT
trap shutdown INT TERM

start_execution
start_shadow

while true; do
  if ! kill -0 "$execution_pid" 2>/dev/null; then
    wait "$execution_pid" 2>/dev/null || true
    echo "fatal: V7 execution child exited" >&2
    exit 1
  fi
  if ! kill -0 "$shadow_pid" 2>/dev/null; then
    wait "$shadow_pid" 2>/dev/null || true
    echo "warning: V7 shadow scheduler exited; restarting" >&2
    start_shadow
  fi
  tmp="$RUN_ROOT/v7_supervisor.json.tmp.${BASHPID:-$$}"
  printf '{"timestamp":%s,"execution_pid":%s,"shadow_pid":%s,"execution_alive":true,"shadow_alive":true}\n' \
    "$(date +%s)" "$execution_pid" "$shadow_pid" >"$tmp"
  mv "$tmp" "$RUN_ROOT/v7_supervisor.json"
  sleep 5
done
