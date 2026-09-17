from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))

from v7_london_ssm_benchmark import parse_result, remote_command, stack_instances


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
        self.assertNotIn("real-order", command.lower())
        self.assertNotIn("tailscale up", command)
        with self.assertRaises(ValueError):
            remote_command("bad", "smoke", "ubuntu")
        self.assertTrue(command.startswith("bash -lc "))
        self.assertIn("set -euo pipefail", command)

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
