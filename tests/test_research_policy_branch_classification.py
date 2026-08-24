from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "research_pr_policy.py"


class ResearchPolicyBranchClassificationTest(unittest.TestCase):
    def run_policy(self, branch: str, body: str, changed_files: list[str], labels: list[str] | None = None, draft: bool = False):
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
            return subprocess.run(
                [
                    "python3",
                    str(POLICY),
                    "--event",
                    str(event_path),
                    "--changed-files",
                    str(changed_path),
                    "--manifest-existed-on-base",
                    "true",
                    "--output",
                    str(report_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_feature_branch_cannot_modify_live_model_runtime(self):
        result = self.run_policy(
            "feature/all-market-engine",
            "Expand alpha opportunity scanning and candidate bundle capacity for the paper champion.",
            ["scripts/paper_v4_loop.sh", "scripts/build_global_opportunity_book.py"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unapproved model/runtime work", result.stdout)
        self.assertIn("scripts/paper_v4_loop.sh", result.stdout)

    def test_feature_branch_cannot_hide_direct_model_source_change(self):
        result = self.run_policy(
            "feature/pca-residual-upgrade",
            "Improve PCA stat-arb alpha and residual model signals.",
            ["src/pca_stat_arb.cpp", "tests/test_v4_research.py"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unapproved model/runtime work", result.stdout)
        self.assertIn("src/pca_stat_arb.cpp", result.stdout)

    def test_feature_branch_cannot_hide_engine_strategy_change(self):
        result = self.run_policy(
            "feature/engine-opportunity-ranking",
            "Change strategy opportunity ranking inside the portfolio engine.",
            ["src/engine.cpp"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("src/engine.cpp", result.stdout)

    def test_opaque_alpha_bootstrap_cannot_hide_on_feature_branch(self):
        result = self.run_policy(
            "feature/hourly-alpha-council",
            "Bootstrap alpha model research and paper champion candidates.",
            [
                ".github/workflows/bootstrap-hourly-alpha-council.yml",
                "ops/hourly-alpha-council-payload.b64",
            ],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("opaque bootstrap payload", result.stdout)

    def test_shadow_isolated_label_cannot_touch_production_surfaces(self):
        result = self.run_policy(
            "research/shadow-probe",
            "Measurement-only shadow instrumentation.",
            ["scripts/build_v4_intents.py", "src/execution.cpp"],
            labels=["shadow-isolated"],
            draft=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shadow-isolated code cannot modify production", result.stdout)

    def test_data_transport_fix_is_not_misclassified_as_model_work(self):
        result = self.run_policy(
            "fix/api-data-freshness",
            "Fail stale shadow market data before accepting research evidence.",
            ["src/http.cpp", "scripts/validate_fast_data_health.py"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("policy: `pass`", result.stdout)

    def test_normal_infrastructure_change_remains_allowed(self):
        result = self.run_policy(
            "fix/document-health-output",
            "Improve scheduler health report formatting.",
            ["docs/SCHEDULER_CONTROL_PLANE.md"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("policy: `pass`", result.stdout)


if __name__ == "__main__":
    unittest.main()
