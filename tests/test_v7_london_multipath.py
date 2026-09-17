from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops"
sys.path.insert(0, str(OPS))

import v7_london_multipath_ssm as multipath


def source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class LondonMultipathTests(unittest.TestCase):
    def test_remote_command_is_paper_only_and_runtime_disabled(self) -> None:
        sha = "a" * 40
        command = multipath.remote_command(sha, "euw2-az1", "ubuntu", 30, 5, 5)
        self.assertIn(sha, command)
        self.assertIn("! systemctl is-active --quiet polymarket-v7-paper.service", command)
        self.assertIn("v7_public_ws_latency_probe.py", command)
        self.assertIn("v7_external_tls_probe.py", command)
        self.assertIn("v7_external_ws_age_probe.py", command)
        self.assertNotIn("systemctl enable", command)
        self.assertNotIn("real-order", command.lower())
        with self.assertRaises(ValueError):
            multipath.remote_command("bad", "euw2-az1", "ubuntu", 30, 5, 5)
        with self.assertRaises(ValueError):
            multipath.remote_command(sha, "eu-west-2a", "ubuntu", 30, 5, 5)

    def test_parse_and_validate_require_same_zone_and_sha(self) -> None:
        sha = "b" * 40
        base = {
            "region": "euw2-az2", "exact_code_sha": sha,
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "authorizes_live_execution": False,
        }
        payload = {name: dict(base) for name in ("polymarket_ws", "external_tls", "external_ws")}
        encoded = json.dumps(payload, separators=(",", ":"))
        parsed = multipath.parse_result("noise\nV7_MULTIPATH=" + encoded + "\n")
        multipath.validate_result(parsed, "euw2-az2", sha)
        parsed["external_tls"]["region"] = "euw2-az1"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            multipath.validate_result(parsed, "euw2-az2", sha)

    def test_receipt_never_selects_zone_or_authorizes_execution(self) -> None:
        text = source("ops/v7_london_multipath_ssm.py")
        self.assertIn('"selection_ready": False', text)
        self.assertIn('"selected_zone": None', text)
        self.assertIn('"authenticated_order_latency_observed": False', text)
        self.assertIn('"one_way_latency_proven": False', text)
        self.assertIn('"authorizes_live_execution": False', text)

    def test_all_probe_sources_are_exact_sha_and_zero_authority(self) -> None:
        for relative in (
            "scripts/v7_public_ws_latency_probe.py",
            "scripts/v7_external_tls_probe.py",
            "scripts/v7_external_ws_age_probe.py",
        ):
            text = source(relative)
            with self.subTest(relative=relative):
                self.assertIn("expected-sha", text)
                self.assertIn("exact_code_sha", text)
                self.assertIn("paper_only", text)
                self.assertIn("authenticated_execution", text)
                self.assertIn("real_order_submission", text)
                self.assertNotIn("PRIVATE_KEY", text)
                self.assertNotIn("Authorization:", text)

    def test_external_feed_age_is_explicitly_not_one_way_proof(self) -> None:
        text = source("scripts/v7_external_ws_age_probe.py")
        self.assertIn("not one-way latency proof", text)


if __name__ == "__main__":
    unittest.main()
