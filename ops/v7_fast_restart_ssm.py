#!/usr/bin/env python3
"""Fast restart an existing London PAPER runtime through AWS SSM.

This is intentionally not a deploy/cutover path. It restarts the already
installed PAPER unit, verifies the exact runtime SHA and PAPER-only safety
contract, then checks Prometheus, Grafana, and the V7 exporter locally.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REQUEST_KEYS = {
    "schema",
    "version",
    "request_id",
    "expected_sha",
    "paper_only",
    "authenticated_execution",
    "real_order_submission",
    "restart_approved",
    "trigger",
}


class FastRestartError(RuntimeError):
    pass


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and completed.returncode != 0:
        raise FastRestartError(
            f"command failed: {' '.join(args)}\n"
            f"stdout={completed.stdout[-4000:]!r}\n"
            f"stderr={completed.stderr[-4000:]!r}"
        )
    return completed


def aws_json(region: str, args: list[str]) -> dict[str, Any]:
    completed = run(["aws", *args, "--region", region, "--output", "json"])
    try:
        value = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FastRestartError("AWS returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise FastRestartError("AWS JSON object required")
    return value


def load_request(path: Path) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    if set(value) != REQUEST_KEYS:
        raise FastRestartError(f"invalid request keys: {sorted(value)}")
    if value.get("schema") != "polymarket_v7_paper_fast_restart_v5_request_v1":
        raise FastRestartError("invalid request schema")
    if value.get("version") != 1:
        raise FastRestartError("invalid request version")
    if not isinstance(value.get("request_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{8,128}", value["request_id"]
    ):
        raise FastRestartError("invalid request id")
    expected_sha = value.get("expected_sha")
    if not isinstance(expected_sha, str) or not SHA_RE.fullmatch(expected_sha):
        raise FastRestartError("exact expected_sha required")
    if value.get("paper_only") is not True:
        raise FastRestartError("paper_only must be true")
    if value.get("authenticated_execution") is not False:
        raise FastRestartError("authenticated_execution must be false")
    if value.get("real_order_submission") is not False:
        raise FastRestartError("real_order_submission must be false")
    if value.get("restart_approved") is not True:
        raise FastRestartError("restart_approved must be true")
    if value.get("trigger") != "GITHUB_AWS_OIDC_SSM_FAST_RESTART_V5":
        raise FastRestartError("invalid trigger")
    return expected_sha


def runtime_checker_b64() -> str:
    checker = r'''
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
sha = sys.argv[2]
status = root / 'control' / 'runtime_status.json'
if not status.is_file():
    raise SystemExit('runtime_status_missing')
row = {}
for _ in range(20):
    row = json.loads(status.read_text(encoding='utf-8'))
    if row.get('state') == 'running' and row.get('killed') is not True:
        break
    time.sleep(1)
safe = {
    key: row.get(key)
    for key in [
        'state',
        'killed',
        'model_sha',
        'paper_only',
        'authenticated_execution',
        'real_order_submission',
        'pid',
        'readiness',
        'economic_decision_state',
        'authorized_alpha_actions',
        'safe_actions',
        'timestamp',
    ]
}
print('V7_FAST_V5_RUNTIME=' + json.dumps(safe, sort_keys=True, separators=(',', ':')))
if safe.get('model_sha') != sha:
    raise SystemExit('sha_mismatch ' + json.dumps(safe, sort_keys=True))
if safe.get('paper_only') is not True:
    raise SystemExit('paper_only_failed ' + json.dumps(safe, sort_keys=True))
if safe.get('authenticated_execution') is not False:
    raise SystemExit('authenticated_execution_enabled ' + json.dumps(safe, sort_keys=True))
if safe.get('real_order_submission') is not False:
    raise SystemExit('real_order_submission_enabled ' + json.dumps(safe, sort_keys=True))
if safe.get('state') != 'running' or safe.get('killed') is True:
    raise SystemExit('runtime_not_running ' + json.dumps(safe, sort_keys=True))
pid = int(safe.get('pid') or 0)
if pid <= 0:
    raise SystemExit('pid_missing ' + json.dumps(safe, sort_keys=True))
os.kill(pid, 0)
if int(time.time()) - int(safe.get('timestamp') or 0) > 180:
    raise SystemExit('runtime_status_stale ' + json.dumps(safe, sort_keys=True))
'''.strip() + "\n"
    return base64.b64encode(checker.encode("utf-8")).decode("ascii")


def remote_script(expected_sha: str) -> str:
    check_b64 = runtime_checker_b64()
    return f'''set -euo pipefail
SHA={expected_sha!r}
CHECK_B64={check_b64!r}
UNIT=polymarket-v7-paper.service
EXPORTER=polymarket-v7-exporter.service

echo fast_v5=restart_begin
systemctl reset-failed "$UNIT" || true
systemctl reset-failed "$EXPORTER" || true
systemctl restart "$EXPORTER" || true
systemctl restart "$UNIT"
for i in $(seq 1 20); do
  if systemctl is-active --quiet "$UNIT"; then
    break
  fi
  sleep 1
done
if ! systemctl is-active --quiet "$UNIT"; then
  echo fast_v5=paper_unit_inactive
  systemctl status "$UNIT" --no-pager -l || true
  journalctl -u "$UNIT" -n 200 --no-pager || true
  exit 20
fi

ROOT="$(systemctl show "$UNIT" -p Environment --value | python3 -c 'import shlex,sys; print(next((x.split("=",1)[1] for x in shlex.split(sys.stdin.read()) if x.startswith("PM_V7_RUN_ROOT=")), ""))')"
if [[ -z "$ROOT" || "$ROOT" != /* ]]; then
  echo fast_v5=run_root_missing
  exit 21
fi
printf '%s' "$CHECK_B64" | base64 -d >/tmp/v7_fast_v5_check.py
python3 /tmp/v7_fast_v5_check.py "$ROOT" "$SHA"

echo fast_v5=metrics
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null
curl -fsS http://127.0.0.1:3000/api/health >/dev/null
curl -fsS --get --data-urlencode 'query=up{{job="polymarket-v7"}}' http://127.0.0.1:9090/api/v1/query | python3 -c 'import json,sys; v=json.load(sys.stdin); r=v.get("data",{{}}).get("result",[]); assert len(r)==1 and r[0]["value"][1]=="1", v'
curl -fsS http://127.0.0.1:9108/metrics | grep -E '^polymarket_v7_execution_alive(\{{| )' >/dev/null
echo V7_FAST_RESTART_V5_OK="$SHA"
'''


def send_fast_restart(region: str, instance_id: str, expected_sha: str, timeout_s: int) -> dict[str, Any]:
    script = remote_script(expected_sha)
    script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = (
        "printf '%s' '"
        + script_b64
        + "' | base64 -d >/tmp/v7_fast_restart_v5.sh && bash /tmp/v7_fast_restart_v5.sh"
    )
    parameters = json.dumps({"commands": [command], "executionTimeout": [str(timeout_s)]})
    response = aws_json(
        region,
        [
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            parameters,
            "--comment",
            "Polymarket V7 PAPER fast restart v5",
        ],
    )
    command_id = (response.get("Command") or {}).get("CommandId")
    if not isinstance(command_id, str) or not command_id:
        raise FastRestartError("SSM command id missing")
    deadline = time.monotonic() + timeout_s + 30
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = aws_json(
            region,
            [
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            ],
        )
        if last.get("Status") in TERMINAL:
            break
        time.sleep(2)
    else:
        raise FastRestartError(f"SSM command timeout command={command_id}")
    stdout = str(last.get("StandardOutputContent") or "")
    stderr = str(last.get("StandardErrorContent") or "")
    print(stdout[-16000:])
    if last.get("Status") != "Success":
        if stderr:
            print(stderr[-10000:], file=sys.stderr)
        raise FastRestartError(f"fast restart failed status={last.get('Status')} command={command_id}")
    return {
        "schema": "polymarket_v7_paper_fast_restart_v5_receipt_v1",
        "expected_sha": expected_sha,
        "instance_id": instance_id,
        "region": region,
        "command_id": command_id,
        "status": last.get("Status"),
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=150)
    args = parser.parse_args()
    try:
        expected_sha = load_request(args.request)
        receipt = send_fast_restart(args.region, args.instance_id, expected_sha, args.timeout_seconds)
    except (OSError, ValueError, FastRestartError) as exc:
        parser.exit(2, f"v7_fast_restart_ssm: {exc}\n")
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("fast_restart_result=success")
    print(f"fast_restart_sha={expected_sha}")
    print(f"fast_restart_receipt={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
