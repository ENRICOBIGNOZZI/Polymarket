#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v4.json}"
RUN_ROOT="${2:-runs/paper_v4_live}"
ALL_ROOT="$RUN_ROOT/all_market"
FAST_ROOT="$RUN_ROOT/fast"
FAST_MARKETS="${ALL_MARKET_FAST_MARKETS:-0}"
FAST_MIN_LIQUIDITY="${ALL_MARKET_FAST_MIN_LIQUIDITY:-20}"
FAST_SHARD_SIZE="${ALL_MARKET_FAST_SHARD_SIZE:-1000}"
CANDIDATE_LIMIT="${ALL_MARKET_CANDIDATE_LIMIT:-1000}"
UNIVERSE_REFRESH="${ALL_MARKET_UNIVERSE_REFRESH_SECONDS:-300}"
BOOK_REFRESH="${ALL_MARKET_BOOK_REFRESH_SECONDS:-5}"
ACCOUNT_REFRESH="${ALL_MARKET_ACCOUNT_REFRESH_SECONDS:-60}"

mkdir -p "$ALL_ROOT" "$FAST_ROOT"

fast_pid=0
last_universe=0
last_book=0
last_account=0

start_fast() {
  ./build/polymarket_fast_arb_shadow \
    --config "$CONFIG" \
    --policy config/fast_arb_policy.json \
    --relations config/fast_arb_relations.csv \
    --external-signals data/external_signals.csv \
    --run-dir "$FAST_ROOT" \
    --markets "$FAST_MARKETS" \
    --min-liquidity "$FAST_MIN_LIQUIDITY" \
    --shard-size "$FAST_SHARD_SIZE" \
    --snapshot-refresh-seconds 30 \
    --status-interval-seconds 1 \
    --recycle-seconds 900 \
    >> "$ALL_ROOT/fast_shadow.log" 2>&1 &
  fast_pid=$!
}

cleanup() {
  if (( fast_pid > 0 )); then
    kill "$fast_pid" 2>/dev/null || true
    wait "$fast_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_fast

while true; do
  now="$(date +%s)"

  if ! kill -0 "$fast_pid" 2>/dev/null; then
    wait "$fast_pid" 2>/dev/null || true
    sleep 2
    start_fast
  fi

  if (( now - last_universe >= UNIVERSE_REFRESH )); then
    python3 scripts/all_market_universe.py \
      --output "$ALL_ROOT/universe.csv" \
      --status "$ALL_ROOT/universe_status.json" \
      --limit 0 \
      --tier1-min-liquidity "$FAST_MIN_LIQUIDITY" \
      --tier2-min-liquidity 100 \
      >> "$ALL_ROOT/universe.log" 2>&1 || true
    last_universe=$now
  fi

  if (( now - last_book >= BOOK_REFRESH )); then
    python3 scripts/build_global_opportunity_book.py \
      --run-root "$RUN_ROOT" \
      --limit "$CANDIDATE_LIMIT" \
      >> "$ALL_ROOT/opportunity_book.log" 2>&1 || true
    last_book=$now
  fi

  if (( now - last_account >= ACCOUNT_REFRESH )); then
    python3 scripts/polymarket_account_readonly.py \
      --output "$RUN_ROOT/account_readonly_status.json" \
      >> "$ALL_ROOT/account_readonly.log" 2>&1 || true
    last_account=$now
  fi

  sleep 1
done
