#!/usr/bin/env python3
"""Read-only London SSM runner for nested Direct Action gate selection."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import sys
import tarfile

from v7_direct_action_research_ssm import (
    REGION, SHA_RE, download, extract, remote_context, run, upload,
)

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SOURCE_PATHS = (
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v3/risk_frontier.py",
    "research/walk_forward_v3/gate_frontier.py",
    "research/walk_forward_v3/run_gate_frontier.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/causal_replay.py",
    "research/requirements-learning.txt",
)


def load_request(path: Path) -> dict:
    value=json.loads(path.read_text(encoding="utf-8"))
    required={
        "schema","version","request_id","instance_id","minimum_wall_ns",
        "outer_folds","inner_folds","latency_ms","capital_budget",
        "output_directory","paper_only","authenticated_execution",
        "real_order_submission","real_capital_at_risk",
    }
    if set(value)!=required:
        raise ValueError("unexpected gate request fields")
    if value["schema"]!="polymarket_v7_direct_action_gate_research_ssm_request_v1":
        raise ValueError("invalid gate request schema")
    if value["version"]!=1:
        raise ValueError("invalid gate request version")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}",value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if not isinstance(value["minimum_wall_ns"],int) or value["minimum_wall_ns"]<=0:
        raise ValueError("invalid minimum wall ns")
    if not isinstance(value["outer_folds"],int) or not 2<=value["outer_folds"]<=5:
        raise ValueError("invalid outer folds")
    if not isinstance(value["inner_folds"],int) or not 2<=value["inner_folds"]<=4:
        raise ValueError("invalid inner folds")
    if value["latency_ms"] not in (25,50,100,250):
        raise ValueError("invalid latency")
    if not isinstance(value["capital_budget"],(int,float)) or isinstance(value["capital_budget"],bool):
        raise ValueError("invalid capital budget")
    if not 1<=float(value["capital_budget"])<=100000:
        raise ValueError("capital budget outside bounded range")
    if not re.fullmatch(
        r"docs/research/direct-action-gates-[0-9]{4}-[0-9]{2}-[0-9]{2}",
        value["output_directory"],
    ):
        raise ValueError("invalid output directory")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
    ):
        raise ValueError("PAPER-only authority contract violated")
    return value


def source_archive(repo: Path) -> bytes:
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode="w:gz") as archive:
        for relative in SOURCE_PATHS:
            path=repo/relative
            if not path.is_file():
                raise FileNotFoundError(relative)
            archive.add(path,arcname=relative,recursive=False)
    return buffer.getvalue()


def execute(instance: str, remote: str, context: dict, request: dict) -> dict:
    run_root=context["run_root"]
    app_dir=context["app_dir"]
    command=f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
PYTHONPATH={remote}/src:{app_dir} venv/bin/python -m research.walk_forward_v3.run_gate_frontier \
  --input-root {run_root} \
  --output {remote}/output/gate_frontier.json \
  --minimum-wall-ns {request['minimum_wall_ns']} \
  --outer-folds {request['outer_folds']} \
  --inner-folds {request['inner_folds']} \
  --latency-ms {request['latency_ms']} \
  --capital-budget {request['capital_budget']}
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/'results.tgz'
print('DIRECT_ACTION_GATE_RESULT='+json.dumps(
    {{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},
    sort_keys=True))
PY"""
    stdout,_=run(REGION,instance,command,7200)
    line=next(x for x in stdout.splitlines() if x.startswith("DIRECT_ACTION_GATE_RESULT="))
    return json.loads(line.split("=",1)[1])


def main() -> int:
    if len(sys.argv)!=3:
        raise SystemExit("usage: v7_direct_action_gate_research_ssm.py REQUEST SOURCE_SHA")
    repo=Path(__file__).resolve().parents[1]
    request=load_request(Path(sys.argv[1]))
    source_sha=sys.argv[2]
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("exact source SHA required")
    context=remote_context(request["instance_id"])
    remote="/tmp/polymarket-direct-action-gates-"+source_sha[:12]
    upload(request["instance_id"],remote,source_archive(repo))
    info=execute(request["instance_id"],remote,context,request)
    results=download(request["instance_id"],remote,info)
    extract(results,repo/request["output_directory"])
    run(REGION,request["instance_id"],f"rm -rf {remote}",60)
    print("DIRECT_ACTION_GATE_LOCAL_OUTPUT="+request["output_directory"])
    return 0


if __name__=="__main__":
    raise SystemExit(main())
