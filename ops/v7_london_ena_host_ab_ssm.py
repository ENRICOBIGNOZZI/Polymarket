#!/usr/bin/env python3
"""Run reversible ENA host-only A/B on all three disabled London PAPER hosts."""
from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v7_london_ssm_benchmark import aws_json, send, stack_instances, wait_one


def valid_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def remote_command(sha: str, zone: str, service_user: str,
                   samples: int, interval_ms: int) -> str:
    if not valid_sha(sha):
        raise ValueError("exact SHA required")
    app = f"/home/{service_user}/polymarket"
    bench = "/mnt/polymarket-data/benchmarks"
    output = f"{bench}/ena-host-ab.{sha}.{zone}.json"
    plan = f"{bench}/hft-resource-plan.{sha}.{zone}.ena.json"
    audit = f"{bench}/hft-audit.{sha}.{zone}.pre-ena.json"
    return f'''set -euo pipefail
APP={shlex.quote(app)}
SHA={sha}
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "$SHA" ]]
! systemctl is-active --quiet polymarket-v7-paper.service
mkdir -p {shlex.quote(bench)}
python3 "$APP/scripts/v7_runtime_resource_plan.py" \
  --config "$APP/config/v7_runtime_resources.json" --output {shlex.quote(plan)}
python3 "$APP/ops/v7_linux_hft_host_audit.py" \
  --policy "$APP/config/v7_linux_hft_host_policy.json" \
  --resource-plan {shlex.quote(plan)} --output {shlex.quote(audit)} || true
python3 - {shlex.quote(audit)} <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); assert v['baseline_ready'] is True, v['baseline_failures']
assert not v['hard_failures'], v['hard_failures']
PY
python3 "$APP/ops/v7_ena_host_ab.py" \
  --expected-sha "$SHA" --region-label {shlex.quote(zone)} --app "$APP" \
  --probe "$APP/build/polymarket_v7_latency_probe" \
  --resource-plan {shlex.quote(plan)} --samples {samples} --interval-ms {interval_ms} \
  --output {shlex.quote(output)}
python3 - {shlex.quote(output)} <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); assert v['restored_exactly'] is True
assert v['paper_only'] is True and v['authenticated_execution'] is False and v['real_order_submission'] is False
print('V7_ENA_HOST_AB='+json.dumps(v,sort_keys=True,separators=(',',':')))
PY'''


def parse_result(stdout: str) -> dict[str, Any]:
    prefix = "V7_ENA_HOST_AB="
    rows = [line[len(prefix):] for line in stdout.splitlines() if line.startswith(prefix)]
    if len(rows) != 1:
        raise ValueError("exactly one V7_ENA_HOST_AB envelope required")
    value = json.loads(rows[0])
    if not isinstance(value, dict) or value.get("restored_exactly") is not True:
        raise ValueError("invalid ENA host A/B result")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--stack-name", default="polymarket-v7-london-shootout")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--service-user", default="ubuntu")
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--interval-ms", type=int, default=500)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.region != "eu-west-2" or not valid_sha(args.expected_sha):
        raise SystemExit("eu-west-2 and exact SHA required")
    if not 10 <= args.samples <= 100000 or not 0 <= args.interval_ms <= 60000:
        raise SystemExit("benchmark arguments out of range")

    stack = aws_json(args.region, [
        "cloudformation", "describe-stacks", "--stack-name", args.stack_name])
    instances = stack_instances(stack)
    submitted: dict[str, dict[str, str]] = {}
    timeout_s = max(900, args.samples * max(args.interval_ms, 1) // 1000 * 8 + 300)
    for zone, instance in instances.items():
        command_id = send(
            args.region, instance,
            remote_command(args.expected_sha, zone, args.service_user,
                           args.samples, args.interval_ms), timeout_s)
        submitted[zone] = {"instance_id": instance, "command_id": command_id}

    deadline = time.monotonic() + timeout_s + 600
    results: dict[str, Any] = {}
    for zone in sorted(submitted):
        identity = submitted[zone]
        value = wait_one(args.region, identity["command_id"],
                         identity["instance_id"], deadline, 5.0)
        if value.get("Status") != "Success":
            raise RuntimeError(
                f"{zone} ENA host A/B failed: "
                f"{value.get('StandardErrorContent') or value.get('StatusDetails') or value.get('Status')}")
        result = parse_result(str(value.get("StandardOutputContent") or ""))
        if result.get("exact_code_sha") != args.expected_sha or result.get("region") != zone:
            raise RuntimeError(f"{zone} ENA A/B provenance mismatch")
        results[zone] = result

    receipt = {
        "schema": "polymarket_v7_london_ena_host_ab_ssm_v1",
        "timestamp_ns": time.time_ns(),
        "exact_code_sha": args.expected_sha,
        "region": args.region,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "persistent_tuning": False,
        "automatic_promotion": False,
        "zones": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"receipt={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
