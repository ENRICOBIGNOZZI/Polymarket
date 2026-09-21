#!/usr/bin/env python3
"""Run the A0/A1/A2 Direct Action horse race on London PAPER evidence.

Research-only. This script performs no deployment, restart, order submission,
runtime mutation, model promotion, or risk-limit change.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile

from v7_london_ssm_deploy import REGION, run
from v7_direct_action_research_ssm import (
    CHUNK,
    download,
    extract,
    remote_context,
    upload,
)

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SOURCE_PATHS = (
    "config/v7_trade_frequency_sizing_challenger.json",
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/direct_action.py",
    "research/walk_forward_v3/decomposed_action.py",
    "research/walk_forward_v3/decomposed_benchmark.py",
    "research/walk_forward_v3/risk_frontier.py",
    "research/walk_forward_v3/bilateral.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/causal_replay.py",
    "research/requirements-learning.txt",
)


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id",
        "expected_research_sha", "minimum_wall_ns", "folds", "latency_ms",
        "capital_budget", "output_directory", "paper_only",
        "authenticated_execution", "real_order_submission",
        "real_capital_at_risk", "automatic_promotion",
    }
    if set(value) != required:
        raise ValueError("unexpected decomposed benchmark request fields")
    if value["schema"] != "polymarket_v7_decomposed_benchmark_ssm_request_v1":
        raise ValueError("invalid benchmark request schema")
    if value["version"] != 1:
        raise ValueError("invalid benchmark request version")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value["request_id"]):
        raise ValueError("invalid benchmark request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid benchmark instance id")
    if not SHA_RE.fullmatch(value["expected_research_sha"]):
        raise ValueError("exact research SHA required")
    if not isinstance(value["minimum_wall_ns"], int) or value["minimum_wall_ns"] <= 0:
        raise ValueError("invalid minimum wall ns")
    if not isinstance(value["folds"], int) or not 2 <= value["folds"] <= 8:
        raise ValueError("invalid folds")
    if value["latency_ms"] not in (25, 50, 100, 250):
        raise ValueError("latency outside training support")
    if (
        not isinstance(value["capital_budget"], (int, float))
        or isinstance(value["capital_budget"], bool)
        or not 1 <= float(value["capital_budget"]) <= 100_000
    ):
        raise ValueError("invalid capital budget")
    if not re.fullmatch(
        r"docs/research/decomposed-horse-race-[0-9]{4}-[0-9]{2}-[0-9]{2}",
        value["output_directory"],
    ):
        raise ValueError("invalid benchmark output directory")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
        and value["automatic_promotion"] is False
    ):
        raise ValueError("research-only benchmark contract violated")
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


def execute(
    instance: str,
    remote: str,
    context: dict,
    request: dict,
) -> dict:
    run_root = context["run_root"]
    app_dir = context["app_dir"]
    command = f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
PYTHONPATH={remote}/src:{app_dir} venv/bin/python -m research.walk_forward_v3.decomposed_benchmark \
  --root {run_root} \
  --output {remote}/output/horse_race.json \
  --minimum-wall-ns {request['minimum_wall_ns']} \
  --folds {request['folds']} \
  --latency-ms {request['latency_ms']} \
  --capital-budget {request['capital_budget']} \
  --challenger-config {remote}/src/config/v7_trade_frequency_sizing_challenger.json
python3 - {remote}/output/horse_race.json <<'PY'
import json,sys
p=sys.argv[1]
v=json.load(open(p,encoding='utf-8'))
assert v.get('schema')=='polymarket_v7_decomposed_action_horse_race_v1'
assert v.get('state')=='READY'
assert v.get('paper_only') is True
assert v.get('authenticated_execution') is False
assert v.get('real_order_submission') is False
assert v.get('real_capital_at_risk') is False
assert v.get('automatic_promotion') is False
assert set(v.get('policies') or {{}})=={{'A0_BASELINE','A1_DIRECT_CHALLENGER','A2_DECOMPOSED'}}
for policy in v['policies'].values():
    metrics=policy.get('aggregate_oos') or {{}}
    assert int(metrics.get('opportunities') or 0)>0
print('HORSE_RACE_READY')
PY
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/'results.tgz'
print('DECOMPOSED_BENCHMARK_RESULT='+json.dumps({{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},sort_keys=True))
PY"""
    stdout, _ = run(REGION, instance, command, 7200)
    marker = next(
        line for line in stdout.splitlines()
        if line.startswith("DECOMPOSED_BENCHMARK_RESULT=")
    )
    return json.loads(marker.split("=", 1)[1])


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: v7_decomposed_benchmark_ssm.py REQUEST")
    repo = Path(__file__).resolve().parents[1]
    request = load_request(Path(sys.argv[1]))
    source_sha = request["expected_research_sha"]
    context = remote_context(request["instance_id"])
    remote = "/tmp/polymarket-decomposed-benchmark-" + source_sha[:12]
    payload = source_archive(repo)
    upload(request["instance_id"], remote, payload)
    info = execute(request["instance_id"], remote, context, request)
    result = download(request["instance_id"], remote, info)
    destination = repo / request["output_directory"]
    extract(result, destination)
    run(REGION, request["instance_id"], f"rm -rf {remote}", 60)

    output = destination / "horse_race.json"
    value = json.loads(output.read_text(encoding="utf-8"))
    value["research_source_sha"] = source_sha
    value["research_request_id"] = request["request_id"]
    value["automatic_promotion"] = False
    output.write_text(
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print("DECOMPOSED_BENCHMARK_LOCAL_OUTPUT=" + str(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
