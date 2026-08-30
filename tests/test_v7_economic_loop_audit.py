import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_economic_loop_audit import identity, summarize_ledgers  # noqa: E402


class EconomicLoopAuditTests(unittest.TestCase):
    def test_dedup_identity_includes_model_policy_config_and_record(self) -> None:
        row = {"model_sha": "a", "policy_hash": "b", "config_hash": "c", "record_id": "d"}
        self.assertEqual(identity(row), ("a", "b", "c", "d"))

    def test_telemetry_and_independent_units_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ledger = pathlib.Path(folder) / "execution.jsonl"
            rows = [
                {"event_type": "ORDER_SUBMITTED", "strategy": "maker", "model_sha": "a",
                 "policy_hash": "p", "config_hash": "c", "record_id": "r1", "order_id": "o1"},
                {"event_type": "ORDER_SUBMITTED", "strategy": "maker", "model_sha": "a",
                 "policy_hash": "p", "config_hash": "c", "record_id": "r1", "order_id": "o1"},
                {"event_type": "ORDER_STATE", "strategy": "maker", "model_sha": "a",
                 "policy_hash": "p", "config_hash": "c", "record_id": "r2", "order_id": "o1"},
            ]
            ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            audit = summarize_ledgers([ledger])
            self.assertEqual(audit["raw_rows"], 3)
            self.assertEqual(audit["deduplicated_rows"], 2)
            self.assertEqual(audit["duplicate_rows_removed"], 1)
            self.assertEqual(audit["families"]["maker"]["telemetry_rows"], 2)
            self.assertEqual(audit["families"]["maker"]["funnel"]["orders"], 1)


if __name__ == "__main__":
    unittest.main()
