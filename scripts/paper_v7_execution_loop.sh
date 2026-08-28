#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CONFIG="${PM_V7_CONFIG:-config/paper_v7.json}"
RUN_ROOT="${PM_V7_RUN_ROOT:-runs/paper_v7_live}"
RECORDER="${PM_TRADE_RECORDER:-build/polymarket_v7_trade_recorder}"
MAKER_RUNTIME="${PM_V7_MARKET_MAKER_RUNTIME:-build/polymarket_v7_market_maker_runtime}"
MARKOUT_OBSERVER="${PM_V7_MAKER_MARKOUT_OBSERVER:-build/polymarket_v7_maker_markout_observer}"
MAKER_POLICY="${PM_V7_MAKER_POLICY:-config/v7_professional_market_maker.json}"
SHA="$(git rev-parse HEAD)"
MAKER_CHAMPION_MODEL="$RUN_ROOT/micro_maker/execution_model.json"
MAKER_CHALLENGER_MODEL="$RUN_ROOT/micro_maker/execution_model_challenger.json"
MAKER_MODEL_REGISTRY="$RUN_ROOT/micro_maker/model_registry.json"
PUBLIC_PROXY_PORT="${PM_V7_PUBLIC_PROXY_PORT:-19109}"
PUBLIC_PROXY="http://127.0.0.1:$PUBLIC_PROXY_PORT"
WS_PUBLIC_HOST="ws-subscriptions-clob.polymarket.com"
# Adaptive JSON arenas are bounded per decoder. With the current canonical
# 40-market universe the Maker owns at most five 8-market shards, while the
# evidence-only markout observer owns one all-market decoder. The defaults
# therefore expose exactly 3.5 GiB of aggregate ceiling (5*512 MiB + 1 GiB)
# without eagerly allocating it. Each decoder starts small and grows only when
# an unusually large venue frame actually requires the memory.
WS_JSON_ARENA_MAKER_MAX_BYTES="${PM_V7_WS_JSON_ARENA_MAKER_MAX_BYTES:-536870912}"
WS_JSON_ARENA_OBSERVER_MAX_BYTES="${PM_V7_WS_JSON_ARENA_OBSERVER_MAX_BYTES:-1073741824}"
WS_JSON_ARENA_TOTAL_BUDGET_BYTES="${PM_V7_WS_JSON_ARENA_TOTAL_BUDGET_BYTES:-3758096384}"
# Bind the C++ slow-path execution-cell loader explicitly to the same exact SHA
# and *champion* model file passed to the canonical Maker runtime. Challenger
# refits are registered separately and can never be hot-reloaded by this loop.
export PM_V7_MODEL_SHA="$SHA"
export PM_V7_MAKER_EXECUTION_MODEL="$MAKER_CHAMPION_MODEL"
CONTROL="$RUN_ROOT/control"
ALLOC="$CONTROL/allocations"
KILL="$CONTROL/KILL"
LOCK="$CONTROL/runtime.lock"
mkdir -p "$CONTROL" "$RUN_ROOT/ledger" "$RUN_ROOT/market_data" "$RUN_ROOT/graph_rv" "$RUN_ROOT/hard_arb" "$RUN_ROOT/micro_taker" "$RUN_ROOT/micro_maker" "$RUN_ROOT/external" "$RUN_ROOT/learned_execution"
touch "$RUN_ROOT/ledger/execution.jsonl"

python3 - "$CONFIG" "$MAKER_POLICY" "$WS_JSON_ARENA_MAKER_MAX_BYTES" "$WS_JSON_ARENA_OBSERVER_MAX_BYTES" "$WS_JSON_ARENA_TOTAL_BUDGET_BYTES" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
v7=cfg.get("v7") or {}
maker=json.load(open(sys.argv[2]))
maker_arena=int(sys.argv[3])
observer_arena=int(sys.argv[4])
total_budget=int(sys.argv[5])
assert cfg.get("engine_version")==7
assert cfg.get("paper_only") is True
assert v7.get("paper_only") is True
assert v7.get("authenticated_execution") is False
assert v7.get("real_order_submission") is False
assert float(cfg.get("max_drawdown",0)) <= .15 + 1e-12
assert maker.get("paper_only") is True
assert maker.get("authenticated_execution") is False
assert maker.get("real_order_submission") is False
assert maker.get("architecture",{}).get("single_runtime_owner") is True
assert maker.get("architecture",{}).get("single_account_allocator") is True
assert maker.get("architecture",{}).get("single_canonical_ledger_writer") is True
assert maker.get("architecture",{}).get("fast_path") == "cpp_websocket_event_driven"
assert maker.get("architecture",{}).get("slow_path") == "python_reward_selection_and_model_fit"
assert maker_arena >= 16*1024*1024
assert observer_arena >= 16*1024*1024
assert maker_arena*5 + observer_arena <= total_budget
PY

if [[ -d "$LOCK" ]]; then
  old="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    echo "V7 runtime already active pid=$old" >&2
    exit 73
  fi
  rm -rf "$LOCK"
fi
mkdir "$LOCK"
echo $$ > "$LOCK/pid"
rm -f "$KILL"

python3 scripts/v7_capital_allocator.py --config "$CONFIG" --output-dir "$ALLOC" >/dev/null

write_runtime_status() {
  local state="$1"
  local killed="${2:-false}"
  local now
  now="$(date +%s)"
  local tmp="$CONTROL/runtime_status.json.tmp.$$"
  printf '{"schema":"polymarket_v7_runtime_status_v2","timestamp":%s,"version":7,"paper_only":true,"authenticated_execution":false,"real_order_submission":false,"model_sha":"%s","pid":%s,"state":"%s","killed":%s,"primary_economic_sleeve":"MICRO_MAKER_PRO"}\n' \
    "$now" "$SHA" "$$" "$state" "$killed" > "$tmp"
  mv "$tmp" "$CONTROL/runtime_status.json"
}
write_runtime_status starting false

pids=()
cleanup_started=0
cleanup() {
  if [[ "$cleanup_started" == 1 ]]; then
    return 0
  fi
  cleanup_started=1
  set +e
  touch "$KILL"
  for pid in "${pids[@]:-}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 50); do
    alive=0
    for pid in "${pids[@]:-}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive=1
        break
      fi
    done
    [[ "$alive" == 0 ]] && break
    sleep 0.1
  done
  for pid in "${pids[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${pids[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$LOCK"
  return 0
}
shutdown() {
  write_runtime_status stopping false || true
  cleanup
  exit 0
}
trap cleanup EXIT
trap shutdown INT TERM

if [[ ! -x "$RECORDER" ]]; then
  echo "missing canonical V7 trade recorder executable: $RECORDER" >&2
  exit 74
fi
if [[ ! -x "$MAKER_RUNTIME" ]]; then
  echo "missing canonical V7 market-maker runtime executable: $MAKER_RUNTIME" >&2
  exit 75
fi
if [[ ! -x "$MARKOUT_OBSERVER" ]]; then
  echo "missing V7 maker markout observer executable: $MARKOUT_OBSERVER" >&2
  exit 76
fi
if [[ ! -f scripts/v7_public_https_proxy.py ]]; then
  echo "missing V7 public HTTPS proxy" >&2
  exit 77
fi

# The PAPER server may sit behind an ISP resolver that filters Polymarket public
# domains. Keep the operating-system DNS untouched: a loopback CONNECT tunnel
# resolves through public DNS and relays end-to-end TLS without seeing payloads
# or credentials. The latency-sensitive WebSocket is not proxied; it receives
# only publicly resolved IPs below while retaining the original TLS hostname.
python3 scripts/v7_public_https_proxy.py --host 127.0.0.1 --port "$PUBLIC_PROXY_PORT" \
  >> "$RUN_ROOT/public_https_proxy.log" 2>&1 &
pids+=("$!")
proxy_ready=0
for _ in $(seq 1 50); do
  if python3 - "$PUBLIC_PROXY_PORT" <<'PY' >/dev/null 2>&1
import socket,sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=.2):
    pass
PY
  then
    proxy_ready=1
    break
  fi
  sleep 0.1
done
if [[ "$proxy_ready" != 1 ]]; then
  echo "V7 public HTTPS proxy did not become ready" >&2
  exit 77
fi

export PM_V7_HTTPS_PROXY="$PUBLIC_PROXY"
export HTTPS_PROXY="$PUBLIC_PROXY"
export https_proxy="$PUBLIC_PROXY"
export HTTP_PROXY="$PUBLIC_PROXY"
export http_proxy="$PUBLIC_PROXY"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="$NO_PROXY"
if [[ -z "${PM_V7_WS_RESOLVE_IPS:-}" ]]; then
  PM_V7_WS_RESOLVE_IPS="$(python3 scripts/v7_public_https_proxy.py --resolve "$WS_PUBLIC_HOST")"
fi
[[ -n "$PM_V7_WS_RESOLVE_IPS" ]] || { echo "public WS DNS resolution returned no addresses" >&2; exit 77; }
export PM_V7_WS_RESOLVE_IPS

maker_selection_ready() {
  python3 - "$RUN_ROOT/micro_maker/reward_selection.json" <<'PY' >/dev/null 2>&1
import json,sys
from pathlib import Path
path=Path(sys.argv[1])
if not path.is_file(): raise SystemExit(1)
try: obj=json.loads(path.read_text(encoding="utf-8"))
except Exception: raise SystemExit(1)
markets=obj.get("markets")
ok=(obj.get("paper_only") is True and obj.get("authenticated_execution") is False
    and isinstance(markets,list) and len(markets)>0)
raise SystemExit(0 if ok else 1)
PY
}

"$RECORDER" \
  --config "$CONFIG" \
  --run-dir "$RUN_ROOT" \
  --data-url "https://data-api.polymarket.com" \
  --markets 1000 --batch 40 --min-liquidity 2 \
  --lookback-seconds 180 --interval 5 --loop \
  >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
pids+=("$!")

# One persistent canonical ledger router. 100ms transport cadence keeps FILL
# evidence available before the 1s markout horizon without creating a second
# ledger writer or repeatedly spawning Python processes.
python3 scripts/v7_ledger_spool.py \
  --run-root "$RUN_ROOT" --model-sha "$SHA" --loop --interval 0.1 \
  >> "$RUN_ROOT/ledger_router.log" 2>&1 &
pids+=("$!")

# Slow-plane reward selection only. It may perform REST discovery, but it never
# decides/cancels quotes and is not a second maker runtime.
(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_market_maker_rewards.py \
      --config "$MAKER_POLICY" \
      --output "$RUN_ROOT/micro_maker/reward_selection.json" \
      >> "$RUN_ROOT/micro_maker/reward_selection.log" 2>&1 || true
    sleep 60
  done
) & pids+=("$!")

# Slow-plane exact-SHA fill/markout fit. A refit is a CHALLENGER only. It is
# written to a separate artifact and registered for OOS/shadow-PAPER review.
# This loop NEVER overwrites the runtime champion and NEVER auto-promotes.
(
  while [[ ! -e "$KILL" ]]; do
    if [[ -s "$RUN_ROOT/ledger/execution.jsonl" ]]; then
      if python3 scripts/v7_market_maker_model.py \
        --ledger "$RUN_ROOT/ledger/execution.jsonl" \
        --model-sha "$SHA" \
        --artifact-role challenger \
        --output "$MAKER_CHALLENGER_MODEL" \
        >> "$RUN_ROOT/micro_maker/model.log" 2>&1; then
        python3 scripts/v7_maker_model_registry.py \
          --registry "$MAKER_MODEL_REGISTRY" \
          --model-sha "$SHA" \
          --challenger "$MAKER_CHALLENGER_MODEL" \
          --champion "$MAKER_CHAMPION_MODEL" \
          >> "$RUN_ROOT/micro_maker/model_registry.log" 2>&1 || true
      fi
    fi
    sleep 60
  done
) & pids+=("$!")

# Canonical Maker owner: public WS -> bounded V7 L2 -> C++ features/decision ->
# common V7 OMS/queue PAPER engine -> spool. Startup waits for the first valid
# slow-plane selection instead of turning a transient public-data dependency
# into a whole-runtime kill. Once started, the maker remains fail-closed.
(
  while [[ ! -e "$KILL" ]] && ! maker_selection_ready; do sleep 1; done
  [[ ! -e "$KILL" ]] || exit 0
  export PM_V7_WS_JSON_ARENA_MAX_BYTES="$WS_JSON_ARENA_MAKER_MAX_BYTES"
  exec "$MAKER_RUNTIME" \
    --config "$ALLOC/micro_maker.json" \
    --maker-policy "$MAKER_POLICY" \
    --run-root "$RUN_ROOT" \
    --selection "$RUN_ROOT/micro_maker/reward_selection.json" \
    --model "$MAKER_CHAMPION_MODEL" \
    --model-sha "$SHA"
) >> "$RUN_ROOT/micro_maker/runtime.log" 2>&1 &
pids+=("$!")

# Evidence-only observer. It has no order/OMS/risk authority: it tails canonical
# FILL events, observes the same bounded public WS/L10 state, and emits only
# full-size executable MARKOUT records at 1/10/45/60/300s into the common spool.
(
  while [[ ! -e "$KILL" ]] && ! maker_selection_ready; do sleep 1; done
  [[ ! -e "$KILL" ]] || exit 0
  export PM_V7_WS_JSON_ARENA_MAX_BYTES="$WS_JSON_ARENA_OBSERVER_MAX_BYTES"
  exec "$MARKOUT_OBSERVER" \
    --config "$ALLOC/micro_maker.json" \
    --run-root "$RUN_ROOT" \
    --selection "$RUN_ROOT/micro_maker/reward_selection.json" \
    --model-sha "$SHA"
) >> "$RUN_ROOT/micro_maker/markout_observer.log" 2>&1 &
pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    if ! maker_selection_ready; then
      sleep 1
      continue
    fi
    if ! python3 scripts/v7_market_maker_status.py \
      --state "$RUN_ROOT/micro_maker/state.json" \
      --config "$ALLOC/micro_maker.json" \
      --selection "$RUN_ROOT/micro_maker/reward_selection.json" \
      --output "$RUN_ROOT/micro_maker/status.json" \
      >> "$RUN_ROOT/micro_maker/status.log" 2>&1; then
      printf '{"schema":"polymarket_v7_maker_risk_failure_v1","timestamp":%s,"paper_only":true,"authenticated_execution":false,"model_sha":"%s"}\n' "$(date +%s)" "$SHA" > "$KILL"
      break
    fi
    sleep 1
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_graph_rv_executable_intents.py \
      --config "$ALLOC/graph_rv.json" \
      --output "$RUN_ROOT/graph_rv/intents.csv" \
      --status "$RUN_ROOT/graph_rv/scan_status.json" \
      >> "$RUN_ROOT/graph_rv/scan.log" 2>&1 || true
    sleep 15
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    joint_policy="$RUN_ROOT/learned_execution/joint_policy.json"
    if [[ -s "$joint_policy" ]]; then
      python3 scripts/v7_graph_rv.py \
        --config "$ALLOC/graph_rv.json" \
        --run-root "$RUN_ROOT" \
        --intents "$RUN_ROOT/graph_rv/intents.csv" \
        --trade-tape "$RUN_ROOT/trade_tape.csv" \
        --joint-model "$joint_policy" \
        >> "$RUN_ROOT/graph_rv/execution.log" 2>&1 || true
    else
      python3 scripts/v7_graph_rv.py \
        --config "$ALLOC/graph_rv.json" \
        --run-root "$RUN_ROOT" \
        --intents "$RUN_ROOT/graph_rv/intents.csv" \
        --trade-tape "$RUN_ROOT/trade_tape.csv" \
        >> "$RUN_ROOT/graph_rv/execution.log" 2>&1 || true
    fi
    sleep 1
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_graph_cost_vector.py --run-root "$RUN_ROOT" --model-sha "$SHA" --slippage-bps 5 \
      >> "$RUN_ROOT/graph_rv/cost_vector.log" 2>&1 || true
    python3 scripts/v7_joint_execution_policy.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
      --output "$RUN_ROOT/learned_execution/joint_policy.json" --strategy GRAPH_RV --min-bundles 20 \
      >> "$RUN_ROOT/learned_execution/joint_policy.log" 2>&1 || true
    python3 scripts/v7_learned_execution_model.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
      --output "$RUN_ROOT/learned_execution/oos_report.json" \
      >> "$RUN_ROOT/learned_execution/model.log" 2>&1 || true
    python3 scripts/v7_canonical_economics.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --expected-model-sha "$SHA" \
      --output "$RUN_ROOT/canonical_economics.json" >> "$RUN_ROOT/canonical_economics.log" 2>&1 || true
    sleep 60
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_hard_arb_guard.py \
      --config "$ALLOC/hard_arb.json" --run-dir "$RUN_ROOT/hard_arb" \
      --markets 1000 --min-liquidity 2 --max-events 80 --min-edge 0.00005 \
      --max-trade-usd 1e100 --slippage-bps 5 --leg-latency-ms 100 \
      >> "$RUN_ROOT/hard_arb/runtime.log" 2>&1 || true
    sleep 5
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_micro_taker_worker.py \
      --config "$ALLOC/micro_taker.json" --run-dir "$RUN_ROOT/micro_taker" \
      --trade-tape "$RUN_ROOT/trade_tape.csv" --markets 1000 --min-liquidity 2 \
      --horizon-seconds 30 --max-trade-usd 1e100 --min-edge 0.00005 --slippage-bps 5 \
      >> "$RUN_ROOT/micro_taker/runtime.log" 2>&1 || true
    sleep 5
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_external_bridge.py \
      --output "$RUN_ROOT/external/external_signals.csv" \
      --status "$RUN_ROOT/external/status.json" --max-age-seconds 7200 \
      >> "$RUN_ROOT/external/bridge.log" 2>&1 || true
    sleep 60
  done
) & pids+=("$!")

write_runtime_status running false

while [[ ! -e "$KILL" ]]; do
  if ! python3 scripts/v7_portfolio_guard.py --run-root "$RUN_ROOT" \
      --allocation-manifest "$ALLOC/manifest.json" --max-drawdown 0.15 \
      > "$RUN_ROOT/portfolio_guard.log" 2>&1; then
    break
  fi
  write_runtime_status running false
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '{"schema":"polymarket_v7_runtime_failure_v1","timestamp":%s,"paper_only":true,"authenticated_execution":false,"model_sha":"%s","dead_pid":%s}\n' "$(date +%s)" "$SHA" "$pid" > "$KILL"
      break
    fi
  done
  sleep 1
done

write_runtime_status killed true
exit 2