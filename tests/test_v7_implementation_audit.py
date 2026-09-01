from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_implementation_audit", ROOT / "scripts/v7_implementation_audit.py")
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class ImplementationAuditTests(unittest.TestCase):
    def test_repository_has_every_required_implementation_surface(self) -> None:
        result = audit.audit(ROOT)
        self.assertTrue(result["implementation_complete"])
        self.assertEqual(result["missing_files"], [])
        self.assertEqual(result["present_file_count"], result["required_file_count"])

    def test_missing_surface_is_explicit_and_does_not_become_an_evidence_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = audit.audit(root)
            self.assertFalse(result["implementation_complete"])
            self.assertEqual(result["present_file_count"], 0)
            self.assertIn("profitability or real PnL", result["limitations"][1])


if __name__ == "__main__":
    unittest.main()
