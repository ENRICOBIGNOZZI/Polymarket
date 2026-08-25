#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MicroStructuralV4Contracts(unittest.TestCase):
    def test_micro_v4_has_multiscale_flow_and_momentum_features(self):
        micro = load("micro_v4_contract", "scripts/v6_micro_taker_v4.py")
        common = load("micro_v4_common", "scripts/v6_market_common.py")

        class Token:
            def __init__(self, token): self.token = token

        flow = common.TapeFlow([
            common.TapeTrade(900, "y", "BUY", 0.48, 10),
            common.TapeTrade(990, "y", "BUY", 0.52, 10),
            common.TapeTrade(900, "n", "SELL", 0.52, 10),
            common.TapeTrade(990, "n", "SELL", 0.48, 10),
        ], now=1000)
        original = micro.base.BASE_FEATURES
        micro.base.BASE_FEATURES = lambda y, n, f, w: ([0.0] * 10, 0.50, 0.02)
        try:
            x, mid, spread = micro.features(Token("y"), Token("n"), flow, 180)
        finally:
            micro.base.BASE_FEATURES = original
        self.assertEqual(len(x), 20)
        self.assertEqual(mid, 0.50)
        self.assertEqual(spread, 0.02)
        self.assertGreater(x[-2], 0.0)
        self.assertGreater(x[-1], 0.0)

    def test_structural_projection_is_monotone_and_weighted(self):
        structural = load("structural_curve_contract", "scripts/v6_structural_curve.py")
        observed = [0.70, 0.80, 0.40, 0.30]
        weights = [1.0, 3.0, 1.0, 1.0]
        q = structural.monotone_projection(observed, weights, decreasing=True)
        self.assertTrue(all(q[i] >= q[i + 1] - 1e-12 for i in range(len(q) - 1)))
        # The first violation pools 0.70 and 0.80 at their weighted mean 0.775.
        self.assertAlmostEqual(q[0], 0.775, places=12)
        self.assertAlmostEqual(q[1], 0.775, places=12)

    def test_structural_increasing_projection_for_below_thresholds(self):
        structural = load("structural_curve_down_contract", "scripts/v6_structural_curve.py")
        q = structural.monotone_projection([0.20, 0.15, 0.60], [1, 1, 1], decreasing=False)
        self.assertTrue(all(q[i] <= q[i + 1] + 1e-12 for i in range(len(q) - 1)))
        self.assertAlmostEqual(q[0], 0.175, places=12)
        self.assertAlmostEqual(q[1], 0.175, places=12)


if __name__ == "__main__":
    unittest.main()
