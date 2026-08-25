#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
RUN_ROOT="${2:-runs/v6_frontier}"
TRADE_TAPE="${3:-$RUN_ROOT/trade_tape.csv}"
MARKETS="${V6_FRONTIER_MARKETS:-700}"
MIN_LIQUIDITY="${V6_FRONTIER_MIN_LIQUIDITY:-2}"
MIN_EDGE="${V6_FRONTIER_MIN_EDGE:-0.00005}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/hard_arb" "$RUN_ROOT/external"

if [[ ! -s "$TRADE_TAPE" ]]; then
  mkdir -p "$(dirname "$TRADE_TAPE")"
  printf 'timestamp,asset_id,side,price,size,transaction_hash\n' > "$TRADE_TAPE"
fi

python3 scripts/v6_micro_maker_v2.py --config "$CONFIG" --run-dir "$RUN_ROOT/maker" --trade-tape "$TRADE_TAPE" \
  --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$MIN_EDGE" --max-order-usd 60 \
  --ttl-seconds 60 --hold-seconds 180 --flow-lookback-seconds 900 --min-fill-probability 0.005 \
  --target-fill-probability 0.10 --max-improve-ticks 3 --slippage-bps 5 >"$RUN_ROOT/maker_frontier.log" 2>&1 || true

python3 scripts/v6_micro_taker_v4.py --config "$CONFIG" --run-dir "$RUN_ROOT/micro_taker" --trade-tape "$TRADE_TAPE" \
  --markets 500 --min-liquidity 5 --horizon-seconds 30 --max-target-staleness-seconds 10 \
  --flow-lookback-seconds 180 --model-half-life-seconds 21600 --max-trade-usd 30 --min-edge "$MIN_EDGE" \
  --slippage-bps 5 --max-positions 30 >"$RUN_ROOT/micro_taker_frontier.log" 2>&1 || true

python3 scripts/v6_local_factor_v4.py --config "$CONFIG" --output "$RUN_ROOT/local_factor_raw.csv" \
  --status "$RUN_ROOT/local_factor_status.json" --trade-tape "$TRADE_TAPE" --markets "$MARKETS" \
  --min-liquidity "$MIN_LIQUIDITY" --lookback-hours 336 --fidelity-minutes 60 --max-clusters 30 \
  --min-common-points 36 --min-z 0.75 --fdr 0.15 --min-edge "$MIN_EDGE" --max-trade-usd 100 \
  --slippage-bps 5 --flow-lookback-seconds 900 --min-fill-probability 0 --exit-buffer-seconds 900 \
  >"$RUN_ROOT/local_factor_frontier.log" 2>&1 || true

python3 scripts/v6_bundle_state_guard.py --config "$CONFIG" --input "$RUN_ROOT/local_factor_raw.csv" \
  --output "$RUN_ROOT/local_factor_intents.csv" --status "$RUN_ROOT/local_factor_state_guard.json" \
  --trade-tape "$TRADE_TAPE" --min-edge "$MIN_EDGE" --lookback-seconds 900 --window-seconds 180 \
  --min-windows 4 --slippage-bps 5 >"$RUN_ROOT/local_factor_state_guard.log" 2>&1 || true

python3 scripts/v6_relation_intents_v2.py --config "$CONFIG" --output "$RUN_ROOT/relation_raw.csv" \
  --status "$RUN_ROOT/relation_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
  --min-edge "$MIN_EDGE" --max-trade-usd 125 --max-events 150 >"$RUN_ROOT/relation_frontier.log" 2>&1 || true

python3 scripts/v6_structural_curve.py --config "$CONFIG" --output "$RUN_ROOT/structural_curve.csv" \
  --status "$RUN_ROOT/structural_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
  --min-family-size 3 --slippage-bps 5 >"$RUN_ROOT/structural_frontier.log" 2>&1 || true

python3 scripts/v6_intent_guard.py --input "$RUN_ROOT/relation_raw.csv" --output "$RUN_ROOT/relation_guarded.csv" \
  --status "$RUN_ROOT/relation_guard_status.json" --min-edge "$MIN_EDGE" --stress-bps 5 --max-age-seconds 240 \
  >"$RUN_ROOT/relation_guard.log" 2>&1 || true

# Marginal fill hazards are used only to decide whether paying a tick for queue
# priority is worthwhile. Final economic admission is the empirical joint-state
# EV guard below; no product-of-marginals is used as the final completion model.
python3 scripts/v6_bundle_quote_optimizer.py --config "$CONFIG" --input "$RUN_ROOT/relation_guarded.csv" \
  --output "$RUN_ROOT/relation_queue.csv" --status "$RUN_ROOT/relation_queue_status.json" \
  --trade-tape "$TRADE_TAPE" --min-edge "$MIN_EDGE" --reserve-bps 2 --flow-lookback-seconds 900 \
  --horizon-seconds 180 --min-leg-fill-probability 0 --min-joint-fill-probability 0 \
  --target-leg-fill-probability 0.10 --max-improve-ticks-per-leg 12 >"$RUN_ROOT/relation_queue.log" 2>&1 || true

python3 scripts/v6_bundle_state_guard.py --config "$CONFIG" --input "$RUN_ROOT/relation_queue.csv" \
  --output "$RUN_ROOT/relation_intents.csv" --status "$RUN_ROOT/relation_state_guard.json" \
  --trade-tape "$TRADE_TAPE" --min-edge "$MIN_EDGE" --lookback-seconds 900 --window-seconds 180 \
  --min-windows 4 --slippage-bps 5 >"$RUN_ROOT/relation_state_guard.log" 2>&1 || true

# Production `v6_queue_filter.py hard` already owns the realistic sequential
# execution path on main. V4 stays a shadow challenger adding stronger bounded
# cross-leg freshness and double-snapshot stability evidence.
python3 scripts/v6_hard_arb_paper_v4.py --config "$CONFIG" --run-dir "$RUN_ROOT/hard_arb" --markets "$MARKETS" \
  --min-liquidity "$MIN_LIQUIDITY" --max-events 150 --min-edge "$MIN_EDGE" --max-trade-usd 125 \
  --slippage-bps 5 --max-leg-age-ms 2000 --max-cross-leg-skew-ms 1000 >"$RUN_ROOT/hard_arb_frontier.log" 2>&1 || true

if [[ "${V6_FRONTIER_EXTERNAL:-1}" == "1" ]]; then
  EXT="$RUN_ROOT/external"
  python3 - "$EXT" <<'PY'
import gzip, sys
from pathlib import Path
root=Path(sys.argv[1]); root.mkdir(parents=True,exist_ok=True)
for name in ('observations.jsonl.gz','prices.jsonl.gz'):
    path=root/name
    if not path.exists(): path.write_bytes(gzip.compress(b'',mtime=0))
state=root/'state.json'
if not state.exists(): state.write_text('{}\n',encoding='utf-8')
PY
  python3 scripts/external_intelligence_v2.py --config config/external_intelligence.json \
    --observations-in "$EXT/observations.jsonl.gz" --prices-in "$EXT/prices.jsonl.gz" --state-in "$EXT/state.json" \
    --observations-out "$EXT/observations.next.jsonl.gz" --prices-out "$EXT/prices.next.jsonl.gz" --state-out "$EXT/state.next.json" \
    --signals-out "$EXT/signals.jsonl" --report-json "$EXT/report.json" --report-markdown "$EXT/report.md" --mode incremental \
    >"$RUN_ROOT/external_frontier.log" 2>&1 || true
fi

python3 - "$RUN_ROOT" <<'PY'
import json, sys
from pathlib import Path
root=Path(sys.argv[1]); status={"paper_only":True,"frontier":True,"components":{}}
for name,path in {
    "maker":root/"maker/status.json", "micro_taker":root/"micro_taker/status.json",
    "local_factor":root/"local_factor_status.json", "local_factor_state":root/"local_factor_state_guard.json",
    "relation":root/"relation_status.json", "relation_state":root/"relation_state_guard.json",
    "structural":root/"structural_status.json", "hard_arb":root/"hard_arb/status.json",
    "external":root/"external/report.json",
}.items():
    try: status["components"][name]=json.loads(path.read_text())
    except Exception: status["components"][name]={"available":False}
(root/"frontier_status.json").write_text(json.dumps(status,indent=2,sort_keys=True)+"\n")
print(json.dumps({"frontier_status":str(root/"frontier_status.json"),"components":list(status["components"])},sort_keys=True))
PY
