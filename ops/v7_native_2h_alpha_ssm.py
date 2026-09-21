#!/usr/bin/env python3
"""Run only the native 2H alpha/PnL equity library on London PAPER evidence.

Read-only research transport. No deployment, restart, authentication to trading
venues, order submission, cancellation, promotion, or capital authority.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

from v7_london_ssm_deploy import REGION, run
from v7_direct_action_research_ssm import upload, extract

SHA=re.compile(r"^[0-9a-f]{40}$")
INSTANCE=re.compile(r"^i-[0-9a-f]+$")
CHUNK=14000
SOURCE_PATHS=(
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/native_2h_alpha_library.py",
    "research/walk_forward_v3/multi_alpha_2h.py",
    "research/walk_forward_v3/btc_compact_equity.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v3/dynamic_exit.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/causal_replay.py",
    "scripts/v7_multi_crypto_compact_pm_tape.py",
    "research/requirements-learning.txt",
)

def archive(repo):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tf:
        for rel in SOURCE_PATHS:
            p=repo/rel
            if not p.is_file():raise FileNotFoundError(rel)
            tf.add(p,arcname=rel,recursive=False)
    return buf.getvalue()

def download_large(instance,remote,info):
    size=int(info["bytes"])
    if size<=0 or size>160*1024*1024:
        raise ValueError("native alpha result archive outside bounded size")
    chunks=[]
    for offset in range(0,size,CHUNK):
        code=(
            "import base64;f=open("+repr(remote+"/results.tgz")+",'rb');"
            "f.seek("+str(offset)+");print(base64.b64encode(f.read("+str(CHUNK)+")).decode())"
        )
        stdout,_=run(REGION,instance,"python3 -c "+repr(code),60)
        chunks.append(base64.b64decode(stdout.strip().splitlines()[-1],validate=True))
    payload=b"".join(chunks)
    if len(payload)!=size or hashlib.sha256(payload).hexdigest()!=info["sha256"]:
        raise RuntimeError("native alpha archive identity mismatch")
    return payload

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id",required=True)
    p.add_argument("--source-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    a=p.parse_args()
    if not INSTANCE.fullmatch(a.instance_id):p.error("invalid instance")
    if not SHA.fullmatch(a.source_sha):p.error("exact source SHA required")
    if a.minimum_wall_ns<=0:p.error("positive minimum wall required")

    repo=Path(__file__).resolve().parents[1]
    remote="/tmp/polymarket-native-alpha-2h-"+a.source_sha[:12]
    upload(a.instance_id,remote,archive(repo))
    command=f"""set -euo pipefail
cd {remote}
rm -rf src output results.tgz
mkdir -p src output
RUN_ROOT="$(python3 - <<'PY'
import json,time
from pathlib import Path

patterns=(
    "/mnt/polymarket-data/*/control/runtime_status.json",
    "/home/*/polymarket-runs/*/control/runtime_status.json",
)
candidates=[]
seen=set()
for pattern in patterns:
    for status_path in Path("/").glob(pattern.lstrip("/")):
        try:
            root=status_path.parent.parent.resolve()
            if str(root) in seen:
                continue
            seen.add(str(root))
            state=json.loads(status_path.read_text())
        except Exception:
            continue
        if not (
            state.get("state")=="running"
            and state.get("paper_only") is True
            and state.get("authenticated_execution") is False
            and state.get("real_order_submission") is False
        ):
            continue
        timestamp=int(state.get("timestamp") or 0)
        candidates.append((timestamp,status_path.stat().st_mtime_ns,str(root),state.get("model_sha")))

if not candidates:
    raise SystemExit("NO_RUNNING_PAPER_RUN_ROOT")
candidates.sort(reverse=True)
best=candidates[0]
# Fail closed if two distinct roots report equally fresh active state.
if len(candidates)>1 and candidates[1][:2]==best[:2] and candidates[1][2]!=best[2]:
    raise SystemExit("AMBIGUOUS_RUNNING_PAPER_RUN_ROOT")
print(best[2])
PY
)"
test -n "$RUN_ROOT"
python3 - "$RUN_ROOT" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
status=root/"control/runtime_status.json"
state=json.loads(status.read_text())
assert state.get("state")=="running"
assert state.get("paper_only") is True
assert state.get("authenticated_execution") is False
assert state.get("real_order_submission") is False
print("NATIVE_ALPHA_CONTEXT_FS="+str(root))
PY
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
POLYMARKET_RESEARCH_HORIZONS_MS=5,10,25,50,100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000 \
POLYMARKET_RESEARCH_EXECUTION_LATENCIES_MS=5,10,25,50,100,250 \
PYTHONPATH={remote}/src nice -n 18 venv/bin/python -m research.walk_forward_v3.native_2h_alpha_library \
  --root "$RUN_ROOT" \
  --output-dir {remote}/output \
  --minimum-wall-ns {a.minimum_wall_ns} \
  --skip-figures
python3 - {remote}/output <<'PY'
import csv,gzip,json,sys
from pathlib import Path
root=Path(sys.argv[1])
j=json.load(open(root/'20_native_alpha_library.json'))
assert j['paper_only'] is True
assert j['authenticated_execution'] is False
assert j['real_order_submission'] is False
assert j['window']['profitability_used_for_selection'] is False
assert len(j['alphas'])>=20
assert j['latencies_ms']==[5,10,25,50,100,250]
assert j['exit_horizons_ms']==[500,750,1000,1500,2000,3000,4000,5000,7500,10000]
assert (root/'21_native_alpha_grid.csv').is_file()
assert (root/'22_native_alpha_equity_paths.csv.gz').is_file()
manifest=json.load(open(root/'native_alpha_equity_gallery/gallery_manifest.json'))
assert manifest['alpha_count']>=20
assert manifest['cells_per_alpha']==60
assert manifest['generation_deferred'] is True
assert manifest['figure_count']==0
print('NATIVE_ALPHA_EQUITY_READY')
PY
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/'results.tgz'
print('NATIVE_ALPHA_RESULT='+json.dumps({{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},sort_keys=True))
PY"""
    stdout,_=run(REGION,a.instance_id,command,5400)
    marker=next(line for line in stdout.splitlines() if line.startswith("NATIVE_ALPHA_RESULT="))
    info=json.loads(marker.split("=",1)[1])
    payload=download_large(a.instance_id,remote,info)
    extract(payload,a.output_dir)
    run(REGION,a.instance_id,f"rm -rf {remote}",60)
    print("NATIVE_ALPHA_LOCAL_OUTPUT="+str(a.output_dir))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
