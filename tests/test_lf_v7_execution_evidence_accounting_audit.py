#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lf_v7_execution_evidence_accounting_audit import run_audit


class V7ExecutionEvidenceAccountingAuditTest(unittest.TestCase):
    def test_round_trip_is_currently_double_counted_as_fills(self) -> None:
        report = run_audit()
        self.assertEqual(report["submission_count"], 1)
        self.assertEqual(report["current_counted_fills"], 2)
        self.assertEqual(report["current_fill_rate"], 2.0)
        self.assertEqual(report["expected_fill_opportunities_completed"], 1)

    def test_entry_and_partial_zero_pnl_rows_are_currently_counted_as_realized(self) -> None:
        report = run_audit()
        self.assertTrue(report["entry_zero_pnl_counted_as_realized"])
        self.assertTrue(report["partial_zero_pnl_counted_as_realized"])
        self.assertEqual(report["current_counted_realized_pnl_rows"], 3)
        self.assertEqual(report["expected_terminal_realized_pnl_rows"], 1)

    def test_audit_remains_paper_only_and_fail_closed(self) -> None:
        report = run_audit()
        self.assertEqual(report["decision"], "STRUCTURAL_EVIDENCE_ACCOUNTING_BLOCKER")
        self.assertTrue(report["paper_only"])
        self.assertFalse(report["authenticated_execution"])


if __name__ == "__main__":
    unittest.main()
