from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7LivePaperValidationContractTest(unittest.TestCase):
    def test_workflow_is_exact_sha_v7_paper_only_and_economically_gated(self) -> None:
        text = (ROOT / ".github/workflows/v7-live-paper-validation.yml").read_text(encoding="utf-8")
        for required in (
            "name: V7 live PAPER validation",
            "expected_sha:",
            "Private runtime single-writer validation",
            'test "$(git rev-parse origin/main)" = "$VALIDATION_SHA"',
            "scripts/v7_cutover_contract.py",
            "scripts/paper_v7_execution_loop.sh",
            "scripts/v7_ledger_spool.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_portfolio_guard.py",
            "economic_ready",
            "promotion_ready",
            "paper-validated unchanged",
            "git merge-base --is-ancestor \"$old_validated\" \"$VALIDATION_SHA\"",
            '-F force=false',
        ):
            self.assertIn(required, text)
        for forbidden in (
            "force=true",
            "git push origin main",
            "git push origin paper-validated",
            "gh pr merge",
            "POLYMARKET_DEPLOY_REF=",
            "paper_v7_loop.sh",
            "v7_execution_evidence_hardened.py",
        ):
            self.assertNotIn(forbidden, text)

    def test_cutover_contract_reads_operator_authority_and_canonical_v7_envelope(self) -> None:
        text = (ROOT / "scripts/v7_cutover_contract.py").read_text(encoding="utf-8")
        for required in (
            '"config/operator_directives.json"',
            '"latest_explicit_user_instruction"',
            '"paper_v7_authorization"',
            '"config/live_champion.json"',
            '"scripts/paper_v7_execution_loop.sh"',
            '"config/paper_v7.json"',
            '"market_limit"',
            '"min_liquidity"',
            '"min_net_edge"',
            '"uncertainty_penalty"',
            '"fractional_kelly_ceiling"',
            '"fixed_dollar_trade_cap_enabled"',
            '"max_drawdown"',
            '"authoritative_fee_required"',
            '"shared_execution_ledger_required"',
            '"single_canonical_ledger_writer"',
            '"joint_fill_state_required_for_multileg"',
            '"queue_never_grants_size"',
            '"partial_unwind_required"',
            '"cost_vector_required"',
            '"markout_horizons_seconds"',
            '"hard_arb_max_trade_fraction"',
        ):
            self.assertIn(required, text)

    def test_registry_assigns_live_validation_without_merge_or_deploy_authority(self) -> None:
        registry = json.loads((ROOT / "config/scheduler_registry.json").read_text(encoding="utf-8"))
        matches = [row for row in registry["schedulers"] if row["id"] == "v7-live-paper-validation"]
        self.assertEqual(len(matches), 1)
        row = matches[0]
        self.assertEqual(row["workflow"], ".github/workflows/v7-live-paper-validation.yml")
        self.assertFalse(row["merge_authority"])
        self.assertFalse(row["deploy_authority"])
        self.assertFalse(row["validation_dispatch_authority"])
        self.assertTrue(row["critical"])

    def test_candidate_champion_is_enabled_only_for_paper_evidence(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertFalse(manifest["real_order_submission"])
        self.assertTrue(manifest["candidate_only_until_promoted"])
        self.assertEqual(manifest["loop"], "scripts/paper_v7_execution_loop.sh")


if __name__ == "__main__":
    unittest.main()
