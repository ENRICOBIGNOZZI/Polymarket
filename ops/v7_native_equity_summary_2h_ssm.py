#!/usr/bin/env python3
"""Read-only native 2H equity summary; no artifact transfer or rendering."""
from __future__ import annotations
import argparse,io,json,re,tarfile
from pathlib import Path
from v7_london_ssm_deploy import REGION,run
from v7_direct_action_research_ssm import remote_context,upload

SHA=re.compile(r"^[0-9a-f]{40}$")
INSTANCE=re.compile(r"^i-[0-9a-f]+$")
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
    b=io.BytesIO()
    with tarfile.open(fileobj=b,mode="w:gz") as t:
        for rel in PATHS:
            p=repo/rel
            if not p.is_file():raise FileNotFoundError(rel)
            t.add(p,arcname=rel,recursive=False)
    return b.getvalue()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--instance-id",required=True)
    p.add_argument("--source-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args()
    if not INSTANCE.fullmatch(a.instance_id):p.error("invalid instance")
    if not SHA.fullmatch(a.source_sha):p.error("invalid sha")
    repo=Path(__file__).resolve().parents[1]
    ctx=remote_context(a.instance_id)
    remote="/tmp/pm-native-summary-"+a.source_sha[:12]
    upload(a.instance_id,remote,archive(repo))
    cmd=f"""set -euo pipefail
cd {remote}
rm -rf src output venv
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
POLYMARKET_RESEARCH_HORIZONS_MS=5,10,25,50,100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000 \
POLYMARKET_RESEARCH_EXECUTION_LATENCIES_MS=5,10,25,50,100,250 \
PYTHONPATH={remote}/src:{ctx['app_dir']} nice -n 18 venv/bin/python -m research.walk_forward_v3.native_equity_grid_2h \
 --root {ctx['run_root']} --output-dir {remote}/output \
 --minimum-wall-ns {a.minimum_wall_ns} --code-sha {a.source_sha} --skip-gallery >/dev/null
python3 - {remote}/output <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
m=json.load(open(root/'manifest.json'))
g=json.load(open(root/'equity_grid.json'))['results']
s=json.load(open(root/'summary.json'))['strategies']
keep=('BASELINE','CONTINUATION','REVERSAL')
grids={{
 name:{{key:cell.get('total_pnl') for key,cell in g[name]['metrics'].items()}}
 for name in keep
}}
details={{}}
for name,res in g.items():
 valid=[(k,v) for k,v in res['metrics'].items() if isinstance(v.get('total_pnl'),(int,float))]
 if valid:
  best=max(valid,key=lambda kv:kv[1]['total_pnl'])
  worst=min(valid,key=lambda kv:kv[1]['total_pnl'])
  details[name]={{
   'best_cell':best[0],'best_pnl':best[1]['total_pnl'],'best_fills':best[1]['fills'],
   'best_hit_rate':best[1]['hit_rate'],'best_drawdown':best[1]['max_drawdown'],
   'worst_cell':worst[0],'worst_pnl':worst[1]['total_pnl'],
  }}
out={{
 'window_start_ns':m['window_start_ns'],'window_end_ns':m['window_end_ns'],
 'rows':m['rows'],'assets':m['assets'],'contract_horizons':m['contract_horizons'],
 'selection':m['selection'],'grids':grids,'strategies':details,
}}
print('NATIVE_2H_SUMMARY='+json.dumps(out,separators=(',',':'),sort_keys=True))
PY
rm -rf {remote}
"""
    stdout,_=run(REGION,a.instance_id,cmd,1800)
    line=next(x for x in stdout.splitlines() if x.startswith("NATIVE_2H_SUMMARY="))
    value=json.loads(line.split("=",1)[1])
    print("NATIVE_2H_SUMMARY="+json.dumps(value,sort_keys=True))
    return 0
if __name__=="__main__":raise SystemExit(main())
