#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESEARCH_ROOT="${PM_V7_RESEARCH_ROOT:-$HOME/polymarket-research/learning}"
PYTHON="${PM_V7_RESEARCH_PYTHON:-python3}"
cd "$ROOT"
exec "$PYTHON" -m research.learning.daily --root "$RESEARCH_ROOT"
