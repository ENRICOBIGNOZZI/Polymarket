#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
    local tmp="$RUN_ROOT/external_signals.csv.tmp"
    printf 'market_key,q_yes,confidence,source,timestamp\n' >"$tmp"
    mv "$tmp" "$RUN_ROOT/external_signals.csv"
  fi
}
seed_market_proxy_cache(){
  local target="$RUN_ROOT/market_proxy_cache.json"
  local seed="${V6_MARKET_PROXY_SEED_CACHE:-$HOME/polymarket/runs/paper_v6_live/market_proxy_cache.json}"
  [[ -s "$target" || ! -s "$seed" ]] && return 0
  python3 - "$seed" "$target" <<'PY'
import json, os, sys, time
from pathlib import Path
src, dst = map(Path, sys.argv[1:])
try:
    value = json.loads(src.read_text(encoding='utf-8'))
    markets = value.get('markets') or []
    age = time.time() - float(value.get('timestamp') or 0)
    valid = (
        value.get('schema') == 'polymarket_v6_market_proxy_cache_v1'
        and 0 <= age <= 300
        and isinstance(markets, list) and len(markets) > 0
        and all(isinstance(row, dict) and str(row.get('id') or '') and str(row.get('conditionId') or '') for row in markets)
    )
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    valid = False
if valid:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + f'.seed.{os.getpid()}')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(tmp, dst)
    print(f'v6_market_cache_seeded source={src} markets={len(markets)} age_seconds={age:.3f}')
PY
}
warm_market_proxy(){
  # Readiness proves that the public discovery path can return valid market rows;
  # it is intentionally independent of the paper-liquidity admission filter so a
  # CLOB fallback without Gamma liquidity metadata is not mistaken for an outage.
  local url="http://127.0.0.1:${MARKET_PROXY_PORT}/markets?active=true&closed=false&limit=100&offset=0&order=liquidityNum&ascending=false&liquidity_num_min=0"
  for _ in {1..4}; do
    if curl -fsS --max-time 5 "$url" | python3 -c 'import json,sys; rows=json.load(sys.stdin); assert isinstance(rows,list) and len(rows)>0' >/dev/null 2>&1; then
      return 0
    fi
    kill -0 "$proxy_pid" 2>/dev/null || return 1
    sleep 1
  done
  if [[ -s "$RUN_ROOT/market_proxy_status.json" ]]; then
    cat "$RUN_ROOT/market_proxy_status.json" >&2 || true
  fi
  if [[ -s "$RUN_ROOT/market_proxy.log" ]]; then
    tail -n 80 "$RUN_ROOT/market_proxy.log" >&2 || true
  fi
  return 1
}
start_external(){ ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --paper --loop >>"$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!; }
write_supervisor(){
  local ra=0 ba=0 ea=0; kill -0 "$rec_pid" 2>/dev/null&&ra=1||true; kill -0 "$broker_pid" 2>/dev/null&&ba=1||true; kill -0 "$external_pid" 2>/dev/null&&ea=1||true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp.${BASHPID:-$$}"
  printf 'timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n' >"$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >>"$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}
report_startup_ready(){
  python3 - "$RUN_ROOT/market_proxy_status.json" "$RUN_ROOT/external_signals.csv" "$rec_pid" "$broker_pid" "$external_pid" <<'PY'
import csv, json, sys
status_path, feed_path, rec_pid, broker_pid, external_pid = sys.argv[1:]
status = json.load(open(status_path, encoding='utf-8'))
assert int(status.get('markets') or 0) > 0, status
source = str(status.get('source') or '')
assert source, status
age = float(status.get('cache_age_seconds') or 0.0)
with open(feed_path, encoding='utf-8', newline='') as handle:
    rows = list(csv.reader(handle))
assert rows and rows[0] == ['market_key','q_yes','confidence','source','timestamp'], rows[:1]
for pid in (rec_pid, broker_pid, external_pid):
    assert int(pid) > 0
print(
    'v6_startup_data_ready '
    f'source={source} markets={int(status.get("markets") or 0)} '
    f'cache_age_seconds={age:.3f} external_feed_rows={max(0, len(rows)-1)} '
    f'recorder_pid={rec_pid} broker_pid={broker_pid} external_pid={external_pid}'
)
PY
}
cleanup(){ for p in "$rec_pid" "$broker_pid" "$external_pid" "$proxy_pid";do if ((p>0));then kill "$p" 2>/dev/null||true;fi;done;for p in "$rec_pid" "$broker_pid" "$external_pid" "$proxy_pid";do if ((p>0));then wait "$p" 2>/dev/null||true;fi;done; }
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
seed_market_proxy_cache
reap_orphan_v6_loops
start_proxy
proxy_ready=0
for _ in {1..50};do
  if curl -fsS "http://127.0.0.1:${MARKET_PROXY_PORT}/healthz" >/dev/null 2>&1;then proxy_ready=1;break;fi
  kill -0 "$proxy_pid" 2>/dev/null||break
  sleep 0.1
done
((proxy_ready==1))||{ echo "fatal: V6 market proxy failed to start" >&2; exit 1; }
warm_market_proxy || { echo "fatal: V6 market proxy has no usable public market data" >&2; exit 1; }
refresh_external_feed
start_recorder;start_broker;start_external;write_supervisor
report_startup_ready || { echo "fatal: V6 startup data-ready contract failed" >&2; exit 1; }

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
  if ! kill -0 "$proxy_pid" 2>/dev/null;then wait "$proxy_pid" 2>/dev/null||true;proxy_restarts=$((proxy_restarts+1));start_proxy;sleep 1;fi
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
