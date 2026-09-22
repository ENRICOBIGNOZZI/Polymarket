#!/usr/bin/env python3
"""Run zero-authority Maker value challenger on active London PAPER evidence."""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

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
    "research/walk_forward_v3/__init__.py",
    "research/walk_forward_v3/maker_value_challenger.py",
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/economic/causal_replay.py",
    "scripts/v7_maker_durable_learning.py",
    "research/requirements-learning.txt",
)


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id",
        "markout_horizon", "bootstrap_samples", "output_directory",
        "paper_only", "authenticated_execution", "real_order_submission",
        "real_capital_at_risk",
    }
    if set(value) != required:
        raise ValueError("unexpected Maker challenger request fields")
    if value["schema"] != "polymarket_v7_maker_value_challenger_ssm_request_v1":
        raise ValueError("invalid Maker challenger request schema")
    if value["version"] != 1:
        raise ValueError("invalid Maker challenger request version")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if value["markout_horizon"] not in (
        "100ms", "250ms", "500ms", "1s", "5s", "10s", "30s", "45s", "60s", "300s"
    ):
        raise ValueError("invalid Maker markout horizon")
    if (
        not isinstance(value["bootstrap_samples"], int)
        or not 100 <= value["bootstrap_samples"] <= 20_000
    ):
        raise ValueError("invalid bootstrap sample count")
    if not re.fullmatch(
        r"docs/research/maker-value-[0-9]{4}-[0-9]{2}-[0-9]{2}",
        value["output_directory"],
    ):
        raise ValueError("invalid output directory")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
    ):
        raise ValueError("PAPER-only Maker research contract violated")
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


def execute(instance: str, remote: str, context: dict, request: dict) -> dict:
    run_root = context["run_root"]
    app_dir = context["app_dir"]
    command = f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
MODEL_SHA="$(python3 - <<'PY'
import json
from pathlib import Path
root=Path({run_root!r})
state=json.loads((root/'control/runtime_status.json').read_text())
assert state.get('paper_only') is True
assert state.get('authenticated_execution') is False
assert state.get('real_order_submission') is False
value=str(state.get('model_sha') or '')
assert len(value)==40 and all(ch in '0123456789abcdef' for ch in value)
print(value)
PY
)"
PYTHONPATH={remote}/src:{app_dir} venv/bin/python -m research.walk_forward_v3.maker_value_challenger \
  --source-root {run_root}/ledger/execution.jsonl \
  --source-root {run_root}/research/evidence/maker_markout \
  --model-sha "$MODEL_SHA" \
  --markout-horizon {request['markout_horizon']} \
  --bootstrap-samples {request['bootstrap_samples']} \
  --output {remote}/output/maker_value.json
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/'results.tgz'
print('MAKER_VALUE_RESULT='+json.dumps(
    {{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},
    sort_keys=True))
PY"""
    stdout, _ = run(REGION, instance, command, 1800)
    line = next(
        item for item in stdout.splitlines()
        if item.startswith("MAKER_VALUE_RESULT=")
    )
    return json.loads(line.split("=", 1)[1])


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: v7_maker_value_challenger_ssm.py REQUEST SOURCE_SHA")
    repo = Path(__file__).resolve().parents[1]
    request = load_request(Path(sys.argv[1]))
    source_sha = sys.argv[2]
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("exact source SHA required")
    context = remote_context(request["instance_id"])
    remote = "/tmp/polymarket-maker-value-" + source_sha[:12]
    upload(request["instance_id"], remote, source_archive(repo))
    info = execute(request["instance_id"], remote, context, request)
    payload = download(request["instance_id"], remote, info)
    extract(payload, repo / request["output_directory"])
    run(REGION, request["instance_id"], f"rm -rf {remote}", 60)
    print("MAKER_VALUE_LOCAL_OUTPUT=" + request["output_directory"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
