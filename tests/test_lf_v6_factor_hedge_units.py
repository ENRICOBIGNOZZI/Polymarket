#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v6_factor_hedge_units_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v6_factor_hedge_units_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class LocalFactorHedgeUnitAuditTest(unittest.TestCase):
    def test_current_source_uses_standardized_loading_for_weights_but_yes_sd_for_forecast(self) -> None:
        contract = mod.source_contract(ROOT / "scripts" / "v6_local_factor_intents.py")
        self.assertTrue(contract["uses_loading_only_for_pair_weight"])
        self.assertTrue(contract["converts_residual_to_logit_units_with_yes_sd"])

    def test_loading_only_weight_can_leave_large_price_factor_exposure(self) -> None:
        a = mod.Leg(mid=0.50, yes_sd=0.10, loading=1.0)
        b = mod.Leg(mid=0.95, yes_sd=0.30, loading=-1.0)
        incumbent = mod.incumbent_weight(a, b)
        corrected = mod.price_delta_weight(a, b)
        residual = mod.residual_price_exposure(a, b, incumbent)
        self.assertAlmostEqual(incumbent, 1.0, places=12)
        self.assertAlmostEqual(corrected, 1.7543859649122793, places=12)
        self.assertGreater(abs(residual) / abs(mod.price_factor_delta(a)), 0.40)
        self.assertAlmostEqual(mod.residual_price_exposure(a, b, corrected), 0.0, places=12)

    def test_extreme_probability_compresses_price_delta(self) -> None:
        a = mod.Leg(mid=0.50, yes_sd=0.10, loading=1.0)
        b = mod.Leg(mid=0.99, yes_sd=0.30, loading=-1.0)
        self.assertAlmostEqual(mod.incumbent_weight(a, b), 1.0, places=12)
        self.assertGreater(mod.price_delta_weight(a, b), 8.0)
        residual_fraction = abs(mod.residual_price_exposure(a, b, 1.0)) / abs(mod.price_factor_delta(a))
        self.assertGreater(residual_fraction, 0.85)

    def test_yes_sd_mismatch_can_turn_nominal_hedge_into_overhedge(self) -> None:
        a = mod.Leg(mid=0.50, yes_sd=0.10, loading=1.0)
        b = mod.Leg(mid=0.50, yes_sd=0.50, loading=-1.0)
        self.assertAlmostEqual(mod.incumbent_weight(a, b), 1.0, places=12)
        self.assertAlmostEqual(mod.price_delta_weight(a, b), 0.2, places=12)
        residual = mod.residual_price_exposure(a, b, 1.0)
        self.assertLess(residual, 0.0)
        self.assertAlmostEqual(abs(residual) / abs(mod.price_factor_delta(a)), 4.0, places=12)

    def test_no_side_reverses_price_factor_exposure(self) -> None:
        yes = mod.Leg(mid=0.40, yes_sd=0.20, loading=0.7, side="YES")
        no = mod.Leg(mid=0.40, yes_sd=0.20, loading=0.7, side="NO")
        self.assertTrue(math.isclose(mod.price_factor_delta(yes), -mod.price_factor_delta(no), rel_tol=0.0, abs_tol=1e-15))


if __name__ == "__main__":
    unittest.main()
