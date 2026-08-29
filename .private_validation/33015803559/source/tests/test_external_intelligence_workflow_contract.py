from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ExternalIntelligenceWorkflowContractTest(unittest.TestCase):
    def test_writer_and_validation_concurrency_are_isolated(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "external-intelligence.yml").read_text(
            encoding="utf-8"
        )
        writer_condition = (
            "github.event_name == 'schedule' || "
            "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')"
        )
        self.assertIn(
            "group: polymarket-external-intelligence-${{ (" + writer_condition + ") && "
            "'writer' || github.ref }}",
            workflow,
        )
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' || github.event_name == 'push' || "
            "(github.event_name == 'workflow_dispatch' && github.ref != 'refs/heads/main') }}",
            workflow,
        )
        self.assertIn(
            "if: (" + writer_condition + ") && success()",
            workflow,
        )
        self.assertNotIn(
            "if: (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && success()",
            workflow,
            "manual dispatch from an unmerged ref must never publish durable telemetry",
        )
        self.assertIn('cron: "17 * * * *"', workflow)
        self.assertNotIn('cron: "17,47 * * * *"', workflow)
        self.assertIn("timeout-minutes: 55", workflow)
        self.assertNotIn("timeout-minutes: 25", workflow)

    def test_workflow_runs_this_contract_test(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "external-intelligence.yml").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(
            workflow.count("tests/test_external_intelligence_workflow_contract.py"),
            3,
            "contract test must trigger on push/PR changes and execute in validation",
        )


if __name__ == "__main__":
    unittest.main()
