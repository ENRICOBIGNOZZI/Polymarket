from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowYamlContractTest(unittest.TestCase):
    def test_shell_heredoc_terminators_remain_inside_yaml_block_scalars(self) -> None:
        workflow_dir = ROOT / ".github" / "workflows"
        offenders: list[str] = []
        for path in sorted(workflow_dir.glob("*.yml")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip() in {"REMOTE", "PY"} and not line.startswith("          "):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_number}:{line!r}")
        self.assertEqual(offenders, [], "workflow heredoc terminator escaped its YAML block scalar")

    def test_deploy_workflow_keeps_all_remote_terminators_indented(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\nREMOTE\n", deploy)
        self.assertEqual(deploy.count("\n          REMOTE\n"), 3)

    def test_scheduled_ci_backfills_api_updated_pr_heads_exactly_once(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("  actions: write\n", ci)
        self.assertIn("  pull-requests: read\n", ci)
        self.assertIn(
            "if: github.event_name == 'schedule' && matrix.build_type == 'Release'",
            ci,
        )
        self.assertIn("actions/workflows/ci.yml/runs?head_sha=${head_sha}&per_page=1", ci)
        self.assertIn("(( run_count == 0 )) || continue", ci)
        self.assertIn("repos/${repo}/pulls/${pr_number}", ci)
        self.assertIn("[[ \"$current_sha\" == \"$head_sha\" ]] || continue", ci)
        self.assertIn(
            'gh workflow run ci.yml --ref "$head_ref" -f expected_sha="$head_sha"',
            ci,
        )
        self.assertNotIn("gh pr merge", ci)
        self.assertNotIn("paper-validated", ci)
        self.assertNotIn("POLYMARKET_DEPLOY_REF", ci)

    def test_live_telemetry_publish_retries_only_cas_conflicts(self) -> None:
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("for attempt in 1 2 3 4 5; do", smoke)
        self.assertIn('current_sha="$(gh api "${endpoint}?ref=telemetry"', smoke)
        self.assertIn('if publish_output="$(gh api "${args[@]}" 2>&1)"; then', smoke)
        self.assertIn("HTTP 409|expected .* but (is|was) at", smoke)
        self.assertIn("telemetry publication exhausted conflict retries", smoke)
        self.assertIn("sleep \"$attempt\"", smoke)
        self.assertLess(
            smoke.index("- name: Publish latest public telemetry"),
            smoke.index("- name: Advance paper validated ref"),
        )


if __name__ == "__main__":
    unittest.main()
