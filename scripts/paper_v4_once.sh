#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v4.json}"
RUN_ROOT="${2:-runs/paper_v4}"
ALPHA_CONFIG="${ALPHA_CONFIG:-config/alpha_research.json}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/terminal"
eval "$(python3 scripts/alpha_config_env.py --config "$ALPHA_CONFIG")"

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
  --config "$CONFIG" --markets "$B1_MARKETS" --history-universe "$B1_HISTORY_UNIVERSE" \
  --lookback-hours "$B1_LOOKBACK_HOURS" --fidelity-minutes "$B1_FIDELITY_MINUTES" --min-z "$B1_MIN_Z" \
  --max-half-life-hours "$B1_MAX_HALF_LIFE_HOURS" --min-t-reversion "$B1_MIN_T_REVERSION" --top "$B1_TOP" \
  --csv "$RUN_ROOT/stat_arb_pairs.csv" \
  | tee "$RUN_ROOT/stat_arb_pairs_latest.log"

./build/polymarket_pca_stat_arb \
  --config "$CONFIG" --markets "$B2_MARKETS" --universe "$B2_UNIVERSE" \
  --lookback-hours "$B2_LOOKBACK_HOURS" --fidelity-minutes "$B2_FIDELITY_MINUTES" \
  --factors "$B2_FACTORS" --max-hedges "$B2_MAX_HEDGES" --min-z "$B2_MIN_Z" \
  --max-half-life-hours "$B2_MAX_HALF_LIFE_HOURS" --min-t-reversion "$B2_MIN_T_REVERSION" \
  --max-factor-hedge-error "$B2_MAX_FACTOR_HEDGE_ERROR" --top "$B2_TOP" \
  --csv "$RUN_ROOT/stat_arb_pca.csv" \
  | tee "$RUN_ROOT/stat_arb_pca_latest.log"

python3 scripts/build_v4_intents.py \
  --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" --output "$RUN_ROOT/b1_intents.csv" \
  --config "$CONFIG" --min-edge "$B1_EXECUTION_MIN_EDGE"
python3 scripts/build_v4_intents.py \
  --strategy B2 --input "$RUN_ROOT/stat_arb_pca.csv" --output "$RUN_ROOT/b2_intents.csv" \
  --config "$CONFIG" --min-edge "$B2_EXECUTION_MIN_EDGE"
python3 scripts/merge_v4_intents.py \
  --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
  --output "$RUN_ROOT/intents.csv" --min-edge "$ALPHA_MERGE_MIN_EDGE" --max-age-seconds 600 --max-bundles 20

./build/polymarket_multileg_paper \
  --config "$CONFIG" --run-dir "$RUN_ROOT" \
  --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" \
  --min-edge "$ALPHA_MERGE_MIN_EDGE" --completion-threshold 0.95 \
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
