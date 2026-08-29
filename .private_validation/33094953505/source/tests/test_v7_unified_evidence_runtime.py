from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7UnifiedEvidenceRuntimeTest(unittest.TestCase):
    def test_runtime_selects_one_dynamic_exact_sha_candidate(self) -> None:
        cfg = json.loads((ROOT / "config/v7_evidence_runtime.json").read_text())
        self.assertEqual(cfg["schema_version"], 2)
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(cfg["authenticated_execution"])
        selection = cfg["source_selection"]
        self.assertEqual(selection["mode"], "single_open_canonical_integration_pr")
        self.assertEqual(selection["base_branch"], "main")
        self.assertEqual(selection["head_prefix"], "integration/v7-")
        self.assertTrue(selection["require_current_main_ancestor"])
        self.assertEqual(cfg["state_partition"], "source_head_sha")
        self.assertTrue(cfg["restart_on_source_head_change"])
        self.assertEqual(
            set(cfg["required_successful_workflows"]),
            {"ci", "Polymarket Research Policy", "monitoring", "Private runtime single-writer validation"},
        )
        contract = cfg["candidate_contract"]
        self.assertEqual(contract["champion_loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertTrue(contract["require_enabled_v7_champion"])
        self.assertTrue(contract["require_single_canonical_ledger_writer"])
        self.assertTrue(contract["require_complete_cost_vector"])
        self.assertTrue(contract["require_account_drawdown_guard"])

    def test_forced_paper_champion_is_safe_and_exact_sha_health_remains_pending(self) -> None:
        champion = json.loads((ROOT / "config/live_champion.json").read_text())
        context = json.loads((ROOT / "config/project_context.json").read_text())
        self.assertTrue(champion["enabled"])
        self.assertEqual(champion["version"], 7)
        self.assertTrue(champion["paper_only"])
        self.assertFalse(champion["authenticated_execution"])
        self.assertFalse(champion["real_order_submission"])
        self.assertFalse(champion["candidate_only_until_promoted"])
        self.assertEqual(champion["promotion_policy"], "operator_forced_v7_paper_champion")
        self.assertFalse(champion["legacy_fallback_allowed"])
        self.assertEqual(champion["loop"], "scripts/paper_v7_execution_loop.sh")
        # project_context is canonical main-owned metadata and may describe the
        # V7 monitoring/runtime plane as active. It is not an exact-head deploy
        # receipt: the state itself must continue to say exact-SHA deploy/health
        # is pending until the lifecycle workflow proves it.
        self.assertTrue(context["runtime"]["active_champion"])
        self.assertTrue(context["grafana"]["active"])
        self.assertIn("pending_exact_sha_deploy_health", context["cutover"]["current_state"])

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

    def test_workflow_is_exact_sha_paper_only_and_uses_canonical_economics(self) -> None:
        text = (ROOT / ".github/workflows/v7-unified-paper-evidence.yml").read_text()
        for required in (
            "actions: read",
            "contents: read",
            "pull-requests: read",
            "integration/v7-",
            "EVENT_HEAD_SHA",
            "SOURCE_SHA",
            "git merge-base --is-ancestor \"$MAIN_SHA\" \"$SOURCE_SHA\"",
            "head_sha=${SOURCE_SHA}",
            "cfg['required_successful_workflows']",
            "scripts/paper_v7_execution_loop.sh",
            "scripts/v7_ledger_spool.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_portfolio_guard.py",
            "by-sha",
            "canonical_economics.json",
            "promotion_ready",
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
            "paper_v7_loop.sh",
            "v7_execution_evidence_hardened.py",
            "version\") == 6",
        ):
            self.assertNotIn(forbidden, text)

    def test_candidate_contract_requires_current_main_operator_authority(self) -> None:
        text = (ROOT / "scripts/v7_evidence_candidate_contract.py").read_text()
        self.assertIn("candidate operator_directives.json must exactly match current main", text)
        self.assertIn("require_single_canonical_ledger_writer", text)
        self.assertIn("one canonical ledger writer", text)
        self.assertIn("complete non-overlapping execution cost vector", text)
        self.assertIn("account-level capital allocator", text)


if __name__ == "__main__":
    unittest.main()
