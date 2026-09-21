#!/usr/bin/env python3
"""Run fast native 2H equity-grid research on London PAPER evidence."""
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
from v7_direct_action_research_ssm import remote_context, upload, extract

SHA=re.compile(r"^[0-9a-f]{40}$")
INSTANCE=re.compile(r"^i-[0-9a-f]+$")
CHUNK=9000
PATHS=(
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/native_equity_grid_2h.py",
    "research/walk_forward_v3/btc_compact_equity.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/causal_replay.py",
    "scripts/v7_multi_crypto_compact_pm_tape.py",
    "research/requirements-learning.txt",
)

def archive(repo):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tf:
        for rel in PATHS:
            path=repo/rel
            if not path.is_file():
                raise FileNotFoundError(rel)
            tf.add(path,arcname=rel,recursive=False)
    return buf.getvalue()


def download_large(instance,remote,info):
    size=int(info["bytes"])
    if size<=0 or size>160*1024*1024:
        raise ValueError("native equity archive outside bounded research size")
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
        raise RuntimeError("native equity archive identity mismatch")
    return payload

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id",required=True)
    p.add_argument("--expected-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    a=p.parse_args()
    if not INSTANCE.fullmatch(a.instance_id): p.error("invalid instance")
    if not SHA.fullmatch(a.expected_sha): p.error("exact SHA required")

    repo=Path(__file__).resolve().parents[1]
    ctx=remote_context(a.instance_id)
    remote="/tmp/polymarket-native-equity-2h-"+a.expected_sha[:12]
    upload(a.instance_id,remote,archive(repo))
    command=f"""set -euo pipefail
cd {remote}
rm -rf src output venv
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
POLYMARKET_RESEARCH_HORIZONS_MS=5,10,25,50,100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000 \
POLYMARKET_RESEARCH_EXECUTION_LATENCIES_MS=5,10,25,50,100,250 \
PYTHONPATH={remote}/src:{ctx['app_dir']} nice -n 18 venv/bin/python -m research.walk_forward_v3.native_equity_grid_2h \
  --root {ctx['run_root']} \
  --output-dir {remote}/output \
  --minimum-wall-ns {a.minimum_wall_ns} \
  --code-sha {a.expected_sha} \
  --skip-gallery
python3 - {remote}/output <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
manifest=json.load(open(root/'manifest.json'))
summary=json.load(open(root/'summary.json'))
assert manifest['paper_only'] is True
assert manifest['authenticated_execution'] is False
assert manifest['real_order_submission'] is False
assert manifest['window_end_ns']-manifest['window_start_ns']==7200*10**9
assert manifest['selection']['profitability_used_for_selection'] is False
assert manifest['evidence_source']=='NATIVE_KIND6_CAUSAL_REPRICING'
assert len(summary['strategies'])==11
assert (root/'equity_grid.json').is_file()
assert (root/'pnl_grid.csv').is_file()
print('NATIVE_EQUITY_2H_READY')
print(json.dumps({{'window_start_ns':manifest['window_start_ns'],'window_end_ns':manifest['window_end_ns'],'rows':manifest['rows'],'assets':manifest['assets'],'summary':summary['strategies']}},sort_keys=True))
PY
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
from pathlib import Path
import hashlib,json
p=Path({remote!r})/'results.tgz'
print('NATIVE_EQUITY_RESULT='+json.dumps({{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},sort_keys=True))
PY"""
    stdout,_=run(REGION,a.instance_id,command,5400)
    marker=next(x for x in stdout.splitlines() if x.startswith("NATIVE_EQUITY_RESULT="))
    info=json.loads(marker.split("=",1)[1])
    payload=download_large(a.instance_id,remote,info)
    extract(payload,a.output_dir)
    from research.walk_forward_v3.native_equity_grid_2h import render_gallery
    local_results=json.loads((a.output_dir/"equity_grid.json").read_text(encoding="utf-8"))["results"]
    render_gallery(a.output_dir,local_results)
    run(REGION,a.instance_id,f"rm -rf {remote}",60)
    print("native_equity_output="+str(a.output_dir))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
