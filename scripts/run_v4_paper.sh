#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cmake -S v4 -B build-v4 -DCMAKE_BUILD_TYPE=Release
cmake --build build-v4 -j

exec ./build-v4/polymarket_v4 \
  --config config/v4.paper.example.json \
  --loop \
  --paper
