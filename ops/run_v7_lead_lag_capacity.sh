#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ROOT="${POLYMARKET_RUN_ROOT:-$ROOT/runs/paper_v7_live}"
OUTPUT_ROOT="${POLYMARKET_CAPACITY_OUTPUT_ROOT:-$RUN_ROOT/research/lead_lag_capacity_v1}"
PYTHON="${POLYMARKET_PYTHON:-python3}"

args=(
  "$ROOT/scripts/v7_lead_lag_capacity_history.py"
  --run-root "$RUN_ROOT"
  --output-root "$OUTPUT_ROOT"
)

if [[ -n "${POLYMARKET_CAPACITY_LABEL:-}" ]]; then
  args+=(--label "$POLYMARKET_CAPACITY_LABEL")
fi
if [[ "${POLYMARKET_CAPACITY_FORCE:-0}" == "1" ]]; then
  args+=(--force)
fi

exec "$PYTHON" "${args[@]}" "$@"
