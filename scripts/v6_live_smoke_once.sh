#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
R="${2:-paper_v6_live}"
MARKETS="${V6_SMOKE_MARKETS:-400}"
MIN_LIQUIDITY="${V6_SMOKE_MIN_LIQUIDITY:-2}"
EDGE="${V6_SMOKE_MIN_EDGE:-0.00005}"
FLOW_CYCLES="${V6_SMOKE_FLOW_CYCLES:-3}"
FLOW_SLEEP="${V6_SMOKE_FLOW_SLEEP_SECONDS:-10}"
mkdir -p "$R" "$R/maker" "$R/micro_taker" "$R/hard_arb" "$R/external"

python3 scripts/v6_materialize_configs.py --config "$CONFIG" --run-root "$R" > "$R/materialize.log"

record_once() {
  ./build/polymarket_trade_recorder \
    --config "$CONFIG" --run-dir "$R" --markets "$MARKETS" --batch 40 \
    --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --once \
    | tee -a "$R/trade_recorder_latest.log"
}

maker_once() {
  python3 scripts/v6_micro_maker.py \
    --config "$R/maker_config.json" --run-dir "$R/maker" --trade-tape "$R/trade_tape.csv" \
    --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$EDGE" \
    --max-order-usd 60 --ttl-seconds 60 --hold-seconds 180 --adverse-selection-mult 0.15 \
    --flow-lookback-seconds 900 --min-fill-probability 0.005 --target-fill-probability 0.10 \
    --max-improve-ticks 3 --slippage-bps 5 \
    | tee -a "$R/maker_latest.log"
}

micro_once() {
  python3 scripts/v6_micro_taker_v2.py \
    --config "$R/micro_taker_config.json" --run-dir "$R/micro_taker" \
    --trade-tape "$R/trade_tape.csv" --markets 300 --min-liquidity 5 \
    --horizon-seconds 5 --flow-lookback-seconds 180 --model-half-life-seconds 21600 \
    --max-trade-usd 30 --min-edge "$EDGE" --slippage-bps 5 --max-positions 20 \
    >> "$R/micro_taker_latest.log" 2>&1 || true
}

broker_once() {
  ./build/polymarket_multileg_paper \
    --config "$R/broker_config.json" --run-dir "$R" --intents "$R/intents.csv" \
    --trade-tape "$R/trade_tape.csv" --min-edge "$EDGE" --completion-threshold 0.60 \
    --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 10 \
    --max-leg-risk-usd 25 --adverse-horizon-seconds 45 --once \
    | tee -a "$R/multileg_latest.log"
}

# Snapshot zero establishes the pre-order tape. Orders are deliberately created
# only after this point, so historical tape cannot be counted as a fill.
record_once
maker_once
micro_once

# Hard arbitrage is taker-style and can be evaluated immediately because it uses
# current displayed depth, verified fee semantics and multi-level VWAP.
python3 scripts/v6_hard_arb_paper_v2.py \
  --config "$R/hard_arb_config.json" --run-dir "$R/hard_arb" \
  --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --max-events 120 \
  --min-edge "$EDGE" --max-trade-usd 125 --slippage-bps 5 \
  | tee "$R/hard_arb_latest.log" || true

# Repaired local factor: leave-one-out factor, null-preserving unit-root bootstrap,
# n-step horizon and time-to-resolution guard before aggressive discovery.
python3 scripts/v6_local_factor_v3.py \
  --config "$CONFIG" --output "$R/local_factor_intents.csv" \
  --status "$R/local_factor_status.json" --trade-tape "$R/trade_tape.csv" \
  --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
  --lookback-hours 168 --fidelity-minutes 60 --max-clusters 25 \
  --min-common-points 36 --min-z 0.75 --fdr 0.15 --min-edge "$EDGE" \
  --max-trade-usd 100 --slippage-bps 5 --flow-lookback-seconds 900 \
  --min-fill-probability 0.005 --exit-buffer-seconds 900 \
  | tee "$R/local_factor_latest.log" || true

# Graph/RV first proves logical/event structure, then passes a separate
# queue/completion gate that may spend only bounded edge on price improvement.
python3 scripts/v6_relation_intents.py \
  --config "$CONFIG" --output "$R/relation_intents_raw.csv" \
  --status "$R/relation_status.json" --markets "$MARKETS" \
  --min-liquidity "$MIN_LIQUIDITY" --min-edge "$EDGE" --max-trade-usd 125 --max-events 120 \
  | tee "$R/relation_latest.log" || true
python3 scripts/v6_intent_guard.py \
  --input "$R/relation_intents_raw.csv" --output "$R/relation_guarded.csv" \
  --status "$R/relation_guard_status.json" --min-edge "$EDGE" \
  --stress-bps 5 --max-age-seconds 180 \
  | tee "$R/relation_guard.log"
python3 scripts/v6_queue_filter.py \
  --config "$CONFIG" --input "$R/relation_guarded.csv" --output "$R/relation_intents.csv" \
  --status "$R/queue_filter_status.json" --trade-tape "$R/trade_tape.csv" \
  --min-edge "$EDGE" --reserve-bps 5 --flow-lookback-seconds 900 --horizon-seconds 180 \
  --min-leg-fill-probability 0.005 --min-joint-fill-probability 0.000001 \
  --target-leg-fill-probability 0.10 --max-improve-ticks-per-leg 4 \
  | tee "$R/queue_filter.log" || true

python3 scripts/merge_v4_intents.py \
  --input "$R/local_factor_intents.csv" --input "$R/relation_intents.csv" \
  --output "$R/intents.csv" --min-edge "$EDGE" --max-age-seconds 240 --max-bundles 120 \
  | tee "$R/intent_merge.log"
broker_once

# Crucial fill validation: after orders are posted, collect genuinely later public
# trades and re-run the paper executors. This makes a smoke capable of observing
# FIFO depletion/partial fills/completions instead of being structurally zero-fill.
for ((cycle=1; cycle<=FLOW_CYCLES; cycle++)); do
  sleep "$FLOW_SLEEP"
  record_once
  maker_once
  micro_once
  broker_once
  python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$R" \
    > "$R/runtime_status.log" 2>&1 || true
done

# External information stays fail-closed: lower confidence may broaden approved
# direct probabilities, but raw telemetry is never fabricated into q_external.
python3 scripts/v6_external_bridge.py \
  --output "$R/external_signals.csv" --status "$R/external_bridge_status.json" \
  --max-age-seconds 21600 --min-confidence 0.20 \
  | tee "$R/external_bridge.log" || true
./build/polymarket_engine \
  --config "$R/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
  --paper --once | tee "$R/external/engine.log"

python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$R" \
  | tee "$R/runtime_status.log"

PYTHONPATH=monitoring python3 - "$R" "$CONFIG" <<'PY'
from pathlib import Path
import csv, json, sys
from exporter_latest import LatestCollector
root=Path(sys.argv[1]); config=sys.argv[2]
c=LatestCollector(Path('.'), Path('config'), root.name, config, 20)
text=c.collect()
assert f'polymarket_runtime_info{{adapter="v6",run_root="{root.name}",version="v6"}} 1' in text, text[:2000]
assert 'polymarket_v6_exporter_info{' in text
assert 'polymarket_model_info{' in text
assert 'polymarket_allocator_state_present 1' in text
(root/'metrics.prom').write_text(text,encoding='utf-8')
status=json.loads((root/'runtime_status.json').read_text())
assert status['version']==6 and status['paper_only'] is True
assert float(status['drawdown']) <= 0.15 + 1e-12
assert (root/'hard_arb/status.json').exists()
assert (root/'local_factor_status.json').exists()
assert (root/'relation_guard_status.json').exists()
assert (root/'queue_filter_status.json').exists()
# Emit an explicit execution evidence record even when the chronological sample
# happens to have zero fills; validation must distinguish zero opportunity from
# a smoke that was incapable of observing post-order flow.
def count_rows(path):
    try:
        with path.open(newline='',encoding='utf-8') as h:return sum(1 for _ in csv.DictReader(h))
    except Exception:return 0
fills=count_rows(root/'maker'/'maker_fills.csv')
bundle_rows=count_rows(root/'bundle_ledger.csv')
maker_orders=count_rows(root/'maker'/'maker_orders.csv')
q=json.loads((root/'queue_filter_status.json').read_text())
evidence={
    'post_order_flow_cycles': int(__import__('os').environ.get('V6_SMOKE_FLOW_CYCLES','3')),
    'maker_orders_open': maker_orders,
    'maker_fill_rows': fills,
    'closed_bundle_rows': bundle_rows,
    'queue_filter': q,
    'realized_pnl': float(status.get('realized_pnl',0.0)),
    'marked_pnl': float(status.get('pnl',0.0)),
    'drawdown': float(status.get('drawdown',0.0)),
}
(root/'execution_evidence.json').write_text(json.dumps(evidence,indent=2,sort_keys=True)+'\n',encoding='utf-8')
print(json.dumps(evidence,sort_keys=True))
PY

python3 - "$R" <<'PY'
from pathlib import Path
import json,sys
r=Path(sys.argv[1])
for p in ('runtime_status.json','allocator_status.json','local_factor_status.json','relation_status.json','relation_guard_status.json','queue_filter_status.json','hard_arb/status.json','execution_evidence.json'):
    json.loads((r/p).read_text())
print('v6_live_smoke_ok')
PY
