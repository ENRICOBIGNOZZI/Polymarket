#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
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
            {"topic": "crypto_prices_twap_sixty", "payload": [
                {"symbol": "btc/usd", "timestamp": 1788019000123, "value": 77001.25,
                 "window_s": 60},
            ]},
            {"topic": "crypto_prices", "payload": {
                "symbol": "BTCUSDT", "timestamp": 1788019001123, "value": "77002.5"
            }},
        ]
        rows = list(module.observations(envelope))
        self.assertEqual([row["topic"] for row in rows], [
            "crypto_prices_twap_sixty", "crypto_prices"
        ])
        self.assertEqual(rows[0]["timestamp_ms"], 1788019000123)
        self.assertEqual(rows[0]["window_seconds"], 60)
        self.assertEqual(rows[1]["price"], 77002.5)

    def test_invalid_or_unrelated_values_fail_closed(self) -> None:
        self.assertEqual(list(module.observations({"topic": "comments", "value": 1})), [])
        self.assertEqual(list(module.observations({
            "topic": "crypto_prices_twap_sixty", "value": "nan", "timestamp": 1,
            "window_s": 60
        })), [])

    def test_oracle_observation_requires_explicit_sixty_second_window(self) -> None:
        base = {"topic": module.ORACLE_TOPIC, "symbol": "btc/usd",
                "value": 77001.25, "timestamp": 1788019000000}
        self.assertEqual(list(module.observations(base)), [])
        self.assertEqual(list(module.observations({**base, "window_s": 30})), [])
        rows = list(module.observations({**base, "windowSeconds": 60}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["window_seconds"], 60)

    def test_boundary_reference_is_causal_bounded_and_prefers_latest(self) -> None:
        history = {
            7_000: {"timestamp_ms": 7_000, "price": 70.0},
            9_000: {"timestamp_ms": 9_000, "price": 90.0},
            11_000: {"timestamp_ms": 11_000, "price": 110.0},
        }
        self.assertEqual(module.boundary_reference(history, 10_000)["timestamp_ms"], 9_000)
        self.assertIsNone(module.boundary_reference({7_000: history[7_000]}, 10_000))
        self.assertIsNone(module.boundary_reference({11_000: history[11_000]}, 10_000))
        exact = {10_000: {"timestamp_ms": 10_000, "price": 100.0}, **history}
        self.assertEqual(module.boundary_reference(exact, 10_000)["timestamp_ms"], 10_000)

    def test_full_accuracy_oracle_value_is_preserved_as_decimal(self) -> None:
        rows = list(module.observations({
            "topic": module.ORACLE_TOPIC, "symbol": "btc/usd", "timestamp": 10_000,
            "value": 65000.5, "full_accuracy_value": "65000500000000000000000", "window_s": 60,
        }))
        self.assertEqual(rows[0]["price_decimal"], "65000.5")

    def test_runtime_contract_uses_official_twap_topic_and_application_heartbeat(self) -> None:
        source = (ROOT / "scripts" / "v7_rtds_external_fair_monitor.py").read_text()
        self.assertIn('ORACLE_TOPIC = "crypto_prices_twap_sixty"', source)
        self.assertIn('send_frame(stream, 0x1, b"PING")', source)

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
            self.assertIn("FAIR_VALUE_INVALID", status["blockers"])

    def test_launcher_uses_public_monitor_without_private_binding(self) -> None:
        launcher = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
        self.assertIn("v7_rtds_external_fair_monitor.py", launcher)
        self.assertNotIn("binding_not_configured_contracts_quarantined", launcher)

    def test_verified_contract_reference_and_multi_venue_produce_valid_fair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = int(time.time())
            start = now - now % 300
            # Keep the live observation strictly after the contract boundary.
            # At an exact five-minute wall-clock boundary, using ``now`` here
            # would accidentally turn this fallback test into an exact-match
            # test and make the assertion depend on test start time.
            live_timestamp_ms = max(now * 1000, start * 1000 + 1000)
            universe = root / "universe.json"
            universe.write_text(json.dumps({"markets": [{
                "market_id": "m1", "condition_id": "c1", "event_ids": ["e1"],
                "slug": f"btc-updown-5m-{start}",
                "question": "Bitcoin Up or Down - test",
                "description": "This market will resolve to \"Up\" if the time-weighted average price (TWAP) of Bitcoin, generated by Chainlink, of the time range specified in the title is greater than or equal to the price at the beginning of that range. Otherwise, it will resolve to \"Down\". The resolution source for this market is information from Chainlink, specifically the BTC/USD TWAP data stream available at https://data.chain.link/streams/btc-usd-twap-60s-streams. Please note that this market is about the price according to the TWAP Chainlink data stream for the asset pair BTC/USD, not according to any other sources or spot markets.",
                "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
                "clob_token_ids": ["yes", "no"], "outcomes": ["Up", "Down"],
                "accepting_orders": True, "midpoint": 0.55,
                "fee_schedule": {"rate": 0.07, "exponent": 1, "takerOnly": True},
            }]}))
            venues = root / "venues.json"
            venues.write_text(json.dumps({
                "code_sha": "a" * 40, "timestamp_ns": time.time_ns(), "valid": True,
                "fresh_venue_count": 3, "composite_price": 77020.0,
                "composite_microprice": 77020.0, "dispersion_bps": 1.0,
            }))
            monitor = module.Monitor(
                root / "external", "a" * 40, universe_path=universe,
                approvals_path=ROOT / "config" / "v7_external_fair_rule_approvals.json",
                external_venues_path=venues,
            )
            monitor.connection_epoch = 1
            monitor.ingest({"topic": module.ORACLE_TOPIC, "symbol": "btc/usd",
                            "price": 77000.0, "price_decimal": "77000.0",
                            "timestamp_ms": start * 1000 - 1000})
            monitor.ingest({"topic": module.ORACLE_TOPIC, "symbol": "btc/usd",
                            "price": 77010.0, "timestamp_ms": live_timestamp_ms})
            monitor.ingest({"topic": module.EXTERNAL_TOPIC, "symbol": "btcusdt",
                            "price": 77019.0, "timestamp_ms": live_timestamp_ms})
            monitor.publish()
            status = json.loads((root / "external" / "status.json").read_text())
            self.assertTrue(status["contract"]["verified"])
            self.assertTrue(status["contract"]["rules_hash_recognized"])
            self.assertTrue(status["settlement_reference"]["valid"])
            self.assertTrue(status["settlement_reference"]["boundary_fallback"])
            self.assertEqual(status["settlement_reference"]["boundary_gap_ms"], 1000)
            self.assertEqual(status["settlement_reference"]["observation_timestamp_ms"], start * 1000 - 1000)
            self.assertTrue(status["external"]["healthy"])
            self.assertTrue(status["fair"]["valid"])
            self.assertEqual(status["blockers"], ["OMS_EXTERNAL_FAIR_ROUTING_NOT_RUNNING"])

            (root / "external" / "paper_router_status.json").write_text(json.dumps({
                "schema": "polymarket_v7_external_fair_paper_router_v1",
                "timestamp": int(time.time()), "code_sha": "a" * 40, "state": "RUNNING",
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "execution_authority": "PAPER_EXECUTION_OWNER",
                "order_submission_enabled": True, "economic_confidence": "MORE_EVIDENCE_REQUIRED",
                "killed": False,
                "actions": {"TAKE": 2, "NOTHING": 3}, "realized_pnl": 0.0, "blocker": "",
            }))
            monitor.publish()
            active = json.loads((root / "external" / "status.json").read_text())
            self.assertEqual(active["state"], "FULL_FAIR_PAPER_OPERATIONAL")
            self.assertEqual(active["execution_authority"], "PAPER_EXECUTION_OWNER")
            self.assertEqual(active["blockers"], [])
            self.assertEqual(active["actions"]["TAKE"], 2)


if __name__ == "__main__":
    unittest.main()
