from __future__ import annotations

import json
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))

from v7_london_ssm_benchmark import (
    collect_formal_command,
    detached_formal_command,
    parse_launch,
    parse_result,
    remote_command,
    send,
    stack_instances,
)


class LondonSsmBenchmarkTests(unittest.TestCase):
    def test_stack_requires_three_distinct_hosts(self) -> None:
        value = {"Stacks": [{"Outputs": [
            {"OutputKey": "InstanceAz1", "OutputValue": "i-111"},
            {"OutputKey": "InstanceAz2", "OutputValue": "i-222"},
            {"OutputKey": "InstanceAz3", "OutputValue": "i-333"},
        ]}]}
        self.assertEqual(stack_instances(value), {
            "euw2-az1": "i-111", "euw2-az2": "i-222", "euw2-az3": "i-333",
        })
        value["Stacks"][0]["Outputs"][2]["OutputValue"] = "i-222"
        with self.assertRaisesRegex(ValueError, "distinct"):
            stack_instances(value)

    def test_remote_command_is_exact_sha_and_runtime_disabled(self) -> None:
        sha = "a" * 40
        command = remote_command(sha, "smoke", "ubuntu")
        self.assertIn(sha, command)
        self.assertIn("! systemctl is-active --quiet polymarket-v7-paper.service", command)
        self.assertIn("v7_london_benchmark.sh", command)
        self.assertIn("sudo -u ubuntu git -C", command)
        self.assertNotIn("real-order", command.lower())
        self.assertNotIn("tailscale up", command)
        with self.assertRaises(ValueError):
            remote_command("bad", "smoke", "ubuntu")

    def test_detached_formal_is_exact_sha_locked_and_non_trading(self) -> None:
        sha = "b" * 40
        command = detached_formal_command(sha, "ubuntu", "run-123-euw2-az1")
        self.assertIn("systemd-run", command)
        self.assertIn("--property=Type=simple", command)
        self.assertNotIn("--property=Type=oneshot", command)
        self.assertIn("formal.lock", command)
        self.assertIn("flock -n", command)
        self.assertIn("v7_london_benchmark.sh\" formal", command)
        self.assertIn("! systemctl is-active --quiet polymarket-v7-paper.service", command)
        self.assertIn(sha, command)
        self.assertNotIn("systemctl enable --now polymarket-v7-paper", command)
        self.assertNotIn("real-order", command.lower())
        with self.assertRaises(ValueError):
            detached_formal_command(sha, "ubuntu", "bad/run")

    def test_collect_requires_matching_launch_receipt(self) -> None:
        sha = "c" * 40
        launch = {
            "unit": "polymarket-v7-formal-run-123-euw2-az1",
            "job_dir": "/mnt/polymarket-data/benchmarks/formal-jobs/run-123-euw2-az1",
            "code_sha": sha,
        }
        command = collect_formal_command(sha, "ubuntu", launch)
        self.assertIn("V7_PENDING=", command)
        self.assertIn("launch.json", command)
        self.assertIn("V7_RESULT=", command)
        with self.assertRaises(ValueError):
            collect_formal_command("d" * 40, "ubuntu", launch)
        with self.assertRaises(ValueError):
            collect_formal_command(sha, "ubuntu", {**launch, "job_dir": "/tmp/no"})

    def test_parse_launch_requires_one_marked_envelope(self) -> None:
        launch = {
            "unit": "polymarket-v7-formal-run-123-euw2-az1",
            "job_dir": "/mnt/polymarket-data/benchmarks/formal-jobs/run-123-euw2-az1",
            "code_sha": "a" * 40,
        }
        payload = json.dumps(launch, separators=(",", ":"))
        self.assertEqual(parse_launch("V7_LAUNCH=" + payload + "\n"), launch)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_launch("no envelope")

    def test_send_forces_bash_for_pipefail_remote_command(self) -> None:
        with mock.patch("v7_london_ssm_benchmark.aws_json", return_value={
            "Command": {"CommandId": "cmd-123"}
        }) as aws:
            self.assertEqual(send("eu-west-2", "i-1234567890abcdef0", "set -euo pipefail\ntrue", 60), "cmd-123")
        args = aws.call_args.args[1]
        raw = args[args.index("--parameters") + 1]
        params = json.loads(raw)
        self.assertEqual(params["executionTimeout"], ["60"])
        self.assertTrue(params["commands"][0].startswith("bash -lc "))

    def test_parse_result_requires_one_marked_envelope(self) -> None:
        probe = {"region": "euw2-az1", "exact_code_sha": "a" * 40}
        manifest = {"zone_id": "euw2-az1", "code_sha": "a" * 40}
        payload = json.dumps({"probe": probe, "manifest": manifest}, separators=(",", ":"))
        p, m = parse_result("diagnostic\nV7_RESULT=" + payload + "\n")
        self.assertEqual(p, probe)
        self.assertEqual(m, manifest)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_result("no envelope")

    def test_source_keeps_no_cutover_authority(self) -> None:
        source = (ROOT / "ops/v7_london_ssm_benchmark.py").read_text(encoding="utf-8")
        self.assertIn('"automatic_cutover": False', source)
        self.assertIn('"real_order_submission": False', source)
        self.assertNotIn("cloudformation delete-stack", source)
        self.assertNotIn("systemctl enable --now polymarket-v7-paper", source)


if __name__ == "__main__":
    unittest.main()
