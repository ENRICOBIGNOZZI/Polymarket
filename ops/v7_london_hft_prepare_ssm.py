#!/usr/bin/env python3
"""Prepare, reboot and audit all three London PAPER HFT benchmark hosts over SSM."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from v7_london_ssm_benchmark import aws_json, send, stack_instances, wait_one

SCHEMA = "polymarket_v7_london_hft_prepare_ssm_v1"


def valid_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def prepare_command(sha: str, service_user: str, zone: str) -> str:
    if not valid_sha(sha):
        raise ValueError("exact SHA required")
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
RUN=/mnt/polymarket-data/paper_v7_london
BENCH=/mnt/polymarket-data/benchmarks
SHA={sha}
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "$SHA" ]]
! systemctl is-active --quiet polymarket-v7-paper.service
mkdir -p "$BENCH"
python3 "$APP/scripts/v7_runtime_resource_plan.py" \
  --config "$APP/config/v7_runtime_resources.json" \
  --output "$BENCH/hft-resource-plan.$SHA.{zone}.json"
python3 "$APP/ops/v7_linux_hft_prepare.py" \
  --policy "$APP/config/v7_linux_hft_host_policy.json" \
  --resource-plan "$BENCH/hft-resource-plan.$SHA.{zone}.json" \
  --receipt "$BENCH/hft-prepare.$SHA.{zone}.json" --apply
python3 - "$BENCH/hft-prepare.$SHA.{zone}.json" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); assert v['preparation_complete'] is True and v['reboot_required'] is True
assert v['paper_only'] is True and v['authenticated_execution'] is False and v['real_order_submission'] is False
print('V7_HFT_PREP='+json.dumps(v,sort_keys=True,separators=(',',':')))
PY'''


def audit_command(sha: str, service_user: str, zone: str) -> str:
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
BENCH=/mnt/polymarket-data/benchmarks
SHA={sha}
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "$SHA" ]]
! systemctl is-active --quiet polymarket-v7-paper.service
python3 "$APP/scripts/v7_runtime_resource_plan.py" \
  --config "$APP/config/v7_runtime_resources.json" \
  --output "$BENCH/hft-resource-plan.$SHA.{zone}.postreboot.json"
set +e
python3 "$APP/ops/v7_linux_hft_host_audit.py" \
  --policy "$APP/config/v7_linux_hft_host_policy.json" \
  --resource-plan "$BENCH/hft-resource-plan.$SHA.{zone}.postreboot.json" \
  --output "$BENCH/hft-audit.$SHA.{zone}.json"
rc=$?
set -e
python3 - "$BENCH/hft-audit.$SHA.{zone}.json" "$rc" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); rc=int(sys.argv[2])
assert not v['hard_failures'], v['hard_failures']
assert v['baseline_ready'] is True, v['baseline_failures']
# Network readiness is intentionally a separate ENA A/B gate.
print('V7_HFT_AUDIT='+json.dumps(v,sort_keys=True,separators=(',',':')))
PY'''


def parse_envelope(stdout: str, prefix: str) -> dict[str, Any]:
    rows = [line[len(prefix):] for line in stdout.splitlines() if line.startswith(prefix)]
    if len(rows) != 1:
        raise ValueError(f"exactly one {prefix} envelope required")
    value = json.loads(rows[0])
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def aws_nojson(region: str, args: list[str]) -> None:
    value = subprocess.run(
        ["aws", *args, "--region", region], text=True, capture_output=True,
        check=False, env={**os.environ, "AWS_PAGER": ""})
    if value.returncode != 0:
        raise RuntimeError(value.stderr.strip() or "AWS CLI failed")


def wait_ssm_online(region: str, instance_id: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        value = aws_json(region, ["ssm", "describe-instance-information",
            "--filters", f"Key=InstanceIds,Values={instance_id}"])
        rows = value.get("InstanceInformationList") or []
        if any(row.get("InstanceId") == instance_id and row.get("PingStatus") == "Online" for row in rows):
            return
        time.sleep(5)
    raise TimeoutError(f"SSM did not return online: {instance_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--stack-name", default="polymarket-v7-london-shootout")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--service-user", default="ubuntu")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.region != "eu-west-2" or not valid_sha(args.expected_sha):
        raise SystemExit("eu-west-2 and exact SHA required")

    stack = aws_json(args.region, ["cloudformation", "describe-stacks", "--stack-name", args.stack_name])
    instances = stack_instances(stack)
    prepared: dict[str, Any] = {}
    for zone, instance in instances.items():
        command_id = send(args.region, instance,
                          prepare_command(args.expected_sha, args.service_user, zone), 900)
        result = wait_one(args.region, command_id, instance, time.monotonic() + 1200, 5)
        if result.get("Status") != "Success":
            raise RuntimeError(f"{zone} HFT prepare failed: {result.get('StandardErrorContent') or result.get('Status')}")
        prepared[zone] = parse_envelope(str(result.get("StandardOutputContent") or ""), "V7_HFT_PREP=")

    aws_nojson(args.region, ["ec2", "reboot-instances", "--instance-ids", *instances.values()])
    for instance in instances.values():
        aws_nojson(args.region, ["ec2", "wait", "instance-status-ok", "--instance-ids", instance])
        wait_ssm_online(args.region, instance, time.monotonic() + 600)

    audited: dict[str, Any] = {}
    for zone, instance in instances.items():
        command_id = send(args.region, instance,
                          audit_command(args.expected_sha, args.service_user, zone), 300)
        result = wait_one(args.region, command_id, instance, time.monotonic() + 600, 5)
        if result.get("Status") != "Success":
            raise RuntimeError(f"{zone} HFT audit failed: {result.get('StandardErrorContent') or result.get('Status')}")
        audited[zone] = parse_envelope(str(result.get("StandardOutputContent") or ""), "V7_HFT_AUDIT=")

    receipt = {
        "schema": SCHEMA,
        "timestamp": int(time.time()),
        "exact_code_sha": args.expected_sha,
        "region": args.region,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "automatic_cutover": False,
        "prepared": prepared,
        "audited": audited,
        "baseline_ready_all_zones": all(v.get("baseline_ready") is True for v in audited.values()),
        "network_ready_all_zones": all(v.get("network_ready") is True for v in audited.values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"receipt={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
