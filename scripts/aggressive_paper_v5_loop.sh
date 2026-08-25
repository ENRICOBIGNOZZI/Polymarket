#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v5.json}"
RUN_ROOT="${2:-runs/paper_v5_live}"
EXTERNAL_REFRESH_SECONDS="${V5_EXTERNAL_REFRESH_SECONDS:-300}"
EXTERNAL_MIN_CONFIDENCE="${V5_EXTERNAL_MIN_CONFIDENCE:-0.30}"
EXTERNAL_MAX_AGE_SECONDS="${V5_EXTERNAL_MAX_AGE_SECONDS:-43200}"

# Aggressive paper defaults: widen and accelerate discovery without converting
# cost-negative raw signals into executable intents.
export V5_MIN_LIQUIDITY="${V5_MIN_LIQUIDITY:-10}"
export V5_MODEL_MARKETS="${V5_MODEL_MARKETS:-1000}"
export V5_RECORDER_MARKETS="${V5_RECORDER_MARKETS:-1500}"
export V5_RECORDER_BATCH="${V5_RECORDER_BATCH:-80}"
export V5_RECORDER_LOOKBACK_SECONDS="${V5_RECORDER_LOOKBACK_SECONDS:-900}"
export V5_STAT_INTERVAL_SECONDS="${V5_STAT_INTERVAL_SECONDS:-60}"
export V5_STRUCTURAL_INTERVAL_SECONDS="${V5_STRUCTURAL_INTERVAL_SECONDS:-30}"
export V5_REWARD_INTERVAL_SECONDS="${V5_REWARD_INTERVAL_SECONDS:-180}"
export V5_REPORT_INTERVAL_SECONDS="${V5_REPORT_INTERVAL_SECONDS:-30}"
export V5_INTENT_MIN_EDGE="${V5_INTENT_MIN_EDGE:-0.00025}"

mkdir -p "$RUN_ROOT"

refresh_external_signals() {
  local input="$RUN_ROOT/latest-external-signals.jsonl"
  local temporary="$input.tmp"

  git fetch --no-tags origin telemetry:refs/remotes/origin/telemetry >/dev/null 2>&1 || true
  if ! git show origin/telemetry:telemetry/latest-external-signals.jsonl > "$temporary" 2>/dev/null; then
    if [[ -s telemetry/latest-external-signals.jsonl ]]; then
      cp telemetry/latest-external-signals.jsonl "$temporary"
    else
      : > "$temporary"
    fi
  fi
  mv "$temporary" "$input"

  python3 scripts/materialize_external_signals.py \
    --input "$input" \
    --output data/external_signals.csv \
    --min-confidence "$EXTERNAL_MIN_CONFIDENCE" \
    --max-age-seconds "$EXTERNAL_MAX_AGE_SECONDS" \
    >> "$RUN_ROOT/external_signal_materializer.log" 2>&1
}

external_refresh_loop() {
  while true; do
    refresh_external_signals || true
    sleep "$EXTERNAL_REFRESH_SECONDS"
  done
}

EXTERNAL_PID=0
LOOP_PID=0
cleanup() {
  if (( EXTERNAL_PID > 0 )); then kill "$EXTERNAL_PID" 2>/dev/null || true; fi
  if (( LOOP_PID > 0 )); then kill "$LOOP_PID" 2>/dev/null || true; fi
  if (( EXTERNAL_PID > 0 )); then wait "$EXTERNAL_PID" 2>/dev/null || true; fi
  if (( LOOP_PID > 0 )); then wait "$LOOP_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

refresh_external_signals || true
external_refresh_loop &
EXTERNAL_PID=$!

scripts/paper_v5_loop.sh "$CONFIG" "$RUN_ROOT" &
LOOP_PID=$!
wait "$LOOP_PID"
