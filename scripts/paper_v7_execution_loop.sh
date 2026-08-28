#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CONFIG="${PM_V7_CONFIG:-config/paper_v7.json}"
RUN_ROOT="${PM_V7_RUN_ROOT:-runs/paper_v7_live}"
RECORDER="${PM_TRADE_RECORDER:-build/polymarket_v7_trade_recorder}"
MAKER_RUNTIME="${PM_V7_MARKET_MAKER_RUNTIME:-build/polymarket_v7_market_maker_runtime}"
MARKOUT_OBSERVER="${PM_V7_MAKER_MARKOUT_OBSERVER:-build/polymarket_v7_maker_markout_observer}"
FILLABILITY_OBSERVER="${PM_V7_MAKER_FILLABILITY_OBSERVER:-build/polymarket_v7_maker_fillability_observer}"
FAST_STRUCTURAL_RUNTIME="${PM_V7_FAST_STRUCTURAL_RUNTIME:-build/polymarket_v7_fast_structural_runtime}"
MAKER_POLICY="${PM_V7_MAKER_POLICY:-config/v7_professional_market_maker.json}"
FAST_STRUCTURAL_POLICY="${PM_V7_FAST_STRUCTURAL_POLICY:-config/v7_fast_structural.json}"
FAST_STRUCTURAL_RELATIONS="${PM_V7_FAST_STRUCTURAL_RELATIONS:-config/v7_fast_structural_relations.csv}"
EXTERNAL_FAIR_POLICY="${PM_V7_EXTERNAL_FAIR_POLICY:-config/v7_external_fair.json}"
ORACLE_BINDING="${PM_V7_ORACLE_BINDING:-}"
EXACT_SHA_CI_GREEN="${PM_V7_EXACT_SHA_CI_GREEN:-false}"
OSINT_SOURCE_REGISTRY="${PM_V7_OSINT_SOURCE_REGISTRY:-config/v7_osint_sources.json}"
SHA="$(git rev-parse HEAD)"
[[ "$EXACT_SHA_CI_GREEN" == "true" || "$EXACT_SHA_CI_GREEN" == "false" ]] || {
  echo "PM_V7_EXACT_SHA_CI_GREEN must be true or false" >&2
  exit 72
}
MAKER_CHAMPION_MODEL="$RUN_ROOT/micro_maker/execution_model.json"
MAKER_CHALLENGER_MODEL="$RUN_ROOT/micro_maker/execution_model_challenger.json"
MAKER_MODEL_REGISTRY="$RUN_ROOT/micro_maker/model_registry.json"
PUBLIC_PROXY_PORT="${PM_V7_PUBLIC_PROXY_PORT:-19109}"
PUBLIC_PROXY="http://127.0.0.1:$PUBLIC_PROXY_PORT"
WS_PUBLIC_HOST="ws-subscriptions-clob.polymarket.com"
# Adaptive JSON arenas are bounded per decoder. With the current canonical
# 40-market universe the Maker owns at most five 8-market shards, while the
# evidence-only markout and fillability observers each own one all-market
# decoder. The defaults expose a 4 GiB aggregate ceiling without eagerly
# allocating it; each decoder starts small and grows only for large venue frames.
WS_JSON_ARENA_MAKER_MAX_BYTES="${PM_V7_WS_JSON_ARENA_MAKER_MAX_BYTES:-536870912}"
WS_JSON_ARENA_OBSERVER_MAX_BYTES="${PM_V7_WS_JSON_ARENA_OBSERVER_MAX_BYTES:-1073741824}"
WS_JSON_ARENA_FILLABILITY_MAX_BYTES="${PM_V7_WS_JSON_ARENA_FILLABILITY_MAX_BYTES:-536870912}"
WS_JSON_ARENA_TOTAL_BUDGET_BYTES="${PM_V7_WS_JSON_ARENA_TOTAL_BUDGET_BYTES:-4294967296}"
# Bind the C++ slow-path execution-cell loader explicitly to the same exact SHA
# and *champion* model file passed to the canonical Maker runtime. Challenger
# refits are registered separately and can never be hot-reloaded by this loop.
export PM_V7_MODEL_SHA="$SHA"
export PM_V7_MAKER_EXECUTION_MODEL="$MAKER_CHAMPION_MODEL"
CONTROL="$RUN_ROOT/control"
ALLOC="$CONTROL/allocations"
KILL="$CONTROL/KILL"
LOCK="$CONTROL/runtime.lock"
mkdir -p "$CONTROL" "$RUN_ROOT/ledger" "$RUN_ROOT/market_data" "$RUN_ROOT/fast_structural" "$RUN_ROOT/graph_rv" "$RUN_ROOT/hard_arb" "$RUN_ROOT/micro_taker" "$RUN_ROOT/micro_maker" "$RUN_ROOT/external" "$RUN_ROOT/external_fair" "$RUN_ROOT/osint" "$RUN_ROOT/market_open" "$RUN_ROOT/learned_execution"
touch "$RUN_ROOT/ledger/execution.jsonl"

python3 - "$CONFIG" "$MAKER_POLICY" "$EXTERNAL_FAIR_POLICY" "$OSINT_SOURCE_REGISTRY" "$WS_JSON_ARENA_MAKER_MAX_BYTES" "$WS_JSON_ARENA_OBSERVER_MAX_BYTES" "$WS_JSON_ARENA_FILLABILITY_MAX_BYTES" "$WS_JSON_ARENA_TOTAL_BUDGET_BYTES" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
v7=cfg.get("v7") or {}
maker=json.load(open(sys.argv[2]))
external=json.load(open(sys.argv[3]))
osint_sources=json.load(open(sys.argv[4]))
registry_path=v7.get("strategy_registry")
assert isinstance(registry_path,str) and registry_path
registry=json.load(open(registry_path))
families={
    "professional_maker","fast_structural","hard_arb","graph_rv",
    "crypto_settlement_fair","crypto_informed_taker","micro_taker","osint",
    "sports_latency","cross_platform","wallet_intelligence","market_open",
    "ranking","pca","local_factor",
}
registered=[row.get("family") for row in registry.get("strategies",[])]
assert registry.get("schema")=="polymarket_v7_strategy_registry_v1"
assert set(registered)==families and len(registered)==len(families)
assert registry.get("safety",{}).get("paper_only") is True
assert registry.get("safety",{}).get("authenticated_execution") is False
assert registry.get("safety",{}).get("real_order_submission") is False
assert registry.get("governance",{}).get("automatic_promotion") is False
assert all(row.get("authority") in {"RESEARCH","SHADOW","PAPER"} for row in registry.get("strategies",[]))
assert osint_sources.get("schema") == "polymarket_v7_osint_source_registry_v1"
assert osint_sources.get("paper_only") is True
assert osint_sources.get("authenticated_execution") is False
assert osint_sources.get("real_order_submission") is False
assert osint_sources.get("sources")
maker_arena=int(sys.argv[5])
observer_arena=int(sys.argv[6])
fillability_arena=int(sys.argv[7])
total_budget=int(sys.argv[8])
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
assert external.get("execution_authority") == "PAPER_EXECUTION_OWNER"
assert external.get("paper_only") is True
assert external.get("authenticated_execution") is False
assert external.get("real_order_submission") is False
assert external.get("taker",{}).get("enabled_for_execution") is True
assert external.get("maker",{}).get("external_fair_enabled_for_live_quotes") is True
assert external.get("gate_classes",{}).get("A_HARD_CORRECTNESS_SAFETY",{}).get("may_block_paper") is True
assert external.get("gate_classes",{}).get("B_ECONOMIC_MATURITY",{}).get("may_block_paper") is False
assert maker_arena >= 16*1024*1024
assert observer_arena >= 16*1024*1024
assert fillability_arena >= 16*1024*1024
assert maker_arena*5 + observer_arena + fillability_arena <= total_budget
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
pids=()

# Official settlement-source data is optional at process level and mandatory at
# contract level. Missing credentials/binding isolate only settlement-aware
# contracts; unrelated maker/arb sleeves continue. When configured, the adapter
# authenticates only to Chainlink market data and can never submit an order.
if [[ -n "$ORACLE_BINDING" && -f "$ORACLE_BINDING" ]]; then
  python3 scripts/v7_same_oracle_adapter.py \
    --binding "$ORACLE_BINDING" \
    --tape "$RUN_ROOT/external_fair/oracle_events.jsonl" \
    --status "$RUN_ROOT/external_fair/oracle_status.json" \
    >> "$RUN_ROOT/external_fair/oracle_adapter.log" 2>&1 &
  pids+=("$!")
else
  python3 - "$RUN_ROOT/external_fair/oracle_status.json" <<'PY'
import json,os,sys,time
from pathlib import Path
path=Path(sys.argv[1]); tmp=path.with_name(path.name+f'.tmp.{os.getpid()}')
tmp.write_text(json.dumps({
    'schema':'polymarket_v7_same_oracle_status_v1','state':'UNKNOWN',
    'reason':'binding_not_configured_contracts_quarantined','timestamp_ns':time.time_ns(),
    'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
},sort_keys=True)+'\n',encoding='utf-8'); os.replace(tmp,path)
PY
fi

CONFIG_HASH="$(git hash-object "$CONFIG")"
POLICY_HASH="$(git hash-object "$MAKER_POLICY")"
RUN_ID="${PM_V7_RUN_ID:-${SHA:0:12}-$(date +%s)-$$}"
LEDGER_ID="${PM_V7_LEDGER_ID:-$RUN_ID:execution}"
SERVER_ID="${PM_V7_SERVER_ID:-$(hostname -s 2>/dev/null || hostname)}"

write_runtime_status() {
  local state="$1"
  local killed="${2:-false}"
  local now
  now="$(date +%s)"
  local model_hash model_source
  if [[ -s "$MAKER_CHAMPION_MODEL" ]]; then
    model_hash="$(git hash-object "$MAKER_CHAMPION_MODEL")"
    model_source="maker_execution_model"
  else
    model_hash="$POLICY_HASH"
    model_source="cold_start_policy"
  fi
  local tmp="$CONTROL/runtime_status.json.tmp.$$"
  printf '{"schema":"polymarket_v7_runtime_status_v2","timestamp":%s,"version":7,"paper_only":true,"authenticated_execution":false,"real_order_submission":false,"model_sha":"%s","config_hash":"%s","policy_hash":"%s","model_hash":"%s","model_identity_source":"%s","run_id":"%s","ledger_id":"%s","server_id":"%s","pid":%s,"state":"%s","killed":%s,"primary_economic_sleeve":"MICRO_MAKER_PRO","execution_authority":"PAPER_EXECUTION_OWNER","single_execution_owner":true,"canonical_state_reconciled":true,"exact_sha_ci_green":%s,"p0_authority_configured":["professional_maker","crypto_settlement_fair","crypto_informed_taker"],"p0_full_stack_ready":false,"readiness":"CORE_RUNTIME_ONLY","external_fair_runtime_ready":false}\n' \
    "$now" "$SHA" "$CONFIG_HASH" "$POLICY_HASH" "$model_hash" "$model_source" "$RUN_ID" "$LEDGER_ID" "$SERVER_ID" "$$" "$state" "$killed" "$EXACT_SHA_CI_GREEN" > "$tmp"
  mv "$tmp" "$CONTROL/runtime_status.json"
}
write_runtime_status starting false
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
if [[ ! -x "$FILLABILITY_OBSERVER" ]]; then
  echo "missing V7 maker exact-WS fillability observer executable: $FILLABILITY_OBSERVER" >&2
  exit 78
fi
if [[ ! -x "$FAST_STRUCTURAL_RUNTIME" ]]; then
  echo "missing V7 Fast Structural PAPER runtime executable: $FAST_STRUCTURAL_RUNTIME" >&2
  exit 79
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

# V7 Fast Structural is a PAPER-only detector/execution-planning sleeve. It
# owns neither authenticated transport nor a second account; its child config
# is pre-partitioned by the canonical allocator and all outputs remain inside
# the canonical run root.
"$FAST_STRUCTURAL_RUNTIME" \
  --config "$ALLOC/fast_structural.json" \
  --policy "$FAST_STRUCTURAL_POLICY" \
  --relations "$FAST_STRUCTURAL_RELATIONS" \
  --external-signals "$RUN_ROOT/external/external_signals.csv" \
  --run-dir "$RUN_ROOT/fast_structural" --run-root "$RUN_ROOT" --model-sha "$SHA" \
  --markets 1000 --min-liquidity 2 --shard-size 200 \
  >> "$RUN_ROOT/fast_structural/runtime.log" 2>&1 &
pids+=("$!")

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

# Evidence-only markout observer. It has no order/OMS/risk authority: it tails
# canonical FILL events, observes bounded public WS/L10 state, and emits only
# executable MARKOUT records into the common spool.
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

# Evidence-only exact-WS fillability observer. It has no OMS, capital, risk or
# order authority. The WS callback only pushes compact trade observations into
# a bounded SPSC queue; disk serialization happens on the observer consumer
# thread. Drops/decoder gaps are explicit in fillability_ws_status.json.
(
  while [[ ! -e "$KILL" ]] && ! maker_selection_ready; do sleep 1; done
  [[ ! -e "$KILL" ]] || exit 0
  export PM_V7_WS_JSON_ARENA_MAX_BYTES="$WS_JSON_ARENA_FILLABILITY_MAX_BYTES"
  exec "$FILLABILITY_OBSERVER" \
    --config "$ALLOC/micro_maker.json" \
    --run-root "$RUN_ROOT" \
    --selection "$RUN_ROOT/micro_maker/reward_selection.json" \
    --model-sha "$SHA"
) >> "$RUN_ROOT/micro_maker/fillability_observer.log" 2>&1 &
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

# Research-only official-source OSINT tape. It has no model-to-intent bridge,
# OMS, capital, risk, order, or promotion authority. Source failures are
# recorded per source and do not mutate or stop the economic PAPER runtime.
python3 scripts/v7_osint_collector.py \
  --registry "$OSINT_SOURCE_REGISTRY" \
  --tape "$RUN_ROOT/osint/raw_events.jsonl" \
  --state "$RUN_ROOT/osint/collector_state.json" \
  --status "$RUN_ROOT/osint/status.json" \
  --interval 60 --loop \
  >> "$RUN_ROOT/osint/collector.log" 2>&1 &
pids+=("$!")

# Research-only market-creation/milestone collector. Its first snapshot is a
# baseline and can never masquerade as a burst of new listings. Exact semantic
# verification and a separate race/edge-decay gate are required before any
# downstream authority can change.
python3 scripts/v7_market_open_collector.py \
  --tape "$RUN_ROOT/market_open/events.jsonl" \
  --state "$RUN_ROOT/market_open/collector_state.json" \
  --status "$RUN_ROOT/market_open/status.json" \
  --interval 5 --loop \
  >> "$RUN_ROOT/market_open/collector.log" 2>&1 &
pids+=("$!")

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
