#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v6_queue_size_coupling_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v6_queue_size_coupling_audit", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class QueueSizeCouplingAuditTest(unittest.TestCase):
    def test_incumbent_rejects_low_queue_even_when_risk_budget_is_available(self) -> None:
        for queue in (5.0, 19.0):
            generated = MODULE.incumbent_generator_max_notional(queue, 0.98, 5.0, 60.0)
            shares, admitted = MODULE.incumbent_broker_target_shares(generated, 0.98, queue, 5.0)
            self.assertFalse(admitted)
            self.assertLess(shares, 5.0)

    def test_twenty_share_queue_is_exact_hard_floor_for_five_share_minimum(self) -> None:
        generated = MODULE.incumbent_generator_max_notional(20.0, 0.98, 5.0, 60.0)
        shares, admitted = MODULE.incumbent_broker_target_shares(generated, 0.98, 20.0, 5.0)
        self.assertTrue(admitted)
        self.assertAlmostEqual(shares, 5.0, places=9)

    def test_worse_queue_can_mechanically_increase_target_size(self) -> None:
        small_cap = MODULE.incumbent_generator_max_notional(20.0, 0.98, 5.0, 60.0)
        small_shares, small_ok = MODULE.incumbent_broker_target_shares(small_cap, 0.98, 20.0, 5.0)
        large_cap = MODULE.incumbent_generator_max_notional(1000.0, 0.98, 5.0, 60.0)
        large_shares, large_ok = MODULE.incumbent_broker_target_shares(large_cap, 0.98, 1000.0, 5.0)
        self.assertTrue(small_ok and large_ok)
        self.assertAlmostEqual(small_shares, 5.0, places=9)
        self.assertAlmostEqual(large_shares, 60.0 / 0.98, places=9)
        self.assertGreater(large_shares, 12.0 * small_shares)

    def test_reference_separates_queue_from_unwind_capacity(self) -> None:
        shares, admitted = MODULE.decoupled_reference_target_shares(60.0, 0.98, 5.0, 100.0)
        self.assertTrue(admitted)
        self.assertAlmostEqual(shares, 25.0, places=9)

    def test_source_contract_binds_audit_to_current_relation_and_broker_paths(self) -> None:
        relation = (ROOT / "scripts" / "v6_relation_intents.py").read_text(encoding="utf-8")
        broker = (ROOT / "src" / "multileg_paper.cpp").read_text(encoding="utf-8")
        self.assertIn("min_shares = min(book.bid_size", relation)
        self.assertIn("max_notional = min(max_trade, min_shares * max(cost, 1e-6))", relation)
        self.assertIn("0.25*std::max(1.0,touch_size_at(book,limits[i],true))", broker)
        self.assertIn("units*v[i].weight + 1e-9 < book.min_order_size", broker)


if __name__ == "__main__":
    unittest.main()
