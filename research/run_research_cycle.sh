#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${PM_V7_RESEARCH_WORKTREE_ROOT:-$HOME/.cache/polymarket-research-worktrees}"
SYNC="${PM_V7_RESEARCH_SYNC_ROOT:-$HOME/polymarket-research/london}"
ARTIFACTS="${PM_V7_RESEARCH_ARTIFACT_ROOT:-$HOME/polymarket-research/runtime_artifacts}"
STATUS="${PM_V7_RESEARCH_STATUS:-$HOME/polymarket-research/research_cycle_status.json}"
LOCK="${PM_V7_RESEARCH_LOCK:-$HOME/polymarket-research/research-cycle.lock}"
mkdir -p "$CACHE" "$(dirname "$STATUS")" "$(dirname "$LOCK")"
if ! mkdir "$LOCK" 2>/dev/null; then echo "research cycle already active" >&2; exit 75; fi
work=""
cleanup(){ set +e; [[ -n "$work" ]] && git -C "$ROOT" worktree remove --force "$work" >/dev/null 2>&1; rmdir "$LOCK" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

git -C "$ROOT" fetch --no-tags origin main
SHA="${1:-$(git -C "$ROOT" rev-parse origin/main)}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 64; }

# Sync first; missing observations are never converted to zero.
PM_V7_RESEARCH_SYNC_ROOT="$SYNC" "$ROOT/research/pull_london_evidence.sh"
work="$CACHE/$SHA-$$"; git -C "$ROOT" worktree add --detach "$work" "$SHA" >/dev/null
PM_V7_RESEARCH_SOURCE_ROOT="$SYNC/current" \
PM_V7_RESEARCH_ARTIFACT_ROOT="$ARTIFACTS" \
  "$work/research/build_runtime_artifacts.sh" "$SHA"
PM_V7_RESEARCH_RUN_ROOT="$SYNC/current" \
  "$work/research/run_offline_analytics.sh" || true
PM_V7_RESEARCH_ARTIFACT_ROOT="$ARTIFACTS" \
  "$work/research/push_runtime_artifacts.sh" "$SHA"
python3 - "$STATUS" "$SHA" "$SYNC/latest_sync_receipt.json" "$ARTIFACTS/by-sha/$SHA/manifest.json" <<'PY'
import json,sys,time
from pathlib import Path
out,sha,sync,artifact=map(Path,sys.argv[1:]) if False else (Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3]),Path(sys.argv[4]))
s=json.loads(sync.read_text()); a=json.loads(artifact.read_text())
v={'schema':'polymarket_v7_research_cycle_status_v1','timestamp_ns':time.time_ns(),'state':'PROMOTED_TO_LONDON','target_sha':sha,'synced_through_ns':s['synced_through_ns'],'artifact_bundle_id':a['bundle_id'],'paper_only':True,'execution_authority':False}
out.write_text(json.dumps(v,sort_keys=True,indent=2)+'\n')
PY
cat "$STATUS"
