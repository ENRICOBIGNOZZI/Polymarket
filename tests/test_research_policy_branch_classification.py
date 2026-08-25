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
            temp = Path(temp_dir); event = {"pull_request":{"head":{"ref":branch},"draft":draft,"body":body,"labels":[{"name":label} for label in (labels or [])]}}
            event_path = temp / "event.json"; changed_path = temp / "changed.txt"; report_path = temp / "report.md"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            changed_path.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
            command = ["python3",str(POLICY),"--event",str(event_path),"--changed-files",str(changed_path),"--manifest-existed-on-base","true","--output",str(report_path)]
            if source_research is not None:
                source_path = temp / "source-research.json"
                source_path.write_text(json.dumps(source_research), encoding="utf-8")
                command.extend(["--source-research-json", str(source_path)])
            return subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True, timeout=10)

    def test_feature_branch_cannot_modify_live_model_runtime(self):
        result = self.run_policy("feature/all-market-engine","Expand alpha opportunity scanning and candidate bundle capacity for the paper champion.",["scripts/paper_v4_loop.sh","scripts/build_global_opportunity_book.py"])
        self.assertNotEqual(result.returncode, 0); self.assertIn("model/runtime work cannot change", result.stdout); self.assertIn("scripts/paper_v4_loop.sh", result.stdout)

    def test_feature_branch_cannot_modify_live_b2_filter(self):
        result = self.run_policy("fix/coherent-hedge-selection","Change B2 hedge coherence filtering before intent generation.",["scripts/filter_coherent_hedges.py"])
        self.assertNotEqual(result.returncode, 0); self.assertIn("model/runtime work cannot change", result.stdout)

    def test_feature_branch_cannot_modify_v6_queue_filter(self):
        result = self.run_policy("fix/v6-queue-admission","Adjust passive queue admission before broker intents.",["scripts/v6_queue_filter.py"])
        self.assertNotEqual(result.returncode, 0); self.assertIn("model/runtime work cannot change", result.stdout); self.assertIn("scripts/v6_queue_filter.py", result.stdout)

    def test_feature_branch_cannot_hide_direct_model_source_change(self):
        result = self.run_policy("feature/pca-residual-upgrade","Improve PCA stat-arb alpha and residual model signals.",["src/pca_stat_arb.cpp","tests/test_v4_research.py"])
        self.assertNotEqual(result.returncode, 0); self.assertIn("src/pca_stat_arb.cpp", result.stdout)

    def test_feature_branch_cannot_hide_engine_strategy_change(self):
        result = self.run_policy("feature/engine-opportunity-ranking","Change strategy opportunity ranking inside the portfolio engine.",["src/engine.cpp"])
        self.assertNotEqual(result.returncode, 0); self.assertIn("src/engine.cpp", result.stdout)

    def test_opaque_alpha_bootstrap_cannot_hide_on_feature_branch(self):
        result = self.run_policy("feature/hourly-alpha-council","Bootstrap alpha model research and paper champion candidates.",[".github/workflows/bootstrap-hourly-alpha-council.yml","ops/hourly-alpha-council-payload.b64"])
        self.assertNotEqual(result.returncode, 0); self.assertIn("opaque bootstrap payload", result.stdout)

    def test_shadow_isolated_label_cannot_touch_production_surfaces(self):
        result = self.run_policy("research/shadow-probe","Measurement-only shadow instrumentation.",["scripts/build_v4_intents.py","src/execution.cpp"],labels=["shadow-isolated"],draft=False)
        self.assertNotEqual(result.returncode, 0); self.assertIn("shadow-isolated code cannot modify production", result.stdout)

    def test_shadow_isolated_label_cannot_touch_v6_queue_filter(self):
        result = self.run_policy("research/v6-queue-shadow","Measurement-only queue research.",["scripts/v6_queue_filter.py"],labels=["shadow-isolated"],draft=True)
        self.assertNotEqual(result.returncode, 0); self.assertIn("shadow-isolated code cannot modify production", result.stdout); self.assertIn("scripts/v6_queue_filter.py", result.stdout)

    def test_shadow_isolated_label_cannot_touch_oos_or_realized_pnl_evidence(self):
        result = self.run_policy("research/shadow-oos-report","Measurement-only shadow instrumentation.",["scripts/walk_forward_v4.py","scripts/runtime_action_report.py"],labels=["shadow-isolated"],draft=False)
        self.assertNotEqual(result.returncode, 0); self.assertIn("scripts/walk_forward_v4.py", result.stdout); self.assertIn("scripts/runtime_action_report.py", result.stdout)

    def test_non_draft_integration_requires_numbered_and_approved_source(self):
        rejected = self.run_policy("integration/alpha","No numbered provenance yet.",["config/paper_v5.json"],labels=[],draft=False)
        self.assertNotEqual(rejected.returncode, 0); self.assertIn("numbered source research PR", rejected.stdout)
        body = "Source research PR/branch/commit: #123\n"
        missing_source = self.run_policy("integration/alpha",body,["config/paper_v5.json"],labels=["autonomous-promotion-approved"],draft=False)
        self.assertNotEqual(missing_source.returncode, 0); self.assertIn("source research metadata", missing_source.stdout)
        approved_source = {
            "number": 123,
            "headRefName": "research/alpha",
            "body": "research candidate",
            "comments": [{"createdAt":"2026-08-25T00:00:00Z","authorAssociation":"OWNER","body":"Research Governance — APPROVED_FOR_INTEGRATION"}],
            "reviews": [],
        }
        accepted = self.run_policy("integration/alpha",body,["config/paper_v5.json"],labels=["autonomous-promotion-approved"],draft=False,source_research=approved_source)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", accepted.stdout)
        self.assertIn("automatic_paper_promotion: `True`", accepted.stdout)
        self.assertIn("manual_approval_labels_required: `False`", accepted.stdout)

    def test_data_transport_fix_is_not_misclassified_as_model_work(self):
        result = self.run_policy("fix/api-data-freshness","Fail stale shadow market data before accepting research evidence.",["src/http.cpp","scripts/validate_fast_data_health.py"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr); self.assertIn("policy: `pass`", result.stdout)

    def test_normal_infrastructure_change_remains_allowed(self):
        result = self.run_policy("fix/document-health-output","Improve scheduler health report formatting.",["docs/SCHEDULER_CONTROL_PLANE.md"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr); self.assertIn("policy: `pass`", result.stdout)


if __name__ == "__main__":
    unittest.main()
