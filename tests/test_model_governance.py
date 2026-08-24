from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ModelGovernanceContractTest(unittest.TestCase):
    def test_live_runtime_is_selected_by_explicit_champion_manifest(self):
        manifest_path = ROOT / "config" / "live_champion.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["version"], 4)
        self.assertEqual(manifest["loop"], "scripts/paper_v4_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v4.json")
        self.assertEqual(manifest["run_root"], "runs/paper_v4_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertEqual(manifest["promotion_policy"], "approved integration PR only")

        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn("config/live_champion.json", selector)
        self.assertIn("approved integration PR only", selector)
        self.assertIn("--print-champion", selector)
        self.assertNotIn("scripts/paper_v*_loop.sh", selector)
        self.assertNotIn("best_version", selector)

        completed = subprocess.run(
            ["bash", "scripts/paper_latest_loop.sh", "--print-champion"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn("paper_champion version=4", completed.stdout)
        self.assertIn("loop=scripts/paper_v4_loop.sh", completed.stdout)
        self.assertIn("config/paper_v4.json", completed.stdout)
        self.assertIn("run_root=", completed.stdout)
        self.assertIn("deploy_ref=paper-validated", completed.stdout)

    def test_system_watch_owns_research_to_single_champion_cycle(self):
        policy = (ROOT / "docs" / "SYSTEM_WATCH.md").read_text(encoding="utf-8")
        required_phrases = (
            "one live champion",
            "research -> evidence -> approval -> integration -> validation -> single live champion",
            "Keep unapproved research isolated",
            "Shadow-only exception",
            "integration/*",
            "approved-for-integration",
            "single-model-reviewed",
            "main == paper-validated == deployed HEAD",
            "one shared model registry/orchestrator",
            "at most one approved integration PR at a time",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, policy)

    def test_development_policy_keeps_unapproved_research_off_main(self):
        development = (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
        self.assertIn("Unapproved research belongs on `research/*`", development)
        self.assertIn("Approval of a research result does not authorize merging the research branch", development)
        self.assertIn("`integration/*`: the only branch class", development)
        self.assertIn("`config/live_champion.json` selects one loop", development)
        self.assertIn("The hourly model-governance scheduler may merge at most one", development)

    def test_pull_request_template_requires_unification_and_shadow_isolation(self):
        template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("Approved research integration into the single champion", template)
        self.assertIn("Source research PR/branch/commit", template)
        self.assertIn("The candidate is connected to the existing expert/signal/intent interfaces", template)
        self.assertIn("There remains one model orchestrator/registry", template)
        self.assertIn("Duplicated or superseded implementation", template)
        self.assertIn("Shadow outputs use separate files/state/telemetry", template)
        self.assertIn("main == paper-validated == deployed HEAD", template)

    def test_hourly_workflow_is_gated_and_never_admin_merges(self):
        workflow = (ROOT / ".github" / "workflows" / "model-governance.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "11 * * * *"', workflow)
        self.assertIn("Polymarket System Watch - Model Governance", workflow)
        self.assertIn("research-approved", workflow)
        self.assertIn("approved-for-integration", workflow)
        self.assertIn("single-model-reviewed", workflow)
        self.assertIn("shadow-isolated", workflow)
        self.assertIn('head.startswith("integration/")', workflow)
        self.assertIn("len(candidates) > 1", workflow)
        self.assertIn("merge at most one coherent champion change per cycle", workflow)
        self.assertIn('gh pr merge "$PR_NUMBER" --squash --delete-branch', workflow)
        self.assertNotIn("--admin", workflow)
        self.assertIn("ci.yml monitoring.yml v4-live-smoke.yml", workflow)
        self.assertIn("The incumbent champion remains live", workflow)
        self.assertIn("model-governance-report.md", workflow)
        self.assertIn("retention-days: 30", workflow)


if __name__ == "__main__":
    unittest.main()
