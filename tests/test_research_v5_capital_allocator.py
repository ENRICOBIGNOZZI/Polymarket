#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "research_v5_capital_allocator", ROOT / "scripts" / "research_v5_capital_allocator.py"
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class V5CapitalAllocatorResearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = {
            "micro": 0.10,
            "pca": 0.20,
            "graph": 0.30,
            "semantic": 0.10,
            "external": 0.20,
        }
        self.policy = mod.AllocationPolicy()

    @staticmethod
    def good_row(net: float = 0.01) -> dict[str, object]:
        return {
            "net_return": net,
            "stress_1_5_return": net * 0.8,
            "stress_2_0_return": net * 0.6,
            "trades": 40,
            "active_folds": 3,
            "profit_factor": 1.3,
            "bootstrap_pvalue": 0.02,
            "positive_fold_fraction": 0.75,
            "max_drawdown": 0.02,
            "eligible_for_tiny_pilot": True,
            "production_threshold_present": True,
        }

    def test_no_evidence_fails_closed_to_exploration_and_reserve(self) -> None:
        proposal = mod.propose_allocation(self.baseline, {}, self.policy)
        self.assertEqual(proposal["status"], "EXPLORATION_ONLY")
        for fraction in proposal["strategy_fractions"].values():
            self.assertAlmostEqual(fraction, 0.02)
        self.assertAlmostEqual(proposal["reserve_fraction"], 0.90)

    def test_positive_strategy_gets_incremental_capital_but_is_capped(self) -> None:
        rows = {"pca": self.good_row()}
        proposal = mod.propose_allocation(self.baseline, rows, self.policy)
        self.assertEqual(proposal["status"], "EVIDENCE_WEIGHTED")
        self.assertAlmostEqual(proposal["strategy_fractions"]["pca"], 0.30)
        self.assertTrue(proposal["details"]["pca"]["gate_pass"])
        self.assertGreater(proposal["reserve_fraction"], 0.50)
        for name in ("micro", "graph", "semantic", "external"):
            self.assertAlmostEqual(proposal["strategy_fractions"][name], 0.02)

    def test_cost_stress_failure_cannot_receive_incremental_capital(self) -> None:
        bad = self.good_row()
        bad["stress_2_0_return"] = -0.001
        proposal = mod.propose_allocation(self.baseline, {"graph": bad}, self.policy)
        self.assertFalse(proposal["details"]["graph"]["gate_pass"])
        self.assertIn("nonpositive_2_0x_cost_return", proposal["details"]["graph"]["reasons"])
        self.assertAlmostEqual(proposal["strategy_fractions"]["graph"], 0.02)

    def test_existing_oos_gate_cannot_be_bypassed(self) -> None:
        bad = self.good_row()
        bad["eligible_for_tiny_pilot"] = False
        proposal = mod.propose_allocation(self.baseline, {"micro": bad}, self.policy)
        self.assertFalse(proposal["details"]["micro"]["gate_pass"])
        self.assertIn("existing_oos_gate_not_passed", proposal["details"]["micro"]["reasons"])
        self.assertAlmostEqual(proposal["strategy_fractions"]["micro"], 0.02)

    def test_chronological_ablation_uses_only_prior_windows(self) -> None:
        empty = {
            "net_return": 0.0,
            "stress_1_5_return": 0.0,
            "stress_2_0_return": 0.0,
            "trades": 0,
            "active_folds": 0,
            "profit_factor": 0.0,
            "bootstrap_pvalue": 1.0,
            "positive_fold_fraction": 0.0,
            "max_drawdown": 0.0,
            "eligible_for_tiny_pilot": False,
            "production_threshold_present": False,
        }
        windows = [
            {"timestamp": 1, "strategies": {name: dict(empty) for name in self.baseline}},
            {"timestamp": 2, "strategies": {name: dict(empty) for name in self.baseline}},
            {"timestamp": 3, "strategies": {name: dict(empty) for name in self.baseline}},
        ]
        windows[2]["strategies"]["graph"] = self.good_row(0.50)
        result = mod.chronological_ablation(self.baseline, windows, self.policy, min_train_windows=2)
        self.assertEqual(result["test_folds"], 1)
        allocation = result["folds"][0]["allocation"]
        self.assertEqual(allocation["status"], "EXPLORATION_ONLY")
        self.assertAlmostEqual(allocation["strategy_fractions"]["graph"], 0.02)

    def test_v5_baseline_contract(self) -> None:
        config = {
            "multi_strategy": {
                "paper_only": True,
                "reserve_fraction": 0.10,
                "strategies": [
                    {"name": name, "capital_fraction": fraction, "enabled": True}
                    for name, fraction in self.baseline.items()
                ],
            }
        }
        baseline, reserve = mod.load_v5_baseline(config)
        self.assertEqual(baseline, self.baseline)
        self.assertAlmostEqual(sum(baseline.values()) + reserve, 1.0)


if __name__ == "__main__":
    unittest.main()
