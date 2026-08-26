from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "research_pr_policy.py"


class ResearchPolicyBranchClassificationTest(unittest.TestCase):
    def run_policy(
        self,
        branch: str,
        body: str,
        changed_files: list[str],
        labels: list[str] | None = None,
        draft: bool = False,
        source_research: dict | None = None,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            event = {
                "pull_request": {
                    "head": {"ref": branch},
                    "draft": draft,
                    "body": body,
                    "labels": [{"name": label} for label in (labels or [])],
                }
            }
            event_path = temp / "event.json"
            changed_path = temp / "changed.txt"
            report_path = temp / "report.md"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            changed_path.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
            command = [
                "python3", str(POLICY),
                "--event", str(event_path),
                "--changed-files", str(changed_path),
                "--manifest-existed-on-base", "true",
                "--output", str(report_path),
            ]
            if source_research is not None:
                source_path = temp / "source-research.json"
                source_path.write_text(json.dumps(source_research), encoding="utf-8")
                command.extend(["--source-research-json", str(source_path)])
            return subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)

    def test_feature_branch_cannot_modify_v7_execution_runtime(self):
        changed = ["scripts/paper_v7_execution_loop.sh", "scripts/v7_micro_maker_worker.py"]
        result = self.run_policy("feature/execution-change", "Change V7 execution.", changed)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model/runtime work cannot change", result.stdout)
        for path in changed:
            self.assertIn(path, result.stdout)

    def test_feature_branch_cannot_modify_v7_model_core(self):
        changed = ["scripts/v7_pca_stat_arb_core.py", "src/pca_stat_arb.cpp"]
        result = self.run_policy("fix/pca-model", "Change PCA alpha.", changed)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model/runtime work cannot change", result.stdout)

    def test_feature_branch_cannot_hide_engine_strategy_change(self):
        result = self.run_policy("feature/engine-opportunity-ranking", "Change strategy opportunity ranking.", ["src/engine.cpp"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("src/engine.cpp", result.stdout)

    def test_opaque_alpha_bootstrap_cannot_hide_on_feature_branch(self):
        result = self.run_policy(
            "feature/hourly-alpha-council",
            "Bootstrap alpha model research and paper champion candidates.",
            [".github/workflows/bootstrap-hourly-alpha-council.yml", "ops/hourly-alpha-council-payload.b64"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("opaque bootstrap payload", result.stdout)

    def test_shadow_isolated_label_cannot_touch_execution_or_pnl_surfaces(self):
        changed = ["scripts/v7_multileg_broker.py", "scripts/runtime_action_report.py"]
        result = self.run_policy("research/shadow-probe", "Measurement-only shadow instrumentation.", changed, labels=["shadow-isolated"], draft=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shadow-isolated code cannot modify production", result.stdout)
        for path in changed:
            self.assertIn(path, result.stdout)

    def test_v7_research_model_surfaces_require_research_lifecycle(self):
        changed = [
            "config/research_v7_pca_stat_arb.json",
            "config/research_v7_local_factor.json",
            "config/research_v7_cross_sectional_rank.json",
            "scripts/v7_pca_stat_arb_core.py",
            "scripts/v7_pca_stat_arb_research.py",
            "scripts/v7_local_factor_core.py",
            "scripts/v7_local_factor_research.py",
            "scripts/v7_cross_sectional_rank.py",
            "scripts/v7_cross_sectional_rank_core.py",
        ]
        blocked = self.run_policy("fix/v7-model-helper", "Adjust V7 model research.", changed)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("model/runtime work cannot change", blocked.stdout)
        research = self.run_policy("research/v7-model-helper", "V7 model research only.", changed, draft=True)
        self.assertEqual(research.returncode, 0, research.stdout + research.stderr)
        self.assertIn("policy: `pass`", research.stdout)

    def test_research_branch_must_remain_draft(self):
        result = self.run_policy("research/v7-alpha", "V7 model research.", ["scripts/v7_pca_stat_arb_research.py"], draft=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must remain draft", result.stdout)

    def test_non_draft_integration_requires_numbered_and_approved_source(self):
        changed = ["config/paper_v7.json"]
        rejected = self.run_policy("integration/alpha", "No numbered provenance yet.", changed, labels=[], draft=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("bind exact source provenance", rejected.stdout)

        approved_sha = "0123456789abcdef0123456789abcdef01234567"
        body = f"Source research PR/branch/commit: #123 / research/alpha / {approved_sha}\n"
        missing_source = self.run_policy("integration/alpha", body, changed, labels=["autonomous-promotion-approved"], draft=False)
        self.assertNotEqual(missing_source.returncode, 0)
        self.assertIn("source research metadata", missing_source.stdout)
        approved_source = {
            "number": 123,
            "headRefName": "research/alpha",
            "headRefOid": approved_sha,
            "body": "research candidate",
            "comments": [{
                "createdAt": "2026-08-25T00:00:00Z",
                "authorAssociation": "OWNER",
                "body": f"Research Governance — APPROVED_FOR_INTEGRATION\nExact validated head: `{approved_sha}`",
            }],
            "reviews": [],
        }
        accepted = self.run_policy("integration/alpha", body, changed, labels=["autonomous-promotion-approved"], draft=False, source_research=approved_source)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", accepted.stdout)
        self.assertIn("source_research_approved_sha: `" + approved_sha + "`", accepted.stdout)
        self.assertIn("automatic_paper_promotion: `True`", accepted.stdout)
        self.assertIn("manual_approval_labels_required: `False`", accepted.stdout)

    def test_autopilot_branches_cannot_redefine_operator_authority(self):
        changed = ["config/operator_directives.json", "scripts/hard_safety_policy.py", "tests/test_v7_authorized_paper_envelope.py"]
        for branch in ("fix/restore-authorized-v7-paper-envelope", "research/reinterpret-v7-policy", "integration/rewrite-operator-policy"):
            result = self.run_policy(branch, "Reinterpret the current V7 authorization.", changed, draft=branch.startswith("research/"))
            self.assertNotEqual(result.returncode, 0, branch)
            self.assertIn("operator authority surfaces may change only on operator/*", result.stdout)

    def test_operator_authority_change_requires_explicit_user_instruction_marker(self):
        changed = ["config/operator_directives.json", "scripts/hard_safety_policy.py"]
        missing = self.run_policy("operator/update-v7-authority", "Update operator policy.", changed)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("must contain the exact marker", missing.stdout)
        allowed = self.run_policy(
            "operator/update-v7-authority",
            "Operator authorization change: latest explicit user instruction\nApply the direct user instruction exactly.",
            changed,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertIn("operator_authorization_marker: `True`", allowed.stdout)
        self.assertIn("policy: `pass`", allowed.stdout)

    def test_operator_authority_branch_cannot_change_live_champion(self):
        result = self.run_policy(
            "operator/update-v7-authority",
            "Operator authorization change: latest explicit user instruction",
            ["config/operator_directives.json", "config/live_champion.json"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operator authority PRs may not change the live champion manifest", result.stdout)

    def test_data_transport_fix_is_not_misclassified_as_model_work(self):
        result = self.run_policy("fix/api-data-freshness", "Fail stale public data.", ["src/http.cpp", "scripts/validate_fast_data_health.py"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("policy: `pass`", result.stdout)

    def test_normal_infrastructure_change_remains_allowed(self):
        result = self.run_policy("fix/document-health-output", "Improve scheduler health report formatting.", ["docs/SCHEDULER_CONTROL_PLANE.md"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("policy: `pass`", result.stdout)

    def test_retired_runtime_names_do_not_appear_in_policy_tests(self):
        text = Path(__file__).read_text(encoding="utf-8")
        for token in ("paper_v4", "paper_v5", "paper_v6", "v6_queue_filter", "filter_coherent_hedges", "walk_forward_v4"):
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
