#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v4.json}"
RUN_ROOT="${2:-runs/paper_v4}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/terminal"

./build/polymarket_trade_recorder \
  --config "$CONFIG" \
  --run-dir "$RUN_ROOT" \
  --markets 400 --batch 40 --min-liquidity 100 \
  --lookback-seconds 120 --once \
  | tee "$RUN_ROOT/trade_recorder_latest.log"

./build/polymarket_negrisk_arb \
  --config "$CONFIG" --markets 600 --min-liquidity 100 --top 60 \
  | tee "$RUN_ROOT/structural_latest.log" "$RUN_ROOT/structural_latest.csv"

./build/polymarket_stat_arb \
  --config "$CONFIG" --markets 600 --history-universe 160 \
  --lookback-hours 336 --fidelity-minutes 30 --min-z 1.5 \
  --max-half-life-hours 168 --top 60 \
  --csv "$RUN_ROOT/stat_arb_pairs.csv" \
  | tee "$RUN_ROOT/stat_arb_pairs_latest.log"

./build/polymarket_pca_stat_arb \
  --config "$CONFIG" --markets 600 --universe 120 \
  --lookback-hours 336 --fidelity-minutes 30 --factors 3 --max-hedges 4 --min-z 1.5 \
  --max-half-life-hours 168 --top 60 \
  --csv "$RUN_ROOT/stat_arb_pca.csv" \
  | tee "$RUN_ROOT/stat_arb_pca_latest.log"

python3 scripts/build_v4_intents.py \
  --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" --output "$RUN_ROOT/b1_intents.csv" \
  --config "$CONFIG" --min-edge 0.001
python3 scripts/build_v4_intents.py \
  --strategy B2 --input "$RUN_ROOT/stat_arb_pca.csv" --output "$RUN_ROOT/b2_intents.csv" \
  --config "$CONFIG" --min-edge 0.001
python3 scripts/merge_v4_intents.py \
  --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
  --output "$RUN_ROOT/intents.csv" --min-edge 0.001 --max-age-seconds 600 --max-bundles 20

./build/polymarket_multileg_paper \
  --config "$CONFIG" --run-dir "$RUN_ROOT" \
  --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" \
  --min-edge 0.001 --completion-threshold 0.95 \
  --submit-latency-ms 250 --cancel-latency-ms 250 --max-replaces 3 \
  --max-leg-risk-usd 5 --adverse-horizon-seconds 60 --once \
  | tee "$RUN_ROOT/multileg_latest.log"

./build/polymarket_maker_paper \
  --config "$CONFIG" --run-dir "$RUN_ROOT/maker" \
  --markets 240 --min-liquidity 100 --min-edge 0.003 --max-order-usd 50 \
  --ttl-seconds 300 --hold-seconds 180 --adverse-selection-mult 0.50 --once \
  | tee "$RUN_ROOT/maker_latest.log"

./build/polymarket_engine \
  --config "$CONFIG" --once --scan-only --markets 240 --min-liquidity 100 \
  --run-dir "$RUN_ROOT/terminal" | tee "$RUN_ROOT/terminal_latest.log"

python3 scripts/walk_forward_v4.py \
  --ledger "$RUN_ROOT/bundle_ledger.csv" --output "$RUN_ROOT/walk_forward.json" \
  --starting-capital 10000 || true
