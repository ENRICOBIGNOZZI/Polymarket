#!/usr/bin/env bash
set -euo pipefail
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
./build/poly-engine --port 8080 --cycle 5 --cash 10000 --web-root web
