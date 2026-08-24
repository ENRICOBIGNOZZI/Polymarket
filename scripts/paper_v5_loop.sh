#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v5.json}"
RUN_ROOT="${2:-runs/paper_v5_live}"
MIN_LIQUIDITY="${V5_MIN_LIQUIDITY:-25}"
MODEL_MARKETS="${V5_MODEL_MARKETS:-500}"
RECORDER_MARKETS="${V5_RECORDER_MARKETS:-1000}"
RECORDER_BATCH="${V5_RECORDER_BATCH:-40}"
RECORDER_LOOKBACK_SECONDS="${V5_RECORDER_LOOKBACK_SECONDS:-600}"
MODEL_MARKET_ARGS=(--markets "$MODEL_MARKETS")
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/strategies"

COMPACTION_INTERVAL_SECONDS="$(python3 - "$CONFIG" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
value = config.get("multi_strategy", {}).get("log_retention", {}).get("compaction_interval_seconds", 60)
value = int(value)
if value <= 0:
    raise SystemExit("compaction_interval_seconds must be positive")
print(value)
PY
)"

python3 scripts/multi_strategy_paper.py \
  --config "$CONFIG" --run-root "$RUN_ROOT" --engine ./build/polymarket_engine \
  "${MODEL_MARKET_ARGS[@]}" --min-liquidity "$MIN_LIQUIDITY" --validate-only \
  > "$RUN_ROOT/allocator_validate.log"

# Compatibility surface for existing terminal diagnostics. V5 aggregate PnL is
# read from runtime_status.json; this link exposes the graph specialist only to
# legacy detailed panels and action-report code.
if [[ ! -e "$RUN_ROOT/terminal" && ! -L "$RUN_ROOT/terminal" ]]; then
  ln -s "strategies/graph" "$RUN_ROOT/terminal"
fi

filter_b2() {
  python3 scripts/filter_coherent_hedges.py \
    --input "$RUN_ROOT/stat_arb_pca_raw.csv" \
    --output "$RUN_ROOT/stat_arb_pca.csv" \
    --rejections "$RUN_ROOT/stat_arb_pca_rejected.csv" \
    --cache "$RUN_ROOT/market_metadata_cache.json" \
    --allow-latent-factor \
    --max-latent-hedge-error 0.85 \
    --min-latent-stability 0.20 \
    --min-latent-z 0.65 \
    --require-positive-maker-edge \
    >> "$RUN_ROOT/coherent_hedges.log" 2>&1
}

rebuild_intents() {
  rm -f "$RUN_ROOT/b1_intents.csv" "$RUN_ROOT/b2_intents.csv" "$RUN_ROOT/intents.csv"
  python3 scripts/build_v4_intents.py \
    --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" \
    --output "$RUN_ROOT/b1_intents.csv" --config "$CONFIG" --min-edge 0.00025 \
    >> "$RUN_ROOT/intent_build.log" 2>&1
  python3 scripts/build_v4_intents.py \
    --strategy B2 --input "$RUN_ROOT/stat_arb_pca.csv" \
    --output "$RUN_ROOT/b2_intents.csv" --config "$CONFIG" --min-edge 0.00025 \
    >> "$RUN_ROOT/intent_build.log" 2>&1
  python3 scripts/merge_v4_intents.py \
    --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
    --output "$RUN_ROOT/intents.csv" --min-edge 0.00025 \
    --max-age-seconds 900 --max-bundles 50 \
    >> "$RUN_ROOT/intent_merge.log" 2>&1
}

# Restart is fail-closed: stale scanner output must never become a fresh intent.
rm -f \
  "$RUN_ROOT/stat_arb_pairs.csv" \
  "$RUN_ROOT/stat_arb_pca_raw.csv" \
  "$RUN_ROOT/stat_arb_pca.csv" \
  "$RUN_ROOT/stat_arb_pca_rejected.csv" \
  "$RUN_ROOT/b1_intents.csv" \
  "$RUN_ROOT/b2_intents.csv" \
  "$RUN_ROOT/intents.csv"
filter_b2 || true
rebuild_intents || true

supervise_execution() {
  local rec_pid=0
  local broker_pid=0
  local allocator_pid=0
  local rec_restarts=0
  local broker_restarts=0
  local allocator_restarts=0

  start_recorder() {
    ./build/polymarket_trade_recorder \
      --config "$CONFIG" --run-dir "$RUN_ROOT" \
      --markets "$RECORDER_MARKETS" --batch "$RECORDER_BATCH" --min-liquidity "$MIN_LIQUIDITY" \
      --lookback-seconds "$RECORDER_LOOKBACK_SECONDS" \
      --interval 5 --loop >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
    rec_pid=$!
  }

  start_broker() {
    ./build/polymarket_multileg_paper \
      --config "$CONFIG" --run-dir "$RUN_ROOT" \
      --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" \
      --min-edge 0.00025 --completion-threshold 0.85 \
      --submit-latency-ms 150 --cancel-latency-ms 150 --max-replaces 5 \
      --max-leg-risk-usd 10 --adverse-horizon-seconds 90 --interval 2 --loop \
      >> "$RUN_ROOT/multileg.log" 2>&1 &
    broker_pid=$!
  }

  start_allocator() {
    python3 scripts/multi_strategy_paper.py \
      --config "$CONFIG" --run-root "$RUN_ROOT" --engine ./build/polymarket_engine \
      "${MODEL_MARKET_ARGS[@]}" --min-liquidity "$MIN_LIQUIDITY" \
      >> "$RUN_ROOT/allocator.log" 2>&1 &
    allocator_pid=$!
  }

  append_event() {
    local component="$1"
    local event="$2"
    local count="$3"
    local path="$RUN_ROOT/runtime_supervisor_events.csv"
    if [[ ! -s "$path" ]]; then
      printf 'timestamp,component,event,restart_count\n' > "$path"
    fi
    printf '%s,%s,%s,%s\n' "$(date +%s)" "$component" "$event" "$count" >> "$path"
  }

  write_status() {
    local rec_alive=0
    local broker_alive=0
    local allocator_alive=0
    if (( rec_pid > 0 )) && kill -0 "$rec_pid" 2>/dev/null; then rec_alive=1; fi
    if (( broker_pid > 0 )) && kill -0 "$broker_pid" 2>/dev/null; then broker_alive=1; fi
    if (( allocator_pid > 0 )) && kill -0 "$allocator_pid" 2>/dev/null; then allocator_alive=1; fi
    local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp"
    printf 'timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n' > "$tmp"
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$(date +%s)" "$rec_alive" "$broker_alive" "$allocator_alive" \
      "$rec_restarts" "$broker_restarts" "$allocator_restarts" \
      "$rec_pid" "$broker_pid" "$allocator_pid" >> "$tmp"
    mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
  }

  child_cleanup() {
    if (( rec_pid > 0 )); then kill "$rec_pid" 2>/dev/null || true; fi
    if (( broker_pid > 0 )); then kill "$broker_pid" 2>/dev/null || true; fi
    if (( allocator_pid > 0 )); then kill "$allocator_pid" 2>/dev/null || true; fi
    if (( rec_pid > 0 )); then wait "$rec_pid" 2>/dev/null || true; fi
    if (( broker_pid > 0 )); then wait "$broker_pid" 2>/dev/null || true; fi
    if (( allocator_pid > 0 )); then wait "$allocator_pid" 2>/dev/null || true; fi
  }

  supervisor_shutdown() {
    trap - EXIT INT TERM
    child_cleanup
    exit 0
  }

  trap child_cleanup EXIT
  trap supervisor_shutdown INT TERM

  start_recorder
  start_broker
  start_allocator
  append_event recorder start 0
  append_event broker start 0
  append_event allocator start 0
  write_status

  while true; do
    if ! kill -0 "$rec_pid" 2>/dev/null; then
      wait "$rec_pid" 2>/dev/null || true
      rec_restarts=$((rec_restarts + 1))
      append_event recorder restart "$rec_restarts"
      sleep 1
      start_recorder
    fi
    if ! kill -0 "$broker_pid" 2>/dev/null; then
      wait "$broker_pid" 2>/dev/null || true
      broker_restarts=$((broker_restarts + 1))
      append_event broker restart "$broker_restarts"
      sleep 1
      start_broker
    fi
    if ! kill -0 "$allocator_pid" 2>/dev/null; then
      wait "$allocator_pid" 2>/dev/null || true
      allocator_restarts=$((allocator_restarts + 1))
      append_event allocator restart "$allocator_restarts"
      sleep 1
      start_allocator
    fi
    write_status
    sleep 3
  done
}

SUPERVISOR_PID=0
SUPERVISOR_RESTARTS=0

start_supervisor() {
  supervise_execution >> "$RUN_ROOT/runtime_supervisor.log" 2>&1 &
  SUPERVISOR_PID=$!
}

cleanup() {
  if (( SUPERVISOR_PID > 0 )); then kill "$SUPERVISOR_PID" 2>/dev/null || true; fi
  if (( SUPERVISOR_PID > 0 )); then wait "$SUPERVISOR_PID" 2>/dev/null || true; fi
}

parent_shutdown() {
  trap - EXIT INT TERM
  cleanup
  exit 0
}

trap cleanup EXIT
trap parent_shutdown INT TERM
start_supervisor

last_stat=0
last_structural=0
last_rewards=0
last_oos=0
last_report=0
last_compaction=0
last_activity=0

while true; do
  now=$(date +%s)

  if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    wait "$SUPERVISOR_PID" 2>/dev/null || true
    SUPERVISOR_RESTARTS=$((SUPERVISOR_RESTARTS + 1))
    printf '%s,supervisor,restart,%s\n' "$(date +%s)" "$SUPERVISOR_RESTARTS" >> "$RUN_ROOT/runtime_supervisor_events.csv"
    sleep 1
    start_supervisor
  fi

  ./build/polymarket_maker_paper \
    --config "$CONFIG" --run-dir "$RUN_ROOT/maker" --markets "$MODEL_MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
    --min-edge 0.00025 --max-order-usd 35 --ttl-seconds 180 --hold-seconds 300 \
    --adverse-selection-mult 0.15 --once >> "$RUN_ROOT/maker.log" 2>&1 || true

  if (( now - last_stat >= 120 )); then
    rm -f "$RUN_ROOT/stat_arb_pairs.csv" "$RUN_ROOT/stat_arb_pca_raw.csv"
    ./build/polymarket_stat_arb \
      --config "$CONFIG" --markets "$MODEL_MARKETS" --history-universe 320 \
      --lookback-hours 336 --fidelity-minutes 15 --min-z 0.75 \
      --min-t-reversion 0.50 --max-half-life-hours 336 --top 150 --csv "$RUN_ROOT/stat_arb_pairs.csv" \
      > "$RUN_ROOT/stat_arb_pairs_latest.log" 2> "$RUN_ROOT/stat_arb_pairs_errors.log" || true
    ./build/polymarket_pca_stat_arb \
      --config "$CONFIG" --markets "$MODEL_MARKETS" --universe 300 \
      --lookback-hours 336 --fidelity-minutes 15 --factors 4 --max-hedges 8 \
      --min-z 0.65 --min-t-reversion 0.50 --max-half-life-hours 336 \
      --max-factor-hedge-error 0.85 --top 150 --csv "$RUN_ROOT/stat_arb_pca_raw.csv" \
      > "$RUN_ROOT/stat_arb_pca_latest.log" 2> "$RUN_ROOT/stat_arb_pca_errors.log" || true
    filter_b2 || true
    rebuild_intents || true
    last_stat=$now
  fi

  if (( now - last_structural >= 120 )); then
    ./build/polymarket_negrisk_arb \
      --config "$CONFIG" --markets "$MODEL_MARKETS" --min-liquidity "$MIN_LIQUIDITY" --top 150 \
      2> "$RUN_ROOT/structural_errors.log" \
      | tee "$RUN_ROOT/structural_latest.log" "$RUN_ROOT/structural_latest.csv" >/dev/null || true
    last_structural=$now
  fi

  # Rewards remain diagnostic only and are not booked into V5 aggregate PnL.
  if (( now - last_rewards >= 300 )); then
    rm -f "$RUN_ROOT/reward_opportunities.csv"
    ./build/polymarket_rewards_scan \
      --config "$CONFIG" --markets 2000 --top 120 \
      --quote-shares 50 --max-notional 100 --improve-ticks 0 \
      --competition-multiplier 2.0 --reward-haircut 0.25 \
      --native-reward-unit-usd 1.0 --annual-capital-rate 0.20 \
      --adverse-bps 50 --one-sided-fills-per-day 1.0 \
      --csv "$RUN_ROOT/reward_opportunities.csv" \
      > "$RUN_ROOT/reward_latest.log" 2> "$RUN_ROOT/reward_errors.log" || true
    if [[ -s "$RUN_ROOT/reward_opportunities.csv" ]]; then
      python3 scripts/apply_reward_payout_floor.py \
        --csv "$RUN_ROOT/reward_opportunities.csv" --minimum-daily-payout-usd 1.0 \
        >> "$RUN_ROOT/reward_latest.log" 2>> "$RUN_ROOT/reward_errors.log" || true
    fi
    last_rewards=$now
  fi

  if (( now - last_oos >= 3600 )); then
    python3 scripts/walk_forward_v4.py \
      --ledger "$RUN_ROOT/bundle_ledger.csv" --output "$RUN_ROOT/walk_forward.json" \
      --starting-capital 10000 >> "$RUN_ROOT/walk_forward.log" 2>&1 || true
    last_oos=$now
  fi

  if (( now - last_report >= 120 )); then
    python3 scripts/runtime_action_report.py \
      --run-root "$RUN_ROOT" --external-signals data/external_signals.csv \
      --window-seconds 3600 --production-edge 0.00025 \
      --output-json "$RUN_ROOT/action_report.json" \
      --output-markdown "$RUN_ROOT/action_report.md" \
      > "$RUN_ROOT/action_report_latest.log" 2> "$RUN_ROOT/action_report_errors.log" || true
    last_report=$now
  fi

  if (( now - last_activity >= 60 )); then
    python3 scripts/aggressive_activity_guard.py \
      --run-root "$RUN_ROOT" --expected-models 5 --minimum-markets 100 \
      --minimum-tradable-fraction 0.10 --max-model-staleness-seconds 180 \
      --output "$RUN_ROOT/activity_status.json" \
      >> "$RUN_ROOT/activity_guard.log" 2>&1 || true
    last_activity=$now
  fi

  if (( now - last_compaction >= COMPACTION_INTERVAL_SECONDS )); then
    python3 scripts/compact_strategy_logs.py \
      --config "$CONFIG" --run-root "$RUN_ROOT" \
      >> "$RUN_ROOT/log_compaction.log" 2>&1 || true
    last_compaction=$now
  fi

  sleep 5
done
