from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_v6_resolution_atomicity_audit",
    ROOT / "scripts" / "lf_v6_resolution_atomicity_audit.py",
)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class ResolutionAtomicityAuditTest(unittest.TestCase):
    def test_current_source_has_partial_close_abort_contract(self) -> None:
        contract = MOD.source_contract(ROOT)
        self.assertTrue(contract["closed_legs_are_settled_individually"])
        self.assertTrue(contract["any_closed_leg_aborts_resting_or_complete_bundle"])
        self.assertTrue(contract["abort_path_sells_still_tradable_filled_legs"])
        self.assertTrue(contract["graph_hold_deadline_can_extend_past_market_end"])
        self.assertFalse(contract["bundle_level_settling_state_present"])

    def test_current_graph_fixture_is_positive_if_held_to_terminal_settlement(self) -> None:
        basket = MOD.ExhaustiveBasket(prices=(0.74, 0.15, 0.09), max_notional=60.0)
        state = MOD.loser_first_transition(basket, 2)
        self.assertAlmostEqual(state["target_units"], 60.0 / 0.98, places=10)
        self.assertAlmostEqual(state["guaranteed_terminal_pnl"], 60.0 / 0.98 * 0.02, places=10)
        self.assertGreater(state["guaranteed_terminal_pnl"], 0.0)

    def test_any_loser_first_abort_can_flip_the_structural_edge_negative(self) -> None:
        basket = MOD.ExhaustiveBasket(prices=(0.74, 0.15, 0.09), max_notional=60.0)
        states = [MOD.loser_first_transition(basket, i) for i in range(3)]
        self.assertTrue(all(x["turns_positive_structural_edge_negative"] for x in states))
        pnls = [float(x["transition_pnl_before_exit_fees_slippage"]) for x in states]
        self.assertAlmostEqual(max(pnls), -60.0 * 0.09 / 0.98, places=10)
        self.assertAlmostEqual(min(pnls), -60.0 * 0.74 / 0.98, places=10)
        self.assertTrue(all(math.isfinite(x) for x in pnls))

    def test_exit_costs_can_only_worsen_unchanged_bid_counterexample(self) -> None:
        basket = MOD.ExhaustiveBasket(prices=(0.74, 0.15, 0.09), max_notional=60.0)
        baseline = MOD.loser_first_transition(basket, 2)
        stressed = MOD.loser_first_transition(basket, 2, remaining_unwind_prices=(0.739, 0.149))
        self.assertLess(
            stressed["transition_pnl_before_exit_fees_slippage"],
            baseline["transition_pnl_before_exit_fees_slippage"],
        )

    def test_report_requires_terminal_settlement_barrier(self) -> None:
        report = MOD.run_audit(ROOT)
        required = " ".join(report["required_successor_contract"]).lower()
        self.assertIn("settling", required)
        self.assertIn("event-level terminal barrier", required)
        self.assertIn("residual unmatched exposure", required)


if __name__ == "__main__":
    unittest.main()
