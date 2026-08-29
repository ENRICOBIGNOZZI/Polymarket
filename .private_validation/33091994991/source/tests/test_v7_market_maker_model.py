#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent
from v7_market_maker_model import fit

SHA = "a" * 40


class MakerExecutionModelTests(unittest.TestCase):
    def order(self, index: int, *, action: str = "JOIN", event_id: str = "e1") -> LedgerEvent:
        return LedgerEvent(
            event_type="ORDER_SUBMITTED",
            strategy="MICRO_MAKER_PRO",
            model_sha=SHA,
            order_id=f"o{index}",
            market_id="m1",
            event_id=event_id,
            token_id="t1",
            side="BUY",
            decision_ts_ms=1000 + index,
            exchange_ts_ms=900 + index,
            receive_ts_ms=950 + index,
            book_snapshot_id=f"b{index}",
            intended_action=action,
            intended_size=10.0,
            metadata={"outcome": "YES"},
        )

    def fill_event(self, index: int) -> LedgerEvent:
        return LedgerEvent(
            event_type="FILL",
            strategy="MICRO_MAKER_PRO",
            model_sha=SHA,
            order_id=f"o{index}",
            fill_id=f"f{index}",
            market_id="m1",
            event_id=f"e{index % 8}",
            token_id="t1",
            side="BUY",
            exchange_ts_ms=2000 + index,
            receive_ts_ms=2010 + index,
            fill_price=0.48,
            filled_size=10.0,
            fee=0.0,
            fee_source="polymarket:maker_fee_zero",
            metadata={"outcome": "YES"},
        )

    def markout(self, index: int, value: float, horizon: str = "60s") -> LedgerEvent:
        return LedgerEvent(
            event_type="MARKOUT",
            strategy="MICRO_MAKER_PRO",
            model_sha=SHA,
            order_id=f"o{index}",
            fill_id=f"f{index}",
            market_id="m1",
            event_id=f"e{index % 8}",
            token_id="t1",
            side="BUY",
            exchange_ts_ms=80_000 + index,
            receive_ts_ms=80_010 + index,
            book_snapshot_id=f"mark{index}",
            executable_liquidation_value=4.7,
            markouts={horizon: value},
        )

    def test_fill_probability_uses_orders_not_leg_or_markout_counts(self) -> None:
        records = [self.order(i) for i in range(10)]
        records += [self.fill_event(i) for i in range(3)]
        records += [self.markout(i, -0.01) for i in range(3)]
        model = fit(records, cold_fill_prior=0.02, prior_strength=20.0)
        group = model["groups"]["JOIN|YES|BUY"]
        self.assertEqual(group["orders"], 10)
        self.assertEqual(group["filled_orders"], 3)
        expected = (0.4 + 3.0) / (20.0 + 10.0)
        self.assertAlmostEqual(group["fill_probability"], expected)

    def test_adverse_markout_is_fill_conditioned(self) -> None:
        records = [self.order(i) for i in range(4)]
        records += [self.fill_event(0), self.fill_event(1)]
        records += [self.markout(0, -0.02), self.markout(1, 0.01)]
        model = fit(records)
        group = model["groups"]["JOIN|YES|BUY"]
        self.assertAlmostEqual(group["adverse_markout_per_share"], 0.005)
        self.assertEqual(group["markouts"]["60s"]["n"], 2)

    def test_action_specific_groups_do_not_pool_join_and_improve(self) -> None:
        records = [self.order(i, action="JOIN") for i in range(5)]
        records += [self.order(100 + i, action="IMPROVE1") for i in range(5)]
        records += [self.fill_event(i) for i in range(2)]
        model = fit(records)
        self.assertIn("JOIN|YES|BUY", model["groups"])
        self.assertIn("IMPROVE1|YES|BUY", model["groups"])
        self.assertGreater(
            model["groups"]["JOIN|YES|BUY"]["fill_probability"],
            model["groups"]["IMPROVE1|YES|BUY"]["fill_probability"],
        )

    def test_maturity_requires_orders_fills_and_independent_event_clusters(self) -> None:
        records = []
        for i in range(60):
            records.append(self.order(i, event_id=f"event-{i % 10}"))
        for i in range(25):
            fill = self.fill_event(i)
            records.append(fill)
            records.append(self.markout(i, 0.002))
        model = fit(records)
        self.assertTrue(model["groups"]["JOIN|YES|BUY"]["mature"])


if __name__ == "__main__":
    unittest.main()
