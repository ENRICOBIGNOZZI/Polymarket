from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        evidence = cfg["execution_evidence"]
        self.assertFalse(evidence["marginal_fill_product_is_joint_estimator"])
        self.assertFalse(evidence["minimum_marginal_fill_is_joint_estimator"])
        self.assertTrue(evidence["require_joint_fill_state_bitmasks"])
        self.assertTrue(evidence["require_partial_state_abort_unwind_pnl"])

    def test_runner_keeps_unestimable_declared_pairs_at_p_one_and_uses_by(self) -> None:
        source = (ROOT / "scripts/v7_local_factor_research.py").read_text()
        self.assertIn("{key: 1.0 for key in predeclared_keys}", source)
        self.assertIn("multiplicity.by_selected(pvalues, fdr_q)", source)
        self.assertIn("multiplicity.by_resolution_diagnostics(predeclared_pair_count, reps, fdr_q)", source)
        self.assertNotIn("core.bh_selected(pvalues", source)
        self.assertIn('"multiplicity_method": "benjamini_yekutieli_arbitrary_dependence"', source)
        self.assertIn('"survivorship_safe": False', source)
        self.assertIn('"execution_joint_state_validated": False', source)
        self.assertIn('"fill_conditioned_pnl_validated": False', source)

    def test_pair_graph_is_frozen_before_price_history_fetch(self) -> None:
        source = (ROOT / "scripts/v7_local_factor_research.py").read_text()
        graph_pos = source.index("pair_graphs: dict")
        history_pos = source.index("histories, history_failures = fetch_histories_chunked")
        self.assertLess(graph_pos, history_pos)


if __name__ == "__main__":
    unittest.main()
