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
    "research/walk_forward_v3/maker_market_metadata.py",
    "research/walk_forward_v3/multi_alpha_2h.py",
    "research/walk_forward_v3/btc_compact_equity.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v3/dynamic_exit.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/causal_replay.py",
    "research/tools/v7_external_event_export.cpp",
    "include/pm/v7_external_tape.hpp",
    "include/pm/v7_external_fair.hpp",
    "include/pm/v7_spsc.hpp",
    "include/pm/v7_intent.hpp",
    "scripts/v7_multi_crypto_compact_pm_tape.py",
    "config/v7_crypto_settlement_markets.json",
    "research/requirements-learning.txt",
)
REQUIRED=(
    "00_manifest.json","01_grid.json","02_training_receipts.json",
    "03_monotonicity.json","04_grid.csv","05_summary.json",
    "06_paired_vs_a0.json",
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
python3 - {context['run_root']} <<'PYDISC'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
data=root.parent
dirs=[]
seen=set()
candidates=[
    root/"research"/"repricing_book"/"book_observations",
    root/"archive"/"repricing-book",
]
if data.is_dir():
    for name in ("book_observations","repricing-book"):
        try:
            candidates.extend(data.rglob(name))
        except OSError:
            pass
for directory in candidates:
    try:
        directory=directory.resolve()
    except OSError:
        continue
    if str(directory) in seen or not directory.is_dir() or directory.is_symlink():
        continue
    seen.add(str(directory))
    files=[]
    total=0
    for pattern in ("*.jsonl","*.jsonl.gz","*.gz","*.bin","*.bin.open"):
        for p in directory.glob(pattern):
            if p.is_file() and not p.is_symlink():
                try:
                    size=p.stat().st_size
                except OSError:
                    continue
                files.append((p.name,size))
                total+=size
    if files:
        files.sort()
        dirs.append(dict(
            path=str(directory),
            files=len(files),
            bytes=total,
            first=files[0][0],
            last=files[-1][0],
        ))
dirs.sort(key=lambda x:x["path"])
print("A0_A5_DATA_DISCOVERY="+json.dumps(dict(
    run_root=str(root),data_root=str(data),dirs=dirs[:80],
),sort_keys=True,separators=(",",":")))
PYDISC
python3 -m venv {remote}/venv
{remote}/venv/bin/pip install --disable-pip-version-check --quiet -r {remote}/src/research/requirements-learning.txt
PYTHONPATH={remote}/src:{context['app_dir']} {remote}/venv/bin/python -m research.walk_forward_v3.maker_market_metadata \
  --run-root {context['run_root']} \
  --registry {remote}/src/config/v7_crypto_settlement_markets.json \
  --minimum-wall-ns {a.minimum_wall_ns} \
  --output {remote}/market_metadata.json
c++ -std=c++20 -O2 -I{remote}/src/include \
  {remote}/src/research/tools/v7_external_event_export.cpp \
  -o {remote}/v7_external_event_export
END_WALL_NS="$(python3 -c 'import time; print(time.time_ns())')"
python3 - {context['run_root']} {remote} {a.minimum_wall_ns} "$END_WALL_NS" {remote}/v7_external_event_export <<'PYEXT'
import json,subprocess,sys
from pathlib import Path
run_root=Path(sys.argv[1]); remote=Path(sys.argv[2])
start=int(sys.argv[3]); end=int(sys.argv[4]); exporter=sys.argv[5]
metadata=json.loads((remote/"market_metadata.json").read_text(encoding="utf-8"))
assets=sorted({{str(row.get("asset") or "").upper() for row in metadata.get("markets") or [] if row.get("asset")}})
if not assets:
    raise SystemExit("NO_METADATA_ASSETS")
lines=[]
for asset in assets:
    base=run_root/"external_fair" if asset=="BTC" else run_root/"external_fair"/"assets"/asset.lower()
    directory=base/"normalized_events"
    paths=[]
    if directory.is_dir():
        paths=sorted(
            p for p in directory.iterdir()
            if p.is_file() and (p.name.endswith(".bin") or p.name.endswith(".bin.open"))
        )
    if not paths:
        continue
    csv=remote/("external_"+asset.lower()+".csv")
    cmd=[exporter,"--start-wall-ns",str(start),"--end-wall-ns",str(end)]
    for tape in paths:
        cmd.extend(["--tape",str(tape)])
    with csv.open("wb") as out:
        subprocess.run(cmd,stdout=out,check=True)
    if csv.stat().st_size>0:
        lines.append(asset+"="+str(csv))
if len(lines)!=len(assets):
    raise SystemExit("EXTERNAL_VENUE_EXPORT_INCOMPLETE:required="+",".join(assets)+";ready="+",".join(lines))
(remote/"external_specs.txt").write_text("\\n".join(lines)+"\\n",encoding="utf-8")
PYEXT
{remote}/venv/bin/python - {remote}/venv/bin/python {remote}/src {context['app_dir']} {context['run_root']} {a.minimum_wall_ns} {a.expected_sha} {remote}/output/a0-a5 {remote}/external_specs.txt <<'PYRUN'
import os,subprocess,sys
from pathlib import Path
venv,src,app,run_root,minwall,sha,output,specfile=sys.argv[1:]
cmd=[venv,"-m","research.walk_forward_v3.maker_a0_a5_2h",
     "--root",run_root,"--minimum-wall-ns",minwall,
     "--code-sha",sha,"--output-dir",output,
     "--market-metadata",str(Path(specfile).parent/"market_metadata.json")]
for spec in Path(specfile).read_text(encoding="utf-8").splitlines():
    if spec.strip():
        cmd.extend(["--external-venue-csv",spec.strip()])
env=os.environ.copy()
env["PYTHONPATH"]=src+":"+app
subprocess.run(cmd,check=True,env=env)
PYRUN
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
paired=json.load(open(root/"06_paired_vs_a0.json"))
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
assert len(rows)==6*6*12
assert m["grid_cell_count"]==6*6*12
assert m["directional_target_semantics"]=="PM_YES_AT_DECISION_PLUS_HORIZON_MINUS_PM_YES_AT_DECISION;LATENCY_DOES_NOT_SHIFT_LABEL"
assert "EXACT_500MS_GRID" in m["anchor_source"]["timing_selection"]
assert int(m["feature_join"]["joined_rows"])>0
assert m["a5_rich_feature_names"]
assert set(paired["policies"])==policies-{{"A0_BASELINE"}}
series=set(((m.get("external_venue_tape") or dict()).get("load") or dict()).get("series") or [])
for asset in m["selection"].get("assets") or []:
    ready=sum(1 for venue in ("binance","coinbase","bybit") if asset+":"+venue in series)
    assert ready>=2,(asset,ready,sorted(series))
feature_counts=dict((k,len(v)) for k,v in m["feature_sets"].items())
compact=dict(
  split=m["split"],
  feature_sets=feature_counts,
  external_series=sorted(series),
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
