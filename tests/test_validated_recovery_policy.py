from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "research-policy.yml"


class ValidatedRecoveryPolicyTest(unittest.TestCase):
    def test_recovery_requires_explicit_marker_and_missing_base_pr_provenance(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Validated rollback recovery: true", text)
        self.assertIn("base_merged_pr=", text)
        self.assertIn('[[ -z "$base_merged_pr" ]]', text)

    def test_recovery_is_exactly_bound_to_existing_validated_ref(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("git fetch --no-tags origin main paper-validated", text)
        self.assertIn('git merge-base --is-ancestor "$validated_sha" "$base_sha"', text)
        self.assertIn('[[ "$validated_sha" != "$base_sha" ]]', text)
        self.assertIn('git diff --quiet "$validated_sha" HEAD -- "$path"', text)
        self.assertIn('[[ "$path" != "config/live_champion.json" ]]', text)

    def test_recovery_keeps_hard_safety_and_only_neutralizes_sensitive_diff_for_lifecycle_check(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python3 scripts/hard_safety_policy.py", text)
        self.assertIn("validated-recovery-empty-files.txt", text)
        self.assertIn('policy_changed_files="changed-files.txt"', text)
        self.assertIn('--changed-files "$policy_changed_files"', text)
        self.assertIn("validated-recovery-report.md", text)


if __name__ == "__main__":
    unittest.main()
