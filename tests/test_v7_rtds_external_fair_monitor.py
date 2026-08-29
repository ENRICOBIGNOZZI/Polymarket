#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "rtds_monitor", ROOT / "scripts" / "v7_rtds_external_fair_monitor.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RtdsExternalFairMonitorTests(unittest.TestCase):
    def test_observations_decode_history_and_live_envelopes(self) -> None:
        envelope = [
            {"topic": "crypto_prices_chainlink", "payload": [
                {"symbol": "btc/usd", "timestamp": 1788019000123, "value": 77001.25},
            ]},
            {"topic": "crypto_prices", "payload": {
                "symbol": "BTCUSDT", "timestamp": 1788019001123, "value": "77002.5"
            }},
        ]
        rows = list(module.observations(envelope))
        self.assertEqual([row["topic"] for row in rows], [
            "crypto_prices_chainlink", "crypto_prices"
        ])
        self.assertEqual(rows[0]["timestamp_ms"], 1788019000123)
        self.assertEqual(rows[1]["price"], 77002.5)

    def test_invalid_or_unrelated_values_fail_closed(self) -> None:
        self.assertEqual(list(module.observations({"topic": "comments", "value": 1})), [])
        self.assertEqual(list(module.observations({
            "topic": "crypto_prices_chainlink", "value": "nan", "timestamp": 1
        })), [])

    def test_status_reports_inputs_but_never_fake_fair_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = module.Monitor(root, "a" * 40)
            monitor.connection_epoch = 1
            monitor.ingest({"topic": module.ORACLE_TOPIC, "symbol": "btc/usd",
                            "price": 77000.0, "timestamp_ms": 1788019000000})
            monitor.ingest({"topic": module.EXTERNAL_TOPIC, "symbol": "btcusdt",
                            "price": 77001.0, "timestamp_ms": 1788019000001})
            monitor.publish()
            status = json.loads((root / "status.json").read_text())
            self.assertTrue(status["oracle"]["healthy"])
            self.assertEqual(status["external"]["fresh_venue_count"], 1)
            self.assertFalse(status["external"]["healthy"])
            self.assertFalse(status["fair"]["valid"])
            self.assertEqual(status["execution_authority"], "SHADOW_ZERO_AUTHORITY")
            self.assertFalse(status["real_order_submission"])
            self.assertIn("FAIR_VALUE_RUNTIME_NOT_RUNNING", status["blockers"])

    def test_launcher_uses_public_monitor_without_private_binding(self) -> None:
        launcher = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
        self.assertIn("v7_rtds_external_fair_monitor.py", launcher)
        self.assertNotIn("binding_not_configured_contracts_quarantined", launcher)


if __name__ == "__main__":
    unittest.main()
