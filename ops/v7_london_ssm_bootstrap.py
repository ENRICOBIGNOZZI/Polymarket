#!/usr/bin/env python3
"""Re-bootstrap all three London EC2 hosts to one exact PAPER-only SHA via SSM."""
from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path
from typing import Any

from v7_london_ssm_benchmark import aws_json, stack_instances, wait_one

SCHEMA = "polymarket_v7_london_ssm_bootstrap_v1"
TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def remote_command(sha: str, service_user: str) -> str:
    if not exact_sha(sha):
        raise ValueError("invalid exact SHA")
    if not service_user or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in service_user):
        raise ValueError("invalid service user")
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
RUN=/mnt/polymarket-data/paper_v7_london
BENCH=/mnt/polymarket-data/benchmarks
SHA={sha}
RUN_ID="$(cat /proc/sys/kernel/random/uuid)"
[[ "$RUN_ID" =~ ^[0-9a-f-]{{36}}$ ]]
LOG="$BENCH/bootstrap.$SHA.$RUN_ID.log"
mkdir -p "$BENCH"
[[ -d "$APP/.git" ]]
! systemctl is-active --quiet polymarket-v7-paper.service
SERVICE_GROUP="$(id -gn {service_user})"
# This is the disabled benchmark source checkout. Previous root-side bootstrap
# steps may have left .git objects or tracked files root-owned; restore the
# checkout to its declared owner before any git mutation.
chown -R {service_user}:"$SERVICE_GROUP" "$APP"
[[ "$(stat -c '%U' "$APP")" == "{service_user}" ]]
[[ "$(stat -c '%U' "$APP/.git")" == "{service_user}" ]]
dirty_before="$(sudo -u {service_user} git -C "$APP" status --porcelain)"
if [[ -n "$dirty_before" ]]; then
  echo "benchmark_source_checkout_dirty=1"
  printf '%s\n' "$dirty_before" | head -50
  sudo -u {service_user} git -C "$APP" reset --hard HEAD
  sudo -u {service_user} git -C "$APP" clean -fd
fi
[[ -z "$(sudo -u {service_user} git -C "$APP" status --porcelain)" ]]
sudo -u {service_user} git -C "$APP" fetch --no-tags origin main
sudo -u {service_user} git -C "$APP" cat-file -e "$SHA^{{commit}}"
sudo -u {service_user} git -C "$APP" show "$SHA:ops/v7_london_bootstrap.sh" > /tmp/v7_london_bootstrap.$SHA.sh
chmod 755 /tmp/v7_london_bootstrap.$SHA.sh
if ! env POLYMARKET_EXPECTED_SHA="$SHA" POLYMARKET_SERVICE_USER={service_user} \
  POLYMARKET_APP_DIR="$APP" PM_V7_RUN_ROOT="$RUN" \
  POLYMARKET_REUSE_EXACT_SHA_CI=1 PM_V7_CI_REPOSITORY=ENRICOBIGNOZZI/Polymarket \
  POLYMARKET_INSTALL_TAILSCALE=1 POLYMARKET_INSTALL_GRAFANA=1 \
  bash /tmp/v7_london_bootstrap.$SHA.sh >"$LOG" 2>&1; then
  tail -200 "$LOG" >&2 || true
  exit 1
fi
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "$SHA" ]]
[[ -z "$(sudo -u {service_user} git -C "$APP" status --porcelain)" ]]
! systemctl is-active --quiet polymarket-v7-paper.service
python3 - "$RUN/bootstrap_receipt.json" "$SHA" "$LOG" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))
assert v['code_sha']==sys.argv[2]
assert v['paper_only'] is True
assert v['authenticated_execution'] is False
assert v['real_order_submission'] is False
assert v['systemd_installed_but_disabled'] is True
v['ssm_bootstrap_log']=sys.argv[3]
print('V7_BOOTSTRAP='+json.dumps(v,sort_keys=True,separators=(',',':')))
PY'''


def send(region: str, instance_id: str, command: str, timeout_s: int) -> str:
    parameters = json.dumps({
        "commands": [f"bash -lc {shlex.quote(command)}"],
        "executionTimeout": [str(timeout_s)],
    })
    value = aws_json(region, [
        "ssm", "send-command", "--instance-ids", instance_id,
        "--document-name", "AWS-RunShellScript", "--parameters", parameters,
        "--comment", "Polymarket V7 London exact-SHA PAPER bootstrap",
    ])
    command_id = (value.get("Command") or {}).get("CommandId")
    if not isinstance(command_id, str) or not command_id:
        raise RuntimeError("SSM command id missing")
    return command_id


def parse_receipt(stdout: str) -> dict[str, Any]:
    rows = [line[len("V7_BOOTSTRAP="):] for line in stdout.splitlines()
            if line.startswith("V7_BOOTSTRAP=")]
    if len(rows) != 1:
        raise ValueError("exactly one bootstrap receipt required")
    value = json.loads(rows[0])
    if not isinstance(value, dict):
        raise ValueError("bootstrap receipt must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--stack-name", default="polymarket-v7-london-shootout")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--service-user", default="ubuntu")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.region != "eu-west-2" or not exact_sha(args.expected_sha):
        raise SystemExit("eu-west-2 and exact SHA required")

    stack = aws_json(args.region, [
        "cloudformation", "describe-stacks", "--stack-name", args.stack_name,
    ])
    instances = stack_instances(stack)
    command = remote_command(args.expected_sha, args.service_user)
    timeout_s = 3600
    submitted = {
        zone: {"instance_id": instance, "command_id": send(args.region, instance, command, timeout_s)}
        for zone, instance in instances.items()
    }
    deadline = time.monotonic() + timeout_s + 600
    receipts: dict[str, Any] = {}
    for zone in sorted(submitted):
        identity = submitted[zone]
        value = wait_one(
            args.region, identity["command_id"], identity["instance_id"], deadline, 10.0,
        )
        if value.get("Status") != "Success":
            error = str(value.get("StandardErrorContent") or "").strip()
            raise RuntimeError(
                f"{zone} bootstrap failed command={identity['command_id']}: "
                f"{error[-2000:] or value.get('StatusDetails') or value.get('Status')}"
            )
        receipt = parse_receipt(str(value.get("StandardOutputContent") or ""))
        if receipt.get("code_sha") != args.expected_sha:
            raise RuntimeError(f"{zone} bootstrap SHA mismatch")
        receipts[zone] = receipt

    result = {
        "schema": SCHEMA,
        "timestamp": int(time.time()),
        "expected_sha": args.expected_sha,
        "region": args.region,
        "commands": submitted,
        "receipts": receipts,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "runtime_services_enabled": False,
        "automatic_cutover": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"receipt={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
