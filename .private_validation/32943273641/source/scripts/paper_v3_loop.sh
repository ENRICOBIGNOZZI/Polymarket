#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v3.json}"
RUN_ROOT="${2:-runs/paper_v3_live}"
mkdir -p "$RUN_ROOT/maker" "$RUN_ROOT/terminal"

last_structural=0
last_stat=0

while true; do
  now=$(date +%s)

  # Conservative maker execution experiment: frequent because it follows the live book.
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

  # Strategy A: structural arbitrage.
  if (( now - last_structural >= 30 )); then
    ./build/polymarket_negrisk_arb \
      --config "$CONFIG" \
      --markets 600 \
      --min-liquidity 100 \
      --top 60 > "$RUN_ROOT/structural_latest.csv" 2> "$RUN_ROOT/structural_errors.log" || true
    last_structural=$now
  fi

  # Strategy B: slower multi-day refits for both independent statistical sleeves.
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
      --csv "$RUN_ROOT/stat_arb_pairs.csv" > "$RUN_ROOT/stat_arb_pairs_latest.log" 2> "$RUN_ROOT/stat_arb_pairs_errors.log" || true

    ./build/polymarket_pca_stat_arb \
      --config "$CONFIG" \
      --markets 600 \
      --universe 120 \
      --lookback-hours 336 \
      --fidelity-minutes 30 \
      --factors 3 \
      --min-z 1.5 \
      --max-half-life-hours 168 \
      --top 60 \
      --csv "$RUN_ROOT/stat_arb_pca.csv" > "$RUN_ROOT/stat_arb_pca_latest.log" 2> "$RUN_ROOT/stat_arb_pca_errors.log" || true
    last_stat=$now
  fi

  sleep 10
done
