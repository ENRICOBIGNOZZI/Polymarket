#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
RUN_ROOT="${PM_V7_RESEARCH_RUN_ROOT:-$HOME/polymarket-research/london/current}"
OUT="${PM_V7_RESEARCH_REPORT_ROOT:-$HOME/polymarket-research/reports/$(date +%Y%m%d-%H%M%S)}"; mkdir -p "$OUT"
SHA="${PM_V7_RESEARCH_MODEL_SHA:-$(python3 - "$RUN_ROOT/control/runtime_identity.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['runtime_sha'])
PY
)}"
python3 scripts/v7_canonical_economics.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --expected-model-sha "$SHA" --markout-evidence "$RUN_ROOT/research/evidence/maker_markout" --output "$OUT/canonical_economics.json" || true
python3 scripts/v7_generate_economic_artifacts.py --repo "$ROOT" --run-root "$RUN_ROOT" --output "$OUT/economic_artifacts" || true
python3 scripts/v7_profit_attribution.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --run-root "$RUN_ROOT" --output "$OUT/profit_attribution.json" --csv "$OUT/profit_attribution.csv" || true
printf 'research_report_root=%s\n' "$OUT"
