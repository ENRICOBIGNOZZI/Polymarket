#!/usr/bin/env python3
"""Run rich-history net-PnL research on London PAPER evidence.

Read-only research transport. No deployment, restart, exchange authentication,
order submission/cancellation, promotion or real capital authority.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import tarfile

from v7_london_ssm_deploy import REGION, run
from v7_direct_action_research_ssm import remote_context, upload, download, extract

SHA=re.compile(r"^[0-9a-f]{40}$")
INSTANCE=re.compile(r"^i-[0-9a-f]+$")

SOURCE_PATHS=(
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/rich_history_information.py",
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

HORIZONS=tuple(sorted(set(
    (5,10,25,50,100,250,500,750,1000,1250,1500,1750,2000,3000,4000,5000,7500,10000)
)))
LATENCIES=(5,10,25,50,100,250,500,750,1000)


def archive(repo):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode="w:gz") as tf:
        for rel in SOURCE_PATHS:
            path=repo/rel
            if not path.is_file():
                raise FileNotFoundError(rel)
            tf.add(path,arcname=rel,recursive=False)
    return buf.getvalue()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-id",required=True)
    p.add_argument("--source-sha",required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--maximum-history-hours",type=float,default=168.0)
    p.add_argument("--output-dir",type=Path,required=True)
    a=p.parse_args()
    if not INSTANCE.fullmatch(a.instance_id):p.error("invalid instance")
    if not SHA.fullmatch(a.source_sha):p.error("exact source SHA required")
    if a.minimum_wall_ns<=0:p.error("positive minimum wall required")
    if a.maximum_history_hours<=0:p.error("positive history hours required")

    repo=Path(__file__).resolve().parents[1]
    ctx=remote_context(a.instance_id)
    remote="/tmp/polymarket-rich-history-"+a.source_sha[:12]
    upload(a.instance_id,remote,archive(repo))
    horizons=",".join(map(str,HORIZONS))
    latencies=",".join(map(str,LATENCIES))
    command=f"""set -euo pipefail
cd {remote}
rm -rf src output results.tgz venv
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
POLYMARKET_RESEARCH_HORIZONS_MS={horizons} \
POLYMARKET_RESEARCH_EXECUTION_LATENCIES_MS={latencies} \
PYTHONPATH={remote}/src:{ctx['app_dir']} nice -n 18 venv/bin/python -m research.walk_forward_v3.rich_history_information \
  --root {ctx['run_root']} \
  --output-dir {remote}/output \
  --minimum-wall-ns {a.minimum_wall_ns} \
  --maximum-history-hours {a.maximum_history_hours}
python3 - {remote}/output <<'PY'
import csv,json,sys
from pathlib import Path
root=Path(sys.argv[1])
m=json.load(open(root/'30_rich_history_manifest.json'))
assert m['paper_only'] is True
assert m['authenticated_execution'] is False
assert m['real_order_submission'] is False
assert m['real_capital_at_risk'] is False
assert m['automatic_promotion'] is False
assert m['window']['profitability_used_for_selection'] is False
assert m['selection_guards']['history_selected_on_pnl'] is False
assert m['selection_guards']['lambda_selected_on_test_pnl'] is False
assert m['selection_guards']['tau_tuned_on_test'] is False
assert m['selection_guards']['missing_as_zero'] is False
assert len(m['latencies_ms']) >= 6
assert len(m['requested_exit_horizons_ms']) >= 16
for name in (
    '31_rich_data_coverage_by_crypto.json',
    '32_rich_information_models.json',
    '33_rich_pooled_grid.csv',
    '34_rich_crypto_grid.csv',
    '35_rich_incremental_information.json',
    '36_rich_fill_adequacy.json',
    '37_rich_equity_1m.csv.gz',
):
    assert (root/name).is_file(), name
with open(root/'34_rich_crypto_grid.csv',newline='') as f:
    rows=list(csv.DictReader(f))
assert rows
assets={{r['asset'] for r in rows}}
assert len(assets)>=2
print('RICH_HISTORY_ARTIFACTS_VALID')
PY
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/'results.tgz'
print('RICH_HISTORY_RESULT='+json.dumps({{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},sort_keys=True))
PY"""
    stdout,_=run(REGION,a.instance_id,command,10800)
    marker=next(line for line in stdout.splitlines() if line.startswith("RICH_HISTORY_RESULT="))
    info=json.loads(marker.split("=",1)[1])
    payload=download(a.instance_id,remote,info)
    extract(payload,a.output_dir)
    run(REGION,a.instance_id,f"rm -rf {remote}",60)
    print("RICH_HISTORY_LOCAL_OUTPUT="+str(a.output_dir))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
