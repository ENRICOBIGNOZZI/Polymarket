from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7UnifiedEvidenceRuntimeTest(unittest.TestCase):
    def test_runtime_is_exact_sha_partitioned_paper_only(self) -> None:
        cfg = json.loads((ROOT / "config/v7_evidence_runtime.json").read_text())
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(cfg["authenticated_execution"])
        self.assertEqual(cfg["source_pr"], 546)
        self.assertEqual(cfg["source_branch"], "research/v7-unified-final-evidence-20260826")
        self.assertEqual(cfg["proxy_port"], 9130)
        self.assertEqual(cfg["state_partition"], "source_head_sha")
        self.assertTrue(cfg["restart_on_source_head_change"])
        self.assertTrue(cfg["require_operator_directives_match_main"])
        self.assertTrue(cfg["require_source_live_champion_unchanged"])
        self.assertEqual(
            set(cfg["required_successful_workflows"]),
            {"ci", "Polymarket Research Policy", "monitoring", "Private runtime single-writer validation"},
        )

    def test_scheduler_has_no_promotion_or_deployment_authority(self) -> None:
        registry = json.loads((ROOT / "config/scheduler_registry.json").read_text())
        rows = {row["id"]: row for row in registry["schedulers"]}
        row = rows["v7-unified-paper-evidence"]
        self.assertEqual(row["workflow"], ".github/workflows/v7-unified-paper-evidence.yml")
        self.assertEqual(row["workflow_name"], "V7 unified PAPER evidence runtime")
        self.assertEqual(row["job"], "reconcile")
        self.assertFalse(row["merge_authority"])
        self.assertFalse(row["deploy_authority"])
        self.assertFalse(row["validation_dispatch_authority"])
        assignment = json.loads((ROOT / "config/operator_directives.json").read_text())["scheduler_assignments"]["v7-unified-paper-evidence"]
        self.assertIn("exact", assignment.lower())
        self.assertIn("per-SHA", assignment)

    def test_required_pr_workflows_bind_to_source_head_not_merge_ref(self) -> None:
        exact_with_override = "github.event.inputs.expected_sha || github.event.pull_request.head.sha || github.sha"
        exact_pr_head = "github.event.pull_request.head.sha || github.sha"
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        monitoring = (ROOT / ".github/workflows/monitoring.yml").read_text()
        research_policy = (ROOT / ".github/workflows/research-policy.yml").read_text()
        private_runtime = (ROOT / ".github/workflows/private-runtime-single-writer-validation.yml").read_text()
        self.assertGreaterEqual(ci.count(exact_with_override), 2)
        self.assertGreaterEqual(monitoring.count(exact_with_override), 2)
        self.assertIn(f"ref: ${{{{ {exact_pr_head} }}}}", research_policy)
        self.assertIn(f"ref: ${{{{ {exact_pr_head} }}}}", private_runtime)

    def test_workflow_is_separate_from_incumbent_and_fail_closed(self) -> None:
        text = (ROOT / ".github/workflows/v7-unified-paper-evidence.yml").read_text()
        for required in (
            "actions: read",
            "contents: read",
            "pull-requests: read",
            "research/v7-unified-final-evidence-20260826",
            "config/v7_evidence_runtime.json",
            "Private runtime single-writer validation",
            "polymarket-v7-evidence/repo",
            "V7_MARKET_PROXY_PORT",
            "runtime_singleton_launcher.py",
            "runtime_owner.lock",
            "by-sha",
            "origin/main:$path",
            "config/live_champion.json",
            "int(m[\"version\"]) == 6",
            "fixed_dollar_trade_cap_enabled",
            "v7_execution_evidence.json",
        ):
            self.assertIn(required, text)
        for forbidden in (
            "contents: write",
            "gh pr merge",
            "git push origin main",
            "git push origin paper-validated",
            "gh workflow run integration-merge.yml",
            "gh workflow run promotion-controller.yml",
            "POLYMARKET_DEPLOY_REF=",
        ):
            self.assertNotIn(forbidden, text)

    def test_control_plane_validator_guards_scheduler_authority(self) -> None:
        text = (ROOT / "scripts/validate_scheduler_registry.py").read_text()
        self.assertIn('"v7-unified-paper-evidence"', text)
        self.assertIn("v7-unified-paper-evidence contains forbidden authority", text)
        self.assertIn("contents: write", text)
        self.assertIn("git push origin paper-validated", text)


if __name__ == "__main__":
    unittest.main()
