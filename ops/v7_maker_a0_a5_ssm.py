#!/usr/bin/env python3
"""Run exact A0-A5 2H maker research on London PAPER evidence."""
from __future__ import annotations

import argparse
import io
import json
import re
import tarfile
from pathlib import Path

from v7_london_ssm_deploy import REGION, run
from v7_direct_action_research_ssm import remote_context, upload, download, extract

SHA=re.compile(r"^[0-9a-f]{40}$")
INSTANCE=re.compile(r"^i-[0-9a-f]+$")
PATHS=(
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/maker_a0_a5_2h.py",
    "research/walk_forward_v3/multi_alpha_2h.py",
    "research/walk_forward_v3/btc_compact_equity.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v3/dynamic_exit.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/__init__.py",
    "research/economic/causal_replay.py",
    "scripts/v7_multi_crypto_compact_pm_tape.py",
    "research/requirements-learning.txt",
)
REQUIRED=(
    "00_manifest.json","01_grid.json","02_training_receipts.json",
    "03_monotonicity.json","04_grid.csv","05_summary.json",
)


def archive(repo: Path) -> bytes:
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tf:
        for rel in PATHS:
            path=repo/rel
            if not path.is_file():raise FileNotFoundError(rel)
            tf.add(path,arcname=rel,recursive=False)
    return buf.getvalue()


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id",required=True)
    p.add_argument("--expected-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--output-dir",type=Path,required=True)
    a=p.parse_args()
    if not INSTANCE.fullmatch(a.instance_id):p.error("invalid instance")
    if not SHA.fullmatch(a.expected_sha):p.error("exact SHA required")
    repo=Path(__file__).resolve().parents[1]
    context=remote_context(a.instance_id)
    remote="/tmp/polymarket-maker-a0-a5-"+a.expected_sha[:12]
    upload(a.instance_id,remote,archive(repo))
    command=f"""set -euo pipefail
rm -rf {remote}/src {remote}/output
mkdir -p {remote}/src {remote}/output
tar -xzf {remote}/source.tgz -C {remote}/src
python3 -m venv {remote}/venv
{remote}/venv/bin/pip install --disable-pip-version-check --quiet -r {remote}/src/research/requirements-learning.txt
PYTHONPATH={remote}/src:{context['app_dir']} nice -n 18 {remote}/venv/bin/python -m research.walk_forward_v3.maker_a0_a5_2h \
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
m=json.load(open(root/"00_manifest.json"))
g=json.load(open(root/"01_grid.json"))
s=json.load(open(root/"05_summary.json"))
assert m["paper_only"] is True
assert m["authenticated_execution"] is False
assert m["real_order_submission"] is False
assert m["real_capital_at_risk"] is False
assert m["automatic_promotion"] is False
assert m["canonical_ledger_writes"] is False
assert m["code_sha"]==sha
assert m["window_end_ns"]-m["window_start_ns"]==7_200_000_000_000
assert m["latencies_ms"]==[5,10,25,50,100,250]
assert m["holding_horizons_ms"]==[100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000]
assert m["maker_mechanics"]["placement"]=="JOIN"
assert m["maker_mechanics"]["quote_ttl_ms"]==500
assert abs(float(m["maker_mechanics"]["queue_ahead_multiplier"])-1.25)<1e-12
policies=set(("A0_BASELINE","A1_EXTERNAL","A2_PM","A3_EXTERNAL_PM","A4_RESIDUAL","A5_FULL_EXECUTION"))
assert set(g["policies"])==policies
rows=list(csv.DictReader(open(root/"04_grid.csv",newline="")))
assert rows
feature_counts=dict((k,len(v)) for k,v in m["feature_sets"].items())
compact=dict(
  split=m["split"],
  feature_sets=feature_counts,
  summary=s["policies"],
  grid_rows=len(rows),
)
print("A0_A5_COMPACT="+json.dumps(compact,sort_keys=True,separators=(",",":")))
PY
tar -C {remote}/output/a0-a5 -czf {remote}/results.tgz .
python3 - {remote}/results.tgz <<'PY'
from pathlib import Path
import hashlib,json,sys
p=Path(sys.argv[1])
result=dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
print("A0_A5_RESULT="+json.dumps(result,sort_keys=True))
PY"""
    stdout,_=run(REGION,a.instance_id,command,5400)
    marker=next(line for line in stdout.splitlines() if line.startswith("A0_A5_RESULT="))
    info=json.loads(marker.split("=",1)[1])
    payload=download(a.instance_id,remote,info)
    extract(payload,a.output_dir)
    print("\n".join(line for line in stdout.splitlines() if line.startswith(("MAKER_A0_A5_READY=","A0_A5_COMPACT="))))
    run(REGION,a.instance_id,f"rm -rf {remote}",60)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
