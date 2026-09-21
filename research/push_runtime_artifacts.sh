#!/usr/bin/env bash
set -euo pipefail
TARGET_SHA="${1:?target SHA required}"; LOCAL_ROOT="${PM_V7_RESEARCH_ARTIFACT_ROOT:-$HOME/polymarket-research/runtime_artifacts}"
REMOTE="${POLYMARKET_LONDON_HOST:?POLYMARKET_LONDON_HOST required}"; USER="${POLYMARKET_LONDON_USER:-enrico}"; REMOTE_ROOT="${POLYMARKET_LONDON_ARTIFACT_ROOT:-/home/$USER/polymarket-artifacts}"
BUNDLE="$LOCAL_ROOT/by-sha/$TARGET_SHA"; [[ -f "$BUNDLE/manifest.json" ]] || { echo "missing bundle" >&2; exit 66; }
APPROVAL="${PM_V7_MODEL_RUNTIME_APPROVAL:-$PWD/deploy/v7-model-runtime-approval.json}"
[[ -f "$APPROVAL" && ! -L "$APPROVAL" ]] || { echo "explicit model runtime approval file required" >&2; exit 67; }
REPORT_REL="$(python3 - "$APPROVAL" <<'PY'
import json,sys
from pathlib import Path
value=json.load(open(sys.argv[1],encoding='utf-8'))
path=value.get('backtest_report_path')
assert isinstance(path,str) and path and not Path(path).is_absolute() and '..' not in Path(path).parts
print(path)
PY
)"
REVIEWED_REPORT="$PWD/$REPORT_REL"
python3 scripts/v7_model_runtime_approval.py \
  --manifest "$BUNDLE/manifest.json" --artifact-root "$BUNDLE" \
  --approval "$APPROVAL" --reviewed-report "$REVIEWED_REPORT" \
  --target-sha "$TARGET_SHA" >/dev/null
python3 - "$BUNDLE/candidate_validation.json" "$TARGET_SHA" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]));assert v.get('schema')=='polymarket_v7_candidate_validation_v1';assert v.get('target_sha')==sys.argv[2];assert v.get('state')=='PROMOTABLE' and v.get('promotable') is True;assert v.get('automatic_deployment') is False
PY
expected="$(python3 - "$BUNDLE/manifest.json" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); print(v['target_model_sha'])
PY
)"; [[ "$expected" == "$TARGET_SHA" ]]
cp "$APPROVAL" "$BUNDLE/model_runtime_approval.json.tmp"
mv "$BUNDLE/model_runtime_approval.json.tmp" "$BUNDLE/model_runtime_approval.json"
cp "$REVIEWED_REPORT" "$BUNDLE/reviewed_backtest_report.tmp"
mv "$BUNDLE/reviewed_backtest_report.tmp" "$BUNDLE/reviewed_backtest_report"
ssh "$USER@$REMOTE" "mkdir -p '$REMOTE_ROOT/by-sha'"
rsync -a --delete "$BUNDLE/" "$USER@$REMOTE:$REMOTE_ROOT/by-sha/$TARGET_SHA.tmp/"
ssh "$USER@$REMOTE" "set -e; rm -rf '$REMOTE_ROOT/by-sha/$TARGET_SHA'; mv '$REMOTE_ROOT/by-sha/$TARGET_SHA.tmp' '$REMOTE_ROOT/by-sha/$TARGET_SHA'; ln -sfn 'by-sha/$TARGET_SHA' '$REMOTE_ROOT/current'; test -s '$REMOTE_ROOT/current/manifest.json'"
echo "pushed_runtime_artifact_sha=$TARGET_SHA"
