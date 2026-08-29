#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHAMPION_MANIFEST="${POLYMARKET_CHAMPION_MANIFEST:-config/live_champion.json}"
if [[ ! -f "$CHAMPION_MANIFEST" ]]; then
  echo "fatal: PAPER champion manifest is missing: $CHAMPION_MANIFEST" >&2
  exit 78
fi

read -r ENABLED VERSION LOOP CONFIG RUN_ROOT PAPER_ONLY AUTHENTICATED < <(
  python3 - "$CHAMPION_MANIFEST" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    manifest = json.load(handle)

def token(value):
    return "-" if value is None or value == "" else str(value)

print(
    "1" if manifest.get("enabled") is True else "0",
    token(manifest.get("version")),
    token(manifest.get("loop")),
    token(manifest.get("config")),
    token(manifest.get("run_root")),
    "1" if manifest.get("paper_only") is True else "0",
    "1" if manifest.get("authenticated_execution") is True else "0",
)
PY
)

if [[ "$ENABLED" != "1" ]]; then
  echo "fatal: no operational V7 PAPER champion is enabled; cutover validation/promotion is still required" >&2
  exit 78
fi
if [[ "$VERSION" != "7" ]]; then
  echo "fatal: refusing non-V7 PAPER champion version=$VERSION" >&2
  exit 78
fi
if [[ "$PAPER_ONLY" != "1" || "$AUTHENTICATED" != "0" ]]; then
  echo "fatal: champion violates PAPER-only/authenticated-execution boundary" >&2
  exit 78
fi

case "$LOOP" in
  scripts/*) ;;
  *) echo "fatal: champion loop must stay within scripts/" >&2; exit 78 ;;
esac
case "$CONFIG" in
  config/*) ;;
  *) echo "fatal: champion config must stay within config/" >&2; exit 78 ;;
esac
case "$RUN_ROOT" in
  runs/*) ;;
  *) echo "fatal: champion run_root must stay within runs/" >&2; exit 78 ;;
esac
for path in "$LOOP" "$CONFIG" "$RUN_ROOT"; do
  if [[ "$path" == *"/../"* || "$path" == ../* || "$path" == */.. ]]; then
    echo "fatal: champion manifest paths may not contain parent traversal" >&2
    exit 78
  fi
done

if [[ ! -f "$LOOP" ]]; then
  echo "fatal: V7 champion loop is missing: $LOOP" >&2
  exit 78
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "fatal: V7 champion config is missing: $CONFIG" >&2
  exit 78
fi

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

exec bash "$LOOP" "$CONFIG" "$RUN_ROOT"
