#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
RUN_ROOT="${2:-runs/paper_v6_live}"
MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-10}"
MARKETS="${V6_MARKETS:-700}"
# The recorder needs the tradable universe, not a larger discovery universe.
# Keeping the two aligned materially reduces Gamma pagination while preserving
# every market that a V6 sleeve may submit to the shared paper broker.
RECORDER_MARKETS="${V6_RECORDER_MARKETS:-$MARKETS}"
RECORDER_INTERVAL_SECONDS="${V6_RECORDER_INTERVAL_SECONDS:-10}"
MAKER_INTERVAL_SECONDS="${V6_MAKER_INTERVAL_SECONDS:-15}"
MICRO_TAKER_INTERVAL_SECONDS="${V6_MICRO_TAKER_INTERVAL_SECONDS:-7}"
INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-0.00020}"
CORE_STARTUP_GAP_SECONDS="${V6_CORE_STARTUP_GAP_SECONDS:-20}"
MAKER_STARTUP_DELAY_SECONDS="${V6_MAKER_STARTUP_DELAY_SECONDS:-40}"
MICRO_STARTUP_DELAY_SECONDS="${V6_MICRO_STARTUP_DELAY_SECONDS:-55}"
HARD_ARB_STARTUP_DELAY_SECONDS="${V6_HARD_ARB_STARTUP_DELAY_SECONDS:-70}"
RELATION_STARTUP_DELAY_SECONDS="${V6_RELATION_STARTUP_DELAY_SECONDS:-90}"
FACTOR_STARTUP_DELAY_SECONDS="${V6_FACTOR_STARTUP_DELAY_SECONDS:-120}"
EXTERNAL_STARTUP_DELAY_SECONDS="${V6_EXTERNAL_STARTUP_DELAY_SECONDS:-30}"
REPORT_STARTUP_DELAY_SECONDS="${V6_REPORT_STARTUP_DELAY_SECONDS:-20}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/hard_arb" "$RUN_ROOT/external"

for value in \
  "$RECORDER_INTERVAL_SECONDS" "$MAKER_INTERVAL_SECONDS" "$MICRO_TAKER_INTERVAL_SECONDS" \
  "$CORE_STARTUP_GAP_SECONDS" "$MAKER_STARTUP_DELAY_SECONDS" "$MICRO_STARTUP_DELAY_SECONDS" \
  "$HARD_ARB_STARTUP_DELAY_SECONDS" "$RELATION_STARTUP_DELAY_SECONDS" \
  "$FACTOR_STARTUP_DELAY_SECONDS" "$EXTERNAL_STARTUP_DELAY_SECONDS" "$REPORT_STARTUP_DELAY_SECONDS"; do
  [[ "$value" =~ ^[0-9]+$ ]] || { echo "V6 startup/cadence values must be non-negative integers" >&2; exit 2; }
done
(( RECORDER_INTERVAL_SECONDS > 0 && MAKER_INTERVAL_SECONDS > 0 && MICRO_TAKER_INTERVAL_SECONDS > 0 )) || {
  echo "V6 runtime polling intervals must be positive" >&2
  exit 2
}

# Capital-isolated sleeves sum to parent capital. There is no operational
# mixture-of-experts: every sleeve has one economic task and its own execution.
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
        # External information refreshes once per minute. Re-discovering hundreds
        # of Gamma markets every five seconds only creates network load and does
        # not add external-information freshness.
        child['interval_seconds']=max(30,int(child.get('interval_seconds',5)))
    (root/f'{name}_config.json').write_text(json.dumps(child,indent=2,sort_keys=True)+'\n')
PY

rm -f "$RUN_ROOT"/intents.csv "$RUN_ROOT"/local_factor_intents.csv \
      "$RUN_ROOT"/relation_intents_raw.csv "$RUN_ROOT"/relation_intents.csv \
      "$RUN_ROOT"/stat_arb_pairs_diagnostic.csv

rec_pid=0; broker_pid=0; external_pid=0
rec_restarts=0; broker_restarts=0; external_restarts=0
start_recorder(){ ./build/polymarket_trade_recorder --config "$CONFIG" --run-dir "$RUN_ROOT" --markets "$RECORDER_MARKETS" --batch 40 --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --interval "$RECORDER_INTERVAL_SECONDS" --loop >>"$RUN_ROOT/trade_recorder.log" 2>&1 & rec_pid=$!; }
start_broker(){ ./build/polymarket_multileg_paper --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.75 --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 6 --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --interval 1 --loop >>"$RUN_ROOT/multileg.log" 2>&1 & broker_pid=$!; }
start_external(){ ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --paper --loop >>"$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!; }
write_supervisor(){
  local ra=0 ba=0 ea=0; kill -0 "$rec_pid" 2>/dev/null&&ra=1||true; kill -0 "$broker_pid" 2>/dev/null&&ba=1||true; kill -0 "$external_pid" 2>/dev/null&&ea=1||true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp"
  printf 'timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n' >"$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >>"$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}
cleanup(){ for p in "$rec_pid" "$broker_pid" "$external_pid";do if ((p>0));then kill "$p" 2>/dev/null||true;fi;done;for p in "$rec_pid" "$broker_pid" "$external_pid";do if ((p>0));then wait "$p" 2>/dev/null||true;fi;done; }
trap cleanup EXIT INT TERM

# Gamma discovery is comparatively expensive on the private Mac path. Give the
# recorder exclusive first access, start the network-light broker alongside it,
# then bring up the persistent external sleeve after a bounded gap. Auxiliary
# scanners below are phase shifted again rather than stampeding Gamma at t=0.
start_recorder;start_broker
if (( CORE_STARTUP_GAP_SECONDS > 0 ));then sleep "$CORE_STARTUP_GAP_SECONDS";fi
start_external;write_supervisor

startup_epoch=$(date +%s)
last_maker=$((startup_epoch-MAKER_INTERVAL_SECONDS+MAKER_STARTUP_DELAY_SECONDS))
last_micro_taker=$((startup_epoch-MICRO_TAKER_INTERVAL_SECONDS+MICRO_STARTUP_DELAY_SECONDS))
last_hard_arb=$((startup_epoch-10+HARD_ARB_STARTUP_DELAY_SECONDS))
last_relation=$((startup_epoch-30+RELATION_STARTUP_DELAY_SECONDS))
last_factor=$((startup_epoch-60+FACTOR_STARTUP_DELAY_SECONDS))
last_external=$((startup_epoch-60+EXTERNAL_STARTUP_DELAY_SECONDS))
last_report=$((startup_epoch-60+REPORT_STARTUP_DELAY_SECONDS))

rebuild_intents(){ python3 scripts/merge_v4_intents.py --input "$RUN_ROOT/local_factor_intents.csv" --input "$RUN_ROOT/relation_intents.csv" --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" --max-age-seconds 240 --max-bundles 120 >>"$RUN_ROOT/intent_merge.log" 2>&1||true; }

while true;do
  now="$(date +%s)"
  if ! kill -0 "$rec_pid" 2>/dev/null;then wait "$rec_pid" 2>/dev/null||true;rec_restarts=$((rec_restarts+1));start_recorder;fi
  if ! kill -0 "$broker_pid" 2>/dev/null;then wait "$broker_pid" 2>/dev/null||true;broker_restarts=$((broker_restarts+1));start_broker;fi
  if ! kill -0 "$external_pid" 2>/dev/null;then wait "$external_pid" 2>/dev/null||true;external_restarts=$((external_restarts+1));start_external;fi
  write_supervisor

  # Micro maker: passive spread capture with queue/fill/adverse-selection accounting.
  if ((now-startup_epoch>=MAKER_STARTUP_DELAY_SECONDS && now-last_maker>=MAKER_INTERVAL_SECONDS));then
    ./build/polymarket_maker_paper --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge 0.00035 --max-order-usd 25 --ttl-seconds 90 --hold-seconds 240 --adverse-selection-mult 0.15 --once >>"$RUN_ROOT/maker.log" 2>&1||true
    last_maker=$now
  fi

  # Micro taker: online short-horizon markout model; mandatory horizon exit.
  if ((now-last_micro_taker>=MICRO_TAKER_INTERVAL_SECONDS));then
    python3 scripts/v6_micro_taker.py --config "$RUN_ROOT/micro_taker_config.json" --run-dir "$RUN_ROOT/micro_taker" --markets 250 --min-liquidity 25 --horizon-seconds 30 --max-trade-usd 15 --min-edge 0.00030 --slippage-bps 5 --max-positions 20 >>"$RUN_ROOT/micro_taker.log" 2>&1||true
    last_micro_taker=$now
  fi

  # Hard graph arbitrage: complete non-augmented NegRisk sets only. Admission is
  # all-or-none against displayed ask depth in one snapshot, after fee/slippage.
  if ((now-last_hard_arb>=10));then
    python3 scripts/v6_hard_arb_paper.py --config "$RUN_ROOT/hard_arb_config.json" --run-dir "$RUN_ROOT/hard_arb" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --max-events 80 --min-edge 0.00020 --max-trade-usd 60 --slippage-bps 5 >>"$RUN_ROOT/hard_arb.log" 2>&1||true
    last_hard_arb=$now
  fi

  if ((now-last_factor>=60));then
    rm -f "$RUN_ROOT/local_factor_intents.csv" "$RUN_ROOT/stat_arb_pairs_diagnostic.csv"
    # Legacy B1 is diagnostic only under strict gates and cannot generate V6 intents.
    ./build/polymarket_stat_arb --config "$RUN_ROOT/broker_config.json" --markets "$MARKETS" --history-universe 300 --lookback-hours 720 --fidelity-minutes 30 --min-z 1.25 --min-t-reversion 2.00 --max-half-life-hours 168 --top 150 --csv "$RUN_ROOT/stat_arb_pairs_diagnostic.csv" >"$RUN_ROOT/stat_arb_pairs_diagnostic.log" 2>"$RUN_ROOT/stat_arb_pairs_errors.log"||true
    # Production mean reversion: local panel, common sample and BH-FDR controlled.
    python3 scripts/v6_local_factor_intents.py --config "$CONFIG" --output "$RUN_ROOT/local_factor_intents.csv" --status "$RUN_ROOT/local_factor_status.json" --markets 400 --min-liquidity "$MIN_LIQUIDITY" --lookback-hours 336 --fidelity-minutes 60 --max-clusters 15 --min-common-points 48 --min-z 1.00 --fdr 0.10 --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --slippage-bps 5 >"$RUN_ROOT/local_factor_latest.log" 2>"$RUN_ROOT/local_factor_errors.log"||true
    rebuild_intents;last_factor=$now
  fi

  if ((now-last_relation>=30));then
    # Semantic parsing discovers relations only. Maker bundles are GRAPH_RV, not hard arb.
    python3 scripts/v6_relation_intents.py --config "$CONFIG" --output "$RUN_ROOT/relation_intents_raw.csv" --status "$RUN_ROOT/relation_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --max-events 80 >"$RUN_ROOT/relation_latest.log" 2>"$RUN_ROOT/relation_errors.log"||true
    python3 scripts/v6_intent_guard.py --input "$RUN_ROOT/relation_intents_raw.csv" --output "$RUN_ROOT/relation_intents.csv" --status "$RUN_ROOT/relation_guard_status.json" --min-edge "$INTENT_MIN_EDGE" --stress-bps 10 --max-age-seconds 240 >>"$RUN_ROOT/relation_guard.log" 2>&1||true
    rebuild_intents;last_relation=$now
  fi

  if ((now-last_external>=60));then
    python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.35 >"$RUN_ROOT/external_bridge.log" 2>&1||true
    last_external=$now
  fi

  if ((now-last_report>=60));then
    python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$RUN_ROOT" >"$RUN_ROOT/runtime_status.log" 2>&1||true
    python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" --window-seconds 3600 --production-edge "$INTENT_MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" --output-markdown "$RUN_ROOT/action_report.md" >"$RUN_ROOT/action_report_latest.log" 2>&1||true
    last_report=$now
  fi
  sleep 5
done
