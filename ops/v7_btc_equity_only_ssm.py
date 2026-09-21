#!/usr/bin/env python3
"""Run BTC continuous-tape equity replay only on London PAPER evidence."""
from __future__ import annotations
import argparse,hashlib,io,json,re,tarfile
from pathlib import Path
from v7_london_ssm_deploy import REGION,run
from v7_direct_action_research_ssm import remote_context,upload,download,extract

SHA=re.compile(r"^[0-9a-f]{40}$")
INSTANCE=re.compile(r"^i-[0-9a-f]+$")
PATHS=(
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v3/btc_compact_equity.py",
    "scripts/v7_multi_crypto_compact_pm_tape.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/causal_replay.py",
    "research/requirements-learning.txt",
)

def archive(repo):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tf:
        for rel in PATHS:
            tf.add(repo/rel,arcname=rel,recursive=False)
    return buf.getvalue()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--instance-id",required=True)
    p.add_argument("--expected-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    a=p.parse_args()
    if not INSTANCE.fullmatch(a.instance_id): p.error("invalid instance")
    if not SHA.fullmatch(a.expected_sha): p.error("exact sha required")
    repo=Path(__file__).resolve().parents[1]
    ctx=remote_context(a.instance_id)
    remote="/tmp/polymarket-btc-equity-only-"+a.expected_sha[:12]
    upload(a.instance_id,remote,archive(repo))
    cmd=f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
POLYMARKET_RESEARCH_HORIZONS_MS=50,100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000 \
POLYMARKET_RESEARCH_EXECUTION_LATENCIES_MS=5,10,25,50,100,250 \
PYTHONPATH={remote}/src:{ctx['app_dir']} nice -n 18 venv/bin/python -m research.walk_forward_v3.btc_compact_equity \
  --root {ctx['run_root']} \
  --minimum-wall-ns {a.minimum_wall_ns} \
  --output {remote}/output/btc_compact_equity.json
python3 - {remote}/output/btc_compact_equity.json <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))
assert v['schema']=='polymarket_v7_btc_compact_timing_equity_v1'
assert v['state']=='READY'
assert v['paper_only'] is True
assert v['authenticated_execution'] is False
assert v['real_order_submission'] is False
assert v['latencies_ms']==[5,10,25,50,100,250]
assert v['exit_horizons_ms']==[500,750,1000,1500,2000,3000,4000,5000,7500,10000]
print('BTC_EQUITY_ONLY_READY')
print(json.dumps(v['tape_diagnostics'],sort_keys=True))
PY
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
from pathlib import Path
import hashlib,json
p=Path({remote!r})/'results.tgz'
print('BTC_EQUITY_ONLY_RESULT='+json.dumps({{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},sort_keys=True))
PY"""
    stdout,_=run(REGION,a.instance_id,cmd,5400)
    marker=next(line for line in stdout.splitlines() if line.startswith("BTC_EQUITY_ONLY_RESULT="))
    info=json.loads(marker.split("=",1)[1])
    data=download(a.instance_id,remote,info)
    extract(data,a.output_dir)
    run(REGION,a.instance_id,f"rm -rf {remote}",60)
    print("btc_equity_only_output="+str(a.output_dir))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
