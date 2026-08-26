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
    def test_live_runtime_is_v7_only_and_paper_only(self) -> None:
        manifest = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(int(manifest["version"]), 7)
        self.assertEqual(manifest["loop"], "scripts/paper_v7_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v7.json")
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertTrue((ROOT / manifest["loop"]).is_file())
        self.assertTrue((ROOT / manifest["config"]).is_file())

    def test_registry_covers_workflows_and_preserves_separate_authorities(self) -> None:
        completed = subprocess.run(
            ["python3", "scripts/validate_scheduler_registry.py", "--root", ".", "--registry", "config/scheduler_registry.json"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Registry and one-job-per-workflow contract are valid", completed.stdout)
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(registry["administrator"]["paper_promotion_mode"], "automatic_objective_gates")
        self.assertFalse(registry["administrator"]["manual_approval_required"])
        schedulers = registry["schedulers"]
        ids = [item["id"] for item in schedulers]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([item["id"] for item in schedulers if item["merge_authority"]], ["integration-merge"])
        self.assertEqual([item["id"] for item in schedulers if item["deploy_authority"]], ["paper-server-deploy"])
        self.assertEqual([item["id"] for item in schedulers if item["validation_dispatch_authority"]], ["post-merge-validation"])
        for forbidden in ("v6-live-data-research", "v6-market-cache-relay", "v7-cross-sectional-ranking-research"):
            self.assertNotIn(forbidden, ids)

    def test_workflows_never_push_directly_to_main(self) -> None:
        offenders = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if "git push origin HEAD:main" in text or "git push origin main" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"direct main mutation workflows: {offenders}")

    def test_legacy_versioned_runtime_surfaces_are_absent(self) -> None:
        forbidden_paths = [
            "config/paper_v3.json", "config/paper_v4.json", "config/paper_v5.json", "config/paper_v6.json",
            "scripts/paper_v4_loop.sh", "scripts/paper_v5_loop.sh", "scripts/paper_v6_loop.sh",
            "scripts/paper_latest_loop.sh", "scripts/multi_strategy_paper.py",
            "monitoring/exporter_v4.py", "monitoring/exporter_v5.py", "monitoring/exporter_v6.py",
            "monitoring/exporter_latest.py", "monitoring/exporter_latest_v7.py",
            ".github/workflows/v4-live-smoke.yml", ".github/workflows/v6-research-smoke.yml",
            ".github/workflows/v6-market-cache-relay.yml", ".github/workflows/v7-cross-sectional-ranking-research.yml",
        ]
        for relative in forbidden_paths:
            self.assertFalse((ROOT / relative).exists(), relative)
        self.assertEqual(list((ROOT / "scripts").glob("v6_*.py")), [])
        self.assertEqual(list((ROOT / "tests").glob("test_v6_*.py")), [])

    def test_promotion_controller_decides_and_integration_scheduler_only_executes(self) -> None:
        controller = (WORKFLOWS / "promotion-controller.yml").read_text(encoding="utf-8")
        integration = (WORKFLOWS / "integration-merge.yml").read_text(encoding="utf-8")
        meta = (WORKFLOWS / "control-plane.yml").read_text(encoding="utf-8")
        for token in ("scripts/promotion_gate.py", "scripts/research_pr_policy.py", "autonomous-promotion-approved", "source-match-files.txt", "economic_source_content_mismatch"):
            self.assertIn(token, controller)
        self.assertNotIn("gh pr merge", controller)
        for token in ('gh pr merge "$PR_NUMBER" --squash --delete-branch', "--match-head-commit", "baseRefOid", "source-match-files.txt", "champion-integration-merged"):
            self.assertIn(token, integration)
        self.assertNotIn("--admin", integration)
        self.assertIn("promotion-controller.yml", meta)
        dispatch_case = meta.split('case "$workflow" in', 1)[1].split("esac", 1)[0]
        self.assertNotIn("integration-merge.yml", dispatch_case)

    def test_research_policy_keeps_research_isolated_and_allows_machine_label_on_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            changed = temp / "changed.txt"
            changed.write_text("src/engine.cpp\n", encoding="utf-8")
            report = temp / "report.md"
            research_event = {"pull_request": {"head": {"ref": "research/new-alpha"}, "draft": False, "body": "research", "labels": []}}
            research_path = temp / "research-event.json"
            research_path.write_text(json.dumps(research_event), encoding="utf-8")
            rejected = subprocess.run(
                ["python3", "scripts/research_pr_policy.py", "--event", str(research_path), "--changed-files", str(changed), "--manifest-existed-on-base", "true", "--output", str(report)],
                cwd=ROOT, check=False, capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("must remain draft", rejected.stdout)

            approved_sha = "0123456789abcdef0123456789abcdef01234567"
            integration_event = {"pull_request": {"head": {"ref": "integration/new-alpha"}, "draft": False, "body": f"Source research PR/branch/commit: #123 / research/new-alpha / {approved_sha}\n", "labels": [{"name": "autonomous-promotion-approved"}]}}
            integration_path = temp / "integration-event.json"
            integration_path.write_text(json.dumps(integration_event), encoding="utf-8")
            source = {
                "number": 123,
                "headRefName": "research/new-alpha",
                "headRefOid": approved_sha,
                "body": "research candidate",
                "comments": [{"createdAt": "2026-08-25T00:00:00Z", "authorAssociation": "OWNER", "body": f"Research Governance — APPROVED_FOR_INTEGRATION\nExact validated head: `{approved_sha}`"}],
                "reviews": [],
            }
            source_path = temp / "source-research.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            accepted = subprocess.run(
                ["python3", "scripts/research_pr_policy.py", "--event", str(integration_path), "--changed-files", str(changed), "--manifest-existed-on-base", "true", "--output", str(report), "--source-research-json", str(source_path)],
                cwd=ROOT, check=False, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", accepted.stdout)

    def test_research_policy_runs_on_every_main_push_and_rejects_direct_pushes(self) -> None:
        policy = (WORKFLOWS / "research-policy.yml").read_text(encoding="utf-8")
        push_block = policy.split("  push:\n", 1)[1].split("  pull_request:\n", 1)[0]
        self.assertIn("branches: [main]", push_block)
        self.assertNotIn("paths:", push_block)
        self.assertIn("Enforce pull-request provenance for main pushes", policy)
        self.assertIn("commits/${GITHUB_SHA}/pulls", policy)
        self.assertIn('.base.ref == "main"', policy)
        self.assertIn('.merged_at != null', policy)

    def test_post_merge_deploy_and_live_validation_are_v7_exact_sha_only(self) -> None:
        post_merge = (WORKFLOWS / "post-merge-validation.yml").read_text(encoding="utf-8")
        deploy = (WORKFLOWS / "deploy-paper-server.yml").read_text(encoding="utf-8")
        live_validation = (WORKFLOWS / "v7-live-paper-validation.yml").read_text(encoding="utf-8")
        self.assertIn("ci.yml monitoring.yml v7-live-paper-validation.yml", post_merge)
        self.assertIn('-f expected_sha="$EXPECTED_SHA"', post_merge)
        self.assertIn('workflows: ["v7-live-paper-validation"]', deploy)
        self.assertNotIn("v4-live-paper-smoke", deploy + post_merge + live_validation)
        self.assertIn('test "$validated_sha" = "$main_sha"', live_validation)
        self.assertIn('-f sha="$validated_sha" -F force=false', live_validation)
        self.assertIn("V7 validation refuses non-V7 champion", live_validation)

    def test_scheduler_scripts_compile(self) -> None:
        for relative in (
            "scripts/validate_scheduler_registry.py", "scripts/research_pr_policy.py", "scripts/research_queue_report.py",
            "scripts/integration_gate.py", "scripts/promotion_gate.py", "scripts/admin_supervisor_report.py",
        ):
            py_compile.compile(str(ROOT / relative), doraise=True)


if __name__ == "__main__":
    unittest.main()
