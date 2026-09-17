#!/usr/bin/env python3
"""Run reversible ENA latency A/B on all three London PAPER benchmark hosts."""
from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path
from typing import Any

from v7_london_ssm_benchmark import (
    ZONE_OUTPUTS, aws_json, send, stack_instances, wait_one,
)


def valid_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)


def remote_command(sha: str, zone: str, service_user: str,
                   samples: int, interval_ms: int) -> str:
    if not valid_sha(sha) or zone not in ZONE_OUTPUTS:
        raise ValueError("invalid ENA A/B identity")
    app = f"/home/{service_user}/polymarket"
    output = f"/mnt/polymarket-data/benchmarks/ena-ab.{sha}.{zone}.json"
    return f'''set -euo pipefail
APP={shlex.quote(app)}
RUN=/mnt/polymarket-data/paper_v7_london
OUT={shlex.quote(output)}
LOCK=/mnt/polymarket-data/benchmarks/ena-ab.lock
mkdir -p /mnt/polymarket-data/benchmarks
exec 9>""
flock -n 9 || {{ echo "ENA A/B lock busy" >&2; exit 73; }}
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "{sha}" ]]
python3 -c 'import json; v=json.load(open("'$RUN'/bootstrap_receipt.json")); assert v["code_sha"]=="{sha}" and v["systemd_installed_but_disabled"] is True and v["paper_only"] is True and v["authenticated_execution"] is False and v["real_order_submission"] is False'
! systemctl is-active --quiet polymarket-v7-paper.service
python3 "$APP/ops/v7_ena_latency_ab.py" \
  --expected-sha "{sha}" --region-label "{zone}" --app "$APP" \
  --probe "$APP/build/polymarket_v7_latency_probe" \
  --samples "{samples}" --interval-ms "{interval_ms}" --output "$OUT"
python3 - "$OUT" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))
assert v['exact_code_sha']=="{sha}" and v['region']=="{zone}"
assert v['paper_only'] is True and v['authenticated_execution'] is False
assert v['real_order_submission'] is False and v['persistent_tuning'] is False
assert v['restored_exactly'] is True
print('V7_ENA_AB='+json.dumps(v,sort_keys=True,separators=(',',':')))
PY'''


def parse_result(stdout: str) -> dict[str, Any]:
    rows = [line[len("V7_ENA_AB="):] for line in stdout.splitlines()
            if line.startswith("V7_ENA_AB=")]
    if len(rows) != 1:
        raise ValueError("exactly one V7_ENA_AB envelope required")
    value = json.loads(rows[0])
    if not isinstance(value, dict) or value.get("restored_exactly") is not True:
        raise ValueError("invalid ENA A/B result")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--stack-name", default="polymarket-v7-london-shootout")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--service-user", default="ubuntu")
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--interval-ms", type=int, default=500)
    parser.add_argument("--output-dir", type=Path,
                        default=Path.home() / "polymarket-london")
    args = parser.parse_args()
    if not valid_sha(args.expected_sha):
        raise SystemExit("exact lowercase SHA required")
    if args.region != "eu-west-2":
        raise SystemExit("ENA A/B is restricted to London eu-west-2")
    if args.samples < 10 or args.samples > 100000:
        raise SystemExit("samples out of range")
    stack = aws_json(args.region, [
        "cloudformation", "describe-stacks", "--stack-name", args.stack_name,
    ])
    instances = stack_instances(stack)
    command_ids: dict[str, str] = {}
    for zone, instance_id in instances.items():
        command_ids[zone] = send(
            args.region, instance_id,
            remote_command(args.expected_sha, zone, args.service_user,
                           args.samples, args.interval_ms),
            max(900, args.samples * max(args.interval_ms, 1) // 1000 + 300),
        )

    deadline = time.monotonic() + max(
        1200, args.samples * max(args.interval_ms, 1) / 1000 + 600)
    results: dict[str, Any] = {}
    for zone, instance_id in instances.items():
        value = wait_one(args.region, command_ids[zone], instance_id, deadline, 3.0)
        if value.get("Status") != "Success":
            raise RuntimeError(f"{zone} ENA A/B failed: {value.get('StatusDetails') or value.get('Status')}")
        result = parse_result(value.get("StandardOutputContent", ""))
        if result.get("region") != zone or result.get("exact_code_sha") != args.expected_sha:
            raise RuntimeError(f"{zone} ENA A/B provenance mismatch")
        results[zone] = result

    args.output_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": "polymarket_v7_london_ena_ab_ssm_v1",
        "timestamp_ns": time.time_ns(),
        "exact_code_sha": args.expected_sha,
        "region": args.region,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "persistent_tuning": False,
        "zones": results,
    }
    output = args.output_dir / f"ena-ab.{args.expected_sha}.json"
    output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"result={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
