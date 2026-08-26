#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_NAME = "v7_local_factor_core"
CORE_SPEC = importlib.util.spec_from_file_location(CORE_NAME, ROOT / "scripts" / "v7_local_factor_core.py")
assert CORE_SPEC is not None and CORE_SPEC.loader is not None
core = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_NAME] = core
CORE_SPEC.loader.exec_module(core)

NAME = "v7_local_factor_inference"
SPEC = importlib.util.spec_from_file_location(NAME, ROOT / "scripts" / "v7_local_factor_inference.py")
assert SPEC is not None and SPEC.loader is not None
inference = importlib.util.module_from_spec(SPEC)
sys.modules[NAME] = inference
SPEC.loader.exec_module(inference)


class V7LocalFactorInferenceTests(unittest.TestCase):
    def test_intersection_union_uses_max_marginal_pvalue(self) -> None:
        self.assertEqual(inference.intersection_union_pvalue(0.01, 0.20), 0.20)

    def test_59_reps_cannot_resolve_singleton_bh_tail_for_1339_pairs(self) -> None:
        diagnostics = inference.bh_resolution_diagnostics(1339, 59, 0.10)
        self.assertAlmostEqual(diagnostics["minimum_attainable_pvalue"], 1.0 / 60.0)
        self.assertAlmostEqual(diagnostics["first_rank_bh_threshold"], 0.10 / 1339.0)
        self.assertFalse(diagnostics["singleton_bh_resolution_adequate"])
        self.assertEqual(diagnostics["repetitions_required_for_singleton_bh_resolution"], 13389)
        self.assertGreaterEqual(diagnostics["minimum_rank_needed_if_pvalues_hit_nominal_floor"], 224)

    def test_resolution_becomes_adequate_when_repetitions_cover_first_bh_step(self) -> None:
        diagnostics = inference.bh_resolution_diagnostics(10, 599, 0.10)
        self.assertTrue(diagnostics["singleton_bh_resolution_adequate"])
        self.assertLessEqual(
            diagnostics["minimum_attainable_pvalue"], diagnostics["first_rank_bh_threshold"]
        )


if __name__ == "__main__":
    unittest.main()
