#!/usr/bin/env python3
"""Run exact-SHA A0-A5 2H research on London PAPER evidence."""
from __future__ import annotations
import argparse,io,json,re,tarfile
from pathlib import Path
from v7_london_ssm_deploy import REGION,run
from v7_direct_action_research_ssm import remote_context,upload,download,extract

SHA=re.compile(r"^[0-9a-f]{40}$")
INSTANCE=re.compile(r"^i-[0-9a-f]+$")
PATHS=(
 "research/walk_forward_v3/__init__.py",
 "research/walk_forward_v3/a0_a5_maker_2h.py",
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
REQUIRED=(
 "00_summary.json","01_manifest.json","02_training_receipts.json",
 "03_locked_oos_grid.json","04_monotonicity.json","05_dynamic_exit.json",
 "06_descriptive_cells.json","07_locked_oos_table.csv",
)

def archive(repo:Path)->bytes:
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tf:
        for rel in PATHS:
            path=repo/rel
            if not path.is_file(): raise FileNotFoundError(rel)
            tf.add(path,arcname=rel,recursive=False)
    return buf.getvalue()

def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id",required=True)
    p.add_argument("--expected-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    a=p.parse_args()
    if not INSTANCE.fullmatch(a.instance_id): p.error("invalid instance")
    if not SHA.fullmatch(a.expected_sha): p.error("exact SHA required")
    repo=Path(__file__).resolve().parents[1]
    context=remote_context(a.instance_id)
    remote="/tmp/polymarket-a0-a5-"+a.expected_sha[:12]
    upload(a.instance_id,remote,archive(repo))
    command=f"""set -euo pipefail
rm -rf {remote}/src {remote}/output
mkdir -p {remote}/src {remote}/output
tar -xzf {remote}/source.tgz -C {remote}/src
python3 -m venv {remote}/venv
{remote}/venv/bin/pip install --disable-pip-version-check --quiet -r {remote}/src/research/requirements-learning.txt
PYTHONPATH={remote}/src:{context['app_dir']} nice -n 18 {remote}/venv/bin/python -m research.walk_forward_v3.a0_a5_maker_2h \
  --root {context['run_root']} \
  --minimum-wall-ns {a.minimum_wall_ns} \
  --code-sha {a.expected_sha} \
  --output-dir {remote}/output/a0-a5
python3 - {remote}/output/a0-a5 {a.expected_sha} <<'PY'
import csv,json,sys
from pathlib import Path
root=Path(sys.argv[1]);sha=sys.argv[2]
required={json.dumps(list(REQUIRED))}
missing=[x for x in required if not (root/x).is_file()]
assert not missing,missing
m=json.load(open(root/'01_manifest.json'))
g=json.load(open(root/'03_locked_oos_grid.json'))
assert m['paper_only'] is True
assert m['authenticated_execution'] is False
assert m['real_order_submission'] is False
assert m['automatic_promotion'] is False
assert m['code_sha']==sha
assert m['window_end_ns']-m['window_start_ns']==7200*10**9
assert m['split']['train_60']['rows']>0 and m['split']['locked_oos_40']['rows']>0
assert m['latencies_ms']==[5,10,25,50,100,250]
assert m['horizons_ms']==[100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000]
rows=g['rows']
policies=sorted(set(r['policy'] for r in rows))
assert 'A0_BASELINE_NO_ADDED_ALPHA' in policies
assert 'A1_EXTERNAL_MOMENTUM' in policies
assert 'A2_PM_MICROSTRUCTURE' in policies
assert 'A3_EXTERNAL_PLUS_PM' in policies
assert 'A4_RESIDUAL_MEAN_REVERSION' in policies or json.load(open(root/'02_training_receipts.json'))['A4_residual']['state']=='INSUFFICIENT_DATA'
assert 'A5_FULL_EXECUTION_ALPHA' in policies or json.load(open(root/'02_training_receipts.json'))['models']['A5_FULL_EXECUTION_ALPHA']['state']=='INSUFFICIENT_DATA'
table=list(csv.DictReader(open(root/'07_locked_oos_table.csv')))
assert len(table)==len(rows)
print('A0_A5_PACKAGE_OK')
PY
tar -C {remote}/output/a0-a5 -czf {remote}/results.tgz .
python3 - {remote}/results.tgz <<'PY'
from pathlib import Path
import hashlib,json,sys
p=Path(sys.argv[1])
print('A0_A5_RESULT='+json.dumps(dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()),sort_keys=True))
PY"""
    stdout,_=run(REGION,a.instance_id,command,5400)
    marker=next(line for line in stdout.splitlines() if line.startswith("A0_A5_RESULT="))
    info=json.loads(marker.split("=",1)[1])
    payload=download(a.instance_id,remote,info)
    extract(payload,a.output_dir)
    run(REGION,a.instance_id,f"rm -rf {remote}",60)
    print("a0_a5_output="+str(a.output_dir))
    return 0

if __name__=="__main__": raise SystemExit(main())
