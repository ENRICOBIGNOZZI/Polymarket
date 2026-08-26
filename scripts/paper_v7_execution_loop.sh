#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CONFIG="${1:-config/paper_v7.json}"
RUN_ROOT="${2:-runs/paper_v7_live/execution}"
FREQ_CONFIG="${V7_FREQUENCY_CONFIG:-config/v7_frequency_matrix.json}"
CAPACITY_LOCK="$RUN_ROOT/token_capacity.lock"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/hard_arb" "$RUN_ROOT/external"

read -r MARKETS MIN_LIQUIDITY MIN_EDGE MAX_TRADE HARD_EVENTS HARD_EDGE HARD_TRADE < <(python3 - "$CONFIG" <<'PY'
import json,sys
c=json.load(open(sys.argv[1])); v=c['v7']
print(c['market_limit'],c['min_liquidity'],v['intent_min_edge'],c['max_trade_usd'],v['hard_arb_max_events'],v['hard_arb_min_net_edge'],v['hard_arb_max_trade_usd'])
PY
)
read -r MAKER_SECONDS TAKER_SECONDS HARD_SECONDS GRAPH_SECONDS < <(python3 - "$FREQ_CONFIG" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]))['runtime_primary_seconds']
print(c['micro_maker'],c['micro_taker'],c['hard_arb'],c['graph_relative_value'])
PY
)

MARKET_PROXY_PORT="${V7_MARKET_PROXY_PORT:-9120}"
RUNTIME_CONFIG="$RUN_ROOT/runtime_config.json"
read -r GAMMA_URL CLOB_URL < <(python3 - "$CONFIG" "$RUNTIME_CONFIG" "$MARKET_PROXY_PORT" <<'PY'
import json,os,sys
from pathlib import Path
src=Path(sys.argv[1]); dst=Path(sys.argv[2]); port=int(sys.argv[3])
cfg=json.loads(src.read_text()); gamma=str(cfg['gamma_url']); clob=str(cfg['clob_url'])
cfg['gamma_url']=f'http://127.0.0.1:{port}'
tmp=dst.with_name(dst.name+f'.tmp.{os.getpid()}'); tmp.write_text(json.dumps(cfg,indent=2,sort_keys=True)+'\n'); os.replace(tmp,dst)
print(gamma,clob)
PY
)

python3 - "$RUNTIME_CONFIG" "$RUN_ROOT" <<'PY'
import json,sys
from pathlib import Path
cfg=json.load(open(sys.argv[1])); root=Path(sys.argv[2]); v=cfg['v7']; total=float(cfg['starting_capital'])
alloc=[('maker',v['micro_maker_capital_fraction']),('micro_taker',v['micro_taker_capital_fraction']),('broker',v['relative_value_capital_fraction']),('hard_arb',v['hard_arb_capital_fraction']),('external',v['external_capital_fraction'])]
assert abs(sum(float(x[1]) for x in alloc)+float(v['reserve_fraction'])-1.0)<1e-9
for name,frac in alloc:
    child={k:x for k,x in cfg.items() if k not in {'v7','v6','multi_strategy'}}
    child['starting_capital']=total*float(frac); child['run_dir']=str(root/name)
    child['expert_weights']={'micro':0.0,'pca':0.0,'graph':0.0,'semantic':0.0,'external':0.0}
    if name=='external':
        child['external_signals_file']=str(root/'external_signals.csv'); child['expert_weights']['external']=1.0
    (root/f'{name}_config.json').write_text(json.dumps(child,indent=2,sort_keys=True)+'\n')
PY

rm -f "$RUN_ROOT/intents.csv" "$RUN_ROOT/relation_intents_raw.csv" "$RUN_ROOT/relation_intents_static.csv" \
      "$RUN_ROOT/relation_intents_optimized.csv" "$RUN_ROOT/relation_intents.csv"

proxy_pid=0; recorder_pid=0; broker_pid=0; external_pid=0
cleanup(){
  local p
  for p in "$external_pid" "$broker_pid" "$recorder_pid" "$proxy_pid"; do [[ "$p" =~ ^[1-9][0-9]*$ ]] && kill -TERM "$p" 2>/dev/null || true; done
  for p in "$external_pid" "$broker_pid" "$recorder_pid" "$proxy_pid"; do [[ "$p" =~ ^[1-9][0-9]*$ ]] && wait "$p" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

reap_stale_proxy(){
  command -v lsof >/dev/null 2>&1 || return 0
  local pid cmd cwd
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n1)"
    if [[ "$cmd" == *"v6_market_proxy.py"* && "$cwd" == "$ROOT" ]]; then
      kill -TERM "$pid" 2>/dev/null || true
    else
      echo "fatal: unverified process owns V7 proxy port $MARKET_PROXY_PORT pid=$pid" >&2
      return 1
    fi
  done < <(lsof -nP -t -iTCP:"$MARKET_PROXY_PORT" -sTCP:LISTEN 2>/dev/null || true)
}

start_proxy(){ PYTHONUNBUFFERED=1 python3 scripts/v6_market_proxy.py --host 127.0.0.1 --port "$MARKET_PROXY_PORT" --gamma "$GAMMA_URL" --clob "$CLOB_URL" --cache "$RUN_ROOT/market_proxy_cache.json" --status "$RUN_ROOT/market_proxy_status.json" >>"$RUN_ROOT/market_proxy.log" 2>&1 & proxy_pid=$!; }
start_recorder(){ ./build/polymarket_trade_recorder --config "$RUNTIME_CONFIG" --run-dir "$RUN_ROOT" --markets "$MARKETS" --batch 20 --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --interval 2 --loop >>"$RUN_ROOT/trade_recorder.log" 2>&1 & recorder_pid=$!; }
start_broker(){ python3 scripts/v7_multileg_broker_runner.py --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" --capacity-lock "$CAPACITY_LOCK" --min-edge "$MIN_EDGE" --submit-latency-ms 100 --slippage-bps 5 --adverse-horizon-seconds 45 --interval 1 --loop >>"$RUN_ROOT/multileg.log" 2>&1 & broker_pid=$!; }
refresh_external(){ python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.35 >>"$RUN_ROOT/external_bridge.log" 2>&1 || true; }
start_external(){ ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --paper --loop >>"$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!; }

rebuild_intents(){
  python3 scripts/merge_v4_intents.py --input "$RUN_ROOT/relation_intents.csv" --output "$RUN_ROOT/intents.csv" --min-edge "$MIN_EDGE" --max-age-seconds 240 --max-bundles 120 >>"$RUN_ROOT/intent_merge.log" 2>&1 || true
}
run_graph(){
  python3 scripts/v6_relation_intents.py --config "$RUNTIME_CONFIG" --output "$RUN_ROOT/relation_intents_raw.csv" --status "$RUN_ROOT/relation_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$MIN_EDGE" --max-trade-usd "$MAX_TRADE" --max-events "$HARD_EVENTS" >>"$RUN_ROOT/relation.log" 2>&1 || true
  python3 scripts/v6_intent_guard.py --input "$RUN_ROOT/relation_intents_raw.csv" --output "$RUN_ROOT/relation_intents_static.csv" --status "$RUN_ROOT/relation_static_guard.json" --min-edge "$MIN_EDGE" --stress-bps 10 --max-age-seconds 240 >>"$RUN_ROOT/relation_static_guard.log" 2>&1 || true
  python3 scripts/v6_bundle_quote_optimizer.py --config "$RUNTIME_CONFIG" --input "$RUN_ROOT/relation_intents_static.csv" --output "$RUN_ROOT/relation_intents_optimized.csv" --status "$RUN_ROOT/relation_quote_optimizer.json" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$MIN_EDGE" --reserve-bps 0.5 --min-leg-fill-probability 0.001 --min-joint-fill-probability 0 --target-leg-fill-probability 0.10 >>"$RUN_ROOT/relation_quote_optimizer.log" 2>&1 || true
  # Final Graph/RV admission is prospective, executable and dependence-robust.
  # A complete basket uses a proved non-augmented NegRisk terminal payout floor
  # minus executed entry prices, verified fees and capital time. Partial states
  # are unwound against contemporaneous depth. Historical sessions must be no
  # easier than the current execution state and inference uses chronological
  # circular blocks rather than iid row resampling.
  python3 scripts/v7_graph_execution_guard.py --config "$RUNTIME_CONFIG" --input "$RUN_ROOT/relation_intents_optimized.csv" --output "$RUN_ROOT/relation_intents.csv" --state "$RUN_ROOT/graph_execution_state.json" --status "$RUN_ROOT/relation_joint_state_guard.json" --trade-tape "$RUN_ROOT/trade_tape.csv" --window-seconds 180 --min-sessions 4 --slippage-bps 5 --capital-cost-bps-per-hour 0.25 --bootstrap-reps 800 --bootstrap-quantile 0.10 >>"$RUN_ROOT/relation_joint_state_guard.log" 2>&1 || true
  rebuild_intents
}

reap_stale_proxy
start_proxy
for _ in $(seq 1 50); do curl -fsS --max-time 1 "http://127.0.0.1:${MARKET_PROXY_PORT}/healthz" >/dev/null 2>&1 && break; kill -0 "$proxy_pid" 2>/dev/null || exit 1; sleep 0.1; done
curl -fsS --max-time 1 "http://127.0.0.1:${MARKET_PROXY_PORT}/healthz" >/dev/null
refresh_external
start_recorder
start_broker
start_external

last_maker=0; last_taker=0; last_hard=0; last_graph=0; last_external=0; last_report=0
while true; do
  now="$(date +%s)"
  kill -0 "$proxy_pid" 2>/dev/null || { echo "fatal: V7 market proxy exited" >&2; exit 1; }
  kill -0 "$recorder_pid" 2>/dev/null || { echo "fatal: V7 recorder exited" >&2; exit 1; }
  kill -0 "$broker_pid" 2>/dev/null || { echo "fatal: V7 broker exited" >&2; exit 1; }
  kill -0 "$external_pid" 2>/dev/null || { echo "fatal: V7 external sleeve exited" >&2; exit 1; }

  if (( now-last_maker >= MAKER_SECONDS )); then
    python3 scripts/v7_capacity_lock.py --lock "$CAPACITY_LOCK" -- \
      python3 scripts/v7_micro_maker_worker.py --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" --trade-tape "$RUN_ROOT/trade_tape.csv" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$MIN_EDGE" --max-order-usd "$MAX_TRADE" --ttl-seconds 60 --hold-seconds 240 --flow-lookback-seconds 300 --min-fill-probability 0.001 --max-improve-ticks 1 --slippage-bps 5 --capital-cost-bps-per-hour 0.25 >>"$RUN_ROOT/maker.log" 2>&1 || true
    last_maker=$now
  fi
  if (( now-last_taker >= TAKER_SECONDS )); then
    python3 scripts/v7_micro_taker_worker.py --config "$RUN_ROOT/micro_taker_config.json" --run-dir "$RUN_ROOT/micro_taker" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --horizon-seconds 30 --max-trade-usd "$MAX_TRADE" --min-edge "$MIN_EDGE" --slippage-bps 5 --uncertainty-z 1.0 --adverse-markout-bps 2 --capital-cost-bps-per-hour 0.25 --max-book-age-seconds 5 --max-positions 20 >>"$RUN_ROOT/micro_taker.log" 2>&1 || true
    last_taker=$now
  fi
  if (( now-last_hard >= HARD_SECONDS )); then
    python3 scripts/v6_hard_arb_paper.py --config "$RUN_ROOT/hard_arb_config.json" --run-dir "$RUN_ROOT/hard_arb" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --max-events "$HARD_EVENTS" --min-edge "$HARD_EDGE" --max-trade-usd "$HARD_TRADE" --slippage-bps 5 >>"$RUN_ROOT/hard_arb.log" 2>&1 || true
    last_hard=$now
  fi
  if (( now-last_graph >= GRAPH_SECONDS )); then run_graph; last_graph=$now; fi
  if (( now-last_external >= 60 )); then refresh_external; last_external=$now; fi
  if (( now-last_report >= 60 )); then
    python3 scripts/v6_runtime_status.py --config "$RUNTIME_CONFIG" --run-root "$RUN_ROOT" >>"$RUN_ROOT/runtime_status.log" 2>&1 || true
    python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" --window-seconds 3600 --production-edge "$MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" --output-markdown "$RUN_ROOT/action_report.md" >>"$RUN_ROOT/action_report.log" 2>&1 || true
    python3 scripts/v7_execution_evidence.py --run-root "$RUN_ROOT" --policy config/v7_execution_evidence.json >>"$RUN_ROOT/v7_execution_evidence.log" 2>&1 || true
    tmp="$RUN_ROOT/v7_execution_supervisor.json.tmp.${BASHPID:-$$}"
    printf '{"timestamp":%s,"paper_only":true,"maker_seconds":%s,"taker_seconds":%s,"hard_seconds":%s,"graph_seconds":%s,"capacity_lock":"%s"}\n' "$now" "$MAKER_SECONDS" "$TAKER_SECONDS" "$HARD_SECONDS" "$GRAPH_SECONDS" "$CAPACITY_LOCK" >"$tmp"; mv "$tmp" "$RUN_ROOT/v7_execution_supervisor.json"
    last_report=$now
  fi
  sleep 1
done
