import json
import sys
import tempfile
import unittest
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
        snapshot = {"api": {"version": "CLOB_V2", "production_url": "https://clob.polymarket.com", "chain_id": 137}, "contracts": {key: self.registry["contracts"][key] for key in ("pUSD", "pUSD_decimals", "ctf_exchange", "neg_risk_ctf_exchange", "neg_risk_adapter")}, "protocol": {"order_heartbeat_endpoint": "/heartbeats", "matching_engine_restart_status": 425}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = monitor.report(self.registry, path)
        self.assertEqual(result["status"], "HEALTHY")
        self.assertEqual(result["required_execution_mode"], "PAPER_SIMULATED")

    def test_missing_or_changed_platform_field_fails_closed(self) -> None:
        snapshot = {"api": {"version": "CLOB_V1"}, "contracts": {}, "protocol": {}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = monitor.report(self.registry, path)
        self.assertEqual(result["status"], "DRIFT")
        self.assertEqual(result["required_execution_mode"], "SHADOW_LIVE_READ_ONLY")
        self.assertIn("api.version", result["drift"])


if __name__ == "__main__":
    unittest.main()
