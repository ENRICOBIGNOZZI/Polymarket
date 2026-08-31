from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("v7_security_audit", ROOT / "scripts/v7_security_audit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


security_audit = load()


class SecurityAuditTests(unittest.TestCase):
    def test_clean_redacted_scan_still_requires_external_security_evidence(self) -> None:
        report = security_audit.audit(
            ROOT, now=datetime(2026, 8, 31, tzinfo=timezone.utc),
            secret_report={"history_scanned": True, "finding_count": 0, "findings": []},
        )
        self.assertEqual(report["state"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(report["authenticated_execution_allowed"])
        self.assertTrue(report["checked_in_live_caps_zero"])
        self.assertEqual(report["inputs"]["secret_scan"], {"finding_count": 0, "findings": []})

    def test_finding_is_preserved_only_as_redacted_fingerprint_and_blocks(self) -> None:
        report = security_audit.audit(
            ROOT, now=datetime(2026, 8, 31, tzinfo=timezone.utc),
            secret_report={
                "history_scanned": True,
                "finding_count": 1,
                "findings": [{"kind": "assigned_secret", "location": "history:abc:file.txt:7", "fingerprint": "a" * 16}],
            },
        )
        self.assertEqual(report["state"], "SECURITY_BLOCKED")
        self.assertIn("historical_or_worktree_secret_scan_findings", report["reason_codes"])
        self.assertEqual(report["inputs"]["secret_scan"]["findings"][0]["fingerprint"], "a" * 16)

    def test_raw_or_malformed_finding_is_rejected(self) -> None:
        with self.assertRaisesRegex(security_audit.SecurityAuditError, "finding_shape"):
            security_audit.audit(
                ROOT,
                secret_report={"history_scanned": True, "finding_count": 1,
                               "findings": [{"kind": "x", "location": "x", "fingerprint": "x", "secret": "x"}]},
            )


if __name__ == "__main__":
    unittest.main()
