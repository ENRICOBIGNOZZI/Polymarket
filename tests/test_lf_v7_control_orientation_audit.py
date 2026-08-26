#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lf_v7_control_orientation_audit as audit


class LocalFactorControlOrientationAuditTest(unittest.TestCase):
    def test_equal_weight_controls_can_annihilate_common_factor(self) -> None:
        latent, controls, target_a, target_b = audit.deterministic_fixture()
        raw = audit.equal_weight_factor(controls)
        self.assertAlmostEqual(audit.stdev(raw), 0.0, places=12)
        self.assertIsNone(audit.ols_loading(target_a, raw))
        self.assertIsNone(audit.ols_loading(target_b, raw))
        self.assertGreater(audit.stdev(latent), 0.9)

    def test_target_free_orientation_recovers_common_factor(self) -> None:
        latent, controls, target_a, target_b = audit.deterministic_fixture()
        oriented = audit.anchor_oriented_factor(controls)
        self.assertGreater(audit.stdev(oriented), 0.9)
        self.assertAlmostEqual(abs(audit.correlation(oriented, latent)), 1.0, places=12)
        loading_a = audit.ols_loading(target_a, oriented)
        loading_b = audit.ols_loading(target_b, oriented)
        self.assertIsNotNone(loading_a)
        self.assertIsNotNone(loading_b)
        self.assertGreater(abs(float(loading_a)), 0.5)
        self.assertGreater(abs(float(loading_b)), 0.5)

    def test_oriented_factor_is_invariant_to_control_coding_up_to_global_sign(self) -> None:
        _latent, controls, _target_a, _target_b = audit.deterministic_fixture()
        baseline = audit.anchor_oriented_factor(controls)
        recoded = {
            "c1": tuple(-x for x in controls["c1"]),
            "c2": controls["c2"],
            "c3": tuple(-x for x in controls["c3"]),
            "c4": controls["c4"],
        }
        candidate = audit.anchor_oriented_factor(recoded)
        self.assertAlmostEqual(abs(audit.correlation(baseline, candidate)), 1.0, places=12)

    def test_audit_contract_and_safety(self) -> None:
        result = audit.run_audit()
        self.assertTrue(result.raw_factor_annihilated)
        self.assertTrue(result.orientation_invariant_up_to_global_sign)
        self.assertTrue(math.isfinite(result.oriented_factor_sd))
        self.assertAlmostEqual(result.oriented_latent_abs_correlation, 1.0, places=12)


if __name__ == "__main__":
    unittest.main()
