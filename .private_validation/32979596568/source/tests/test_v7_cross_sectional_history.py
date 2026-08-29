from __future__ import annotations

import importlib.util
import sys
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_cross_sectional_rank as base
import v7_cross_sectional_history as history


@dataclass(frozen=True)
class Market:
    market_id: str
    yes_token: str


class ParallelHistoryTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original = base.request_json

    def tearDown(self) -> None:
        base.request_json = self.original

    def test_parallel_transport_preserves_panel_and_bounds_concurrency(self) -> None:
        lock = threading.Lock()
        active = 0
        peak = 0

        def fake_request(_url, payload=None, timeout=20):
            nonlocal active, peak
            self.assertIsInstance(payload, dict)
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            rows = {}
            for token in payload["markets"]:
                rows[token] = [
                    {"t": payload["start_ts"] + 60, "p": "0.40"},
                    {"t": payload["end_ts"] - 60, "p": "0.60"},
                ]
            with lock:
                active -= 1
            return {"history": rows}

        base.request_json = fake_request
        old_window = base.HISTORY_WINDOW_SECONDS
        old_batch = base.HISTORY_BATCH_SIZE
        try:
            base.HISTORY_WINDOW_SECONDS = 100
            base.HISTORY_BATCH_SIZE = 2
            markets = [Market(str(i), f"t{i}") for i in range(6)]
            panel, failures = history.fetch_histories(
                "https://clob.test",
                markets,
                0,
                300,
                1,
                max_workers=3,
            )
        finally:
            base.HISTORY_WINDOW_SECONDS = old_window
            base.HISTORY_BATCH_SIZE = old_batch
        self.assertEqual(failures, [])
        self.assertEqual(set(panel), {str(i) for i in range(6)})
        self.assertGreaterEqual(peak, 2)
        self.assertLessEqual(peak, 3)
        self.assertTrue(all(len(series) >= 3 for series in panel.values()))

    def test_empty_or_reversed_range_is_noop(self) -> None:
        markets = [Market("a", "ta")]
        self.assertEqual(history.fetch_histories("x", markets, 10, 10, 1), ({}, []))
        self.assertEqual(history.fetch_histories("x", markets, 11, 10, 1), ({}, []))


if __name__ == "__main__":
    unittest.main()
