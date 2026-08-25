#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location("v6_queue_filter", SCRIPTS / "v6_queue_filter.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)

MICRO_SPEC = importlib.util.spec_from_file_location("v6_micro_taker_under_test", SCRIPTS / "v6_micro_taker.py")
assert MICRO_SPEC and MICRO_SPEC.loader
MICRO = importlib.util.module_from_spec(MICRO_SPEC)
sys.modules[MICRO_SPEC.name] = MICRO
MICRO_SPEC.loader.exec_module(MICRO)


def rows() -> list[dict[str, str]]:
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


def token_map() -> dict[tuple[str, str], str]:
    return {
        ("3535881", "YES"): "t1",
        ("3535883", "YES"): "t2",
        ("3535882", "YES"): "t3",
    }


def books(queue_sizes: list[float]) -> dict[str, dict]:
    prices = [0.33, 0.36, 0.29]
    return {
        f"t{i+1}": {
            "bids": [(price, queue_sizes[i])],
            "asks": [(price + 0.02, 1000.0)],
            "best_bid": price,
            "best_ask": price + 0.02,
            "min_order": 5.0,
        }
        for i, price in enumerate(prices)
    }


def micro_market(**extra) -> dict:
    raw = {
        "id": "1",
        "conditionId": "cond-1",
        "eventId": "event-1",
        "slug": "test-market",
        "clobTokenIds": ["yes-token", "no-token"],
        "outcomes": ["Yes", "No"],
        "liquidityNum": 1000,
    }
    raw.update(extra)
    return raw


class V6QueueFilterTests(unittest.TestCase):
    def test_rejects_observed_graph_bundle_with_extreme_queue(self) -> None:
        ok, reason, ratio, diagnostics = MOD.evaluate_bundle(
            rows(), token_map(), books([30849.0, 40035.5, 38851.4]), 50.0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "queue_ratio")
        self.assertGreater(ratio, 500.0)
        self.assertEqual(len(diagnostics), 3)

    def test_accepts_same_economics_when_queue_is_manageable(self) -> None:
        ok, reason, ratio, diagnostics = MOD.evaluate_bundle(
            rows(), token_map(), books([300.0, 250.0, 200.0]), 50.0
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "accepted")
        self.assertLess(ratio, 50.0)
        self.assertEqual(len(diagnostics), 3)

    def test_stale_passive_limit_fails_closed(self) -> None:
        b = books([10.0, 10.0, 10.0])
        b["t1"]["best_bid"] = 0.34
        b["t1"]["bids"] = [(0.34, 10.0), (0.33, 10.0)]
        ok, reason, _, _ = MOD.evaluate_bundle(rows(), token_map(), b, 50.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_limit")

    def test_v6_loop_broadens_taker_without_lowering_edge(self) -> None:
        text = (SCRIPTS / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn('--markets 500 --min-liquidity 25', text)
        self.assertIn('--min-edge 0.00030', text)
        self.assertIn('scripts/v6_queue_filter.py', text)
        self.assertIn('V6_MAX_QUEUE_RATIO:-50', text)
        self.assertIn('scripts/v6_execution_diagnostics.py', text)

    def test_micro_explicit_zero_fee_market_is_not_charged_fallback_curve(self) -> None:
        market = MICRO.Market(micro_market(feesEnabled=False))
        self.assertEqual(market.fee_rate, 0.0)
        self.assertEqual(market.fee_source, "gamma_disabled")
        self.assertEqual(MICRO.fee_per_share(0.5, market.fee_rate, market.fee_exp), 0.0)

    def test_micro_gamma_fee_schedule_remains_authoritative(self) -> None:
        market = MICRO.Market(
            micro_market(feesEnabled=True, feeSchedule={"rate": 0.07, "exponent": 1.0, "takerOnly": True})
        )
        self.assertAlmostEqual(market.fee_rate, 0.07)
        self.assertEqual(market.fee_source, "gamma_schedule")

    def test_micro_unknown_fee_uses_clob_fee_rate_before_conservative_fallback(self) -> None:
        market = MICRO.Market(micro_market())
        self.assertTrue(math.isnan(market.fee_rate))
        original = MICRO.request_json
        calls: list[str] = []

        def fake_request(url: str, payload=None, timeout: int = 20):
            calls.append(url)
            if "/clob-markets/" in url:
                return {}
            if "/fee-rate?" in url:
                return {"base_fee": 0}
            raise AssertionError(url)

        MICRO.request_json = fake_request
        try:
            stats = MICRO.resolve_fee_details("https://clob.example", [market])
        finally:
            MICRO.request_json = original
        self.assertEqual(market.fee_rate, 0.0)
        self.assertEqual(market.fee_source, "clob_fee_rate")
        self.assertEqual(stats["conservative_fallback"], 0)
        self.assertEqual(stats["resolved_without_fallback"], 1)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
