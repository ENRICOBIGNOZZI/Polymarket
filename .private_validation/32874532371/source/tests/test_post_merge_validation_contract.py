from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "post-merge-validation.yml"


class PostMergeValidationContractTest(unittest.TestCase):
    def test_recovery_ancestry_check_has_full_git_history(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("fetch-depth: 0", workflow)
        self.assertNotIn("fetch-depth: 1", workflow)
        self.assertIn(
            'git merge-base --is-ancestor "$validated_sha" "$checked_out_sha"',
            workflow,
        )

    def test_exact_sha_dispatch_contract_remains_fail_closed(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('test "$checked_out_sha" = "$EXPECTED_SHA"', workflow)
        self.assertIn('test "$main_sha" = "$EXPECTED_SHA"', workflow)
        self.assertIn('gh workflow run "$workflow" --ref main -f expected_sha="$EXPECTED_SHA"', workflow)
        self.assertNotIn("gh pr merge", workflow)


if __name__ == "__main__":
    unittest.main()
