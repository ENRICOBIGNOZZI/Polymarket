#!/usr/bin/env python3
"""Run fail-closed London PAPER latency probes on the three EC2 benchmark hosts."""
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
        text=True,
        capture_output=True,
        check=False,
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


def _valid_sha(sha: str) -> bool:
    return len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def remote_command(sha: str, mode: str, service_user: str) -> str:
    if mode not in {"smoke", "formal"}:
        raise ValueError("mode must be smoke or formal")
    if not _valid_sha(sha):
        raise ValueError("invalid exact SHA")
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
RUN=/mnt/polymarket-data/paper_v7_london
BENCH=/mnt/polymarket-data/benchmarks
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "{sha}" ]]
python3 -c 'import json; v=json.load(open("'$RUN'/bootstrap_receipt.json")); assert v["code_sha"]=="{sha}" and v["systemd_installed_but_disabled"] is True and v["paper_only"] is True and v["authenticated_execution"] is False and v["real_order_submission"] is False'
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


def detached_formal_command(sha: str, service_user: str, run_id: str) -> str:
    if not _valid_sha(sha):
        raise ValueError("invalid exact SHA")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    if not run_id or any(c not in allowed for c in run_id):
        raise ValueError("invalid run id")
    unit = f"polymarket-v7-formal-{run_id}"
    job = f"/mnt/polymarket-data/benchmarks/formal-jobs/{run_id}"
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
RUN=/mnt/polymarket-data/paper_v7_london
JOB={shlex.quote(job)}
UNIT={shlex.quote(unit)}
SHA={sha}
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "$SHA" ]]
python3 -c 'import json; v=json.load(open("'$RUN'/bootstrap_receipt.json")); assert v["code_sha"]=="{sha}" and v["systemd_installed_but_disabled"] is True and v["paper_only"] is True and v["authenticated_execution"] is False and v["real_order_submission"] is False'
! systemctl is-active --quiet polymarket-v7-paper.service
if systemctl list-units --type=service --state=running --no-legend 'polymarket-v7-formal-*' | grep -q .; then
  echo 'another formal benchmark is already running' >&2
  exit 73
fi
install -d -o {service_user} -g {service_user} "$JOB"
cat > "$JOB/run.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
exec 9>/mnt/polymarket-data/benchmarks/formal.lock
flock -n 9 || {{ echo LOCK_BUSY >&2; exit 73; }}
rc=0
env POLYMARKET_EXPECTED_SHA="{sha}" POLYMARKET_APP_DIR="/home/{service_user}/polymarket" POLYMARKET_BENCHMARK_DIR="/mnt/polymarket-data/benchmarks" \
  "/home/{service_user}/polymarket/ops/v7_london_benchmark.sh" formal > "{job}/stdout.log" 2> "{job}/stderr.log" || rc=$?
printf '%s\n' "$rc" > "{job}/exit_code"
exit "$rc"
SH
chown {service_user}:{service_user} "$JOB/run.sh"
chmod 700 "$JOB/run.sh"
python3 - "$UNIT" "$JOB" "$SHA" > "$JOB/launch.json" <<'PY'
import json,sys,time
print(json.dumps({{'schema':'polymarket_v7_detached_formal_launch_v1','unit':sys.argv[1],'job_dir':sys.argv[2],'code_sha':sys.argv[3],'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'created_at_ns':time.time_ns()}},sort_keys=True))
PY
systemd-run --quiet --unit="$UNIT" --property=User={service_user} --property=Group={service_user} --property=WorkingDirectory="$APP" --property=Type=simple "$JOB/run.sh"
sleep 1
systemctl is-active --quiet "$UNIT" || {{ systemctl status --no-pager "$UNIT" >&2; exit 74; }}
python3 - "$UNIT" "$JOB" "$SHA" <<'PY'
import json,sys
print('V7_LAUNCH='+json.dumps({{'unit':sys.argv[1],'job_dir':sys.argv[2],'code_sha':sys.argv[3]}},sort_keys=True,separators=(',',':')))
PY'''


def collect_formal_command(sha: str, service_user: str, launch: dict[str, str]) -> str:
    if not _valid_sha(sha) or launch.get("code_sha") != sha:
        raise ValueError("invalid formal launch SHA")
    unit = launch.get("unit", "")
    job = launch.get("job_dir", "")
    if not unit.startswith("polymarket-v7-formal-"):
        raise ValueError("invalid formal unit")
    if not job.startswith("/mnt/polymarket-data/benchmarks/formal-jobs/"):
        raise ValueError("invalid formal job directory")
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
UNIT={shlex.quote(unit)}
JOB={shlex.quote(job)}
! systemctl is-active --quiet polymarket-v7-paper.service
python3 - "$JOB/launch.json" "$UNIT" "$JOB" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); assert v['code_sha']=="{sha}" and v['unit']==sys.argv[2] and v['job_dir']==sys.argv[3] and v['paper_only'] is True and v['authenticated_execution'] is False and v['real_order_submission'] is False
PY
if [[ ! -f "$JOB/exit_code" ]]; then
  active=$(systemctl show "$UNIT" -p ActiveState --value 2>/dev/null || true)
  if [[ "$active" == "active" || "$active" == "activating" ]]; then
    echo "V7_PENDING=$UNIT"
    exit 75
  fi
  echo "missing exit code for non-running $UNIT" >&2
  exit 76
fi
rc=$(cat "$JOB/exit_code")
[[ "$rc" == "0" ]] || {{ cat "$JOB/stderr.log" >&2; exit "$rc"; }}
probe=$(awk -F= '$1=="probe"{{print substr($0,7)}}' "$JOB/stdout.log" | tail -1)
manifest=$(awk -F= '$1=="manifest"{{print substr($0,10)}}' "$JOB/stdout.log" | tail -1)
[[ -f "$probe" && -f "$manifest" ]]
python3 - "$probe" "$manifest" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); m=json.load(open(sys.argv[2]))
print('V7_RESULT='+json.dumps({{'probe':p,'manifest':m}},sort_keys=True,separators=(',',':')))
PY'''


def latency_command(sha: str, zone_id: str, service_user: str, samples: int) -> str:
    tune_samples = min(samples, 500)
    if not _valid_sha(sha):
        raise ValueError("invalid exact SHA")
    if zone_id not in ZONE_OUTPUTS:
        raise ValueError("invalid physical zone id")
    if not 100 <= samples <= 20000:
        raise ValueError("latency samples out of range")
    return f'''set -euo pipefail
APP=/home/{service_user}/polymarket
OUT=/mnt/polymarket-data/benchmarks/latency-{zone_id}-{sha}
[[ "$(sudo -u {service_user} git -C "$APP" rev-parse HEAD)" == "{sha}" ]]
! systemctl is-active --quiet polymarket-v7-paper.service
rm -rf "$OUT"
install -d -o {service_user} -g {service_user} "$OUT"
set +e
sudo -u {service_user} env POLYMARKET_APP_DIR="$APP" \
  bash "$APP/ops/v7_london_latency_lab.sh" \
    --sha "{sha}" --samples "{samples}" --output-dir "$OUT"
lab_rc=$?
set -e
if (( lab_rc != 0 )); then
  echo "latency_lab_rc=$lab_rc" >&2
  for name in host.json sign-noipo.json sign-ipo.json sign-pgo-use.json handoff.json public-paired-clob.json summary.json; do
    if [[ -s "$OUT/$name" ]]; then
      echo "===== $name =====" >&2
      tail -c 12000 "$OUT/$name" >&2 || true
      echo >&2
    fi
  done
  exit "$lab_rc"
fi
python3 "$APP/ops/v7_host_latency_ab.py" \
  --expected-sha "{sha}" \
  --probe "$OUT/public-paired-clob-probe" \
  --samples "{tune_samples}" --interval-ms 20 \
  --output "$OUT/host-tuning.json"
python3 - "$OUT/summary.json" "$OUT/host-tuning.json" <<'PY'
import json,sys
v=json.load(open(sys.argv[1],encoding='utf-8'))
t=json.load(open(sys.argv[2],encoding='utf-8'))
assert v['schema']=='polymarket_v7_london_latency_lab_v1'
assert v['sha']=='{sha}'
assert v['paper_only'] is True
assert v['authenticated_execution'] is False
assert v['real_order_submission'] is False
assert t['schema']=='polymarket_v7_host_latency_ab_v1'
assert t['expected_sha']=='{sha}'
assert t['paper_only'] is True
assert t['authenticated_execution'] is False
assert t['real_order_submission'] is False
assert t['restored_exactly'] is True
compact_profiles=[]
for p in t.get('profiles',[]):
    profile=p.get('profile') or {}
    comparison=p.get('comparison') or {}
    compact_profiles.append({
        'name':profile.get('name'),
        'supported':bool(p.get('supported')),
        'error':p.get('error'),
        'promotion_candidate':bool(comparison.get('promotion_candidate',False)),
        'p99_improvement_pct':comparison.get('p99_improvement_pct'),
        'p999_improvement_pct':comparison.get('p999_improvement_pct'),
        'failure_delta':comparison.get('failure_delta'),
        'candidate_p99_ns':comparison.get('candidate_p99_ns'),
        'baseline_p99_ns':comparison.get('baseline_p99_ns'),
        'candidate_p999_ns':comparison.get('candidate_p999_ns'),
        'baseline_p999_ns':comparison.get('baseline_p999_ns'),
    })
v['physical_zone_id']='{zone_id}'
v['host_tuning']={
    'schema':'polymarket_v7_host_latency_ab_compact_v1',
    'restored_exactly':True,
    'persistent_tuning':False,
    'full_evidence_path':str(sys.argv[2]),
    'profiles':compact_profiles,
}
print('V7_LATENCY='+json.dumps(v,sort_keys=True,separators=(',',':')))
PY'''


def parse_latency(stdout: str) -> dict[str, Any]:
    rows = [line[len("V7_LATENCY="):] for line in stdout.splitlines()
            if line.startswith("V7_LATENCY=")]
    if len(rows) != 1:
        raise ValueError("exactly one V7_LATENCY result required")
    value = json.loads(rows[0])
    if not isinstance(value, dict):
        raise ValueError("malformed V7_LATENCY result")
    return value


def evaluate_latency(rows: dict[str, dict[str, Any]], sha: str) -> dict[str, Any]:
    if set(rows) != set(ZONE_OUTPUTS):
        raise ValueError("latency result must cover exactly three physical zones")
    measurements = []
    for zone_id in sorted(rows):
        row = rows[zone_id]
        if row.get("sha") != sha or row.get("physical_zone_id") != zone_id:
            raise ValueError(f"{zone_id} latency identity mismatch")
        if row.get("paper_only") is not True \
                or row.get("authenticated_execution") is not False \
                or row.get("real_order_submission") is not False:
            raise ValueError(f"{zone_id} latency safety boundary invalid")
        network = row["public_transport"]["parallel_persistent_legs"]
        pair = network["pair_completion_ns"]
        measurements.append({
            "physical_zone_id": zone_id,
            "pair_p99_ns": int(pair["p99"]),
            "pair_p999_ns": int(pair["p999"]),
            "wire_start_skew_p99_ns": int(network["wire_start_skew_ns"]["p99"]),
            "wire_complete_skew_p99_ns": int(network["wire_complete_skew_ns"]["p99"]),
            "ack_skew_p99_ns": int(network["ack_skew_ns"]["p99"]),
            "direct_p99_ns": int(row["handoff"]["direct"]["p99"]),
            "spsc_p99_ns": int(row["handoff"]["spsc_decision_core"]["p99"]),
            "ipo_sign_p99_ns": int(row["signing"]["ipo"]["p99"]),
            "ipo_sign_p999_ns": int(row["signing"]["ipo"]["p999"]),
            "pgo_sign_p99_ns": int(row["signing"]["pgo"]["p99"]),
            "pgo_sign_p999_ns": int(row["signing"]["pgo"]["p999"]),
            "ipo_promotion_candidate": bool(row["signing"]["ipo_promotion_candidate"]),
            "pgo_promotion_candidate": bool(row["signing"]["pgo_promotion_candidate"]),
            "host_tuning": [
                {
                    "name": p["profile"]["name"],
                    "supported": bool(p.get("supported")),
                    "promotion_candidate": bool(
                        (p.get("comparison") or {}).get("promotion_candidate", False)),
                    "p99_improvement_pct": (p.get("comparison") or {}).get(
                        "p99_improvement_pct"),
                    "p999_improvement_pct": (p.get("comparison") or {}).get(
                        "p999_improvement_pct"),
                }
                for p in (row.get("host_tuning") or {}).get("profiles", [])
            ],
        })
    ordered = sorted(measurements, key=lambda x: (
        x["pair_p99_ns"], x["pair_p999_ns"], x["physical_zone_id"]))
    return {
        "schema": "polymarket_v7_london_latency_shootout_v1",
        "expected_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "automatic_cutover": False,
        "authenticated_order_latency_observed": False,
        "matching_engine_latency_observed": False,
        "selection_metric": "public paired CLOB persistent-transport GET /time p99 then p999",
        "selected_physical_zone_id": ordered[0]["physical_zone_id"],
        "measurements": measurements,
        "raw": rows,
    }


def parse_result(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [line[len("V7_RESULT="):] for line in stdout.splitlines() if line.startswith("V7_RESULT=")]
    if len(rows) != 1:
        raise ValueError("exactly one V7_RESULT envelope required")
    value = json.loads(rows[0])
    if not isinstance(value, dict) or not isinstance(value.get("probe"), dict) \
            or not isinstance(value.get("manifest"), dict):
        raise ValueError("malformed V7_RESULT envelope")
    return value["probe"], value["manifest"]


def parse_launch(stdout: str) -> dict[str, str]:
    rows = [line[len("V7_LAUNCH="):] for line in stdout.splitlines() if line.startswith("V7_LAUNCH=")]
    if len(rows) != 1:
        raise ValueError("exactly one V7_LAUNCH envelope required")
    value = json.loads(rows[0])
    if not isinstance(value, dict):
        raise ValueError("malformed V7_LAUNCH envelope")
    keys = ("unit", "job_dir", "code_sha")
    if any(not isinstance(value.get(k), str) or not value[k] for k in keys):
        raise ValueError("malformed V7_LAUNCH envelope")
    return {k: value[k] for k in keys}


def send(region: str, instance_id: str, command: str, timeout_s: int) -> str:
    parameters = json.dumps({"commands": [f"bash -lc {shlex.quote(command)}"], "executionTimeout": [str(timeout_s)]})
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
        if value.get("Status") in TERMINAL:
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


def _validate_probe(zone_id: str, sha: str, stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    probe, manifest = parse_result(stdout)
    if probe.get("region") != zone_id or manifest.get("zone_id") != zone_id:
        raise RuntimeError(f"{zone_id} returned mismatched physical zone identity")
    if probe.get("exact_code_sha") != sha or manifest.get("code_sha") != sha:
        raise RuntimeError(f"{zone_id} returned mismatched exact SHA")
    return probe, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("smoke", "formal", "collect", "latency"))
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--stack-name", default="polymarket-v7-london-shootout")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--service-user", default="ubuntu")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "polymarket-london")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--latency-samples", type=int, default=1000)
    args = parser.parse_args()
    if args.region != "eu-west-2":
        raise SystemExit("eu-west-2 required")
    if not _valid_sha(args.expected_sha):
        raise SystemExit("valid --expected-sha required")
    root = Path(__file__).resolve().parents[1]
    stack = aws_json(args.region, ["cloudformation", "describe-stacks", "--stack-name", args.stack_name])
    instances = stack_instances(stack)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "latency":
        if not 100 <= args.latency_samples <= 20000:
            raise SystemExit("--latency-samples must be in [100,20000]")
        timeout_s = 3600
        submitted: dict[str, dict[str, str]] = {}
        for zone_id, instance_id in instances.items():
            command = latency_command(
                args.expected_sha, zone_id, args.service_user,
                args.latency_samples)
            submitted[zone_id] = {
                "instance_id": instance_id,
                "command_id": send(args.region, instance_id, command, timeout_s),
            }
        deadline = time.monotonic() + timeout_s + 900
        results: dict[str, dict[str, Any]] = {}
        for zone_id in sorted(submitted):
            identity = submitted[zone_id]
            invocation = wait_one(
                args.region, identity["command_id"], identity["instance_id"],
                deadline, 10.0)
            if invocation.get("Status") != "Success":
                error = str(invocation.get("StandardErrorContent") or "").strip()
                raise RuntimeError(
                    f"{zone_id} latency lab failed command={identity['command_id']}: "
                    f"{error[-3000:] or invocation.get('StatusDetails') or invocation.get('Status')}")
            value = parse_latency(str(invocation.get("StandardOutputContent") or ""))
            results[zone_id] = value
            (args.output_dir / f"latency.{zone_id}.json").write_text(
                json.dumps(value, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
        shootout = evaluate_latency(results, args.expected_sha)
        output = args.output_dir / f"latency.{args.expected_sha}.shootout.json"
        output.write_text(
            json.dumps(shootout, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        print(f"shootout={output}")
        return 0

    if args.mode == "formal":
        run_id = f"{args.expected_sha[:12]}-{time.time_ns()}"
        commands: dict[str, dict[str, str]] = {}
        launches: dict[str, dict[str, str]] = {}
        for zone_id, instance_id in instances.items():
            remote = detached_formal_command(args.expected_sha, args.service_user, f"{run_id}-{zone_id}")
            command_id = send(args.region, instance_id, remote, 120)
            commands[zone_id] = {"instance_id": instance_id, "command_id": command_id}
        for zone_id in sorted(commands):
            meta = commands[zone_id]
            value = wait_one(args.region, meta["command_id"], meta["instance_id"], time.monotonic() + 300, 2.0)
            if value.get("Status") != "Success":
                raise RuntimeError(f"{zone_id} detached formal launch failed: {value.get('StatusDetails') or value.get('Status')}")
            launch = parse_launch(str(value.get("StandardOutputContent") or ""))
            if launch["code_sha"] != args.expected_sha:
                raise RuntimeError(f"{zone_id} detached formal launch SHA mismatch")
            launches[zone_id] = launch
        receipt = {
            "schema": "polymarket_v7_london_detached_formal_v1",
            "mode": "formal",
            "expected_sha": args.expected_sha,
            "region": args.region,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "automatic_cutover": False,
            "commands": commands,
            "launches": launches,
        }
        receipt_path = args.output_dir / f"formal.{run_id}.detached.json"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(f"receipt={receipt_path}")
        return 0

    if args.mode == "collect":
        if args.receipt is None:
            raise SystemExit("--receipt is required for collect")
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        if receipt.get("schema") != "polymarket_v7_london_detached_formal_v1" \
                or receipt.get("expected_sha") != args.expected_sha:
            raise SystemExit("invalid detached formal receipt")
        launches = receipt.get("launches")
        if not isinstance(launches, dict) or set(launches) != set(instances):
            raise SystemExit("detached formal receipt must cover exactly three zones")
        probe_paths: list[Path] = []
        pending: list[str] = []
        for zone_id in sorted(instances):
            instance_id = instances[zone_id]
            launch = launches[zone_id]
            command_id = send(args.region, instance_id,
                              collect_formal_command(args.expected_sha, args.service_user, launch), 120)
            value = wait_one(args.region, command_id, instance_id, time.monotonic() + 300, 2.0)
            stdout = str(value.get("StandardOutputContent") or "")
            if "V7_PENDING=" in stdout:
                pending.append(zone_id)
                continue
            if value.get("Status") != "Success":
                raise RuntimeError(f"{zone_id} detached formal collect failed: {value.get('StatusDetails') or value.get('Status')}")
            probe, manifest = _validate_probe(zone_id, args.expected_sha, stdout)
            probe_path = args.output_dir / f"formal.{zone_id}.probe.json"
            manifest_path = args.output_dir / f"formal.{zone_id}.host.json"
            probe_path.write_text(json.dumps(probe, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            probe_paths.append(probe_path)
        if pending:
            print("pending=" + ",".join(pending))
            return 75
        shootout = args.output_dir / f"formal.{args.expected_sha}.shootout.json"
        evaluate_formal(root, probe_paths, shootout)
        print(f"shootout={shootout}")
        return 0

    timeout_s = 1800
    command = remote_command(args.expected_sha, "smoke", args.service_user)
    commands: dict[str, tuple[str, str]] = {}
    for zone_id, instance_id in instances.items():
        commands[zone_id] = (instance_id, send(args.region, instance_id, command, timeout_s))
    deadline = time.monotonic() + timeout_s + 600
    receipt = {
        "schema": "polymarket_v7_london_ssm_benchmark_v1",
        "mode": "smoke",
        "expected_sha": args.expected_sha,
        "region": args.region,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "automatic_cutover": False,
        "commands": {z: {"instance_id": i, "command_id": c} for z, (i, c) in commands.items()},
    }
    for zone_id in sorted(commands):
        instance_id, command_id = commands[zone_id]
        value = wait_one(args.region, command_id, instance_id, deadline, 5.0)
        if value.get("Status") != "Success":
            raise RuntimeError(f"{zone_id} SSM benchmark failed: {value.get('StatusDetails') or value.get('Status')}")
        probe, manifest = _validate_probe(zone_id, args.expected_sha, str(value.get("StandardOutputContent") or ""))
        (args.output_dir / f"smoke.{zone_id}.probe.json").write_text(
            json.dumps(probe, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (args.output_dir / f"smoke.{zone_id}.host.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    receipt_path = args.output_dir / f"smoke.{args.expected_sha}.ssm.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"receipt={receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
