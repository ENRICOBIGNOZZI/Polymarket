#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
RUN_ROOT="${PM_V7_RESEARCH_RUN_ROOT:-$HOME/polymarket-research/london/current}"
DURABLE_ROOT="${PM_V7_RESEARCH_DURABLE_ROOT:-$HOME/polymarket-research/durable}"
ARCHIVE_ROOT="${PM_V7_RESEARCH_ARCHIVE_ROOT:-$HOME/polymarket-research/archives}"
OUT="${PM_V7_RESEARCH_REPORT_ROOT:-$HOME/polymarket-research/reports/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT" "$DURABLE_ROOT" "$ARCHIVE_ROOT" "$OUT/learned_execution" "$OUT/reports"
SHA="${PM_V7_RESEARCH_MODEL_SHA:-$(python3 - "$RUN_ROOT/control/runtime_identity.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['runtime_sha'])
PY
)}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact research SHA required" >&2; exit 64; }
# All work below is retrospective/read-only with respect to London and owns no execution authority.
python3 scripts/v7_canonical_economics.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --expected-model-sha "$SHA" \
  --markout-evidence "$RUN_ROOT/research/evidence/maker_markout" --output "$OUT/canonical_economics.json" || true
python3 scripts/v7_generate_economic_artifacts.py --repo "$ROOT" --run-root "$RUN_ROOT" --output "$OUT/economic_artifacts" || true
python3 scripts/v7_joint_execution_policy.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
  --output "$OUT/learned_execution/joint_policy.json" --strategy CRYPTO_SETTLEMENT_ENGINE --min-bundles 20 || true
python3 scripts/v7_learned_execution_model.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --model-sha "$SHA" \
  --output "$OUT/learned_execution/oos_report.json" || true
python3 scripts/v7_profit_attribution.py --ledger "$RUN_ROOT/ledger/execution.jsonl" --run-root "$RUN_ROOT" \
  --output "$OUT/profit_attribution.json" --csv "$OUT/profit_attribution.csv" || true
EXPERIMENT_ROOT="$RUN_ROOT/research/collector_buffer/profit_experiments"
[[ -d "$EXPERIMENT_ROOT" ]] && python3 scripts/v7_profit_report.py --experiment-root "$EXPERIMENT_ROOT" --all-cohorts \
  --output "$OUT/profit_experiment_report.json" || true
python3 scripts/v7_fast_cancel_latency_report.py --maker-evidence "$RUN_ROOT/ledger/execution.jsonl" \
  --output "$OUT/reports/fast_cancel_latency.json" || true
python3 scripts/v7_maker_execution_horse_race.py \
  --maker-evidence "$RUN_ROOT/ledger/execution.jsonl" \
  --maker-evidence "$RUN_ROOT/research/evidence/maker_markout" \
  --hard-cancel-events "$RUN_ROOT/research/external_cancel_signals.jsonl" \
  --learned-shadow "$RUN_ROOT/research/pm_repricing_shadow.jsonl" \
  --output "$OUT/reports/maker_execution_horse_race.json" --allow-global-hard-scope \
  --cancel-latency-ms 25 --stress-cancel-latency-ms 5,25,50,100,200 || true
python3 scripts/v7_economic_decision_report.py --run-root "$RUN_ROOT" --durable-root "$DURABLE_ROOT" \
  --benchmark "$DURABLE_ROOT/permanent_evidence/benchmarks/latest.json" || true
python3 scripts/v7_lossless_data_compaction.py --root "all=${RUN_ROOT%/*}" \
  --store "$DURABLE_ROOT/permanent_evidence/store" --output "$DURABLE_ROOT/permanent_evidence/compaction.jsonl" \
  --maximum-groups 10 --maximum-seconds 20 --nonblocking --apply || true
python3 scripts/v7_permanent_evidence.py --run-root "$RUN_ROOT" --durable-root "$DURABLE_ROOT" \
  --archive-root "$ARCHIVE_ROOT" --repository-root "$ROOT" --maximum-seconds 20 --maximum-bytes 67108864 || true
printf 'research_report_root=%s\n' "$OUT"
