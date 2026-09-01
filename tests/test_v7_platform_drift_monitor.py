import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_platform_drift_monitor as monitor  # noqa: E402


class PlatformDriftMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = monitor.load(ROOT / "config/v7_platform_contract.json")

    def test_registry_is_strict_and_valid(self) -> None:
        monitor.validate_registry(self.registry)

    def test_exact_archived_snapshot_is_healthy(self) -> None:
        snapshot = {"observed_at": "2026-08-31T00:00:00Z", "api": self.registry["api"],
                    "contracts": self.registry["contracts"], "protocol": self.registry["protocol"],
                    "market_contract": self.registry["market_contract"],
                    "data_api": self.registry["data_api"],
                    "market_constraints": self.registry["market_constraints"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = monitor.report(self.registry, path, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "HEALTHY")
        self.assertEqual(result["required_execution_mode"], "PAPER_SIMULATED")

    def test_missing_or_changed_platform_field_fails_closed(self) -> None:
        snapshot = {"observed_at": "2026-08-29T00:00:00Z", "api": {"version": "CLOB_V1"}, "contracts": {}, "protocol": {}, "market_contract": {}, "data_api": {}, "market_constraints": {}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = monitor.report(self.registry, path, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "DRIFT")
        self.assertEqual(result["required_execution_mode"], "SHADOW_LIVE_READ_ONLY")
        self.assertIn("api.version", result["drift"])
        self.assertIn("snapshot.observed_at", result["drift"])

    def test_authenticated_clob_endpoint_or_pagination_drift_fails_closed(self) -> None:
        snapshot = {"observed_at": "2026-08-31T00:00:00Z", "api": self.registry["api"],
                    "contracts": self.registry["contracts"], "protocol": dict(self.registry["protocol"]),
                    "market_contract": self.registry["market_contract"],
                    "data_api": self.registry["data_api"],
                    "market_constraints": self.registry["market_constraints"]}
        snapshot["protocol"]["authenticated_orders_endpoint"] = "/orders"
        snapshot["protocol"]["authenticated_pagination"] = "offset"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = monitor.report(self.registry, path, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "DRIFT")
        self.assertIn("protocol.authenticated_orders_endpoint", result["drift"])
        self.assertIn("protocol.authenticated_pagination", result["drift"])

    def test_future_snapshot_fails_closed(self) -> None:
        snapshot = {"observed_at": "2026-09-01T00:00:00Z", "api": self.registry["api"],
                    "contracts": self.registry["contracts"], "protocol": self.registry["protocol"],
                    "market_contract": self.registry["market_contract"],
                    "data_api": self.registry["data_api"],
                    "market_constraints": self.registry["market_constraints"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = monitor.report(self.registry, path, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "DRIFT")
        self.assertIn("snapshot.observed_at", result["drift"])


if __name__ == "__main__":
    unittest.main()
