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
    def test_live_runtime_is_exactly_v7(self) -> None:
        manifest = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["version"], 7)
        self.assertEqual(manifest["loop"], "scripts/paper_v7_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v7.json")
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertEqual(manifest["promotion_policy"], "automatic validated integration")
        self.assertFalse((ROOT / "scripts" / "paper_latest_loop.sh").exists())
        self.assertFalse((ROOT / "config" / "v7_champion_candidate.json").exists())

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
        self.assertIn("Status: **PASS**", completed.stdout)
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(registry["administrator"]["paper_promotion_mode"], "automatic_objective_gates")
        self.assertFalse(registry["administrator"]["manual_approval_required"])
        schedulers = registry["schedulers"]
        ids = [item["id"] for item in schedulers]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([item["id"] for item in schedulers if item["merge_authority"]], ["integration-merge"])
        self.assertEqual([item["id"] for item in schedulers if item["deploy_authority"]], ["paper-server-deploy"])
        self.assertEqual([item["id"] for item in schedulers if item["validation_dispatch_authority"]], ["post-merge-validation"])
        self.assertIn("live-paper-validation", ids)
        self.assertIn("v7-market-cache-relay", ids)
        self.assertNotIn("v7-unified-paper-evidence", ids)

    def test_workflows_never_push_directly_to_main(self) -> None:
        offenders = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if "git push origin HEAD:main" in text or "git push origin main" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"direct main mutation workflows: {offenders}")

    def test_retired_runtime_surfaces_are_absent(self) -> None:
        retired = [
            "config/paper_v3.json",
            "config/paper_v4.json",
            "config/paper_v5.json",
            "config/paper_v6.json",
            "monitoring/exporter_v4.py",
            "monitoring/exporter_v5.py",
            "monitoring/exporter_v6.py",
            "monitoring/exporter_latest.py",
            "scripts/paper_latest_loop.sh",
            ".github/workflows/v4-live-smoke.yml",
            ".github/workflows/v6-research-smoke.yml",
            ".github/workflows/v6-market-cache-relay.yml",
            ".github/workflows/v7-unified-paper-evidence.yml",
            "config/v7_evidence_runtime.json",
            "config/v7_champion_candidate.json",
            "scripts/tiny_live_pilot.py",
            "scripts/filter_coherent_hedges.py",
            "scripts/summarize_live_smoke.py",
        ]
        present = [path for path in retired if (ROOT / path).exists()]
        self.assertEqual(present, [])

    def test_promotion_controller_decides_and_integration_scheduler_only_executes(self) -> None:
        controller = (WORKFLOWS / "promotion-controller.yml").read_text(encoding="utf-8")
        integration = (WORKFLOWS / "integration-merge.yml").read_text(encoding="utf-8")
        bridge = (WORKFLOWS / "control-plane-event-bridge.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "1,16,31,46 * * * *"', controller)
        self.assertIn("scripts/promotion_gate.py", controller)
        self.assertNotIn("gh pr merge", controller)
        self.assertIn('cron: "3,18,33,48 * * * *"', integration)
        self.assertIn('gh pr merge "$PR_NUMBER" --squash --delete-branch', integration)
        self.assertIn("--match-head-commit", integration)
        self.assertIn("champion-integration-merged", integration)
        self.assertIn("gh workflow run promotion-controller.yml --ref main", bridge)
        self.assertIn("gh workflow run integration-merge.yml --ref main", bridge)
        self.assertNotIn("pulls/546", bridge)
        self.assertNotIn("v7-unified-paper-evidence", bridge)

    def test_research_policy_keeps_research_isolated_and_machine_promotion_on_integration_only(self) -> None:
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
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
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
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", accepted.stdout)
            self.assertIn("manual_approval_labels_required: `False`", accepted.stdout)

    def test_post_merge_and_deployment_use_exact_v7_validated_sha(self) -> None:
        post_merge = (WORKFLOWS / "post-merge-validation.yml").read_text(encoding="utf-8")
        deploy = (WORKFLOWS / "deploy-paper-server.yml").read_text(encoding="utf-8")
        live_validation = (WORKFLOWS / "v7-live-paper-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("repository_dispatch", post_merge)
        self.assertIn("ci.yml monitoring.yml v7-live-paper-smoke.yml", post_merge)
        self.assertIn('-f expected_sha="$EXPECTED_SHA"', post_merge)
        self.assertNotIn("gh pr merge", post_merge)
        self.assertIn("POLYMARKET_DEPLOY_REF=paper-validated", deploy)
        self.assertIn('workflows: ["V7 live PAPER smoke"]', deploy)
        self.assertIn('test "$validated_sha" = "$main_sha"', live_validation)
        self.assertIn('-f sha="$validated_sha" -F force=false', live_validation)

    def test_scheduler_scripts_compile(self) -> None:
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
