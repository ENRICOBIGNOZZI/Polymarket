#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
RUN_ROOT="${2:-runs/paper_v6_live}"
MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-10}"
MARKETS="${V6_MARKETS:-700}"
RECORDER_MARKETS="${V6_RECORDER_MARKETS:-1200}"
INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-0.00020}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/external"

# Capital-isolated sleeves sum to parent capital. There is no operational
# mixture-of-experts: every sleeve has one economic task and its own execution.
python3 - "$CONFIG" "$RUN_ROOT" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text()); root = Path(sys.argv[2]); v6 = cfg['v6']; total = float(cfg['starting_capital'])
alloc = [
    ('maker', v6['micro_maker_capital_fraction']),
    ('micro_taker', v6['micro_taker_capital_fraction']),
    ('broker', v6['multileg_capital_fraction']),
    ('external', v6['external_capital_fraction']),
]
assert abs(sum(float(x[1]) for x in alloc) + float(v6['reserve_fraction']) - 1.0) < 1e-9
for name, frac in alloc:
    child = {k: v for k, v in cfg.items() if k != 'v6'}
    child['starting_capital'] = total * float(frac)
    child['run_dir'] = str(root / name)
    child['expert_weights'] = {'micro':0.0,'pca':0.0,'graph':0.0,'semantic':0.0,'external':0.0}
    if name == 'external':
        child['external_signals_file'] = str(root / 'external_signals.csv')
        child['expert_weights']['external'] = 1.0
        child['uncertainty_penalty'] = 0.0
    (root / f'{name}_config.json').write_text(json.dumps(child, indent=2, sort_keys=True) + '\n')
PY

rm -f "$RUN_ROOT"/intents.csv "$RUN_ROOT"/b1_intents.csv "$RUN_ROOT"/local_factor_intents.csv \
      "$RUN_ROOT"/relation_intents_raw.csv "$RUN_ROOT"/relation_intents.csv "$RUN_ROOT"/stat_arb_pairs.csv

rec_pid=0; broker_pid=0; external_pid=0
rec_restarts=0; broker_restarts=0; external_restarts=0

start_recorder() {
  ./build/polymarket_trade_recorder --config "$CONFIG" --run-dir "$RUN_ROOT" \
    --markets "$RECORDER_MARKETS" --batch 40 --min-liquidity "$MIN_LIQUIDITY" \
    --lookback-seconds 900 --interval 5 --loop >> "$RUN_ROOT/trade_recorder.log" 2>&1 & rec_pid=$!
}
start_broker() {
  ./build/polymarket_multileg_paper --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" \
    --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" \
    --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.75 --submit-latency-ms 100 --cancel-latency-ms 100 \
    --max-replaces 6 --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --interval 1 --loop \
    >> "$RUN_ROOT/multileg.log" 2>&1 & broker_pid=$!
}
start_external() {
  ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" \
    --min-liquidity "$MIN_LIQUIDITY" --paper --loop >> "$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!
}
write_supervisor() {
  local ra=0 ba=0 ea=0
  kill -0 "$rec_pid" 2>/dev/null && ra=1 || true
  kill -0 "$broker_pid" 2>/dev/null && ba=1 || true
  kill -0 "$external_pid" 2>/dev/null && ea=1 || true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp"
  printf 'timestamp,recorder_alive,broker_alive,external_alive,recorder_restarts,broker_restarts,external_restarts,recorder_pid,broker_pid,external_pid\n' > "$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >> "$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}
cleanup() {
  for p in "$rec_pid" "$broker_pid" "$external_pid"; do if (( p > 0 )); then kill "$p" 2>/dev/null || true; fi; done
  for p in "$rec_pid" "$broker_pid" "$external_pid"; do if (( p > 0 )); then wait "$p" 2>/dev/null || true; fi; done
}
trap cleanup EXIT INT TERM
start_recorder; start_broker; start_external; write_supervisor

last_factor=0; last_relation=0; last_external=0; last_report=0; last_micro_taker=0

rebuild_intents() {
  python3 scripts/build_v4_intents.py --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" --output "$RUN_ROOT/b1_intents.csv" \
    --config "$RUN_ROOT/broker_config.json" --min-edge "$INTENT_MIN_EDGE" >> "$RUN_ROOT/intent_build.log" 2>&1 || true
  python3 scripts/merge_v4_intents.py --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/local_factor_intents.csv" --input "$RUN_ROOT/relation_intents.csv" \
    --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" --max-age-seconds 240 --max-bundles 120 >> "$RUN_ROOT/intent_merge.log" 2>&1 || true
}

while true; do
  now="$(date +%s)"
  if ! kill -0 "$rec_pid" 2>/dev/null; then wait "$rec_pid" 2>/dev/null || true; rec_restarts=$((rec_restarts+1)); start_recorder; fi
  if ! kill -0 "$broker_pid" 2>/dev/null; then wait "$broker_pid" 2>/dev/null || true; broker_restarts=$((broker_restarts+1)); start_broker; fi
  if ! kill -0 "$external_pid" 2>/dev/null; then wait "$external_pid" 2>/dev/null || true; external_restarts=$((external_restarts+1)); start_external; fi
  write_supervisor

  # MICRO MAKER: spread capture with fill/adverse-selection accounting.
  ./build/polymarket_maker_paper --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" --markets "$MARKETS" \
    --min-liquidity "$MIN_LIQUIDITY" --min-edge 0.00035 --max-order-usd 25 --ttl-seconds 90 --hold-seconds 240 \
    --adverse-selection-mult 0.15 --once >> "$RUN_ROOT/maker.log" 2>&1 || true

  # MICRO TAKER: pooled online forward-markout regression. Positions are forced
  # out after the forecast horizon so they cannot become terminal event bets.
  if (( now - last_micro_taker >= 5 )); then
    python3 scripts/v6_micro_taker.py --config "$RUN_ROOT/micro_taker_config.json" --run-dir "$RUN_ROOT/micro_taker" \
      --markets 250 --min-liquidity 25 --horizon-seconds 30 --max-trade-usd 15 --min-edge 0.00030 \
      --slippage-bps 5 --max-positions 20 >> "$RUN_ROOT/micro_taker.log" 2>&1 || true
    last_micro_taker=$now
  fi

  if (( now - last_factor >= 60 )); then
    rm -f "$RUN_ROOT/stat_arb_pairs.csv" "$RUN_ROOT/local_factor_intents.csv"
    ./build/polymarket_stat_arb --config "$RUN_ROOT/broker_config.json" --markets "$MARKETS" --history-universe 350 \
      --lookback-hours 720 --fidelity-minutes 30 --min-z 0.65 --min-t-reversion 0.60 --max-half-life-hours 336 \
      --top 250 --csv "$RUN_ROOT/stat_arb_pairs.csv" > "$RUN_ROOT/stat_arb_pairs_latest.log" 2> "$RUN_ROOT/stat_arb_pairs_errors.log" || true
    # Unlike V5 global PCA, this engine constructs many event/payoff-local panels.
    python3 scripts/v6_local_factor_intents.py --config "$CONFIG" --output "$RUN_ROOT/local_factor_intents.csv" \
      --status "$RUN_ROOT/local_factor_status.json" --markets 500 --min-liquidity "$MIN_LIQUIDITY" --lookback-hours 336 \
      --fidelity-minutes 60 --max-clusters 30 --min-z 0.65 --min-t 0.75 --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 \
      > "$RUN_ROOT/local_factor_latest.log" 2> "$RUN_ROOT/local_factor_errors.log" || true
    rebuild_intents; last_factor=$now
  fi

  if (( now - last_relation >= 30 )); then
    # Relation discovery may emit maker graph dislocations. They are explicitly
    # downgraded to GRAPH_RV unless a future scanner supplies a true TAKER hard-arb
    # intent. This prevents partial-fill maker bundles from being mislabeled as
    # deterministic arbitrage.
    python3 scripts/v6_relation_intents.py --config "$CONFIG" --output "$RUN_ROOT/relation_intents_raw.csv" --status "$RUN_ROOT/relation_status.json" \
      --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --max-events 80 \
      > "$RUN_ROOT/relation_latest.log" 2> "$RUN_ROOT/relation_errors.log" || true
    python3 scripts/v6_intent_guard.py --input "$RUN_ROOT/relation_intents_raw.csv" --output "$RUN_ROOT/relation_intents.csv" \
      --status "$RUN_ROOT/relation_guard_status.json" --min-edge "$INTENT_MIN_EDGE" --stress-bps 10 --max-age-seconds 240 \
      >> "$RUN_ROOT/relation_guard.log" 2>&1 || true
    # Independent executable-book NegRisk diagnostic: asks, taker fees, slippage,
    # complete-event metadata and displayed depth. It remains diagnostic until the
    # multi-leg broker has a true immediate-taker/atomic execution path.
    ./build/polymarket_negrisk_arb --config "$CONFIG" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --top 100 \
      > "$RUN_ROOT/graph_hard_taker_scan.csv" 2> "$RUN_ROOT/graph_hard_taker_errors.log" || true
    rebuild_intents; last_relation=$now
  fi

  if (( now - last_external >= 60 )); then
    python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" \
      --max-age-seconds 21600 --min-confidence 0.35 > "$RUN_ROOT/external_bridge.log" 2>&1 || true
    last_external=$now
  fi

  if (( now - last_report >= 60 )); then
    python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$RUN_ROOT" > "$RUN_ROOT/runtime_status.log" 2>&1 || true
    python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" --window-seconds 3600 \
      --production-edge "$INTENT_MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" --output-markdown "$RUN_ROOT/action_report.md" \
      > "$RUN_ROOT/action_report_latest.log" 2>&1 || true
    last_report=$now
  fi
  sleep 5
done
