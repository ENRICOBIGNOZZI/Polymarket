#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_v6_crossfit_horizon_audit",
    ROOT / "scripts/lf_v6_crossfit_horizon_audit.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalFactorCrossfitHorizonAuditTest(unittest.TestCase):
    def test_target_self_inclusion_manufactures_loading(self) -> None:
        result = MODULE.self_inclusion_diagnostic(markets=5, points=32)
        self.assertAlmostEqual(result["min_abs_inclusive_loading"], 1.0, places=12)
        self.assertAlmostEqual(result["max_abs_leave_one_out_loading"], 0.0, places=12)

    def test_hold_horizon_forecast_is_materially_larger_than_one_step(self) -> None:
        phi_90 = MODULE.horizon_diagnostic(0.90)
        phi_95 = MODULE.horizon_diagnostic(0.95)
        phi_98 = MODULE.horizon_diagnostic(0.98)
        self.assertAlmostEqual(phi_90["hold_horizon_reversion_fraction"], 0.75, places=12)
        self.assertGreater(phi_90["horizon_to_one_step_ratio"], 7.0)
        self.assertGreater(phi_95["horizon_to_one_step_ratio"], 14.0)
        self.assertGreater(phi_98["horizon_to_one_step_ratio"], 19.0)

    def test_current_v6_source_contract_is_detected(self) -> None:
        contract = MODULE.source_contract(ROOT)
        self.assertTrue(all(contract.values()), contract)

    def test_aggressive_successor_keeps_execution_economics(self) -> None:
        report = MODULE.build_report(ROOT)
        profile = report["paper_only_successor_profile"]
        aggressive = profile["aggressive_discovery_after_preconditions"]
        economics = profile["unchanged_execution_economics"]
        self.assertEqual(aggressive["markets"], 700)
        self.assertEqual(aggressive["min_common_points"], 36)
        self.assertEqual(aggressive["min_abs_residual_z"], 0.75)
        self.assertEqual(economics["min_edge"], 0.00020)
        self.assertEqual(economics["max_trade_usd"], 60.0)
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
