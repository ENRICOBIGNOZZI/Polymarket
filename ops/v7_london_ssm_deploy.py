#!/usr/bin/env python3
"""Deploy one exact PAPER-only V7 SHA to the London host through AWS SSM.

This is a transport fallback for the canonical V7 cutover. It discovers only
known/labelled Polymarket London instances, selects exactly one existing runtime
by Tailscale-IP match or unique active PAPER service, uploads the already-built
research-plane artifact in bounded SSM chunks, then invokes the same
v7_london_stage_release.sh and v7_london_cutover.sh used by the SSH path.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Any

REGION = "eu-west-2"
STACK = "polymarket-v7-london-shootout"
TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
MAX_CANDIDATES = 16
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
CHUNK_CHARS = 12000


class SsmDeployError(RuntimeError):
    pass


def exact_sha(value: str) -> bool:
    return bool(SHA_RE.fullmatch(value or ""))


def aws_json(region: str, args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "AWS_PAGER": ""},
    )
    if completed.returncode != 0:
        raise SsmDeployError(completed.stderr.strip() or "AWS CLI failed")
    try:
        value = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SsmDeployError("AWS returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise SsmDeployError("AWS JSON object required")
    return value


def stack_instances(region: str, stack_name: str) -> set[str]:
    try:
        value = aws_json(region, [
            "cloudformation", "describe-stacks", "--stack-name", stack_name,
        ])
    except SsmDeployError:
        return set()
    stacks = value.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        return set()
    outputs = {
        x.get("OutputKey"): x.get("OutputValue")
        for x in (stacks[0].get("Outputs") or [])
        if isinstance(x, dict)
    }
    out = set()
    for key in ("InstanceAz1", "InstanceAz2", "InstanceAz3"):
        value = outputs.get(key)
        if isinstance(value, str) and INSTANCE_RE.fullmatch(value):
            out.add(value)
    return out


def tagged_instances(region: str) -> set[str]:
    value = aws_json(region, [
        "ec2", "describe-instances",
        "--filters",
        "Name=instance-state-name,Values=running",
        "Name=tag:PolymarketMode,Values=PAPER_SHADOW_ONLY",
    ])
    out: set[str] = set()
    for reservation in value.get("Reservations") or []:
        if not isinstance(reservation, dict):
            continue
        for item in reservation.get("Instances") or []:
            instance = item.get("InstanceId") if isinstance(item, dict) else None
            if isinstance(instance, str) and INSTANCE_RE.fullmatch(instance):
                out.add(instance)
    return out


def ssm_online_instances(region: str) -> set[str]:
    value = aws_json(region, ["ssm", "describe-instance-information"])
    out = set()
    for item in value.get("InstanceInformationList") or []:
        if not isinstance(item, dict) or item.get("PingStatus") != "Online":
            continue
        instance = item.get("InstanceId")
        if isinstance(instance, str) and INSTANCE_RE.fullmatch(instance):
            out.add(instance)
    return out


def candidate_instances(region: str, stack_name: str) -> list[str]:
    known = stack_instances(region, stack_name) | tagged_instances(region)
    online = ssm_online_instances(region)
    candidates = sorted(known & online)
    if not candidates:
        raise SsmDeployError("no online known Polymarket London SSM instance")
    if len(candidates) > MAX_CANDIDATES:
        raise SsmDeployError("too many London SSM candidates")
    return candidates


def send(region: str, instance: str, command: str, timeout_s: int = 600) -> str:
    if not INSTANCE_RE.fullmatch(instance):
        raise SsmDeployError("invalid instance id")
    parameters = json.dumps({
        "commands": [f"bash -lc {shlex.quote(command)}"],
        "executionTimeout": [str(timeout_s)],
    })
    value = aws_json(region, [
        "ssm", "send-command",
        "--instance-ids", instance,
        "--document-name", "AWS-RunShellScript",
        "--parameters", parameters,
        "--comment", "Polymarket V7 exact-SHA PAPER SSM transport",
    ])
    command_id = (value.get("Command") or {}).get("CommandId")
    if not isinstance(command_id, str) or not command_id:
        raise SsmDeployError("SSM command id missing")
    return command_id


def wait(region: str, instance: str, command_id: str,
         timeout_s: int = 900, poll_s: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        value = aws_json(region, [
            "ssm", "get-command-invocation",
            "--command-id", command_id,
            "--instance-id", instance,
        ])
        last = value
        if value.get("Status") in TERMINAL:
            return value
        time.sleep(poll_s)
    raise SsmDeployError(
        f"SSM command timeout instance={instance} command={command_id} "
        f"last={last.get('Status')}"
    )


def run(region: str, instance: str, command: str,
        timeout_s: int = 900) -> tuple[str, str]:
    command_id = send(region, instance, command, timeout_s)
    value = wait(region, instance, command_id, timeout_s + 120)
    stdout = str(value.get("StandardOutputContent") or "")
    stderr = str(value.get("StandardErrorContent") or "")
    if value.get("Status") != "Success":
        status = str(value.get("StatusDetails") or value.get("Status") or "")
        raise SsmDeployError(
            f"SSM failed instance={instance} command={command_id}: "
            f"stdout_tail={stdout[-8000:]!r} stderr_tail={stderr[-4000:]!r} status={status!r}"
        )
    return stdout, stderr


PROBE_COMMAND = r"""set -euo pipefail
python3 - <<'PY'
import json,os,shlex,subprocess,socket
def cmd(args):
    p=subprocess.run(args,text=True,capture_output=True)
    return p.returncode,(p.stdout or '').strip(),(p.stderr or '').strip()
unit='polymarket-v7-paper.service'
rc,active,_=cmd(['systemctl','is-active',unit])
unit_active=(rc==0 and active=='active')
rc,user,_=cmd(['systemctl','show',unit,'-p','User','--value'])
user=user.strip() if rc==0 else ''
apps=[]
for candidate in ('enrico','ubuntu'):
    path=f'/home/{candidate}/polymarket'
    if os.path.isdir(path+'/.git'):
        apps.append({'user':candidate,'path':path})
if user and any(x['user']==user for x in apps):
    app=next(x for x in apps if x['user']==user)
elif len(apps)==1:
    app=apps[0]
else:
    app=None
repo_sha=''; dirty=None
if app:
    rc,repo_sha,_=cmd(['sudo','-u',app['user'],'git','-C',app['path'],'rev-parse','HEAD'])
    rc2,status,_=cmd(['sudo','-u',app['user'],'git','-C',app['path'],'status','--porcelain'])
    if rc or rc2: repo_sha=''
    else: dirty=bool(status)
tailscale=[]
rc,ts,_=cmd(['tailscale','ip','-4'])
if rc==0:
    tailscale=[x.strip() for x in ts.splitlines() if x.strip()]
env={}
rc,raw,_=cmd(['systemctl','show',unit,'-p','Environment','--value'])
if rc==0:
    try:
        for item in shlex.split(raw):
            if '=' in item:
                k,v=item.split('=',1);env[k]=v
    except ValueError:
        pass
run_root=env.get('PM_V7_RUN_ROOT','')
runtime={}
if run_root and os.path.isabs(run_root):
    path=os.path.join(run_root,'control','runtime_status.json')
    try:
        with open(path) as f: runtime=json.load(f)
    except Exception:
        runtime={}
print('V7_SSM_PROBE='+json.dumps({
  'hostname':socket.gethostname(),
  'unit_active':unit_active,
  'unit_user':user or None,
  'app':app,
  'app_candidates':apps,
  'repo_sha':repo_sha or None,
  'repo_dirty':dirty,
  'tailscale_ips':tailscale,
  'run_root':run_root or None,
  'runtime_state':runtime.get('state'),
  'runtime_sha':runtime.get('model_sha'),
  'paper_only':runtime.get('paper_only'),
  'authenticated_execution':runtime.get('authenticated_execution'),
  'real_order_submission':runtime.get('real_order_submission'),
},sort_keys=True,separators=(',',':')))
PY"""


def parse_marker(stdout: str, prefix: str) -> dict[str, Any]:
    rows = [line[len(prefix):] for line in stdout.splitlines()
            if line.startswith(prefix)]
    if len(rows) != 1:
        raise SsmDeployError(f"expected one {prefix} marker")
    try:
        value = json.loads(rows[0])
    except json.JSONDecodeError as exc:
        raise SsmDeployError(f"invalid {prefix} JSON") from exc
    if not isinstance(value, dict):
        raise SsmDeployError(f"invalid {prefix} payload")
    return value


def probe(region: str, instances: list[str]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for instance in instances:
        try:
            stdout, _ = run(region, instance, PROBE_COMMAND, 180)
            value = parse_marker(stdout, "V7_SSM_PROBE=")
            value["instance_id"] = instance
            results[instance] = value
        except SsmDeployError as exc:
            results[instance] = {
                "instance_id": instance,
                "probe_error": str(exc),
                "unit_active": False,
                "tailscale_ips": [],
            }
    return results


def select_target(probes: dict[str, dict[str, Any]],
                  expected_tailscale_ip: str,
                  expected_instance_id: str = "") -> dict[str, Any]:
    healthy = [
        value for value in probes.values()
        if not value.get("probe_error") and isinstance(value.get("app"), dict)
    ]
    if expected_instance_id:
        if not INSTANCE_RE.fullmatch(expected_instance_id):
            raise SsmDeployError("invalid expected London instance id")
        exact = [value for value in healthy if value.get("instance_id") == expected_instance_id]
        if len(exact) != 1:
            raise SsmDeployError(
                f"expected London instance unavailable: {expected_instance_id}"
            )
        selected = exact[0]
        reason = "EXACT_INSTANCE_ID"
    else:
            ip_matches = [
            value for value in healthy
            if expected_tailscale_ip
            and expected_tailscale_ip in (value.get("tailscale_ips") or [])
        ]
        if len(ip_matches) == 1:
            selected = ip_matches[0]
            reason = "EXACT_TAILSCALE_IP"
        elif len(ip_matches) > 1:
            raise SsmDeployError("multiple instances claim expected Tailscale IP")
        else:
            active = [value for value in healthy if value.get("unit_active") is True]
            if len(active) != 1:
                raise SsmDeployError(
                    f"cannot select unique London runtime: ip_matches={len(ip_matches)} "
                    f"active={len(active)} healthy={len(healthy)}"
                )
            selected = active[0]
            reason = "UNIQUE_ACTIVE_PAPER_SERVICE"
    user = (selected.get("app") or {}).get("user")
    app = (selected.get("app") or {}).get("path")
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise SsmDeployError("selected service user invalid")
    if app != f"/home/{user}/polymarket":
        raise SsmDeployError("selected repository path invalid")
    selected = dict(selected)
    selected["selection_reason"] = reason
    return selected


def validate_artifact(path: Path, expected_sha: str) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise SsmDeployError("artifact archive missing or unsafe")
    size = path.stat().st_size
    if not 0 < size <= MAX_ARTIFACT_BYTES:
        raise SsmDeployError("artifact archive size outside bound")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not exact_sha(expected_sha):
        raise SsmDeployError("expected SHA invalid")
    return size, digest


def upload_artifact(region: str, instance: str, archive: Path,
                    expected_sha: str, user: str) -> dict[str, Any]:
    size, digest = validate_artifact(archive, expected_sha)
    encoded = base64.b64encode(archive.read_bytes()).decode("ascii")
    remote_b64 = f"/tmp/polymarket-v7-artifact-{expected_sha}.b64"
    remote_tgz = f"/tmp/polymarket-v7-artifact-{expected_sha}.tgz"
    run(region, instance, f"set -euo pipefail; rm -f {shlex.quote(remote_b64)} {shlex.quote(remote_tgz)}; : > {shlex.quote(remote_b64)}", 120)
    chunks = 0
    for offset in range(0, len(encoded), CHUNK_CHARS):
        chunk = encoded[offset:offset + CHUNK_CHARS]
        command = (
            "set -euo pipefail; "
            f"printf '%s' {shlex.quote(chunk)} >> {shlex.quote(remote_b64)}"
        )
        run(region, instance, command, 120)
        chunks += 1
    artifact_root = f"/home/{user}/polymarket-artifacts"
    target = f"{artifact_root}/by-sha/{expected_sha}"
    tmp = f"{target}.tmp.ssm"
    finalize = f"""set -euo pipefail
USER_NAME={shlex.quote(user)}
SHA={expected_sha}
B64={shlex.quote(remote_b64)}
TGZ={shlex.quote(remote_tgz)}
EXPECTED_DIGEST={digest}
ARTIFACT_ROOT={shlex.quote(artifact_root)}
TARGET={shlex.quote(target)}
TMP={shlex.quote(tmp)}
base64 -d "$B64" > "$TGZ"
printf '%s  %s\n' "$EXPECTED_DIGEST" "$TGZ" | sha256sum -c -
rm -rf "$TMP"
mkdir -p "$TMP"
tar -xzf "$TGZ" -C "$TMP"
python3 - "$TMP/manifest.json" "$SHA" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))
assert v.get('schema')=='polymarket_v7_runtime_artifact_bundle_v1'
assert v.get('target_model_sha')==sys.argv[2]
assert v.get('paper_only') is True
assert v.get('authenticated_execution') is False
assert v.get('real_order_submission') is False
assert v.get('runtime_training') is False
PY
install -d -o "$USER_NAME" -g "$(id -gn "$USER_NAME")" "$ARTIFACT_ROOT/by-sha"
chown -R "$USER_NAME:$(id -gn "$USER_NAME")" "$TMP"
rm -rf "$TARGET"
mv "$TMP" "$TARGET"
rm -f "$B64" "$TGZ"
printf 'V7_ARTIFACT_READY=%s\n' "$TARGET"
"""
    stdout, _ = run(region, instance, finalize, 300)
    if f"V7_ARTIFACT_READY={target}" not in stdout:
        raise SsmDeployError("artifact finalize marker missing")
    return {"bytes": size, "sha256": digest, "chunks": chunks, "remote": target}


def cutover_command(expected_sha: str, selected: dict[str, Any]) -> str:
    if not exact_sha(expected_sha):
        raise SsmDeployError("invalid cutover SHA")
    user = (selected.get("app") or {}).get("user")
    app = (selected.get("app") or {}).get("path")
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise SsmDeployError("invalid cutover user")
    if app != f"/home/{user}/polymarket":
        raise SsmDeployError("invalid cutover app path")
    run_root = selected.get("run_root")
    if run_root in (None, ""):
        run_root = (
            "/mnt/polymarket-data/paper_v7_london"
            if user == "ubuntu"
            else f"/home/{user}/polymarket-runs/paper_v7_london"
        )
    elif not isinstance(run_root, str) or not run_root.startswith("/"):
        raise SsmDeployError("unsafe run root")
    if not (
        run_root.startswith(f"/home/{user}/")
        or run_root.startswith("/mnt/polymarket-data/")
    ):
        raise SsmDeployError("unsafe run root")
    archive_root = (
        "/mnt/polymarket-data/paper_v7_london_archives"
        if run_root.startswith("/mnt/polymarket-data/")
        else f"/home/{user}/polymarket-runs/paper_v7_london_archives"
    )
    runtime_root = f"/home/{user}/polymarket-runtime"
    artifact_root = f"/home/{user}/polymarket-artifacts"
    worktree_parent = f"/home/{user}/.cache/polymarket-v7-deploy"
    worktree = f"{worktree_parent}/{expected_sha}"
    return f"""set -euo pipefail
SHA={expected_sha}
SERVICE_USER={shlex.quote(user)}
APP={shlex.quote(app)}
WORKTREE_PARENT={shlex.quote(worktree_parent)}
WORKTREE={shlex.quote(worktree)}
RUNTIME_ROOT={shlex.quote(runtime_root)}
ARTIFACT_ROOT={shlex.quote(artifact_root)}
RUN_ROOT={shlex.quote(run_root)}
ARCHIVE_ROOT={shlex.quote(archive_root)}
[[ -d "$APP/.git" ]]
sudo -u "$SERVICE_USER" git -C "$APP" fetch --no-tags origin main
sudo -u "$SERVICE_USER" git -C "$APP" cat-file -e "$SHA^{{commit}}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$WORKTREE_PARENT"
sudo -u "$SERVICE_USER" git -C "$APP" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
rm -rf -- "$WORKTREE"
sudo -u "$SERVICE_USER" git -C "$APP" worktree prune
sudo -u "$SERVICE_USER" git -C "$APP" worktree add --detach "$WORKTREE" "$SHA" >/dev/null
cleanup() {{
  sudo -u "$SERVICE_USER" git -C "$APP" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf -- "$WORKTREE"
}}
trap cleanup EXIT
[[ "$(sudo -u "$SERVICE_USER" git -C "$WORKTREE" rev-parse HEAD)" == "$SHA" ]]
[[ -z "$(sudo -u "$SERVICE_USER" git -C "$WORKTREE" status --porcelain)" ]]
# Stage/build/test runs entirely as the service user that owns the immutable
# worktree. Keep root only for the later cutover/systemd control plane.
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$RUNTIME_ROOT" "$RUNTIME_ROOT/by-sha"
rm -rf -- "$RUNTIME_ROOT/by-sha/$SHA"
sudo -u "$SERVICE_USER" -H env POLYMARKET_EXPECTED_SHA="$SHA" \
  POLYMARKET_SERVICE_USER="$SERVICE_USER" \
  POLYMARKET_APP_DIR="$WORKTREE" \
  POLYMARKET_RUNTIME_ROOT="$RUNTIME_ROOT" \
  bash "$WORKTREE/ops/v7_london_stage_release.sh"
env POLYMARKET_EXPECTED_SHA="$SHA" \
  POLYMARKET_SERVICE_USER="$SERVICE_USER" \
  POLYMARKET_APP_DIR="$WORKTREE" \
  POLYMARKET_RUNTIME_ROOT="$RUNTIME_ROOT" \
  POLYMARKET_ARTIFACT_ROOT="$ARTIFACT_ROOT" \
  PM_V7_RUN_ROOT="$RUN_ROOT" \
  PM_V7_ARCHIVE_ROOT="$ARCHIVE_ROOT" \
  POLYMARKET_RUNTIME_HEALTH_ATTEMPTS=390 \
  bash "$WORKTREE/ops/v7_london_cutover.sh"
python3 - "$RUN_ROOT" "$SHA" <<'PY'
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]);sha=sys.argv[2]
r=json.loads((root/'control/runtime_status.json').read_text())
assert r.get('state')=='running'
assert r.get('model_sha')==sha
assert r.get('paper_only') is True
assert r.get('authenticated_execution') is False
assert r.get('real_order_submission') is False
pid=int(r.get('pid') or 0); assert pid>0; os.kill(pid,0)
assert int(time.time())-int(r.get('timestamp') or 0)<=30
print('V7_SSM_CUTOVER='+json.dumps({{
 'sha':sha,'run_root':str(root),'pid':pid,
 'paper_only':True,'authenticated_execution':False,'real_order_submission':False
}},sort_keys=True,separators=(',',':')))
PY
"""


def deploy(region: str, stack_name: str, expected_sha: str,
           expected_tailscale_ip: str, expected_instance_id: str,
           artifact: Path) -> dict[str, Any]:
    if region != REGION or not exact_sha(expected_sha):
        raise SsmDeployError("eu-west-2 and exact SHA required")
    # Prove the runner's AWS identity before any remote operation.
    identity = aws_json(region, ["sts", "get-caller-identity"])
    candidates = candidate_instances(region, stack_name)
    probes = probe(region, candidates)
    selected = select_target(probes, expected_tailscale_ip, expected_instance_id)
    user = selected["app"]["user"]
    artifact_result = upload_artifact(
        region, selected["instance_id"], artifact, expected_sha, user,
    )
    stdout, stderr = run(
        region, selected["instance_id"],
        cutover_command(expected_sha, selected),
        3600,
    )
    receipt = parse_marker(stdout, "V7_SSM_CUTOVER=")
    if receipt.get("sha") != expected_sha:
        raise SsmDeployError("cutover receipt SHA mismatch")
    return {
        "schema": "polymarket_v7_ssm_deploy_receipt_v1",
        "expected_sha": expected_sha,
        "region": region,
        "aws_account": identity.get("Account"),
        "selected": selected,
        "probes": probes,
        "artifact": artifact_result,
        "cutover": receipt,
        "stderr_tail": stderr[-2000:],
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-tailscale-ip", default="")
    parser.add_argument("--expected-instance-id", default="")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--stack-name", default=STACK)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = deploy(
            args.region, args.stack_name, args.expected_sha,
            args.expected_tailscale_ip, args.expected_instance_id, args.artifact,
        )
    except (OSError, ValueError, SsmDeployError) as exc:
        parser.exit(2, f"v7_london_ssm_deploy: {exc}\\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\\n",
        encoding="utf-8",
    )
    print("ssm_deploy_result=success")
    print(f"deployed_sha={receipt['expected_sha']}")
    print(f"ssm_instance={receipt['selected']['instance_id']}")
    print(f"ssm_selection_reason={receipt['selected']['selection_reason']}")
    print(f"ssm_receipt={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
