#!/usr/bin/env bash
set -euo pipefail
BIN="${1:?engine path required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'kill ${PID:-0} 2>/dev/null || true; rm -rf "$TMP"' EXIT
PORT=$((20000 + RANDOM % 1000))
TRADE="$TMP/trade.flag"
python3 "$ROOT/tests/maker_mock_server.py" "$PORT" "$TRADE" & PID=$!
for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:$PORT/markets" >/dev/null 2>&1 && break; sleep 0.1; done
NOW="$(date +%s)"
cat > "$TMP/ext.csv" <<E
market_key,q_yes,confidence,source,timestamp
m1,0.90,1.0,test,$NOW
E
cat > "$TMP/config.json" <<E
{
 "gamma_url":"http://127.0.0.1:$PORT",
 "clob_url":"http://127.0.0.1:$PORT",
 "data_url":"http://127.0.0.1:$PORT",
 "run_dir":"$TMP/run",
 "external_signals_file":"$TMP/ext.csv",
 "market_limit":10,"gamma_page_size":10,"books_batch_size":10,"starting_capital":10000,
 "min_liquidity":0,"min_mid":0.01,"max_mid":0.99,"max_spread":0.15,
 "min_net_edge":1.0,"uncertainty_penalty":0.05,"slippage_bps":0,
 "fractional_kelly":0.10,"max_trade_usd":100,"max_market_fraction":0.10,
 "max_event_fraction":0.10,"max_gross_fraction":0.20,"max_drawdown":0.15,
 "history_bootstrap":false,"pca_window":10,"pca_min_history":3,"pca_universe":10,
 "maker_enabled":true,"maker_min_edge":0.01,"maker_quote_usd":50,
 "maker_fractional_kelly":0.05,"maker_uncertainty_penalty":0.05,
 "maker_adverse_spread_mult":0.0,"maker_max_open_quotes":5,
 "maker_order_ttl_seconds":60,"maker_min_fill_fraction":0.25,"maker_improve_ticks":1,
 "expert_weights":{"micro":0.1,"pca":0.1,"graph":0.1,"semantic":0.0,"external":1.0}
}
E

"$BIN" --config "$TMP/config.json" --once --paper
# Taker threshold is impossible; the first tick must place a passive maker quote but no fill.
test "$(($(wc -l < "$TMP/run/fills.csv")-1))" -eq 0
test "$(($(wc -l < "$TMP/run/maker_orders.csv")-1))" -eq 1
grep -q '0.39' "$TMP/run/maker_orders.csv"

sleep 2
touch "$TRADE"
"$BIN" --config "$TMP/config.json" --once --paper
# A later taker SELL of 10k shares at 0.39 consumes the displayed 5k queue and fills our quote.
grep -q 'MAKER_BUY' "$TMP/run/fills.csv"
test "$(($(wc -l < "$TMP/run/maker_orders.csv")-1))" -eq 0
grep -q '^m1,' "$TMP/run/broker_state.csv"
echo "maker mock integration passed"
