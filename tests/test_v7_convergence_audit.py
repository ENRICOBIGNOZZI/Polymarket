from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("v7_convergence_audit", SCRIPTS / "v7_convergence_audit.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class V7ConvergenceAuditTest(unittest.TestCase):
    def test_repository_contracts_validate(self) -> None:
        rows = MODULE.validate_scheduler(ROOT)
        matrix = MODULE.validate_capabilities(ROOT)
        incumbent = MODULE.validate_repository(ROOT)
        self.assertEqual(len(rows), len(list((ROOT / ".github/workflows").glob("*.yml"))))
        self.assertEqual(len(matrix["strategies"]), 15)
        self.assertFalse(incumbent["verified"])

    def test_mutating_schedulers_are_frozen(self) -> None:
        rows = json.loads((ROOT / "config/v7_scheduler_freeze.json").read_text())["workflows"]
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["v7-deploy-paper-server.yml"]["decision"], "MANUAL_CUTOVER_ONLY")
        self.assertFalse(by_id["v7-deploy-paper-server.yml"]["scheduled"])
        self.assertFalse(by_id["v7-live-paper-validation.yml"]["scheduled"])

    def test_report_refuses_to_call_unverified_deployment_accepted(self) -> None:
        matrix = MODULE.validate_capabilities(ROOT)
        report = {
            "valid": True,
            "identity": {"candidate_sha":"a"*40,"main_sha":"a"*40,"paper_validated_sha":"a"*40,"deployed_sha":"UNKNOWN_UNVERIFIED"},
            "canonical_refs_equal_candidate": True,
            "deployed_identity_verified": False,
            "engineering_acceptance": False,
        }
        rendered = MODULE.markdown(report, matrix)
        self.assertIn("deployed identity verified: `False`", rendered)
        self.assertNotIn("engineering/repository acceptance: `True`", rendered)

    def test_missing_shallow_checkout_refs_remain_unknown(self) -> None:
        self.assertEqual(MODULE.optional_git_ref(ROOT, "refs/heads/definitely-missing"), "UNKNOWN_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
