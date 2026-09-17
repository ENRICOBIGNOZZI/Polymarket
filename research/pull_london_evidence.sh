#!/usr/bin/env bash
set -euo pipefail
REMOTE="${POLYMARKET_LONDON_HOST:?POLYMARKET_LONDON_HOST required}"; USER="${POLYMARKET_LONDON_USER:-enrico}"; REMOTE_ROOT="${POLYMARKET_LONDON_RUN_ROOT:-/home/$USER/polymarket-runs/paper_v7_london}"
LOCAL_ROOT="${PM_V7_RESEARCH_SYNC_ROOT:-$HOME/polymarket-research/london}"
mkdir -p "$LOCAL_ROOT/current" "$LOCAL_ROOT/receipts"
# Copy raw crypto evidence and canonical execution state. Partial files remain
# partial and are never zero-filled; rsync's temp-file semantics avoid torn replacements.
rsync -a --partial --append-verify \
  --include='/external_fair/***' --include='/universe/***' --include='/micro_maker/***' \
  --include='/research/***' \
  --include='/ledger/***' --include='/trade_tape.csv*' --include='/control/runtime_identity.json' \
  --include='/control/runtime_artifact_receipt.json' --exclude='*' \
  "$USER@$REMOTE:$REMOTE_ROOT/" "$LOCAL_ROOT/current/"
receipt_tmp="$(mktemp)"
python3 - "$LOCAL_ROOT" "$REMOTE" "$receipt_tmp" <<'PY'
import fnmatch,hashlib,json,sys,time
from pathlib import Path
root=Path(sys.argv[1]); current=root/'current'; cutoff=time.time_ns(); patterns=['external_fair/raw/*.bin*','external_fair/normalized_events/*.bin*','trade_tape.csv.*','micro_maker/book_observations/*.jsonl.*','micro_maker/fillability_ws.jsonl.*','**/*.log.*','**/*.log.gz']
def dig(p):
 h=hashlib.sha256();
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()
files=[]
for p in current.rglob('*'):
 if not p.is_file():continue
 rel=str(p.relative_to(current)); st=p.stat()
 if st.st_mtime_ns<=cutoff and any(fnmatch.fnmatch(rel,x) for x in patterns): files.append({'path':rel,'size':st.st_size,'sha256':dig(p),'mtime_ns':st.st_mtime_ns})
total=sum(p.stat().st_size for p in current.rglob('*') if p.is_file())
v={'schema':'polymarket_v7_research_offload_receipt_v1','timestamp_ns':time.time_ns(),'source_host':sys.argv[2],'synced_through_ns':cutoff,'bytes_local':total,'complete_command':True,'zero_fill_missing':False,'files':files}
Path(sys.argv[3]).write_text(json.dumps(v,sort_keys=True,indent=2)+'\n'); local=root/'receipts'/f"sync-{v['timestamp_ns']}.json";local.write_text(json.dumps(v,sort_keys=True,indent=2)+'\n');(root/'latest_sync_receipt.json').write_text(json.dumps(v,sort_keys=True,indent=2)+'\n');print(json.dumps({'bytes_local':total,'verified_closed_files':len(files),'synced_through_ns':cutoff},sort_keys=True))
PY
scp "$receipt_tmp" "$USER@$REMOTE:$REMOTE_ROOT/control/research_offload_receipt.json.tmp"
ssh "$USER@$REMOTE" "mv '$REMOTE_ROOT/control/research_offload_receipt.json.tmp' '$REMOTE_ROOT/control/research_offload_receipt.json'"
rm -f "$receipt_tmp"
