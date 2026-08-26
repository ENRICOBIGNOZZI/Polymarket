from __future__ import annotations

import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v6_hf_active_maker as m
from v6_queue_filter import FeeDetails


class HfActiveMakerTest(unittest.TestCase):
    def test_late_received_trade_that_occurred_inside_ttl_can_fill(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        run = Path(td.name)
        maker = object.__new__(m.ActiveMaker)
        maker.a = SimpleNamespace(
            ttl_seconds=60,
            activity_gate=False,
            dead_queue_grace_seconds=30,
            dead_queue_pfill=0.02,
            activity_lookback_seconds=120,
        )
        maker.killed = False
        fd = FeeDetails(False, 0.0, 1.0, True, "test")
        order = m.Order("m1", "e1", "c1", "slug", "YES", "tok", 0.50, 5.0, 5.0, 100, 0.55, 0.01, 0.8, fd)
        maker.orders = {"m1": order}
        maker.positions = {}
        maker.fill_lots = []
        maker.cash = 100.0
        maker.counters = Counter()
        maker.order_log = run / "orders.csv"
        maker.fill_log = run / "fills.csv"
        maker.mark_log = run / "markouts.csv"
        maker.realized_pnl = 0.0
        maker.closed_shares = 0.0
        book = m.micro.Book(
            {
                "asset_id": "tok",
                "tick_size": "0.01",
                "min_order_size": "1",
                "bids": [{"price": "0.50", "size": "5"}],
                "asks": [{"price": "0.52", "size": "20"}],
            }
        )
        # Event occurred at t=120 within [100,160], but was locally received after
        # TTL. At processing t=170 it is legitimate event-time fill evidence.
        tape = [
            {
                "timestamp": "120",
                "received_ms": "165000",
                "condition_id": "c1",
                "asset_id": "tok",
                "side": "SELL",
                "price": "0.50",
                "size": "10",
                "transaction_hash": "tx1",
            }
        ]
        maker.process_orders(170, 170000, {"tok": book}, tape, {})
        self.assertNotIn("m1", maker.orders)
        self.assertIn("m1", maker.positions)
        self.assertAlmostEqual(maker.positions["m1"].shares, 5.0)
        self.assertEqual(maker.counters["BUY_MAKER"], 1)

    def test_hard_paper_envelope_is_encoded(self):
        text = (ROOT / "scripts" / "v6_hf_active_maker.py").read_text(encoding="utf-8")
        self.assertIn('default=1000', text)
        self.assertIn('default=2.0', text)
        self.assertIn('default=0.00005', text)
        self.assertIn('default=125.0', text)
        self.assertIn('default=0.05', text)
        self.assertIn('default=0.15', text)
        self.assertIn('default=0.70', text)
        self.assertIn('if a.max_drawdown > 0.15', text)
        self.assertIn('"authenticated_execution": False', text)
        self.assertIn('horizons = (45, 60, 300)', text)
        self.assertIn('fill_ev <= 0.0', text)


if __name__ == "__main__":
    unittest.main()
