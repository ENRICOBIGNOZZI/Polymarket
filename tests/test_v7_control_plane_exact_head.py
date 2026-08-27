from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.validate_project_context import EXPECTED_CLEANUP_SEQUENCE, validate

ROOT = Path(__file__).resolve().parents[1]
EXACT_WITH_OVERRIDE = "github.event.inputs.expected_sha || github.event.pull_request.head.sha || github.sha"
EXACT_PR_HEAD = "github.event.pull_request.head.sha || github.sha"


class V7ControlPlaneExactHeadTest(unittest.TestCase):
    def test_ci_and_monitoring_validate_pr_source_head_not_merge_ref(self) -> None:
        for rel in (".github/workflows/ci.yml", ".github/workflows/monitoring.yml"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertGreaterEqual(text.count(EXACT_WITH_OVERRIDE), 2, rel)
            self.assertNotIn("VALIDATION_SHA: ${{ github.event.inputs.expected_sha || github.sha }}", text, rel)

    def test_research_policy_checks_out_exact_pr_source_head(self) -> None:
        text = (ROOT / ".github/workflows/research-policy.yml").read_text(encoding="utf-8")
        self.assertIn(f"ref: ${{{{ {EXACT_PR_HEAD} }}}}", text)
        self.assertIn(f"POLICY_SHA: ${{{{ {EXACT_PR_HEAD} }}}}", text)
        self.assertNotIn("delete_legacy_immediately_then_promote_only_validated_v7", text)
        self.assertNotIn("operator-authorized immediate legacy retirement", text)

    def test_critical_v7_lifecycle_workflows_are_active(self) -> None:
        contracts = {
            ".github/workflows/control-plane-event-bridge.yml": (
                "actions: write",
                "Verify current project context",
                "gh workflow run promotion-controller.yml --ref main",
                "gh workflow run integration-merge.yml --ref main",
            ),
            ".github/workflows/promotion-controller.yml": (
                "issues: write",
                "pull-requests: write",
                "Evaluate integration queue and authorize one promotion",
                "python3 scripts/promotion_gate.py",
                "--add-label autonomous-promotion-approved",
            ),
            ".github/workflows/integration-merge.yml": (
                "contents: write",
                "pull-requests: write",
                "Select exactly one controller-authorized integration",
                "gh pr merge \"$PR_NUMBER\" --squash --delete-branch --match-head-commit \"$expected_head\"",
                "champion-integration-merged",
            ),
            ".github/workflows/v7-live-paper-validation.yml": (
                "contents: write",
                "Coordinate exact-SHA V7 validation",
                "Bounded same-SHA public-data PAPER runtime",
                "Advance paper-validated to exact economically validated SHA",
                "-F force=false",
            ),
            ".github/workflows/v7-deploy-paper-server.yml": (
                "Fail-closed V7 deploy preflight",
                "Select Tailscale credential mode",
                "Reconcile exact paper-validated V7 SHA on server",
                "POLYMARKET_DEPLOY_REF=paper-validated",
            ),
        }
        for rel, required in contracts.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("if: ${{ false }}", text, rel)
            self.assertNotIn("FIXED-SHA PAPER EVIDENCE FREEZE", text.upper(), rel)
            self.assertNotIn("automatic promotion decisions disabled", text, rel)
            self.assertNotIn("automatic integration merge disabled", text, rel)
            self.assertNotIn("automatic paper-validated advancement disabled", text, rel)
            self.assertNotIn("automatic PAPER server deploy disabled", text, rel)
            for marker in required:
                self.assertIn(marker, text, f"{rel}: missing active lifecycle marker {marker!r}")

    def test_cleanup_retirement_is_authorized_without_restoring_old_runtime(self) -> None:
        text = (ROOT / ".github/workflows/research-policy.yml").read_text(encoding="utf-8")
        self.assertNotIn('if [[ "$head_ref" == cleanup/* ]]', text)
        self.assertIn("V7 the sole generation and authorizes immediate retirement", text)
        self.assertIn("PAPER/authenticated-execution separation", text)
        self.assertIn("python3 scripts/hard_safety_policy.py", text)
        self.assertIn("python3 scripts/research_pr_policy.py", text)
        self.assertIn("Verify exact policy revision", text)

    def test_operator_directive_preserves_master_cutover_sequence(self) -> None:
        directives = json.loads((ROOT / "config/operator_directives.json").read_text(encoding="utf-8"))
        self.assertEqual(directives["operator_instruction_id"], "user-v7-master-multi-agent-operating-prompt-20260827")
        self.assertEqual(directives["architecture"]["cleanup_sequence"], EXPECTED_CLEANUP_SEQUENCE)
        self.assertTrue(directives["paper_v7_authorization"]["paper_only"])
        self.assertFalse(directives["paper_v7_authorization"]["authenticated_execution"])

    def test_current_project_context_is_coherent_under_master(self) -> None:
        errors, _ = validate(ROOT)
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
