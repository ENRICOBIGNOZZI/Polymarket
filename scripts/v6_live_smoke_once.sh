#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
R="${2:-paper_v6_live}"
MARKETS="${V6_SMOKE_MARKETS:-500}"
MIN_LIQUIDITY="${V6_SMOKE_MIN_LIQUIDITY:-2}"
EDGE="${V6_SMOKE_MIN_EDGE:-0.00005}"
FORWARD_TICKS="${V6_SMOKE_FORWARD_TICKS:-8}"
FORWARD_SLEEP_SECONDS="${V6_SMOKE_FORWARD_SLEEP_SECONDS:-10}"
MAKER_MAX_ORDER_USD="${V6_SMOKE_MAKER_MAX_ORDER_USD:-125}"
[[ "$FORWARD_TICKS" =~ ^[1-9][0-9]*$ ]]
[[ "$FORWARD_SLEEP_SECONDS" =~ ^[0-9]+$ ]]
mkdir -p "$R" "$R/maker" "$R/micro_taker" "$R/hard_arb" "$R/external"

python3 scripts/v6_materialize_configs.py --config "$CONFIG" --run-root "$R" > "$R/materialize.log"

./build/polymarket_trade_recorder \
  --config "$CONFIG" --run-dir "$R" --markets "$MARKETS" --batch 40 \
  --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --once \
  | tee "$R/trade_recorder_latest.log"

# HF research challenger: broaden discovery and lower the paper edge floor inside
# the user-authorized envelope, but do not buy fills. A join order must clear a
# stricter queue/size gate; inside-spread improvement is allowed for up to three
# ticks only while the existing maker model still has positive post-cost edge.
# Refresh public tape before each subsequent maker tick so all queue depletion
# and fills are strictly post-entry evidence.
: > "$R/maker_latest.log"
for (( maker_tick=1; maker_tick<=FORWARD_TICKS; maker_tick++ )); do
  if (( maker_tick > 1 )); then
    sleep "$FORWARD_SLEEP_SECONDS"
    ./build/polymarket_trade_recorder \
      --config "$CONFIG" --run-dir "$R" --markets "$MARKETS" --batch 40 \
      --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --once \
      | tee -a "$R/trade_recorder_latest.log"
  fi
  ./build/polymarket_maker_paper \
    --config "$R/maker_config.json" --run-dir "$R/maker" --markets "$MARKETS" \
    --min-liquidity "$MIN_LIQUIDITY" --min-edge "$EDGE" --max-order-usd "$MAKER_MAX_ORDER_USD" \
    --ttl-seconds 60 --hold-seconds 300 --adverse-selection-mult 0.10 \
    --improve-ticks 3 --max-queue-multiple 2 --once \
    | tee -a "$R/maker_latest.log"
done

python3 scripts/hf_maker_queue_hazard_audit.py \
  --order-log "$R/maker/maker_order_log.csv" --trade-tape "$R/trade_tape.csv" \
  --max-queue-multiple 2 --output "$R/maker/queue_hazard_audit.json" \
  | tee "$R/maker_queue_hazard_audit.log"

# Three snapshots deliberately exercise feature persistence and maturity; a
# smoke does not require the online taker model to invent a trade before it has
# causal forward evidence. Do not force taker fills from a flat target.
for i in 1 2 3; do
  python3 scripts/v6_micro_taker.py \
    --config "$R/micro_taker_config.json" --run-dir "$R/micro_taker" \
    --markets 120 --min-liquidity 25 --horizon-seconds 5 --max-trade-usd 15 \
    --min-edge 0.00030 --slippage-bps 5 --max-positions 10 \
    >> "$R/micro_taker_latest.log" 2>&1 || true
  if (( i < 3 )); then sleep 6; fi
done

# Keep the existing paper hard-arb smoke bounded. Its output is discovery
# evidence only until per-token receive-time, leg-skew and legging/unwind
# contracts are present in the paper executor as they are in the fast shadow.
python3 scripts/v6_hard_arb_paper.py \
  --config "$R/hard_arb_config.json" --run-dir "$R/hard_arb" \
  --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --max-events 50 \
  --min-edge "$EDGE" --max-trade-usd 60 --slippage-bps 5 \
  | tee "$R/hard_arb_latest.log"

python3 scripts/v6_local_factor_intents.py \
  --config "$CONFIG" --output "$R/local_factor_intents.csv" \
  --status "$R/local_factor_status.json" --markets "$MARKETS" \
  --min-liquidity "$MIN_LIQUIDITY" --lookback-hours 168 --fidelity-minutes 60 \
  --max-clusters 12 --min-common-points 48 --min-z 1.00 --fdr 0.10 \
  --min-edge "$EDGE" --max-trade-usd 60 --slippage-bps 5 \
  | tee "$R/local_factor_latest.log" || true

python3 scripts/v6_relation_intents.py \
  --config "$CONFIG" --output "$R/relation_intents_raw.csv" \
  --status "$R/relation_status.json" --markets "$MARKETS" \
  --min-liquidity "$MIN_LIQUIDITY" --min-edge "$EDGE" --max-trade-usd 60 --max-events 50 \
  | tee "$R/relation_latest.log" || true
python3 scripts/v6_intent_guard.py \
  --input "$R/relation_intents_raw.csv" --output "$R/relation_intents.csv" \
  --status "$R/relation_guard_status.json" --min-edge "$EDGE" \
  --stress-bps 10 --max-age-seconds 240 \
  | tee "$R/relation_guard.log"

python3 scripts/merge_v4_intents.py \
  --input "$R/local_factor_intents.csv" --input "$R/relation_intents.csv" \
  --output "$R/intents.csv" --min-edge "$EDGE" --max-age-seconds 240 --max-bundles 60 \
  | tee "$R/intent_merge.log"

./build/polymarket_multileg_paper \
  --config "$R/broker_config.json" --run-dir "$R" --intents "$R/intents.csv" \
  --trade-tape "$R/trade_tape.csv" --min-edge "$EDGE" --completion-threshold 0.75 \
  --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 6 \
  --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --once \
  | tee "$R/multileg_latest.log"

python3 scripts/v6_external_bridge.py \
  --output "$R/external_signals.csv" --status "$R/external_bridge_status.json" \
  --max-age-seconds 21600 --min-confidence 0.35 \
  | tee "$R/external_bridge.log" || true
./build/polymarket_engine \
  --config "$R/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
  --paper --once | tee "$R/external/engine.log"

python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$R" \
  | tee "$R/runtime_status.log"

PYTHONPATH=monitoring python3 - "$R" "$CONFIG" <<'PY'
from pathlib import Path
import json, sys
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
assert (root/'maker/queue_hazard_audit.json').exists()
PY

python3 - "$R" <<'PY'
from pathlib import Path
import json,sys
r=Path(sys.argv[1])
for p in ('runtime_status.json','allocator_status.json','local_factor_status.json','relation_status.json','relation_guard_status.json','hard_arb/status.json','maker/queue_hazard_audit.json'):
    json.loads((r/p).read_text())
print('v6_live_smoke_ok')
PY
