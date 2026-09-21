#!/usr/bin/env python3
"""Launch the zero-authority executable-markout forward shadow on London via SSM."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import sys
import tarfile
import time

from v7_london_ssm_deploy import REGION, run

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
CHUNK = 9000


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id",
        "artifact_path", "artifact_sha256", "source_code_sha",
        "duration_seconds", "poll_ms", "maximum_inference_age_ms",
        "receipt_path", "paper_only", "authenticated_execution",
        "real_order_submission", "real_capital_at_risk",
    }
    if set(value) != required:
        raise ValueError("unexpected request fields")
    if value["schema"] != "polymarket_v7_executable_markout_forward_shadow_request_v1" or value["version"] != 1:
        raise ValueError("invalid request schema")
    if not REQUEST_RE.fullmatch(value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance")
    if not HASH_RE.fullmatch(value["artifact_sha256"]):
        raise ValueError("invalid artifact hash")
    if not SHA_RE.fullmatch(value["source_code_sha"]):
        raise ValueError("invalid source code sha")
    if value["artifact_path"] != "docs/research/historical-walk-forward-v2-2026-09-21/full_window_repricing_models.json":
        raise ValueError("unexpected artifact path")
    if not isinstance(value["duration_seconds"], int) or not 300 <= value["duration_seconds"] <= 14400:
        raise ValueError("invalid duration")
    if not isinstance(value["poll_ms"], int) or not 1 <= value["poll_ms"] <= 100:
        raise ValueError("invalid poll interval")
    if not isinstance(value["maximum_inference_age_ms"], int) or not 1 <= value["maximum_inference_age_ms"] <= 250:
        raise ValueError("invalid inference age")
    if not re.fullmatch(r"docs/research/executable-markout-forward-shadow-launch-[0-9]{4}-[0-9]{2}-[0-9]{2}\.json", value["receipt_path"]):
        raise ValueError("invalid receipt path")
    if not (value["paper_only"] is True and value["authenticated_execution"] is False
            and value["real_order_submission"] is False and value["real_capital_at_risk"] is False):
        raise ValueError("authority contract violated")
    return value


def archive(repo: Path, request: dict) -> bytes:
    paths = [
        Path("scripts/v7_executable_markout_forward_shadow.py"),
        Path(request["artifact_path"]),
    ]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative in paths:
            source = repo / relative
            if not source.is_file():
                raise FileNotFoundError(relative)
            tar.add(source, arcname=str(relative), recursive=False)
    return buffer.getvalue()


def context(instance: str) -> dict:
    command = r"""set -euo pipefail
python3 - <<'PY'
import json,shlex,subprocess
from pathlib import Path
unit='polymarket-v7-paper.service'
show=subprocess.check_output(['systemctl','show',unit,'-p','Environment','-p','User','-p','Group','-p','WorkingDirectory'],text=True)
values={}
for line in show.splitlines():
    if '=' in line:
        k,v=line.split('=',1); values[k]=v
env=values.get('Environment','')
root=Path(next(v.split('=',1)[1] for v in shlex.split(env) if v.startswith('PM_V7_RUN_ROOT='))).resolve()
state=json.loads((root/'control/runtime_status.json').read_text())
assert state.get('paper_only') is True
assert state.get('authenticated_execution') is False
assert state.get('real_order_submission') is False
assert root.parent==Path('/mnt/polymarket-data')
user=values.get('User') or 'ubuntu'
group=values.get('Group') or user
print('MARKOUT_CONTEXT='+json.dumps({
    'run_root':str(root),'user':user,'group':group,
    'working_directory':values.get('WorkingDirectory','')
},sort_keys=True))
PY"""
    stdout, _ = run(REGION, instance, command, 60)
    line = next(x for x in stdout.splitlines() if x.startswith("MARKOUT_CONTEXT="))
    return json.loads(line.split("=", 1)[1])


def upload(instance: str, remote: str, payload: bytes, user: str, group: str) -> None:
    expected = hashlib.sha256(payload).hexdigest()
    run(REGION, instance, f"rm -rf {shlex.quote(remote)} && mkdir -p {shlex.quote(remote)} && : > {shlex.quote(remote + '/bundle.tgz')}", 60)
    for offset in range(0, len(payload), CHUNK):
        encoded = base64.b64encode(payload[offset:offset+CHUNK]).decode()
        code = (
            "import base64;f=open(" + repr(remote + "/bundle.tgz") + ",'ab');"
            "f.write(base64.b64decode(" + repr(encoded) + "));f.close()"
        )
        run(REGION, instance, "python3 -c " + shlex.quote(code), 60)
    command = f"""set -euo pipefail
cd {shlex.quote(remote)}
actual="$(sha256sum bundle.tgz | awk '{{print $1}}')"
test "$actual" = {shlex.quote(expected)}
tar -xzf bundle.tgz
rm bundle.tgz
chown -R {shlex.quote(user)}:{shlex.quote(group)} {shlex.quote(remote)}
"""
    run(REGION, instance, command, 60)


def launch(instance: str, remote: str, ctx: dict, request: dict, shadow_sha: str) -> dict:
    run_root = ctx["run_root"]
    user, group = ctx["user"], ctx["group"]
    artifact = remote + "/" + request["artifact_path"]
    script = remote + "/scripts/v7_executable_markout_forward_shadow.py"
    output = remote + "/predictions.jsonl"
    status = remote + "/status.json"
    unit = "polymarket-v7-markout-shadow-" + hashlib.sha256(request["request_id"].encode()).hexdigest()[:12]
    command = f"""set -euo pipefail
test "$(sha256sum {shlex.quote(artifact)} | awk '{{print $1}}')" = {shlex.quote(request['artifact_sha256'])}
test -f {shlex.quote(run_root + '/control/runtime_status.json')}
systemctl stop {shlex.quote(unit)} >/dev/null 2>&1 || true
systemctl reset-failed {shlex.quote(unit)} >/dev/null 2>&1 || true
systemd-run --unit={shlex.quote(unit)} --collect \
  --uid={shlex.quote(user)} --gid={shlex.quote(group)} \
  -p NoNewPrivileges=yes -p Nice=10 -p IOSchedulingClass=idle \
  -p CPUQuota=25% -p MemoryMax=256M -p PrivateTmp=yes \
  -p RestrictAddressFamilies=AF_UNIX \
  /usr/bin/python3 {shlex.quote(script)} \
    --run-root {shlex.quote(run_root)} \
    --artifact {shlex.quote(artifact)} \
    --artifact-sha256 {shlex.quote(request['artifact_sha256'])} \
    --source-code-sha {shlex.quote(request['source_code_sha'])} \
    --shadow-code-sha {shlex.quote(shadow_sha)} \
    --output {shlex.quote(output)} \
    --status {shlex.quote(status)} \
    --poll-ms {request['poll_ms']} \
    --maximum-inference-age-ms {request['maximum_inference_age_ms']} \
    --duration-seconds {request['duration_seconds']} >/dev/null
for _ in $(seq 1 20); do
  if test -f {shlex.quote(status)}; then break; fi
  sleep 0.5
done
systemctl is-active {shlex.quote(unit)}
python3 - <<'PY'
import json
from pathlib import Path
p=Path({status!r})
value=json.loads(p.read_text()) if p.is_file() else {{}}
print('MARKOUT_LAUNCH='+json.dumps({{
  'unit':{unit!r},'remote_root':{remote!r},
  'output':{output!r},'status':{status!r},
  'initial_status':value
}},sort_keys=True))
PY"""
    stdout, _ = run(REGION, instance, command, 90)
    line = next(x for x in stdout.splitlines() if x.startswith("MARKOUT_LAUNCH="))
    return json.loads(line.split("=", 1)[1])


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: v7_executable_markout_forward_shadow_ssm.py REQUEST SHADOW_SHA")
    repo = Path(__file__).resolve().parents[1]
    request = load_request(Path(sys.argv[1]))
    shadow_sha = sys.argv[2]
    if not SHA_RE.fullmatch(shadow_sha):
        raise ValueError("exact shadow SHA required")
    ctx = context(request["instance_id"])
    remote = "/mnt/polymarket-data/research_shadow/v2/" + request["request_id"]
    payload = archive(repo, request)
    upload(request["instance_id"], remote, payload, ctx["user"], ctx["group"])
    info = launch(request["instance_id"], remote, ctx, request, shadow_sha)
    receipt = {
        "schema": "polymarket_v7_executable_markout_forward_shadow_launch_v1",
        "request_id": request["request_id"],
        "instance_id": request["instance_id"],
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "artifact_sha256": request["artifact_sha256"],
        "source_code_sha": request["source_code_sha"],
        "shadow_code_sha": shadow_sha,
        "duration_seconds": request["duration_seconds"],
        "poll_ms": request["poll_ms"],
        "maximum_inference_age_ms": request["maximum_inference_age_ms"],
        "unit": info["unit"],
        "remote_root": info["remote_root"],
        "output": info["output"],
        "status": info["status"],
        "initial_status": info["initial_status"],
    }
    destination = repo / request["receipt_path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("MARKOUT_RECEIPT=" + str(request["receipt_path"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
