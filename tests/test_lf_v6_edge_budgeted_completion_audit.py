#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v6_edge_budgeted_completion_audit.py"
spec = importlib.util.spec_from_file_location("lf_v6_edge_budgeted_completion_audit", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class EdgeBudgetedCompletionAuditTest(unittest.TestCase):
    def test_current_broker_uses_fixed_operational_completion(self) -> None:
        contract = mod.source_contract(ROOT)
        self.assertTrue(contract["completion_is_minimum_leg_fraction"])
        self.assertTrue(contract["fixed_threshold_marks_bundle_complete"])
        self.assertTrue(contract["residual_orders_cancelled_after_threshold"])
        self.assertTrue(contract["absolute_unmatched_entry_risk_gate_present"])
        self.assertTrue(contract["unmatched_risk_uses_common_completion"])
        self.assertTrue(contract["v6_completion_threshold_is_075"])
        self.assertTrue(contract["v6_max_leg_risk_is_12"])
        self.assertFalse(contract["economic_completion_recheck_present"])

    def test_current_graph_fixture_can_be_complete_but_have_negative_terminal_states(self) -> None:
        fixture = mod.BasketFixture(
            prices=(0.74, 0.15, 0.09),
            max_notional=60.0,
            completion_threshold=0.75,
            max_leg_risk_usd=12.0,
            expected_edge=0.02,
        )
        state = mod.evaluate_threshold_state(fixture, excess_leg=0)
        self.assertTrue(state["passes_fixed_completion_gate"])
        self.assertTrue(state["passes_absolute_unmatched_risk_gate"])
        self.assertAlmostEqual(state["common_completion"], 0.75)
        self.assertGreater(state["full_bundle_guaranteed_profit"], 0.0)
        self.assertLess(state["worst_terminal_pnl"], 0.0)
        self.assertGreater(state["worst_loss_to_full_profit_ratio"], 8.0)

    def test_absolute_unmatched_risk_can_dwarf_the_structural_edge(self) -> None:
        fixture = mod.BasketFixture(
            prices=(0.74, 0.15, 0.09),
            max_notional=60.0,
            completion_threshold=0.75,
            max_leg_risk_usd=12.0,
            expected_edge=0.02,
        )
        state = mod.evaluate_threshold_state(fixture, excess_leg=0)
        self.assertLess(state["unmatched_entry_risk"], fixture.max_leg_risk_usd)
        self.assertGreater(
            state["unmatched_entry_risk"],
            state["advertised_edge_dollars_at_max_notional"] * 9.0,
        )

    def test_full_fill_preserves_structural_payoff_floor(self) -> None:
        fixture = mod.BasketFixture(
            prices=(0.74, 0.15, 0.09),
            max_notional=60.0,
            completion_threshold=1.0,
            max_leg_risk_usd=12.0,
            expected_edge=0.02,
        )
        state = mod.evaluate_threshold_state(fixture, excess_leg=0)
        for pnl in state["terminal_pnl_by_winning_leg"]:
            self.assertAlmostEqual(pnl, state["full_bundle_guaranteed_profit"], places=10)
        self.assertAlmostEqual(state["unmatched_entry_risk"], 0.0, places=12)


if __name__ == "__main__":
    unittest.main()