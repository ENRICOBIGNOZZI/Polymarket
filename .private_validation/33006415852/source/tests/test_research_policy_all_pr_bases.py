from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / ".github" / "workflows" / "research-policy.yml"


class ResearchPolicyAllPullRequestBasesTest(unittest.TestCase):
    def test_pull_request_trigger_has_no_base_branch_filter(self):
        text = POLICY.read_text(encoding="utf-8")
        pull_request_block = text.split("  pull_request:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertIn("types:", pull_request_block)
        self.assertNotIn("branches:", pull_request_block)
        self.assertNotIn("branches-ignore:", pull_request_block)

    def test_policy_diffs_and_checks_the_exact_pr_base(self):
        text = POLICY.read_text(encoding="utf-8")
        self.assertIn('["pull_request"]["base"]["sha"]', text)
        self.assertIn('["pull_request"]["head"]["sha"]', text)
        self.assertIn('git diff --name-only "$base_sha...$head_sha"', text)
        self.assertIn('git cat-file -e "$base_sha:config/live_champion.json"', text)
        self.assertIn('--base-ref "$base_sha"', text)
        self.assertNotIn('git diff --name-only origin/main...HEAD', text)
        self.assertNotIn('--base-ref origin/main', text)


if __name__ == "__main__":
    unittest.main()
