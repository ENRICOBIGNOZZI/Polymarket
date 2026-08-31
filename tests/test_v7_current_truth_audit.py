from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("v7_current_truth_audit", ROOT / "scripts/v7_current_truth_audit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = load()


class CurrentTruthAuditTests(unittest.TestCase):
    def test_report_is_redacted_and_hashes_current_sources(self) -> None:
        report = audit.audit(ROOT, now=datetime(2026, 8, 31, tzinfo=timezone.utc),
                             secret_report={
                                 "history_scanned": True,
                                 "finding_count": 1,
                                 "findings": [{
                                     "kind": "assigned_secret",
                                     "location": "history:abc:note.txt:1",
                                     "fingerprint": "a" * 16,
                                 }],
                             })
        self.assertEqual(report["audit_timestamp"], "2026-08-31T00:00:00Z")
        self.assertEqual(set(report["source_sha256"]), set(audit.SOURCE_PATHS))
        self.assertFalse(report["security"]["safe_for_authenticated_execution"])
        self.assertNotIn("findings", report["security"])
        self.assertEqual(report["security"]["audit_state"], "SECURITY_BLOCKED")
        self.assertIn("MORE_EVIDENCE_REQUIRED = TRUE", audit.markdown(report))


if __name__ == "__main__":
    unittest.main()
