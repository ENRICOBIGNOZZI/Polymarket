#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v5_pca_state_bias_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v5_pca_state_bias_audit", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class V5PcaStateBiasAuditTest(unittest.TestCase):
    def test_source_contract_matches_current_v5_pca(self) -> None:
        contract = mod.source_contract(ROOT)
        self.assertTrue(contract["engine_contract_present"])
        self.assertEqual(contract["pca_capital_fraction"], 0.25)
        self.assertEqual(contract["pca_min_history"], 24)
        self.assertEqual(contract["pca_window"], 720)
        self.assertEqual(contract["pca_universe"], 350)
        self.assertEqual(contract["pca_min_residual_z"], 0.60)

    def test_iid_null_fixture_has_material_false_admission(self) -> None:
        expected = {
            23: (125, 0.3125),
            48: (123, 0.3075),
            96: (86, 0.2150),
            720: (68, 0.1700),
        }
        for T, (count, rate) in expected.items():
            with self.subTest(T=T):
                result = mod.iid_null_experiment(T, paths=400)
                self.assertEqual(result["admitted"], count)
                self.assertAlmostEqual(result["admission_rate"], rate, places=12)
                self.assertLess(result["mean_beta"], 0.0)
                self.assertGreater(result["independence_scale_expected_admissions_at_350_markets"], 50.0)

    def test_minimum_history_can_leave_only_23_returns(self) -> None:
        contract = mod.source_contract(ROOT)
        minimum_returns = contract["pca_min_history"] - 1
        self.assertEqual(minimum_returns, 23)
        result = mod.iid_null_experiment(minimum_returns, paths=400)
        self.assertEqual(result["H"], 5)
        self.assertGreater(result["admission_rate"], 0.30)

    def test_summary_is_research_only(self) -> None:
        summary = mod.summarize(ROOT, paths=400)
        self.assertEqual(summary["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertIn("block-bootstrap", " ".join(summary["required_experiment"]))
        self.assertIn("multiplicity", " ".join(summary["required_experiment"]))


if __name__ == "__main__":
    unittest.main()
