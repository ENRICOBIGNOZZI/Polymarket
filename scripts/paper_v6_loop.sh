#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG="${1:-config/paper_v6.json}"
BASE_CONFIG="$CONFIG"
RUN_ROOT="${2:-runs/paper_v6_live}"
MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-10}"
MARKETS="${V6_MARKETS:-700}"
RECORDER_MARKETS="${V6_RECORDER_MARKETS:-1200}"
INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-0.00020}"
RUNTIME_PARENT_PID="${POLYMARKET_RUNTIME_PARENT_PID:-}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/hard_arb" "$RUN_ROOT/external"

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
      "$RUN_ROOT"/stat_arb_pairs_diagnostic.csv

proxy_pid=0; rec_pid=0; broker_pid=0; external_pid=0
proxy_restarts=0; rec_restarts=0; broker_restarts=0; external_restarts=0
start_proxy(){ PYTHONUNBUFFERED=1 python3 scripts/v6_market_proxy.py --host 127.0.0.1 --port "$MARKET_PROXY_PORT" --gamma "$GAMMA_URL" --clob "$CLOB_URL" --cache "$RUN_ROOT/market_proxy_cache.json" --status "$RUN_ROOT/market_proxy_status.json" >>"$RUN_ROOT/market_proxy.log" 2>&1 & proxy_pid=$!; }
start_recorder(){ ./build/polymarket_trade_recorder --config "$CONFIG" --run-dir "$RUN_ROOT" --markets "$RECORDER_MARKETS" --batch 40 --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --interval 5 --loop >>"$RUN_ROOT/trade_recorder.log" 2>&1 & rec_pid=$!; }
start_broker(){ python3 scripts/v6_multileg_launcher.py --lock "$RUN_ROOT/multileg.lock" -- ./build/polymarket_multileg_paper --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.75 --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 6 --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --interval 1 --loop >>"$RUN_ROOT/multileg.log" 2>&1 & broker_pid=$!; }
refresh_external_feed(){
  python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.35 >"$RUN_ROOT/external_bridge.log" 2>&1 || true
  if [[ ! -s "$RUN_ROOT/external_signals.csv" ]]; then
    local tmp="$RUN_ROOT/external_signals.csv.tmp.${BASHPID:-$$}"
    printf 'market_key,q_yes,confidence,source,timestamp\n' >"$tmp"
    mv "$tmp" "$RUN_ROOT/external_signals.csv"
  fi
}
start_external(){ ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --paper --loop >>"$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!; }
write_supervisor(){
  local ra=0 ba=0 ea=0; kill -0 "$rec_pid" 2>/dev/null&&ra=1||true; kill -0 "$broker_pid" 2>/dev/null&&ba=1||true; kill -0 "$external_pid" 2>/dev/null&&ea=1||true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp.${BASHPID:-$$}"
  printf 'timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n' >"$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >>"$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}
cleanup(){ for p in "$rec_pid" "$broker_pid" "$external_pid" "$proxy_pid";do if ((p>0));then kill "$p" 2>/dev/null||true;fi;done;for p in "$rec_pid" "$broker_pid" "$external_pid" "$proxy_pid";do if ((p>0));then wait "$p" 2>/dev/null||true;fi;done; }
shutdown(){ trap - EXIT INT TERM; cleanup; exit 0; }
parent_runtime_alive(){ [[ -z "$RUNTIME_PARENT_PID" ]] || kill -0 "$RUNTIME_PARENT_PID" 2>/dev/null; }
is_current_runtime_descendant(){
  local pid="$1" parent hops=0
  while [[ "$pid" =~ ^[1-9][0-9]*$ && "$hops" -lt 64 ]]; do
    [[ "$pid" == "$RUNTIME_PARENT_PID" ]] && return 0
    parent="$(/bin/ps -o ppid= -p "$pid" 2>/dev/null | /usr/bin/tr -d '[:space:]')"
    [[ "$parent" =~ ^[1-9][0-9]*$ && "$parent" != "$pid" ]] || return 1
    pid="$parent"
    hops=$((hops + 1))
  done
  return 1
}
process_cwd(){
  local pid="$1"
  command -v lsof >/dev/null 2>&1 || return 1
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | /usr/bin/sed -n 's/^n//p' | /usr/bin/head -n 1
}
process_command(){ /bin/ps -o command= -p "$1" 2>/dev/null || true; }
process_executable(){ /bin/ps -o comm= -p "$1" 2>/dev/null || true; }
is_stale_v6_loop(){
  local pid="$1"
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] || return 1
  ! is_current_runtime_descendant "$pid"
}
is_same_repository_v6_loop(){
  local pid="$1" command_line cwd
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  command_line="$(process_command "$pid")"
  [[ "$command_line" == *"paper_v6_loop.sh"* ]] || return 1
  # Current launchers use an absolute script path.  Older macOS launchd
  # instances can retain `bash scripts/paper_v6_loop.sh`; accept that form
  # only after the process working directory proves it is this repository.
  [[ "$command_line" == *"$ROOT/scripts/paper_v6_loop.sh"* ]] && return 0
  cwd="$(process_cwd "$pid")"
  [[ "$cwd" == "$ROOT" ]]
}
reap_stale_v6_loops(){
  # A pre-handoff loop from an older deployment cannot know about the parent
  # liveness contract above. Retire only a same-repository V6 loop outside the
  # current runtime-owner ancestry; an active descendant is never touched.
  [[ -n "$RUNTIME_PARENT_PID" ]] || return 0
  command -v pgrep >/dev/null 2>&1 || return 0
  local pid remaining attempt
  for ((attempt=1; attempt<=25; ++attempt)); do
    remaining=0
    while IFS= read -r pid; do
      if is_same_repository_v6_loop "$pid" && is_stale_v6_loop "$pid"; then
        remaining=1
        if (( attempt == 1 )); then
          echo "stale_v6_loop_reaped=$pid" >&2
          kill -TERM "$pid" 2>/dev/null || true
        fi
      fi
    done < <(pgrep -f 'paper_v6_loop\.sh' 2>/dev/null || true)
    (( remaining == 0 )) && return 0
    sleep 0.2
  done
  echo "fatal: stale V6 loop did not exit before startup" >&2
  return 1
}
proxy_listener_pids(){
  command -v lsof >/dev/null 2>&1 || return 1
  lsof -nP -t -iTCP:"$MARKET_PROXY_PORT" -sTCP:LISTEN 2>/dev/null || true
}
is_same_repository_v6_proxy_listener(){
  local pid="$1" command_line executable cwd
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] || return 1
  command_line="$(process_command "$pid")"
  executable="$(process_executable "$pid")"
  [[ "$command_line" == *"v6_market_proxy.py"* ]] || return 1
  # Require the real Python interpreter as well as its argv.  This excludes
  # pgrep/diagnostic commands that happen to mention the script name.
  [[ "$executable" == *"python"* || "$executable" == *"Python"* ]] || return 1
  cwd="$(process_cwd "$pid")"
  [[ "$cwd" == "$ROOT" ]]
}
is_v6_broker_for_run(){
  local pid="$1" command_line executable
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "$$" ]] || return 1
  command_line="$(process_command "$pid")"
  executable="$(process_executable "$pid")"
  [[ "$executable" == *"polymarket_multileg_paper"* ]] || return 1
  [[ "$command_line" == *"--run-dir"* && "$command_line" == *"$RUN_ROOT"* ]]
}
is_same_repository_v6_broker(){
  local pid="$1" cwd
  is_v6_broker_for_run "$pid" || return 1
  cwd="$(process_cwd "$pid")"
  [[ "$cwd" == "$ROOT" ]]
}
reap_stale_v6_proxy_listener(){
  # A loop that died before its child process group could be collected leaves a
  # live old proxy on the fixed localhost port.  Merely curling /healthz would
  # then bless that old binary and make the replacement loop restart a proxy
  # that can never bind.  Retire only a same-repository Python proxy outside
  # the new singleton owner's ancestry; a foreign listener stops startup.
  [[ -n "$RUNTIME_PARENT_PID" ]] || return 0
  command -v lsof >/dev/null 2>&1 || { echo "fatal: V6 proxy ownership requires lsof" >&2; return 1; }
  local pid remaining attempt
  for ((attempt=1; attempt<=25; ++attempt)); do
    remaining=0
    while IFS= read -r pid; do
      [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
      is_current_runtime_descendant "$pid" && continue
      if ! is_same_repository_v6_proxy_listener "$pid"; then
        echo "fatal: V6 market proxy port $MARKET_PROXY_PORT is owned by unverified pid=$pid" >&2
        return 1
      fi
      remaining=1
      if (( attempt == 1 )); then
        echo "stale_v6_proxy_listener_reaped=$pid" >&2
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done < <(proxy_listener_pids)
    (( remaining == 0 )) && return 0
    sleep 0.2
  done
  echo "fatal: stale V6 proxy listener did not exit before startup" >&2
  return 1
}
reap_stale_v6_brokers(){
  # The multi-leg launcher owns a lock, but an older orphan may still be
  # writing shared state before that lock is observed.  Retire only an exact
  # same-repository broker for this run root outside the active owner tree.
  [[ -n "$RUNTIME_PARENT_PID" ]] || return 0
  command -v pgrep >/dev/null 2>&1 || return 0
  command -v lsof >/dev/null 2>&1 || { echo "fatal: V6 broker ownership requires lsof" >&2; return 1; }
  local pid remaining attempt
  for ((attempt=1; attempt<=25; ++attempt)); do
    remaining=0
    while IFS= read -r pid; do
      is_v6_broker_for_run "$pid" || continue
      is_current_runtime_descendant "$pid" && continue
      if ! is_same_repository_v6_broker "$pid"; then
        echo "fatal: V6 multi-leg broker for $RUN_ROOT is owned by unverified pid=$pid" >&2
        return 1
      fi
      remaining=1
      if (( attempt == 1 )); then
        echo "stale_v6_broker_reaped=$pid" >&2
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done < <(pgrep -f 'polymarket_multileg_paper' 2>/dev/null || true)
    (( remaining == 0 )) && return 0
    sleep 0.2
  done
  echo "fatal: stale V6 multi-leg broker did not exit before startup" >&2
  return 1
}
proxy_pid_owns_port(){
  [[ "$proxy_pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$proxy_pid" 2>/dev/null || return 1
  local pid
  while IFS= read -r pid; do
    [[ "$pid" == "$proxy_pid" ]] && return 0
  done < <(proxy_listener_pids)
  return 1
}
wait_for_owned_proxy(){
  local attempt
  for ((attempt=1; attempt<=50; ++attempt)); do
    if proxy_pid_owns_port && curl -fsS --max-time 1 "http://127.0.0.1:${MARKET_PROXY_PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    kill -0 "$proxy_pid" 2>/dev/null || break
    sleep 0.1
  done
  return 1
}
trap cleanup EXIT
trap shutdown INT TERM
reap_stale_v6_loops
reap_stale_v6_proxy_listener
reap_stale_v6_brokers
start_proxy
wait_for_owned_proxy || { echo "fatal: V6 market proxy failed to start with verified port ownership" >&2; exit 1; }
refresh_external_feed
start_recorder;start_broker;start_external;write_supervisor

# Backward-safe deployment: new queue-aware flags are used as soon as the rebuilt
# binary exposes them; an older binary can keep running until the rebuild lands.
MAKER_QUEUE_ARGS=()
if ./build/polymarket_maker_paper --help 2>&1 | grep -q -- '--improve-ticks'; then
  MAKER_QUEUE_ARGS=(--improve-ticks 1 --max-queue-multiple 6)
fi

last_factor=0;last_relation=0;last_external="$(date +%s)";last_report=0;last_micro_taker=0;last_hard_arb=0
rebuild_intents(){ python3 scripts/merge_v4_intents.py --input "$RUN_ROOT/local_factor_intents.csv" --input "$RUN_ROOT/relation_intents.csv" --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" --max-age-seconds 240 --max-bundles 120 >>"$RUN_ROOT/intent_merge.log" 2>&1||true; }

while true;do
  if ! parent_runtime_alive;then
    echo "runtime_parent_lost=1; exiting V6 children for a clean runtime handoff" >&2
    exit 0
  fi
  now="$(date +%s)"
  if ! proxy_pid_owns_port || ! curl -fsS --max-time 1 "http://127.0.0.1:${MARKET_PROXY_PORT}/healthz" >/dev/null 2>&1;then
    # Do not spin forever against an old listener or a live-but-wedged child.
    # Exiting lets the singleton parent perform one clean, provenance-checked
    # handoff rather than allowing the recorder to keep timing out silently.
    echo "fatal: V6 market proxy lost verified listener ownership or health" >&2
    exit 1
  fi
  if ! kill -0 "$rec_pid" 2>/dev/null;then wait "$rec_pid" 2>/dev/null||true;rec_restarts=$((rec_restarts+1));start_recorder;fi
  if ! kill -0 "$broker_pid" 2>/dev/null;then wait "$broker_pid" 2>/dev/null||true;broker_restarts=$((broker_restarts+1));start_broker;fi
  if ! kill -0 "$external_pid" 2>/dev/null;then wait "$external_pid" 2>/dev/null||true;external_restarts=$((external_restarts+1));start_external;fi
  write_supervisor

  # Micro maker: edge-aware inside-spread improvement plus FIFO queue gating.
  # Do not fake fills: the actual paper fill still requires compatible taker SELL flow.
  ./build/polymarket_maker_paper --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-order-usd 60 --ttl-seconds 90 --hold-seconds 240 --adverse-selection-mult 0.10 "${MAKER_QUEUE_ARGS[@]}" --once >>"$RUN_ROOT/maker.log" 2>&1||true

  # Micro taker: online short-horizon markout model; mandatory horizon exit.
  if ((now-last_micro_taker>=5));then
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
    refresh_external_feed
    last_external=$now
  fi

  if ((now-last_report>=60));then
    python3 scripts/v6_runtime_status.py --config "$CONFIG" --run-root "$RUN_ROOT" >"$RUN_ROOT/runtime_status.log" 2>&1||true
    python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" --window-seconds 3600 --production-edge "$INTENT_MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" --output-markdown "$RUN_ROOT/action_report.md" >"$RUN_ROOT/action_report_latest.log" 2>&1||true
    python3 scripts/v7_execution_evidence.py --run-root "$RUN_ROOT" --policy config/v7_execution_evidence.json >"$RUN_ROOT/v7_execution_evidence.log" 2>&1||true
    last_report=$now
  fi
  sleep 5
done