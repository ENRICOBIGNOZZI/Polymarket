#!/usr/bin/env python3
"""Run HISTORICAL_WALK_FORWARD_V2 on the active London PAPER host via SSM.

Only public-safe report artifacts are returned to the GitHub runner. Runtime data
never leaves London. No deployment, authenticated execution or real order path is
modified.
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

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CHUNK = 9000
SOURCE_PATHS = (
    "research/walk_forward_v2/__init__.py",
    "research/walk_forward_v2/core.py",
    "research/walk_forward_v2/report.py",
    "research/walk_forward_v2/run.py",
    "research/economic/causal_replay.py",
    "research/learning/__init__.py",
    "research/learning/common.py",
    "research/learning/models.py",
    "research/learning/validation.py",
    "scripts/v7_evidence_store.py",
    "research/requirements-learning.txt",
)


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id", "minimum_wall_ns", "folds",
        "output_directory", "paper_only", "authenticated_execution",
        "real_order_submission", "real_capital_at_risk",
    }
    if set(value) != required:
        raise ValueError("unexpected request fields")
    if value["schema"] != "polymarket_v7_historical_walk_forward_v2_ssm_request_v1" or value["version"] != 1:
        raise ValueError("invalid request schema")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if not isinstance(value["minimum_wall_ns"], int) or value["minimum_wall_ns"] <= 0:
        raise ValueError("invalid minimum wall ns")
    if not isinstance(value["folds"], int) or not 2 <= value["folds"] <= 8:
        raise ValueError("invalid folds")
    if not re.fullmatch(r"docs/research/historical-walk-forward-v2-[0-9]{4}-[0-9]{2}-[0-9]{2}", value["output_directory"]):
        raise ValueError("invalid output directory")
    if not (value["paper_only"] is True and value["authenticated_execution"] is False
            and value["real_order_submission"] is False and value["real_capital_at_risk"] is False):
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


def remote_context(instance: str) -> dict:
    command = r"""python3 - <<'PY'
import json,shlex,subprocess
from pathlib import Path
unit='polymarket-v7-paper.service'
env=subprocess.check_output(['systemctl','show',unit,'-p','Environment','--value'],text=True)
root=Path(next(v.split('=',1)[1] for v in shlex.split(env) if v.startswith('PM_V7_RUN_ROOT='))).resolve()
app=Path(subprocess.check_output(['systemctl','show',unit,'-p','WorkingDirectory','--value'],text=True).strip()).resolve()
state=json.loads((root/'control/runtime_status.json').read_text())
assert state.get('paper_only') is True
assert state.get('authenticated_execution') is False
assert state.get('real_order_submission') is False
assert root.parent==Path('/mnt/polymarket-data')
assert app.is_dir()
print('V2_CONTEXT='+json.dumps({'run_root':str(root),'app_dir':str(app)},sort_keys=True))
PY"""
    stdout, _ = run(REGION, instance, command, 60)
    line = next(x for x in stdout.splitlines() if x.startswith("V2_CONTEXT="))
    return json.loads(line.split("=", 1)[1])


def upload(instance: str, remote: str, payload: bytes) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    run(REGION, instance, f"rm -rf {remote} && mkdir -p {remote} && : > {remote}/source.tgz", 60)
    for offset in range(0, len(payload), CHUNK):
        encoded = base64.b64encode(payload[offset:offset+CHUNK]).decode()
        command = (
            "python3 -c " +
            repr("import base64;f=open(" + repr(remote + "/source.tgz") + ",'ab');"
                 "f.write(base64.b64decode(" + repr(encoded) + "));f.close()")
        )
        run(REGION, instance, command, 60)
    verify = (
        "python3 -c " +
        repr("import hashlib;print(hashlib.sha256(open(" + repr(remote + "/source.tgz") +
             ",'rb').read()).hexdigest())")
    )
    stdout, _ = run(REGION, instance, verify, 60)
    if stdout.strip().splitlines()[-1] != digest:
        raise RuntimeError("remote source archive hash mismatch")


def execute(instance: str, remote: str, context: dict, request: dict, source_sha: str) -> dict:
    run_root = context["run_root"]
    app_dir = context["app_dir"]
    command = f"""set -euo pipefail
cd {remote}
mkdir -p src output
tar -xzf source.tgz -C src
python3 -m venv venv
venv/bin/pip install --disable-pip-version-check --quiet -r src/research/requirements-learning.txt
PYTHONPATH={remote}/src:{app_dir} venv/bin/python -m research.walk_forward_v2.run \
  --input-root {run_root} \
  --output {remote}/output \
  --minimum-wall-ns {request['minimum_wall_ns']} \
  --folds {request['folds']} \
  --code-sha {source_sha}
tar -C {remote}/output -czf {remote}/results.tgz .
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path({remote!r})/'results.tgz'
print('V2_RESULT='+json.dumps({{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}},sort_keys=True))
PY"""
    stdout, _ = run(REGION, instance, command, 3600)
    line = next(x for x in stdout.splitlines() if x.startswith("V2_RESULT="))
    return json.loads(line.split("=", 1)[1])


def download(instance: str, remote: str, info: dict) -> bytes:
    size = int(info["bytes"])
    if size <= 0 or size > 64 * 1024 * 1024:
        raise ValueError("result archive outside bounded size")
    chunks = []
    for offset in range(0, size, CHUNK):
        code = (
            "import base64;f=open(" + repr(remote + "/results.tgz") + ",'rb');"
            "f.seek(" + str(offset) + ");print(base64.b64encode(f.read(" + str(CHUNK) + ")).decode())"
        )
        stdout, _ = run(REGION, instance, "python3 -c " + repr(code), 60)
        chunks.append(base64.b64decode(stdout.strip().splitlines()[-1], validate=True))
    payload = b"".join(chunks)
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != info["sha256"]:
        raise RuntimeError("downloaded result archive failed identity check")
    return payload


def extract(payload: bytes, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError("unsafe result archive member")
        archive.extractall(destination)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: v7_historical_walk_forward_v2_ssm.py REQUEST SOURCE_SHA")
    repo = Path(__file__).resolve().parents[1]
    request = load_request(Path(sys.argv[1]))
    source_sha = sys.argv[2]
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("exact source SHA required")
    context = remote_context(request["instance_id"])
    remote = "/tmp/polymarket-walk-forward-v2-" + source_sha[:12]
    payload = source_archive(repo)
    upload(request["instance_id"], remote, payload)
    info = execute(request["instance_id"], remote, context, request, source_sha)
    results = download(request["instance_id"], remote, info)
    extract(results, repo / request["output_directory"])
    run(REGION, request["instance_id"], f"rm -rf {remote}", 60)
    print("V2_LOCAL_OUTPUT=" + request["output_directory"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
