#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v4.json}"
RUN_ROOT="${2:-runs/paper_v4_live}"
ALPHA_CONFIG="${ALPHA_CONFIG:-config/alpha_research.json}"
RECORDER_MARKETS="${V4_RECORDER_MARKETS:-600}"
RECORDER_BATCH="${V4_RECORDER_BATCH:-20}"
RECORDER_LOOKBACK_SECONDS="${V4_RECORDER_LOOKBACK_SECONDS:-300}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/terminal"
eval "$(python3 scripts/alpha_config_env.py --config "$ALPHA_CONFIG")"

python3 scripts/build_v4_intents.py --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" --output "$RUN_ROOT/b1_intents.csv" --config "$CONFIG" --min-edge "$B1_EXECUTION_MIN_EDGE" >/dev/null
python3 scripts/build_v4_intents.py --strategy B2 --input "$RUN_ROOT/stat_arb_pca.csv" --output "$RUN_ROOT/b2_intents.csv" --config "$CONFIG" --min-edge "$B2_EXECUTION_MIN_EDGE" >/dev/null
python3 scripts/merge_v4_intents.py \
  --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
  --output "$RUN_ROOT/intents.csv" --min-edge "$ALPHA_MERGE_MIN_EDGE" --max-bundles 20 >/dev/null

supervise_execution() {
  local rec_pid=0
  local broker_pid=0
  local rec_restarts=0
  local broker_restarts=0

  start_recorder() {
    ./build/polymarket_trade_recorder \
      --config "$CONFIG" --run-dir "$RUN_ROOT" \
      --markets "$RECORDER_MARKETS" --batch "$RECORDER_BATCH" --min-liquidity 100 \
      --lookback-seconds "$RECORDER_LOOKBACK_SECONDS" \
      --interval 10 --loop >> "$RUN_ROOT/trade_recorder.log" 2>&1 &
    rec_pid=$!
  }

  start_broker() {
    ./build/polymarket_multileg_paper \
      --config "$CONFIG" --run-dir "$RUN_ROOT" \
      --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" \
      --min-edge "$ALPHA_MERGE_MIN_EDGE" --completion-threshold 0.95 \
      --submit-latency-ms 250 --cancel-latency-ms 250 --max-replaces 3 \
      --max-leg-risk-usd 5 --adverse-horizon-seconds 60 --interval 3 --loop \
      >> "$RUN_ROOT/multileg.log" 2>&1 &
    broker_pid=$!
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
    if (( rec_pid > 0 )) && kill -0 "$rec_pid" 2>/dev/null; then rec_alive=1; fi
    if (( broker_pid > 0 )) && kill -0 "$broker_pid" 2>/dev/null; then broker_alive=1; fi
    local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp"
    printf 'timestamp,recorder_alive,broker_alive,recorder_restarts,broker_restarts,recorder_pid,broker_pid\n' > "$tmp"
    printf '%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$rec_alive" "$broker_alive" "$rec_restarts" "$broker_restarts" "$rec_pid" "$broker_pid" >> "$tmp"
    mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
  }

  child_cleanup() {
    if (( rec_pid > 0 )); then kill "$rec_pid" 2>/dev/null || true; fi
    if (( broker_pid > 0 )); then kill "$broker_pid" 2>/dev/null || true; fi
    if (( rec_pid > 0 )); then wait "$rec_pid" 2>/dev/null || true; fi
    if (( broker_pid > 0 )); then wait "$broker_pid" 2>/dev/null || true; fi
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
  append_event recorder start 0
  append_event broker start 0
  write_status

  while true; do
    if ! kill -0 "$rec_pid" 2>/dev/null; then
      wait "$rec_pid" 2>/dev/null || true
      rec_restarts=$((rec_restarts + 1))
      append_event recorder restart "$rec_restarts"
      sleep 2
      start_recorder
    fi
    if ! kill -0 "$broker_pid" 2>/dev/null; then
      wait "$broker_pid" 2>/dev/null || true
      broker_restarts=$((broker_restarts + 1))
      append_event broker restart "$broker_restarts"
      sleep 2
      start_broker
    fi
    write_status
    sleep 5
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
last_terminal=0
last_oos=0

while true; do
  now=$(date +%s)

  if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    wait "$SUPERVISOR_PID" 2>/dev/null || true
    SUPERVISOR_RESTARTS=$((SUPERVISOR_RESTARTS + 1))
    printf '%s,supervisor,restart,%s\n' "$(date +%s)" "$SUPERVISOR_RESTARTS" >> "$RUN_ROOT/runtime_supervisor_events.csv"
    sleep 2
    start_supervisor
  fi

  ./build/polymarket_maker_paper \
    --config "$CONFIG" --run-dir "$RUN_ROOT/maker" --markets 240 --min-liquidity 100 \
    --min-edge 0.003 --max-order-usd 50 --ttl-seconds 300 --hold-seconds 180 \
    --adverse-selection-mult 0.50 --once >> "$RUN_ROOT/maker.log" 2>&1 || true

  if (( now - last_stat >= 900 )); then
    # Reload the versioned champion before each refit so a newly validated
    # promotion takes effect without a hidden hard-coded parameter fork.
    eval "$(python3 scripts/alpha_config_env.py --config "$ALPHA_CONFIG")"
    ./build/polymarket_stat_arb \
      --config "$CONFIG" --markets "$B1_MARKETS" --history-universe "$B1_HISTORY_UNIVERSE" \
      --lookback-hours "$B1_LOOKBACK_HOURS" --fidelity-minutes "$B1_FIDELITY_MINUTES" \
      --min-z "$B1_MIN_Z" --max-half-life-hours "$B1_MAX_HALF_LIFE_HOURS" \
      --min-t-reversion "$B1_MIN_T_REVERSION" --top "$B1_TOP" \
      --csv "$RUN_ROOT/stat_arb_pairs.csv" \
      > "$RUN_ROOT/stat_arb_pairs_latest.log" 2> "$RUN_ROOT/stat_arb_pairs_errors.log" || true
    ./build/polymarket_pca_stat_arb \
      --config "$CONFIG" --markets "$B2_MARKETS" --universe "$B2_UNIVERSE" \
      --lookback-hours "$B2_LOOKBACK_HOURS" --fidelity-minutes "$B2_FIDELITY_MINUTES" \
      --factors "$B2_FACTORS" --max-hedges "$B2_MAX_HEDGES" --min-z "$B2_MIN_Z" \
      --max-half-life-hours "$B2_MAX_HALF_LIFE_HOURS" --min-t-reversion "$B2_MIN_T_REVERSION" \
      --max-factor-hedge-error "$B2_MAX_FACTOR_HEDGE_ERROR" --top "$B2_TOP" \
      --csv "$RUN_ROOT/stat_arb_pca.csv" \
      > "$RUN_ROOT/stat_arb_pca_latest.log" 2> "$RUN_ROOT/stat_arb_pca_errors.log" || true
    python3 scripts/build_v4_intents.py \
      --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" --output "$RUN_ROOT/b1_intents.csv" \
      --config "$CONFIG" --min-edge "$B1_EXECUTION_MIN_EDGE" >> "$RUN_ROOT/intent_build.log" 2>&1 || true
    python3 scripts/build_v4_intents.py \
      --strategy B2 --input "$RUN_ROOT/stat_arb_pca.csv" --output "$RUN_ROOT/b2_intents.csv" \
      --config "$CONFIG" --min-edge "$B2_EXECUTION_MIN_EDGE" >> "$RUN_ROOT/intent_build.log" 2>&1 || true
    python3 scripts/merge_v4_intents.py \
      --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
      --output "$RUN_ROOT/intents.csv" --min-edge "$ALPHA_MERGE_MIN_EDGE" --max-age-seconds 600 --max-bundles 20 \
      >> "$RUN_ROOT/intent_merge.log" 2>&1 || true
    last_stat=$now
  fi

  if (( now - last_structural >= 300 )); then
    ./build/polymarket_negrisk_arb --config "$CONFIG" --markets 600 --min-liquidity 100 --top 60 \
      2> "$RUN_ROOT/structural_errors.log" \
      | tee "$RUN_ROOT/structural_latest.log" "$RUN_ROOT/structural_latest.csv" >/dev/null || true
    last_structural=$now
  fi

  # B3 is diagnostic only. Rewards are not booked as paper PnL and no intent is
  # sent to the broker until estimated shares are validated against real earnings.
  if (( now - last_rewards >= 300 )); then
    ./build/polymarket_rewards_scan \
      --config "$CONFIG" --markets 2000 --top 80 \
      --quote-shares 50 --max-notional 100 --improve-ticks 0 \
      --competition-multiplier 2.0 --reward-haircut 0.25 --native-reward-unit-usd 1.0 \
      --annual-capital-rate 0.20 --adverse-bps 50 --one-sided-fills-per-day 1.0 \
      --csv "$RUN_ROOT/reward_opportunities.csv" \
      > "$RUN_ROOT/reward_latest.log" 2> "$RUN_ROOT/reward_errors.log" || true
    if [[ -s "$RUN_ROOT/reward_opportunities.csv" ]]; then
      python3 scripts/apply_reward_payout_floor.py \
        --csv "$RUN_ROOT/reward_opportunities.csv" --minimum-daily-payout-usd 1.0 \
        >> "$RUN_ROOT/reward_latest.log" 2>> "$RUN_ROOT/reward_errors.log" || true
    fi
    last_rewards=$now
  fi

  if (( now - last_terminal >= 300 )); then
    ./build/polymarket_engine --config "$CONFIG" --once --scan-only --markets 240 --min-liquidity 100 \
      --run-dir "$RUN_ROOT/terminal" > "$RUN_ROOT/terminal_latest.log" 2> "$RUN_ROOT/terminal_errors.log" || true
    last_terminal=$now
  fi

  if (( now - last_oos >= 3600 )); then
    python3 scripts/walk_forward_v4.py --ledger "$RUN_ROOT/bundle_ledger.csv" \
      --output "$RUN_ROOT/walk_forward.json" --starting-capital 10000 \
      >> "$RUN_ROOT/walk_forward.log" 2>&1 || true
    last_oos=$now
  fi

  sleep 10
done
