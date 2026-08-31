#!/usr/bin/env python3
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODES = {
    "RESEARCH_ZERO_AUTHORITY", "PAPER_SIMULATED", "SHADOW_LIVE_READ_ONLY", "MICRO_LIVE",
    "LIVE_RESTRICTED", "LIVE_SCALED", "DRAIN_ONLY", "CANCEL_ONLY", "KILLED",
}


class ExecutionModeContractTests(unittest.TestCase):
    def test_checked_in_execution_modes_are_explicit_and_zero_cap(self) -> None:
        value = json.loads((ROOT / "config/v7_execution_modes.json").read_text(encoding="utf-8"))
        self.assertEqual(value["schema"], "polymarket_v7_execution_modes_v1")
        self.assertEqual(value["default_execution_mode"], "PAPER_SIMULATED")
        self.assertEqual(set(value["modes"]), MODES)
        self.assertTrue(all(cap == 0 for cap in value["checked_in_live_caps"].values()))
        self.assertFalse(value["modes"]["PAPER_SIMULATED"]["authenticated_read"])
        self.assertFalse(value["modes"]["PAPER_SIMULATED"]["submit_orders"])

    def test_canonical_configs_agree_on_paper_simulated(self) -> None:
        paper = json.loads((ROOT / "config/paper_v7.json").read_text(encoding="utf-8"))
        supervision = json.loads((ROOT / "config/v7_runtime_supervision.json").read_text(encoding="utf-8"))
        champion = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertEqual(paper["execution_mode"], "PAPER_SIMULATED")
        self.assertEqual(paper["v7"]["execution_mode"], "PAPER_SIMULATED")
        self.assertEqual(paper["v7"]["execution_modes_policy"], "config/v7_execution_modes.json")
        self.assertEqual(supervision["execution_mode"], "PAPER_SIMULATED")
        self.assertEqual(champion["execution_mode"], "PAPER_SIMULATED")


if __name__ == "__main__":
    unittest.main()
