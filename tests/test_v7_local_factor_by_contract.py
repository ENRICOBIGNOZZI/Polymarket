from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


class LocalFactorBYContractTest(unittest.TestCase):
    def test_config_freezes_dependence_robust_full_family_contract(self) -> None:
        cfg = json.loads((ROOT / "config/research_v7_local_factor.json").read_text())
        self.assertEqual(cfg["schema_version"], 2)
        self.assertTrue(cfg["paper_only"])
        self.assertTrue(cfg["research_only"])
        self.assertFalse(cfg["live_intents_enabled"])
        inf = cfg["inference"]
        self.assertEqual(inf["multiplicity_method"], "benjamini_yekutieli_arbitrary_dependence")
        self.assertEqual(inf["predeclared_unestimable_pair_pvalue"], 1.0)
        self.assertTrue(inf["apply_multiplicity_before_residual_z_phi_and_economic_filters"])
        self.assertGreaterEqual(inf["maximum_bootstrap_repetitions"], 12000)
        self.assertGreaterEqual(inf["maximum_pair_controls"], inf["minimum_pair_controls"])
        self.assertLessEqual(inf["maximum_pair_controls"], 6)
        evidence = cfg["execution_evidence"]
        self.assertFalse(evidence["marginal_fill_product_is_joint_estimator"])
        self.assertFalse(evidence["minimum_marginal_fill_is_joint_estimator"])
        self.assertTrue(evidence["require_joint_fill_state_bitmasks"])
        self.assertTrue(evidence["require_partial_state_abort_unwind_pnl"])

    def test_base_keeps_unestimable_declared_pairs_at_p_one_and_uses_by(self) -> None:
        base = source("v7_local_factor_research_base.py")
        self.assertIn("{key: 1.0 for key in predeclared_keys}", base)
        self.assertIn("multiplicity.by_selected(pvalues, fdr_q)", base)
        self.assertIn("multiplicity.by_resolution_diagnostics(predeclared_pair_count, reps, fdr_q)", base)
        self.assertNotIn("core.bh_selected(pvalues", base)
        self.assertIn('"multiplicity_method": "benjamini_yekutieli_arbitrary_dependence"', base)
        self.assertIn('"survivorship_safe": False', base)
        self.assertIn('"execution_joint_state_validated": False', base)
        self.assertIn('"fill_conditioned_pnl_validated": False', base)

    def test_pair_graph_and_controls_are_frozen_before_price_history_fetch(self) -> None:
        base = source("v7_local_factor_research_base.py")
        graph_pos = base.index("pair_graphs: dict")
        controls_pos = base.index("pair_control_plans: dict")
        history_pos = base.index("histories, history_failures = fetch_histories_chunked")
        self.assertLess(graph_pos, controls_pos)
        self.assertLess(controls_pos, history_pos)

    def test_base_builds_pair_specific_panel_not_cluster_wide_complete_case(self) -> None:
        base = source("v7_local_factor_research_base.py")
        self.assertIn("pair_market_ids = [market_a, market_b, *controls]", base)
        self.assertIn("core.build_regular_panel(completed_histories, pair_market_ids", base)
        self.assertIn("pair_controls_frozen_before_price_history", base)
        self.assertNotIn("market_ids = [market.market_id for market in group]", base)
        self.assertNotIn("histories,\n            market_ids,", base)

    def test_current_wrapper_adds_causal_books_and_current_residual_state(self) -> None:
        wrapper = source("v7_local_factor_research.py")
        self.assertIn("v7_model_book_snapshot", wrapper)
        self.assertIn("validate_coherent_books", wrapper)
        self.assertIn("required_markets = (fit.market_a, fit.market_b, *fit.controls)", wrapper)
        self.assertIn("current_residual_reconstructed_from_frozen_controls", wrapper)
        self.assertIn("config/research_v7_market_data.json", wrapper)
        self.assertIn("operational_paper_config_introduced", wrapper)
        self.assertNotIn("config/paper_v7.json", wrapper)


if __name__ == "__main__":
    unittest.main()
