#!/usr/bin/env bash
set -euo pipefail

source scripts/v6_task_runtime.sh

CONFIG="${1:-config/paper_v6.json}"
BASE_CONFIG="$CONFIG"
RUN_ROOT="${2:-runs/paper_v6_live}"
MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-10}"
MARKETS="${V6_MARKETS:-700}"
RECORDER_MARKETS="${V6_RECORDER_MARKETS:-1200}"
INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-0.00020}"
RUNTIME_PARENT_PID="${POLYMARKET_RUNTIME_PARENT_PID:-}"
V6_TASK_STATUS_DIR="$RUN_ROOT/task_status"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/hard_arb" "$RUN_ROOT/external" "$V6_TASK_STATUS_DIR"

# V6 market discovery is routed through a local read-only resilience proxy. It
# prefers Gamma keyset pagination (the current stable API), falls back to public
# CLOB market+book data, and only then uses a bounded stale metadata cache.
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

# Research controls are explicit in the paper manifest.  They remain within the
# existing micro sleeve and cannot turn on authenticated execution.
read -r GRAPH_JOINT_OBSERVATIONS GRAPH_LATENCY_BPS GRAPH_COMPLETION GRAPH_COST_QUANTILE EXPLORATION_ENABLED EXPLORATION_MAX_TRADE EXPLORATION_MAX_OPENS EXPLORATION_MAX_POSITIONS EXPLORATION_HOLD EXPLORATION_MIN_ACTIVITY < <(python3 - "$CONFIG" <<'PY'
import json, sys
from pathlib import Path
cfg=json.loads(Path(sys.argv[1]).read_text()); v=cfg.get('v6') or {}
graph=v.get('graph') or {}; exploration=v.get('micro_taker_exploration') or {}
assert graph.get('mode') == 'research_only'
assert graph.get('broker_routing_enabled') is False
assert bool(exploration.get('paper_only'))
assert 30 <= int(exploration.get('hold_seconds', 45)) <= 60
assert 0.0 < float(exploration.get('max_trade_usd', 5.0)) <= 5.0
print(int(graph.get('min_joint_fill_observations', 30)), float(graph.get('capital_latency_bps', 1.0)),
      float(graph.get('completion_threshold', .75)), float(graph.get('cost_quantile', .75)),
      int(bool(exploration.get('enabled'))), float(exploration.get('max_trade_usd', 5.0)),
      int(exploration.get('max_opens_per_hour', 6)), int(exploration.get('max_positions', 2)),
      int(exploration.get('hold_seconds', 45)), int(exploration.get('min_activity_trades_60s', 1)))
PY
)

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
    # Keep the global $/trade ceiling economically reachable inside the smaller
    # maker sleeve instead of accidentally shrinking it to 2.5% of sleeve NAV.
    if name=='maker' and child['starting_capital']>0:
        trade_cap=float(cfg.get('max_trade_usd',60.0))
        child['max_market_fraction']=max(float(child.get('max_market_fraction',0.0)),min(1.0,trade_cap/child['starting_capital']))
    if name=='external':
        child['external_signals_file']=str(root/'external_signals.csv'); child['expert_weights']['external']=1.0; child['uncertainty_penalty']=0.0
    (root/f'{name}_config.json').write_text(json.dumps(child,indent=2,sort_keys=True)+'\n')
PY

rm -f "$RUN_ROOT"/intents.csv "$RUN_ROOT"/local_factor_intents.csv \
      "$RUN_ROOT"/relation_intents_raw.csv "$RUN_ROOT"/relation_intents.csv \
      "$RUN_ROOT"/graph_research_ev.csv "$RUN_ROOT"/graph_research_status.json \
      "$RUN_ROOT"/stat_arb_pairs_diagnostic.csv

proxy_pid=0; rec_pid=0; broker_pid=0; external_pid=0
maker_pid=0; micro_taker_pid=0; hard_arb_pid=0; factor_pid=0
relation_pid=0; external_bridge_pid=0; report_pid=0
proxy_restarts=0; rec_restarts=0; broker_restarts=0; external_restarts=0
start_proxy(){ PYTHONUNBUFFERED=1 python3 scripts/v6_market_proxy.py --host 127.0.0.1 --port "$MARKET_PROXY_PORT" --gamma "$GAMMA_URL" --clob "$CLOB_URL" --cache "$RUN_ROOT/market_proxy_cache.json" --status "$RUN_ROOT/market_proxy_status.json" >>"$RUN_ROOT/market_proxy.log" 2>&1 & proxy_pid=$!; }
start_recorder(){ ./build/polymarket_trade_recorder --config "$CONFIG" --run-dir "$RUN_ROOT" --markets "$RECORDER_MARKETS" --batch 40 --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --interval 5 --loop >>"$RUN_ROOT/trade_recorder.log" 2>&1 & rec_pid=$!; }
start_broker(){ python3 scripts/v6_multileg_launcher.py --lock "$RUN_ROOT/multileg.lock" -- ./build/polymarket_multileg_paper --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.75 --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 6 --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --interval 1 --loop >>"$RUN_ROOT/multileg.log" 2>&1 & broker_pid=$!; }
start_external(){ ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --paper --loop >>"$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!; }
write_supervisor(){
  local ra=0 ba=0 ea=0; kill -0 "$rec_pid" 2>/dev/null&&ra=1||true; kill -0 "$broker_pid" 2>/dev/null&&ba=1||true; kill -0 "$external_pid" 2>/dev/null&&ea=1||true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp.${BASHPID:-$$}"
  printf 'timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n' >"$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >>"$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}
cleanup(){
  # Stop task wrappers first so each wrapper can forward TERM to its active
  # scanner and publish a terminal status.  Both phases are bounded and use
  # KILL only after the TERM grace period.
  v6_task_terminate_pids "$maker_pid" "$micro_taker_pid" "$hard_arb_pid" "$factor_pid" \
    "$relation_pid" "$external_bridge_pid" "$report_pid"
  v6_task_terminate_pids "$rec_pid" "$broker_pid" "$external_pid" "$proxy_pid"
}
shutdown(){ trap - EXIT INT TERM; cleanup; exit 0; }
parent_runtime_alive(){ [[ -z "$RUNTIME_PARENT_PID" ]] || kill -0 "$RUNTIME_PARENT_PID" 2>/dev/null; }
is_orphan_v6_loop(){
  local pid="$1" parent
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] || return 1
  parent="$(/bin/ps -o ppid= -p "$pid" 2>/dev/null | /usr/bin/tr -d '[:space:]')"
  [[ "$parent" == "1" ]]
}
reap_orphan_v6_loops(){
  # A pre-handoff loop from an older deployment cannot know about the parent
  # liveness contract above. It is safe to retire only a same-repository V6
  # loop that launchd has already orphaned (PPID 1); a live sibling fails
  # closed rather than being touched.
  [[ -n "$RUNTIME_PARENT_PID" ]] || return 0
  command -v pgrep >/dev/null 2>&1 || return 0
  local pid remaining attempt
  for ((attempt=1; attempt<=25; ++attempt)); do
    remaining=0
    while IFS= read -r pid; do
      if is_orphan_v6_loop "$pid"; then
        remaining=1
        if (( attempt == 1 )); then
          echo "orphan_v6_loop_reaped=$pid" >&2
          kill -TERM "$pid" 2>/dev/null || true
        fi
      fi
    done < <(pgrep -f "$ROOT/scripts/paper_v6_loop.sh" 2>/dev/null || true)
    (( remaining == 0 )) && return 0
    sleep 0.2
  done
  echo "fatal: orphaned V6 loop did not exit before startup" >&2
  return 1
}
trap cleanup EXIT
trap shutdown INT TERM
reap_orphan_v6_loops
start_proxy
proxy_ready=0
for _ in {1..50};do
  if curl -fsS "http://127.0.0.1:${MARKET_PROXY_PORT}/healthz" >/dev/null 2>&1;then proxy_ready=1;break;fi
  kill -0 "$proxy_pid" 2>/dev/null||break
  sleep 0.1
done
((proxy_ready==1))||{ echo "fatal: V6 market proxy failed to start" >&2; exit 1; }
start_recorder;start_broker;start_external;write_supervisor

# Backward-safe deployment: new queue-aware flags are used as soon as the rebuilt
# binary exposes them; an older binary can keep running until the rebuild lands.
MAKER_QUEUE_ARGS=()
if ./build/polymarket_maker_paper --help 2>&1 | grep -q -- '--improve-ticks'; then
  MAKER_QUEUE_ARGS=(--improve-ticks 1 --max-queue-multiple 6)
fi
EXPLORATION_ARGS=()
if [[ "$EXPLORATION_ENABLED" == "1" ]]; then
  EXPLORATION_ARGS=(--exploration-enabled --exploration-max-trade-usd "$EXPLORATION_MAX_TRADE" --exploration-max-opens-per-hour "$EXPLORATION_MAX_OPENS" --exploration-max-positions "$EXPLORATION_MAX_POSITIONS" --exploration-hold-seconds "$EXPLORATION_HOLD" --exploration-min-activity "$EXPLORATION_MIN_ACTIVITY")
fi

last_factor=0;last_relation=0;last_external=0;last_report=0;last_micro_taker=0;last_hard_arb=0
# The broker consumes only the independently executable local-factor sleeve.
# GRAPH_RV remains running as a research scanner, but its raw spread is never
# presented to the broker as an execution edge.
task_rc=0
capture_task_failure(){ local rc="$1"; if ((task_rc == 0));then task_rc="$rc";fi; }
task_rebuild_intents(){
  v6_task_run_child python3 scripts/merge_v4_intents.py --input "$RUN_ROOT/local_factor_intents.csv" --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" --max-age-seconds 240 --max-bundles 120 >>"$RUN_ROOT/intent_merge.log" 2>&1 || capture_task_failure "$?"
}

task_maker(){
  task_rc=0
  v6_task_run_child ./build/polymarket_maker_paper --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-order-usd 60 --ttl-seconds 90 --hold-seconds 240 --adverse-selection-mult 0.10 "${MAKER_QUEUE_ARGS[@]}" --once >>"$RUN_ROOT/maker.log" 2>&1 || capture_task_failure "$?"
  return "$task_rc"
}

task_micro_taker(){
  task_rc=0
  v6_task_run_child python3 scripts/v6_queue_filter.py micro --config "$RUN_ROOT/micro_taker_config.json" --run-dir "$RUN_ROOT/micro_taker" --markets 250 --min-liquidity 25 --horizon-seconds 30 --max-target-staleness-seconds 10 --max-trade-usd 15 --min-edge 0.00030 --slippage-bps 5 --max-positions 20 --trade-tape "$RUN_ROOT/trade_tape.csv" "${EXPLORATION_ARGS[@]}" >>"$RUN_ROOT/micro_taker.log" 2>&1 || capture_task_failure "$?"
  return "$task_rc"
}

task_hard_arb(){
  task_rc=0
  v6_task_run_child python3 scripts/v6_queue_filter.py hard --config "$RUN_ROOT/hard_arb_config.json" --run-dir "$RUN_ROOT/hard_arb" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --max-events 80 --min-edge 0.00020 --max-trade-usd 60 --slippage-bps 5 --leg-latency-ms 100 >>"$RUN_ROOT/hard_arb.log" 2>&1 || capture_task_failure "$?"
  return "$task_rc"
}

task_factor(){
  task_rc=0
  rm -f "$RUN_ROOT/local_factor_intents.csv" "$RUN_ROOT/stat_arb_pairs_diagnostic.csv"
  # Legacy B1 is diagnostic only under strict gates and cannot generate V6 intents.
  v6_task_run_child ./build/polymarket_stat_arb --config "$RUN_ROOT/broker_config.json" --markets "$MARKETS" --history-universe 300 --lookback-hours 720 --fidelity-minutes 30 --min-z 1.25 --min-t-reversion 2.00 --max-half-life-hours 168 --top 150 --csv "$RUN_ROOT/stat_arb_pairs_diagnostic.csv" >"$RUN_ROOT/stat_arb_pairs_diagnostic.log" 2>"$RUN_ROOT/stat_arb_pairs_errors.log" || capture_task_failure "$?"
  # Production mean reversion: local panel, common sample and BH-FDR controlled.
  v6_task_run_child python3 scripts/v6_local_factor_intents.py --config "$CONFIG" --output "$RUN_ROOT/local_factor_intents.csv" --status "$RUN_ROOT/local_factor_status.json" --markets 400 --min-liquidity "$MIN_LIQUIDITY" --lookback-hours 336 --fidelity-minutes 60 --max-clusters 15 --min-common-points 48 --min-z 1.00 --fdr 0.10 --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --slippage-bps 5 >"$RUN_ROOT/local_factor_latest.log" 2>"$RUN_ROOT/local_factor_errors.log" || capture_task_failure "$?"
  task_rebuild_intents
  return "$task_rc"
}

task_relation(){
  task_rc=0
  # Semantic parsing remains enabled for Graph research.  The guard keeps its
  # contract semantics strict; graph_research_ev replaces scanner edge with
  # conditional joint-fill EV and writes no broker intent.
  v6_task_run_child python3 scripts/v6_relation_intents.py --config "$CONFIG" --output "$RUN_ROOT/relation_intents_raw.csv" --status "$RUN_ROOT/relation_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --max-events 80 >"$RUN_ROOT/relation_latest.log" 2>"$RUN_ROOT/relation_errors.log" || capture_task_failure "$?"
  v6_task_run_child python3 scripts/v6_intent_guard.py --input "$RUN_ROOT/relation_intents_raw.csv" --output "$RUN_ROOT/relation_intents.csv" --status "$RUN_ROOT/relation_guard_status.json" --min-edge "$INTENT_MIN_EDGE" --stress-bps 10 --max-age-seconds 240 >>"$RUN_ROOT/relation_guard.log" 2>&1 || capture_task_failure "$?"
  v6_task_run_child python3 scripts/graph_research_ev.py --input "$RUN_ROOT/relation_intents.csv" --ledger "$RUN_ROOT/bundle_ledger.csv" --events "$RUN_ROOT/multileg_events.csv" --output "$RUN_ROOT/graph_research_ev.csv" --status "$RUN_ROOT/graph_research_status.json" --min-observations "$GRAPH_JOINT_OBSERVATIONS" --minimum-ev-usd 0 --capital-latency-bps "$GRAPH_LATENCY_BPS" --completion-threshold "$GRAPH_COMPLETION" --cost-quantile "$GRAPH_COST_QUANTILE" >>"$RUN_ROOT/graph_research.log" 2>&1 || capture_task_failure "$?"
  return "$task_rc"
}

task_external_bridge(){
  task_rc=0
  v6_task_run_child python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.35 >"$RUN_ROOT/external_bridge.log" 2>&1 || capture_task_failure "$?"
  return "$task_rc"
}

task_report(){
  task_rc=0
  v6_task_run_child python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$RUN_ROOT" >"$RUN_ROOT/runtime_status.log" 2>&1 || capture_task_failure "$?"
  v6_task_run_child python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" --window-seconds 3600 --production-edge "$INTENT_MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" --output-markdown "$RUN_ROOT/action_report.md" >"$RUN_ROOT/action_report_latest.log" 2>&1 || capture_task_failure "$?"
  v6_task_run_child python3 scripts/v7_execution_evidence.py --run-root "$RUN_ROOT" --policy config/v7_execution_evidence.json >"$RUN_ROOT/v7_execution_evidence.log" 2>&1 || capture_task_failure "$?"
  return "$task_rc"
}

reap_finished_tasks(){
  if v6_task_reap_if_finished "$maker_pid";then maker_pid=0;fi
  if v6_task_reap_if_finished "$micro_taker_pid";then micro_taker_pid=0;fi
  if v6_task_reap_if_finished "$hard_arb_pid";then hard_arb_pid=0;fi
  if v6_task_reap_if_finished "$factor_pid";then factor_pid=0;fi
  if v6_task_reap_if_finished "$relation_pid";then relation_pid=0;fi
  if v6_task_reap_if_finished "$external_bridge_pid";then external_bridge_pid=0;fi
  if v6_task_reap_if_finished "$report_pid";then report_pid=0;fi
}

while true;do
  if ! parent_runtime_alive;then
    echo "runtime_parent_lost=1; exiting V6 children for a clean runtime handoff" >&2
    exit 0
  fi
  now="$(date +%s)"
  if ! kill -0 "$proxy_pid" 2>/dev/null;then wait "$proxy_pid" 2>/dev/null||true;proxy_restarts=$((proxy_restarts+1));start_proxy;sleep 1;fi
  if ! kill -0 "$rec_pid" 2>/dev/null;then wait "$rec_pid" 2>/dev/null||true;rec_restarts=$((rec_restarts+1));start_recorder;fi
  if ! kill -0 "$broker_pid" 2>/dev/null;then wait "$broker_pid" 2>/dev/null||true;broker_restarts=$((broker_restarts+1));start_broker;fi
  if ! kill -0 "$external_pid" 2>/dev/null;then wait "$external_pid" 2>/dev/null||true;external_restarts=$((external_restarts+1));start_external;fi
  write_supervisor
  reap_finished_tasks

  # Micro maker: edge-aware inside-spread improvement plus FIFO queue gating.
  # Do not fake fills: the actual paper fill still requires compatible taker SELL flow.
  if ((maker_pid == 0));then
    v6_task_start maker "$now" task_maker
    maker_pid="$V6_TASK_STARTED_PID"
  fi

  # Micro taker: depth-aware paper execution.  The separate exploration sleeve
  # is capped at $5 per entry, exits at 45 seconds, and measures actual public
  # book fees, markouts, and activity/depth strata without touching real orders.
  if ((micro_taker_pid == 0 && now-last_micro_taker >= 5));then
    v6_task_start micro_taker "$now" task_micro_taker
    micro_taker_pid="$V6_TASK_STARTED_PID"
    last_micro_taker=$now
  fi

  # Hard graph arbitrage: complete non-augmented NegRisk sets only.  The paper
  # executor walks displayed depth, pays market-specific fees/slippage, and
  # revalidates every remaining leg after the configured inter-leg latency.
  # Partial sequences are immediately unwound and tracked until flat.
  if ((hard_arb_pid == 0 && now-last_hard_arb >= 10));then
    v6_task_start hard_arb "$now" task_hard_arb
    hard_arb_pid="$V6_TASK_STARTED_PID"
    last_hard_arb=$now
  fi

  if ((factor_pid == 0 && now-last_factor >= 60));then
    v6_task_start factor "$now" task_factor
    factor_pid="$V6_TASK_STARTED_PID"
    last_factor=$now
  fi

  if ((relation_pid == 0 && now-last_relation >= 30));then
    v6_task_start relation "$now" task_relation
    relation_pid="$V6_TASK_STARTED_PID"
    last_relation=$now
  fi

  if ((external_bridge_pid == 0 && now-last_external >= 60));then
    v6_task_start external_bridge "$now" task_external_bridge
    external_bridge_pid="$V6_TASK_STARTED_PID"
    last_external=$now
  fi

  if ((report_pid == 0 && now-last_report >= 60));then
    v6_task_start report "$now" task_report
    report_pid="$V6_TASK_STARTED_PID"
    last_report=$now
  fi
  sleep 5
done
