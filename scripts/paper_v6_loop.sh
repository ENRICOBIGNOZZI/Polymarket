#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
BASE_CONFIG="$CONFIG"
RUN_ROOT="${2:-runs/paper_v6_live}"
MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-2}"
MARKETS="${V6_MARKETS:-1000}"
RECORDER_MARKETS="${V6_RECORDER_MARKETS:-1000}"
INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-0.00005}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/hard_arb" "$RUN_ROOT/external"

# Keep the resilient read-only discovery proxy from the validated V6 runtime.
MARKET_PROXY_PORT="${V6_MARKET_PROXY_PORT:-9120}"
RUNTIME_CONFIG="$RUN_ROOT/runtime_config.json"
read -r GAMMA_URL CLOB_URL < <(python3 - "$BASE_CONFIG" "$RUNTIME_CONFIG" "$MARKET_PROXY_PORT" <<'PY'
import json, os, sys
from pathlib import Path
src=Path(sys.argv[1]); dst=Path(sys.argv[2]); port=int(sys.argv[3])
cfg=json.loads(src.read_text(encoding='utf-8'))
gamma=str(cfg.get('gamma_url','https://gamma-api.polymarket.com'))
clob=str(cfg.get('clob_url','https://clob.polymarket.com'))
cfg['gamma_url']=f'http://127.0.0.1:{port}'
tmp=dst.with_name(dst.name+f'.tmp.{os.getpid()}')
tmp.write_text(json.dumps(cfg,indent=2,sort_keys=True)+'\n',encoding='utf-8')
os.replace(tmp,dst)
print(gamma, clob)
PY
)
CONFIG="$RUNTIME_CONFIG"

# V6 capital-isolated sleeves. The reserve remains explicit and all execution is paper-only.
python3 - "$CONFIG" "$RUN_ROOT" <<'PY'
import json, sys
from pathlib import Path
cfg=json.loads(Path(sys.argv[1]).read_text()); root=Path(sys.argv[2]); v=cfg['v6']; total=float(cfg['starting_capital'])
alloc=[
 ('maker',v['micro_maker_capital_fraction']),('micro_taker',v['micro_taker_capital_fraction']),
 ('broker',v['relative_value_capital_fraction']),('hard_arb',v['hard_arb_capital_fraction']),
 ('external',v['external_capital_fraction'])]
assert abs(sum(float(x[1]) for x in alloc)+float(v['reserve_fraction'])-1.0)<1e-9
for name,frac in alloc:
    child={k:x for k,x in cfg.items() if k not in {'v6','multi_strategy'}}
    child['starting_capital']=total*float(frac); child['run_dir']=str(root/name)
    child['expert_weights']={'micro':0.0,'pca':0.0,'graph':0.0,'semantic':0.0,'external':0.0}
    if name=='external':
        child['external_signals_file']=str(root/'external_signals.csv'); child['expert_weights']['external']=1.0; child['uncertainty_penalty']=0.0
    (root/f'{name}_config.json').write_text(json.dumps(child,indent=2,sort_keys=True)+'\n')
PY

rm -f "$RUN_ROOT"/intents.csv "$RUN_ROOT"/local_factor_intents.csv \
      "$RUN_ROOT"/relation_intents_raw.csv "$RUN_ROOT"/relation_guarded.csv \
      "$RUN_ROOT"/relation_intents.csv "$RUN_ROOT"/stat_arb_pairs_diagnostic.csv

proxy_pid=0; rec_pid=0; broker_pid=0; external_pid=0
proxy_restarts=0; rec_restarts=0; broker_restarts=0; external_restarts=0
start_proxy(){ PYTHONUNBUFFERED=1 python3 scripts/v6_market_proxy.py --host 127.0.0.1 --port "$MARKET_PROXY_PORT" --gamma "$GAMMA_URL" --clob "$CLOB_URL" --cache "$RUN_ROOT/market_proxy_cache.json" --status "$RUN_ROOT/market_proxy_status.json" >>"$RUN_ROOT/market_proxy.log" 2>&1 & proxy_pid=$!; }
start_recorder(){ ./build/polymarket_trade_recorder --config "$CONFIG" --run-dir "$RUN_ROOT" --markets "$RECORDER_MARKETS" --batch 40 --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --interval 5 --loop >>"$RUN_ROOT/trade_recorder.log" 2>&1 & rec_pid=$!; }
start_broker(){ python3 scripts/v6_multileg_launcher.py --lock "$RUN_ROOT/multileg.lock" -- ./build/polymarket_multileg_paper --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.60 --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 10 --max-leg-risk-usd 25 --adverse-horizon-seconds 45 --interval 1 --loop >>"$RUN_ROOT/multileg.log" 2>&1 & broker_pid=$!; }
start_external(){ ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --paper --loop >>"$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!; }
write_supervisor(){
  local ra=0 ba=0 ea=0; kill -0 "$rec_pid" 2>/dev/null&&ra=1||true; kill -0 "$broker_pid" 2>/dev/null&&ba=1||true; kill -0 "$external_pid" 2>/dev/null&&ea=1||true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp"
  printf 'timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n' >"$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >>"$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}
cleanup(){ for p in "$rec_pid" "$broker_pid" "$external_pid" "$proxy_pid";do if ((p>0));then kill "$p" 2>/dev/null||true;fi;done;for p in "$rec_pid" "$broker_pid" "$external_pid" "$proxy_pid";do if ((p>0));then wait "$p" 2>/dev/null||true;fi;done; }
trap cleanup EXIT INT TERM
start_proxy
proxy_ready=0
for _ in {1..50};do
  if curl -fsS "http://127.0.0.1:${MARKET_PROXY_PORT}/healthz" >/dev/null 2>&1;then proxy_ready=1;break;fi
  kill -0 "$proxy_pid" 2>/dev/null||break
  sleep 0.1
done
((proxy_ready==1))||{ echo "fatal: V6 market proxy failed to start" >&2; exit 1; }

# Materialize the fail-closed external feed before the external engine starts.
python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.20 >"$RUN_ROOT/external_bridge.log" 2>&1||true
start_recorder;start_broker;start_external;write_supervisor

last_factor=0;last_relation=0;last_external=0;last_report=0;last_micro_taker=0;last_hard_arb=0
rebuild_intents(){ python3 scripts/merge_v4_intents.py --input "$RUN_ROOT/local_factor_intents.csv" --input "$RUN_ROOT/relation_intents.csv" --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" --max-age-seconds 240 --max-bundles 200 >>"$RUN_ROOT/intent_merge.log" 2>&1||true; }

while true;do
  now="$(date +%s)"
  if ! kill -0 "$proxy_pid" 2>/dev/null;then wait "$proxy_pid" 2>/dev/null||true;proxy_restarts=$((proxy_restarts+1));start_proxy;sleep 1;fi
  if ! kill -0 "$rec_pid" 2>/dev/null;then wait "$rec_pid" 2>/dev/null||true;rec_restarts=$((rec_restarts+1));start_recorder;fi
  if ! kill -0 "$broker_pid" 2>/dev/null;then wait "$broker_pid" 2>/dev/null||true;broker_restarts=$((broker_restarts+1));start_broker;fi
  if ! kill -0 "$external_pid" 2>/dev/null;then wait "$external_pid" 2>/dev/null||true;external_restarts=$((external_restarts+1));start_external;fi
  write_supervisor

  # Flow/queue-aware maker: improve inside the spread only when fill probability rises
  # and the post-cost edge still pays for the improvement. Dead queues expire quickly.
  python3 scripts/v6_micro_maker.py --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" --trade-tape "$RUN_ROOT/trade_tape.csv" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge 0.00005 --max-order-usd 60 --ttl-seconds 60 --hold-seconds 180 --adverse-selection-mult 0.15 --flow-lookback-seconds 900 --min-fill-probability 0.005 --target-fill-probability 0.10 --max-improve-ticks 3 --slippage-bps 5 >>"$RUN_ROOT/maker.log" 2>&1||true

  # Causal flow-aware micro taker. Lower edge floor is still post-depth/fee/slippage,
  # so a flat midpoint model cannot manufacture negative-edge fills.
  if ((now-last_micro_taker>=5));then
    python3 scripts/v6_micro_taker_v2.py --config "$RUN_ROOT/micro_taker_config.json" --run-dir "$RUN_ROOT/micro_taker" --trade-tape "$RUN_ROOT/trade_tape.csv" --markets 500 --min-liquidity 5 --horizon-seconds 30 --flow-lookback-seconds 180 --model-half-life-seconds 21600 --max-trade-usd 30 --min-edge 0.00005 --slippage-bps 5 --max-positions 30 >>"$RUN_ROOT/micro_taker.log" 2>&1||true
    last_micro_taker=$now
  fi

  # Hard complete-set arbitrage with verified fee semantics and multi-level VWAP.
  if ((now-last_hard_arb>=10));then
    python3 scripts/v6_hard_arb_paper_v2.py --config "$RUN_ROOT/hard_arb_config.json" --run-dir "$RUN_ROOT/hard_arb" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --max-events 150 --min-edge 0.00005 --max-trade-usd 125 --slippage-bps 5 >>"$RUN_ROOT/hard_arb.log" 2>&1||true
    last_hard_arb=$now
  fi

  if ((now-last_factor>=30));then
    rm -f "$RUN_ROOT/local_factor_intents.csv" "$RUN_ROOT/stat_arb_pairs_diagnostic.csv"
    ./build/polymarket_stat_arb --config "$RUN_ROOT/broker_config.json" --markets 600 --history-universe 500 --lookback-hours 720 --fidelity-minutes 30 --min-z 0.90 --min-t-reversion 1.75 --max-half-life-hours 168 --top 250 --csv "$RUN_ROOT/stat_arb_pairs_diagnostic.csv" >"$RUN_ROOT/stat_arb_pairs_diagnostic.log" 2>"$RUN_ROOT/stat_arb_pairs_errors.log"||true
    # Aggressive discovery only after repairing self-inclusion, unit-root inference,
    # horizon mismatch and time-to-resolution validity.
    python3 scripts/v6_local_factor_v3.py --config "$CONFIG" --output "$RUN_ROOT/local_factor_intents.csv" --status "$RUN_ROOT/local_factor_status.json" --trade-tape "$RUN_ROOT/trade_tape.csv" --markets 700 --min-liquidity "$MIN_LIQUIDITY" --lookback-hours 336 --fidelity-minutes 60 --max-clusters 30 --min-common-points 36 --min-z 0.75 --fdr 0.15 --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 100 --slippage-bps 5 --flow-lookback-seconds 900 --min-fill-probability 0.01 --exit-buffer-seconds 900 >"$RUN_ROOT/local_factor_latest.log" 2>"$RUN_ROOT/local_factor_errors.log"||true
    rebuild_intents;last_factor=$now
  fi

  if ((now-last_relation>=15));then
    python3 scripts/v6_relation_intents.py --config "$CONFIG" --output "$RUN_ROOT/relation_intents_raw.csv" --status "$RUN_ROOT/relation_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 125 --max-events 150 >"$RUN_ROOT/relation_latest.log" 2>"$RUN_ROOT/relation_errors.log"||true
    python3 scripts/v6_intent_guard.py --input "$RUN_ROOT/relation_intents_raw.csv" --output "$RUN_ROOT/relation_guarded.csv" --status "$RUN_ROOT/relation_guard_status.json" --min-edge "$INTENT_MIN_EDGE" --stress-bps 5 --max-age-seconds 180 >>"$RUN_ROOT/relation_guard.log" 2>&1||true
    # Spend a bounded part of graph edge to improve the worst queue legs, then
    # admit based on estimated leg/joint completion rather than quote edge alone.
    python3 scripts/v6_queue_filter.py --config "$CONFIG" --input "$RUN_ROOT/relation_guarded.csv" --output "$RUN_ROOT/relation_intents.csv" --status "$RUN_ROOT/queue_filter_status.json" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$INTENT_MIN_EDGE" --reserve-bps 5 --flow-lookback-seconds 900 --horizon-seconds 180 --min-leg-fill-probability 0.005 --min-joint-fill-probability 0.000001 --target-leg-fill-probability 0.10 --max-improve-ticks-per-leg 4 >>"$RUN_ROOT/queue_filter.log" 2>&1||true
    rebuild_intents;last_relation=$now
  fi

  if ((now-last_external>=30));then
    python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.20 >"$RUN_ROOT/external_bridge.log" 2>&1||true
    last_external=$now
  fi

  if ((now-last_report>=30));then
    python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$RUN_ROOT" >"$RUN_ROOT/runtime_status.log" 2>&1||true
    python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" --window-seconds 3600 --production-edge "$INTENT_MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" --output-markdown "$RUN_ROOT/action_report.md" >"$RUN_ROOT/action_report_latest.log" 2>&1||true
    last_report=$now
  fi
  sleep 5
done
