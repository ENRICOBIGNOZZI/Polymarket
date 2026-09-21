#!/usr/bin/env python3
"""Run the frozen two-hour multi-alpha research pass on London PAPER evidence.

This is a read-only research transport. It uploads code to /tmp, reads existing
PAPER evidence, downloads research artifacts, and removes the temporary bundle.
It never deploys/restarts trading services or submits/cancels orders.
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

SHA = re.compile(r"^[0-9a-f]{40}$")
INSTANCE = re.compile(r"^i-[0-9a-f]+$")
SOURCE_PATHS = (
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/multi_alpha_2h.py",
    "research/walk_forward_v3/native_2h_alpha_library.py",
    "research/walk_forward_v3/multi_alpha_2h_figures.py",
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
            path=repo/rel
            if not path.is_file():
                raise FileNotFoundError(rel)
            tf.add(path,arcname=rel,recursive=False)
    return buf.getvalue()

def load_request(path):
    value=json.loads(Path(path).read_text(encoding="utf-8"))
    expected={
        "schema","version","request_id","instance_id","expected_research_sha",
        "baseline_code_sha","minimum_wall_ns","output_directory",
        "paper_only","authenticated_execution","real_order_submission",
        "real_capital_at_risk","automatic_promotion",
    }
    if set(value)!=expected:
        raise ValueError("unexpected request fields")
    if value["schema"]!="polymarket_v7_multi_alpha_2h_request_v1" or value["version"]!=1:
        raise ValueError("invalid request identity")
    if not INSTANCE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance")
    if not SHA.fullmatch(value["expected_research_sha"]) or not SHA.fullmatch(value["baseline_code_sha"]):
        raise ValueError("exact SHA required")
    if not isinstance(value["minimum_wall_ns"],int) or value["minimum_wall_ns"]<=0:
        raise ValueError("invalid minimum wall")
    if value["output_directory"]!="docs/research/multi-alpha-2h-2026-09-21":
        raise ValueError("unexpected output directory")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
        and value["automatic_promotion"] is False
    ):
        raise ValueError("PAPER-only authority contract violated")
    return value

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("request",type=Path)
    p.add_argument("source_sha")
    a=p.parse_args()
    if not SHA.fullmatch(a.source_sha):
        p.error("exact source SHA required")
    req=load_request(a.request)
    if req["expected_research_sha"]!=a.source_sha:
        raise ValueError("request/source SHA mismatch")

    repo=Path(__file__).resolve().parents[1]
    ctx=remote_context(req["instance_id"])
    remote="/tmp/polymarket-multi-alpha-2h-"+a.source_sha[:12]
    upload(req["instance_id"],remote,archive(repo))
    command=f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
POLYMARKET_RESEARCH_HORIZONS_MS=5,10,25,50,100,250,500,750,1000,1500,2000,3000,4000,5000,7500,10000 \
POLYMARKET_RESEARCH_EXECUTION_LATENCIES_MS=5,10,25,50,100,250 \
PYTHONPATH={remote}/src:{ctx['app_dir']} nice -n 18 venv/bin/python -m research.walk_forward_v3.multi_alpha_2h \
  --root {ctx['run_root']} \
  --output-dir {remote}/output \
  --minimum-wall-ns {req['minimum_wall_ns']} \
  --baseline-code-sha {req['baseline_code_sha']} \
  --source-sha {a.source_sha}
PYTHONPATH={remote}/src:{ctx['app_dir']} nice -n 18 venv/bin/python -m research.walk_forward_v3.native_2h_alpha_library \\
  --root {ctx['run_root']} \\
  --output-dir {remote}/output \\
  --minimum-wall-ns {req['minimum_wall_ns']}
PYTHONPATH={remote}/src:{ctx['app_dir']} venv/bin/python -m research.walk_forward_v3.multi_alpha_2h_figures \\
  --root {remote}/output
python3 - {remote}/output <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
manifest=json.load(open(root/'01_2h_manifest.json'))
baseline=json.load(open(root/'04_baseline_2h.json'))
short=json.load(open(root/'17_shortlist.json'))
assert manifest['paper_only'] is True
assert manifest['authenticated_execution'] is False
assert manifest['real_order_submission'] is False
assert manifest['window_end_ns']-manifest['window_start_ns']==7200*10**9
assert manifest['selection_policy']=='DATA_QUALITY_ONLY_NO_PNL'
assert baseline['paper_only'] is True
assert short['automatic_promotion'] is False
required=[f'{{i:02d}}_' for i in range(20)]
names=[p.name for p in root.iterdir()]
for prefix in required:
    assert any(name.startswith(prefix) for name in names), prefix
figures=[
'01_baseline_equity.png','02_best_enriched_equity.png','03_baseline_vs_enriched_equity.png',
'04_baseline_entry_exit_heatmap.png','05_rich_entry_exit_heatmap.png','06_incremental_heatmap.png',
'07_information_latency_frontier.png','08_signal_decay.png','09_momentum_reversal_map.png',
'10_alpha_family_contribution.png','11_dynamic_exit.png','12_pnl_by_asset.png','13_pnl_by_contract_horizon.png']
for name in figures:
    assert (root/name).is_file(), name
assert (root/'figures_manifest.json').is_file()
print('MULTI_ALPHA_ARTIFACTS_VALID')
PY
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/'results.tgz'
print('MULTI_ALPHA_RESULT='+json.dumps({{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},sort_keys=True))
PY"""
    stdout,_=run(REGION,req["instance_id"],command,5400)
    marker=next(line for line in stdout.splitlines() if line.startswith("MULTI_ALPHA_RESULT="))
    info=json.loads(marker.split("=",1)[1])
    payload=download(req["instance_id"],remote,info)
    extract(payload,repo/req["output_directory"])
    run(REGION,req["instance_id"],f"rm -rf {remote}",60)
    print("MULTI_ALPHA_LOCAL_OUTPUT="+req["output_directory"])
    return 0

if __name__=="__main__":
    raise SystemExit(main())
