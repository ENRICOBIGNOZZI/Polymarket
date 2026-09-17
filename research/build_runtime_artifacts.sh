#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
TARGET_SHA="${1:-$(git rev-parse HEAD)}"
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact target SHA required" >&2; exit 64; }
[[ "$(git rev-parse HEAD)" == "$TARGET_SHA" ]] || { echo "checkout must equal target SHA" >&2; exit 65; }
SOURCE_ROOT="${PM_V7_RESEARCH_SOURCE_ROOT:-$HOME/polymarket/runs/paper_v7_live}"
ARCHIVE_ROOT="${PM_V7_RESEARCH_ARCHIVE_ROOT:-$HOME/polymarket/runs/paper_v7_archives}"
DURABLE_ROOT="${PM_V7_RESEARCH_DURABLE_ROOT:-$HOME/polymarket/runs/paper_v7_durable}"
OUTPUT_ROOT="${PM_V7_RESEARCH_ARTIFACT_ROOT:-$HOME/polymarket-research/runtime_artifacts}"
BUNDLE="$OUTPUT_ROOT/by-sha/$TARGET_SHA"; tmp="${BUNDLE}.tmp.$$"; rm -rf "$tmp"; mkdir -p "$tmp"
work="$(mktemp -d)"; trap 'rm -rf "$work" "$tmp"' EXIT
python3 scripts/v7_capital_allocator.py --config config/paper_v7.json --output-dir "$work/alloc" >/dev/null
sources=(); [[ -d "$ARCHIVE_ROOT" ]] && sources+=(--source-root "$ARCHIVE_ROOT"); [[ -d "$SOURCE_ROOT" ]] && sources+=(--source-root "$SOURCE_ROOT")
python3 scripts/v7_maker_durable_learning.py "${sources[@]}" \
  --store "$DURABLE_ROOT/micro_maker/research_evidence.jsonl" \
  --store-status "$tmp/maker_training_status.json" \
  --output-model "$tmp/maker_execution_model.json" \
  --policy config/v7_professional_market_maker.json --config "$work/alloc/micro_maker.json" --model-sha "$TARGET_SHA"
rich_args=(); for tape in "$DURABLE_ROOT/external_fair/counterfactuals.jsonl" "$SOURCE_ROOT/external_fair/counterfactuals.jsonl"; do [[ -f "$tape" ]] && rich_args+=(--tape "$tape"); done
if (( ${#rich_args[@]} )); then
  set +e
  python3 scripts/v7_external_rich_train.py "${rich_args[@]}" --output-model "$tmp/rich_research_model.json" \
    --replace-research-model --config config/v7_external_fair.json --model-sha "$TARGET_SHA" --status "$tmp/rich_training_status.json"
  rich_status=$?
  set -e
  if (( rich_status != 0 )); then rm -f "$tmp/rich_research_model.json"; fi
else
  printf '%s\n' '{"state":"NO_COUNTERFACTUAL_TAPE","paper_only":true}' > "$tmp/rich_training_status.json"
fi
manifest_args=(--target-sha "$TARGET_SHA" --maker-model "$tmp/maker_execution_model.json" --output "$tmp/manifest.json")
[[ -f "$tmp/rich_research_model.json" ]] && manifest_args+=(--rich-model "$tmp/rich_research_model.json")
python3 research/runtime_artifact_manifest.py "${manifest_args[@]}"
mkdir -p "$(dirname "$BUNDLE")"; rm -rf "$BUNDLE"; mv "$tmp" "$BUNDLE"; trap 'rm -rf "$work"' EXIT
ln -sfn "by-sha/$TARGET_SHA" "$OUTPUT_ROOT/current"
printf 'runtime_artifact_bundle=%s\n' "$BUNDLE"
