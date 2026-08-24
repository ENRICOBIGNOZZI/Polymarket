#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("meta_supervisor", ROOT / "scripts" / "meta_supervisor.py")
assert SPEC and SPEC.loader
meta_supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(meta_supervisor)


class MetaSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config" / "alpha_factory.json").read_text(encoding="utf-8"))
        self.now = 1_800_000_000
        self.main = "a" * 40
        self.validated = self.main

    def workflow_run(
        self,
        workflow_name: str,
        *,
        age: int = 60,
        sha: str | None = None,
        status: str = "completed",
        conclusion: str = "success",
        run_id: int = 1,
        branch: str = "main",
    ) -> dict:
        return {
            "databaseId": run_id,
            "workflowName": workflow_name,
            "status": status,
            "conclusion": conclusion,
            "headSha": self.main if sha is None else sha,
            "headBranch": branch,
            "event": "schedule",
            "createdAt": self.now - age - 10,
            "updatedAt": self.now - age,
            "url": f"https://example.invalid/run/{run_id}",
        }

    def healthy_runs(self) -> list[dict]:
        specs = self.config["coordination"]["workflows"]
        return [
            self.workflow_run(spec["name"], run_id=index + 1)
            for index, spec in enumerate(specs.values())
        ]

    def snapshot(self, runs: list[dict], *, ancestor: bool = True, validated: str | None = None) -> dict:
        return {
            "main_sha": self.main,
            "paper_validated_sha": self.validated if validated is None else validated,
            "paper_validated_is_ancestor": ancestor,
            "runs": runs,
        }

    def test_all_healthy_requires_no_dispatch(self) -> None:
        report = meta_supervisor.build_report(
            self.config, self.snapshot(self.healthy_runs()), self.now
        )
        self.assertEqual(report["status"], "HEALTHY")
        self.assertEqual(report["dispatch_plan"], [])
        self.assertTrue(report["invariants"]["allowlist_respected"])
        self.assertFalse(report["invariants"]["deployment_dispatched_directly"])
        self.assertFalse(report["invariants"]["authenticated_execution"])

    def test_outdated_ci_and_monitoring_run_before_dependent_smoke(self) -> None:
        old = "b" * 40
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] in {"ci", "monitoring", "v4-live-paper-smoke"}:
                run["headSha"] = old
        report = meta_supervisor.build_report(
            self.config, self.snapshot(runs, validated=old), self.now
        )
        plan = [item["workflow_file"] for item in report["dispatch_plan"]]
        self.assertEqual(plan, ["ci.yml", "monitoring.yml"])
        blocked = {item["workflow_file"]: item["reason"] for item in report["blocked_actions"]}
        self.assertIn("v4-live-smoke.yml", blocked)
        self.assertIn("dependencies are not healthy", blocked["v4-live-smoke.yml"])

    def test_stale_forward_research_is_restarted_but_alpha_factory_waits(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] == "forward-maker-alpha-research":
                run["updatedAt"] = self.now - 6000
            if run["workflowName"] == "Polymarket Alpha Factory":
                run["updatedAt"] = self.now - 12000
        report = meta_supervisor.build_report(
            self.config, self.snapshot(runs), self.now
        )
        plan = [item["workflow_file"] for item in report["dispatch_plan"]]
        self.assertEqual(plan, ["forward-maker-research.yml"])
        blocked = {item["workflow_file"] for item in report["blocked_actions"]}
        self.assertIn("alpha-factory.yml", blocked)

    def test_running_workflow_is_not_duplicated(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] == "v4-live-paper-smoke":
                run["status"] = "in_progress"
                run["conclusion"] = ""
                run["updatedAt"] = self.now - 10
        report = meta_supervisor.build_report(
            self.config, self.snapshot(runs), self.now
        )
        self.assertEqual(
            report["workflow_status"]["v4-live-smoke.yml"]["state"], "running"
        )
        self.assertNotIn(
            "v4-live-smoke.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
        )

    def test_failure_cooldown_prevents_restart_storm(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] == "ci":
                run["conclusion"] = "failure"
                run["updatedAt"] = self.now - 60
        report = meta_supervisor.build_report(
            self.config, self.snapshot(runs), self.now
        )
        self.assertEqual(report["workflow_status"]["ci.yml"]["state"], "failure_cooldown")
        self.assertNotIn(
            "ci.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
        )

    def test_old_failure_is_allowlisted_for_bounded_remediation(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] == "ci":
                run["conclusion"] = "failure"
                run["updatedAt"] = self.now - 3600
        report = meta_supervisor.build_report(
            self.config, self.snapshot(runs), self.now
        )
        self.assertIn(
            "ci.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
        )
        self.assertLessEqual(
            len(report["dispatch_plan"]),
            self.config["coordination"]["max_dispatches_per_cycle"],
        )

    def test_deploy_and_private_health_are_never_auto_dispatched(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] in {"deploy-paper-server", "paper-server-health"}:
                run["conclusion"] = "failure"
                run["updatedAt"] = self.now - 3600
        report = meta_supervisor.build_report(
            self.config, self.snapshot(runs), self.now
        )
        plan = {item["workflow_file"] for item in report["dispatch_plan"]}
        self.assertNotIn("deploy-paper-server.yml", plan)
        self.assertNotIn("server-health.yml", plan)
        self.assertFalse(report["invariants"]["deployment_dispatched_directly"])
        self.assertFalse(report["invariants"]["server_health_dispatched_directly"])

    def test_skipped_private_health_is_not_accepted_as_healthy(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] == "paper-server-health":
                run["conclusion"] = "skipped"
        report = meta_supervisor.build_report(
            self.config, self.snapshot(runs), self.now
        )
        health = report["workflow_status"]["server-health.yml"]
        self.assertEqual(health["state"], "skipped")
        self.assertFalse(health["dispatch_needed"])
        self.assertEqual(report["status"], "DEGRADED")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("WORKFLOW_SKIPPED", codes)
        self.assertNotIn(
            "server-health.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
        )

    def test_diverged_validated_ref_is_critical_and_deploy_stays_blocked(self) -> None:
        report = meta_supervisor.build_report(
            self.config,
            self.snapshot(self.healthy_runs(), ancestor=False, validated="c" * 40),
            self.now,
        )
        self.assertEqual(report["status"], "DEGRADED")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("VALIDATED_REF_DIVERGED", codes)
        self.assertNotIn(
            "deploy-paper-server.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
        )

    def test_pr_runs_do_not_mask_main_workflow_state(self) -> None:
        main_run = self.workflow_run("ci", age=100, run_id=1)
        pr_run = self.workflow_run(
            "ci", age=10, sha="d" * 40, run_id=2, branch="feature/example"
        )
        latest = meta_supervisor.latest_main_runs([main_run, pr_run])
        self.assertEqual(latest["ci"]["database_id"], 1)
        self.assertEqual(latest["ci"]["head_sha"], self.main)


if __name__ == "__main__":
    unittest.main()
