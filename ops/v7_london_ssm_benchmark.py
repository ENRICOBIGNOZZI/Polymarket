#!/usr/bin/env python3
"""Run the fail-closed London PAPER latency probe on all three EC2 hosts via SSM."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

ZONE_OUTPUTS = {
    "euw2-az1": "InstanceAz1",
    "euw2-az2": "InstanceAz2",
    "euw2-az3": "InstanceAz3",
}
TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}


def aws_json(region: str, args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json"],
        text=True, capture_output=True, check=False,
        env={**__import__("os").environ, "AWS_PAGER": ""},
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "AWS CLI failed")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("AWS JSON object required")
    return value


def stack_instances(value: dict[str, Any]) -> dict[str, str]:
    stacks = value.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise ValueError("exactly one stack required")
    outputs = {x.get("OutputKey"): x.get("OutputValue") for x in stacks[0].get("Outputs", [])}
    result: dict[str, str] = {}
    for zone_id, key in ZONE_OUTPUTS.items():
        instance_id = outputs.get(key)
        if not isinstance(instance_id, str) or not instance_id.startswith("i-"):
            raise ValueError(f"missing stack output {key}")
        result[zone_id] = instance_id
    if len(set(result.values())) != 3:
        raise ValueError("three distinct instances required")
    return result


def remote_command(sha: str, mode: str, service_user: str) -> str:
    if mode not in {"smoke", "formal"}:
        raise ValueError("mode must be smoke or formal")
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("invalid exact SHA")
    script = f'''set -euo pipefail
APP=/home/{service_user}/polymarket
RUN=/mnt/polymarket-data/paper_v7_london
BENCH=/mnt/polymarket-data/benchmarks
[[ "$(git -C "$APP" rev-parse HEAD)" == "{sha}" ]]
python3 -c 'import json; v=json.load(open("'$RUN'/bootstrap_receipt.json")); assert v["code_sha"]=="{sha}" and v["systemd_installed_but_disabled"] is True'
! systemctl is-active --quiet polymarket-v7-paper.service
out=$(sudo -u {service_user} env POLYMARKET_EXPECTED_SHA="{sha}" POLYMARKET_APP_DIR="$APP" POLYMARKET_BENCHMARK_DIR="$BENCH" "$APP/ops/v7_london_benchmark.sh" {mode})
probe=$(printf '%s\n' "$out" | awk -F= '$1=="probe"{{print substr($0,7)}}' | tail -1)
manifest=$(printf '%s\n' "$out" | awk -F= '$1=="manifest"{{print substr($0,10)}}' | tail -1)
[[ -f "$probe" && -f "$manifest" ]]
python3 - "$probe" "$manifest" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); m=json.load(open(sys.argv[2]))
print('V7_RESULT='+json.dumps({{'probe':p,'manifest':m}},sort_keys=True,separators=(',',':')))
PY'''
    # AWS-RunShellScript invokes /bin/sh by default. Force Bash because the
    # benchmark intentionally uses pipefail and [[ ... ]] guards.
    return "bash -lc " + shlex.quote(script)

def parse_result(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [line[len("V7_RESULT="):] for line in stdout.splitlines() if line.startswith("V7_RESULT=")]
    if len(rows) != 1:
        raise ValueError("exactly one V7_RESULT envelope required")
    value = json.loads(rows[0])
    if not isinstance(value, dict) or not isinstance(value.get("probe"), dict) \
            or not isinstance(value.get("manifest"), dict):
        raise ValueError("malformed V7_RESULT envelope")
    return value["probe"], value["manifest"]


def send(region: str, instance_id: str, command: str, timeout_s: int) -> str:
    parameters = json.dumps({"commands": [command], "executionTimeout": [str(timeout_s)]})
    value = aws_json(region, [
        "ssm", "send-command", "--instance-ids", instance_id,
        "--document-name", "AWS-RunShellScript", "--parameters", parameters,
        "--comment", "Polymarket V7 London PAPER latency benchmark",
    ])
    command_id = (value.get("Command") or {}).get("CommandId")
    if not isinstance(command_id, str) or not command_id:
        raise RuntimeError("SSM command id missing")
    return command_id


def wait_one(region: str, command_id: str, instance_id: str,
             deadline: float, poll_s: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        value = aws_json(region, ["ssm", "get-command-invocation", "--command-id", command_id,
                                  "--instance-id", instance_id])
        status = value.get("Status")
        if status in TERMINAL:
            return value
        time.sleep(poll_s)
    raise TimeoutError(f"SSM command {command_id} exceeded local deadline")

def evaluate_formal(root: Path, probes: list[Path], output: Path) -> None:
    command = ["python3", str(root / "scripts/v7_regional_shootout.py"),
               "--policy", str(root / "config/v7_latency_slo.json")]
    for zone_id in sorted(ZONE_OUTPUTS):
        command.extend(["--candidate-region", zone_id])
    for probe in probes:
        command.extend(["--probe", str(probe)])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    output.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "formal regional shootout failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("smoke", "formal"))
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--stack-name", default="polymarket-v7-london-shootout")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--service-user", default="ubuntu")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "polymarket-london")
    args = parser.parse_args()
    if args.region != "eu-west-2":
        raise SystemExit("eu-west-2 required")
    root = Path(__file__).resolve().parents[1]
    stack = aws_json(args.region, ["cloudformation", "describe-stacks", "--stack-name", args.stack_name])
    instances = stack_instances(stack)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timeout_s = 1800 if args.mode == "smoke" else 100000
    poll_s = 5.0 if args.mode == "smoke" else 30.0
    commands: dict[str, tuple[str, str]] = {}
    command = remote_command(args.expected_sha, args.mode, args.service_user)
    for zone_id, instance_id in instances.items():
        commands[zone_id] = (instance_id, send(args.region, instance_id, command, timeout_s))
    deadline = time.monotonic() + timeout_s + 600
    probe_paths: list[Path] = []
    receipt = {"schema": "polymarket_v7_london_ssm_benchmark_v1", "mode": args.mode,
               "expected_sha": args.expected_sha, "region": args.region,
               "paper_only": True, "authenticated_execution": False,
               "real_order_submission": False, "automatic_cutover": False,
               "commands": {z: {"instance_id": i, "command_id": c} for z, (i, c) in commands.items()}}
    for zone_id in sorted(commands):
        instance_id, command_id = commands[zone_id]
        value = wait_one(args.region, command_id, instance_id, deadline, poll_s)
        if value.get("Status") != "Success":
            raise RuntimeError(f"{zone_id} SSM benchmark failed: {value.get('StatusDetails') or value.get('Status')}")
        probe, manifest = parse_result(str(value.get("StandardOutputContent") or ""))
        if probe.get("region") != zone_id or manifest.get("zone_id") != zone_id:
            raise RuntimeError(f"{zone_id} returned mismatched physical zone identity")
        if probe.get("exact_code_sha") != args.expected_sha or manifest.get("code_sha") != args.expected_sha:
            raise RuntimeError(f"{zone_id} returned mismatched exact SHA")
        probe_path = args.output_dir / f"{args.mode}.{zone_id}.probe.json"
        manifest_path = args.output_dir / f"{args.mode}.{zone_id}.host.json"
        probe_path.write_text(json.dumps(probe, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        probe_paths.append(probe_path)
    receipt_path = args.output_dir / f"{args.mode}.{args.expected_sha}.ssm.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if args.mode == "formal":
        evaluate_formal(root, probe_paths, args.output_dir / f"formal.{args.expected_sha}.shootout.json")
    print(f"receipt={receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
