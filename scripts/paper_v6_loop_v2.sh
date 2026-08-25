#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v6.json}"
RUN_ROOT="${2:-runs/paper_v6_live}"
MIN_LIQUIDITY="${V6_MIN_LIQUIDITY:-10}"
MARKETS="${V6_MARKETS:-700}"
RECORDER_MARKETS="${V6_RECORDER_MARKETS:-1200}"
INTENT_MIN_EDGE="${V6_INTENT_MIN_EDGE:-0.00020}"
mkdir -p "$RUN_ROOT" "$RUN_ROOT/maker" "$RUN_ROOT/micro_taker" "$RUN_ROOT/hard_arb" "$RUN_ROOT/external"
python3 scripts/v6_materialize_configs.py --config "$CONFIG" --run-root "$RUN_ROOT" >>"$RUN_ROOT/materialize.log" 2>&1

rm -f "$RUN_ROOT"/intents.csv "$RUN_ROOT"/local_factor_intents.csv \
      "$RUN_ROOT"/relation_intents_raw.csv "$RUN_ROOT"/relation_guarded.csv \
      "$RUN_ROOT"/typed_structural_intents.csv "$RUN_ROOT"/relation_intents.csv

rec_pid=0; broker_pid=0; external_pid=0
rec_restarts=0; broker_restarts=0; external_restarts=0
start_recorder(){ ./build/polymarket_trade_recorder --config "$CONFIG" --run-dir "$RUN_ROOT" --markets "$RECORDER_MARKETS" --batch 40 --min-liquidity "$MIN_LIQUIDITY" --lookback-seconds 900 --interval 5 --loop >>"$RUN_ROOT/trade_recorder.log" 2>&1 & rec_pid=$!; }
start_broker(){ ./build/polymarket_multileg_paper --config "$RUN_ROOT/broker_config.json" --run-dir "$RUN_ROOT" --intents "$RUN_ROOT/intents.csv" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$INTENT_MIN_EDGE" --completion-threshold 0.75 --submit-latency-ms 100 --cancel-latency-ms 100 --max-replaces 6 --max-leg-risk-usd 12 --adverse-horizon-seconds 45 --interval 1 --loop >>"$RUN_ROOT/multileg.log" 2>&1 & broker_pid=$!; }
start_external(){ ./build/polymarket_engine --config "$RUN_ROOT/external_config.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --paper --loop >>"$RUN_ROOT/external/engine.log" 2>&1 & external_pid=$!; }
write_supervisor(){
  local ra=0 ba=0 ea=0; kill -0 "$rec_pid" 2>/dev/null&&ra=1||true; kill -0 "$broker_pid" 2>/dev/null&&ba=1||true; kill -0 "$external_pid" 2>/dev/null&&ea=1||true
  local tmp="$RUN_ROOT/runtime_supervisor.csv.tmp"
  printf 'timestamp,recorder_alive,broker_alive,allocator_alive,recorder_restarts,broker_restarts,allocator_restarts,recorder_pid,broker_pid,allocator_pid\n' >"$tmp"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$ra" "$ba" "$ea" "$rec_restarts" "$broker_restarts" "$external_restarts" "$rec_pid" "$broker_pid" "$external_pid" >>"$tmp"
  mv "$tmp" "$RUN_ROOT/runtime_supervisor.csv"
}
cleanup(){ for p in "$rec_pid" "$broker_pid" "$external_pid";do if ((p>0));then kill "$p" 2>/dev/null||true;fi;done; for p in "$rec_pid" "$broker_pid" "$external_pid";do if ((p>0));then wait "$p" 2>/dev/null||true;fi;done; }
trap cleanup EXIT INT TERM
start_recorder; start_broker; start_external; write_supervisor

last_factor=0; last_relation=0; last_external=0; last_status=0; last_action_report=0; last_micro=0; last_hard=0
rebuild_intents(){ python3 scripts/merge_v4_intents.py --input "$RUN_ROOT/local_factor_intents.csv" --input "$RUN_ROOT/relation_intents.csv" --output "$RUN_ROOT/intents.csv" --min-edge "$INTENT_MIN_EDGE" --max-age-seconds 240 --max-bundles 120 >>"$RUN_ROOT/intent_merge.log" 2>&1 || true; }

while true; do
  now="$(date +%s)"
  if ! kill -0 "$rec_pid" 2>/dev/null; then wait "$rec_pid" 2>/dev/null||true; rec_restarts=$((rec_restarts+1)); start_recorder; fi
  if ! kill -0 "$broker_pid" 2>/dev/null; then wait "$broker_pid" 2>/dev/null||true; broker_restarts=$((broker_restarts+1)); start_broker; fi
  if ! kill -0 "$external_pid" 2>/dev/null; then wait "$external_pid" 2>/dev/null||true; external_restarts=$((external_restarts+1)); start_external; fi
  write_supervisor

  python3 scripts/v6_micro_maker.py --config "$RUN_ROOT/maker_config.json" --run-dir "$RUN_ROOT/maker" --trade-tape "$RUN_ROOT/trade_tape.csv" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge 0.00035 --max-order-usd 25 --ttl-seconds 90 --hold-seconds 240 --adverse-selection-mult 0.15 --flow-lookback-seconds 900 --min-fill-probability 0.02 --target-fill-probability 0.15 --max-improve-ticks 2 >>"$RUN_ROOT/maker.log" 2>&1 || true

  if ((now-last_micro>=5)); then
    python3 scripts/v6_micro_taker_v2.py --config "$RUN_ROOT/micro_taker_config.json" --run-dir "$RUN_ROOT/micro_taker" --trade-tape "$RUN_ROOT/trade_tape.csv" --markets 250 --min-liquidity 25 --horizon-seconds 30 --flow-lookback-seconds 180 --model-half-life-seconds 21600 --max-trade-usd 15 --min-edge 0.00030 --slippage-bps 5 --max-positions 20 >>"$RUN_ROOT/micro_taker.log" 2>&1 || true
    last_micro=$now
  fi

  if ((now-last_hard>=10)); then
    python3 scripts/v6_hard_arb_paper_v2.py --config "$RUN_ROOT/hard_arb_config.json" --run-dir "$RUN_ROOT/hard_arb" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --max-events 80 --min-edge 0.00020 --max-trade-usd 60 --slippage-bps 5 >>"$RUN_ROOT/hard_arb.log" 2>&1 || true
    last_hard=$now
  fi

  if ((now-last_factor>=60)); then
    rm -f "$RUN_ROOT/local_factor_intents.csv"
    python3 scripts/v6_local_factor_v2.py --config "$CONFIG" --output "$RUN_ROOT/local_factor_intents.csv" --status "$RUN_ROOT/local_factor_status.json" --trade-tape "$RUN_ROOT/trade_tape.csv" --markets 400 --min-liquidity "$MIN_LIQUIDITY" --lookback-hours 336 --fidelity-minutes 60 --max-clusters 15 --min-common-points 48 --min-z 1.00 --fdr 0.10 --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --slippage-bps 5 --flow-lookback-seconds 900 --min-fill-probability 0.03 >"$RUN_ROOT/local_factor_latest.log" 2>"$RUN_ROOT/local_factor_errors.log" || true
    rebuild_intents; last_factor=$now
  fi

  if ((now-last_relation>=30)); then
    python3 scripts/v6_relation_intents.py --config "$CONFIG" --output "$RUN_ROOT/relation_intents_raw.csv" --status "$RUN_ROOT/relation_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 --max-events 80 >"$RUN_ROOT/relation_latest.log" 2>"$RUN_ROOT/relation_errors.log" || true
    python3 scripts/v6_intent_guard.py --input "$RUN_ROOT/relation_intents_raw.csv" --output "$RUN_ROOT/relation_guarded.csv" --status "$RUN_ROOT/relation_guard_status.json" --min-edge "$INTENT_MIN_EDGE" --stress-bps 10 --max-age-seconds 240 >>"$RUN_ROOT/relation_guard.log" 2>&1 || true
    python3 scripts/v6_typed_structural_v2.py --config "$CONFIG" --output "$RUN_ROOT/typed_structural_intents.csv" --status "$RUN_ROOT/typed_structural_status.json" --markets "$MARKETS" --min-liquidity "$MIN_LIQUIDITY" --min-edge "$INTENT_MIN_EDGE" --max-trade-usd 60 >>"$RUN_ROOT/typed_structural.log" 2>&1 || true
    python3 scripts/v6_queue_filter.py --config "$CONFIG" --input "$RUN_ROOT/relation_guarded.csv" --input "$RUN_ROOT/typed_structural_intents.csv" --output "$RUN_ROOT/relation_intents.csv" --status "$RUN_ROOT/queue_filter_status.json" --trade-tape "$RUN_ROOT/trade_tape.csv" --min-edge "$INTENT_MIN_EDGE" --reserve-bps 10 --flow-lookback-seconds 900 --horizon-seconds 180 --min-leg-fill-probability 0.02 --min-joint-fill-probability 0.0005 --target-leg-fill-probability 0.20 --max-improve-ticks-per-leg 3 >>"$RUN_ROOT/queue_filter.log" 2>&1 || true
    rebuild_intents; last_relation=$now
  fi

  if ((now-last_external>=60)); then
    python3 scripts/v6_external_bridge.py --output "$RUN_ROOT/external_signals.csv" --status "$RUN_ROOT/external_bridge_status.json" --max-age-seconds 21600 --min-confidence 0.35 >"$RUN_ROOT/external_bridge.log" 2>&1 || true
    last_external=$now
  fi

  if ((now-last_status>=15)); then
    python3 scripts/v6_runtime_status_v2.py --config "$CONFIG" --run-root "$RUN_ROOT" >"$RUN_ROOT/runtime_status.log" 2>&1 || true
    last_status=$now
  fi
  if ((now-last_action_report>=60)); then
    python3 scripts/runtime_action_report.py --run-root "$RUN_ROOT" --external-signals "$RUN_ROOT/external_signals.csv" --window-seconds 3600 --production-edge "$INTENT_MIN_EDGE" --output-json "$RUN_ROOT/action_report.json" --output-markdown "$RUN_ROOT/action_report.md" >"$RUN_ROOT/action_report_latest.log" 2>&1 || true
    last_action_report=$now
  fi
  sleep 5
done
