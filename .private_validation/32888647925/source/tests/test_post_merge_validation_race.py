from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "post-merge-validation.yml"


class PostMergeValidationRaceContractTest(unittest.TestCase):
    def test_superseded_schedule_exits_cleanly_without_dispatching_stale_sha(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('checked_out_sha="$(git rev-parse HEAD)"', workflow)
        self.assertIn('if [[ "$checked_out_sha" != "$main_sha" ]]', workflow)
        self.assertIn("decision=superseded_by_new_main", workflow)
        self.assertIn(
            'if [[ "$decision" != "superseded_by_new_main" ]]',
            workflow,
        )
        self.assertIn(
            "dispatch skipped because main advanced",
            workflow,
        )
        self.assertIn(
            "a newer push or recovery cycle owns validation",
            workflow,
        )
        self.assertNotIn('EXPECTED_SHA="$main_sha"', workflow)

    def test_explicit_dispatch_remains_exact_sha_and_fail_closed(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('test "$checked_out_sha" = "$EXPECTED_SHA"', workflow)
        self.assertIn('test "$main_sha" = "$EXPECTED_SHA"', workflow)
        self.assertIn("ci.yml monitoring.yml v4-live-smoke.yml", workflow)
        self.assertIn('-f expected_sha="$EXPECTED_SHA"', workflow)
        self.assertNotIn("gh pr merge", workflow)


if __name__ == "__main__":
    unittest.main()
