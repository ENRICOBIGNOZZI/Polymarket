from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "scheduler_registry.json"
SCRIPT = ROOT / "scripts" / "scheduler_meta_supervisor.py"
NOW = "2026-08-24T15:00:00Z"
MAIN_SHA = "a" * 40
VALIDATED_SHA = "b" * 40


class SchedulerPeriodicityTest(unittest.TestCase):
    def registry(self) -> dict:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))

    def test_alpha_factory_and_meta_supervisor_are_distinct_schedulers(self):
        by_id = {item["id"]: item for item in self.registry()["schedulers"]}
        alpha = by_id["alpha-factory"]
        meta = by_id["meta-supervisor"]
        self.assertEqual(alpha["workflow"], ".github/workflows/alpha-factory.yml")
        self.assertEqual(alpha["workflow_name"], "Polymarket Alpha Factory")
        self.assertEqual(alpha["job"], "evaluate")
        self.assertEqual(meta["workflow"], ".github/workflows/control-plane.yml")
        self.assertEqual(meta["workflow_name"], "Polymarket Meta-Supervisor")
        self.assertEqual(meta["job"], "coordinate")
        self.assertNotEqual(alpha["workflow"], meta["workflow"])

    def test_every_registered_scheduler_has_an_explicit_timer(self):
        registry = self.registry()
        for scheduler in registry["schedulers"]:
            with self.subTest(scheduler=scheduler["id"]):
                self.assertIsInstance(scheduler["cron"], str)
                self.assertTrue(scheduler["cron"].strip())
                self.assertIsInstance(scheduler["max_staleness_minutes"], int)
                self.assertGreaterEqual(scheduler["max_staleness_minutes"], 5)
                self.assertIsInstance(scheduler["meta_dispatch"], bool)
                self.assertIsInstance(scheduler["recovery_dispatch_authority"], bool)
                workflow = ROOT / scheduler["workflow"]
                text = workflow.read_text(encoding="utf-8")
                self.assertIn("  schedule:\n", text)
                self.assertIn(f'- cron: "{scheduler["cron"]}"', text)

    def test_recovery_dispatch_authority_is_unique(self):
        registry = self.registry()
        owners = [
            scheduler["id"]
            for scheduler in registry["schedulers"]
            if scheduler["recovery_dispatch_authority"]
        ]
        self.assertEqual(owners, ["meta-supervisor"])

        meta_item = next(item for item in registry["schedulers"] if item["id"] == "meta-supervisor")
        meta = (ROOT / meta_item["workflow"]).read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', meta)
        self.assertIn("gh workflow run", meta)
        self.assertIn("scripts/scheduler_meta_supervisor.py", meta)
        self.assertNotIn("gh pr merge", meta)
        self.assertNotIn("git/refs/heads/paper-validated", meta)
        self.assertNotIn("POLYMARKET_DEPLOY_REF=", meta)
        self.assertIn("integration-merge.yml", meta)
        self.assertIn("deploy-paper-server.yml", meta)
        self.assertIn("server-health.yml", meta)
        self.assertIn("forbidden", meta)

    def test_reconciliation_only_workflows_are_periodic_and_fail_closed(self):
        post_merge = (ROOT / ".github" / "workflows" / "post-merge-validation.yml").read_text(
            encoding="utf-8"
        )
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "1,11,21,31,41,51 * * * *"', post_merge)
        self.assertIn("required: false", post_merge)
        self.assertIn("VALIDATION_REQUIRED", post_merge)
        self.assertIn("no_unvalidated_main_revision", post_merge)
        self.assertIn('cron: "22,52 * * * *"', deploy)
        self.assertIn("github.event_name == 'schedule'", deploy)
        self.assertIn("vars.POLYMARKET_SERVER_DEPLOY == 'true'", deploy)

    def run_planner(
        self,
        runs: list[dict],
        *,
        relation: str,
        validated_sha: str,
        deploy_enabled: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runs_path = temp / "runs.json"
            plan_path = temp / "plan.json"
            report_path = temp / "report.md"
            runs_path.write_text(json.dumps(runs), encoding="utf-8")
            completed = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--registry",
                    str(REGISTRY),
                    "--runs",
                    str(runs_path),
                    "--main-sha",
                    MAIN_SHA,
                    "--validated-sha",
                    validated_sha,
                    "--validation-relation",
                    relation,
                    "--deploy-enabled",
                    "true" if deploy_enabled else "false",
                    "--now",
                    NOW,
                    "--plan",
                    str(plan_path),
                    "--markdown",
                    str(report_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            return completed, plan

    def fresh_runs(self) -> list[dict]:
        runs: list[dict] = []
        for scheduler in self.registry()["schedulers"]:
            if scheduler["id"] == "meta-supervisor":
                continue
            runs.append(
                {
                    "workflowName": scheduler["workflow_name"],
                    "status": "completed",
                    "conclusion": "success",
                    "headBranch": "main",
                    "headSha": MAIN_SHA,
                    "event": "schedule",
                    "createdAt": "2026-08-24T14:59:00Z",
                    "url": "https://example.invalid/run",
                }
            )
        return runs

    def test_fresh_current_control_plane_requires_no_recovery(self):
        completed, plan = self.run_planner(
            self.fresh_runs(), relation="current", validated_sha=MAIN_SHA
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(plan, [])
        self.assertIn("recovery dispatches planned: 0", completed.stdout)

    def test_missing_validators_are_dispatched_for_exact_main_sha(self):
        missing = {"ci", "monitoring"}
        runs = [run for run in self.fresh_runs() if run["workflowName"] not in missing]
        completed, plan = self.run_planner(runs, relation="current", validated_sha=MAIN_SHA)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        by_id = {item["id"]: item for item in plan}
        self.assertEqual(by_id["code-validation"]["inputs"]["expected_sha"], MAIN_SHA)
        self.assertEqual(by_id["monitoring-validation"]["inputs"]["expected_sha"], MAIN_SHA)
        self.assertNotIn("post-merge-validation", by_id)

    def test_pending_main_dispatches_post_merge_reconciliation(self):
        runs = [
            run
            for run in self.fresh_runs()
            if run["workflowName"] != "Polymarket Post-Merge Validation"
        ]
        completed, plan = self.run_planner(
            runs,
            relation="pending_validation",
            validated_sha=VALIDATED_SHA,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        by_id = {item["id"]: item for item in plan}
        self.assertEqual(
            by_id["post-merge-validation"]["inputs"]["expected_sha"], MAIN_SHA
        )
        self.assertNotIn("integration-merge", by_id)

    def test_diverged_validation_state_fails_closed(self):
        completed, plan = self.run_planner(
            self.fresh_runs(),
            relation="diverged",
            validated_sha=VALIDATED_SHA,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("main and paper-validated have diverged", completed.stdout)
        self.assertIsInstance(plan, list)


if __name__ == "__main__":
    unittest.main()
