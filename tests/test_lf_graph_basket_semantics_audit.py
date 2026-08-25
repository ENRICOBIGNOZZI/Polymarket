#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_graph_basket_semantics_audit", ROOT / "scripts" / "lf_graph_basket_semantics_audit.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class GraphBasketSemanticsAuditTest(unittest.TestCase):
    def test_current_graph_child_is_enabled_and_single_expert(self) -> None:
        contract = MODULE.source_contract(ROOT)
        self.assertTrue(contract["graph_child_enabled"])
        self.assertAlmostEqual(float(contract["graph_capital_fraction"]), 0.25)
        self.assertAlmostEqual(float(contract["graph_max_sum_error"]), 0.35)
        self.assertTrue(contract["projection_present"])
        self.assertTrue(contract["single_market_candidate_path_present"])
        self.assertTrue(contract["single_market_paper_trade_present"])
        self.assertTrue(contract["singleton_child_weighting_present"])

    def test_projection_can_create_false_single_leg_directional_edge(self) -> None:
        fixture = MODULE.deterministic_fixture()
        projected = fixture["projected_probabilities"]
        self.assertEqual(len(projected), 3)
        for q in projected:
            self.assertAlmostEqual(q, 1.0 / 3.0, places=12)
        self.assertAlmostEqual(float(fixture["sum_error"]), 0.34, places=12)
        self.assertTrue(fixture["passes_current_graph_sum_gate"])
        self.assertGreater(float(fixture["graph_single_leg_model_edge"]), 0.10)
        self.assertLess(float(fixture["true_single_leg_ev"]), -0.12)
        self.assertGreater(float(fixture["all_yes_basket_gross_profit"]), 0.30)

    def test_true_probabilities_satisfy_the_same_structural_constraint(self) -> None:
        fixture = MODULE.deterministic_fixture()
        probs = fixture["true_probabilities"]
        self.assertTrue(math.isclose(sum(probs), 1.0, abs_tol=1e-12))
        self.assertGreater(float(fixture["graph_single_leg_model_edge"]), 0.0)
        self.assertLess(float(fixture["true_single_leg_ev"]), 0.0)

    def test_full_audit_marks_structural_defect_without_promotion(self) -> None:
        result = MODULE.audit(ROOT)
        self.assertEqual(result["status"], "STRUCTURAL_DEFECT")
        self.assertEqual(result["research_decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertIn("complete executable baskets", result["candidate_design"]["execution"])


if __name__ == "__main__":
    unittest.main()
