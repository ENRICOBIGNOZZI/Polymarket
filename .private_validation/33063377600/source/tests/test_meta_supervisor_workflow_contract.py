import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "alpha_factory.json"
WORKFLOW = ROOT / ".github" / "workflows" / "control-plane.yml"
WORKFLOW_DIR = ROOT / ".github" / "workflows"


class MetaSupervisorWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_dispatch_authority_is_config_driven_and_v7_only(self):
        coordination = self.config["coordination"]
        allowlisted = set(coordination["allowlisted_dispatches"])
        forbidden = set(coordination["forbidden_dispatches"])
        self.assertFalse(allowlisted & forbidden)
        self.assertIn("meta-supervisor dispatch contract error", self.text)
        self.assertIn("allowlisted_dispatches", self.text)
        self.assertIn("forbidden_dispatches", self.text)
        self.assertNotIn(
            "ci.yml|monitoring.yml|v4-live-smoke.yml|forward-maker-research.yml",
            self.text,
        )
        retired = {"v4-live-smoke.yml", "forward-maker-research.yml", "v6-research-smoke.yml"}
        self.assertFalse(allowlisted & retired)
        for workflow in allowlisted:
            path = WORKFLOW_DIR / workflow
            self.assertTrue(path.is_file(), workflow)
            self.assertRegex(path.read_text(encoding="utf-8"), r"(?m)^  workflow_dispatch:\s*$")

    def test_workflow_history_is_sampled_per_configured_workflow(self):
        self.assertIn('gh run list --workflow "$workflow" --limit 30', self.text)
        self.assertIn("control_plane/run-slices", self.text)
        self.assertNotIn(
            "gh run list --limit 100 --json databaseId,workflowName,status,conclusion,headSha,headBranch,event,createdAt,updatedAt,url > control_plane/runs.json",
            self.text,
        )

    def test_single_writer_lifecycle_authorities_remain_non_dispatchable(self):
        forbidden = set(self.config["coordination"]["forbidden_dispatches"])
        self.assertTrue(
            {
                "control-plane-event-bridge.yml",
                "integration-merge.yml",
                "post-merge-validation.yml",
                "v7-live-paper-validation.yml",
                "v7-deploy-paper-server.yml",
                "v7-paper-server-health.yml",
            }.issubset(forbidden)
        )
        self.assertRegex(self.text, re.compile(r'gh workflow run "\$workflow" --ref main'))


if __name__ == "__main__":
    unittest.main()
