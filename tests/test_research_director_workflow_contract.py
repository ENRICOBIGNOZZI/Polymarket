import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "research_director.json"
WORKFLOW_DIR = ROOT / ".github" / "workflows"
DIRECTOR_WORKFLOW = WORKFLOW_DIR / "research-queue.yml"


class ResearchDirectorWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_all_research_owners_exist_and_support_manual_dispatch(self):
        owners = self.config["owner_workflows"]
        self.assertTrue(owners)
        for workflow in owners:
            path = WORKFLOW_DIR / workflow
            self.assertTrue(path.is_file(), workflow)
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r"(?m)^  workflow_dispatch:\s*$", workflow)

    def test_retired_v3_v6_workers_are_not_research_owners(self):
        owners = set(self.config["owner_workflows"])
        retired = {
            "forward-maker-research.yml",
            "v4-live-smoke.yml",
            "v6-research-smoke.yml",
            "deploy-paper-server.yml",
            "server-health.yml",
        }
        self.assertFalse(owners & retired)

    def test_research_owners_do_not_overlap_non_research_authorities(self):
        owners = set(self.config["owner_workflows"])
        forbidden = set(self.config["forbidden_workflows"])
        self.assertFalse(owners & forbidden)
        self.assertTrue(
            {
                "integration-merge.yml",
                "promotion-controller.yml",
                "v7-live-paper-validation.yml",
                "v7-deploy-paper-server.yml",
                "v7-paper-server-health.yml",
            }.issubset(forbidden)
        )

    def test_dispatch_step_is_config_driven_and_preflights_trigger_contract(self):
        text = DIRECTOR_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("research-director dispatch contract error", text)
        self.assertIn("owner_workflows", text)
        self.assertIn("workflow_dispatch", text)
        self.assertNotIn(
            "forward-maker-research.yml|external-intelligence.yml|v6-research-smoke.yml",
            text,
        )
        self.assertRegex(text, re.compile(r"gh workflow run \"\$workflow\" --ref main"))


if __name__ == "__main__":
    unittest.main()
