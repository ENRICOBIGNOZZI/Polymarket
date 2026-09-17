from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_coordinator_reservations import NOW, fill, final, request
from v7_lead_lag_accounting_audit import audit_records
from v7_lead_lag_replay import primitive


def cohort():
    req = request()
    position = "lead-position-test"
    f = fill(req)
    f = replace(f, position_id=position,
                decision_ts_ms=f.recorded_ts_ms - 1,
                metadata=dict(f.metadata, model_family="lead_lag_taker_v1"))
    z = final(req)
    z = replace(z, position_id=position,
                metadata=dict(z.metadata, model_family="lead_lag_taker_v1"))
    return req, f, z


class AccountingAuditTest(unittest.TestCase):
    def test_exact_fill_and_embedded_resolution_reconcile(self):
        _, f, z = cohort()
        report = audit_records([f, z])
        self.assertEqual(report["lead_lag_position_count"], 1)
        self.assertEqual(report["unresolved_position_count"], 0)
        self.assertEqual(D(report["supported_pnl_contribution"]), D("0.965"))
        self.assertEqual(report["positions"][0]["state"], "SUPPORTED_ACCOUNTING")
        self.assertEqual(report["positions"][0]["precision"], "DECIMAL_EXACT_METADATA")
        self.assertEqual(report["flag_counts"], {"FILL_TIMING_IS_SYNTHETIC_ORDERING_NOT_LATENCY": 1})
        self.assertEqual(report["status"], "SCOPED_AUDIT_COMPLETE_NO_PORTFOLIO_AUTHORITY")

    def test_missing_resolution_is_unresolved_not_zero(self):
        _, f, z = cohort()
        metadata = dict(z.metadata)
        metadata.pop("resolution_proof")
        z = replace(z, metadata=metadata)
        report = audit_records([f, z])
        self.assertEqual(report["unresolved_position_count"], 1)
        self.assertEqual(report["supported_pnl_contribution"], "0")
        self.assertIsNone(report["supported_complete_scope_pnl"])
        self.assertIn("RESOLUTION_UNRESOLVED_OR_PROOF_MISSING", report["flag_counts"])

    def test_wrong_final_pnl_produces_append_only_proposal(self):
        _, f, z = cohort()
        z = replace(z, final_pnl=0.0)
        report = audit_records([f, z])
        row = report["positions"][0]
        self.assertEqual(D(row["supported_settlement_pnl"]), D("0.965"))
        self.assertIn("RECORDED_PNL_REQUIRES_CORRECTION", row["flags"])
        self.assertEqual(row["correction_proposal"]["status"], "PROPOSED_APPEND_ONLY_NOT_APPLIED")
        self.assertFalse(report["history_modified"])

    def test_duplicate_final_is_not_silently_last_write_wins(self):
        _, f, z = cohort()
        z2 = replace(z, record_id="another-final")
        report = audit_records([f, z, z2])
        self.assertEqual(report["positions"][0]["final_count"], 2)
        self.assertIn("MULTIPLE_FINALS_REQUIRE_EXPLICIT_CORRECTION", report["flag_counts"])
        self.assertEqual(report["status"], "REQUIRES_REVIEW")

    def test_conservative_terminal_marker_requires_authoritative_reconciliation(self):
        _, f, z = cohort()
        z = replace(z, metadata=dict(z.metadata, zero_recovery=True))
        report = audit_records([f, z])
        self.assertIn("CONSERVATIVE_TERMINAL_REQUIRES_OFFICIAL_RECONCILIATION", report["flag_counts"])
        self.assertIsNotNone(report["positions"][0]["correction_proposal"])

    def test_legacy_float_rows_are_explicitly_not_upgraded_to_decimal_precision(self):
        _, f, z = cohort()
        metadata = dict(f.metadata)
        metadata.pop("exact_paper_fill")
        f = replace(f, metadata=metadata)
        report = audit_records([f, z])
        self.assertEqual(report["positions"][0]["precision"], "RECORDED_FLOAT_COLUMNS_CASH_FEE_INTERPRETATION")
        self.assertEqual(report["whole_portfolio_reconciled"], False)

    def test_duplicate_record_id_conflict_is_rejected(self):
        _, f, _ = cohort()
        with self.assertRaisesRegex(ValueError, "CANONICAL_RECORD_ID_CONFLICT"):
            audit_records([f, replace(f, filled_size=1.0)])

    def test_cli_writes_new_report_only(self):
        _, f, z = cohort()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "snapshot.jsonl"
            output = root / "audit.json"
            ledger.write_text("\n".join(json.dumps(event.to_dict(), sort_keys=True) for event in (f, z)) + "\n")
            command = [sys.executable, str(ROOT / "scripts/v7_lead_lag_accounting_audit.py"),
                       "--ledger-snapshot", str(ledger), "--output", str(output)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output.read_bytes()
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(ledger.read_text().count("\n"), 2)


if __name__ == "__main__":
    unittest.main()
