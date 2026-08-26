from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "admin_supervisor_report.py"
SPEC = importlib.util.spec_from_file_location("admin_supervisor_report", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AdminSupervisorProductionRunTest(unittest.TestCase):
    def run(self, *, workflow: str, event: str, branch: str | None, created: str, conclusion: str) -> dict:
        return {
            "workflowName": workflow,
            "event": event,
            "headBranch": branch,
            "createdAt": created,
            "status": "completed",
            "conclusion": conclusion,
            "url": f"https://example.invalid/{created}",
        }

    def test_newer_research_dispatch_does_not_replace_main_state(self) -> None:
        runs = [
            self.run(
                workflow="ci",
                event="push",
                branch="main",
                created="2026-08-26T15:00:00Z",
                conclusion="success",
            ),
            self.run(
                workflow="ci",
                event="workflow_dispatch",
                branch="research/v7-unified-paper-engine-20260826",
                created="2026-08-26T16:00:00Z",
                conclusion="failure",
            ),
        ]
        latest = MODULE.latest_by_workflow(runs)
        self.assertEqual(latest["ci"]["headBranch"], "main")
        self.assertEqual(MODULE.scheduler_state(latest["ci"]), "success")

    def test_main_manual_failure_remains_visible(self) -> None:
        runs = [
            self.run(
                workflow="ci",
                event="push",
                branch="main",
                created="2026-08-26T15:00:00Z",
                conclusion="success",
            ),
            self.run(
                workflow="ci",
                event="workflow_dispatch",
                branch="main",
                created="2026-08-26T16:00:00Z",
                conclusion="failure",
            ),
        ]
        latest = MODULE.latest_by_workflow(runs)
        self.assertEqual(MODULE.scheduler_state(latest["ci"]), "failure")

    def test_pull_request_run_is_never_production(self) -> None:
        run = self.run(
            workflow="monitoring",
            event="pull_request",
            branch="feature/example",
            created="2026-08-26T16:00:00Z",
            conclusion="failure",
        )
        self.assertFalse(MODULE.run_is_production(run))
        self.assertNotIn("monitoring", MODULE.latest_by_workflow([run]))

    def test_branchless_scheduled_run_remains_eligible(self) -> None:
        run = self.run(
            workflow="paper-server-health",
            event="schedule",
            branch=None,
            created="2026-08-26T16:00:00Z",
            conclusion="success",
        )
        self.assertTrue(MODULE.run_is_production(run))
        self.assertEqual(MODULE.latest_by_workflow([run])["paper-server-health"], run)


if __name__ == "__main__":
    unittest.main()
