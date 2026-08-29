#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v3.json}"
RUN_ROOT="${2:-runs/paper_v3}"
mkdir -p "$RUN_ROOT"

# Strategy A: structural constraints / NegRisk. Read-only.
./build/polymarket_negrisk_arb \
  --config "$CONFIG" \
  --markets 600 \
  --min-liquidity 100 \
  --top 60 \
  | tee "$RUN_ROOT/structural_arb.log"

# Strategy B1: pair-residual multi-horizon relative value.
./build/polymarket_stat_arb \
  --config "$CONFIG" \
  --markets 600 \
  --history-universe 160 \
  --lookback-hours 336 \
  --fidelity-minutes 30 \
  --min-z 1.5 \
  --max-half-life-hours 168 \
  --top 60 \
  --csv "$RUN_ROOT/stat_arb_pairs.csv" \
  | tee "$RUN_ROOT/stat_arb_pairs.log"

# Strategy B2: timestamp-aligned PCA/factor residual relative value.
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
  --csv "$RUN_ROOT/stat_arb_pca.csv" \
  | tee "$RUN_ROOT/stat_arb_pca.log"

# Conservative paper maker execution experiment. Never submits an authenticated order.
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
  --once \
  | tee "$RUN_ROOT/maker_tick.log"

# Terminal-probability sleeve: V3 config disables micro and legacy PCA in Kelly ensemble.
./build/polymarket_engine \
  --config "$CONFIG" \
  --once \
  --scan-only \
  --markets 600 \
  --min-liquidity 100 \
  --run-dir "$RUN_ROOT/terminal" \
  | tee "$RUN_ROOT/terminal_scan.log"
