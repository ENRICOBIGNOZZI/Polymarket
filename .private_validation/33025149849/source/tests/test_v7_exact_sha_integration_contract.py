from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7ExactShaIntegrationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = (ROOT / ".github/workflows/integration-merge.yml").read_text(encoding="utf-8")
        self.validator = (ROOT / "scripts/validate_scheduler_registry.py").read_text(encoding="utf-8")
        self.directives = json.loads((ROOT / "config/operator_directives.json").read_text(encoding="utf-8"))

    def test_operator_lifecycle_requires_same_sha_through_deploy(self) -> None:
        self.assertEqual(self.directives["operator_instruction_id"], "user-v7-master-multi-agent-operating-prompt-20260827")
        self.assertIn("same-SHA PAPER -> main -> paper-validated -> deploy -> server health", str(self.directives["rule"]))

    def test_integration_fast_forwards_exact_candidate_head(self) -> None:
        for token in (
            'git merge-base --is-ancestor "$current_main" "$expected_head"',
            'repos/${GITHUB_REPOSITORY}/git/refs/heads/main',
            '-f sha="$expected_head" -F force=false',
            'test "$(gh api "repos/${GITHUB_REPOSITORY}/commits/main" --jq .sha)" = "$expected_head"',
            'integrated_sha="$expected_head"',
            "'exact_head_fast_forward':True",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.workflow)

    def test_sha_changing_or_force_merge_paths_are_absent(self) -> None:
        for forbidden in ("gh pr merge", "--squash", "--admin", "-F force=true"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.workflow)

    def test_recovery_base_is_narrow_and_fail_closed(self) -> None:
        for token in (
            'git merge-base --is-ancestor "$validated_sha" "$main_sha"',
            "champion.get('enabled') is False",
            "champion.get('paper_only') is True",
            "champion.get('authenticated_execution') is False",
            "user-v7-master-multi-agent-operating-prompt-20260827",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.workflow)

    def test_dispatch_is_bound_to_exact_integrated_head(self) -> None:
        for token in (
            'test "$expected_base" = "$current_main"',
            'integrated_sha="$expected_head"',
            "'sha':sys.argv[1]",
            "'exact_head_fast_forward':True",
            'gh api --method POST "repos/${GITHUB_REPOSITORY}/dispatches"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.workflow)

    def test_post_ref_cleanup_cannot_block_exact_sha_dispatch(self) -> None:
        for token in (
            'pr_state="$(gh pr view "$PR_NUMBER" --json state --jq .state 2>/dev/null || true)"',
            'if [[ "$pr_state" == "OPEN" ]]; then',
            'PR $PR_NUMBER already non-open after exact-head fast-forward',
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.workflow)
        self.assertLess(
            self.workflow.index('pr_state="$(gh pr view "$PR_NUMBER"'),
            self.workflow.index('gh api --method POST "repos/${GITHUB_REPOSITORY}/dispatches"'),
        )

    def test_registry_validator_enforces_same_contract(self) -> None:
        for token in (
            'git merge-base --is-ancestor "$current_main" "$expected_head"',
            '-f sha="$expected_head" -F force=false',
            '"gh pr merge"',
            '"--squash"',
            '"-F force=true"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.validator)


if __name__ == "__main__":
    unittest.main()
