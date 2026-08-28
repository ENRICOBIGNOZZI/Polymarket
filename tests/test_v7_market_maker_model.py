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
    def order(self, index: int, *, action: str = "JOIN", event_id: str = "e1",
              intended_size: float = 10.0) -> LedgerEvent:
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
            intended_size=intended_size,
            metadata={"outcome": "YES"},
        )

    def fill_event(self, index: int, *, filled_size: float = 10.0,
                   suffix: str = "") -> LedgerEvent:
        return LedgerEvent(
            event_type="FILL",
            strategy="MICRO_MAKER_PRO",
            model_sha=SHA,
            order_id=f"o{index}",
            fill_id=f"f{index}{suffix}",
            market_id="m1",
            event_id=f"e{index % 8}",
            token_id="t1",
            side="BUY",
            exchange_ts_ms=2000 + index,
            receive_ts_ms=2010 + index,
            fill_price=0.48,
            filled_size=filled_size,
            fee=0.0,
            fee_source="polymarket:maker_fee_zero",
            metadata={"outcome": "YES"},
        )

    def markout(self, index: int, value: float, horizon: str = "60s",
                *, suffix: str = "") -> LedgerEvent:
        return LedgerEvent(
            event_type="MARKOUT",
            strategy="MICRO_MAKER_PRO",
            model_sha=SHA,
            order_id=f"o{index}",
            fill_id=f"f{index}{suffix}",
            market_id="m1",
            event_id=f"e{index % 8}",
            token_id="t1",
            side="BUY",
            exchange_ts_ms=80_000 + index,
            receive_ts_ms=80_010 + index,
            book_snapshot_id=f"mark{index}-{suffix}-{horizon}",
            executable_liquidation_value=4.7,
            markouts={horizon: value},
        )

    def test_fill_probability_uses_filled_fraction_not_leg_or_markout_counts(self) -> None:
        records = [self.order(i) for i in range(10)]
        records += [self.fill_event(i) for i in range(3)]
        records += [self.markout(i, -0.01) for i in range(3)]
        model = fit(records, cold_fill_prior=0.02, prior_strength=20.0)
        group = model["groups"]["JOIN|YES|BUY"]
        self.assertEqual(group["orders"], 10)
        self.assertEqual(group["filled_orders"], 3)
        self.assertEqual(group["fully_filled_orders"], 3)
        expected = (0.4 + 3.0) / (20.0 + 10.0)
        self.assertAlmostEqual(group["fill_probability"], expected)
        self.assertAlmostEqual(group["any_fill_probability"], expected)
        self.assertAlmostEqual(group["full_fill_probability"], expected)
        self.assertEqual(
            group["fill_probability_semantics"],
            "posterior_expected_filled_fraction_per_posted_share",
        )

    def test_multiple_partial_fills_contribute_fractional_success_mass(self) -> None:
        records = [self.order(0, intended_size=10.0)]
        records += [
            self.fill_event(0, filled_size=2.0, suffix="a"),
            self.fill_event(0, filled_size=3.0, suffix="b"),
        ]
        model = fit(records, cold_fill_prior=0.02, prior_strength=20.0)
        group = model["groups"]["JOIN|YES|BUY"]
        expected_fraction_posterior = (0.4 + 0.5) / 21.0
        expected_any_posterior = (0.4 + 1.0) / 21.0
        expected_full_posterior = 0.4 / 21.0
        self.assertAlmostEqual(group["empirical_mean_filled_fraction"], 0.5)
        self.assertAlmostEqual(group["fill_probability"], expected_fraction_posterior)
        self.assertAlmostEqual(group["any_fill_probability"], expected_any_posterior)
        self.assertAlmostEqual(group["full_fill_probability"], expected_full_posterior)
        self.assertEqual(group["filled_orders"], 1)
        self.assertEqual(group["fully_filled_orders"], 0)
        self.assertEqual(group["partially_filled_orders"], 1)
        self.assertGreater(group["any_fill_probability"], group["fill_probability"])
        self.assertGreater(group["fill_probability"], group["full_fill_probability"])
        self.assertTrue(model["partial_fills_are_fractional_success_mass"])

    def test_adverse_markout_is_fill_conditioned(self) -> None:
        records = [self.order(i) for i in range(4)]
        records += [self.fill_event(0), self.fill_event(1)]
        records += [self.markout(0, -0.02), self.markout(1, 0.01)]
        model = fit(records)
        group = model["groups"]["JOIN|YES|BUY"]
        self.assertAlmostEqual(group["adverse_markout_per_share"], 0.005)
        self.assertEqual(group["adverse_markout_horizon"], "60s")
        self.assertEqual(group["adverse_markout_n"], 2)
        self.assertAlmostEqual(group["adverse_markout_filled_shares"], 20.0)
        self.assertEqual(group["markouts"]["60s"]["n"], 2)
        self.assertEqual(group["markouts"]["60s"]["weighting"], "filled_size")

    def test_markout_mean_is_weighted_by_filled_size(self) -> None:
        records = [self.order(0), self.order(1)]
        records += [
            self.fill_event(0, filled_size=1.0),
            self.fill_event(1, filled_size=9.0),
        ]
        records += [
            self.markout(0, -0.10, "45s"),
            self.markout(1, 0.0, "45s"),
        ]
        model = fit(records)
        group = model["groups"]["JOIN|YES|BUY"]
        # Filled-size weighted mean = (-0.10*1 + 0*9) / 10 = -0.01.
        self.assertAlmostEqual(group["adverse_markout_mean_pnl_per_share"], -0.01)
        self.assertAlmostEqual(group["adverse_markout_per_share"], 0.01)
        self.assertAlmostEqual(group["adverse_markout_filled_shares"], 10.0)
        self.assertAlmostEqual(group["markouts"]["45s"]["filled_shares"], 10.0)
        self.assertAlmostEqual(group["markouts"]["45s"]["mean_pnl_per_share"], -0.01)
        self.assertEqual(group["adverse_markout_weighting"], "filled_size")

    def test_correlated_45s_and_60s_markouts_are_not_pooled(self) -> None:
        records = [self.order(i) for i in range(2)]
        records += [self.fill_event(0), self.fill_event(1)]
        # If these four values were pooled, adverse cost would be 0.0275.
        # The canonical target is 45s, so only its two fill-level observations
        # define the adverse-selection target.
        records += [
            self.markout(0, -0.01, "45s"),
            self.markout(1, 0.01, "45s"),
            self.markout(0, -0.05, "60s"),
            self.markout(1, -0.06, "60s"),
        ]
        model = fit(records)
        group = model["groups"]["JOIN|YES|BUY"]
        self.assertEqual(group["adverse_markout_horizon"], "45s")
        self.assertEqual(group["adverse_markout_n"], 2)
        self.assertAlmostEqual(group["adverse_markout_per_share"], 0.0)
        self.assertTrue(model["correlated_horizons_are_not_pooled"])

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
