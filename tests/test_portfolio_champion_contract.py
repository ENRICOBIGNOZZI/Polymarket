from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PortfolioChampionContractTest(unittest.TestCase):
    def test_manifest_registers_one_supervisor_and_two_bounded_engines(self) -> None:
        manifest = json.loads(
            (ROOT / "config" / "portfolio_champion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["mode"], "paper_only")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertEqual(manifest["promotion_policy"], "approved integration PR only")
        self.assertEqual(set(manifest["engines"]), {"alpha", "cross_venue"})
        self.assertEqual(
            manifest["engines"]["alpha"]["champion_manifest"],
            "config/live_champion.json",
        )
        self.assertEqual(
            manifest["engines"]["cross_venue"]["venues"],
            ["polymarket", "limitless", "kalshi"],
        )
        invariants = manifest["invariants"]
        self.assertTrue(invariants["one_portfolio_champion"])
        self.assertTrue(invariants["independent_failure_domains"])
        self.assertTrue(invariants["global_new_exposure_fail_closed"])
        self.assertFalse(invariants["authenticated_order_submission"])
        self.assertEqual(invariants["maximum_drawdown"], 0.15)

    def test_both_engine_planes_consume_the_same_supervisor_gate(self) -> None:
        alpha_loop = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        cross_config = json.loads(
            (ROOT / "config" / "cross_venue.json").read_text(encoding="utf-8")
        )
        supervisor = json.loads(
            (ROOT / "config" / "portfolio_supervisor.json").read_text(encoding="utf-8")
        )
        limits = supervisor["limits_file"]
        self.assertIn("scripts/apply_portfolio_gate.py", alpha_loop)
        self.assertIn("--engine alpha", alpha_loop)
        self.assertEqual(cross_config["policy"]["supervisor_limits"], limits)
        self.assertTrue(cross_config["policy"]["require_supervisor_gate"])

    def test_governance_defines_portfolio_level_single_champion(self) -> None:
        policy = (ROOT / "docs" / "SYSTEM_WATCH.md").read_text(encoding="utf-8")
        for phrase in (
            "one live champion",
            "one authoritative portfolio supervisor",
            "Independent engine admission contract",
            "config/portfolio_champion.json",
            "global capital, drawdown and new-exposure state",
            "authenticated order submission",
        ):
            self.assertIn(phrase, policy)


if __name__ == "__main__":
    unittest.main()
