#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "lf_resolution_labels", SCRIPTS / "lf_resolution_labels.py"
)
assert SPEC and SPEC.loader
labels = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = labels
SPEC.loader.exec_module(labels)


class LFResolutionLabelTests(unittest.TestCase):
    def test_expired_unresolved_markets_are_detected_and_backfilled(self) -> None:
        now = 1_900_000_000
        observations = [
            {
                "observed_ts": now - 10_000,
                "market_id": "101",
                "event_id": "event-1",
                "end_ts": now - 1_000,
            },
            {
                "observed_ts": now - 9_000,
                "market_id": "102",
                "event_id": "event-2",
                "end_ts": now + 10_000,
            },
        ]
        prices = [
            {
                "observed_ts": now - 10_000,
                "market_id": "101",
                "resolved_outcome": None,
                "mid": 0.60,
            }
        ]

        inventory = labels.terminal_label_inventory(observations, prices, now)
        self.assertEqual(inventory["expired_markets"], 1)
        self.assertEqual(inventory["missing_resolution_labels"], 1)
        self.assertEqual(inventory["missing_market_ids"], ["101"])

        def resolver(market_id: str):
            self.assertEqual(market_id, "101")
            return {
                "id": "101",
                "eventId": "event-1",
                "question": "Will the test event occur?",
                "description": "Deterministic resolution fixture",
                "closed": True,
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["1","0"]',
                "bestBid": "0.999",
                "bestAsk": "0.999",
                "endDate": "2029-01-01T00:00:00Z",
                "clobTokenIds": '["yes-token","no-token"]',
            }

        merged, report = labels.backfill_resolution_labels(
            observations, prices, now=now, max_markets=10, resolver=resolver
        )
        self.assertEqual(report["labels_added"], 1)
        self.assertEqual(report["after"]["missing_resolution_labels"], 0)
        self.assertTrue(report["point_in_time_feature_history_unchanged"])
        self.assertFalse(report["production_change"])
        terminal = [row for row in merged if row.get("resolved_outcome") == 1]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["quote_provenance"], "gamma_terminal_resolution_label")

    def test_open_market_does_not_create_terminal_label(self) -> None:
        payload = {
            "id": "201",
            "eventId": "event-open",
            "question": "Will an open test event occur?",
            "closed": False,
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.6","0.4"]',
            "bestBid": "0.59",
            "bestAsk": "0.61",
            "endDate": "2030-01-01T00:00:00Z",
            "clobTokenIds": '["yes-token","no-token"]',
        }
        self.assertIsNone(labels.fetch_resolution("201", resolver=lambda _: payload))


if __name__ == "__main__":
    unittest.main()
