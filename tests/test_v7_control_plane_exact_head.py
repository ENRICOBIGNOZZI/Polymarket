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

    def test_cleanup_branches_fail_closed_until_cutover_receipt_exists(self) -> None:
        text = (ROOT / ".github/workflows/research-policy.yml").read_text(encoding="utf-8")
        cleanup_guard = 'if [[ "$head_ref" == cleanup/* ]]'
        self.assertEqual(text.count(cleanup_guard), 1)
        guard_pos = text.index(cleanup_guard)
        hard_safety_pos = text.index("python3 scripts/hard_safety_policy.py")
        research_policy_pos = text.index("python3 scripts/research_pr_policy.py")
        self.assertLess(guard_pos, hard_safety_pos)
        self.assertLess(guard_pos, research_policy_pos)
        for required in (
            "cleanup locked during V7 cutover repair",
            "same-SHA PAPER",
            "paper-validated",
            "deploy",
            "server health",
            "durable machine-verifiable lifecycle receipt",
            "exit 1",
        ):
            self.assertIn(required, text)
        self.assertIn("tests/test_v7_control_plane_exact_head.py", text)

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
