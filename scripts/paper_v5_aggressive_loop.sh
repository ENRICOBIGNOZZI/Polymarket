#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v5.json}"
RUN_ROOT="${2:-runs/paper_v5_live}"
SYNC_INTERVAL_SECONDS="${V5_EXTERNAL_SYNC_INTERVAL_SECONDS:-300}"
TELEMETRY_PATH="telemetry/latest-external-signals.jsonl"
LOCAL_JSONL="$RUN_ROOT/latest_external_signals.jsonl"
OUTPUT_CSV="data/external_signals.csv"
mkdir -p "$RUN_ROOT" data

sync_external_once() {
  local temporary="$LOCAL_JSONL.tmp"
  if git fetch -q --no-tags origin telemetry:refs/remotes/origin/telemetry \
    && git show "origin/telemetry:$TELEMETRY_PATH" > "$temporary" 2>/dev/null; then
    mv "$temporary" "$LOCAL_JSONL"
  else
    rm -f "$temporary"
  fi

  python3 scripts/sync_external_signals.py \
    --input "$LOCAL_JSONL" --output "$OUTPUT_CSV" \
    --max-age-seconds 21600 --max-source-age-seconds 43200 \
    --min-confidence 0.20 --min-mapping-score 0.50 \
    --shrink-strength 1.35 --max-probability-gap 0.45 --max-signals 500 \
    >> "$RUN_ROOT/external_signal_sync.log" 2>&1 || true
}

external_sync_loop() {
  while true; do
    sync_external_once
    sleep "$SYNC_INTERVAL_SECONDS"
  done
}

sync_external_once
external_sync_loop &
SYNC_PID=$!

cleanup() {
  kill "$SYNC_PID" 2>/dev/null || true
  wait "$SYNC_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

bash scripts/paper_v5_loop.sh "$CONFIG" "$RUN_ROOT"
