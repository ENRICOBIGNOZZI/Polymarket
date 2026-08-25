from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("v6_queue_filter_test", SCRIPTS / "v6_queue_filter.py")
assert spec and spec.loader
qf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = qf
spec.loader.exec_module(qf)

intent_spec = importlib.util.spec_from_file_location("v6_intent_queue_filter_test", SCRIPTS / "v6_intent_queue_filter.py")
assert intent_spec and intent_spec.loader
iqf = importlib.util.module_from_spec(intent_spec)
sys.modules[intent_spec.name] = iqf
intent_spec.loader.exec_module(iqf)


def graph_rows() -> list[dict[str, str]]:
    prices = [0.33, 0.36, 0.29]
    markets = ["3535881", "3535883", "3535882"]
    return [
        {
            "bundle_id": "GRAPH_RV-test",
            "strategy": "GRAPH_RV",
            "event_id": "837321",
            "created_ts": "1",
            "mode": "MAKER",
            "expected_edge": "0.02",
            "max_notional": "60",
            "market_id": market,
            "side": "YES",
            "weight": "1",
            "limit_price": str(price),
            "execution_deadline_ts": "9999999999",
            "hold_deadline_ts": "9999999999",
        }
        for market, price in zip(markets, prices)
    ]


def graph_tokens() -> dict[tuple[str, str], str]:
    return {
        ("3535881", "YES"): "t1",
        ("3535883", "YES"): "t2",
        ("3535882", "YES"): "t3",
    }


def graph_books(queue_sizes: list[float]) -> dict[str, dict]:
    prices = [0.33, 0.36, 0.29]
    return {
        f"t{i + 1}": {
            "bids": [(price, queue_sizes[i])],
            "asks": [(price + 0.02, 1000.0)],
            "best_bid": price,
            "best_ask": price + 0.02,
            "min_order": 5.0,
        }
        for i, price in enumerate(prices)
    }


class V6ExecutionCostsTest(unittest.TestCase):
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

    def test_extreme_graph_rv_queue_is_rejected(self) -> None:
        ok, reason, ratio, diagnostics = iqf.evaluate_bundle(
            graph_rows(), graph_tokens(), graph_books([30849.0, 40035.5, 38851.4]), 50.0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "queue_ratio")
        self.assertGreater(ratio, 500.0)
        self.assertEqual(len(diagnostics), 3)

    def test_manageable_graph_rv_queue_is_accepted(self) -> None:
        ok, reason, ratio, diagnostics = iqf.evaluate_bundle(
            graph_rows(), graph_tokens(), graph_books([300.0, 250.0, 200.0]), 50.0
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "accepted")
        self.assertLess(ratio, 50.0)
        self.assertEqual(len(diagnostics), 3)

    def test_stale_maker_limit_is_rejected(self) -> None:
        books = graph_books([10.0, 10.0, 10.0])
        books["t1"]["best_bid"] = 0.34
        books["t1"]["bids"] = [(0.34, 10.0), (0.33, 10.0)]
        ok, reason, _, _ = iqf.evaluate_bundle(graph_rows(), graph_tokens(), books, 50.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_limit")

    def test_v6_loop_routes_execution_through_queue_filters(self) -> None:
        loop = (SCRIPTS / "paper_v6_loop.sh").read_text()
        self.assertIn("scripts/v6_queue_filter.py self-test", loop)
        self.assertIn("scripts/v6_queue_filter.py micro", loop)
        self.assertIn("scripts/v6_queue_filter.py hard", loop)
        self.assertIn("scripts/v6_intent_queue_filter.py", loop)
        self.assertIn('V6_MAX_QUEUE_RATIO:-50', loop)
        self.assertIn("--leg-latency-ms 100", loop)
        self.assertIn("--completion-threshold 0.95", loop)
        self.assertNotIn("--completion-threshold 0.75", loop)
        self.assertIn('rm -f "$RUN_ROOT/intents_raw.csv" "$RUN_ROOT/intents.csv"', loop)


if __name__ == "__main__":
    unittest.main()
