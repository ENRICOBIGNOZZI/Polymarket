from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_audit():
    path = ROOT / "scripts/v7_fast_rest_snapshot_provenance_audit.py"
    spec = importlib.util.spec_from_file_location("v7_fast_rest_snapshot_provenance_audit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V7FastRestSnapshotProvenanceAuditTest(unittest.TestCase):
    def test_current_source_exposes_rest_snapshot_provenance_blocker(self) -> None:
        report = load_audit().audit(ROOT)
        self.assertEqual(report["documented_rest_contract"]["required_snapshot_fields"], [
            "timestamp", "hash", "bids", "asks"
        ])
        self.assertTrue(report["blocking_provenance_loss"])
        self.assertEqual(report["state"], "BLOCKING_REST_SNAPSHOT_PROVENANCE_DROPPED")
        self.assertFalse(report["promotion_allowed"])

    def test_audit_binds_the_actual_api_and_runtime_paths(self) -> None:
        report = load_audit().audit(ROOT)
        source = report["source_contract"]
        self.assertFalse(source["book_exchange_timestamp_field"])
        self.assertFalse(source["book_snapshot_hash_field"])
        self.assertFalse(source["fetch_books_parses_timestamp"])
        self.assertFalse(source["fetch_books_parses_hash"])
        self.assertTrue(source["runtime_calls_rest_un_timestamped"])
        self.assertTrue(source["rest_resync_clears_snapshot_ready"])
        self.assertTrue(source["rest_resync_clears_exchange_clock"])
        self.assertTrue(source["rest_resync_clears_receive_clock"])

    def test_successor_contract_does_not_treat_rest_as_gap_free_ws_lineage(self) -> None:
        report = load_audit().audit(ROOT)
        successor = " ".join(report["required_successor"])
        self.assertIn("point-in-time snapshot", successor)
        self.assertIn("not as continuous WS lineage", successor)
        self.assertIn("reject out-of-order deltas", successor)
        self.assertIn("per-token provenance", successor)


if __name__ == "__main__":
    unittest.main()
