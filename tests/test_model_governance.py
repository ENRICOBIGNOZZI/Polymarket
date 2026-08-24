from __future__ import annotations

import json
import py_compile
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "scheduler_registry.json"
WORKFLOWS = ROOT / ".github" / "workflows"


class ModelGovernanceContractTest(unittest.TestCase):
    def test_live_runtime_uses_automatic_validated_promotion_policy(self):
        manifest = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["version"], 5)
        self.assertEqual(manifest["loop"], "scripts/paper_v5_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v5.json")
        self.assertEqual(manifest["run_root"], "runs/paper_v5_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertEqual(manifest["promotion_policy"], "automatic validated integration")

        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn("automatic validated integration", selector)
        self.assertIn("--print-champion", selector)
        completed = subprocess.run(
            ["bash", "scripts/paper_latest_loop.sh", "--print-champion"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn("paper_champion version=5", completed.stdout)
        self.assertIn("deploy_ref=paper-validated", completed.stdout)

    def test_registry_covers_workflows_and_preserves_separate_authorities(self):
        completed = subprocess.run(
            [
                "python3",
                "scripts/validate_scheduler_registry.py",
                "--root",
                ".",
                "--registry",
                "config/scheduler_registry.json",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Registry and one-job-per-workflow contract are valid", completed.stdout)

        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        schedulers = registry["schedulers"]
        ids = [item["id"] for item in schedulers]
        self.assertIn("promotion-controller", ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([item["id"] for item in schedulers if item["merge_authority"]], ["integration-merge"])
        self.assertEqual([item["id"] for item in schedulers if item["deploy_authority"]], ["paper-server-deploy"])
        self.assertEqual(
            [item["id"] for item in schedulers if item["validation_dispatch_authority"]],
            ["post-merge-validation"],
        )

    def test_promotion_controller_decides_and_integration_scheduler_only_executes(self):
        controller = (WORKFLOWS / "promotion-controller.yml").read_text(encoding="utf-8")
        integration = (WORKFLOWS / "integration-merge.yml").read_text(encoding="utf-8")
        meta = (WORKFLOWS / "control-plane.yml").read_text(encoding="utf-8")

        self.assertIn('cron: "1,16,31,46 * * * *"', controller)
        self.assertIn("scripts/promotion_gate.py", controller)
        self.assertIn("--add-label autonomous-promotion-approved", controller)
        self.assertIn("--remove-label autonomous-promotion-approved", controller)
        self.assertIn("source-match-files.txt", controller)
        self.assertIn("economic_source_content_mismatch", controller)
        self.assertNotIn("gh pr merge", controller)
        self.assertNotIn("administrator-approved", controller)

        self.assertIn('cron: "3,18,33,48 * * * *"', integration)
        self.assertIn('--label autonomous-promotion-approved', integration)
        self.assertIn("--require-approval-label", integration)
        self.assertIn("scripts/promotion_gate.py", integration)
        self.assertIn('gh pr merge "$PR_NUMBER" --squash --delete-branch', integration)
        self.assertIn("--match-head-commit", integration)
        self.assertIn("baseRefOid", integration)
        self.assertIn("source-match-files.txt", integration)
        self.assertIn("champion-integration-merged", integration)
        self.assertNotIn("administrator-approved", integration)
        self.assertNotIn("incumbent_health_gate.py", integration)
        self.assertNotIn("--admin", integration)

        self.assertIn("promotion-controller.yml", meta)
        dispatch_case = meta.split('case "$workflow" in', 1)[1].split("esac", 1)[0]
        self.assertNotIn("integration-merge.yml", dispatch_case)

    def test_research_policy_keeps_research_isolated_and_allows_machine_label_on_integration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            changed = temp / "changed.txt"
            changed.write_text("src/engine.cpp\n", encoding="utf-8")
            report = temp / "report.md"

            research_event = {
                "pull_request": {
                    "head": {"ref": "research/new-alpha"},
                    "draft": False,
                    "body": "research",
                    "labels": [],
                }
            }
            research_path = temp / "research-event.json"
            research_path.write_text(json.dumps(research_event), encoding="utf-8")
            rejected = subprocess.run(
                [
                    "python3",
                    "scripts/research_pr_policy.py",
                    "--event",
                    str(research_path),
                    "--changed-files",
                    str(changed),
                    "--manifest-existed-on-base",
                    "true",
                    "--output",
                    str(report),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("must remain draft", rejected.stdout)

            integration_event = {
                "pull_request": {
                    "head": {"ref": "integration/new-alpha"},
                    "draft": False,
                    "body": "Source research PR/branch/commit: #123\n",
                    "labels": [{"name": "autonomous-promotion-approved"}],
                }
            }
            integration_path = temp / "integration-event.json"
            integration_path.write_text(json.dumps(integration_event), encoding="utf-8")
            accepted = subprocess.run(
                [
                    "python3",
                    "scripts/research_pr_policy.py",
                    "--event",
                    str(integration_path),
                    "--changed-files",
                    str(changed),
                    "--manifest-existed-on-base",
                    "true",
                    "--output",
                    str(report),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            self.assertIn("manual_approval_labels_required: `False`", accepted.stdout)

    def test_post_merge_and_deployment_still_use_exact_validated_sha(self):
        post_merge = (WORKFLOWS / "post-merge-validation.yml").read_text(encoding="utf-8")
        deploy = (WORKFLOWS / "deploy-paper-server.yml").read_text(encoding="utf-8")
        live_validation = (WORKFLOWS / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("repository_dispatch", post_merge)
        self.assertIn("ci.yml monitoring.yml v4-live-smoke.yml", post_merge)
        self.assertIn('-f expected_sha="$EXPECTED_SHA"', post_merge)
        self.assertNotIn("gh pr merge", post_merge)
        self.assertIn("POLYMARKET_DEPLOY_REF=paper-validated", deploy)
        self.assertIn('test "$validated_sha" = "$main_sha"', live_validation)
        self.assertIn('-f sha="$validated_sha" -F force=false', live_validation)

    def test_scheduler_scripts_compile(self):
        for relative in (
            "scripts/validate_scheduler_registry.py",
            "scripts/research_pr_policy.py",
            "scripts/research_queue_report.py",
            "scripts/integration_gate.py",
            "scripts/promotion_gate.py",
            "scripts/admin_supervisor_report.py",
        ):
            py_compile.compile(str(ROOT / relative), doraise=True)


if __name__ == "__main__":
    unittest.main()
