#!/usr/bin/env python3
"""Collect read-only London multi-path latency evidence on all three EC2 hosts."""
from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path
from typing import Any

from v7_london_ssm_benchmark import aws_json, send, stack_instances, wait_one

SCHEMA = "polymarket_v7_london_multipath_v1"


def exact_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def remote_command(sha: str, zone_id: str, service_user: str,
                   ws_duration: int, tls_samples: int, feed_duration: int) -> str:
    if not exact_sha(sha):
        raise ValueError("invalid exact SHA")
    if zone_id not in {"euw2-az1", "euw2-az2", "euw2-az3"}:
        raise ValueError("invalid physical zone id")
    if not 5 <= ws_duration <= 3600 or not 1 <= tls_samples <= 1000 \
            or not 1 <= feed_duration <= 3600:
        raise ValueError("probe bounds invalid")
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
OUT=/mnt/polymarket-data/benchmarks/multipath-{zone_id}
[[ "$OUT" == /mnt/polymarket-data/benchmarks/multipath-* ]]
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "{sha}" ]]
! systemctl is-active --quiet polymarket-v7-paper.service
SERVICE_GROUP="$(id -gn {service_user})"
rm -rf "$OUT"
install -d -o {service_user} -g "$SERVICE_GROUP" "$OUT"
[[ "$(stat -c '%U' "$OUT")" == "{service_user}" ]]
sudo -u {service_user} python3 "$APP/scripts/v7_public_ws_latency_probe.py" \
  --app "$APP" --region {zone_id} --expected-sha {sha} \
  --duration {ws_duration} --handshakes 30 --output "$OUT/polymarket-ws.json"
sudo -u {service_user} python3 "$APP/scripts/v7_external_tls_probe.py" \
  --app "$APP" --region {zone_id} --expected-sha {sha} \
  --samples {tls_samples} --output "$OUT/external-tls.json"
sudo -u {service_user} python3 "$APP/scripts/v7_external_ws_age_probe.py" \
  --app "$APP" --region {zone_id} --expected-sha {sha} \
  --duration {feed_duration} --output "$OUT/external-ws.json"
python3 - "$OUT" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
value={{name:json.load(open(root/file)) for name,file in {{
 'polymarket_ws':'polymarket-ws.json','external_tls':'external-tls.json',
 'external_ws':'external-ws.json'}}.items()}}
print('V7_MULTIPATH='+json.dumps(value,sort_keys=True,separators=(',',':')))
PY'''


def parse_result(stdout: str) -> dict[str, Any]:
    rows = [line[len("V7_MULTIPATH="):] for line in stdout.splitlines()
            if line.startswith("V7_MULTIPATH=")]
    if len(rows) != 1:
        raise ValueError("exactly one V7_MULTIPATH result required")
    value = json.loads(rows[0])
    if not isinstance(value, dict) or set(value) != {"polymarket_ws", "external_tls", "external_ws"}:
        raise ValueError("malformed multipath result")
    return value


def validate_result(value: dict[str, Any], zone_id: str, sha: str) -> None:
    for name in ("polymarket_ws", "external_tls", "external_ws"):
        row = value.get(name)
        if not isinstance(row, dict):
            raise ValueError(f"{name} result missing")
        if row.get("region") != zone_id or row.get("exact_code_sha") != sha:
            raise ValueError(f"{name} identity mismatch")
        if row.get("paper_only") is not True \
                or row.get("authenticated_execution") is not False \
                or row.get("real_order_submission") is not False:
            raise ValueError(f"{name} safety boundary invalid")
        if row.get("authorizes_live_execution") not in (None, False):
            raise ValueError(f"{name} unexpectedly authorizes execution")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--stack-name", default="polymarket-v7-london-shootout")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--service-user", default="ubuntu")
    parser.add_argument("--ws-duration", type=int, default=120)
    parser.add_argument("--tls-samples", type=int, default=30)
    parser.add_argument("--feed-duration", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.region != "eu-west-2" or not exact_sha(args.expected_sha):
        raise SystemExit("eu-west-2 and exact SHA required")
    stack = aws_json(args.region, [
        "cloudformation", "describe-stacks", "--stack-name", args.stack_name,
    ])
    instances = stack_instances(stack)
    submitted: dict[str, dict[str, str]] = {}
    timeout_s = max(900, args.ws_duration + 5 * args.feed_duration + 300)
    for zone_id, instance_id in instances.items():
        command = remote_command(
            args.expected_sha, zone_id, args.service_user,
            args.ws_duration, args.tls_samples, args.feed_duration,
        )
        submitted[zone_id] = {
            "instance_id": instance_id,
            "command_id": send(args.region, instance_id, command, timeout_s),
        }
    deadline = time.monotonic() + timeout_s + 300
    results: dict[str, Any] = {}
    for zone_id in sorted(submitted):
        identity = submitted[zone_id]
        invocation = wait_one(
            args.region, identity["command_id"], identity["instance_id"], deadline, 5.0,
        )
        if invocation.get("Status") != "Success":
            error = str(invocation.get("StandardErrorContent") or "").strip()
            raise RuntimeError(
                f"{zone_id} multipath failed command={identity['command_id']}: "
                f"{error[-2000:] or invocation.get('StatusDetails') or invocation.get('Status')}"
            )
        value = parse_result(str(invocation.get("StandardOutputContent") or ""))
        validate_result(value, zone_id, args.expected_sha)
        results[zone_id] = value
    receipt = {
        "schema": SCHEMA, "timestamp": int(time.time()),
        "expected_sha": args.expected_sha, "region": args.region,
        "commands": submitted, "results": results,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "automatic_cutover": False,
        "selection_ready": False, "selected_zone": None,
        "authenticated_order_latency_observed": False,
        "one_way_latency_proven": False,
        "authorizes_live_execution": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(f"receipt={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
