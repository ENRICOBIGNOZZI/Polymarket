#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v5.json}"
RUN_ROOT="${2:-runs/paper_v5_live}"
MIN_LIQUIDITY="${V5_MIN_LIQUIDITY:-25}"
MODEL_MARKETS="${V5_MODEL_MARKETS:-500}"
RECORDER_MARKETS="${V5_RECORDER_MARKETS:-1000}"
RECORDER_BATCH="${V5_RECORDER_BATCH:-40}"
RECORDER_LOOKBACK_SECONDS="${V5_RECORDER_LOOKBACK_SECONDS:-900}"
STAT_INTERVAL_SECONDS="${V5_STAT_INTERVAL_SECONDS:-120}"
STRUCTURAL_INTERVAL_SECONDS="${V5_STRUCTURAL_INTERVAL_SECONDS:-60}"
REWARD_INTERVAL_SECONDS="${V5_REWARD_INTERVAL_SECONDS:-300}"
REPORT_INTERVAL_SECONDS="${V5_REPORT_INTERVAL_SECONDS:-60}"
ACTIVITY_INTERVAL_SECONDS="${V5_ACTIVITY_INTERVAL_SECONDS:-60}"
EXTERNAL_SYNC_INTERVAL_SECONDS="${V5_EXTERNAL_SYNC_INTERVAL_SECONDS:-300}"
INTENT_MIN_EDGE="${V5_INTENT_MIN_EDGE:-0.00025}"
EXTERNAL_TELEMETRY_PATH="telemetry/latest-external-signals.jsonl"
EXTERNAL_LOCAL_JSONL="$RUN_ROOT/latest_external_signals.jsonl"
EXTERNAL_OUTPUT_CSV="data/external_signals.csv"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/strategies" data

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

sync_external_once() {
  local temporary="$EXTERNAL_LOCAL_JSONL.tmp"
  if GIT_TERMINAL_PROMPT=0 git fetch -q --no-tags origin telemetry:refs/remotes/origin/telemetry \
    && git show "origin/telemetry:$EXTERNAL_TELEMETRY_PATH" > "$temporary" 2>/dev/null; then
    mv "$temporary" "$EXTERNAL_LOCAL_JSONL"
  else
    rm -f "$temporary"
  fi

  python3 scripts/sync_external_signals.py \
    --input "$EXTERNAL_LOCAL_JSONL" --output "$EXTERNAL_OUTPUT_CSV" \
    --max-age-seconds 21600 --max-source-age-seconds 43200 \
    --min-confidence 0.20 --min-mapping-score 0.50 \
    --shrink-strength 1.35 --max-probability-gap 0.45 --max-signals 500 \
    >> "$RUN_ROOT/external_signal_sync.log" 2>&1 || true
}

external_sync_loop() {
  while true; do
    sync_external_once
    sleep "$EXTERNAL_SYNC_INTERVAL_SECONDS"
  done
}

# Seed the external sleeve before any child engine starts. Only direct, fresh,
# mapped probabilities are admitted; news and crypto feature rows stay research-only.
sync_external_once

# Materialise and validate all five generated child configs before starting the
# persistent allocator. This is validation only; the allocator below runs them
# continuously in paper mode against live Polymarket books.
python3 scripts/multi_strategy_paper.py \
  --config "$CONFIG" --run-root "$RUN_ROOT" --engine ./build/polymarket_engine \
  --markets "$MODEL_MARKETS" --min-liquidity "$MIN_LIQUIDITY" --validate-only \
  > "$RUN_ROOT/allocator_validate.log"

# Compatibility surface for legacy detailed panels. Aggregate and per-model PnL
# are read from runtime_status.json and strategy_status.csv.
if [[ ! -e "$RUN_ROOT/terminal" && ! -L "$RUN_ROOT/terminal" ]]; then
  ln -s "strategies/graph" "$RUN_ROOT/terminal"
fi

filter_b2() {
  python3 scripts/filter_coherent_hedges.py \
    --input "$RUN_ROOT/stat_arb_pca_raw.csv" \
    --output "$RUN_ROOT/stat_arb_pca.csv" \
    --rejections "$RUN_ROOT/stat_arb_pca_rejected.csv" \
    --cache "$RUN_ROOT/market_metadata_cache.json" \
    --min-jaccard 0.08 --min-shared-tokens 1 \
    --allow-latent-factor \
    --max-latent-hedge-error 0.80 \
    --min-latent-stability 0.20 \
    --min-latent-z 0.65 \
    --require-positive-maker-edge \
    >> "$RUN_ROOT/coherent_hedges.log" 2>&1
}

rebuild_intents() {
  rm -f "$RUN_ROOT/b1_intents.csv" "$RUN_ROOT/b2_intents.csv" "$RUN_ROOT/intents.csv"
  python3 scripts/build_v4_intents.py \
    --strategy B1 --input "$RUN_ROOT/stat_arb_pairs.csv" \
    --output "$RUN_ROOT/b1_intents.csv" --config "$CONFIG" --min-edge "$INTENT_MIN_EDGE" \
    >> "$RUN_ROOT/intent_build.log" 2>&1
  python3 scripts/build_v4_intents.py \
    --strategy B2 --input "$RUN_ROOT/stat_arb_pca.csv" \
    --output "$RUN_ROOT/b2_intents.csv" --config "$CONFIG" --min-edge "$INTENT_MIN_EDGE" \
    >> "$RUN_ROOT/intent_build.log" 2>&1
  python3 scripts/merge_v4_intents.py \
    --input "$RUN_ROOT/b1_intents.csv" --input "$RUN_ROOT/b2_intents.csv" \
    --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" \
    --max-age-seconds 240 --max-bundles 80 \
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
      --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.75 \
      --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 6 \
      --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --interval 1 --loop \
      >> "$RUN_ROOT/multileg.log" 2>&1 &
    broker_pid=$!
  }

  start_allocator() {
    python3 scripts/multi_strategy_paper.py \
      --config "$CONFIG" --run-root "$RUN_ROOT" --engine ./build/polymarket_engine \
      --markets "$MODEL_MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
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
    sleep 2
  done
}

SUPERVISOR_PID=0
SUPERVISOR_RESTARTS=0
EXTERNAL_SYNC_PID=0
EXTERNAL_SYNC_RESTARTS=0

start_supervisor() {
  supervise_execution >> "$RUN_ROOT/runtime_supervisor.log" 2>&1 &
  SUPERVISOR_PID=$!
}

start_external_sync() {
  external_sync_loop >> "$RUN_ROOT/external_signal_supervisor.log" 2>&1 &
  EXTERNAL_SYNC_PID=$!
}

cleanup() {
  if (( EXTERNAL_SYNC_PID > 0 )); then kill "$EXTERNAL_SYNC_PID" 2>/dev/null || true; fi
  if (( SUPERVISOR_PID > 0 )); then kill "$SUPERVISOR_PID" 2>/dev/null || true; fi
  if (( EXTERNAL_SYNC_PID > 0 )); then wait "$EXTERNAL_SYNC_PID" 2>/dev/null || true; fi
  if (( SUPERVISOR_PID > 0 )); then wait "$SUPERVISOR_PID" 2>/dev/null || true; fi
}

parent_shutdown() {
  trap - EXIT INT TERM
  cleanup
  exit 0
}

trap cleanup EXIT
trap parent_shutdown INT TERM
start_external_sync
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

  if ! kill -0 "$EXTERNAL_SYNC_PID" 2>/dev/null; then
    wait "$EXTERNAL_SYNC_PID" 2>/dev/null || true
    EXTERNAL_SYNC_RESTARTS=$((EXTERNAL_SYNC_RESTARTS + 1))
    printf '%s,external_sync,restart,%s\n' "$(date +%s)" "$EXTERNAL_SYNC_RESTARTS" \
      >> "$RUN_ROOT/runtime_supervisor_events.csv"
    sleep 1
    start_external_sync
  fi

  if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    wait "$SUPERVISOR_PID" 2>/dev/null || true
    SUPERVISOR_RESTARTS=$((SUPERVISOR_RESTARTS + 1))
    printf '%s,supervisor,restart,%s\n' "$(date +%s)" "$SUPERVISOR_RESTARTS" >> "$RUN_ROOT/runtime_supervisor_events.csv"
    sleep 1
    start_supervisor
  fi

  # The passive microstructure sleeve is deliberately frequent and small. The
  # edge gate still includes exit fee, slippage and an adverse-selection buffer.
  ./build/polymarket_maker_paper \
    --config "$CONFIG" --run-dir "$RUN_ROOT/maker" --markets "$MODEL_MARKETS" --min-liquidity "$MIN_LIQUIDITY" \
    --min-edge 0.0005 --max-order-usd 25 --ttl-seconds 120 --hold-seconds 300 \
    --adverse-selection-mult 0.20 --once >> "$RUN_ROOT/maker.log" 2>&1 || true

  if (( now - last_stat >= STAT_INTERVAL_SECONDS )); then
    rm -f "$RUN_ROOT/stat_arb_pairs.csv" "$RUN_ROOT/stat_arb_pca_raw.csv"
    ./build/polymarket_stat_arb \
      --config "$CONFIG" --markets "$MODEL_MARKETS" --history-universe 300 \
      --lookback-hours 720 --fidelity-minutes 30 --min-z 0.75 \
      --min-t-reversion 0.75 --max-half-life-hours 336 --top 200 \
      --csv "$RUN_ROOT/stat_arb_pairs.csv" \
      > "$RUN_ROOT/stat_arb_pairs_latest.log" 2> "$RUN_ROOT/stat_arb_pairs_errors.log" || true
    ./build/polymarket_pca_stat_arb \
      --config "$CONFIG" --markets "$MODEL_MARKETS" --universe 300 \
      --lookback-hours 720 --fidelity-minutes 30 --factors 5 --max-hedges 8 \
      --min-z 0.65 --min-t-reversion 0.60 --max-half-life-hours 336 \
      --max-factor-hedge-error 0.80 --top 200 --csv "$RUN_ROOT/stat_arb_pca_raw.csv" \
      > "$RUN_ROOT/stat_arb_pca_latest.log" 2> "$RUN_ROOT/stat_arb_pca_errors.log" || true
    filter_b2 || true
    rebuild_intents || true
    last_stat=$now
  fi

  if (( now - last_structural >= STRUCTURAL_INTERVAL_SECONDS )); then
    ./build/polymarket_negrisk_arb \
      --config "$CONFIG" --markets "$MODEL_MARKETS" --min-liquidity "$MIN_LIQUIDITY" --top 200 \
      2> "$RUN_ROOT/structural_errors.log" \
      | tee "$RUN_ROOT/structural_latest.log" "$RUN_ROOT/structural_latest.csv" >/dev/null || true
    last_structural=$now
  fi

  # Rewards remain diagnostic only and are not booked into V5 aggregate PnL.
  if (( now - last_rewards >= REWARD_INTERVAL_SECONDS )); then
    rm -f "$RUN_ROOT/reward_opportunities.csv"
    ./build/polymarket_rewards_scan \
      --config "$CONFIG" --markets 2000 --top 120 \
      --quote-shares 25 --max-notional 50 --improve-ticks 0 \
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

  if (( now - last_report >= REPORT_INTERVAL_SECONDS )); then
    python3 scripts/runtime_action_report.py \
      --run-root "$RUN_ROOT" --external-signals data/external_signals.csv \
      --window-seconds 3600 --production-edge "$INTENT_MIN_EDGE" \
      --output-json "$RUN_ROOT/action_report.json" \
      --output-markdown "$RUN_ROOT/action_report.md" \
      > "$RUN_ROOT/action_report_latest.log" 2> "$RUN_ROOT/action_report_errors.log" || true
    last_report=$now
  fi

  if (( now - last_activity >= ACTIVITY_INTERVAL_SECONDS )); then
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
