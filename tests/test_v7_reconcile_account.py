from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def load():
    spec = importlib.util.spec_from_file_location("v7_reconcile_account", ROOT / "scripts/v7_reconcile_account.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reconcile = load()


def complete_report() -> dict:
    report = {
        "schema": "polymarket_v7_real_pnl_independent_verifier_v1", "model_sha": SHA,
        "state": "REAL_PNL_RECONCILED_UNSIGNED", "real_pnl_verified": False,
        "journal_entries": 4, "all_entries_live_observed": True,
        "sources_seen": sorted(reconcile.REQUIRED_JOURNAL_SOURCES),
        "evidence_sources_seen": sorted(reconcile.REQUIRED_EVIDENCE_SOURCES),
        "wallet_snapshot_verified": True, "data_api_position_snapshot_verified": True,
        "data_api_activity_coverage_verified": True, "reason_codes": [],
        "journal_provenance_reference_breaks": [], "journal_clob_fill_evidence_breaks": [],
        "observed_balance_breaks": [], "open_outcome_positions": {},
    }
    report["report_sha256"] = reconcile.report_digest(report)
    return report


class ReconcileAccountTests(unittest.TestCase):
    def test_complete_independent_report_is_reconciled(self) -> None:
        result = reconcile.reconcile(complete_report(), run_id="run", exact_code_sha=SHA,
                                     period_start="2026-01-01T00:00:00Z", period_end="2026-01-02T00:00:00Z")
        self.assertEqual(result["state"], "RECONCILED")
        self.assertEqual(result["unresolved_break_count"], 0)

    def test_incomplete_or_nonlive_report_fails_closed(self) -> None:
        report = complete_report()
        report["evidence_sources_seen"] = []
        result = reconcile.reconcile(report, run_id="run", exact_code_sha=SHA,
                                     period_start="2026-01-01T00:00:00Z", period_end="2026-01-02T00:00:00Z")
        self.assertEqual(result["state"], "MORE_EVIDENCE_REQUIRED")
        self.assertIn("required_evidence_sources_missing", result["report_integrity_breaks"])


if __name__ == "__main__":
    unittest.main()
