#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
OUT="${PM_V7_RESEARCH_CONTEXT_ROOT:-$HOME/polymarket-research/external_context}"; mkdir -p "$OUT"
pids=(); trap 'for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done' EXIT INT TERM
python3 scripts/v7_binance_usdm_rest_collector.py --status "$OUT/binance_usdm_rest_status.json" --tape "$OUT/binance_usdm_rest.jsonl" --interval 5 --loop >>"$OUT/binance_usdm_rest.log" 2>&1 & pids+=("$!")
python3 scripts/v7_deribit_rest_collector.py --status "$OUT/deribit_rest_status.json" --tape "$OUT/deribit_rest.jsonl" --interval 15 --loop >>"$OUT/deribit_rest.log" 2>&1 & pids+=("$!")
python3 scripts/v7_coinbase_l2_rest_collector.py --status "$OUT/coinbase_l2_rest_status.json" --tape "$OUT/coinbase_l2_rest.jsonl" --interval 5 --loop >>"$OUT/coinbase_l2_rest.log" 2>&1 & pids+=("$!")
wait
