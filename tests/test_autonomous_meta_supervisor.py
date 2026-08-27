#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "meta_supervisor_v2", SCRIPTS / "meta_supervisor_v2.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class AutonomousMetaSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config" / "alpha_factory.json").read_text(encoding="utf-8"))
        self.now = 1_800_000_000
        self.main = "a" * 40

    def workflow_run(
        self,
        workflow_name: str,
        *,
        age: int = 60,
        conclusion: str = "success",
        run_id: int = 1,
        event: str = "schedule",
    ) -> dict:
        return {
            "databaseId": run_id,
            "workflowName": workflow_name,
            "status": "completed",
            "conclusion": conclusion,
            "headSha": self.main,
            "headBranch": "main",
            "event": event,
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

    def healthy_product(self) -> dict:
        return {
            "generated_ts": self.now - 60,
            "status": "HEALTHY",
            "invariants": {
                "append_only_external_store": True,
                "bounded_allowlisted_research": True,
                "real_order_submission": False,
            },
        }

    def healthy_snapshot(self) -> dict:
        return {
            "main_sha": self.main,
            "paper_validated_sha": self.main,
            "paper_validated_is_ancestor": True,
            "server_deploy_enabled": False,
            "runs": self.healthy_runs(),
        }

    def test_skipped_dispatchable_worker_requires_recovery(self) -> None:
        spec = {"dispatchable": True, "requires_current_main": False, "max_age_seconds": 0}
        latest = {
            "status": "completed",
            "conclusion": "skipped",
            "updated_ts": 1_000,
            "head_sha": "a" * 40,
        }
        result = module.classify_workflow(spec, latest, "a" * 40, 2_000, 300)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["dispatch_needed"])

    def test_expected_deploy_timer_skip_does_not_mask_real_failure(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["coordination"]["workflows"]["v7-deploy-paper-server.yml"]["ignore_scheduled_skips"] = True
        runs = [
            run
            for run in self.healthy_runs()
            if run["workflowName"] != "V7 deploy PAPER server"
        ]
        runs.extend(
            [
                self.workflow_run(
                    "V7 deploy PAPER server",
                    age=120,
                    conclusion="failure",
                    run_id=90,
                    event="workflow_run",
                ),
                self.workflow_run(
                    "V7 deploy PAPER server",
                    age=30,
                    conclusion="skipped",
                    run_id=91,
                    event="schedule",
                ),
            ]
        )
        snapshot = self.healthy_snapshot()
        snapshot["runs"] = runs
        report = module.build_report(config, snapshot, self.now)
        deploy = report["workflow_status"]["v7-deploy-paper-server.yml"]
        self.assertEqual(deploy["state"], "failure_cooldown", deploy)
        self.assertEqual(deploy["latest_run"]["database_id"], 90)
        self.assertIn("failure", deploy["reason"])
        self.assertEqual(report["invariants"]["expected_scheduled_skips_ignored"], 1)
        self.assertEqual(report["status"], "DEGRADED")

    def test_expected_deploy_timer_skip_without_prior_evidence_is_missing(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["coordination"]["workflows"]["v7-deploy-paper-server.yml"]["ignore_scheduled_skips"] = True
        runs = [
            run
            for run in self.healthy_runs()
            if run["workflowName"] != "V7 deploy PAPER server"
        ]
        runs.append(
            self.workflow_run(
                "V7 deploy PAPER server",
                age=30,
                conclusion="skipped",
                run_id=91,
                event="schedule",
            )
        )
        snapshot = self.healthy_snapshot()
        snapshot["runs"] = runs
        report = module.build_report(config, snapshot, self.now)
        deploy = report["workflow_status"]["v7-deploy-paper-server.yml"]
        self.assertEqual(deploy["state"], "missing", deploy)
        self.assertIsNone(deploy["latest_run"])
        self.assertEqual(report["invariants"]["expected_scheduled_skips_ignored"], 1)
        self.assertEqual(report["status"], "DEGRADED")

    def test_nonconfigured_scheduled_skip_is_not_filtered(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] == "Polymarket Research Policy":
                run["conclusion"] = "skipped"
                run["updatedAt"] = self.now - 30
        snapshot = self.healthy_snapshot()
        snapshot["runs"] = runs
        report = module.build_report(self.config, snapshot, self.now)
        policy = report["workflow_status"]["research-policy.yml"]
        self.assertEqual(policy["state"], "skipped", policy)
        self.assertIn("skipped", policy["reason"])
        self.assertEqual(report["invariants"]["expected_scheduled_skips_ignored"], 0)

    def test_missing_autonomous_product_is_unhealthy(self) -> None:
        health = module._autonomous_product_health({}, {}, 2_000)
        self.assertFalse(health["healthy"])
        self.assertIn("autonomous_research_product_missing", health["reasons"])

    def test_unwired_product_channel_does_not_create_false_degradation(self) -> None:
        snapshot = self.healthy_snapshot()
        self.assertFalse(module._autonomous_product_channel_wired(snapshot))
        report = module.build_report(self.config, snapshot, self.now)
        self.assertEqual(report["status"], "HEALTHY", report)
        self.assertFalse(report["invariants"]["autonomous_research_product_channel_wired"])
        product = report["product_health"]["autonomous_research"]
        self.assertEqual(product["reported_status"], "NOT_WIRED")
        self.assertTrue(product["healthy"])
        self.assertFalse(
            any(
                alert.get("code") == "AUTONOMOUS_RESEARCH_PRODUCT_DEGRADED"
                for alert in report["alerts"]
            ),
            report["alerts"],
        )

    def test_wired_but_missing_product_remains_fail_closed(self) -> None:
        snapshot = self.healthy_snapshot()
        snapshot["products"] = {"autonomous_research": {}}
        self.assertTrue(module._autonomous_product_channel_wired(snapshot))
        report = module.build_report(self.config, snapshot, self.now)
        self.assertEqual(report["status"], "REMEDIATING", report)
        self.assertTrue(report["invariants"]["autonomous_research_product_channel_wired"])
        self.assertTrue(
            any(
                alert.get("code") == "AUTONOMOUS_RESEARCH_PRODUCT_DEGRADED"
                for alert in report["alerts"]
            ),
            report["alerts"],
        )
        self.assertIn(
            "research-queue.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
            report["dispatch_plan"],
        )
        self.assertEqual(
            report["workflow_status"]["research-queue.yml"]["state"],
            "product_degraded",
        )

    def test_economic_stagnation_triggers_research_remediation(self) -> None:
        product = self.healthy_product()
        product["economic_progress"] = {
            "state": "STAGNANT",
            "seconds_since_progress": 7_500,
        }
        snapshot = self.healthy_snapshot()
        snapshot["products"] = {"autonomous_research": product}
        report = module.build_report(self.config, snapshot, self.now)
        self.assertEqual(report["status"], "REMEDIATING", report)
        health = report["product_health"]["autonomous_research"]
        self.assertFalse(health["healthy"], health)
        self.assertEqual(health["economic_state"], "STAGNANT")
        self.assertTrue(
            any(reason.startswith("autonomous_research_economic_stagnation") for reason in health["reasons"]),
            health,
        )
        self.assertIn(
            "research-queue.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
            report["dispatch_plan"],
        )
        self.assertFalse(report["invariants"]["economic_stagnation_is_health_evidence"])

    def test_explicit_unprotected_main_is_critical_external_blocker(self) -> None:
        snapshot = self.healthy_snapshot()
        snapshot["main_branch_protected"] = False
        report = module.build_report(self.config, snapshot, self.now)
        self.assertEqual(report["status"], "DEGRADED", report)
        self.assertFalse(report["invariants"]["main_branch_protection_enforced"])
        self.assertTrue(
            any(alert.get("code") == "MAIN_BRANCH_UNPROTECTED" for alert in report["alerts"]),
            report["alerts"],
        )
        self.assertIn(
            "repository-settings/main-branch-protection",
            [item["workflow_file"] for item in report["blocked_actions"]],
            report["blocked_actions"],
        )

    def test_waiting_runtime_is_acceptable_when_server_deploy_is_disabled(self) -> None:
        snapshot = {
            "server_deploy_enabled": False,
            "products": {
                "autonomous_research": {
                    "generated_ts": 1_900,
                    "status": "WAITING_RUNTIME",
                    "invariants": {
                        "append_only_external_store": True,
                        "bounded_allowlisted_research": True,
                        "real_order_submission": False,
                    },
                }
            },
        }
        health = module._autonomous_product_health({}, snapshot, 2_000)
        self.assertTrue(health["healthy"], health)

    def test_waiting_runtime_is_not_acceptable_when_server_deploy_is_enabled(self) -> None:
        snapshot = {
            "server_deploy_enabled": True,
            "products": {
                "autonomous_research": {
                    "generated_ts": 1_900,
                    "status": "WAITING_RUNTIME",
                    "invariants": {
                        "append_only_external_store": True,
                        "bounded_allowlisted_research": True,
                        "real_order_submission": False,
                    },
                }
            },
        }
        health = module._autonomous_product_health({}, snapshot, 2_000)
        self.assertFalse(health["healthy"])
        self.assertTrue(
            any(reason.startswith("autonomous_research_reported_status") for reason in health["reasons"]),
            health,
        )

    def test_private_health_failure_cooldown_is_critical_and_degraded(self) -> None:
        runs = self.healthy_runs()
        for run in runs:
            if run["workflowName"] == "V7 PAPER server health":
                run["conclusion"] = "failure"
                run["updatedAt"] = self.now - 60
        snapshot = {
            "main_sha": self.main,
            "paper_validated_sha": self.main,
            "paper_validated_is_ancestor": True,
            "server_deploy_enabled": True,
            "runs": runs,
            "products": {"autonomous_research": self.healthy_product()},
        }
        report = module.build_report(self.config, snapshot, self.now)
        state = report["workflow_status"]["v7-paper-server-health.yml"]
        self.assertEqual(state["state"], "failure_cooldown")
        self.assertEqual(report["status"], "DEGRADED")
        self.assertFalse(report["invariants"]["failure_cooldown_is_health_evidence"])
        alerts = [
            alert for alert in report["alerts"]
            if alert.get("code") == "WORKFLOW_FAILURE_COOLDOWN"
        ]
        self.assertTrue(alerts, report["alerts"])
        self.assertTrue(any(alert.get("severity") == "critical" for alert in alerts))
        self.assertNotIn(
            "v7-paper-server-health.yml",
            [item["workflow_file"] for item in report["dispatch_plan"]],
        )


if __name__ == "__main__":
    unittest.main()
