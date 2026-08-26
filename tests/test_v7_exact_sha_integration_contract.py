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
        self.assertEqual(
            self.directives["operator_instruction_id"],
            "user-v7-master-multi-agent-operating-prompt-20260827",
        )
        rule = str(self.directives["rule"])
        self.assertIn("same-SHA PAPER -> main -> paper-validated -> deploy -> server health", rule)

    def test_integration_fast_forwards_exact_candidate_head(self) -> None:
        required = (
            'git merge-base --is-ancestor "$current_main" "$expected_head"',
            'repos/${GITHUB_REPOSITORY}/git/refs/heads/main',
            '-f sha="$expected_head" -F force=false',
            'test "$(gh api "repos/${GITHUB_REPOSITORY}/commits/main" --jq .sha)" = "$expected_head"',
            'integrated_sha="$expected_head"',
            "'exact_head_fast_forward':True",
        )
        for token in required:
            with self.subTest(token=token):
                self.assertIn(token, self.workflow)

    def test_sha_changing_or_force_merge_paths_are_absent(self) -> None:
        for forbidden in ("gh pr merge", "--squash", "--admin", "-F force=true"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.workflow)

    def test_recovery_base_is_narrow_and_fail_closed(self) -> None:
        required = (
            'git merge-base --is-ancestor "$validated_sha" "$main_sha"',
            "champion.get('enabled') is False",
            "champion.get('paper_only') is True",
            "champion.get('authenticated_execution') is False",
            "user-v7-master-multi-agent-operating-prompt-20260827",
        )
        for token in required:
            with self.subTest(token=token):
                self.assertIn(token, self.workflow)

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
