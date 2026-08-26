from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_micro_taker_core as core
import v7_micro_taker_data as data
import v7_micro_taker_worker as worker


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
            side="YES", book=self.book, predicted_yes_mid=0.56,
            prediction_sigma_probability=0.0, fee=self.zero_fee,
            horizon_seconds=60, now=100, slippage_bps_per_leg=0,
            uncertainty_z=0, adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0, max_book_age_seconds=10,
        )
        no = core.round_trip_economics(
            side="NO", book=self.book, predicted_yes_mid=0.44,
            prediction_sigma_probability=0.0, fee=self.zero_fee,
            horizon_seconds=60, now=100, slippage_bps_per_leg=0,
            uncertainty_z=0, adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0, max_book_age_seconds=10,
        )
        self.assertIsNotNone(yes)
        self.assertIsNotNone(no)
        assert yes is not None and no is not None
        self.assertAlmostEqual(yes.net_pnl_per_share, no.net_pnl_per_share)
        self.assertAlmostEqual(yes.net_edge, no.net_edge)

    def test_entry_only_edge_can_be_positive_while_round_trip_is_negative(self) -> None:
        fee = core.FeeSpec(True, 0.04, 1.0, True, True)
        candidate = core.round_trip_economics(
            side="YES", book=self.book, predicted_yes_mid=0.525,
            prediction_sigma_probability=0.0, fee=fee, horizon_seconds=60,
            now=100, slippage_bps_per_leg=0, uncertainty_z=0,
            adverse_markout_penalty_bps=0, capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        entry_only_edge = 0.525 - candidate.entry_price - candidate.entry_fee_per_share
        self.assertGreater(entry_only_edge, 0.0)
        self.assertLess(candidate.net_pnl_per_share, 0.0)
        selected = core.choose_side(
            book=self.book, predicted_yes_mid=0.525,
            prediction_sigma_probability=0.0, fee=fee, horizon_seconds=60,
            now=100, slippage_bps_per_leg=0, uncertainty_z=0,
            adverse_markout_penalty_bps=0, capital_cost_bps_per_hour=0,
            max_book_age_seconds=10, minimum_net_edge=0.0,
        )
        self.assertIsNone(selected)

    def test_both_taker_fees_are_charged(self) -> None:
        fee = core.FeeSpec(True, 0.04, 1.0, True, True)
        candidate = core.round_trip_economics(
            side="YES", book=self.book, predicted_yes_mid=0.60,
            prediction_sigma_probability=0.0, fee=fee, horizon_seconds=60,
            now=100, slippage_bps_per_leg=0, uncertainty_z=0,
            adverse_markout_penalty_bps=0, capital_cost_bps_per_hour=0,
            max_book_age_seconds=10,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertGreater(candidate.entry_fee_per_share, 0.0)
        self.assertGreater(candidate.exit_fee_per_share, 0.0)
        expected = candidate.expected_exit_price - candidate.entry_price - candidate.entry_fee_per_share - candidate.exit_fee_per_share
        self.assertAlmostEqual(candidate.net_pnl_per_share, expected)

    def test_uncertainty_adverse_and_capital_time_all_reduce_ev(self) -> None:
        clean = core.round_trip_economics(
            side="YES", book=self.book, predicted_yes_mid=0.60,
            prediction_sigma_probability=0.01, fee=self.zero_fee,
            horizon_seconds=3600, now=100, slippage_bps_per_leg=0,
            uncertainty_z=0, adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0, max_book_age_seconds=10,
        )
        stressed = core.round_trip_economics(
            side="YES", book=self.book, predicted_yes_mid=0.60,
            prediction_sigma_probability=0.01, fee=self.zero_fee,
            horizon_seconds=3600, now=100, slippage_bps_per_leg=0,
            uncertainty_z=1.5, adverse_markout_penalty_bps=5,
            capital_cost_bps_per_hour=2, max_book_age_seconds=10,
        )
        assert clean is not None and stressed is not None
        self.assertLess(stressed.net_pnl_per_share, clean.net_pnl_per_share)
        self.assertGreater(stressed.uncertainty_penalty_per_share, 0.0)
        self.assertGreater(stressed.adverse_markout_penalty_per_share, 0.0)
        self.assertGreater(stressed.capital_time_cost_per_share, 0.0)

    def test_stale_or_future_book_is_rejected(self) -> None:
        common = dict(
            side="YES", predicted_yes_mid=0.60, prediction_sigma_probability=0.0,
            fee=self.zero_fee, horizon_seconds=60, slippage_bps_per_leg=0,
            uncertainty_z=0, adverse_markout_penalty_bps=0,
            capital_cost_bps_per_hour=0, max_book_age_seconds=5,
        )
        self.assertIsNone(core.round_trip_economics(book=self.book, now=106, **common))
        self.assertIsNone(core.round_trip_economics(book=self.book, now=99, **common))

    def test_full_depth_vwap_requires_full_requested_quantity(self) -> None:
        levels = [(0.49, 2.0), (0.48, 3.0)]
        self.assertIsNone(worker.full_depth_vwap(levels, 6.0, buy=False))
        self.assertAlmostEqual(
            worker.full_depth_vwap(levels, 5.0, buy=False),
            (0.49 * 2.0 + 0.48 * 3.0) / 5.0,
        )


NOW = 1_800_000_000


def raw_book(token: str, timestamp: object) -> dict[str, object]:
    return {
        "asset_id": token,
        "timestamp": timestamp,
        "tick_size": "0.01",
        "min_order_size": "1",
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
    }


class MicroTakerBookFreshnessTest(unittest.TestCase):
    def test_epoch_seconds_normalizes_clob_timestamp_units(self) -> None:
        self.assertEqual(data.epoch_seconds(str(NOW)), NOW)
        self.assertEqual(data.epoch_seconds(str(NOW * 1000 + 321)), NOW)
        self.assertEqual(data.epoch_seconds(str(NOW * 1_000_000 + 321_000)), NOW)
        self.assertEqual(data.epoch_seconds("not-a-timestamp"), 0)

    def test_fetch_books_preserves_exchange_and_local_receive_times(self) -> None:
        market = SimpleNamespace(yes="yes-token", no="no-token")
        rows = [raw_book("yes-token", str((NOW - 2) * 1000)), raw_book("no-token", str((NOW - 1) * 1000))]
        with mock.patch.object(data, "request_json", return_value=rows), mock.patch.object(data.time, "time", return_value=NOW):
            books = data.fetch_books("https://clob.invalid", [market])
        self.assertEqual(books["yes-token"].exchange_ts, NOW - 2)
        self.assertEqual(books["yes-token"].received_ts, NOW)
        self.assertEqual(books["no-token"].exchange_ts, NOW - 1)
        self.assertEqual(books["no-token"].received_ts, NOW)

    def test_fresh_pair_uses_oldest_causal_timestamp(self) -> None:
        yes = data.Book(raw_book("yes", str((NOW - 2) * 1000)), received_ts=NOW)
        no = data.Book(raw_book("no", str((NOW - 1) * 1000)), received_ts=NOW)
        snapshot = worker.book_snapshot(yes, no, liquidity=1000.0, now=NOW, max_age_seconds=5)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.received_ts, NOW - 2)

    def test_stale_exchange_snapshot_is_not_rejuvenated_by_decision_time(self) -> None:
        yes = data.Book(raw_book("yes", str((NOW - 8) * 1000)), received_ts=NOW)
        no = data.Book(raw_book("no", str((NOW - 1) * 1000)), received_ts=NOW)
        self.assertIsNone(worker.book_snapshot(yes, no, liquidity=1000.0, now=NOW, max_age_seconds=5))

    def test_missing_exchange_timestamp_fails_closed(self) -> None:
        yes = data.Book(raw_book("yes", ""), received_ts=NOW)
        no = data.Book(raw_book("no", str(NOW * 1000)), received_ts=NOW)
        self.assertIsNone(worker.book_snapshot(yes, no, liquidity=1000.0, now=NOW, max_age_seconds=5))

    def test_future_exchange_or_receive_timestamp_fails_closed(self) -> None:
        future_exchange = data.Book(raw_book("yes", str((NOW + 2) * 1000)), received_ts=NOW)
        normal = data.Book(raw_book("no", str(NOW * 1000)), received_ts=NOW)
        self.assertIsNone(worker.book_snapshot(future_exchange, normal, liquidity=1000.0, now=NOW, max_age_seconds=5))
        future_receive = data.Book(raw_book("yes", str(NOW * 1000)), received_ts=NOW + 2)
        self.assertIsNone(worker.book_snapshot(future_receive, normal, liquidity=1000.0, now=NOW, max_age_seconds=5))

    def test_filtered_stale_pair_makes_open_position_unmarkable(self) -> None:
        positions = {"m": {"side": "YES", "shares": 10.0}}
        equity, unmarkable = worker.conservative_marked_equity(37.5, positions, current={})
        self.assertEqual(equity, 37.5)
        self.assertEqual(unmarkable, [{"market_id": "m", "reason": "missing_current_snapshot"}])


if __name__ == "__main__":
    unittest.main()
