#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source scripts/v7_process_runtime.sh
RUNTIME_PROFILE="${PM_V7_RUNTIME_PROFILE:-config/runtime/paper.json}"
while IFS=$'\t' read -r key value; do
  case "$key" in
    CONFIG|MAKER_POLICY|EXTERNAL_FAIR_POLICY|LEAD_LAG_TAKER_CONFIG|CRYPTO_EXECUTION_ALPHA_CONFIG|EXTERNAL_SOURCE_REGISTRY|LIVE_MODEL_SCOPE|CRYPTO_SETTLEMENT_ENGINE_POLICY|CRYPTO_SETTLEMENT_MARKET_REGISTRY|CRYPTO_SETTLEMENT_MODEL_REGISTRY|LONDON_BUFFER_RETENTION_CONFIG|CRYPTO_UNIVERSE_CONFIG|RUNTIME_RESOURCE_CONFIG|PM_V7_EXECUTION_MODE)
      printf -v "$key" '%s' "$value" ;;
    *) echo "unexpected runtime-profile key: $key" >&2; exit 78 ;;
  esac
done < <(python3 scripts/v7_runtime_profile.py --repository-root "$ROOT" --profile "$RUNTIME_PROFILE" --shell)
export PM_V7_EXECUTION_MODE
RUN_ROOT="${PM_V7_RUN_ROOT:-runs/paper_v7_live}"
RECORDER="${PM_TRADE_RECORDER:-build/polymarket_v7_trade_recorder}"
FILLABILITY_OBSERVER="${PM_V7_MAKER_FILLABILITY_OBSERVER:-build/polymarket_v7_maker_fillability_observer}"
EXTERNAL_VENUE_RUNTIME="${PM_V7_EXTERNAL_VENUE_RUNTIME:-build/polymarket_v7_external_venue_runtime}"
CRYPTO_SETTLEMENT_ENGINE="${PM_V7_CRYPTO_SETTLEMENT_ENGINE:-build/polymarket_v7_crypto_settlement_engine}"
CI_REPOSITORY="${PM_V7_CI_REPOSITORY:-ENRICOBIGNOZZI/Polymarket}"
SHA="${PM_V7_MODEL_SHA:-$(cat deploy/london/runtime_sha 2>/dev/null || git rev-parse HEAD)}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact 40-character runtime SHA required" >&2; exit 78; }
DISK_PRESSURE_MIN_FREE_BYTES="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1],encoding="utf-8")); x=v["disk_pressure_min_free_bytes"]; assert type(x) is int and x>0; print(x)' "$LONDON_BUFFER_RETENTION_CONFIG")"
[[ "$DISK_PRESSURE_MIN_FREE_BYTES" =~ ^[1-9][0-9]*$ ]] || { echo "invalid disk pressure threshold" >&2; exit 74; }
DURABLE_ROOT="${PM_V7_DURABLE_ROOT:-$RUN_ROOT/research/collector_buffer}"
RUNTIME_ARTIFACT_ROOT="${PM_V7_RUNTIME_ARTIFACT_ROOT:-$HOME/polymarket-artifacts/current}"
MAKER_FROZEN_MODEL_SOURCE="$RUNTIME_ARTIFACT_ROOT/maker_execution_model.json"
MAKER_RESEARCH_MODEL="$RUN_ROOT/micro_maker/execution_model.json"
RICH_RESEARCH_MODEL="$RUNTIME_ARTIFACT_ROOT/rich_research_model.json"
EXTERNAL_CANCEL_RULE_SHA="$(python3 - "$CRYPTO_EXECUTION_ALPHA_CONFIG" <<PY
import hashlib,json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
rule=value["execution_alpha"]["cancel"]["research_rule"]
assert value.get("paper_only") is True
assert value.get("real_order_submission") is False
assert value["execution_alpha"]["cancel"].get("research_only") is True
raw=json.dumps(rule,sort_keys=True,separators=(",",":")).encode()
print(hashlib.sha256(raw).hexdigest())
PY
)"
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
# London runtime consumes a frozen research-built maker artifact. No runtime fit.
export PM_V7_MODEL_SHA="$SHA"
export PM_V7_MAKER_EXECUTION_MODEL="$MAKER_RESEARCH_MODEL"
CONTROL="$RUN_ROOT/control"
ALLOC="$CONTROL/allocations"
KILL="$CONTROL/KILL"
LOCK="$CONTROL/runtime.lock"
mkdir -p "$CONTROL" "$RUN_ROOT/ledger" "$RUN_ROOT/opportunities/inbox" "$RUN_ROOT/research/evidence" "$RUN_ROOT/reports" "$RUN_ROOT/market_data" "$RUN_ROOT/universe" "$RUN_ROOT/micro_maker" "$RUN_ROOT/external" "$RUN_ROOT/external_fair" "$RUN_ROOT/learned_execution" "$DURABLE_ROOT/micro_maker" "$DURABLE_ROOT/external_fair" "$DURABLE_ROOT/profit_experiments"
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
# capital, ledger, or execution authority.
python3 scripts/v7_external_source_registry.py --registry "$EXTERNAL_SOURCE_REGISTRY" \
  > "$CONTROL/external_source_registry.json"

python3 - "$CONFIG" "$MAKER_POLICY" "$EXTERNAL_FAIR_POLICY" "$LIVE_MODEL_SCOPE" "$CRYPTO_UNIVERSE_CONFIG" "$WS_JSON_ARENA_OBSERVER_MAX_BYTES" "$WS_JSON_ARENA_FILLABILITY_MAX_BYTES" "$WS_JSON_ARENA_TOTAL_BUDGET_BYTES" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
v7=cfg.get("v7") or {}
maker=json.load(open(sys.argv[2]))
external=json.load(open(sys.argv[3]))
live_scope=json.load(open(sys.argv[4]))
crypto_universe=json.load(open(sys.argv[5]))
registry_path=v7.get("strategy_registry")
assert isinstance(registry_path,str) and registry_path
registry=json.load(open(registry_path))
algorithms={"CRYPTO_SETTLEMENT_ENGINE"}
registered=[row.get("id") for row in registry.get("live_algorithms",[])]
assert registry.get("schema")=="polymarket_v7_live_algorithm_registry_v2"
assert set(registered)==algorithms and len(registered)==1
assert registry.get("safety",{}).get("paper_only") is True
assert registry.get("safety",{}).get("authenticated_execution") is False
assert registry.get("safety",{}).get("real_order_submission") is False
assert all(row.get("mode") == "PAPER" and row.get("enabled") is True for row in registry.get("live_algorithms",[]))
assert live_scope.get("schema") == "polymarket_v7_live_engine_scope_v2"
assert live_scope.get("version") == 8 and live_scope.get("live_algorithm_count") == 1
assert live_scope.get("paper_only") is True
assert live_scope.get("authenticated_execution") is False
assert live_scope.get("real_order_submission") is False
assert set(live_scope.get("live_algorithms") or []) == algorithms
invariants=live_scope.get("runtime_invariants") or {}
assert invariants.get("single_execution_owner") is True
assert invariants.get("global_portfolio_coordinator") == "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"
assert invariants.get("paper_only") is True
assert invariants.get("real_order_submission") is False
assert crypto_universe.get("schema") == "polymarket_v7_crypto_universe_config_v1"
assert crypto_universe.get("version") == 1
assert crypto_universe.get("paper_only") is True
assert crypto_universe.get("authenticated_execution") is False
assert crypto_universe.get("real_order_submission") is False
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
assert maker.get("architecture",{}).get("slow_path") == "python_crypto_selection_frozen_model_inference"
capacity=maker.get("market_selection",{}).get("resource_capacity",{})
max_shards=int(capacity.get("shard_count_budget",0))
markets_per_shard=int(capacity.get("markets_per_shard",0))
assert max_shards > 0 and markets_per_shard == 8
assert int(maker.get("market_selection",{}).get("max_active_markets",0)) == max_shards*markets_per_shard
observation_fractions=v7.get("component_observation_budget_fractions") or {}
expected_maker_observation_budget=float(cfg.get("starting_capital",0))*float(observation_fractions.get("professional_maker",0))
assert abs(float(maker.get("market_selection",{}).get("observation_budget_usd",0))-expected_maker_observation_budget) <= 1e-9
assert external.get("paper_only") is True
assert external.get("authenticated_execution") is False
assert external.get("real_order_submission") is False
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
rm -f "$KILL"

python3 scripts/v7_capital_allocator.py --config "$CONFIG" --output-dir "$ALLOC" >/dev/null
# Resolve CPU classes before any child starts. London keeps the decision and
# execution cores separate from collectors/control work whenever the host has
# enough logical CPUs. The plan is preserved as evidence.
while IFS='=' read -r key value; do
  case "$key" in
    PM_V7_HOT_CPUSET|PM_V7_COLLECTOR_CPUSET|PM_V7_CONTROL_CPUSET|PM_V7_HOT_NICE|PM_V7_COLLECTOR_NICE|PM_V7_CONTROL_NICE)
      export "$key=$value" ;;
    *) echo "unexpected resource-plan key: $key" >&2; exit 74 ;;
  esac
done < <(python3 scripts/v7_runtime_resource_plan.py --config "$RUNTIME_RESOURCE_CONFIG"   --output "$CONTROL/runtime_resource_plan.json" --shell)

# Stage immutable research artifacts into the run root. This validates exact
# target SHA, policy/config identity and content hashes; it never trains.
python3 scripts/v7_runtime_artifacts.py   --artifact-root "$RUNTIME_ARTIFACT_ROOT" --run-root "$RUN_ROOT"   --model-sha "$SHA" --policy "$MAKER_POLICY" --allocation "$ALLOC/micro_maker.json"   --receipt "$CONTROL/runtime_artifact_receipt.json"   >> "$RUN_ROOT/runtime_artifacts.log" 2>&1

pids=()
fatal_pids=()

# Startup can fail before the full runtime cleanup function is defined. Every
# child started before that point must still be owned and terminated; otherwise
# launchd retries can inherit an orphan loopback proxy and collide on port 19109.
bootstrap_cleanup_started=0
bootstrap_cleanup() {
  if [[ "$bootstrap_cleanup_started" == 1 ]]; then return 0; fi
  bootstrap_cleanup_started=1
  set +e
  touch "$KILL"
  for pid in "${pids[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  for _ in $(seq 1 50); do
    alive=0
    for pid in "${pids[@]:-}"; do kill -0 "$pid" 2>/dev/null && { alive=1; break; }; done
    [[ "$alive" == 0 ]] && break
    sleep 0.1
  done
  for pid in "${pids[@]:-}"; do
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$LOCK"
}
bootstrap_shutdown() { bootstrap_cleanup; exit 0; }
trap bootstrap_cleanup EXIT
trap bootstrap_shutdown INT TERM

if [[ ! -f scripts/v7_public_https_proxy.py ]]; then
  echo "missing V7 public HTTPS proxy" >&2
  exit 77
fi

# Start the public-DNS HTTPS tunnel before any child that performs public REST
# traffic.  Environment exports are inherited only by subsequently spawned
# children; starting the External Fair PAPER router before this block leaves
# its CLOB `/books` calls on the filtered operating-system resolver and turns
# every economically actionable fair into a silent `NOTHING` decision.
v7_exec_class COLLECTOR python3 scripts/v7_public_https_proxy.py --host 127.0.0.1 --port "$PUBLIC_PROXY_PORT" \
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

# Fair-value ML is a research-plane artifact. London is inference-only; the
# optional rich artifact is validated by the artifact bundle and otherwise the
# existing deterministic baseline remains the only fair-value fallback.
printf '%s\n' '{"state":"INFERENCE_ONLY","runtime_training":false}'   >> "$RUN_ROOT/external_fair/research_model.log"

# Paid Chainlink Data Streams are intentionally out of scope. Public RTDS
# provides the Chainlink 60-second TWAP observability tape. It never replaces
# the contract resolution oracle or bypasses contract-local verification.
v7_exec_class COLLECTOR python3 scripts/v7_rtds_external_fair_monitor.py \
  --output-dir "$RUN_ROOT/external_fair" --code-sha "$SHA" \
  --universe "$RUN_ROOT/universe/current.json" \
  --approvals "config/v7_external_fair_rule_approvals.json" \
  --external-venues "$RUN_ROOT/external_fair/external_venues.json" \
  --research-model "$RICH_RESEARCH_MODEL" \
  --external-fair-config "$ROOT/config/v7_external_fair.json" \
  >> "$RUN_ROOT/external_fair/rtds_monitor.log" 2>&1 &
v7_register_child "$!"

v7_exec_class COLLECTOR python3 scripts/v7_multi_asset_external_collector.py \
  --repository-root "$ROOT" --run-root "$RUN_ROOT" --model-sha "$SHA" \
  --engine "$EXTERNAL_VENUE_RUNTIME" \
  --market-registry "$CRYPTO_SETTLEMENT_MARKET_REGISTRY" \
  --disk-pressure-marker "$RUN_ROOT/control/DISK_PRESSURE" \
  --disk-pressure-min-free-bytes "$DISK_PRESSURE_MIN_FREE_BYTES" \
  --external-cancel-signal "$RUN_ROOT/external_fair/external_cancel_signal.json" \
  --external-cancel-rule-sha256 "$EXTERNAL_CANCEL_RULE_SHA" \
  >> "$RUN_ROOT/external_fair/external_assets_supervisor.log" 2>&1 &
v7_register_child "$!"

# Causal crypto context collectors remain on London because their receive-time
# evidence cannot be reconstructed perfectly after the fact. They run outside
# the hot CPU set and have no execution/capital/ledger authority.


# Continuous receive-time PM book evidence for every currently traded
# crypto asset×horizon context. The universe collector atomically maintains the
# exact 30-market / 60-token zero-authority selection and this observer reloads
# at rollover. Model fitting and retrospective shadows remain off London.
v7_exec_class COLLECTOR "$FILLABILITY_OBSERVER"   --config "$ALLOC/micro_maker.json" --run-root "$RUN_ROOT" --model-sha "$SHA"   --selection "$RUN_ROOT/universe/book_selection.json" --selection-only   --output-dir "$RUN_ROOT/research/repricing_book"   --disk-pressure-min-free-bytes "$DISK_PRESSURE_MIN_FREE_BYTES"   >> "$RUN_ROOT/research/repricing_book_observer.log" 2>&1 &
# Zero-authority PM book evidence is diagnostically important but reconstructible.
# It must not stop the native execution owner if it rolls or exits during market refresh.
v7_register_optional_child "$!"



# PM repricing and two-sided complete-set shadows are reconstructible from the
# causal tapes above, so those computations run only on the research worker.

CONFIG_HASH="$(v7_blob_hash "$CONFIG")"
POLICY_HASH="$(v7_blob_hash "$MAKER_POLICY")"
RUN_ID="${PM_V7_RUN_ID:-${SHA:0:12}-$(date +%s)-$$}"
LEDGER_ID="${PM_V7_LEDGER_ID:-$RUN_ID:execution}"
SERVER_ID="${PM_V7_SERVER_ID:-$(hostname -s 2>/dev/null || hostname)}"

native_engine_ready() {
  python3 - "$CONTROL/native_engine_manager_status.json" "$SHA" <<'PY' >/dev/null 2>&1
import json,os,sys,time
try:
    value=json.load(open(sys.argv[1],encoding="utf-8"))
except (OSError,json.JSONDecodeError):
    raise SystemExit(1)
try:
    pid=int(value.get("engine_pid") or 0)
    age=int(time.time()*1000)-int(value.get("timestamp_ms") or 0)
except (TypeError,ValueError,OverflowError):
    raise SystemExit(1)
ok=(value.get("schema")=="polymarket_v7_native_engine_manager_status_v1"
    and value.get("model_sha")==sys.argv[2]
    and value.get("state")=="RUNNING"
    and value.get("paper_only") is True
    and value.get("authenticated_execution") is False
    and value.get("real_order_submission") is False
    and value.get("real_capital_at_risk") is False
    and value.get("single_native_hot_path") is True
    and not value.get("blocker")
    and pid>0 and 0<=age<=15000)
if ok:
    try: os.kill(pid,0)
    except OSError: ok=False
raise SystemExit(0 if ok else 1)
PY
}

write_runtime_status() {
  local state="$1"
  local killed="${2:-false}"
  local now p0_ready=false readiness="CORE_RUNTIME_ONLY" external_ready=false
  now="$(date +%s)"
  if [[ "$state" == "running" ]] && native_engine_ready; then
    p0_ready=true
    readiness="FULL_PAPER_RUNTIME"
    external_ready=true
  fi
  local model_hash model_source
  if [[ -s "$MAKER_RESEARCH_MODEL" ]]; then
    model_hash="$(v7_blob_hash "$MAKER_RESEARCH_MODEL")"
    model_source="maker_execution_model"
  else
    model_hash="$POLICY_HASH"
    model_source="cold_start_policy"
  fi
  local tmp="$CONTROL/runtime_status.json.tmp.$$"
  printf '{"schema":"polymarket_v7_runtime_status_v3","timestamp":%s,"version":7,"paper_only":true,"authenticated_execution":false,"real_order_submission":false,"real_capital_at_risk":false,"model_sha":"%s","config_hash":"%s","policy_hash":"%s","model_hash":"%s","model_identity_source":"%s","run_id":"%s","ledger_id":"%s","server_id":"%s","pid":%s,"state":"%s","killed":%s,"economic_system":"V7_UNIFIED","economic_engines":["CRYPTO_SETTLEMENT_ENGINE"],"global_portfolio_coordinator":"V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE","execution_authority":"V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE","single_execution_owner":true,"canonical_state_reconciled":true,"exact_sha_ci_green":%s,"p0_authority_configured":["CRYPTO_SETTLEMENT_ENGINE"],"p0_full_stack_ready":%s,"readiness":"%s","external_fair_runtime_ready":%s,"economic_new_risk_ready":false,"economic_decision_state":"SAFE_ACTIONS_ONLY","authorized_alpha_actions":[],"safe_actions":["CANCEL","WITHDRAW","NOTHING"]}\n' \
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
bootstrap_cleanup_started=1

if [[ ! -x "$RECORDER" ]]; then
  echo "missing canonical V7 trade recorder executable: $RECORDER" >&2
  exit 74
fi
if [[ ! -x "$FILLABILITY_OBSERVER" ]]; then
  echo "missing V7 maker exact-WS fillability observer executable: $FILLABILITY_OBSERVER" >&2
  exit 78
fi
if [[ ! -x "$CRYPTO_SETTLEMENT_ENGINE" ]]; then
  echo "missing canonical native V7 crypto settlement engine: $CRYPTO_SETTLEMENT_ENGINE" >&2
  exit 80
fi
# One canonical exhaustive metadata plane. The venue terminates pagination;
# HOT/WARM capacities are calculated from declared CPU/memory/WS budgets and
# COLD preserves the remainder. No strategy owns a parallel universe cache.
v7_exec_class COLLECTOR python3 scripts/v7_crypto_universe.py \
  --config "$CRYPTO_UNIVERSE_CONFIG" --output-dir "$RUN_ROOT/universe" \
  --model-sha "$SHA" --loop \
  >> "$RUN_ROOT/universe/collector.log" 2>&1 &
v7_register_child "$!"
universe_ready=0
for _ in $(seq 1 600); do
  if python3 - "$RUN_ROOT/universe/status.json" "$SHA" <<'PY' >/dev/null 2>&1
import json,sys
value=json.load(open(sys.argv[1]))
ok=(value.get("schema")=="polymarket_v7_crypto_universe_status_v1"
    and value.get("model_sha")==sys.argv[2] and value.get("state")=="OPERATIONAL"
    and value.get("discovery_exhaustive") is True and int(value.get("eligible_markets") or 0)>0
    and value.get("book_selection_state")=="READY"
    and int(value.get("book_selection_contexts") or 0)==30
    and int(value.get("book_selection_tokens") or 0)==60
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
read -r HOT_MARKET_BUDGET ACTIVE_SCAN_MARKET_BUDGET MAKER_FLOW_LOOKBACK_SECONDS MAKER_SELECTOR_REFRESH_SECONDS MAKER_ROTATION_INTERVAL_SECONDS MAKER_CANDIDATE_CONFIRMATIONS MAKER_ROTATION_MIN_FILL MAKER_ROTATION_MIN_ABSOLUTE_IMPROVEMENT MAKER_ROTATION_MIN_RELATIVE_MULTIPLIER < <(python3 - "$RUN_ROOT/universe/status.json" "$MAKER_POLICY" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
maker=json.load(open(sys.argv[2]))
tiers=value.get("tier_counts") or {}
hot=max(1,int(tiers.get("HOT") or 0))
active=max(hot,hot+int(tiers.get("WARM") or 0))
flow=max(180,int(((maker.get("market_selection") or {}).get("recent_flow") or {}).get("lookback_seconds") or 0))
recent=((maker.get("market_selection") or {}).get("recent_flow") or {})
refresh=max(1,int(recent.get("selector_refresh_seconds") or 5))
rotation=max(0,int(recent.get("rotation_min_interval_seconds") or 300))
confirmations=max(2,int(recent.get("candidate_confirmations") or 2))
minimum_fill=max(0.0,min(1.0,float(recent.get("rotation_min_projected_fill_probability") or 0.004)))
absolute_improvement=max(0.0,min(1.0,float(recent.get("rotation_min_absolute_fill_improvement") or 0.05)))
relative_multiplier=max(1.0,float(recent.get("rotation_min_relative_fill_multiplier") or 1.5))
print(hot,active,flow,refresh,rotation,confirmations,
      minimum_fill,absolute_improvement,relative_multiplier)
PY
)




v7_exec_class COLLECTOR "$RECORDER" \
  --run-dir "$RUN_ROOT" \
  --universe "$RUN_ROOT/universe/current.json" --model-sha "$SHA" \
  --data-url "https://data-api.polymarket.com" --batch 40 \
  --lookback-seconds "$MAKER_FLOW_LOOKBACK_SECONDS" --interval 5 --loop \
  >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
v7_register_child "$!"

# Reserve historical PAPER native claims before this run's sole ledger writer.
# Reconciliation of old ledgers runs only in the cutover control plane, where
# source run roots are writable and no current ledger writer is alive.
python3 scripts/v7_legacy_native_claims.py \
  --current-run-root "$RUN_ROOT" --scan-parent "$(dirname "$RUN_ROOT")" \
  --target-sha "$SHA" --output "$RUN_ROOT/control/legacy_native_claims.json" \
  >> "$RUN_ROOT/legacy_native_claims.log" 2>&1

# One persistent canonical ledger router. 100ms transport cadence keeps FILL
# evidence available before the 1s markout horizon without creating a second
# ledger writer or repeatedly spawning Python processes.
v7_exec_class CONTROL python3 scripts/v7_ledger_spool.py \
  --run-root "$RUN_ROOT" --model-sha "$SHA" --loop --interval 0.1 \
  >> "$RUN_ROOT/ledger_router.log" 2>&1 &
v7_register_child "$!"

# Cold-plane lifecycle manager. It discovers every registered active crypto
# context and grants each native PAPER worker a bounded partition of the one
# canonical CRYPTO_SETTLEMENT_ENGINE envelope. The sum of partitions may never
# exceed the allocator-owned engine budget; real submission remains impossible.
PM_V7_CONTROL_NICE=0 v7_exec_class CONTROL python3 scripts/v7_native_crypto_engine_manager.py \
  --repository-root "$ROOT" --run-root "$RUN_ROOT" --model-sha "$SHA" \
  --run-id "$RUN_ID" --server-id "$SERVER_ID" \
  --universe "$RUN_ROOT/universe/current.json" \
  --allocation "$RUN_ROOT/control/allocations/crypto_settlement_engine.json" \
  --market-registry "$ROOT/config/v7_crypto_settlement_markets.json" \
  --legacy-claims "$RUN_ROOT/control/legacy_native_claims.json" \
  --engine "$CRYPTO_SETTLEMENT_ENGINE" \
  --settler "$ROOT/scripts/v7_native_paper_settlement.py" \
  --engine-log "$RUN_ROOT/native_crypto_settlement_engine.log" \
  --target-quantity-microunits 5000000 \
  --maximum-entry-price-e4 7500 \
  --minimum-tte-ns 105000000000 --maximum-tte-ns 120000000000 \
  --maker-share-cap-microunits 1000000 \
  --capture-native-decisions --capture-native-full-context BTC:M5 \
  --asynchronous-settlement \
  >> "$RUN_ROOT/native_engine_manager.log" 2>&1 &
v7_register_child "$!"



# Maker model learning moved to the research plane. The staged immutable model
# above remains fixed for this entire runtime generation.

# Canonical economics is retained as a lightweight operational reconciliation
# surface for health/PnL truth. Forward reports, attribution, fitting, compaction
# and experiment analysis run only on the research plane.
(
  while [[ ! -e "$KILL" ]]; do
    v7_run_class CONTROL python3 scripts/v7_canonical_economics.py \
      --ledger "$RUN_ROOT/ledger/execution.jsonl" --expected-model-sha "$SHA" \
      --markout-evidence "$RUN_ROOT/research/evidence/maker_markout" \
      --output "$RUN_ROOT/canonical_economics.json" \
      >> "$RUN_ROOT/canonical_economics.log" 2>&1 || true
    sleep 60
  done
) & v7_register_child "$!"

v7_assert_registered_child_count 9
write_runtime_status running false

while [[ ! -e "$KILL" ]]; do
  if ! python3 scripts/v7_portfolio_guard.py --run-root "$RUN_ROOT" \
      --allocation-manifest "$ALLOC/manifest.json" --max-drawdown 0.15 \
      > "$RUN_ROOT/portfolio_guard.log" 2>&1; then
    break
  fi
  write_runtime_status running false
  for pid in "${fatal_pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '{"schema":"polymarket_v7_runtime_failure_v1","timestamp":%s,"paper_only":true,"authenticated_execution":false,"model_sha":"%s","dead_pid":%s}\n' "$(date +%s)" "$SHA" "$pid" > "$KILL"
      break
    fi
  done
  sleep 1
done

write_runtime_status killed true
exit 2
