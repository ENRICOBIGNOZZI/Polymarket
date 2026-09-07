from __future__ import annotations

import copy
import json
import sys
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_final_convergence_audit as convergence  # noqa: E402
from v7_final_convergence_audit import (  # noqa: E402
    FinalConvergenceError,
    REQUIRED_CHECKS,
    build,
    safe_redundant_review_ref,
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
        self.assertGreaterEqual(report["surfaces"]["redundant_review_ref_count"], 0)
        self.assertEqual(report["surfaces"]["temporary_compatibility_count"], 0)
        self.assertFalse(report["safety"]["real_order_submission"])
        self.assertFalse(report["readiness"]["profitability_proven"])


    def test_only_zero_authority_main_reachable_review_refs_are_nonblocking(self) -> None:
        safe = {
            "surface_id": "ref:refs/remotes/origin/codex/v7-merged",
            "path_or_ref": "refs/remotes/origin/codex/v7-merged",
            "object_type": "branch_or_remote_ref",
            "classification": "DELETE_ACTIVE_LEGACY",
            "economic_authority": {
                "owner": None, "capabilities": [], "executable": False,
            },
            "migration_status": "deletion_deferred_until_phase_9",
            "unique_behavior_data_contribution": (
                "fully reachable from origin/main; redundant active ref"
            ),
            "final_disposition": "delete_after_gate",
            "deletion_gate": "UNIQUE_COMMIT_AND_ACTIVE_REFERENCE_SCAN_CLEAN",
        }
        self.assertTrue(safe_redundant_review_ref(safe))
        for mutation in (
            lambda row: row.update(object_type="tracked_path"),
            lambda row: row["economic_authority"].update(executable=True),
            lambda row: row.update(path_or_ref="refs/heads/unsafe-name"),
            lambda row: row.update(unique_behavior_data_contribution="reachability unknown"),
        ):
            candidate = copy.deepcopy(safe)
            mutation(candidate)
            self.assertFalse(safe_redundant_review_ref(candidate))

    def test_tracked_delete_active_legacy_still_fails_final_convergence(self) -> None:
        manifest = convergence.build_manifest(ROOT)
        target = next(
            row for row in manifest["entries"]
            if row["object_type"] == "tracked_path"
            and row["classification"] == "KEEP_CANONICAL"
        )
        target["classification"] = "DELETE_ACTIVE_LEGACY"
        with mock.patch.object(
            convergence, "build_manifest", return_value=manifest
        ):
            with self.assertRaisesRegex(
                FinalConvergenceError, "legacy_or_temporary_surface_remains"
            ):
                build(ROOT)

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
