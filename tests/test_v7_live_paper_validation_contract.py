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
            "validation_state=BLOCKED_STALE_NON_MAIN_SHA",
            "validation_state=BLOCKED_AWAITING_ALL_EXACT_SHA_TECHNICAL_GATES",
            "validation_state=READY_FOR_SUBSTANTIVE_PAPER_VALIDATION",
            "No substantive PAPER validation ran.",
            "scripts/v7_cutover_contract.py",
            "scripts/v7_release_provenance.py",
            "scripts/paper_v7_execution_loop.sh",
            "polymarket_v7_trade_recorder",
            "scripts/v7_ledger_spool.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_portfolio_guard.py",
            "economic_ready",
            "economic_evidence_ready",
            "Record economic readiness separately from PAPER deployment",
            "PAPER deployment is a technical/safety research state",
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

        gate = text.split("- name: Require exact-main V7 technical gates", 1)[1].split(
            "- name: Enforce V7 PAPER safety contract", 1
        )[0]
        self.assertEqual(gate.count("exit 1"), 2)
        self.assertNotIn("exit 0", gate)

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
            '"config/operator_directives.json"', '"latest_explicit_user_instruction"',
            '"paper_v7_authorization"', '"scripts/paper_v7_execution_loop.sh"',
            '"config/paper_v7.json"', '"market_limit"', '"fractional_kelly_ceiling"',
            '"max_drawdown"', '"single_canonical_ledger_writer"', '"cost_vector_required"',
            '"config/v7_live_model_scope.json"', '"polymarket_v7_live_algorithm_registry_v2"',
            '"config/v7_runtime_supervision.json"', '"scripts/v7_secret_scan.py"',
            '"scripts/v7_entropy_secret_scan.py"', '"scripts/v7_security_audit.py"',
            '"scripts/v7_release_provenance.py"',
        ):
            self.assertIn(required, text)

    def test_live_scope_is_one_safe_v7_runtime(self) -> None:
        scope = json.loads((ROOT / "config/v7_live_model_scope.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["version"], 7)
        self.assertTrue(scope["paper_only"])
        self.assertFalse(scope["authenticated_execution"])
        self.assertFalse(scope["real_order_submission"])
        self.assertTrue(scope["runtime_invariants"]["single_execution_owner"])


if __name__ == "__main__":
    unittest.main()
