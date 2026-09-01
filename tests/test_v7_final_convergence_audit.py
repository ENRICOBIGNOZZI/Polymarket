from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_final_convergence_audit import (  # noqa: E402
    FinalConvergenceError,
    REQUIRED_CHECKS,
    build,
    validate_ruleset,
)


class FinalConvergenceAuditTests(unittest.TestCase):
    def test_current_tree_is_one_safe_v7_system(self) -> None:
        report = build(ROOT)
        self.assertTrue(report["valid"])
        self.assertEqual(report["architecture"]["system_count"], 1)
        self.assertEqual(report["architecture"]["live_algorithm_count"], 2)
        self.assertEqual(report["architecture"]["known_migration_defect_count"], 0)
        self.assertEqual(report["surfaces"]["delete_active_legacy_count"], 0)
        self.assertEqual(report["surfaces"]["temporary_compatibility_count"], 0)
        self.assertFalse(report["safety"]["real_order_submission"])
        self.assertFalse(report["readiness"]["profitability_proven"])

    def test_ruleset_check_or_review_weakening_fails_closed(self) -> None:
        ruleset = json.loads(
            (ROOT / "artifacts/github_main_ruleset.json").read_text(encoding="utf-8")
        )
        weakened = copy.deepcopy(ruleset)
        pull_request = next(row for row in weakened["rules"] if row["type"] == "pull_request")
        pull_request["parameters"]["require_code_owner_review"] = False
        with self.assertRaisesRegex(FinalConvergenceError, "ruleset_authority_review"):
            validate_ruleset(weakened)

        missing_check = copy.deepcopy(ruleset)
        checks = next(
            row for row in missing_check["rules"] if row["type"] == "required_status_checks"
        )["parameters"]["required_status_checks"]
        checks.pop()
        with self.assertRaisesRegex(FinalConvergenceError, "ruleset_required_checks"):
            validate_ruleset(missing_check)
        self.assertEqual(len(REQUIRED_CHECKS), 6)


if __name__ == "__main__":
    unittest.main()
