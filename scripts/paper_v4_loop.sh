#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v4.json}"
RUN_ROOT="${2:-runs/paper_v4_live}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/terminal"

# Create a valid empty broker input before starting the long-lived processes.
python3 scripts/merge_v4_intents.py \
  --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
  --output "$RUN_ROOT/intents.csv" --max-bundles 20 >/dev/null

./build/polymarket_trade_recorder \
  --config "$CONFIG" --run-dir "$RUN_ROOT" \
  --markets 400 --batch 40 --min-liquidity 100 --lookback-seconds 120 \
  --interval 10 --loop >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
REC_PID=$!

./build/polymarket_multileg_paper \
  --config "$CONFIG" --run-dir "$RUN_ROOT" \
  --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" \
  --min-edge 0.001 --completion-threshold 0.95 \
  --submit-latency-ms 250 --cancel-latency-ms 250 --max-replaces 3 \
  --max-leg-risk-usd 5 --adverse-horizon-seconds 60 --interval 3 --loop \
  >> "$RUN_ROOT/multileg.log" 2>&1 &
BROKER_PID=$!

cleanup() {
  kill "$REC_PID" "$BROKER_PID" 2>/dev/null || true
  wait "$REC_PID" "$BROKER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

last_stat=0
last_structural=0
last_terminal=0
last_oos=0

while true; do
  now=$(date +%s)

  # Short-horizon single-market maker diagnostic (separate from B1/B2 bundle broker).
  ./build/polymarket_maker_paper \
    --config "$CONFIG" --run-dir "$RUN_ROOT/maker" --markets 240 --min-liquidity 100 \
    --min-edge 0.003 --max-order-usd 50 --ttl-seconds 300 --hold-seconds 180 \
    --adverse-selection-mult 0.50 --once >> "$RUN_ROOT/maker.log" 2>&1 || true

  if (( now - last_stat >= 900 )); then
    ./build/polymarket_stat_arb \
      --config "$CONFIG" --markets 600 --history-universe 160 \
      --lookback-hours 336 --fidelity-minutes 30 --min-z 1.5 --max-half-life-hours 168 --top 60 \
      --csv "$RUN_ROOT/stat_arb_pairs.csv" --intents "$RUN_ROOT/b1_intents.csv" --intent-min-edge 0.001 \
      > "$RUN_ROOT/stat_arb_pairs_latest.log" 2> "$RUN_ROOT/stat_arb_pairs_errors.log" || true
    ./build/polymarket_pca_stat_arb \
      --config "$CONFIG" --markets 600 --universe 120 \
      --lookback-hours 336 --fidelity-minutes 30 --factors 3 --min-z 1.5 --max-half-life-hours 168 --top 60 \
      --csv "$RUN_ROOT/stat_arb_pca.csv" --intents "$RUN_ROOT/b2_intents.csv" --intent-min-edge 0.001 \
      > "$RUN_ROOT/stat_arb_pca_latest.log" 2> "$RUN_ROOT/stat_arb_pca_errors.log" || true
    python3 scripts/merge_v4_intents.py \
      --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
      --output "$RUN_ROOT/intents.csv" --min-edge 0.001 --max-age-seconds 600 --max-bundles 20 \
      >> "$RUN_ROOT/intent_merge.log" 2>&1 || true
    last_stat=$now
  fi

  if (( now - last_structural >= 300 )); then
    ./build/polymarket_negrisk_arb --config "$CONFIG" --markets 600 --min-liquidity 100 --top 60 \
      > "$RUN_ROOT/structural_latest.log" 2> "$RUN_ROOT/structural_errors.log" || true
    last_structural=$now
  fi

  if (( now - last_terminal >= 300 )); then
    ./build/polymarket_engine --config "$CONFIG" --once --scan-only --markets 240 --min-liquidity 100 \
      --run-dir "$RUN_ROOT/terminal" > "$RUN_ROOT/terminal_latest.log" 2> "$RUN_ROOT/terminal_errors.log" || true
    last_terminal=$now
  fi

  if (( now - last_oos >= 3600 )); then
    python3 scripts/walk_forward_v4.py --ledger "$RUN_ROOT/bundle_ledger.csv" \
      --output "$RUN_ROOT/walk_forward.json" --starting-capital 10000 \
      >> "$RUN_ROOT/walk_forward.log" 2>&1 || true
    last_oos=$now
  fi

  # Fail closed if either long-lived execution process dies.
  if ! kill -0 "$REC_PID" 2>/dev/null || ! kill -0 "$BROKER_PID" 2>/dev/null; then
    echo "fatal: trade recorder or multi-leg broker exited" >&2
    exit 1
  fi
  sleep 10
done
