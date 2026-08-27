#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CONFIG="${PM_V7_CONFIG:-config/paper_v7.json}"
RUN_ROOT="${PM_V7_RUN_ROOT:-runs/paper_v7_live}"
RECORDER="${PM_TRADE_RECORDER:-build/polymarket_trade_recorder}"
REWARDS_SCAN="${PM_REWARDS_SCAN:-build/polymarket_rewards_scan}"
MAKER_POLICY="${PM_V7_MAKER_POLICY:-config/v7_professional_market_maker.json}"
SHA="$(git rev-parse HEAD)"
CONTROL="$RUN_ROOT/control"
ALLOC="$CONTROL/allocations"
KILL="$CONTROL/KILL"
LOCK="$CONTROL/runtime.lock"
mkdir -p "$CONTROL" "$RUN_ROOT/market_data" "$RUN_ROOT/graph_rv" "$RUN_ROOT/hard_arb" "$RUN_ROOT/micro_taker" "$RUN_ROOT/micro_maker" "$RUN_ROOT/external" "$RUN_ROOT/learned_execution"

python3 - "$CONFIG" "$MAKER_POLICY" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
v7=cfg.get("v7") or {}
maker=json.load(open(sys.argv[2]))
assert cfg.get("engine_version")==7
assert cfg.get("paper_only") is True
assert v7.get("paper_only") is True
assert v7.get("authenticated_execution") is False
assert v7.get("real_order_submission") is False
assert float(cfg.get("max_drawdown",0)) <= .15 + 1e-12
assert maker.get("paper_only") is True
assert maker.get("authenticated_execution") is False
assert maker.get("real_order_submission") is False
assert maker.get("architecture",{}).get("single_canonical_ledger_writer") is True
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

cat > "$CONTROL/runtime_status.json.tmp" <<JSON
{"schema":"polymarket_v7_runtime_status_v1","paper_only":true,"authenticated_execution":false,"model_sha":"$SHA","pid":$$,"starting":true,"primary_economic_sleeve":"MICRO_MAKER_PRO"}
JSON
mv "$CONTROL/runtime_status.json.tmp" "$CONTROL/runtime_status.json"

pids=()
cleanup() {
  set +e
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${pids[@]:-}"; do wait "$pid" 2>/dev/null || true; done
  rm -rf "$LOCK"
}
trap cleanup EXIT INT TERM

if [[ ! -x "$RECORDER" ]]; then
  echo "missing same-checkout trade recorder executable: $RECORDER" >&2
  exit 74
fi

"$RECORDER" \
  --config "$CONFIG" \
  --run-dir "$RUN_ROOT" \
  --data-url "https://data-api.polymarket.com" \
  --markets 1000 --batch 40 --min-liquidity 2 \
  --lookback-seconds 180 --interval 5 --loop \
  >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_ledger_spool.py --run-root "$RUN_ROOT" --model-sha "$SHA" >> "$RUN_ROOT/ledger_router.log" 2>&1 || true
    sleep 1
  done
) & pids+=("$!")

# Professional maker slow path: public reward-market selection and conservative
# competition/reward context.  This work is deliberately outside the quote hot path.
(
  while [[ ! -e "$KILL" ]]; do
    python3 scripts/v7_market_maker_rewards.py \
      --config "$MAKER_POLICY" \
      --output "$RUN_ROOT/micro_maker/reward_selection.json" \
      >> "$RUN_ROOT/micro_maker/reward_selection.log" 2>&1 || true
    if [[ -x "$REWARDS_SCAN" ]]; then
      "$REWARDS_SCAN" \
        --config "$ALLOC/micro_maker.json" \
        --markets 1000 --top 40 \
        --csv "$RUN_ROOT/micro_maker/reward_scan.csv" \
        --quote-shares 10 --max-notional 100 \
        --competition-multiplier 2 --reward-haircut 0.50 \
        --annual-capital-rate 0.05 --adverse-bps 50 \
        --one-sided-fills-per-day 1 --improve-ticks 0 \
        >> "$RUN_ROOT/micro_maker/reward_scan.log" 2>&1 || true
    fi
    sleep 60
  done
) & pids+=("$!")

# Action-specific causal fill/markout model. Exploration records are allowed as
# training data but retain promotion_credit=false in the canonical ledger.
(
  while [[ ! -e "$KILL" ]]; do
    if [[ -s "$RUN_ROOT/ledger/execution.jsonl" ]]; then
      python3 scripts/v7_market_maker_model.py \
        --ledger "$RUN_ROOT/ledger/execution.jsonl" \
        --model-sha "$SHA" \
        --output "$RUN_ROOT/micro_maker/execution_model.json" \
        >> "$RUN_ROOT/micro_maker/model.log" 2>&1 || true
    fi
    sleep 60
  done
) & pids+=("$!")

# Immediate PAPER/bootstrap maker. It uses public point-in-time books plus the
# canonical recorder tape, models FIFO queue/cancel latency, manages YES/NO
# inventory and emits only through the V7 ledger spool. The production latency
# target remains migration of this decision contract onto the shared C++ WS state.
python3 scripts/v7_market_maker_worker.py \
  --config "$ALLOC/micro_maker.json" \
  --maker-policy "$MAKER_POLICY" \
  --run-root "$RUN_ROOT" \
  --selection "$RUN_ROOT/micro_maker/reward_selection.json" \
  --reward-scan "$RUN_ROOT/micro_maker/reward_scan.csv" \
  --model "$RUN_ROOT/micro_maker/execution_model.json" \
  --trade-tape "$RUN_ROOT/trade_tape.csv" \
  --loop --interval-ms 500 \
  >> "$RUN_ROOT/micro_maker/runtime.log" 2>&1 &
pids+=("$!")

# Full-depth executable mark for account-level risk. Unmarkable maker inventory
# is a hard fail-closed condition rather than being carried at midpoint/cost.
(
  while [[ ! -e "$KILL" ]]; do
    if ! python3 scripts/v7_market_maker_status.py \
      --state "$RUN_ROOT/micro_maker/state.json" \
      --config "$ALLOC/micro_maker.json" \
      --output "$RUN_ROOT/micro_maker/status.json" \
      >> "$RUN_ROOT/micro_maker/status.log" 2>&1; then
      printf '{"schema":"polymarket_v7_maker_risk_failure_v1","paper_only":true,"model_sha":"%s"}\n' "$SHA" > "$KILL"
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
    joint_args=()
    [[ -s "$RUN_ROOT/learned_execution/joint_policy.json" ]] && joint_args=(--joint-model "$RUN_ROOT/learned_execution/joint_policy.json")
    python3 scripts/v7_graph_rv.py \
      --config "$ALLOC/graph_rv.json" \
      --run-root "$RUN_ROOT" \
      --intents "$RUN_ROOT/graph_rv/intents.csv" \
      --trade-tape "$RUN_ROOT/trade_tape.csv" \
      "${joint_args[@]}" \
      >> "$RUN_ROOT/graph_rv/execution.log" 2>&1 || true
    sleep 1
  done
) & pids+=("$!")

(
  while [[ ! -e "$KILL" ]]; do
    if [[ -s "$RUN_ROOT/ledger/execution.jsonl" ]]; then
      python3 scripts/v7_graph_cost_vector.py --run-root "$RUN_ROOT" --model-sha "$SHA" --slippage-bps 5 \
        >> "$RUN_ROOT/graph_rv/cost_vector.log" 2>&1 || true
      python3 scripts/v7_joint_execution_policy.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
        --output "$RUN_ROOT/learned_execution/joint_policy.json" --strategy GRAPH_RV --min-bundles 20 \
        >> "$RUN_ROOT/learned_execution/joint_policy.log" 2>&1 || true
      python3 scripts/v7_learned_execution_model.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
        --output "$RUN_ROOT/learned_execution/oos_report.json" \
        >> "$RUN_ROOT/learned_execution/model.log" 2>&1 || true
    fi
    sleep 60
  done
) & pids+=("$!")

# Native Hard Arb and Micro Taker remain isolated secondary capital sleeves. Their
# CSV/state output stays research-only until strict canonical-ledger producers
# preserve their full causal execution identity.
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

# Account-level kill switch. Any unsafe/unmarkable sleeve or >=15% account DD
# writes control/KILL; the parent then terminates the entire process group.
while [[ ! -e "$KILL" ]]; do
  if ! python3 scripts/v7_portfolio_guard.py --run-root "$RUN_ROOT" \
      --allocation-manifest "$ALLOC/manifest.json" --max-drawdown 0.15 \
      > "$RUN_ROOT/portfolio_guard.log" 2>&1; then
    break
  fi
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '{"schema":"polymarket_v7_runtime_failure_v1","paper_only":true,"model_sha":"%s","dead_pid":%s}\n' "$SHA" "$pid" > "$KILL"
      break
    fi
  done
  sleep 1
done

printf '{"schema":"polymarket_v7_runtime_status_v1","paper_only":true,"authenticated_execution":false,"model_sha":"%s","pid":%s,"killed":true,"primary_economic_sleeve":"MICRO_MAKER_PRO"}\n' "$SHA" "$$" > "$CONTROL/runtime_status.json"
exit 2
