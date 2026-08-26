#!/usr/bin/env python3
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lf_v7_pca_composite_null_audit as audit


class V7PcaCompositeNullAuditTest(unittest.TestCase):
    def test_source_contract_is_frozen(self):
        self.assertEqual(audit.SOURCE_PR, 466)
        self.assertEqual(audit.SOURCE_HEAD, "918f83f23b4fc586747457dcd9d7ef2dd9ddcbbb")
        self.assertEqual(audit.SOURCE_BOOTSTRAP, "joint_all_series_increment_i1")
        self.assertEqual(audit.PROPOSED_BOOTSTRAP, "conditional_target_residual_increment_i1")

    def test_conditional_null_keeps_target_excluded_controls_fixed(self):
        panel = audit.generate_stationary_control_panel(72, 12345, None)
        boot = audit.conditional_target_residual_i1_bootstrap(panel, random.Random(99))
        self.assertIsNotNone(boot)
        self.assertTrue(any(abs(boot[t][0] - panel[t][0]) > 1e-9 for t in range(1, len(panel))))
        for t in range(len(panel)):
            self.assertEqual(boot[t][1:], panel[t][1:])

    def test_joint_all_i1_bootstrap_changes_nuisance_control_paths(self):
        panel = audit.generate_stationary_control_panel(72, 12345, None)
        boot = audit.joint_all_series_i1_bootstrap(panel, random.Random(99))
        changed = any(
            abs(boot[t][j] - panel[t][j]) > 1e-9
            for t in range(1, len(panel))
            for j in range(1, len(panel[t]))
        )
        self.assertTrue(changed)

    def test_power_counterexample_on_stationary_controls(self):
        panel = audit.generate_stationary_control_panel(96, 10009, 0.90)
        result = audit.bootstrap_pvalues(panel, repetitions=79, seed=30009)
        self.assertIsNotNone(result)
        self.assertGreater(result["joint_all_i1_p"], 0.10)
        self.assertLessEqual(result["conditional_target_residual_p"], 0.10)
        self.assertLess(result["conditional_target_residual_p"], result["joint_all_i1_p"])


if __name__ == "__main__":
    unittest.main()
