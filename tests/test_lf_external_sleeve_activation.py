#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_external_sleeve_activation_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_external_sleeve_activation_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LFExternalSleeveActivationTest(unittest.TestCase):
    def test_current_v5_external_sleeve_has_static_activation_gap(self) -> None:
        paper = json.loads((ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"))
        external = json.loads((ROOT / "config" / "external_intelligence.json").read_text(encoding="utf-8"))
        signals_path = ROOT / str(paper["external_signals_file"])

        report = MODULE.assess(
            paper,
            external,
            repository_signal_rows=MODULE.signal_rows(signals_path),
        )

        self.assertTrue(report["external_strategy_enabled"])
        self.assertAlmostEqual(report["external_capital_fraction"], 0.10)
        self.assertAlmostEqual(report["external_starting_capital_usd"], 1000.0)
        self.assertEqual(report["repository_signal_rows"], 0)
        self.assertFalse(report["external_research_allow_production_signal_write"])
        self.assertTrue(report["activation_gap"])

    def test_gap_closes_only_when_a_signal_handoff_exists_or_sleeve_is_inactive(self) -> None:
        paper = {
            "starting_capital": 10000.0,
            "external_signals_file": "data/external_signals.csv",
            "multi_strategy": {
                "strategies": [
                    {"name": "external", "expert": "external", "capital_fraction": 0.10, "enabled": True}
                ]
            },
        }
        research_only = {"allow_production_signal_write": False}

        self.assertTrue(MODULE.assess(paper, research_only, repository_signal_rows=0)["activation_gap"])
        self.assertFalse(MODULE.assess(paper, research_only, repository_signal_rows=1)["activation_gap"])

        inactive = json.loads(json.dumps(paper))
        inactive["multi_strategy"]["strategies"][0]["enabled"] = False
        self.assertFalse(MODULE.assess(inactive, research_only, repository_signal_rows=0)["activation_gap"])

    def test_diagnostic_does_not_propose_bypassing_research_governance(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("live_champion.json", source)
        self.assertNotIn("git push", source)
        self.assertNotIn("authenticated", source.lower())
        self.assertNotIn("order submission", source.lower())


if __name__ == "__main__":
    unittest.main()
