#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import random
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


def _ar1(rng: random.Random, n: int, phi: float, scale: float = 1.0) -> list[float]:
    out = [0.0]
    for _ in range(1, n):
        out.append(phi * out[-1] + rng.gauss(0.0, scale))
    return out


def _random_walk(rng: random.Random, n: int, scale: float = 1.0) -> list[float]:
    out = [0.0]
    for _ in range(1, n):
        out.append(out[-1] + rng.gauss(0.0, scale))
    return out


def _mixed_null_panel(seed: int, controls_mode: str, n: int = 72) -> core.StandardizedPanel:
    rng = random.Random(seed)
    controls: dict[str, list[float]] = {}
    if controls_mode == "stationary":
        for j in range(4):
            controls[f"c{j}"] = _ar1(rng, n, 0.6)
    elif controls_mode == "i1":
        common = _random_walk(rng, n)
        for j in range(4):
            idio = _random_walk(rng, n, 0.4)
            controls[f"c{j}"] = [a + b for a, b in zip(common, idio)]
    elif controls_mode == "cointegrated":
        common = _random_walk(rng, n)
        for j in range(4):
            stationary = _ar1(rng, n, 0.5, 0.5)
            controls[f"c{j}"] = [a + b for a, b in zip(common, stationary)]
    else:
        raise ValueError(controls_mode)

    factor = [sum(controls[mid][t] for mid in controls) / len(controls) for t in range(n)]
    a_residual = _random_walk(rng, n)
    b_residual = _ar1(rng, n, 0.6)
    levels = dict(controls)
    levels["A"] = [0.8 * f + u for f, u in zip(factor, a_residual)]
    levels["B"] = [-0.5 * f + u for f, u in zip(factor, b_residual)]
    panel = core.standardize_levels(levels, tuple(range(n)))
    assert panel is not None
    return panel


def _orientation_cancellation_panel(seed: int = 20260826, n: int = 72) -> core.StandardizedPanel:
    rng = random.Random(seed)
    common = _ar1(rng, n, 0.85, 0.8)
    a_residual = _random_walk(rng, n, 0.45)
    b_residual = _ar1(rng, n, 0.65, 0.45)
    levels = {
        "c0": list(common),
        "c1": list(common),
        "c2": [-x for x in common],
        "c3": [-x for x in common],
        "A": [0.9 * f + u for f, u in zip(common, a_residual)],
        "B": [-0.7 * f + u for f, u in zip(common, b_residual)],
    }
    panel = core.standardize_levels(levels, tuple(range(n)))
    assert panel is not None
    return panel


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

    def test_bootstrap_pair_factor_matches_observed_orientation_invariant_basis(self) -> None:
        panel = _orientation_cancellation_panel()
        pair = ("A", "B")
        factor = inference._pair_factor(panel, pair, 2)
        expected = core.orientation_invariant_pc1(
            {mid: panel.values[mid] for mid in ("c0", "c1", "c2", "c3")}
        )
        self.assertIsNotNone(factor)
        self.assertIsNotNone(expected)
        assert factor is not None and expected is not None
        self.assertGreater(max(abs(x) for x in factor), 0.1)
        for got, want in zip(factor, expected):
            self.assertAlmostEqual(got, want, places=12)

        arithmetic_mean = tuple(
            sum(panel.values[mid][j] for mid in ("c0", "c1", "c2", "c3")) / 4.0
            for j in range(len(panel.times))
        )
        self.assertLess(max(abs(x) for x in arithmetic_mean), 1e-12)

    def test_marginal_null_pvalue_is_invariant_to_control_sign_coding(self) -> None:
        panel = _orientation_cancellation_panel()
        first = inference.marginal_residual_unit_root_pvalue(
            panel, ("A", "B"), "A", reps=99, seed=31
        )
        self.assertIsNotNone(first)

        values = dict(panel.values)
        values["c0"] = tuple(-x for x in values["c0"])
        values["c2"] = tuple(-x for x in values["c2"])
        flipped = core.StandardizedPanel(panel.times, values, dict(panel.means), dict(panel.scales))
        second = inference.marginal_residual_unit_root_pvalue(
            flipped, ("A", "B"), "A", reps=99, seed=31
        )
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertAlmostEqual(first[1], second[1], places=15)
        self.assertAlmostEqual(first[0].adf_a, second[0].adf_a, places=12)

    def test_marginal_null_is_invariant_to_other_target_path(self) -> None:
        panel = _mixed_null_panel(101, "cointegrated")
        first = inference.marginal_residual_unit_root_pvalue(
            panel, ("A", "B"), "A", reps=99, seed=17
        )
        self.assertIsNotNone(first)

        values = dict(panel.values)
        replacement_b = tuple(reversed(panel.values["B"]))
        values["B"] = replacement_b
        changed = core.StandardizedPanel(panel.times, values, dict(panel.means), dict(panel.scales))
        second = inference.marginal_residual_unit_root_pvalue(
            changed, ("A", "B"), "A", reps=99, seed=17
        )
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertAlmostEqual(first[1], second[1], places=15)

    def test_mixed_null_size_is_not_grossly_inflated_across_control_regimes(self) -> None:
        # A has an I(1) residual by construction while B is stationary.  The
        # controls vary from stationary to I(1) to cointegrated.  This is the
        # mixed composite-null configuration that an all-series-I(1) bootstrap
        # did not establish.  The deterministic Monte Carlo tolerance is wider
        # than nominal 10% because the regression test intentionally stays small.
        for mode in ("stationary", "i1", "cointegrated"):
            rejects = 0
            trials = 24
            for i in range(trials):
                panel = _mixed_null_panel(1000 + 100 * len(mode) + i, mode)
                result = inference.marginal_residual_unit_root_pvalue(
                    panel,
                    ("A", "B"),
                    "A",
                    reps=99,
                    seed=9000 + i,
                )
                self.assertIsNotNone(result)
                assert result is not None
                rejects += int(result[1] <= 0.10)
            self.assertLessEqual(rejects, 5, msg=f"mixed-null size too large for {mode}: {rejects}/{trials}")


if __name__ == "__main__":
    unittest.main()
