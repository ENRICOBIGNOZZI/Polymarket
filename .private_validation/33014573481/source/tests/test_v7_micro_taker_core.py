from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_micro_taker_core as core


class MicroTakerRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.book = core.BookSnapshot(
            yes_bid=0.49,
            yes_ask=0.51,
            no_bid=0.49,
            no_ask=0.51,
            liquidity=1000.0,
            received_ts=100,
        )
        self.zero_fee = core.FeeSpec(False, 0.0, authoritative=True)

    def test_unknown_fee_fails_closed(self) -> None:
        candidate = core.choose_side(
            book=self.book,
            predicted_yes_mid=0.55,
            prediction_sigma_probability=0.005,
            fee=core.FeeSpec(True, 0.04, authoritative=False),
            horizon_seconds=60,
            now=100,
            slippage_bps_per_leg=5,
            uncertainty_z=1.0,
            adverse_markout_penalty_bps=2,
            capital_cost_bps_per_hour=0.25,
            max_book_age_seconds=10,
            minimum_net_edge=0.0,
        )
        self.assertIsNone(candidate)

    def test_yes_and_no_are_symmetric(self) -> None:
        yes = core.round_trip_economics(
            side="YES",
            book=self.book,
            predicted_yes_mid=0.56,
            prediction_sigma_probability=0.0,
            fee=self.zero_fee,
            horizon_seconds=60,
            now=100,
            slippage_bps_per_leg=0,
            uncertainty_z=0,
            adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
        )
        no = core.round_trip_economics(
            side="NO",
            book=self.book,
            predicted_yes_mid=0.44,
            prediction_sigma_probability=0.0,
            fee=self.zero_fee,
            horizon_seconds=60,
            now=100,
            slippage_bps_per_leg=0,
            uncertainty_z=0,
            adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
        )
        self.assertIsNotNone(yes)
        self.assertIsNotNone(no)
        assert yes is not None and no is not None
        self.assertAlmostEqual(yes.net_pnl_per_share, no.net_pnl_per_share)
        self.assertAlmostEqual(yes.net_edge, no.net_edge)

    def test_entry_only_edge_can_be_positive_while_round_trip_is_negative(self) -> None:
        fee = core.FeeSpec(True, 0.04, 1.0, True, True)
        # Forecast appears above the current ask, but not by enough to pay current
        # spread, both taker fees and the liquidation leg.
        candidate = core.round_trip_economics(
            side="YES",
            book=self.book,
            predicted_yes_mid=0.525,
            prediction_sigma_probability=0.0,
            fee=fee,
            horizon_seconds=60,
            now=100,
            slippage_bps_per_leg=0,
            uncertainty_z=0,
            adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        entry_only_edge = 0.525 - candidate.entry_price - candidate.entry_fee_per_share
        self.assertGreater(entry_only_edge, 0.0)
        self.assertLess(candidate.net_pnl_per_share, 0.0)
        selected = core.choose_side(
            book=self.book,
            predicted_yes_mid=0.525,
            prediction_sigma_probability=0.0,
            fee=fee,
            horizon_seconds=60,
            now=100,
            slippage_bps_per_leg=0,
            uncertainty_z=0,
            adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
            minimum_net_edge=0.0,
        )
        self.assertIsNone(selected)

    def test_both_taker_fees_are_charged(self) -> None:
        fee = core.FeeSpec(True, 0.04, 1.0, True, True)
        candidate = core.round_trip_economics(
            side="YES",
            book=self.book,
            predicted_yes_mid=0.60,
            prediction_sigma_probability=0.0,
            fee=fee,
            horizon_seconds=60,
            now=100,
            slippage_bps_per_leg=0,
            uncertainty_z=0,
            adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertGreater(candidate.entry_fee_per_share, 0.0)
        self.assertGreater(candidate.exit_fee_per_share, 0.0)
        expected = (
            candidate.expected_exit_price
            - candidate.entry_price
            - candidate.entry_fee_per_share
            - candidate.exit_fee_per_share
        )
        self.assertAlmostEqual(candidate.net_pnl_per_share, expected)

    def test_uncertainty_adverse_and_capital_time_all_reduce_ev(self) -> None:
        clean = core.round_trip_economics(
            side="YES",
            book=self.book,
            predicted_yes_mid=0.60,
            prediction_sigma_probability=0.01,
            fee=self.zero_fee,
            horizon_seconds=3600,
            now=100,
            slippage_bps_per_leg=0,
            uncertainty_z=0,
            adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
        )
        stressed = core.round_trip_economics(
            side="YES",
            book=self.book,
            predicted_yes_mid=0.60,
            prediction_sigma_probability=0.01,
            fee=self.zero_fee,
            horizon_seconds=3600,
            now=100,
            slippage_bps_per_leg=0,
            uncertainty_z=1.5,
            adverse_markout_penalty_bps=5,
            capital_cost_bps_per_hour=2,
            max_book_age_seconds=10,
        )
        assert clean is not None and stressed is not None
        self.assertLess(stressed.net_pnl_per_share, clean.net_pnl_per_share)
        self.assertGreater(stressed.uncertainty_penalty_per_share, 0.0)
        self.assertGreater(stressed.adverse_markout_penalty_per_share, 0.0)
        self.assertGreater(stressed.capital_time_cost_per_share, 0.0)

    def test_stale_or_future_book_is_rejected(self) -> None:
        common = dict(
            side="YES",
            predicted_yes_mid=0.60,
            prediction_sigma_probability=0.0,
            fee=self.zero_fee,
            horizon_seconds=60,
            slippage_bps_per_leg=0,
            uncertainty_z=0,
            adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0,
            max_book_age_seconds=5,
        )
        self.assertIsNone(core.round_trip_economics(book=self.book, now=106, **common))
        self.assertIsNone(core.round_trip_economics(book=self.book, now=99, **common))


if __name__ == "__main__":
    unittest.main()
