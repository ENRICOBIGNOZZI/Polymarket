#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v5_pca_horizon_semantics_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v5_pca_horizon_semantics_audit", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class V5PcaHorizonSemanticsAuditTest(unittest.TestCase):
    def test_source_contract_routes_one_step_pca_into_terminal_path(self) -> None:
        contract = mod.source_contract(ROOT)
        self.assertTrue(contract["engine_contract_present"])
        self.assertTrue(contract["history_is_price_only_contract_present"])
        self.assertTrue(contract["manager_singleton_contract_present"])
        self.assertEqual(contract["pca_capital_fraction"], 0.25)
        self.assertEqual(contract["pca_interval_seconds"], 15)
        self.assertEqual(contract["history_fidelity_minutes"], 30)
        self.assertEqual(contract["pca_fractional_kelly"], 0.12)
        self.assertEqual(contract["pca_child_starting_capital"], 2500.0)

    def test_one_step_fair_can_reverse_terminal_ev_sign(self) -> None:
        result = mod.horizon_semantics_fixture()
        self.assertAlmostEqual(result["engine_interpreted_gross_edge"], 0.04, places=12)
        self.assertAlmostEqual(result["actual_terminal_ev_per_share"], -0.01, places=12)
        self.assertTrue(result["sign_reversal"])
        self.assertAlmostEqual(result["engine_full_kelly"], 0.04 / 0.49, places=12)
        self.assertAlmostEqual(
            result["engine_fractional_kelly_dollars_before_other_caps"],
            0.12 * (0.04 / 0.49) * 2500.0,
            places=10,
        )

    def test_same_markout_forecast_does_not_identify_terminal_probability(self) -> None:
        rows = mod.terminal_identification_set(one_step_fair=0.55, executable_price=0.51)
        self.assertEqual([row["one_step_fair"] for row in rows], [0.55, 0.55, 0.55])
        self.assertEqual([row["terminal_probability"] for row in rows], [0.40, 0.50, 0.60])
        self.assertLess(rows[0]["terminal_ev_per_share"], 0.0)
        self.assertLess(rows[1]["terminal_ev_per_share"], 0.0)
        self.assertGreater(rows[2]["terminal_ev_per_share"], 0.0)

    def test_price_only_window_changes_horizon_composition_after_bootstrap(self) -> None:
        one_hour = mod.mixed_horizon_window(live_elapsed_seconds=3600)
        two_hours = mod.mixed_horizon_window(live_elapsed_seconds=7200)
        three_hours = mod.mixed_horizon_window(live_elapsed_seconds=10800)
        self.assertEqual(one_hour["bootstrap_fidelity_seconds"], 1800)
        self.assertEqual(one_hour["configured_live_sleep_seconds"], 15)
        self.assertEqual(one_hour["approx_live_returns"], 240)
        self.assertEqual(one_hour["approx_bootstrap_returns"], 480)
        self.assertAlmostEqual(one_hour["approx_live_fraction"], 1.0 / 3.0, places=12)
        self.assertEqual(two_hours["approx_live_returns"], 480)
        self.assertEqual(two_hours["approx_bootstrap_returns"], 240)
        self.assertEqual(three_hours["approx_live_returns"], 720)
        self.assertEqual(three_hours["approx_bootstrap_returns"], 0)

    def test_summary_requires_separate_markout_and_terminal_evaluation(self) -> None:
        summary = mod.summarize(ROOT)
        self.assertEqual(summary["decision"], "MORE_EVIDENCE_REQUIRED")
        experiment = " ".join(summary["required_experiment"])
        self.assertIn("mark-to-market", experiment)
        self.assertIn("time-to-resolution", experiment)
        self.assertIn("Brier", experiment)
        self.assertIn("1x/1.5x/2x", experiment)
        self.assertTrue(summary["counterexample"]["sign_reversal"])
        self.assertEqual(summary["target_horizon_mix"][-1]["approx_live_fraction"], 1.0)


if __name__ == "__main__":
    unittest.main()
