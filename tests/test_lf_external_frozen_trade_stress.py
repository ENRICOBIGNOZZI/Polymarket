#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "scripts" / "lf_external_frozen_trade_stress_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_external_frozen_trade_stress_audit", AUDIT_PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


class FrozenTradeCostStressTests(unittest.TestCase):
    def test_reselected_frontier_is_not_frozen_trade_stress(self) -> None:
        payload = AUDIT.evaluate()
        checks = payload["checks"]
        self.assertTrue(checks["reselected_pnl_can_increase_with_cost"])
        self.assertTrue(checks["reselected_trade_count_changes"])
        self.assertTrue(checks["frozen_trade_count_constant"])
        self.assertTrue(checks["frozen_pnl_nonincreasing"])

        example = payload["counterexample"]
        reselected = example["reselected_frontier"]
        frozen = example["frozen_trade_stress"]
        self.assertEqual(reselected["1.0"]["trades"], 2)
        self.assertEqual(reselected["1.5"]["trades"], 1)
        self.assertAlmostEqual(reselected["1.0"]["pnl"], 0.016)
        self.assertAlmostEqual(reselected["1.5"]["pnl"], 0.037)
        self.assertAlmostEqual(reselected["2.0"]["pnl"], 0.036)
        self.assertEqual(frozen["1.0"]["trades"], 2)
        self.assertEqual(frozen["1.5"]["trades"], 2)
        self.assertEqual(frozen["2.0"]["trades"], 2)
        self.assertAlmostEqual(frozen["1.0"]["pnl"], 0.016)
        self.assertAlmostEqual(frozen["1.5"]["pnl"], 0.014)
        self.assertAlmostEqual(frozen["2.0"]["pnl"], 0.012)

    def test_current_external_backtester_reselects_at_each_multiplier(self) -> None:
        source = (ROOT / "scripts" / "external_intelligence.py").read_text(encoding="utf-8")
        self.assertIn("for multiplier in multipliers:", source)
        self.assertIn("trade_pnl(row, predicted, base_cost * multiplier)", source)
        self.assertIn('"cost_stress_net_pnl": {key: sum(values) for key, values in pnls.items()}', source)
        self.assertNotIn("frozen_trade_cost_stress_net_pnl", source)


if __name__ == "__main__":
    unittest.main()
