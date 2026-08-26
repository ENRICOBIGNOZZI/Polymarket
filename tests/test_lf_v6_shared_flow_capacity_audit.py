#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v6_shared_flow_capacity_audit.py"
spec = importlib.util.spec_from_file_location("lf_v6_shared_flow_capacity_audit", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class SharedTradeFlowCapacityAuditTest(unittest.TestCase):
    def test_current_source_replays_full_trade_size_per_matching_leg(self) -> None:
        report = mod.run_audit(ROOT)
        contract = report["source_contract"]
        self.assertTrue(contract["trade_loop_present"])
        self.assertTrue(contract["leg_loop_present"])
        self.assertTrue(contract["full_trade_size_passed_per_leg"])
        self.assertFalse(contract["shared_trade_capacity_variable_present"])

    def test_same_public_trade_cannot_fill_two_orders_beyond_shared_capacity(self) -> None:
        orders = [
            mod.PassiveOrder("A", 100.0, 10.0),
            mod.PassiveOrder("B", 100.0, 10.0),
        ]
        incumbent = mod.incumbent_total_fill(orders, 110.0)
        shared = sum(mod.shared_queue_fill(orders, 110.0).values())
        self.assertEqual(incumbent, 20.0)
        self.assertEqual(shared, 10.0)
        self.assertGreater(incumbent, shared)

    def test_aggregate_simulated_fill_can_exceed_observed_trade_volume(self) -> None:
        orders = [
            mod.PassiveOrder("A", 0.0, 10.0),
            mod.PassiveOrder("B", 0.0, 10.0),
            mod.PassiveOrder("C", 0.0, 10.0),
        ]
        trade_size = 15.0
        incumbent = mod.incumbent_total_fill(orders, trade_size)
        shared = sum(mod.shared_queue_fill(orders, trade_size).values())
        self.assertEqual(incumbent, 30.0)
        self.assertEqual(shared, 15.0)
        self.assertGreater(incumbent, trade_size)
        self.assertLessEqual(shared, trade_size)

    def test_shared_queue_fixture_requires_one_common_snapshot(self) -> None:
        with self.assertRaises(ValueError):
            mod.shared_queue_fill(
                [mod.PassiveOrder("A", 10.0, 5.0), mod.PassiveOrder("B", 11.0, 5.0)],
                20.0,
            )


if __name__ == "__main__":
    unittest.main()
