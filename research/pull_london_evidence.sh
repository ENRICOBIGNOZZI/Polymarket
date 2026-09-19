#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${POLYMARKET_LONDON_HOST:?POLYMARKET_LONDON_HOST required}"; USER="${POLYMARKET_LONDON_USER:-enrico}"; REMOTE_ROOT="${POLYMARKET_LONDON_RUN_ROOT:-/home/$USER/polymarket-runs/paper_v7_london}"
LOCAL_ROOT="${PM_V7_RESEARCH_SYNC_ROOT:-$HOME/polymarket-research/london}"
mkdir -p "$LOCAL_ROOT/current" "$LOCAL_ROOT/receipts"
# Copy raw crypto evidence and canonical execution state. Partial files remain
# partial and are never zero-filled. Mutable snapshots are NOT append-only;
# normal delta transfer handles same-size rewrites and shrinking snapshots.
# Keep incomplete downloads outside the canonical filename until successful.
rsync -a --partial-dir=.rsync-partial \
  --include='/external_fair/***' --include='/universe/***' --include='/micro_maker/***' \
  --include='/research/***' \
  --include='/ledger/***' --include='/trade_tape.csv*' --include='/control/' \
  --include='/control/runtime_identity.json' \
  --include='/control/runtime_artifact_receipt.json' --exclude='*' \
  "$USER@$REMOTE:$REMOTE_ROOT/" "$LOCAL_ROOT/current/"
receipt_tmp="$(mktemp)"
python3 - "$LOCAL_ROOT" "$REMOTE" "$receipt_tmp" "$ROOT/config/v7_london_buffer_retention.json" <<'PY'
import fnmatch,hashlib,json,sys,time
from pathlib import Path
root=Path(sys.argv[1]); current=root/'current'; cutoff=time.time_ns(); policy=json.loads(Path(sys.argv[4]).read_text()); patterns=list(policy['closed_segment_patterns']); never=set(policy.get('never_delete') or [])
def dig(p):
 h=hashlib.sha256();
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()
files=[]
for p in current.rglob('*'):
 if not p.is_file() or p.is_symlink():continue
 rel=str(p.relative_to(current)); st=p.stat()
 if '.rsync-partial' in p.parts or rel in never or rel.endswith('.open') or (rel.endswith('.bin') and '.segment-' not in p.name): continue
 if st.st_mtime_ns<=cutoff and any(fnmatch.fnmatch(rel,x) for x in patterns): files.append({'path':rel,'size':st.st_size,'sha256':dig(p),'mtime_ns':st.st_mtime_ns})
total=sum(p.stat().st_size for p in current.rglob('*') if p.is_file())
v={'schema':'polymarket_v7_research_offload_receipt_v1','timestamp_ns':time.time_ns(),'source_host':sys.argv[2],'synced_through_ns':cutoff,'bytes_local':total,'complete_command':True,'zero_fill_missing':False,'files':files}
Path(sys.argv[3]).write_text(json.dumps(v,sort_keys=True,indent=2)+'\n'); local=root/'receipts'/f"sync-{v['timestamp_ns']}.json";local.write_text(json.dumps(v,sort_keys=True,indent=2)+'\n');(root/'latest_sync_receipt.json').write_text(json.dumps(v,sort_keys=True,indent=2)+'\n');print(json.dumps({'bytes_local':total,'verified_closed_files':len(files),'synced_through_ns':cutoff},sort_keys=True))
PY
scp "$receipt_tmp" "$USER@$REMOTE:$REMOTE_ROOT/control/research_offload_receipt.json.tmp"
ssh "$USER@$REMOTE" "mv '$REMOTE_ROOT/control/research_offload_receipt.json.tmp' '$REMOTE_ROOT/control/research_offload_receipt.json'"
rm -f "$receipt_tmp"
