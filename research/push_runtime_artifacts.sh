#!/usr/bin/env bash
set -euo pipefail
TARGET_SHA="${1:?target SHA required}"; LOCAL_ROOT="${PM_V7_RESEARCH_ARTIFACT_ROOT:-$HOME/polymarket-research/runtime_artifacts}"
REMOTE="${POLYMARKET_LONDON_HOST:?POLYMARKET_LONDON_HOST required}"; USER="${POLYMARKET_LONDON_USER:-enrico}"; REMOTE_ROOT="${POLYMARKET_LONDON_ARTIFACT_ROOT:-/home/$USER/polymarket-artifacts}"
BUNDLE="$LOCAL_ROOT/by-sha/$TARGET_SHA"; [[ -f "$BUNDLE/manifest.json" ]] || { echo "missing bundle" >&2; exit 66; }
APPROVAL="${PM_V7_MODEL_RUNTIME_APPROVAL:-$PWD/deploy/v7-model-runtime-approval.json}"
[[ -f "$APPROVAL" && ! -L "$APPROVAL" ]] || { echo "explicit model runtime approval file required" >&2; exit 67; }
REVIEWED_REPORT="$(python3 - "$APPROVAL" "$BUNDLE/manifest.json" "$TARGET_SHA" "$PWD" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
approval_path=Path(sys.argv[1]); manifest_path=Path(sys.argv[2]); target=sys.argv[3]
repo=Path(sys.argv[4]).resolve()
approval=json.loads(approval_path.read_text(encoding='utf-8'))
manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
assert approval.get('schema')=='polymarket_v7_model_runtime_approval_v1'
assert approval.get('version')==1
assert approval.get('approved') is True
assert approval.get('explicit_user_approval_required') is True
assert approval.get('review_backtest_before_approval') is True
assert approval.get('automatic_promotion') is False
assert approval.get('approval_scope')=='PAPER_RUNTIME_NEW_MODEL_ACTIVATION'
assert re.fullmatch(r'[0-9a-f]{40}',target)
assert approval.get('target_model_sha')==target
assert isinstance(approval.get('bundle_id'),str) and approval['bundle_id']==manifest.get('bundle_id')
maker=(manifest.get('maker_execution_model') or {}).get('sha256')
rich_meta=manifest.get('rich_research_model') or {}
rich=rich_meta.get('sha256') if rich_meta.get('state')=='AVAILABLE' else None
assert isinstance(maker,str) and re.fullmatch(r'[0-9a-f]{64}',maker)
assert approval.get('target_maker_execution_model_sha256')==maker
assert approval.get('target_rich_model_sha256')==rich
report_rel=approval.get('backtest_report_path')
assert isinstance(report_rel,str) and report_rel and not Path(report_rel).is_absolute()
assert '..' not in Path(report_rel).parts
report=(repo/report_rel).resolve()
assert repo==report.parent or repo in report.parents
assert report.is_file() and not report.is_symlink() and report.stat().st_size<=32*1024*1024
expected=approval.get('backtest_report_sha256')
assert isinstance(expected,str) and re.fullmatch(r'[0-9a-f]{64}',expected)
actual=hashlib.sha256(report.read_bytes()).hexdigest()
assert actual==expected
assert isinstance(approval.get('approval_id'),str) and re.fullmatch(r'[A-Za-z0-9._:-]{8,128}',approval['approval_id'])
print(report)
PY
)"
[[ -n "$REVIEWED_REPORT" && -f "$REVIEWED_REPORT" ]] || { echo "reviewed backtest report verification failed" >&2; exit 67; }
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
