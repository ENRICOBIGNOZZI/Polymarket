#!/usr/bin/env python3
"""Run the zero-authority Maker challenger on active London PAPER evidence."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import sys
import tarfile

from v7_direct_action_research_ssm import (
    REGION,
    SHA_RE,
    download,
    extract,
    remote_context,
    run,
    upload,
)

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SOURCE_PATHS = (
    "research/walk_forward_v3/maker_challenger.py",
    "research/walk_forward_v3/maker_value_challenger.py",
    "scripts/v7_maker_durable_learning.py",
    "scripts/v7_maker_fillability_report.py",
    "scripts/v7_maker_fill_conditioned_toxicity.py",
    "scripts/v7_maker_execution_horse_race.py",
    "monitoring/v7_maker_fillability.py",
    "monitoring/v7_maker_fillability_exact.py",
    "config/v7_professional_market_maker.json",
)


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id", "output_directory",
        "paper_only", "authenticated_execution", "real_order_submission",
        "real_capital_at_risk",
    }
    if set(value) != required:
        raise ValueError("unexpected Maker challenger request fields")
    if value["schema"] != "polymarket_v7_maker_challenger_ssm_request_v1":
        raise ValueError("invalid Maker challenger request schema")
    if value["version"] != 1:
        raise ValueError("invalid Maker challenger request version")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if not re.fullmatch(
        r"docs/research/maker-challenger-[0-9]{4}-[0-9]{2}-[0-9]{2}",
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
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative in SOURCE_PATHS:
            path = repo / relative
            if not path.is_file():
                raise FileNotFoundError(relative)
            archive.add(path, arcname=relative, recursive=False)
    return buffer.getvalue()


def execute(instance: str, remote: str, context: dict) -> dict:
    run_root = context["run_root"]
    command = f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
export PYTHONPATH={remote}/src:{remote}/src/scripts:{remote}/src/monitoring
MODEL_SHA="$(cat {run_root}/control/deployed_sha)"
test "$MODEL_SHA" != ""

python3 src/scripts/v7_maker_fillability_report.py \
  --run-root {run_root} \
  --policy src/config/v7_professional_market_maker.json \
  --model-sha "$MODEL_SHA" \
  --output-json output/fillability.json \
  --output-md output/fillability.md >/dev/null

for action in JOIN IMPROVE1 FADE1; do
  python3 src/scripts/v7_maker_fill_conditioned_toxicity.py \
    --maker-evidence {run_root}/ledger \
    --maker-evidence {run_root}/micro_maker \
    --output "output/toxicity_$action.json" \
    --markout-horizon 250ms \
    --placement-action "$action" \
    --minimum-clusters 10 \
    --bootstrap-samples 1000 >/dev/null
done

if ! python3 src/scripts/v7_maker_execution_horse_race.py \
  --maker-evidence {run_root} \
  --output output/horse_race.json \
  --markout-horizon 1s \
  --placement-action JOIN \
  --bootstrap-samples 1000 >/dev/null 2>output/horse_race.stderr; then
  python3 - <<'PY'
import json
from pathlib import Path
err=Path("output/horse_race.stderr").read_text(encoding="utf-8",errors="replace")[-4000:]
Path("output/horse_race.json").write_text(json.dumps({
  "schema":"polymarket_v7_maker_execution_horse_race_v1",
  "paper_only":True,
  "authenticated_execution":False,
  "real_order_submission":False,
  "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
  "state":"INSUFFICIENT_OR_UNAVAILABLE_FILL_CONDITIONED_EVIDENCE",
  "error_tail":err,
},indent=2,sort_keys=True)+"\n",encoding="utf-8")
PY
fi

python3 - {run_root!r} <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
out=Path("output")

def safe(value):
    return (
        isinstance(value,dict)
        and value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
    )

complete=[]
for path in root.rglob("*complete_set*status*.json"):
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        if safe(value):
            complete.append((path.stat().st_mtime_ns,path,value))
    except (OSError,json.JSONDecodeError):
        pass
if complete:
    _,path,value=max(complete,key=lambda x:x[0])
    value=dict(value); value["_source_path"]=str(path)
else:
    value={
      "paper_only":True,"authenticated_execution":False,
      "real_order_submission":False,"real_capital_at_risk":False,
      "state":"UNAVAILABLE_NO_COMPLETE_SET_STATUS"
    }
(out/"complete_set.json").write_text(
    json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8")

forward=[]
exp=root.parent/"paper_v7_experiments"
if exp.is_dir():
    checked=0
    for path in exp.rglob("*.json"):
        checked+=1
        if checked>1000: break
        try:
            candidate=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError):
            continue
        if (
            safe(candidate)
            and str(candidate.get("schema") or "").startswith(
                "polymarket_v7_maker_forward_window_report")
        ):
            forward.append((path.stat().st_mtime_ns,path,candidate))
if forward:
    _,path,value=max(forward,key=lambda x:x[0])
    value=dict(value); value["_source_path"]=str(path)
else:
    value={
      "paper_only":True,"authenticated_execution":False,
      "real_order_submission":False,"real_capital_at_risk":False,
      "state":"UNAVAILABLE_NO_FROZEN_FORWARD_REPORT"
    }
(out/"forward_report.json").write_text(
    json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8")
PY

MODEL_SHA="$MODEL_SHA" RUN_ROOT={run_root} python3 - <<'PY'
import os
from pathlib import Path
from research.walk_forward_v2.core import atomic_json
from research.walk_forward_v3.maker_value_challenger import run

root=Path(os.environ["RUN_ROOT"])
model_sha=os.environ["MODEL_SHA"]
cycle_paths=[]
for base in (root/"micro_maker", root/"research"):
    if not base.is_dir():
        continue
    for pattern in ("*complete_set*.jsonl", "*complete_set*.jsonl.gz"):
        cycle_paths.extend(path for path in base.rglob(pattern) if path.is_file())
result=run(
    [root/"ledger", root/"micro_maker"],
    model_sha=model_sha,
    markout_horizon="1s",
    bootstrap_samples=1000,
    complete_set_paths=sorted(set(cycle_paths)),
)
atomic_json(Path("output/causal_value.json"), result)
PY

python3 -m research.walk_forward_v3.maker_challenger \
  --fillability output/fillability.json \
  --toxicity output/toxicity_JOIN.json \
  --toxicity output/toxicity_IMPROVE1.json \
  --toxicity output/toxicity_FADE1.json \
  --horse-race output/horse_race.json \
  --complete-set output/complete_set.json \
  --forward-report output/forward_report.json \
  --causal-value output/causal_value.json \
  --output output/maker_challenger.json >/dev/null

tar -C output -czf results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path("results.tgz")
print("MAKER_CHALLENGER_RESULT="+json.dumps({
  "bytes":p.stat().st_size,
  "sha256":hashlib.sha256(p.read_bytes()).hexdigest(),
},sort_keys=True))
PY"""
    stdout, _ = run(REGION, instance, command, 3600)
    line = next(
        value for value in stdout.splitlines()
        if value.startswith("MAKER_CHALLENGER_RESULT=")
    )
    return json.loads(line.split("=", 1)[1])


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: v7_maker_challenger_ssm.py REQUEST SOURCE_SHA")
    repo = Path(__file__).resolve().parents[1]
    request = load_request(Path(sys.argv[1]))
    source_sha = sys.argv[2]
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("exact source SHA required")
    context = remote_context(request["instance_id"])
    remote = "/tmp/polymarket-maker-challenger-" + source_sha[:12]
    payload = source_archive(repo)
    upload(request["instance_id"], remote, payload)
    info = execute(request["instance_id"], remote, context)
    results = download(request["instance_id"], remote, info)
    extract(results, repo / request["output_directory"])
    run(REGION, request["instance_id"], f"rm -rf {remote}", 60)
    print("MAKER_CHALLENGER_LOCAL_OUTPUT=" + request["output_directory"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
