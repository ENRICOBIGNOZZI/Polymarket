#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -ne 2 ]]; then
  echo "usage: $0 config/v7_profit_stack.json runs/<v7-run-root>" >&2
  exit 64
fi
CONFIG="$1"
RUN_ROOT="$2"

case "$CONFIG" in
  config/*) ;;
  *) echo "fatal: V7 profit-stack config must stay under config/" >&2; exit 78 ;;
esac
case "$RUN_ROOT" in
  runs/*) ;;
  *) echo "fatal: V7 profit-stack run root must stay under runs/" >&2; exit 78 ;;
esac
if [[ "$CONFIG" == *"/../"* || "$RUN_ROOT" == *"/../"* || "$CONFIG" == ../* || "$RUN_ROOT" == ../* ]]; then
  echo "fatal: parent traversal is forbidden" >&2
  exit 78
fi

MODEL_SHA="$(git rev-parse HEAD)"
[[ "$MODEL_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "fatal: invalid runtime SHA" >&2; exit 78; }
export POLYMARKET_EXPECTED_MODEL_SHA="${POLYMARKET_EXPECTED_MODEL_SHA:-$MODEL_SHA}"
if [[ "$POLYMARKET_EXPECTED_MODEL_SHA" != "$MODEL_SHA" ]]; then
  echo "fatal: checked-out SHA does not equal expected validated SHA" >&2
  exit 78
fi

exec python3 scripts/v7_profit_stack_runtime.py "$CONFIG" "$RUN_ROOT"
