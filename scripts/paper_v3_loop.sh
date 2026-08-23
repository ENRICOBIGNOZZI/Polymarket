#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v3.json}"
RUN_ROOT="${2:-runs/paper_v3_live}"
mkdir -p "$RUN_ROOT/maker" "$RUN_ROOT/terminal"

last_structural=0
last_stat=0

while true; do
  now=$(date +%s)

  # Execution experiment: frequent because it follows the live book.
  ./build/polymarket_maker_paper \
    --config "$CONFIG" \
    --run-dir "$RUN_ROOT/maker" \
    --markets 600 \
    --min-liquidity 100 \
    --min-edge 0.003 \
    --max-order-usd 75 \
    --ttl-seconds 300 \
    --hold-seconds 180 \
    --adverse-selection-mult 0.50 \
    --once >> "$RUN_ROOT/maker.log" 2>&1 || true

  # Structural arbitrage: faster than statistical refits, but not every maker tick.
  if (( now - last_structural >= 30 )); then
    ./build/polymarket_negrisk_arb \
      --config "$CONFIG" \
      --markets 600 \
      --min-liquidity 100 \
      --top 60 > "$RUN_ROOT/structural_latest.csv" 2> "$RUN_ROOT/structural_errors.log" || true
    last_structural=$now
  fi

  # Statistical arbitrage: deliberately slower, multi-day state and 30-minute history buckets.
  if (( now - last_stat >= 900 )); then
    ./build/polymarket_stat_arb \
      --config "$CONFIG" \
      --markets 600 \
      --history-universe 160 \
      --lookback-hours 336 \
      --fidelity-minutes 30 \
      --min-z 1.5 \
      --max-half-life-hours 168 \
      --top 60 \
      --csv "$RUN_ROOT/stat_arb.csv" > "$RUN_ROOT/stat_arb_latest.log" 2> "$RUN_ROOT/stat_arb_errors.log" || true
    last_stat=$now
  fi

  sleep 10
done
