#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/paper_v5.json}"
RUN_ROOT="${2:-runs/paper_v5_live}"

# Aggressive paper defaults: widen and accelerate discovery without converting
# cost-negative raw signals into executable intents or importing unapproved
# research-only external forecasts.
export V5_MIN_LIQUIDITY="${V5_MIN_LIQUIDITY:-10}"
export V5_MODEL_MARKETS="${V5_MODEL_MARKETS:-1000}"
export V5_RECORDER_MARKETS="${V5_RECORDER_MARKETS:-1500}"
export V5_RECORDER_BATCH="${V5_RECORDER_BATCH:-80}"
export V5_RECORDER_LOOKBACK_SECONDS="${V5_RECORDER_LOOKBACK_SECONDS:-900}"
export V5_STAT_INTERVAL_SECONDS="${V5_STAT_INTERVAL_SECONDS:-60}"
export V5_STRUCTURAL_INTERVAL_SECONDS="${V5_STRUCTURAL_INTERVAL_SECONDS:-30}"
export V5_REWARD_INTERVAL_SECONDS="${V5_REWARD_INTERVAL_SECONDS:-180}"
export V5_REPORT_INTERVAL_SECONDS="${V5_REPORT_INTERVAL_SECONDS:-30}"
export V5_INTENT_MIN_EDGE="${V5_INTENT_MIN_EDGE:-0.00025}"

exec scripts/paper_v5_loop.sh "$CONFIG" "$RUN_ROOT"
