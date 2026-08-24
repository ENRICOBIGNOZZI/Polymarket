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

    def test_system_watch_defines_distributed_administration(self):
        policy = (ROOT / "docs" / "SYSTEM_WATCH.md").read_text(encoding="utf-8")
        required_phrases = (
            "control plane",
            "one live champion",
            "research -> evidence -> approval -> integration -> validation -> single live champion",
            "no individual scheduler owns the entire chain",
            "administrator-approved",
            "Keep unapproved research isolated",
            "Shadow-only exception",
            "integration/*",
            "main == paper-validated == deployed HEAD",
            "one shared model registry/orchestrator",
            "at most one administrator-approved integration PR at a time",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, policy)

        control_plane = (ROOT / "docs" / "SCHEDULER_CONTROL_PLANE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("one bounded responsibility", control_plane)
        self.assertIn("Administrator Supervisor observes every node", control_plane)
        self.assertIn("One scheduler, one responsibility", control_plane)
        self.assertIn("administrator-approved", control_plane)

    def test_development_policy_requires_admin_approval_and_separate_handoffs(self):
        development = (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
        self.assertIn("Unapproved research belongs on `research/*`", development)
        self.assertIn("Approval of a research result does not authorize merging the research branch", development)
        self.assertIn("`integration/*`: the only branch class", development)
        self.assertIn("`config/live_champion.json` selects one loop", development)
        self.assertIn("administrator-approved", development)
        self.assertIn("integration scheduler performs only the merge", development)
        self.assertIn("separate post-merge scheduler", development)
        self.assertIn("one top-level job", development)

    def test_pull_request_template_requires_admin_and_scheduler_boundaries(self):
        template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("Approved research integration into the single champion", template)
        self.assertIn("Source research PR/branch/commit", template)
        self.assertIn("The candidate is connected to the existing expert/signal/intent interfaces", template)
        self.assertIn("There remains one model orchestrator/registry", template)
        self.assertIn("Duplicated or superseded implementation", template)
        self.assertIn("## Administrator approval", template)
        self.assertIn("administrator-approved", template)
        self.assertIn("Only the integration scheduler can merge", template)
        self.assertIn("Integration merge and post-merge validation are handled by separate schedulers", template)
        self.assertIn("main == paper-validated == deployed HEAD", template)

    def test_registry_covers_every_workflow_with_one_job(self):
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
        ids = {item["id"] for item in registry["schedulers"]}
        self.assertEqual(
            ids,
            {
                "administrator-supervisor",
                "research-policy",
                "research-queue",
                "integration-merge",
                "post-merge-validation",
                "code-validation",
                "monitoring-validation",
                "live-paper-validation",
                "paper-server-deploy",
                "paper-server-health",
                "forward-maker-research",
                "alpha-factory",
                "meta-supervisor",
                "fast-arb-shadow-research",
                "arb-theory-research",
                "live-api-smoke",
            },
        )
        self.assertFalse((WORKFLOWS / "model-governance.yml").exists())

    def test_privileged_authority_is_unique_and_separated(self):
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        schedulers = registry["schedulers"]
        self.assertEqual(
            [item["id"] for item in schedulers if item["merge_authority"]],
            ["integration-merge"],
        )
        self.assertEqual(
            [item["id"] for item in schedulers if item["deploy_authority"]],
            ["paper-server-deploy"],
        )
        self.assertEqual(
            [item["id"] for item in schedulers if item["validation_dispatch_authority"]],
            ["post-merge-validation"],
        )

        admin = (WORKFLOWS / "admin-supervisor.yml").read_text(encoding="utf-8")
        integration = (WORKFLOWS / "integration-merge.yml").read_text(encoding="utf-8")
        post_merge = (WORKFLOWS / "post-merge-validation.yml").read_text(encoding="utf-8")
        deploy = (WORKFLOWS / "deploy-paper-server.yml").read_text(encoding="utf-8")
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        monitoring = (WORKFLOWS / "monitoring.yml").read_text(encoding="utf-8")
        live_validation = (WORKFLOWS / "v4-live-smoke.yml").read_text(encoding="utf-8")

        for forbidden in (
            "gh pr merge",
            "gh workflow run",
            "repository_dispatch",
            "POLYMARKET_DEPLOY_REF=",
            "git push origin paper-validated",
        ):
            self.assertNotIn(forbidden, admin)
        self.assertIn('gh pr merge "$PR_NUMBER" --squash --delete-branch', integration)
        self.assertNotIn("--admin", integration)
        self.assertIn("administrator-approved", integration)
        self.assertNotIn("gh workflow run", integration)
        self.assertIn("BASE_MAIN_SHA", integration)
        self.assertIn("BASE_VALIDATED_SHA", integration)
        self.assertIn("candidate-final.json", integration)
        self.assertIn("--match-head-commit", integration)
        self.assertIn("current_main_after_merge", integration)

        self.assertIn("repository_dispatch", post_merge)
        self.assertIn("ci.yml monitoring.yml v4-live-smoke.yml", post_merge)
        self.assertIn('-f expected_sha="$EXPECTED_SHA"', post_merge)
        self.assertNotIn("gh pr merge", post_merge)
        self.assertIn("POLYMARKET_DEPLOY_REF=paper-validated", deploy)

        for workflow in (ci, monitoring, live_validation):
            self.assertIn("expected_sha:", workflow)
            self.assertIn("VALIDATION_SHA", workflow)
            self.assertIn('test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"', workflow)
        self.assertIn('test "$validated_sha" = "$main_sha"', live_validation)
        self.assertIn('-f sha="$validated_sha" -F force=false', live_validation)

    def test_research_policy_rejects_unisolated_research_and_accepts_admin_integration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            changed = temp / "changed.txt"
            changed.write_text("src/example.cpp\n", encoding="utf-8")
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
                    "body": (
                        "- [x] Approved research integration into the single champion\n"
                        "- Source research PR/branch/commit: #123\n"
                    ),
                    "labels": [
                        {"name": "approved-for-integration"},
                        {"name": "single-model-reviewed"},
                        {"name": "administrator-approved"},
                    ],
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
            self.assertIn("policy: `pass`", accepted.stdout)

    def test_integration_gate_requires_all_labels_and_green_non_skipped_checks(self):
        checks = [
            {
                "__typename": "CheckRun",
                "name": name,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
            for name in (
                "build-test (Release)",
                "build-test (Debug)",
                "live-paper-smoke",
                "validate",
                "enforce",
            )
        ]
        candidate = {
            "number": 123,
            "headRefName": "integration/new-alpha",
            "isDraft": False,
            "labels": [
                {"name": "approved-for-integration"},
                {"name": "single-model-reviewed"},
                {"name": "administrator-approved"},
            ],
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": checks,
            "body": (
                "- [x] Approved research integration into the single champion\n"
                "- Source research PR/branch/commit: #100\n"
            ),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            candidate_path = temp / "candidate.json"
            report = temp / "gate.md"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            completed = subprocess.run(
                [
                    "python3",
                    "scripts/integration_gate.py",
                    "validate",
                    "--candidate",
                    str(candidate_path),
                    "--report",
                    str(report),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("All integration gates passed", completed.stdout)

            candidate["labels"] = [
                {"name": "approved-for-integration"},
                {"name": "single-model-reviewed"},
            ]
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            rejected_label = subprocess.run(
                [
                    "python3",
                    "scripts/integration_gate.py",
                    "validate",
                    "--candidate",
                    str(candidate_path),
                    "--report",
                    str(report),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(rejected_label.returncode, 0)
            self.assertIn("administrator-approved", rejected_label.stdout)

            candidate["labels"] = [
                {"name": "approved-for-integration"},
                {"name": "single-model-reviewed"},
                {"name": "administrator-approved"},
            ]
            candidate["statusCheckRollup"][0]["conclusion"] = "SKIPPED"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            rejected_skip = subprocess.run(
                [
                    "python3",
                    "scripts/integration_gate.py",
                    "validate",
                    "--candidate",
                    str(candidate_path),
                    "--report",
                    str(report),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(rejected_skip.returncode, 0)
            self.assertIn("concluded SKIPPED", rejected_skip.stdout)

    def test_scheduler_scripts_compile(self):
        for relative in (
            "scripts/validate_scheduler_registry.py",
            "scripts/research_pr_policy.py",
            "scripts/research_queue_report.py",
            "scripts/integration_gate.py",
            "scripts/admin_supervisor_report.py",
        ):
            py_compile.compile(str(ROOT / relative), doraise=True)


if __name__ == "__main__":
    unittest.main()
