from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7LivePaperValidationContractTest(unittest.TestCase):
    def test_workflow_is_exact_sha_v7_paper_only_with_separate_economic_gate(self) -> None:
        text = (ROOT / ".github/workflows/v7-live-paper-validation.yml").read_text(encoding="utf-8")
        for required in (
            "name: V7 live PAPER validation",
            "expected_sha:",
            "Private runtime single-writer validation",
            "ci.yml:ci",
            "monitoring.yml:monitoring",
            "private-runtime-single-writer-validation.yml:Private runtime single-writer validation",
            "all_exact_green=true",
            "validation_state=awaiting_all_exact_sha_technical_gates",
            "scripts/v7_cutover_contract.py",
            "scripts/paper_v7_execution_loop.sh",
            "polymarket_v7_trade_recorder",
            "scripts/v7_ledger_spool.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_portfolio_guard.py",
            "economic_ready",
            "promotion_ready",
            "Record economic readiness separately from PAPER deployment",
            "PAPER deployment is a technical/safety evidence-collection state",
            "Record immutable main-SHA validation result",
            "validated_main_sha=$VALIDATION_SHA",
            "paper_deployment_mode=evidence_collection",
            "git fetch --no-tags origin main",
            "contents: read",
        ):
            self.assertIn(required, text)
        for forbidden in (
            "Polymarket Research Policy",
            "research-policy.yml",
            "project_context",
            "scheduler_registry",
            "gh pr merge",
            "git push origin main",
            "git/refs/heads/",
            "force=true",
            "paper_v7_loop.sh",
            "schedule:",
            "contents: write",
            "cutover_approved:",
        ):
            self.assertNotIn(forbidden, text)

    def test_deploy_is_manual_exact_sha_cutover_only(self) -> None:
        text = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text(encoding="utf-8")
        for required in (
            "expected_sha:",
            "cutover_approved:",
            "inputs.cutover_approved == true",
            "EXPECTED_DEPLOY_SHA: ${{ inputs.expected_sha }}",
            "canonical main does not match the explicitly approved SHA",
        ):
            self.assertIn(required, text)
        for forbidden in ("schedule:", "workflow_run:", "github.event_name == 'schedule'"):
            self.assertNotIn(forbidden, text)
        self.assertIn('git show "$main_sha:scripts/v7_prepare_cutover_run_root.py" > "$archiver"', text)
        self.assertIn('POLYMARKET_CUTOVER_ARCHIVER="$archiver"', text)

    def test_cutover_contract_reads_v7_safety_authority(self) -> None:
        text = (ROOT / "scripts/v7_cutover_contract.py").read_text(encoding="utf-8")
        for required in (
            '"config/operator_directives.json"',
            '"latest_explicit_user_instruction"',
            '"paper_v7_authorization"',
            '"config/live_champion.json"',
            '"scripts/paper_v7_execution_loop.sh"',
            '"config/paper_v7.json"',
            '"market_limit"',
            '"fractional_kelly_ceiling"',
            '"max_drawdown"',
            '"authoritative_fee_required"',
            '"shared_execution_ledger_required"',
            '"single_canonical_ledger_writer"',
            '"joint_fill_state_required_for_multileg"',
            '"queue_never_grants_size"',
            '"partial_unwind_required"',
            '"cost_vector_required"',
            '"config/v7_live_model_scope.json"',
            '"scripts/v7_research_shadow_supervisor.py"',
        ):
            self.assertIn(required, text)

    def test_champion_is_one_safe_v7_runtime(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertFalse(manifest["real_order_submission"])
        self.assertEqual(manifest["loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v7.json")


if __name__ == "__main__":
    unittest.main()
