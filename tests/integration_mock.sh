#!/usr/bin/env bash
set -euo pipefail
BIN="${1:?engine path required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'kill ${PID:-0} 2>/dev/null || true; rm -rf "$TMP"' EXIT
PORT=$((19000 + RANDOM % 1000))
STATE="$TMP/closed.flag"
python3 "$ROOT/tests/mock_server.py" "$PORT" "$STATE" & PID=$!
for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PORT/markets" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -fsS "http://127.0.0.1:$PORT/markets" >/dev/null
NOW="$(date +%s)"
cat > "$TMP/ext.csv" <<E
market_key,q_yes,confidence,source,timestamp
m1,0.90,1.0,test,$NOW
E
cat > "$TMP/config.json" <<E
{
 "gamma_url":"http://127.0.0.1:$PORT",
 "clob_url":"http://127.0.0.1:$PORT",
 "run_dir":"$TMP/run",
 "external_signals_file":"$TMP/ext.csv",
 "market_limit":10,"books_batch_size":10,"starting_capital":10000,
 "min_liquidity":0,"min_net_edge":0.01,"uncertainty_penalty":0.05,"slippage_bps":0,
 "fractional_kelly":0.10,"max_trade_usd":100,"max_market_fraction":0.1,
 "max_event_fraction":0.1,"max_gross_fraction":0.2,"max_drawdown":0.15,
 "pca_window":10,"pca_min_history":3,"pca_universe":10,
 "expert_weights":{"micro":0.1,"pca":0.1,"graph":0.1,"semantic":0.1,"external":1.0}
}
E
"$BIN" --config "$TMP/config.json" --once --paper
FIRST=$(($(wc -l < "$TMP/run/fills.csv")-1))
test "$FIRST" -eq 1
"$BIN" --config "$TMP/config.json" --once --paper
SECOND=$(($(wc -l < "$TMP/run/fills.csv")-1))
test "$SECOND" -eq 1
grep -q '"open_positions":1' "$TMP/run/status.json"

# Regression: experts whose configured terminal weights are all zero must
# not fall back to a 0.5 fair value or create a paper trade.
cat > "$TMP/config_zero.json" <<E
{
 "gamma_url":"http://127.0.0.1:$PORT",
 "clob_url":"http://127.0.0.1:$PORT",
 "run_dir":"$TMP/run_zero",
 "external_signals_file":"$TMP/ext.csv",
 "market_limit":10,"books_batch_size":10,"starting_capital":10000,
 "min_liquidity":0,"min_net_edge":0.01,"uncertainty_penalty":0.05,"slippage_bps":0,
 "fractional_kelly":0.10,"max_trade_usd":100,"max_market_fraction":0.1,
 "max_event_fraction":0.1,"max_gross_fraction":0.2,"max_drawdown":0.15,
 "pca_window":10,"pca_min_history":3,"pca_universe":10,
 "expert_weights":{"micro":0.0,"pca":0.0,"graph":0.0,"semantic":0.0,"external":0.0}
}
E
"$BIN" --config "$TMP/config_zero.json" --once --paper
ZERO_FILLS=$(($(wc -l < "$TMP/run_zero/fills.csv")-1))
ZERO_SIGNALS=$(($(wc -l < "$TMP/run_zero/signals.csv")-1))
test "$ZERO_FILLS" -eq 0
test "$ZERO_SIGNALS" -eq 0
grep -q '"open_positions":0' "$TMP/run_zero/status.json"

touch "$STATE"
"$BIN" --config "$TMP/config.json" --once --paper
THIRD=$(($(wc -l < "$TMP/run/fills.csv")-1))
test "$THIRD" -eq 2
grep -q 'SETTLE' "$TMP/run/fills.csv"
grep -q '"open_positions":0' "$TMP/run/status.json"
test -s "$TMP/run/signals.csv"
echo "mock integration passed"
