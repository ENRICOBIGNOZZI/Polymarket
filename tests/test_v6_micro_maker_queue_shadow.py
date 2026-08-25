from __future__ import annotations

import csv
import importlib.util
import math
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v6_micro_maker_queue_shadow", ROOT / "scripts" / "v6_micro_maker_queue_shadow.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


def book(*, bid: float, ask: float, tick: float, bid_size: float = 1000.0, min_order: float = 5.0):
    return {
        "tick_size": str(tick),
        "min_order_size": str(min_order),
        "bids": [{"price": str(bid), "size": str(bid_size)}],
        "asks": [{"price": str(ask), "size": "1000"}],
    }


def order(*, price: float = 0.10, shares: float = 100.0, queue: float = 1000.0):
    return {
        "market_id": "m1",
        "event_id": "e1",
        "condition_id": "c1",
        "slug": "example",
        "side": "YES",
        "token_id": "t1",
        "limit_price": str(price),
        "remaining_shares": str(shares),
        "queue_ahead": str(queue),
        "created_ts": "100",
        "game_start_ts": "0",
        "timed_sports": "0",
        "last_trade_ts": "100",
        "last_trade_keys": "|old|",
        "fee_rate": "0",
        "fee_exponent": "1",
        "fee_taker_only": "1",
    }


class QueueAwareMakerShadowTest(unittest.TestCase):
    def test_keeps_reasonable_queue(self):
        d = MOD.decide(
            order(queue=200.0), 0.003, book(bid=0.10, ask=0.12, tick=0.01),
            min_edge=0.0002, max_queue_ratio=50.0, max_improve_ticks=1,
        )
        self.assertEqual(d["action"], "KEEP_JOIN")
        self.assertAlmostEqual(d["queue_ratio"], 2.0)

    def test_reprices_only_when_edge_can_pay_for_tick(self):
        d = MOD.decide(
            order(queue=10000.0), 0.015, book(bid=0.10, ask=0.12, tick=0.01),
            min_edge=0.0002, max_queue_ratio=50.0, max_improve_ticks=1,
        )
        self.assertEqual(d["action"], "REPRICE_INSIDE_SHADOW")
        self.assertAlmostEqual(d["new_price"], 0.11)
        self.assertAlmostEqual(d["edge_after_price"], 0.005)
        self.assertLessEqual(d["new_price"], 0.12 - 0.25 * 0.01)
        self.assertAlmostEqual(d["new_shares"] * d["new_price"], 10.0, places=8)

    def test_dead_queue_is_cancelled_when_tick_consumes_edge(self):
        d = MOD.decide(
            order(queue=10000.0), 0.003, book(bid=0.10, ask=0.12, tick=0.01),
            min_edge=0.0002, max_queue_ratio=50.0, max_improve_ticks=1,
        )
        self.assertEqual(d["action"], "CANCEL_DEAD_QUEUE_SHADOW")

    def test_never_crosses_the_ask(self):
        d = MOD.decide(
            order(queue=10000.0), 0.020, book(bid=0.10, ask=0.11, tick=0.01),
            min_edge=0.0002, max_queue_ratio=50.0, max_improve_ticks=1,
        )
        self.assertEqual(d["action"], "CANCEL_DEAD_QUEUE_SHADOW")

    def test_stale_quote_is_not_repriced(self):
        d = MOD.decide(
            order(price=0.10, queue=10000.0), 0.020, book(bid=0.11, ask=0.13, tick=0.01),
            min_edge=0.0002, max_queue_ratio=50.0, max_improve_ticks=1,
        )
        self.assertEqual(d["action"], "CANCEL_STALE_SHADOW")

    def test_apply_reprice_resets_causal_tape_cursor_and_preserves_notional(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fields = list(order().keys())
            with (root / "maker_orders.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                w.writerow(order(price=0.10, shares=100.0, queue=10000.0))
            with (root / "maker_order_log.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence"])
                w.writerow([100, "POST", "m1", "example", "YES", "t1", 0.10, 100, 10000, 0.015, 0.8])
            d = MOD.decide(
                order(price=0.10, shares=100.0, queue=10000.0), 0.015,
                book(bid=0.10, ask=0.12, tick=0.01),
                min_edge=0.0002, max_queue_ratio=50.0, max_improve_ticks=1,
            )
            MOD.apply_shadow_plan(root, [d], 200)
            with (root / "maker_orders.csv").open(newline="", encoding="utf-8") as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual(row["created_ts"], "200")
            self.assertEqual(row["last_trade_ts"], "200")
            self.assertEqual(row["last_trade_keys"], "|")
            self.assertAlmostEqual(float(row["limit_price"]), 0.11)
            self.assertLessEqual(float(row["remaining_shares"]) * float(row["limit_price"]), 10.0 + 1e-9)
            self.assertEqual(float(row["queue_ahead"]), 0.0)


if __name__ == "__main__":
    unittest.main()
