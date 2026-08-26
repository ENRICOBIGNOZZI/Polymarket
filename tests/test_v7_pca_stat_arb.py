#!/usr/bin/env python3
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lf_v7_pca_current_state_z_audit as pca_z_audit
import v7_pca_stat_arb_core as pca
import v7_pca_stat_arb_inference as inference


class V7PcaStatArbTests(unittest.TestCase):
    def _panel(self):
        rng = random.Random(23)
        n = 100
        f1 = [0.0]
        f2 = [0.0]
        for _ in range(1, n):
            f1.append(f1[-1] + rng.gauss(0.0, 0.16))
            f2.append(f2[-1] + rng.gauss(0.0, 0.10))
        controls = {}
        for j in range(5):
            controls[f"c{j}"] = [
                (1.0 + 0.15 * j) * f1[i] + (0.3 - 0.07 * j) * f2[i] + rng.gauss(0.0, 0.03)
                for i in range(n)
            ]
        residual = 0.0
        target = []
        for i in range(n):
            residual = 0.55 * residual + rng.gauss(0.0, 0.06)
            target.append(0.9 * f1[i] - 0.2 * f2[i] + residual)
        values = {"target": tuple(target), **{k: tuple(v) for k, v in controls.items()}}
        return pca.RawPanel(tuple(range(n)), values)

    def test_target_is_excluded_from_pca_controls(self) -> None:
        panel = self._panel()
        model = pca.fit_target(panel, "target")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertNotIn("target", model.controls)
        self.assertEqual(set(model.controls), {f"c{i}" for i in range(5)})

    def test_components_are_bounded_and_control_only(self) -> None:
        model = pca.fit_target(self._panel(), "target", max_components=3, explained_variance_threshold=0.80)
        assert model is not None
        self.assertGreaterEqual(len(model.eigenvalues), 1)
        self.assertLessEqual(len(model.eigenvalues), 3)
        self.assertGreater(model.explained_variance, 0.0)
        self.assertLessEqual(model.explained_variance, 1.0)

    def test_stationary_residual_fit_is_mean_reverting(self) -> None:
        model = pca.fit_target(self._panel(), "target")
        assert model is not None
        self.assertGreater(model.phi, 0.0)
        self.assertLess(model.phi, 0.95)
        self.assertLess(model.adf_t, 0.0)

    def test_conditional_residual_bootstrap_reestimates_target_and_keeps_controls_fixed(self) -> None:
        panel = self._panel()
        observed = pca.fit_target(panel, "target")
        assert observed is not None
        boot = inference.conditional_null_panel(panel, observed, random.Random(3))
        assert boot is not None
        for control in observed.controls:
            self.assertEqual(boot.values[control], panel.values[control])
        self.assertNotEqual(boot.values["target"], panel.values["target"])
        result = inference.conditional_target_bootstrap_pvalue(panel, "target", reps=59, seed=3)
        self.assertIsNotNone(result)
        assert result is not None
        model, pvalue = result
        self.assertGreaterEqual(pvalue, 0.0)
        self.assertLessEqual(pvalue, 1.0)
        self.assertEqual(model.target, "target")

    def test_by_is_dependence_robust_and_stricter_than_bh_fixture(self) -> None:
        pvalues = {"a": 0.03, "b": 0.06, "c": 0.09}
        self.assertEqual(pca.bh_selected(pvalues, 0.10), {"a", "b", "c"})
        self.assertEqual(inference.benjamini_yekutieli_selected(pvalues, 0.10), set())
        self.assertLess(inference.by_effective_q(3, 0.10), 0.10)

    def test_phi_power_h_changes_prediction_with_horizon(self) -> None:
        panel = self._panel()
        model = pca.fit_target(panel, "target")
        assert model is not None
        current = {mid: values[-1] for mid, values in panel.values.items()}
        current["target"] += 0.6
        short = pca.score_current(model, current, 1)
        long = pca.score_current(model, current, 6)
        assert short is not None and long is not None
        self.assertNotAlmostEqual(short.predicted_logit_move, long.predicted_logit_move)
        self.assertGreaterEqual(long.sigma_logit, short.sigma_logit)

    def test_total_single_leg_risk_never_falls_below_residual_only_risk(self) -> None:
        panel = self._panel()
        model = pca.fit_target(panel, "target")
        assert model is not None
        current = {mid: values[-1] for mid, values in panel.values.items()}
        residual_only = pca.score_current(model, current, 4)
        total = inference.score_with_total_single_leg_risk(panel, model, current, 4)
        assert residual_only is not None and total is not None
        self.assertGreaterEqual(total.sigma_logit, residual_only.sigma_logit)

    def test_executable_candidate_is_single_leg(self) -> None:
        score = pca.PcaScore("m", 0.50, 1.0, 2.0, 0.45, 0.08, 2)
        book = pca.BookEconomics("m", "e", 0.49, 0.51, 0.49, 0.51, 100.0, 0.0, 1.0, True, 100)
        candidate = pca.executable_candidate(score, book, 3600, 100, 0.0, 0.0, 0.0, 30)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.market_id, "m")
        self.assertEqual(candidate.side, "YES")
        self.assertFalse(hasattr(candidate, "hedge_market_id"))

    def test_unknown_fee_fails_closed(self) -> None:
        score = pca.PcaScore("m", 0.50, 1.0, 2.0, 0.45, 0.08, 2)
        book = pca.BookEconomics("m", "e", 0.49, 0.51, 0.49, 0.51, 100.0, 0.04, 1.0, False, 100)
        self.assertIsNone(pca.executable_candidate(score, book, 3600, 100, 0.0, 0.0, 0.0, 30))

    def test_irregular_history_uses_only_regular_suffix(self) -> None:
        histories = {
            "a": {0: 0.1, 60: 0.2, 120: 0.3, 480: 0.4, 540: 0.5, 600: 0.6},
            "b": {0: 0.2, 60: 0.3, 120: 0.4, 480: 0.5, 540: 0.6, 600: 0.7},
            "c": {0: 0.3, 60: 0.4, 120: 0.5, 480: 0.6, 540: 0.7, 600: 0.8},
        }
        panel = pca.build_raw_panel(histories, ["a", "b", "c"], 60, 3)
        self.assertIsNotNone(panel)
        assert panel is not None
        self.assertEqual(panel.times, (480, 540, 600))

    def test_driver_uses_predeclared_controls_by_and_uncertainty_deduction(self) -> None:
        source = (ROOT / "scripts" / "v7_pca_stat_arb_research.py").read_text(encoding="utf-8")
        self.assertIn("predeclare_target_controls", source)
        self.assertIn("conditional_target_bootstrap_pvalue", source)
        self.assertIn("benjamini_yekutieli_selected", source)
        self.assertIn("score_with_total_single_leg_risk", source)
        self.assertIn("resolve_fee_details", source)
        self.assertIn("uncertainty_penalty", source)
        self.assertIn('"unestimable_pvalue": 1.0', source)

    def test_current_state_z_gate_audit_reproduces_training_endpoint_mismatch(self) -> None:
        report = pca_z_audit.audit()
        self.assertEqual(report["status"], "STRUCTURAL_BLOCKER")
        contract = report["source_contract"]
        self.assertTrue(contract["uses_training_endpoint_for_post_multiplicity_z_gate"])
        self.assertFalse(contract["uses_current_scored_residual_z_for_post_multiplicity_gate"])
        self.assertTrue(contract["score_current_is_computed_after_training_endpoint_gate"])
        stale_pass = report["counterexamples"]["historical_extreme_currently_mean_reverted"]
        self.assertTrue(stale_pass["incumbent_passes"])
        self.assertFalse(stale_pass["current_state_should_pass"])
        stale_reject = report["counterexamples"]["historical_mild_currently_extreme"]
        self.assertFalse(stale_reject["incumbent_passes"])
        self.assertTrue(stale_reject["current_state_should_pass"])


if __name__ == "__main__":
    unittest.main()
