from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "external-intelligence.yml"


class ExternalIntelligenceWorkflowContractTest(unittest.TestCase):
    def test_external_lane_is_standalone_v7_research(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: Polymarket External Intelligence", workflow)
        self.assertIn('cron: "17 * * * *"', workflow)
        self.assertIn("timeout-minutes: 55", workflow)
        self.assertIn("python3 scripts/run_external_intelligence.py", workflow)
        self.assertIn("paper_only", workflow)
        self.assertIn("authenticated_execution", workflow)
        self.assertNotIn("alpha_factory", workflow.lower())
        self.assertNotIn("attach_external_evidence", workflow)
        self.assertNotIn("live-smoke", workflow.lower())

    def test_only_main_manual_or_schedule_can_publish_durable_telemetry(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "if: (github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')) && success()",
            workflow,
        )
        self.assertNotIn(
            "if: (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && success()",
            workflow,
        )

    def test_workflow_validates_its_current_contract(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertGreaterEqual(
            workflow.count("tests/test_external_intelligence_workflow_contract.py"),
            2,
        )
        self.assertIn("scripts/external_intelligence.py", workflow)
        self.assertIn("scripts/external_request_policy.py", workflow)
        self.assertIn("scripts/gdelt_webngrams.py", workflow)
        self.assertIn("scripts/build_github_contents_request.py", workflow)


if __name__ == "__main__":
    unittest.main()
