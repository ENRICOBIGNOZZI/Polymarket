#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lf_v6_dual_clock_flow_audit import audit


class DualClockFlowAuditTest(unittest.TestCase):
    def test_backfilled_rows_do_not_become_one_market_time_burst(self) -> None:
        decision_ms = 1_000_000
        token = "token-a"
        # Three trades happened 100 seconds apart, but a REST backfill made all
        # three known in one response immediately before the decision.
        tape = [
            {"timestamp": "700", "received_ms": "999000", "asset_id": token, "side": "SELL", "price": "0.40", "size": "10"},
            {"timestamp": "800", "received_ms": "999000", "asset_id": token, "side": "SELL", "price": "0.40", "size": "10"},
            {"timestamp": "900", "received_ms": "999000", "asset_id": token, "side": "SELL", "price": "0.40", "size": "10"},
        ]
        legs = [{
            "bundle_id": "b",
            "market_id": "m",
            "token_id": token,
            "limit_price": "0.40",
            "arrival_ms": str(decision_ms),
            "queue_ahead": "0",
            "target_shares": "10",
        }]
        report = audit(legs, tape, lookback_seconds=400, execution_window_seconds=60)
        leg = report["bundles"][0]["legs"][0]
        self.assertEqual(leg["receive_clock_max_window_flow"], 30.0)
        self.assertEqual(leg["event_clock_max_window_flow"], 10.0)
        self.assertEqual(leg["receive_over_event_flow_inflation"], 3.0)

    def test_future_received_row_is_not_available_to_prior_flow(self) -> None:
        tape = [
            {"timestamp": "990", "received_ms": "1001000", "asset_id": "t", "side": "SELL", "price": "0.20", "size": "100"},
        ]
        legs = [{
            "bundle_id": "b",
            "market_id": "m",
            "token_id": "t",
            "limit_price": "0.20",
            "arrival_ms": "1000000",
            "queue_ahead": "10",
            "target_shares": "10",
        }]
        report = audit(legs, tape, lookback_seconds=60, execution_window_seconds=30)
        leg = report["bundles"][0]["legs"][0]
        self.assertEqual(leg["known_compatible_rows"], 0)
        self.assertEqual(leg["event_clock_max_window_flow"], 0.0)

    def test_current_contract_records_dual_clock_roles(self) -> None:
        report = audit([], [], lookback_seconds=900, execution_window_seconds=180)
        self.assertEqual(report["contract"]["causal_availability_clock"], "received_ms")
        self.assertEqual(report["contract"]["market_activity_window_clock"], "timestamp")
        self.assertIn("received after order arrival", report["contract"]["forward_fill_requires"])


if __name__ == "__main__":
    unittest.main()
