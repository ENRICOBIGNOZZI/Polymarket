from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hf_active_token_gate", ROOT / "scripts" / "hf_active_token_gate.py")
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


class HfActiveTokenGateTest(unittest.TestCase):
    def write_tape(self, rows):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "trade_tape.csv"
        fields = [
            "timestamp", "received_ms", "lag_ms", "condition_id", "asset_id", "outcome", "side",
            "price", "size", "transaction_hash", "slug", "event_slug",
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for row in rows:
                base = {k: "" for k in fields}
                base.update(row)
                w.writerow(base)
        return td, path

    def test_receive_time_is_causal_gate_and_event_time_is_recency_clock(self):
        as_of_ms = 1_000_000
        as_of_s = as_of_ms // 1000
        rows = [
            # Balanced, recent and already received -> eligible.
            {"timestamp": as_of_s - 20, "received_ms": as_of_ms - 1000, "asset_id": "good", "side": "SELL", "size": 10},
            {"timestamp": as_of_s - 10, "received_ms": as_of_ms - 500, "asset_id": "good", "side": "BUY", "size": 8},
            {"timestamp": as_of_s - 5, "received_ms": as_of_ms - 100, "asset_id": "good", "side": "SELL", "size": 7},
            # Old event received just now must remain stale economically.
            {"timestamp": as_of_s - 200, "received_ms": as_of_ms - 10, "asset_id": "old", "side": "SELL", "size": 100},
            {"timestamp": as_of_s - 190, "received_ms": as_of_ms - 10, "asset_id": "old", "side": "BUY", "size": 100},
            # Recent event not yet locally received is unavailable at decision time.
            {"timestamp": as_of_s - 3, "received_ms": as_of_ms + 1, "asset_id": "future_receive", "side": "SELL", "size": 20},
            {"timestamp": as_of_s - 2, "received_ms": as_of_ms + 2, "asset_id": "future_receive", "side": "BUY", "size": 20},
        ]
        td, tape = self.write_tape(rows)
        self.addCleanup(td.cleanup)
        stats = MOD.load_recent_stats(tape, as_of_ms, 120)
        self.assertIn("good", stats)
        self.assertNotIn("old", stats)
        self.assertNotIn("future_receive", stats)

    def test_toxic_one_sided_sell_burst_is_not_admitted(self):
        as_of_ms = 2_000_000
        as_of_s = as_of_ms // 1000
        rows = [
            {"timestamp": as_of_s - 20, "received_ms": as_of_ms - 1000, "asset_id": "toxic", "side": "SELL", "size": 60},
            {"timestamp": as_of_s - 10, "received_ms": as_of_ms - 500, "asset_id": "toxic", "side": "SELL", "size": 50},
            {"timestamp": as_of_s - 5, "received_ms": as_of_ms - 100, "asset_id": "toxic", "side": "BUY", "size": 1},
            {"timestamp": as_of_s - 20, "received_ms": as_of_ms - 1000, "asset_id": "balanced", "side": "SELL", "size": 20},
            {"timestamp": as_of_s - 10, "received_ms": as_of_ms - 500, "asset_id": "balanced", "side": "BUY", "size": 15},
            {"timestamp": as_of_s - 5, "received_ms": as_of_ms - 100, "asset_id": "balanced", "side": "SELL", "size": 10},
        ]
        td, tape = self.write_tape(rows)
        self.addCleanup(td.cleanup)
        stats = MOD.load_recent_stats(tape, as_of_ms, 120)
        chosen = MOD.choose_tokens(
            stats,
            min_trades=2,
            min_sell_shares=5.0,
            min_sell_share=0.05,
            max_sell_share=0.80,
            max_tokens=250,
        )
        ids = [x.token_id for x in chosen]
        self.assertIn("balanced", ids)
        self.assertNotIn("toxic", ids)

    def test_fill_relevant_sell_flow_is_required(self):
        as_of_ms = 3_000_000
        as_of_s = as_of_ms // 1000
        rows = [
            {"timestamp": as_of_s - 20, "received_ms": as_of_ms - 1000, "asset_id": "buy_only", "side": "BUY", "size": 50},
            {"timestamp": as_of_s - 10, "received_ms": as_of_ms - 500, "asset_id": "buy_only", "side": "BUY", "size": 40},
            {"timestamp": as_of_s - 20, "received_ms": as_of_ms - 1000, "asset_id": "sell_ok", "side": "SELL", "size": 8},
            {"timestamp": as_of_s - 10, "received_ms": as_of_ms - 500, "asset_id": "sell_ok", "side": "BUY", "size": 12},
        ]
        td, tape = self.write_tape(rows)
        self.addCleanup(td.cleanup)
        stats = MOD.load_recent_stats(tape, as_of_ms, 120)
        chosen = MOD.choose_tokens(
            stats,
            min_trades=2,
            min_sell_shares=5.0,
            min_sell_share=0.05,
            max_sell_share=0.80,
            max_tokens=250,
        )
        ids = [x.token_id for x in chosen]
        self.assertIn("sell_ok", ids)
        self.assertNotIn("buy_only", ids)


if __name__ == "__main__":
    unittest.main()
