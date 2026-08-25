#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
RUN_ROOT="${2:-runs/paper_v6_live}"
MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-10}"
MARKETS="${V6_MARKETS:-700}"
RECORDER_MARKETS="${V6_RECORDER_MARKETS:-1200}"
INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-0.00020}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/external"

# Materialize capital-isolated child configs. No mixture is used: the external
# child has only the external expert enabled; micro and multi-leg sleeves use
# their dedicated execution engines.
python3 - "$CONFIG" "$RUN_ROOT" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
v6 = cfg['v6']; total = float(cfg['starting_capital'])
for name, frac in [('maker', v6['micro_capital_fraction']), ('broker', v6['multileg_capital_fraction']), ('external', v6['external_capital_fraction'])]:
    child = {k: v for k, v in cfg.items() if k != 'v6'}
    child['starting_capital'] = total * float(frac)
    if name == 'external':
        child['run_dir'] = str(root / 'external')
        child['external_signals_file'] = str(root / 'external_signals.csv')
        child['expert_weights'] = {'micro':0.0,'pca':0.0,'graph':0.0,'semantic':0.0,'external':1.0}
        child['uncertainty_penalty'] = 0.0
    else:
        child['run_dir'] = str(root / name)
        child['expert_weights'] = {'micro':0.0,'pca':0.0,'graph':0.0,'semantic':0.0,'external':0.0}
    (root / f'{name}_config.json').write_text(json.dumps(child, indent=2, sort_keys=True) + '\n')
PY

# Safe restart: stale scanner outputs are never inherited as fresh intents.
rm -f "$RUN_ROOT"/intents.csv "$RUN_ROOT"/b1_intents.csv "$RUN_ROOT"/b2_intents.csv \
      "$RUN_ROOT"/relation_intents.csv "$RUN_ROOT"/stat_arb_pairs.csv \
      "$RUN_ROOT"/stat_arb_pca_raw.csv "$RUN_ROOT"/stat_arb_pca.csv

rec_pid=0
broker_pid=0
external_pid=0
rec_restarts=0
broker_restarts=0
external_restarts=0

start_recorder() {
  ./build/polymarket_trade_recorder --config "$CONFIG" --run-dir "$RUN_ROOT" \
    --markets "$RECORDER_MARKETS" --batch 40 --min-liquidity "$MIN_LIQUIDITY" \
    --lookback-seconds 900 --interval 5 --loop >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
  rec_pid=$!
}
start_broker() {
  ./build/polymarket_multileg_paper --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" \
    --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" \
    --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.75 \
    --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 6 \
    --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --interval 1 --loop \
    >> "$RUN_ROOT/multileg.log" 2>&1 &
  broker_pid=$!
}
start_external() {
  ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" \
    --min-liquidity "$MIN_LIQUIDITY" --paper --loop >> "$RUN_ROOT/external/engine.log" 2>&1 &
  external_pid=$!
}

write_supervisor() {
  local ra=0 ba=0 ea=0
  kill -0 "$rec_pid" 2>/dev/null && ra=1 || true
  kill -0 "$broker_pid" 2>/dev/null && ba=1 || true
  kill -0 "$external_pid" 2>/dev/null && ea=1 || true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp"
  printf 'timestamp,recorder_alive,broker_alive,external_alive,recorder_restarts,broker_restarts,external_restarts,recorder_pid,broker_pid,external_pid\n' > "$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" \
    "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >> "$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}

cleanup() {
  for p in "$rec_pid" "$broker_pid" "$external_pid"; do
    if (( p > 0 )); then kill "$p" 2>/dev/null || true; fi
  done
  for p in "$rec_pid" "$broker_pid" "$external_pid"; do
    if (( p > 0 )); then wait "$p" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT INT TERM

start_recorder
start_broker
start_external
write_supervisor

last_factor=0
last_relation=0
last_external=0
last_report=0

rebuild_intents() {
  python3 scripts/build_v4_intents.py --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" \
    --output "$RUN_ROOT/b1_intents.csv" --config "$RUN_ROOT/broker_config.json" --min-edge "$INTENT_MIN_EDGE" \
    >> "$RUN_ROOT/intent_build.log" 2>&1 || true
  python3 scripts/build_v4_intents.py --strategy B2 --input "$RUN_ROOT/stat_arb_pca.csv" \
    --output "$RUN_ROOT/b2_intents.csv" --config "$RUN_ROOT/broker_config.json" --min-edge "$INTENT_MIN_EDGE" \
    >> "$RUN_ROOT/intent_build.log" 2>&1 || true
  python3 scripts/merge_v4_intents.py \
    --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" --input "$RUN_ROOT/relation_intents.csv" \
    --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" --max-age-seconds 240 --max-bundles 120 \
    >> "$RUN_ROOT/intent_merge.log" 2>&1 || true
}

while true; do
  now="$(date +%s)"

  if ! kill -0 "$rec_pid" 2>/dev/null; then wait "$rec_pid" 2>/dev/null || true; rec_restarts=$((rec_restarts+1)); start_recorder; fi
  if ! kill -0 "$broker_pid" 2>/dev/null; then wait "$broker_pid" 2>/dev/null || true; broker_restarts=$((broker_restarts+1)); start_broker; fi
  if ! kill -0 "$external_pid" 2>/dev/null; then wait "$external_pid" 2>/dev/null || true; external_restarts=$((external_restarts+1)); start_external; fi
  write_supervisor

  # MICRO MAKER: dedicated passive model; this is not a terminal-probability oracle.
  ./build/polymarket_maker_paper --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" \
    --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge 0.00035 \
    --max-order-usd 25 --ttl-seconds 90 --hold-seconds 240 --adverse-selection-mult 0.15 --once \
    >> "$RUN_ROOT/maker.log" 2>&1 || true

  if (( now - last_factor >= 60 )); then
    rm -f "$RUN_ROOT/stat_arb_pairs.csv" "$RUN_ROOT/stat_arb_pca_raw.csv" "$RUN_ROOT/stat_arb_pca.csv"
    ./build/polymarket_stat_arb --config "$RUN_ROOT/broker_config.json" --markets "$MARKETS" --history-universe 350 \
      --lookback-hours 720 --fidelity-minutes 30 --min-z 0.65 --min-t-reversion 0.60 \
      --max-half-life-hours 336 --top 250 --csv "$RUN_ROOT/stat_arb_pairs.csv" \
      > "$RUN_ROOT/stat_arb_pairs_latest.log" 2> "$RUN_ROOT/stat_arb_pairs_errors.log" || true
    ./build/polymarket_pca_stat_arb --config "$RUN_ROOT/broker_config.json" --markets "$MARKETS" --universe 350 \
      --lookback-hours 720 --fidelity-minutes 30 --factors 5 --max-hedges 8 --min-z 0.55 \
      --min-t-reversion 0.50 --max-half-life-hours 336 --max-factor-hedge-error 0.80 --top 250 \
      --csv "$RUN_ROOT/stat_arb_pca_raw.csv" > "$RUN_ROOT/stat_arb_pca_latest.log" 2> "$RUN_ROOT/stat_arb_pca_errors.log" || true
    python3 scripts/filter_coherent_hedges.py --input "$RUN_ROOT/stat_arb_pca_raw.csv" --output "$RUN_ROOT/stat_arb_pca.csv" \
      --rejections "$RUN_ROOT/stat_arb_pca_rejected.csv" --cache "$RUN_ROOT/market_metadata_cache.json" \
      --min-jaccard 0.12 --min-shared-tokens 1 >> "$RUN_ROOT/coherent_hedges.log" 2>&1 || true
    rebuild_intents
    last_factor=$now
  fi

  if (( now - last_relation >= 30 )); then
    python3 scripts/v6_relation_intents.py --config "$CONFIG" --output "$RUN_ROOT/relation_intents.csv" \
      --status "$RUN_ROOT/relation_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
      --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --max-events 80 \
      > "$RUN_ROOT/relation_latest.log" 2> "$RUN_ROOT/relation_errors.log" || true
    rebuild_intents
    last_relation=$now
  fi

  if (( now - last_external >= 60 )); then
    python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" \
      --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.35 \
      > "$RUN_ROOT/external_bridge.log" 2>&1 || true
    last_external=$now
  fi

  if (( now - last_report >= 60 )); then
    python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$RUN_ROOT" \
      > "$RUN_ROOT/runtime_status.log" 2>&1 || true
    python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" \
      --window-seconds 3600 --production-edge "$INTENT_MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" \
      --output-markdown "$RUN_ROOT/action_report.md" > "$RUN_ROOT/action_report_latest.log" 2>&1 || true
    last_report=$now
  fi
  sleep 5
done
