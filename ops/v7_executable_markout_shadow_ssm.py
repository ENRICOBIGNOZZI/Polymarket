#!/usr/bin/env python3
"""Install/start the zero-authority executable-markout forward shadow on London via SSM."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

from v7_london_ssm_deploy import REGION, run

INSTANCE_RE = re.compile(r"^i-[0-9a-f]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 9000
UNIT = "polymarket-v7-executable-markout-shadow.service"
SCRIPT = "scripts/v7_executable_markout_forward_shadow.py"
ARTIFACT = "docs/research/historical-walk-forward-v2-2026-09-21/full_window_repricing_models.json"


def load_request(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema", "version", "request_id", "instance_id", "expected_parent_sha",
        "paper_only", "authenticated_execution", "real_order_submission",
        "real_capital_at_risk", "execution_authority", "automatic_promotion",
        "poll_ms", "maximum_inference_age_ms",
    }
    if set(value) != required:
        raise ValueError("unexpected request fields")
    if value["schema"] != "polymarket_v7_executable_markout_shadow_ssm_request_v1" or value["version"] != 1:
        raise ValueError("invalid request schema")
    if not isinstance(value["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value["request_id"]):
        raise ValueError("invalid request id")
    if not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise ValueError("invalid instance id")
    if not SHA_RE.fullmatch(value["expected_parent_sha"]):
        raise ValueError("invalid expected parent sha")
    if not (
        value["paper_only"] is True
        and value["authenticated_execution"] is False
        and value["real_order_submission"] is False
        and value["real_capital_at_risk"] is False
        and value["execution_authority"] is False
        and value["automatic_promotion"] is False
    ):
        raise ValueError("zero-authority contract violated")
    if not 1 <= int(value["poll_ms"]) <= 20:
        raise ValueError("invalid poll_ms")
    if not 10 <= int(value["maximum_inference_age_ms"]) <= 100:
        raise ValueError("invalid maximum inference age")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle(repo: Path) -> tuple[bytes, str]:
    artifact = repo / ARTIFACT
    script = repo / SCRIPT
    if not artifact.is_file() or not script.is_file():
        raise FileNotFoundError("shadow source/artifact missing")
    artifact_hash = sha256_bytes(artifact.read_bytes())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(script, arcname="v7_executable_markout_forward_shadow.py", recursive=False)
        archive.add(artifact, arcname="full_window_repricing_models.json", recursive=False)
    return buffer.getvalue(), artifact_hash


def remote_context(instance: str) -> dict:
    command = r"""set -euo pipefail
python3 - <<'PY'
import json,shlex,subprocess
from pathlib import Path
unit='polymarket-v7-paper.service'
def out(args):
    return subprocess.check_output(args,text=True).strip()
assert out(['systemctl','is-active',unit])=='active'
user=out(['systemctl','show',unit,'-p','User','--value'])
group=out(['systemctl','show',unit,'-p','Group','--value']) or user
pid=int(out(['systemctl','show',unit,'-p','MainPID','--value']))
env=out(['systemctl','show',unit,'-p','Environment','--value'])
run_root=Path(next(x.split('=',1)[1] for x in shlex.split(env) if x.startswith('PM_V7_RUN_ROOT='))).resolve()
status=json.loads((run_root/'control/runtime_status.json').read_text())
assert status.get('paper_only') is True
assert status.get('authenticated_execution') is False
assert status.get('real_order_submission') is False
assert status.get('state')=='running'
assert pid>0
print('SHADOW_CONTEXT='+json.dumps({
  'user':user,'group':group,'paper_pid':pid,'run_root':str(run_root),
  'runtime_sha':status.get('model_sha')
},sort_keys=True,separators=(',',':')))
PY"""
    stdout, _ = run(REGION, instance, command, 90)
    row = next(line for line in stdout.splitlines() if line.startswith("SHADOW_CONTEXT="))
    return json.loads(row.split("=", 1)[1])


def upload(instance: str, remote_archive: str, payload: bytes) -> str:
    digest = sha256_bytes(payload)
    run(REGION, instance, f"rm -f {remote_archive}; : > {remote_archive}; chmod 600 {remote_archive}", 60)
    for offset in range(0, len(payload), CHUNK):
        encoded = base64.b64encode(payload[offset:offset + CHUNK]).decode()
        code = (
            "import base64;f=open(" + repr(remote_archive) + ",'ab');"
            "f.write(base64.b64decode(" + repr(encoded) + "));f.close()"
        )
        run(REGION, instance, "python3 -c " + repr(code), 60)
    verify = "python3 -c " + repr(
        "import hashlib;print(hashlib.sha256(open(" + repr(remote_archive) + ",'rb').read()).hexdigest())"
    )
    stdout, _ = run(REGION, instance, verify, 60)
    if stdout.strip().splitlines()[-1] != digest:
        raise RuntimeError("remote bundle hash mismatch")
    return digest


def install(instance: str, context: dict, source_sha: str, artifact_sha: str,
            archive_sha: str, request: dict) -> dict:
    user, group = context["user"], context["group"]
    run_root = context["run_root"]
    pre_pid = int(context["paper_pid"])
    bundle_dir = f"/opt/polymarket-v7-research/executable-markout/{source_sha}"
    output_dir = f"{run_root}/research/executable_markout_forward_shadow"
    remote_archive = f"/tmp/polymarket-markout-shadow-{source_sha[:12]}.tgz"
    poll_ms = int(request["poll_ms"])
    max_age = int(request["maximum_inference_age_ms"])
    command = f"""set -euo pipefail
UNIT={UNIT!r}
SOURCE_SHA={source_sha!r}
ARTIFACT_SHA={artifact_sha!r}
ARCHIVE_SHA={archive_sha!r}
BUNDLE={bundle_dir!r}
OUT={output_dir!r}
RUN_ROOT={run_root!r}
SERVICE_USER={user!r}
SERVICE_GROUP={group!r}
PRE_PID={pre_pid}

test "$(sha256sum {remote_archive} | awk '{{print $1}}')" = "$ARCHIVE_SHA"
install -d -m 0755 "$BUNDLE"
tar -xzf {remote_archive} -C "$BUNDLE"
test "$(sha256sum "$BUNDLE/full_window_repricing_models.json" | awk '{{print $1}}')" = "$ARTIFACT_SHA"
chmod 0555 "$BUNDLE/v7_executable_markout_forward_shadow.py"
chmod 0444 "$BUNDLE/full_window_repricing_models.json"
chown -R root:root "$BUNDLE"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$OUT"

cat > /etc/systemd/system/$UNIT <<EOF
[Unit]
Description=Polymarket V7 executable-markout forward shadow (zero authority)
After=polymarket-v7-paper.service
ConditionPathExists=$RUN_ROOT/control/native_engine_manager_status.json

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$BUNDLE
ExecStart=/usr/bin/python3 $BUNDLE/v7_executable_markout_forward_shadow.py --run-root $RUN_ROOT --artifact $BUNDLE/full_window_repricing_models.json --artifact-sha256 $ARTIFACT_SHA --source-code-sha $SOURCE_SHA --output $OUT/predictions.jsonl --status $OUT/status.json --poll-ms {poll_ms} --maximum-inference-age-ms {max_age}
Restart=always
RestartSec=1
Nice=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
PrivateNetwork=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=$OUT

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
# Keep append-only predictions across upgrades, but remove stale health state so
# post-install validation cannot accidentally read the previous process.
rm -f "$OUT/status.json"
systemctl enable "$UNIT"
systemctl restart "$UNIT"
for i in $(seq 1 30); do
  systemctl is-active --quiet "$UNIT" && test -s "$OUT/status.json" && break
  sleep 1
done
systemctl is-active --quiet "$UNIT"
test "$(systemctl show polymarket-v7-paper.service -p MainPID --value)" = "$PRE_PID"

python3 - "$OUT/status.json" "$SOURCE_SHA" "$ARTIFACT_SHA" {max_age} "$PRE_PID" <<'PY'
import json,subprocess,sys
path,source,artifact,max_age,pre_pid=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),int(sys.argv[5])
v=json.load(open(path,encoding='utf-8'))
assert v.get('schema')=='polymarket_v7_executable_markout_forward_shadow_status_v1'
assert v.get('paper_only') is True
assert v.get('authenticated_execution') is False
assert v.get('real_order_submission') is False
assert v.get('real_capital_at_risk') is False
assert v.get('execution_authority')=='ZERO_AUTHORITY_RESEARCH_ONLY'
assert v.get('automatic_promotion') is False
assert v.get('source_code_sha')==source
assert v.get('artifact_sha256')==artifact
assert v.get('maximum_inference_age_ms')==max_age
assert v.get('state') in {{'COLLECTING','AWAITING_NATIVE_RUN'}}
post_pid=int(subprocess.check_output(['systemctl','show','polymarket-v7-paper.service','-p','MainPID','--value'],text=True).strip())
assert post_pid==pre_pid
print('SHADOW_INSTALLED='+json.dumps({{
 'state':v.get('state'),'scored':v.get('scored'),'timely':v.get('timely'),
 'late':v.get('late'),'timely_fraction':v.get('timely_fraction'),
 'active_run_id':v.get('active_run_id'),'paper_pid_unchanged':True,
 'paper_pid':post_pid,'source_code_sha':source,'artifact_sha256':artifact,
 'maximum_inference_age_ms':max_age
}},sort_keys=True,separators=(',',':')))
PY
rm -f {remote_archive}
"""
    stdout, _ = run(REGION, instance, command, 180)
    row = next(line for line in stdout.splitlines() if line.startswith("SHADOW_INSTALLED="))
    return json.loads(row.split("=", 1)[1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = load_request(args.request)
    if not SHA_RE.fullmatch(args.source_sha) or args.source_sha != request["expected_parent_sha"]:
        raise ValueError("source SHA mismatch")
    repo = Path(__file__).resolve().parents[1]
    payload, artifact_sha = bundle(repo)
    if not HASH_RE.fullmatch(artifact_sha):
        raise ValueError("artifact hash invalid")
    context = remote_context(request["instance_id"])
    remote_archive = f"/tmp/polymarket-markout-shadow-{args.source_sha[:12]}.tgz"
    archive_sha = upload(request["instance_id"], remote_archive, payload)
    installed = install(
        request["instance_id"], context, args.source_sha, artifact_sha,
        archive_sha, request,
    )
    receipt = {
        "schema": "polymarket_v7_executable_markout_shadow_ssm_receipt_v1",
        "source_sha": args.source_sha,
        "artifact_sha256": artifact_sha,
        "archive_sha256": archive_sha,
        "instance_id": request["instance_id"],
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "pre_context": context,
        "installed": installed,
    }
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
