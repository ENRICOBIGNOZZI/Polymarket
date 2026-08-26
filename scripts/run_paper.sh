#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHAMPION_MANIFEST="$ROOT/config/live_champion.json"
RUNTIME_LOCK="$ROOT/runs/paper-runtime.lock"

cmake -S "$ROOT" -B "$ROOT/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/build" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)"

# One repository-wide lock owns the canonical PAPER plane. Use absolute paths so
# process ownership/health checks are independent of cwd and cannot match a
# second checkout accidentally.
exec python3 "$ROOT/scripts/runtime_singleton_launcher.py" \
  --lock "$RUNTIME_LOCK" -- \
  python3 "$ROOT/scripts/runtime_plane_supervisor.py" \
    --champion-manifest "$CHAMPION_MANIFEST" \
    --fast-run-dir "$ROOT/runs/live-fast-shadow"
