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
        version = int(manifest["version"])
        self.assertIn(version, (5, 6))
        self.assertEqual(manifest["loop"], f"scripts/paper_v{version}_loop.sh")
        self.assertEqual(manifest["config"], f"config/paper_v{version}.json")
        self.assertEqual(manifest["run_root"], f"runs/paper_v{version}_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertEqual(manifest["promotion_policy"], "automatic validated integration")
        completed = subprocess.run(["bash", "scripts/paper_latest_loop.sh", "--print-champion"], cwd=ROOT, check=True, capture_output=True, text=True, timeout=10)
        self.assertIn(f"paper_champion version={version}", completed.stdout)
        self.assertIn("deploy_ref=paper-validated", completed.stdout)

    def test_registry_covers_workflows_and_preserves_separate_authorities(self):
        completed = subprocess.run(["python3","scripts/validate_scheduler_registry.py","--root",".","--registry","config/scheduler_registry.json"], cwd=ROOT, check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Registry and one-job-per-workflow contract are valid", completed.stdout)
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(registry["administrator"]["paper_promotion_mode"], "automatic_objective_gates")
        self.assertFalse(registry["administrator"]["manual_approval_required"])
        schedulers = registry["schedulers"]
        ids = [item["id"] for item in schedulers]
        self.assertIn("promotion-controller", ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([item["id"] for item in schedulers if item["merge_authority"]], ["integration-merge"])
        self.assertEqual([item["id"] for item in schedulers if item["deploy_authority"]], ["paper-server-deploy"])
        self.assertEqual([item["id"] for item in schedulers if item["validation_dispatch_authority"]], ["post-merge-validation"])

    def test_workflows_never_push_directly_to_main(self):
        offenders = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if "git push origin HEAD:main" in text or "git push origin main" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"direct main mutation workflows: {offenders}")

    def test_one_shot_aggressive_mutation_scaffolding_is_absent(self):
        offenders = []
        for pattern in (
            "scripts/apply_aggressive_v5*20260825.py",
            "automation/aggressive_v5_bootstrap*.txt",
        ):
            offenders.extend(
                str(path.relative_to(ROOT)) for path in sorted(ROOT.glob(pattern))
            )
        self.assertEqual(
            offenders,
            [],
            f"one-shot aggressive mutation scaffolding: {offenders}",
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
        self.assertIn("scripts/research_pr_policy.py", integration)
        self.assertIn("source-comments-pages.json", integration)
        self.assertIn("source-reviews-pages.json", integration)
        self.assertIn("--source-research-json integration-merge/source-research.json", integration)
        self.assertIn("integration-merge/policy-event.json", integration)
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
            temp = Path(temp_dir); changed = temp / "changed.txt"; changed.write_text("src/engine.cpp\n", encoding="utf-8"); report = temp / "report.md"
            research_event = {"pull_request":{"head":{"ref":"research/new-alpha"},"draft":False,"body":"research","labels":[]}}
            research_path = temp / "research-event.json"; research_path.write_text(json.dumps(research_event), encoding="utf-8")
            rejected = subprocess.run(["python3","scripts/research_pr_policy.py","--event",str(research_path),"--changed-files",str(changed),"--manifest-existed-on-base","true","--output",str(report)], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0); self.assertIn("must remain draft", rejected.stdout)

            research_labeled_event = {"pull_request":{"head":{"ref":"research/new-alpha"},"draft":True,"body":"research","labels":[{"name":"autonomous-promotion-approved"}]}}
            research_labeled_path = temp / "research-labeled-event.json"; research_labeled_path.write_text(json.dumps(research_labeled_event), encoding="utf-8")
            research_labeled = subprocess.run(["python3","scripts/research_pr_policy.py","--event",str(research_labeled_path),"--changed-files",str(changed),"--manifest-existed-on-base","true","--output",str(report)], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(research_labeled.returncode, 0); self.assertIn("autonomous-promotion-approved", research_labeled.stdout)

            feature_labeled_event = {"pull_request":{"head":{"ref":"fix/not-an-integration"},"draft":False,"body":"governance fix","labels":[{"name":"autonomous-promotion-approved"}]}}
            feature_labeled_path = temp / "feature-labeled-event.json"; feature_labeled_path.write_text(json.dumps(feature_labeled_event), encoding="utf-8")
            feature_labeled = subprocess.run(["python3","scripts/research_pr_policy.py","--event",str(feature_labeled_path),"--changed-files",str(changed),"--manifest-existed-on-base","true","--output",str(report)], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(feature_labeled.returncode, 0); self.assertIn("research/integration labels are valid only", feature_labeled.stdout)

            approved_sha = "0123456789abcdef0123456789abcdef01234567"
            integration_event = {"pull_request":{"head":{"ref":"integration/new-alpha"},"draft":False,"body":f"Source research PR/branch/commit: #123 / research/new-alpha / {approved_sha}\n","labels":[{"name":"autonomous-promotion-approved"}]}}
            integration_path = temp / "integration-event.json"; integration_path.write_text(json.dumps(integration_event), encoding="utf-8")
            source = {
                "number": 123,
                "headRefName": "research/new-alpha",
                "headRefOid": approved_sha,
                "body": "research candidate",
                "comments": [{"createdAt":"2026-08-25T00:00:00Z","authorAssociation":"OWNER","body":f"Research Governance — APPROVED_FOR_INTEGRATION\nExact validated head: `{approved_sha}`"}],
                "reviews": [],
            }
            source_path = temp / "source-research.json"; source_path.write_text(json.dumps(source), encoding="utf-8")
            accepted = subprocess.run(["python3","scripts/research_pr_policy.py","--event",str(integration_path),"--changed-files",str(changed),"--manifest-existed-on-base","true","--output",str(report),"--source-research-json",str(source_path)], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", accepted.stdout)
            self.assertIn("source_research_approved_sha: `" + approved_sha + "`", accepted.stdout)
            self.assertIn("manual_approval_labels_required: `False`", accepted.stdout)

    def test_research_policy_guards_v6_institutional_runtime_surfaces(self):
        sensitive = [
            "config/v6_model_architecture.json",
            "scripts/v6_dynamic_factor_intents.py",
            "scripts/v6_execution_model.py",
            "scripts/v6_global_risk.py",
            "scripts/v6_intent_queue_filter.py",
            "scripts/v6_micro_taker_institutional.py",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            changed = temp / "changed.txt"
            changed.write_text("\n".join(sensitive) + "\n", encoding="utf-8")
            report = temp / "report.md"

            normal_event = {"pull_request":{"head":{"ref":"fix/v6-helper"},"draft":False,"body":"operational fix","labels":[]}}
            normal_path = temp / "normal-event.json"
            normal_path.write_text(json.dumps(normal_event), encoding="utf-8")
            normal = subprocess.run(["python3","scripts/research_pr_policy.py","--event",str(normal_path),"--changed-files",str(changed),"--manifest-existed-on-base","true","--output",str(report)], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(normal.returncode, 0, normal.stdout + normal.stderr)
            for path in sensitive:
                self.assertIn(path, normal.stdout)

            research_event = {"pull_request":{"head":{"ref":"research/v6-institutional"},"draft":True,"body":"paper-only model research","labels":[]}}
            research_path = temp / "research-event.json"
            research_path.write_text(json.dumps(research_event), encoding="utf-8")
            research = subprocess.run(["python3","scripts/research_pr_policy.py","--event",str(research_path),"--changed-files",str(changed),"--manifest-existed-on-base","true","--output",str(report)], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)
            self.assertEqual(research.returncode, 0, research.stdout + research.stderr)

            shadow_event = {"pull_request":{"head":{"ref":"research/v6-institutional-shadow"},"draft":False,"body":"measurement-only research","labels":[{"name":"shadow-isolated"}]}}
            shadow_path = temp / "shadow-event.json"
            shadow_path.write_text(json.dumps(shadow_event), encoding="utf-8")
            shadow = subprocess.run(["python3","scripts/research_pr_policy.py","--event",str(shadow_path),"--changed-files",str(changed),"--manifest-existed-on-base","true","--output",str(report)], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(shadow.returncode, 0, shadow.stdout + shadow.stderr)
            self.assertIn("shadow-isolated code cannot modify", shadow.stdout)

    def test_research_policy_runs_on_every_main_push_and_rejects_direct_pushes(self):
        policy = (WORKFLOWS / "research-policy.yml").read_text(encoding="utf-8")
        push_block = policy.split("  push:\n", 1)[1].split("  pull_request:\n", 1)[0]
        self.assertIn("branches: [main]", push_block)
        self.assertNotIn("paths:", push_block)
        self.assertIn("Enforce pull-request provenance for main pushes", policy)
        self.assertIn("if: github.event_name == 'push'", policy)
        self.assertIn("commits/${GITHUB_SHA}/pulls", policy)
        self.assertIn('.base.ref == "main"', policy)
        self.assertIn('.merged_at != null', policy)
        self.assertIn("has no merged pull-request provenance", policy)

    def test_post_merge_and_deployment_still_use_exact_validated_sha(self):
        post_merge = (WORKFLOWS / "post-merge-validation.yml").read_text(encoding="utf-8")
        deploy = (WORKFLOWS / "deploy-paper-server.yml").read_text(encoding="utf-8")
        live_validation = (WORKFLOWS / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("repository_dispatch", post_merge); self.assertIn("ci.yml monitoring.yml v4-live-smoke.yml", post_merge)
        self.assertIn('-f expected_sha="$EXPECTED_SHA"', post_merge); self.assertNotIn("gh pr merge", post_merge)
        self.assertIn("POLYMARKET_DEPLOY_REF=paper-validated", deploy)
        self.assertIn('test "$validated_sha" = "$main_sha"', live_validation); self.assertIn('-f sha="$validated_sha" -F force=false', live_validation)

    def test_scheduler_scripts_compile(self):
        for relative in ("scripts/validate_scheduler_registry.py","scripts/research_pr_policy.py","scripts/research_queue_report.py","scripts/integration_gate.py","scripts/promotion_gate.py","scripts/admin_supervisor_report.py"):
            py_compile.compile(str(ROOT / relative), doraise=True)


if __name__ == "__main__":
    unittest.main()
