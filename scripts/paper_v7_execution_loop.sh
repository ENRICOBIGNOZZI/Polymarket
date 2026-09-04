#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source scripts/v7_process_runtime.sh
CONFIG="${PM_V7_CONFIG:-config/paper_v7.json}"
RUN_ROOT="${PM_V7_RUN_ROOT:-runs/paper_v7_live}"
RECORDER="${PM_TRADE_RECORDER:-build/polymarket_v7_trade_recorder}"
MARKOUT_OBSERVER="${PM_V7_MAKER_MARKOUT_OBSERVER:-build/polymarket_v7_maker_markout_observer}"
FILLABILITY_OBSERVER="${PM_V7_MAKER_FILLABILITY_OBSERVER:-build/polymarket_v7_maker_fillability_observer}"
EXTERNAL_VENUE_RUNTIME="${PM_V7_EXTERNAL_VENUE_RUNTIME:-build/polymarket_v7_external_venue_runtime}"
FAST_STRUCTURAL_RUNTIME="${PM_V7_FAST_STRUCTURAL_RUNTIME:-build/polymarket_v7_fast_structural_runtime}"
MAKER_POLICY="${PM_V7_MAKER_POLICY:-config/v7_professional_market_maker.json}"
FAST_STRUCTURAL_POLICY="${PM_V7_FAST_STRUCTURAL_POLICY:-config/v7_fast_structural.json}"
FAST_STRUCTURAL_RELATIONS="${PM_V7_FAST_STRUCTURAL_RELATIONS:-config/v7_fast_structural_relations.csv}"
EXTERNAL_FAIR_POLICY="${PM_V7_EXTERNAL_FAIR_POLICY:-config/v7_external_fair.json}"
EXTERNAL_FORWARD_MIN_DURATION_SECONDS="${PM_V7_EXTERNAL_FORWARD_MIN_DURATION_SECONDS:-}"
EXTERNAL_SOURCE_REGISTRY="${PM_V7_EXTERNAL_SOURCE_REGISTRY:-config/v7_external_source_registry.json}"
CI_REPOSITORY="${PM_V7_CI_REPOSITORY:-ENRICOBIGNOZZI/Polymarket}"
LIVE_MODEL_SCOPE="${PM_V7_LIVE_MODEL_SCOPE:-config/v7_live_model_scope.json}"
CRYPTO_SETTLEMENT_ENGINE_POLICY="${PM_V7_CRYPTO_SETTLEMENT_ENGINE_POLICY:-config/v7_crypto_settlement_engine.json}"
CRYPTO_SETTLEMENT_MARKET_REGISTRY="${PM_V7_CRYPTO_SETTLEMENT_MARKET_REGISTRY:-config/v7_crypto_settlement_markets.json}"
CRYPTO_SETTLEMENT_MODEL_REGISTRY="${PM_V7_CRYPTO_SETTLEMENT_MODEL_REGISTRY:-config/v7_crypto_settlement_model_registry.json}"
ADAPTIVE_UNIVERSE_CONFIG="${PM_V7_ADAPTIVE_UNIVERSE_CONFIG:-config/v7_adaptive_universe.json}"
# The sole legacy-environment compatibility boundary.  Strategy and collector
# code consume PM_V7_* names only; no value is logged or written to config.
export PM_V7_BINANCE_API_KEY="${PM_V7_BINANCE_API_KEY:-${PORTFOLIO_BINANCE_API_KEY:-}}"
export PM_V7_BINANCE_API_SECRET="${PM_V7_BINANCE_API_SECRET:-${PORTFOLIO_BINANCE_API_SECRET:-}}"
export PM_V7_BINANCE_TESTNET_API_KEY="${PM_V7_BINANCE_TESTNET_API_KEY:-${PORTFOLIO_BINANCE_TESTNET_API_KEY:-}}"
export PM_V7_BINANCE_TESTNET_API_SECRET="${PM_V7_BINANCE_TESTNET_API_SECRET:-${PORTFOLIO_BINANCE_TESTNET_API_SECRET:-}}"
export PM_V7_BINANCE_TESTNET_BASE_URL="${PM_V7_BINANCE_TESTNET_BASE_URL:-${PORTFOLIO_BINANCE_TESTNET_BASE_URL:-}}"
export PM_V7_BINANCE_TESTNET_MARKET="${PM_V7_BINANCE_TESTNET_MARKET:-${PORTFOLIO_BINANCE_TESTNET_MARKET:-}}"
SHA="$(git rev-parse HEAD)"
if [[ -z "$EXTERNAL_FORWARD_MIN_DURATION_SECONDS" ]]; then
  EXTERNAL_FORWARD_MIN_DURATION_SECONDS="$(python3 - "$EXTERNAL_FAIR_POLICY" <<'PY'
import json
import sys

try:
    value = float(json.load(open(sys.argv[1], encoding="utf-8"))["forward_evidence"]["minimum_duration_seconds"])
except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid external fair forward-evidence policy: {exc}")
if value < 0:
    raise SystemExit("external fair forward-evidence duration must be non-negative")
print(value)
PY
)"
fi
MAKER_CHAMPION_MODEL="$RUN_ROOT/micro_maker/execution_model.json"
MAKER_CHALLENGER_MODEL="$RUN_ROOT/micro_maker/execution_model_challenger.json"
MAKER_MODEL_REGISTRY="$RUN_ROOT/micro_maker/model_registry.json"
DURABLE_ROOT="${PM_V7_DURABLE_ROOT:-runs/paper_v7_durable}"
MAKER_DURABLE_STORE="$DURABLE_ROOT/micro_maker/evidence.jsonl"
MAKER_DURABLE_STATUS="$DURABLE_ROOT/micro_maker/status.json"
PUBLIC_PROXY_PORT="${PM_V7_PUBLIC_PROXY_PORT:-19109}"
PUBLIC_PROXY="http://127.0.0.1:$PUBLIC_PROXY_PORT"
WS_PUBLIC_HOST="ws-subscriptions-clob.polymarket.com"
# Adaptive JSON arenas are bounded per decoder. With the current canonical
# resource-derived Maker universe owns the declared number of 8-market shards, while the
# evidence-only markout and fillability observers each own one all-market
# decoder. The defaults expose a 4 GiB aggregate ceiling without eagerly
# allocating it; each decoder starts small and grows only for large venue frames.
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
MAKER_FREEZE="$CONTROL/MAKER_FREEZE"
LOCK="$CONTROL/runtime.lock"
mkdir -p "$CONTROL" "$RUN_ROOT/ledger" "$RUN_ROOT/opportunities/inbox" "$RUN_ROOT/research/evidence" "$RUN_ROOT/market_data" "$RUN_ROOT/universe" "$RUN_ROOT/fast_structural" "$RUN_ROOT/structural_relations" "$RUN_ROOT/hard_arb" "$RUN_ROOT/micro_maker" "$RUN_ROOT/external" "$RUN_ROOT/external_fair" "$RUN_ROOT/learned_execution"
touch "$RUN_ROOT/ledger/execution.jsonl"

# The runtime is not allowed to self-assert CI approval through an environment
# flag. Query the public check-runs for this exact immutable SHA and preserve
# the resulting receipt beside the canonical control state before any worker
# can start. Network ambiguity or a missing/failed check is a launch failure.
python3 scripts/v7_exact_sha_ci_gate.py \
  --repository "$CI_REPOSITORY" --sha "$SHA" \
  --output "$CONTROL/exact_sha_ci_receipt.json"
EXACT_SHA_CI_GREEN=true

# Source registration is an authority boundary, not a best-effort manifest.
# Collectors may only publish information; this registry cannot grant OMS,
# capital, ledger, execution, or promotion authority.
python3 scripts/v7_external_source_registry.py --registry "$EXTERNAL_SOURCE_REGISTRY" \
  > "$CONTROL/external_source_registry.json"

python3 - "$CONFIG" "$MAKER_POLICY" "$EXTERNAL_FAIR_POLICY" "$LIVE_MODEL_SCOPE" "$ADAPTIVE_UNIVERSE_CONFIG" "$WS_JSON_ARENA_OBSERVER_MAX_BYTES" "$WS_JSON_ARENA_FILLABILITY_MAX_BYTES" "$WS_JSON_ARENA_TOTAL_BUDGET_BYTES" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
v7=cfg.get("v7") or {}
maker=json.load(open(sys.argv[2]))
external=json.load(open(sys.argv[3]))
live_scope=json.load(open(sys.argv[4]))
adaptive_universe=json.load(open(sys.argv[5]))
registry_path=v7.get("strategy_registry")
assert isinstance(registry_path,str) and registry_path
registry=json.load(open(registry_path))
algorithms={"CRYPTO_SETTLEMENT_ENGINE","STRUCTURAL_ARB_ENGINE"}
registered=[row.get("id") for row in registry.get("live_algorithms",[])]
assert registry.get("schema")=="polymarket_v7_live_algorithm_registry_v2"
assert set(registered)==algorithms and len(registered)==2
assert registry.get("safety",{}).get("paper_only") is True
assert registry.get("safety",{}).get("authenticated_execution") is False
assert registry.get("safety",{}).get("real_order_submission") is False
assert registry.get("governance",{}).get("automatic_promotion") is False
assert all(row.get("mode") == "PAPER" and row.get("enabled") is True for row in registry.get("live_algorithms",[]))
assert live_scope.get("schema") == "polymarket_v7_live_engine_scope_v2"
assert live_scope.get("version") == 7 and live_scope.get("live_algorithm_count") == 2
assert live_scope.get("paper_only") is True
assert live_scope.get("authenticated_execution") is False
assert live_scope.get("real_order_submission") is False
assert set(live_scope.get("live_algorithms") or []) == algorithms
assert len(live_scope.get("legacy_algorithm_families_removed") or []) == 10
governance=live_scope.get("governance") or {}
assert governance.get("single_execution_owner") is True
assert governance.get("automatic_promotion") is False
assert adaptive_universe.get("schema") == "polymarket_v7_adaptive_universe_config_v1"
assert adaptive_universe.get("version") == 7
assert adaptive_universe.get("paper_only") is True
assert adaptive_universe.get("authenticated_execution") is False
assert adaptive_universe.get("real_order_submission") is False
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
capacity=maker.get("market_selection",{}).get("resource_capacity",{})
max_shards=int(capacity.get("shard_count_budget",0))
markets_per_shard=int(capacity.get("markets_per_shard",0))
assert max_shards > 0 and markets_per_shard == 8
assert int(maker.get("market_selection",{}).get("max_active_markets",0)) == max_shards*markets_per_shard
observation_fractions=v7.get("component_observation_budget_fractions") or {}
expected_maker_observation_budget=float(cfg.get("starting_capital",0))*float(observation_fractions.get("professional_maker",0))
assert abs(float(maker.get("market_selection",{}).get("reward_sleeve_capital_usd",0))-expected_maker_observation_budget) <= 1e-9
assert external.get("execution_authority") == "PAPER_EXECUTION_OWNER"
assert external.get("paper_only") is True
assert external.get("authenticated_execution") is False
assert external.get("real_order_submission") is False
assert external.get("taker",{}).get("authority") == "PAPER"
assert external.get("taker",{}).get("enabled_for_execution") is True
assert external.get("taker",{}).get("execution_scope") == "PAPER_EXPLORATION_ONLY"
assert external.get("taker",{}).get("counterfactual_enabled") is True
assert external.get("paper_exploration",{}).get("accounting_mode") == "CANONICAL_PAPER_ACCOUNT"
assert external.get("fair_value",{}).get("default_model_mature") is False
assert external.get("maker",{}).get("external_fair_enabled_for_live_quotes") is False
assert external.get("maker",{}).get("economic_maturity_may_block_paper") is True
assert external.get("gate_classes",{}).get("A_HARD_CORRECTNESS_SAFETY",{}).get("may_block_paper") is True
assert external.get("gate_classes",{}).get("B_ECONOMIC_MATURITY",{}).get("may_block_paper") is True
assert observer_arena >= 16*1024*1024
assert fillability_arena >= 16*1024*1024
assert observer_arena + fillability_arena <= total_budget
PY

python3 scripts/v7_process_manifest.py \
  --repository-root "$ROOT" \
  --output "$CONTROL/process_manifest_resolved.json"

# Freeze the horizon/authority contract even during execution-evidence cold
# start. With no empirical ACK/cancel profile supplied, the resulting snapshot
# deliberately authorizes CANCEL/WITHDRAW/NOTHING only.
python3 scripts/v7_crypto_settlement_engine_contract.py \
  --config "$CRYPTO_SETTLEMENT_ENGINE_POLICY" \
  --registry "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["v7"]["strategy_registry"])' "$CONFIG")" \
  --live-scope "$LIVE_MODEL_SCOPE" --market-registry "$CRYPTO_SETTLEMENT_MARKET_REGISTRY" \
  --model-registry "$CRYPTO_SETTLEMENT_MODEL_REGISTRY" \
  --code-sha "$SHA" --asset BTC --horizon M5 \
  --output "$CONTROL/crypto_settlement_engine_snapshot.json" \
  > "$CONTROL/crypto_settlement_engine_snapshot.summary.json"

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
rm -f "$KILL" "$MAKER_FREEZE"

python3 scripts/v7_capital_allocator.py --config "$CONFIG" --output-dir "$ALLOC" >/dev/null
python3 scripts/v7_evidence_capital_allocator.py \
  --allocation "$ALLOC/manifest.json" --economics "$RUN_ROOT/canonical_economics.json" \
  --output "$CONTROL/evidence_capital_allocator.json" >/dev/null
# Materialize an exact-SHA PAPER champion before the C++ cohort starts. The
# append-only store lives outside the ephemeral live run root and therefore
# survives archive rotation and code cutovers. Incompatible policy/config
# generations remain auditable but cannot train the active snapshot.
python3 scripts/v7_maker_durable_learning.py \
  --source-root runs/paper_v7_archives --source-root "$RUN_ROOT" \
  --store "$MAKER_DURABLE_STORE" --store-status "$MAKER_DURABLE_STATUS" \
  --champion "$MAKER_CHAMPION_MODEL" --policy "$MAKER_POLICY" \
  --config "$ALLOC/micro_maker.json" --model-sha "$SHA" \
  >> "$RUN_ROOT/micro_maker/durable_learning.log" 2>&1
pids=()

if [[ ! -f scripts/v7_public_https_proxy.py ]]; then
  echo "missing V7 public HTTPS proxy" >&2
  exit 77
fi

# Start the public-DNS HTTPS tunnel before any child that performs public REST
# traffic.  Environment exports are inherited only by subsequently spawned
# children; starting the External Fair PAPER router before this block leaves
# its CLOB `/books` calls on the filtered operating-system resolver and turns
# every economically actionable fair into a silent `NOTHING` decision.
python3 scripts/v7_public_https_proxy.py --host 127.0.0.1 --port "$PUBLIC_PROXY_PORT" \
  >> "$RUN_ROOT/public_https_proxy.log" 2>&1 &
v7_register_child "$!"

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

# Freeze one exact-SHA calibration challenger from already-settled SHADOW
# contracts before the RTDS monitor starts. This publishes no execution
# authority; every subsequent settlement is immutable forward OOS evidence.
python3 scripts/v7_external_fair_challenger.py \
  --tape "$RUN_ROOT/../paper_v7_durable/external_fair/counterfactuals.jsonl" \
  --tape "$RUN_ROOT/external_fair/counterfactuals.jsonl" \
  --registry "$RUN_ROOT/external_fair/model_registry" \
  --config "$EXTERNAL_FAIR_POLICY" --model-sha "$SHA" \
  --status "$RUN_ROOT/external_fair/challenger_status.json" \
  >> "$RUN_ROOT/external_fair/challenger.log" 2>&1 || true

# Paid Chainlink Data Streams are intentionally out of scope. Public RTDS
# provides the Chainlink 60-second TWAP observability tape. It never replaces
# the contract resolution oracle or bypasses contract-local verification.
python3 scripts/v7_rtds_external_fair_monitor.py \
  --output-dir "$RUN_ROOT/external_fair" --code-sha "$SHA" \
  --universe "$RUN_ROOT/universe/current.json" \
  --approvals "config/v7_external_fair_rule_approvals.json" \
  --external-venues "$RUN_ROOT/external_fair/external_venues.json" \
  --champion-pointer "$RUN_ROOT/external_fair/model_registry/fair_value_champion.json" \
  --challenger-pointer "$RUN_ROOT/external_fair/model_registry/fair_value_challenger.json" \
  --external-fair-config "$ROOT/config/v7_external_fair.json" \
  >> "$RUN_ROOT/external_fair/rtds_monitor.log" 2>&1 &
v7_register_child "$!"

"$EXTERNAL_VENUE_RUNTIME" \
  --output "$RUN_ROOT/external_fair/external_venues.json" \
  --tape "$RUN_ROOT/external_fair/tapes/external_venues.${SHA}.$$.bin" --model-sha "$SHA" \
  --normalized-event-tape-dir "$RUN_ROOT/external_fair/normalized_events" \
  --raw-tape-dir "$RUN_ROOT/external_fair/raw" \
  >> "$RUN_ROOT/external_fair/external_venues.log" 2>&1 &
v7_register_child "$!"

# Slow public USD-M market state complements the event-driven depth channel.
# It is explicitly polling data and cannot become an execution trigger by
# pretending to have exchange-event latency.
python3 scripts/v7_binance_usdm_rest_collector.py \
  --status "$RUN_ROOT/external_fair/binance_usdm_rest_status.json" \
  --tape "$RUN_ROOT/external_fair/binance_usdm_rest.jsonl" --interval 5 --loop \
  >> "$RUN_ROOT/external_fair/binance_usdm_rest.log" 2>&1 &
v7_register_child "$!"
python3 scripts/v7_deribit_rest_collector.py \
  --status "$RUN_ROOT/external_fair/deribit_rest_status.json" \
  --tape "$RUN_ROOT/external_fair/deribit_rest.jsonl" --interval 15 --loop \
  >> "$RUN_ROOT/external_fair/deribit_rest.log" 2>&1 &
v7_register_child "$!"
python3 scripts/v7_coinbase_l2_rest_collector.py \
  --status "$RUN_ROOT/external_fair/coinbase_l2_rest_status.json" \
  --tape "$RUN_ROOT/external_fair/coinbase_l2_rest.jsonl" --interval 5 --loop \
  >> "$RUN_ROOT/external_fair/coinbase_l2_rest.log" 2>&1 &
v7_register_child "$!"
python3 scripts/v7_external_forward_evidence_gate.py \
  --runtime-status "$RUN_ROOT/external_fair/external_venues.json" \
  --coinbase-rest-status "$RUN_ROOT/external_fair/coinbase_l2_rest_status.json" \
  --deribit-rest-status "$RUN_ROOT/external_fair/deribit_rest_status.json" \
  --output "$RUN_ROOT/external_fair/forward_evidence_status.json" \
  --min-duration-seconds "$EXTERNAL_FORWARD_MIN_DURATION_SECONDS" --loop \
  >> "$RUN_ROOT/external_fair/forward_evidence.log" 2>&1 &
v7_register_child "$!"

python3 scripts/v7_external_fair_paper_router.py \
  --run-root "$RUN_ROOT" --model-sha "$SHA" --config "$EXTERNAL_FAIR_POLICY" --interval 1 \
  >> "$RUN_ROOT/external_fair/paper_router.log" 2>&1 &
v7_register_child "$!"

CONFIG_HASH="$(git hash-object "$CONFIG")"
POLICY_HASH="$(git hash-object "$MAKER_POLICY")"
RUN_ID="${PM_V7_RUN_ID:-${SHA:0:12}-$(date +%s)-$$}"
LEDGER_ID="${PM_V7_LEDGER_ID:-$RUN_ID:execution}"
SERVER_ID="${PM_V7_SERVER_ID:-$(hostname -s 2>/dev/null || hostname)}"

paper_router_ready() {
  python3 - "$RUN_ROOT/external_fair/status.json" "$RUN_ROOT/external_fair/paper_router_status.json" "$SHA" <<'PY'
import json,sys,time
try:
    status=json.load(open(sys.argv[1], encoding="utf-8"))
    value=json.load(open(sys.argv[2], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
contract=status.get("contract") if isinstance(status.get("contract"),dict) else {}
reference=status.get("settlement_reference") if isinstance(status.get("settlement_reference"),dict) else {}
fair=status.get("fair") if isinstance(status.get("fair"),dict) else {}
oracle=status.get("oracle") if isinstance(status.get("oracle"),dict) else {}
external=status.get("external") if isinstance(status.get("external"),dict) else {}
decision=value.get("last_decision") if isinstance(value.get("last_decision"),dict) else {}
ok=(status.get("schema")=="polymarket_v7_external_fair_status_v1"
    and status.get("code_sha")==sys.argv[3]
    and status.get("state")=="FULL_FAIR_PAPER_OPERATIONAL"
    and status.get("paper_only") is True
    and status.get("authenticated_execution") is False
    and status.get("real_order_submission") is False
    and not status.get("blockers")
    and int(status.get("external_fair_required_markets") or 0)>=1
    and contract.get("verified") is True
    and contract.get("rules_hash_recognized") is True
    and reference.get("valid") is True
    and fair.get("valid") is True
    and oracle.get("healthy") is True
    and external.get("healthy") is True
    and value.get("schema")=="polymarket_v7_crypto_settlement_engine_status_v1"
    and value.get("code_sha")==sys.argv[3]
    and value.get("state")=="RUNNING"
    and value.get("paper_only") is True
    and value.get("authenticated_execution") is False
    and value.get("real_order_submission") is False
    and value.get("execution_authority")=="PAPER_EXECUTION_OWNER"
    and value.get("capital_authority") is False
    and value.get("oms_authority") is False
    and value.get("inventory_authority") is False
    and value.get("ledger_writer_authority") is False
    and value.get("order_submission_enabled") is True
    and value.get("counterfactual_collection_enabled") is True
    and value.get("killed") is False
    and not value.get("blocker")
    and int(value.get("book_requests") or 0)>0
    and int(decision.get("books") or 0)==2
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
  printf '{"schema":"polymarket_v7_runtime_status_v3","timestamp":%s,"version":7,"paper_only":true,"authenticated_execution":false,"real_order_submission":false,"real_capital_at_risk":false,"model_sha":"%s","config_hash":"%s","policy_hash":"%s","model_hash":"%s","model_identity_source":"%s","run_id":"%s","ledger_id":"%s","server_id":"%s","pid":%s,"state":"%s","killed":%s,"economic_system":"V7_UNIFIED","economic_engines":["CRYPTO_SETTLEMENT_ENGINE","STRUCTURAL_ARB_ENGINE"],"global_portfolio_coordinator":"V7_GLOBAL_PORTFOLIO_COORDINATOR","execution_authority":"V7_CANONICAL_CHAIN","single_execution_owner":true,"canonical_state_reconciled":true,"exact_sha_ci_green":%s,"p0_authority_configured":["CRYPTO_SETTLEMENT_ENGINE","STRUCTURAL_ARB_ENGINE"],"p0_full_stack_ready":%s,"readiness":"%s","external_fair_runtime_ready":%s,"economic_new_risk_ready":false,"paper_exploration_ready":true,"paper_account_mode":"ACTIVE_SIMULATED","economic_decision_state":"BOUNDED_PAPER_EXPLORATION","authorized_alpha_actions":[],"authorized_paper_actions":["PAPER_EXPLORATION"],"safe_actions":["CANCEL","WITHDRAW","NOTHING"]}\n' \
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
# One canonical exhaustive metadata plane. The venue terminates pagination;
# HOT/WARM capacities are calculated from declared CPU/memory/WS budgets and
# COLD preserves the remainder. No strategy owns a parallel universe cache.
python3 scripts/v7_adaptive_universe.py \
  --config "$ADAPTIVE_UNIVERSE_CONFIG" --output-dir "$RUN_ROOT/universe" \
  --model-sha "$SHA" --loop \
  >> "$RUN_ROOT/universe/collector.log" 2>&1 &
v7_register_child "$!"
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
read -r HOT_MARKET_BUDGET ACTIVE_SCAN_MARKET_BUDGET STRUCTURAL_SCAN_EVENT_BUDGET MAKER_FLOW_LOOKBACK_SECONDS MAKER_SELECTOR_REFRESH_SECONDS MAKER_ROTATION_INTERVAL_SECONDS MAKER_CANDIDATE_CONFIRMATIONS MAKER_ROTATION_MIN_FILL MAKER_ROTATION_MIN_ABSOLUTE_IMPROVEMENT MAKER_ROTATION_MIN_RELATIVE_MULTIPLIER < <(python3 - "$RUN_ROOT/universe/status.json" "$MAKER_POLICY" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
maker=json.load(open(sys.argv[2]))
tiers=value.get("tier_counts") or {}
hot=max(1,int(tiers.get("HOT") or 0))
active=max(hot,hot+int(tiers.get("WARM") or 0))
structural=max(1,int((value.get("resource_capacities") or {}).get("structural_scan_budget_events") or 0))
flow=max(180,int(((maker.get("market_selection") or {}).get("recent_flow") or {}).get("lookback_seconds") or 0))
recent=((maker.get("market_selection") or {}).get("recent_flow") or {})
refresh=max(1,int(recent.get("selector_refresh_seconds") or 5))
rotation=max(0,int(recent.get("rotation_min_interval_seconds") or 300))
confirmations=max(2,int(recent.get("candidate_confirmations") or 2))
minimum_fill=max(0.0,min(1.0,float(recent.get("rotation_min_projected_fill_probability") or 0.004)))
absolute_improvement=max(0.0,min(1.0,float(recent.get("rotation_min_absolute_fill_improvement") or 0.05)))
relative_multiplier=max(1.0,float(recent.get("rotation_min_relative_fill_multiplier") or 1.5))
print(hot,active,structural,flow,refresh,rotation,confirmations,
      minimum_fill,absolute_improvement,relative_multiplier)
PY
)

# Build only deterministically proven same-event NegRisk relations. The C++
# runtime never receives text-similarity candidates or unverified mappings.
VERIFIED_RELATIONS="$RUN_ROOT/structural_relations/verified_relations.csv"
python3 scripts/v7_relation_builder.py \
  --universe "$RUN_ROOT/universe/current.json" \
  --registry "$RUN_ROOT/structural_relations/relation_registry.json" \
  --runtime-csv "$VERIFIED_RELATIONS" --model-sha "$SHA" \
  >> "$RUN_ROOT/structural_relations/relation_builder.log" 2>&1

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_relation_builder.py \
      --universe "$RUN_ROOT/universe/current.json" \
      --registry "$RUN_ROOT/structural_relations/relation_registry.json" \
      --runtime-csv "$VERIFIED_RELATIONS" --model-sha "$SHA" \
      >> "$RUN_ROOT/structural_relations/relation_builder.log" 2>&1 || true
    sleep 60
  done
) & v7_register_child "$!"

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
    and obj.get("source") in {"public_clob_rewards","adaptive_universe_fallback","adaptive_universe_recent_flow"}
    and isinstance(markets,list) and len(markets)>0)
raise SystemExit(0 if ok else 1)
PY
}

fee_registry_ready() {
  python3 - "$CONTROL/fee_reward_registry.json" "$SHA" <<'PY' >/dev/null 2>&1
import json,sys,time
from pathlib import Path
path=Path(sys.argv[1])
if not path.is_file(): raise SystemExit(1)
try: obj=json.loads(path.read_text(encoding="utf-8"))
except Exception: raise SystemExit(1)
now=int(time.time()*1000)
markets=obj.get("markets")
rows=markets if isinstance(markets,list) else []
fresh_verified=any(
    isinstance(row,dict) and isinstance(row.get("fee"),dict)
    and row["fee"].get("verified") is True
    and int(row["fee"].get("observed_at_ms") or 0) <= now <= int(row["fee"].get("expires_at_ms") or 0)
    for row in rows
)
ok=(obj.get("schema")=="polymarket_v7_fee_reward_registry_v1"
    and obj.get("paper_only") is True and obj.get("authenticated_execution") is False
    and obj.get("real_order_submission") is False and obj.get("execution_authority") is False
    and obj.get("model_sha")==sys.argv[2]
    and obj.get("unknown_fee_policy")=="NON_EXECUTABLE"
    and obj.get("unknown_reward_policy")=="ZERO_EXPECTED_VALUE"
    and obj.get("automatic_promotion") is False
    and len(rows)>0
    and int(obj.get("executable_market_count") or 0)>0 and fresh_verified)
raise SystemExit(0 if ok else 1)
PY
}

"$RECORDER" \
  --config "$CONFIG" \
  --run-dir "$RUN_ROOT" \
  --data-url "https://data-api.polymarket.com" \
  --markets "$ACTIVE_SCAN_MARKET_BUDGET" --batch 40 --min-liquidity 2 \
  --lookback-seconds "$MAKER_FLOW_LOOKBACK_SECONDS" --interval 5 --loop \
  >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
v7_register_child "$!"

# One persistent canonical ledger router. 100ms transport cadence keeps FILL
# evidence available before the 1s markout horizon without creating a second
# ledger writer or repeatedly spawning Python processes.
python3 scripts/v7_ledger_spool.py \
  --run-root "$RUN_ROOT" --model-sha "$SHA" --loop --interval 0.1 \
  >> "$RUN_ROOT/ledger_router.log" 2>&1 &
v7_register_child "$!"

# The only consumer of proposals from both economic engines. Checked-in V7
# cannot authorize new risk; the coordinator may select CANCEL/WITHDRAW or emit NOTHING.
python3 scripts/v7_global_portfolio_coordinator.py \
  --run-root "$RUN_ROOT" --loop --interval 0.1 \
  >> "$RUN_ROOT/global_portfolio_coordinator.log" 2>&1 &
v7_register_child "$!"

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
      --live-flow "$RUN_ROOT/market_data/live_trade_flow.json" \
      --trade-tape "$RUN_ROOT/trade_tape.csv" \
      --allocation "$ALLOC/micro_maker.json" \
      --execution-model "$MAKER_CHAMPION_MODEL" \
      --model-sha "$SHA" \
      >> "$RUN_ROOT/micro_maker/reward_selection.log" 2>&1 || true
    sleep "$MAKER_SELECTOR_REFRESH_SECONDS"
  done
) & v7_register_child "$!"

# Exact-SHA fee/reward evidence registry. Unknown fees are explicitly
# non-executable and unknown rewards are forced to zero; this process has no
# OMS, ledger or accounting authority.
python3 scripts/v7_fee_reward_registry.py \
  --universe "$RUN_ROOT/universe/current.json" \
  --rewards "$RUN_ROOT/micro_maker/reward_selection.json" \
  --output "$CONTROL/fee_reward_registry.json" \
  --model-sha "$SHA" --interval 30 \
  >> "$RUN_ROOT/fee_reward_registry.log" 2>&1 &
v7_register_child "$!"

# V7 Fast Structural is the zero-authority detector/planning component of the
# structural-arbitrage owner. It may publish candidates and execution-policy
# evidence, but it cannot publish simulated orders or fills independently.
"$FAST_STRUCTURAL_RUNTIME" \
  --config "$ALLOC/fast_structural.json" \
  --policy "$FAST_STRUCTURAL_POLICY" \
  --relations "$VERIFIED_RELATIONS" \
  --run-dir "$RUN_ROOT/fast_structural" --run-root "$RUN_ROOT" --model-sha "$SHA" \
  --markets "$ACTIVE_SCAN_MARKET_BUDGET" --min-liquidity 2 --shard-size 200 \
  >> "$RUN_ROOT/fast_structural/runtime.log" 2>&1 &
v7_register_child "$!"

# Slow-plane exact-SHA fill/markout fit. A refit is a CHALLENGER only. It is
# written to a separate artifact and registered for OOS/shadow-PAPER review.
# This loop NEVER overwrites the runtime champion and NEVER auto-promotes.
(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_maker_durable_learning.py \
      --source-root runs/paper_v7_archives --source-root "$RUN_ROOT" \
      --store "$MAKER_DURABLE_STORE" --store-status "$MAKER_DURABLE_STATUS" \
      --champion "$MAKER_CHAMPION_MODEL" --policy "$MAKER_POLICY" \
      --config "$ALLOC/micro_maker.json" --model-sha "$SHA" \
      >> "$RUN_ROOT/micro_maker/durable_learning.log" 2>&1 || true
    if [[ -s "$RUN_ROOT/ledger/execution.jsonl" ]]; then
      if python3 scripts/v7_market_maker_model.py \
        --ledger "$RUN_ROOT/ledger/execution.jsonl" \
        --markout-evidence-root "$RUN_ROOT/research/evidence/maker_markout" \
        --model-sha "$SHA" \
        --policy "$MAKER_POLICY" \
        --config "$ALLOC/micro_maker.json" \
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
) & v7_register_child "$!"

# The professional Maker is now an execution-model component of the single crypto
# settlement owner. Keep its exact-WS fillability and fill-conditioned markout
# observers live, but do not launch the legacy independent PAPER maker runtime.
(
  while [[ ! -e "$KILL" ]] && { ! maker_selection_ready || ! fee_registry_ready; }; do sleep 1; done
  [[ ! -e "$KILL" ]] || exit 0
  exec python3 scripts/v7_maker_cohort_supervisor.py \
    --repository-root "$ROOT" \
    --run-root "$RUN_ROOT" \
    --config "$ALLOC/micro_maker.json" \
    --maker-policy "$MAKER_POLICY" \
    --selection "$RUN_ROOT/micro_maker/reward_selection.json" \
    --candidate "$RUN_ROOT/micro_maker/reward_selection_candidate.json" \
    --model "$MAKER_CHAMPION_MODEL" \
    --model-sha "$SHA" \
    --markout-observer "$MARKOUT_OBSERVER" \
    --fillability-observer "$FILLABILITY_OBSERVER" \
    --observer-arena-bytes "$WS_JSON_ARENA_OBSERVER_MAX_BYTES" \
    --fillability-arena-bytes "$WS_JSON_ARENA_FILLABILITY_MAX_BYTES" \
    --candidate-confirmations "$MAKER_CANDIDATE_CONFIRMATIONS" \
    --min-rotation-interval-seconds "$MAKER_ROTATION_INTERVAL_SECONDS" \
    --rotation-min-projected-fill-probability "$MAKER_ROTATION_MIN_FILL" \
    --rotation-min-absolute-fill-improvement "$MAKER_ROTATION_MIN_ABSOLUTE_IMPROVEMENT" \
    --rotation-min-relative-fill-multiplier "$MAKER_ROTATION_MIN_RELATIVE_MULTIPLIER"
) >> "$RUN_ROOT/micro_maker/cohort_supervisor.log" 2>&1 &
v7_register_child "$!"

# Hourly exact-SHA evidence pack. Reports are observational only and remain
# outside the repository checkout, so generating them cannot mutate deployed
# code or create a second cutover SHA.
(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_generate_economic_artifacts.py \
      --repo "$ROOT" --run-root "$RUN_ROOT" --output "$RUN_ROOT/reports" \
      --baseline "$ROOT/artifacts/v7_economic_loop_baseline.json" \
      >> "$RUN_ROOT/economic_artifacts.log" 2>&1 || true
    sleep 3600
  done
) & v7_register_child "$!"

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_graph_cost_vector.py --run-root "$RUN_ROOT" --model-sha "$SHA" --slippage-bps 5 \
      >> "$RUN_ROOT/structural_relations/cost_vector.log" 2>&1 || true
    python3 scripts/v7_joint_execution_policy.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
      --output "$RUN_ROOT/learned_execution/joint_policy.json" --strategy STRUCTURAL_ARB_ENGINE --min-bundles 20 \
      >> "$RUN_ROOT/learned_execution/joint_policy.log" 2>&1 || true
    python3 scripts/v7_learned_execution_model.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
      --output "$RUN_ROOT/learned_execution/oos_report.json" \
      >> "$RUN_ROOT/learned_execution/model.log" 2>&1 || true
    python3 scripts/v7_canonical_economics.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --expected-model-sha "$SHA" \
      --output "$RUN_ROOT/canonical_economics.json" >> "$RUN_ROOT/canonical_economics.log" 2>&1 || true
    python3 scripts/v7_evidence_capital_allocator.py \
      --allocation "$ALLOC/manifest.json" --economics "$RUN_ROOT/canonical_economics.json" \
      --output "$CONTROL/evidence_capital_allocator.json" \
      >> "$RUN_ROOT/evidence_capital_allocator.log" 2>&1 || true
    sleep 60
  done
) & v7_register_child "$!"

v7_assert_registered_child_count 20
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
