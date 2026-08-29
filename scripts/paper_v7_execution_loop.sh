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
EXTERNAL_VENUE_RUNTIME="${PM_V7_EXTERNAL_VENUE_RUNTIME:-build/polymarket_v7_external_venue_runtime}"
FAST_STRUCTURAL_RUNTIME="${PM_V7_FAST_STRUCTURAL_RUNTIME:-build/polymarket_v7_fast_structural_runtime}"
MAKER_POLICY="${PM_V7_MAKER_POLICY:-config/v7_professional_market_maker.json}"
FAST_STRUCTURAL_POLICY="${PM_V7_FAST_STRUCTURAL_POLICY:-config/v7_fast_structural.json}"
FAST_STRUCTURAL_RELATIONS="${PM_V7_FAST_STRUCTURAL_RELATIONS:-config/v7_fast_structural_relations.csv}"
EXTERNAL_FAIR_POLICY="${PM_V7_EXTERNAL_FAIR_POLICY:-config/v7_external_fair.json}"
ORACLE_BINDING="${PM_V7_ORACLE_BINDING:-}"
EXACT_SHA_CI_GREEN="${PM_V7_EXACT_SHA_CI_GREEN:-false}"
OSINT_SOURCE_REGISTRY="${PM_V7_OSINT_SOURCE_REGISTRY:-config/v7_osint_sources.json}"
LIVE_MODEL_SCOPE="${PM_V7_LIVE_MODEL_SCOPE:-config/v7_live_model_scope.json}"
EXTERNAL_INPUT_CONFIG="${PM_V7_EXTERNAL_INPUT_CONFIG:-config/v7_external_inputs.json}"
EXTERNAL_MAPPING_REGISTRY="${PM_V7_EXTERNAL_MAPPING_REGISTRY:-config/v7_external_mappings.json}"
ADAPTIVE_UNIVERSE_CONFIG="${PM_V7_ADAPTIVE_UNIVERSE_CONFIG:-config/v7_adaptive_universe.json}"
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
# resource-derived Maker universe owns the declared number of 8-market shards, while the
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
mkdir -p "$CONTROL" "$RUN_ROOT/ledger" "$RUN_ROOT/market_data" "$RUN_ROOT/universe" "$RUN_ROOT/fast_structural" "$RUN_ROOT/graph_rv" "$RUN_ROOT/hard_arb" "$RUN_ROOT/micro_taker" "$RUN_ROOT/micro_maker" "$RUN_ROOT/external" "$RUN_ROOT/external_fair" "$RUN_ROOT/osint" "$RUN_ROOT/market_open" "$RUN_ROOT/shadow/sports_latency" "$RUN_ROOT/shadow/cross_platform" "$RUN_ROOT/shadow/wallet_intelligence" "$RUN_ROOT/learned_execution"
touch "$RUN_ROOT/ledger/execution.jsonl"

python3 - "$CONFIG" "$MAKER_POLICY" "$EXTERNAL_FAIR_POLICY" "$OSINT_SOURCE_REGISTRY" "$LIVE_MODEL_SCOPE" "$EXTERNAL_INPUT_CONFIG" "$EXTERNAL_MAPPING_REGISTRY" "$ADAPTIVE_UNIVERSE_CONFIG" "$WS_JSON_ARENA_MAKER_MAX_BYTES" "$WS_JSON_ARENA_OBSERVER_MAX_BYTES" "$WS_JSON_ARENA_FILLABILITY_MAX_BYTES" "$WS_JSON_ARENA_TOTAL_BUDGET_BYTES" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
v7=cfg.get("v7") or {}
maker=json.load(open(sys.argv[2]))
external=json.load(open(sys.argv[3]))
osint_sources=json.load(open(sys.argv[4]))
live_scope=json.load(open(sys.argv[5]))
external_inputs=json.load(open(sys.argv[6]))
external_mappings=json.load(open(sys.argv[7]))
adaptive_universe=json.load(open(sys.argv[8]))
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
target=set(live_scope.get("target_live_families") or [])
excluded=set(live_scope.get("excluded_live_families") or [])
shadow=set(live_scope.get("research_shadow_supervised_families") or [])
assert live_scope.get("schema") == "polymarket_v7_live_model_scope_v1"
assert live_scope.get("version") == 7 and live_scope.get("target_live_count") == 12
assert live_scope.get("paper_only") is True
assert live_scope.get("authenticated_execution") is False
assert live_scope.get("real_order_submission") is False
assert target | excluded == families and not target & excluded
assert excluded == {"ranking", "pca", "local_factor"}
assert shadow == {"sports_latency", "cross_platform", "wallet_intelligence"}
governance=live_scope.get("governance") or {}
assert governance.get("single_execution_owner") is True
assert governance.get("research_has_capital") is False
assert governance.get("research_has_oms_authority") is False
assert governance.get("research_has_ledger_writer_authority") is False
assert governance.get("automatic_promotion") is False
assert external_inputs.get("schema") == "polymarket_v7_external_inputs_v1"
assert external_inputs.get("version") == 7
assert external_inputs.get("paper_only") is True
assert external_inputs.get("authenticated_execution") is False
assert external_inputs.get("real_order_submission") is False
assert external_mappings.get("schema") == "polymarket_v7_external_mapping_registry_v1"
assert external_mappings.get("version") == 7
assert external_mappings.get("paper_only") is True
assert external_mappings.get("automatic_promotion") is False
assert all(isinstance(external_mappings.get(name), list) for name in ("osint","sports_latency","cross_platform"))
assert adaptive_universe.get("schema") == "polymarket_v7_adaptive_universe_config_v1"
assert adaptive_universe.get("version") == 7
assert adaptive_universe.get("paper_only") is True
assert adaptive_universe.get("authenticated_execution") is False
assert adaptive_universe.get("real_order_submission") is False
maker_arena=int(sys.argv[9])
observer_arena=int(sys.argv[10])
fillability_arena=int(sys.argv[11])
total_budget=int(sys.argv[12])
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
capacity=maker.get("market_selection",{}).get("resource_capacity",{})
max_shards=int(capacity.get("shard_count_budget",0))
markets_per_shard=int(capacity.get("markets_per_shard",0))
assert max_shards > 0 and markets_per_shard == 8
assert int(maker.get("market_selection",{}).get("max_active_markets",0)) == max_shards*markets_per_shard
expected_maker_capital=float(cfg.get("starting_capital",0))*float(v7.get("micro_maker_capital_fraction",0))
assert abs(float(maker.get("market_selection",{}).get("reward_sleeve_capital_usd",0))-expected_maker_capital) <= 1e-9
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
assert maker_arena*max_shards + observer_arena + fillability_arena <= total_budget
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
  # Public RTDS restores the real Chainlink/Binance data plane without
  # credentials. Contract, fair-value and OMS authorization remain fail-closed
  # and are reported explicitly instead of being represented as missing data.
  python3 scripts/v7_rtds_external_fair_monitor.py \
    --output-dir "$RUN_ROOT/external_fair" --code-sha "$SHA" \
    --universe "$RUN_ROOT/universe/current.json" \
    --approvals "config/v7_external_fair_rule_approvals.json" \
    --external-venues "$RUN_ROOT/external_fair/external_venues.json" \
    >> "$RUN_ROOT/external_fair/rtds_monitor.log" 2>&1 &
  pids+=("$!")
fi

"$EXTERNAL_VENUE_RUNTIME" \
  --output "$RUN_ROOT/external_fair/external_venues.json" --model-sha "$SHA" \
  >> "$RUN_ROOT/external_fair/external_venues.log" 2>&1 &
pids+=("$!")

python3 scripts/v7_external_fair_paper_router.py \
  --run-root "$RUN_ROOT" --model-sha "$SHA" --config "$EXTERNAL_FAIR_POLICY" --interval 1 \
  >> "$RUN_ROOT/external_fair/paper_router.log" 2>&1 &
pids+=("$!")

CONFIG_HASH="$(git hash-object "$CONFIG")"
POLICY_HASH="$(git hash-object "$MAKER_POLICY")"
RUN_ID="${PM_V7_RUN_ID:-${SHA:0:12}-$(date +%s)-$$}"
LEDGER_ID="${PM_V7_LEDGER_ID:-$RUN_ID:execution}"
SERVER_ID="${PM_V7_SERVER_ID:-$(hostname -s 2>/dev/null || hostname)}"

paper_router_ready() {
  python3 - "$RUN_ROOT/external_fair/paper_router_status.json" "$SHA" <<'PY'
import json,sys,time
try:
    value=json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
ok=(value.get("schema")=="polymarket_v7_external_fair_paper_router_v1"
    and value.get("code_sha")==sys.argv[2]
    and value.get("state")=="RUNNING"
    and value.get("paper_only") is True
    and value.get("authenticated_execution") is False
    and value.get("real_order_submission") is False
    and value.get("execution_authority")=="PAPER_EXECUTION_OWNER"
    and value.get("order_submission_enabled") is True
    and value.get("killed") is False
    and not value.get("blocker")
    and int(time.time())-int(value.get("timestamp") or 0)<=5)
raise SystemExit(0 if ok else 1)
PY
}

write_runtime_status() {
  local state="$1"
  local killed="${2:-false}"
  local now p0_ready=false readiness="CORE_RUNTIME_ONLY" external_ready=false
  now="$(date +%s)"
  if [[ "$state" == "running" ]] && paper_router_ready; then
    p0_ready=true
    readiness="FULL_PAPER_RUNTIME"
    external_ready=true
  fi
  local model_hash model_source
  if [[ -s "$MAKER_CHAMPION_MODEL" ]]; then
    model_hash="$(git hash-object "$MAKER_CHAMPION_MODEL")"
    model_source="maker_execution_model"
  else
    model_hash="$POLICY_HASH"
    model_source="cold_start_policy"
  fi
  local tmp="$CONTROL/runtime_status.json.tmp.$$"
  printf '{"schema":"polymarket_v7_runtime_status_v2","timestamp":%s,"version":7,"paper_only":true,"authenticated_execution":false,"real_order_submission":false,"model_sha":"%s","config_hash":"%s","policy_hash":"%s","model_hash":"%s","model_identity_source":"%s","run_id":"%s","ledger_id":"%s","server_id":"%s","pid":%s,"state":"%s","killed":%s,"primary_economic_sleeve":"MICRO_MAKER_PRO","execution_authority":"PAPER_EXECUTION_OWNER","single_execution_owner":true,"canonical_state_reconciled":true,"exact_sha_ci_green":%s,"p0_authority_configured":["professional_maker","crypto_settlement_fair","crypto_informed_taker"],"p0_full_stack_ready":%s,"readiness":"%s","external_fair_runtime_ready":%s}\n' \
    "$now" "$SHA" "$CONFIG_HASH" "$POLICY_HASH" "$model_hash" "$model_source" "$RUN_ID" "$LEDGER_ID" "$SERVER_ID" "$$" "$state" "$killed" "$EXACT_SHA_CI_GREEN" "$p0_ready" "$readiness" "$external_ready" > "$tmp"
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

# One canonical exhaustive metadata plane. The venue terminates pagination;
# HOT/WARM capacities are calculated from declared CPU/memory/WS budgets and
# COLD preserves the remainder. No strategy owns a parallel universe cache.
python3 scripts/v7_adaptive_universe.py \
  --config "$ADAPTIVE_UNIVERSE_CONFIG" --output-dir "$RUN_ROOT/universe" \
  --model-sha "$SHA" --loop \
  >> "$RUN_ROOT/universe/collector.log" 2>&1 &
pids+=("$!")
universe_ready=0
for _ in $(seq 1 600); do
  if python3 - "$RUN_ROOT/universe/status.json" "$SHA" <<'PY' >/dev/null 2>&1
import json,sys
value=json.load(open(sys.argv[1]))
ok=(value.get("schema")=="polymarket_v7_adaptive_universe_status_v1"
    and value.get("model_sha")==sys.argv[2] and value.get("state")=="OPERATIONAL"
    and value.get("discovery_exhaustive") is True and int(value.get("eligible_markets") or 0)>0
    and value.get("paper_only") is True and value.get("authenticated_execution") is False
    and value.get("real_order_submission") is False)
raise SystemExit(0 if ok else 1)
PY
  then
    universe_ready=1
    break
  fi
  sleep 0.5
done
if [[ "$universe_ready" != 1 ]]; then
  echo "adaptive V7 universe did not complete exhaustive discovery" >&2
  exit 77
fi
read -r HOT_MARKET_BUDGET ACTIVE_SCAN_MARKET_BUDGET STRUCTURAL_SCAN_EVENT_BUDGET < <(python3 - "$RUN_ROOT/universe/status.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
tiers=value.get("tier_counts") or {}
hot=max(1,int(tiers.get("HOT") or 0))
active=max(hot,hot+int(tiers.get("WARM") or 0))
structural=max(1,int((value.get("resource_capacities") or {}).get("structural_scan_budget_events") or 0))
print(hot,active,structural)
PY
)

maker_selection_ready() {
  python3 - "$RUN_ROOT/micro_maker/reward_selection.json" "$SHA" <<'PY' >/dev/null 2>&1
import json,sys
from pathlib import Path
path=Path(sys.argv[1])
if not path.is_file(): raise SystemExit(1)
try: obj=json.loads(path.read_text(encoding="utf-8"))
except Exception: raise SystemExit(1)
markets=obj.get("markets")
ok=(obj.get("paper_only") is True and obj.get("authenticated_execution") is False
    and obj.get("real_order_submission") is False and obj.get("model_sha")==sys.argv[2]
    and obj.get("source") in {"public_clob_rewards","adaptive_universe_fallback"}
    and isinstance(markets,list) and len(markets)>0)
raise SystemExit(0 if ok else 1)
PY
}

"$RECORDER" \
  --config "$CONFIG" \
  --run-dir "$RUN_ROOT" \
  --data-url "https://data-api.polymarket.com" \
  --markets "$ACTIVE_SCAN_MARKET_BUDGET" --batch 40 --min-liquidity 2 \
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
      --candidate-output "$RUN_ROOT/micro_maker/reward_selection_candidate.json" \
      --pin-runtime-selection \
      --status "$RUN_ROOT/micro_maker/selector_status.json" \
      --fallback-universe "$RUN_ROOT/universe/current.json" \
      --allocation "$ALLOC/micro_maker.json" \
      --model-sha "$SHA" \
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
  --markets "$HOT_MARKET_BUDGET" --min-liquidity 2 --shard-size 200 \
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
      --markets 0 --min-liquidity 2 --max-events "$STRUCTURAL_SCAN_EVENT_BUDGET" --min-edge 0.00005 \
      --max-trade-usd 1e100 --slippage-bps 5 --leg-latency-ms 100 \
      >> "$RUN_ROOT/hard_arb/runtime.log" 2>&1 || true
    sleep 5
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_micro_taker_worker.py \
      --config "$ALLOC/micro_taker.json" --run-dir "$RUN_ROOT/micro_taker" \
      --trade-tape "$RUN_ROOT/trade_tape.csv" --markets "$ACTIVE_SCAN_MARKET_BUDGET" --min-liquidity 2 \
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

# Candidate discovery is isolated from verification: lexical similarity may
# populate a review queue, but only exact attested semantic bundles can activate
# the verified-only forward reaction path.
python3 scripts/v7_osint_mapping_collector.py \
  --repository-root "$ROOT" \
  --config "$EXTERNAL_INPUT_CONFIG" \
  --mappings "$EXTERNAL_MAPPING_REGISTRY" \
  --raw-tape "$RUN_ROOT/osint/raw_events.jsonl" \
  --candidate-tape "$RUN_ROOT/osint/mapping_candidates.jsonl" \
  --forward-tape "$RUN_ROOT/osint/forward_reactions.jsonl" \
  --state "$RUN_ROOT/osint/mapping_state.json" \
  --status "$RUN_ROOT/osint/mapping_status.json" \
  --interval 60 --loop \
  >> "$RUN_ROOT/osint/mapping.log" 2>&1 &
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

# Sports uses the credential-gated Sportradar Realtime adapter. Without its key
# it remains alive and publishes CREDENTIALS_REQUIRED rather than fabricating a
# feed, latency sample or mapping.
python3 scripts/v7_sports_collector.py \
  --repository-root "$ROOT" \
  --config "$EXTERNAL_INPUT_CONFIG" \
  --mappings "$EXTERNAL_MAPPING_REGISTRY" \
  --tape "$RUN_ROOT/shadow/sports_latency/events.jsonl" \
  --state "$RUN_ROOT/shadow/sports_latency/collector_state.json" \
  --status "$RUN_ROOT/shadow/sports_latency/component_status.json" \
  --interval 10 --loop \
  >> "$RUN_ROOT/shadow/sports_latency/collector.log" 2>&1 &
pids+=("$!")

# Kalshi public REST market data begins collecting immediately. Polling is
# explicitly distinguished from event latency; semantic equivalence and fees
# remain independent fail-closed gates.
python3 scripts/v7_cross_platform_collector.py \
  --repository-root "$ROOT" \
  --config "$EXTERNAL_INPUT_CONFIG" \
  --mappings "$EXTERNAL_MAPPING_REGISTRY" \
  --tape "$RUN_ROOT/shadow/cross_platform/books.jsonl" \
  --state "$RUN_ROOT/shadow/cross_platform/collector_state.json" \
  --status "$RUN_ROOT/shadow/cross_platform/component_status.json" \
  --interval 30 --loop \
  >> "$RUN_ROOT/shadow/cross_platform/collector.log" 2>&1 &
pids+=("$!")

# The exact-SHA supervisor aggregates measured sports/cross evidence and the
# still configuration-blocked wallet sleeve. It owns no OMS, capital, ledger
# writer, order or promotion authority.
# Ranking, PCA and Local Factor are intentionally outside the approved live scope.
python3 scripts/v7_research_shadow_supervisor.py \
  --repository-root "$ROOT" \
  --run-root "$RUN_ROOT" \
  --scope "$LIVE_MODEL_SCOPE" \
  --model-sha "$SHA" \
  --heartbeat-seconds 5 \
  >> "$RUN_ROOT/research_shadow_supervisor.log" 2>&1 &
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
