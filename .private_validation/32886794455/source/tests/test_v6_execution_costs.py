from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("v6_queue_filter_test", SCRIPTS / "v6_queue_filter.py")
assert spec and spec.loader
qf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = qf
spec.loader.exec_module(qf)


class V6ExecutionMeasurementTest(unittest.TestCase):
    def test_explicit_fee_disabled_never_falls_back(self) -> None:
        details = qf.resolve_fee_details(
            {"conditionId": "abc", "feesEnabled": False},
            "https://clob.test",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no lookup expected")),
        )
        self.assertFalse(details.enabled)
        self.assertEqual(qf.fee_amount(100.0, 0.5, details, taker=True), 0.0)

    def test_clob_fee_descriptor_beats_conservative_fallback(self) -> None:
        calls = []

        def request(url, *_args):
            calls.append(url)
            return {"feesEnabled": True, "fd": {"rate": 0.04, "exponent": 1.0, "takerOnly": True}}

        details = qf.resolve_fee_details({"conditionId": "abc"}, "https://clob.test", request)
        self.assertEqual(details.source, "clob:fee_schedule")
        self.assertAlmostEqual(details.rate, 0.04)
        self.assertEqual(calls, ["https://clob.test/clob-markets/abc"])

    def test_clob_v2_compact_fee_descriptor_is_not_mistaken_for_fallback(self) -> None:
        details = qf.resolve_fee_details(
            {"conditionId": "abc"},
            "https://clob.test",
            lambda *_args: {"feesEnabled": True, "fd": {"r": 0.02, "e": 2, "to": True}},
        )
        self.assertEqual(details.source, "clob:fee_schedule")
        self.assertAlmostEqual(details.rate, 0.02)
        self.assertAlmostEqual(details.exponent, 2.0)
        self.assertTrue(details.taker_only)
        self.assertAlmostEqual(qf.fee_amount(100.0, 0.5, details), 0.125)

    def test_taker_fee_is_level_specific_and_maker_fee_is_zero(self) -> None:
        details = qf.FeeDetails(True, 0.07, 1.0, True, "test")
        self.assertEqual(qf.fee_amount(100.0, 0.5, details, taker=False), 0.0)
        fill = qf.walk_book_for_shares(
            [(0.50, 5.0), (0.60, 5.0)],
            8.0,
            details,
            buy=True,
            slippage_bps=0.0,
            require_full=True,
        )
        self.assertIsNotNone(fill)
        assert fill is not None
        expected = qf.fee_amount(5.0, 0.50, details) + qf.fee_amount(3.0, 0.60, details)
        self.assertAlmostEqual(fill.fee, expected)
        self.assertGreater(fill.raw_vwap, 0.50)

    def test_depth_walk_rejects_fake_full_fill_and_supports_partial_exit(self) -> None:
        details = qf.FeeDetails(False, 0.0, 1.0, True, "test")
        self.assertIsNone(qf.walk_book_for_shares([(0.50, 2.0)], 5.0, details, buy=True, require_full=True))
        partial = qf.walk_book_for_shares([(0.49, 2.0)], 5.0, details, buy=False, require_full=False)
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial.filled_shares, 2.0)
        self.assertFalse(partial.complete)

    def test_exploration_round_trip_prices_both_fees_spread_and_full_exit_depth(self) -> None:
        fallback = qf.FeeDetails(True, 0.07, 1.0, True, "fallback:conservative")
        actual = qf.FeeDetails(True, 0.02, 2.0, True, "clob:fee_schedule")
        expensive = qf.paper_round_trip_for_cash(
            [(0.36, 100.0)], [(0.35, 100.0)], 5.0, fallback, slippage_bps=5.0
        )
        corrected = qf.paper_round_trip_for_cash(
            [(0.36, 100.0)], [(0.35, 100.0)], 5.0, actual, slippage_bps=5.0
        )
        cheap = qf.paper_round_trip_for_cash(
            [(0.80, 100.0)], [(0.79, 100.0)], 5.0,
            qf.FeeDetails(False, 0.0, 1.0, True, "market:fees_disabled"), slippage_bps=5.0
        )
        self.assertIsNotNone(expensive)
        self.assertIsNotNone(corrected)
        self.assertIsNotNone(cheap)
        assert expensive is not None and corrected is not None and cheap is not None
        self.assertGreater(expensive.cost_fraction, 0.10)
        self.assertGreater(corrected.cost_fraction, 0.03)
        self.assertLess(corrected.cost_fraction, expensive.cost_fraction)
        self.assertLess(cheap.cost_fraction, 0.03)
        self.assertIsNone(qf.paper_round_trip_for_cash(
            [(0.80, 100.0)], [(0.79, 1.0)], 5.0, actual, slippage_bps=5.0
        ))

    def test_depth_aware_micro_and_tiny_exploration_route_only_to_paper_v6(self) -> None:
        loop = (SCRIPTS / "paper_v6_loop.sh").read_text()
        self.assertIn("scripts/v6_queue_filter.py micro", loop)
        self.assertNotIn("python3 scripts/v6_micro_taker.py", loop)
        self.assertIn("scripts/v6_queue_filter.py hard", loop)
        self.assertIn("--leg-latency-ms 100", loop)
        self.assertIn("--completion-threshold 0.75", loop)
        self.assertIn("--trade-tape \"$RUN_ROOT/trade_tape.csv\"", loop)
        self.assertIn("--exploration-enabled", loop)
        self.assertIn("EXPLORATION_MAX_TRADE", loop)
        self.assertIn("EXPLORATION_HOLD", loop)

    def test_exploration_activity_and_depth_strata_do_not_invent_a_taker_queue(self) -> None:
        self.assertEqual(qf._activity_bucket(1), "low")
        self.assertEqual(qf._activity_bucket(3), "active")
        self.assertEqual(qf._activity_bucket(10), "hot")
        self.assertEqual(qf._queue_depth_bucket(4.9), "thin")
        self.assertEqual(qf._queue_depth_bucket(5.0), "normal")
        self.assertEqual(qf._queue_depth_bucket(25.0), "deep")
        self.assertIn("Takers do not stand in a", qf._recent_trade_activity.__doc__)

    def test_exploration_skips_second_side_without_stopping_later_markets(self) -> None:
        now = 1_700_000_000

        def market(market_id: str) -> object:
            return qf.micro_legacy.Market(
                {
                    "id": market_id,
                    "conditionId": f"condition-{market_id}",
                    "eventId": f"event-{market_id}",
                    "slug": market_id,
                    "clobTokenIds": json.dumps([f"{market_id}-yes", f"{market_id}-no"]),
                    "outcomes": json.dumps(["Yes", "No"]),
                    "liquidityNum": 100,
                }
            )

        def book(token: str, depth: float, bid: float = 0.54, ask: float = 0.55) -> object:
            return qf.micro_legacy.Book(
                {
                    "asset_id": token,
                    "tick_size": "0.01",
                    "min_order_size": "1",
                    "bids": [{"price": str(bid), "size": str(depth)}],
                    "asks": [{"price": str(ask), "size": str(depth)}],
                }
            )

        markets = [market("m1"), market("m2"), market("m3")]
        books = {}
        for item in markets:
            for token in (item.yes, item.no):
                if item.id == "m3":
                    books[token] = book(token, 100.0, 0.998, 0.999)
                else:
                    books[token] = book(token, 30.0 if item.id == "m1" else 4.0)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "gamma_url": "https://gamma.test",
                        "clob_url": "https://clob.test",
                        "starting_capital": 100.0,
                    }
                ),
                encoding="utf-8",
            )
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=qf.TRADE_TAPE_FIELDS)
                writer.writeheader()
                for item, repetitions in ((markets[0], 2), (markets[1], 1), (markets[2], 10)):
                    for token in (item.yes, item.no):
                        for index in range(repetitions):
                            writer.writerow(
                                {
                                    "timestamp": now - 1,
                                    "asset_id": token,
                                    "price": 0.5,
                                    "size": 1,
                                    "transaction_hash": f"{token}-{index}",
                                }
                            )

            argv = [
                "v6_queue_filter.py",
                "--config", str(config),
                "--run-dir", str(root / "micro_taker"),
                "--trade-tape", str(tape),
                "--min-edge", "0.99",
                "--max-positions", "3",
                "--exploration-enabled",
                "--exploration-max-trade-usd", "1",
                "--exploration-max-positions", "2",
                "--exploration-max-opens-per-hour", "6",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(qf.time, "time", return_value=now), mock.patch.object(
                qf.micro_legacy, "discover", return_value=markets
            ), mock.patch.object(qf.micro_legacy, "fetch_books", return_value=books), mock.patch.object(
                qf, "_micro_fee", return_value=qf.FeeDetails(False, 0.0, 1.0, True, "test")
            ):
                self.assertEqual(qf._micro_main(), 0)

            status = json.loads((root / "micro_taker" / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["exploration"]["opened_last_tick"], 2)
            self.assertEqual(set(status["positions"]), {"m1", "m2"})
            self.assertGreaterEqual(status["exploration"]["economic_rejections_last_tick"]["entry_price_band"], 2)
            self.assertEqual(status["exploration"]["max_round_trip_cost_fraction"], 0.03)
            self.assertEqual(
                status["exploration"]["ranking"],
                "lowest_conservative_round_trip_cost_then_activity_volume",
            )

    def test_registered_smoke_uses_realistic_execution_probe(self) -> None:
        workflow = (ROOT / ".github/workflows/v6-research-smoke.yml").read_text()
        self.assertIn("scripts/v6_queue_filter.py self-test", workflow)
        self.assertIn("scripts/v6_queue_filter.py hard", workflow)
        self.assertIn("scripts/v6_queue_filter.py micro", workflow)
        self.assertIn("--leg-latency-ms 100", workflow)


if __name__ == "__main__":
    unittest.main()
