#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHAMPION_MANIFEST="config/live_champion.json"
RUNTIME_LOCK="runs/paper-runtime.lock"

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

# The singleton launcher acquires the repository-wide runtime lock before any
# champion or fast-arb child can start. The plane supervisor then runs exactly
# one manifest-selected PAPER champion plus one separate read-only fast shadow.
exec python3 scripts/runtime_singleton_launcher.py \
  --lock "$RUNTIME_LOCK" -- \
  python3 scripts/runtime_plane_supervisor.py \
    --champion-manifest "$CHAMPION_MANIFEST" \
    --fast-run-dir runs/live-fast-shadow
