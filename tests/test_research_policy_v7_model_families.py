from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "research_pr_policy.py"


class V7ModelFamilyPolicyTest(unittest.TestCase):
    SENSITIVE = [
        "scripts/v7_cross_sectional_history.py",
        "scripts/v7_cross_sectional_rank_fast.py",
        "scripts/v7_cross_sectional_rank_forward.py",
        "scripts/v7_cross_sectional_rank_inference.py",
        "scripts/v7_cross_sectional_relative.py",
        "scripts/v7_cross_sectional_tail_relative.py",
        "scripts/v7_local_factor_inference.py",
        "scripts/v7_local_factor_multiplicity.py",
        "scripts/v7_local_factor_pairs.py",
        "scripts/v7_execution_evidence.py",
    ]

    def run_policy(self, branch: str, *, draft: bool, labels: list[str] | None = None):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            event = {
                "pull_request": {
                    "head": {"ref": branch},
                    "draft": draft,
                    "body": "V7 model-family change",
                    "labels": [{"name": name} for name in (labels or [])],
                }
            }
            event_path = temp / "event.json"
            changed_path = temp / "changed.txt"
            report_path = temp / "report.md"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            changed_path.write_text("\n".join(self.SENSITIVE) + "\n", encoding="utf-8")
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

    def test_normal_feature_fix_cannot_change_v7_model_family_helpers(self):
        completed = self.run_policy("fix/v7-model-helper", draft=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("model/runtime work cannot change", completed.stdout)
        for path in self.SENSITIVE:
            self.assertIn(path, completed.stdout)

    def test_draft_research_can_hold_v7_model_family_helpers(self):
        completed = self.run_policy("research/v7-model-helper", draft=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("policy: `pass`", completed.stdout)

    def test_shadow_isolated_cannot_change_v7_model_family_helpers(self):
        completed = self.run_policy(
            "research/v7-model-helper-shadow",
            draft=False,
            labels=["shadow-isolated"],
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("shadow-isolated code cannot modify", completed.stdout)
        for path in self.SENSITIVE:
            self.assertIn(path, completed.stdout)


if __name__ == "__main__":
    unittest.main()
