#!/usr/bin/env bash
set -euo pipefail
TARGET_SHA="${1:?target SHA required}"; LOCAL_ROOT="${PM_V7_RESEARCH_ARTIFACT_ROOT:-$HOME/polymarket-research/runtime_artifacts}"
REMOTE="${POLYMARKET_LONDON_HOST:?POLYMARKET_LONDON_HOST required}"; USER="${POLYMARKET_LONDON_USER:-enrico}"; REMOTE_ROOT="${POLYMARKET_LONDON_ARTIFACT_ROOT:-/home/$USER/polymarket-artifacts}"
BUNDLE="$LOCAL_ROOT/by-sha/$TARGET_SHA"; [[ -f "$BUNDLE/manifest.json" ]] || { echo "missing bundle" >&2; exit 66; }
python3 - "$BUNDLE/candidate_validation.json" "$TARGET_SHA" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]));assert v.get('schema')=='polymarket_v7_candidate_validation_v1';assert v.get('target_sha')==sys.argv[2];assert v.get('state')=='PROMOTABLE' and v.get('promotable') is True;assert v.get('automatic_deployment') is False
PY
expected="$(python3 - "$BUNDLE/manifest.json" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); print(v['target_model_sha'])
PY
)"; [[ "$expected" == "$TARGET_SHA" ]]
ssh "$USER@$REMOTE" "mkdir -p '$REMOTE_ROOT/by-sha'"
rsync -a --delete "$BUNDLE/" "$USER@$REMOTE:$REMOTE_ROOT/by-sha/$TARGET_SHA.tmp/"
ssh "$USER@$REMOTE" "set -e; rm -rf '$REMOTE_ROOT/by-sha/$TARGET_SHA'; mv '$REMOTE_ROOT/by-sha/$TARGET_SHA.tmp' '$REMOTE_ROOT/by-sha/$TARGET_SHA'; ln -sfn 'by-sha/$TARGET_SHA' '$REMOTE_ROOT/current'; test -s '$REMOTE_ROOT/current/manifest.json'"
echo "pushed_runtime_artifact_sha=$TARGET_SHA"
