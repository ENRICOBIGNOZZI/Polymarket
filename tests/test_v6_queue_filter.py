#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v6_queue_filter", ROOT / "scripts" / "v6_queue_filter.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


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
        text = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn('--markets 500 --min-liquidity 25', text)
        self.assertIn('--min-edge 0.00030', text)
        self.assertIn('scripts/v6_queue_filter.py', text)
        self.assertIn('V6_MAX_QUEUE_RATIO:-50', text)
        self.assertIn('scripts/v6_execution_diagnostics.py', text)


if __name__ == "__main__":
    unittest.main()
