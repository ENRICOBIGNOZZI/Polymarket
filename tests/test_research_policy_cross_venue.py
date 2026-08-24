from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "research_pr_policy.py"


class CrossVenueResearchPolicyTest(unittest.TestCase):
    def run_policy(self, branch: str, changed_files: list[str], labels: list[str] | None = None, draft: bool = False):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            event = {
                "pull_request": {
                    "head": {"ref": branch},
                    "draft": draft,
                    "body": "cross-venue portfolio arbitrage research",
                    "labels": [{"name": name} for name in (labels or [])],
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

    def test_normal_branch_cannot_bypass_cross_venue_model_and_control_surfaces(self):
        changed = [
            "config/cross_venue.json",
            "config/cross_venue_pairs.csv",
            "config/portfolio_supervisor.json",
            "scripts/portfolio_supervisor.py",
            "scripts/cross_venue_loop.sh",
            "scripts/prediction_market_system_loop.sh",
            "include/pm/cross_venue.hpp",
            "src/cross_venue.cpp",
            "src/cross_venue_runtime/part0.inc",
        ]
        completed = self.run_policy("feature/cross-venue", changed)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unapproved model/runtime work", completed.stdout)
        for path in changed:
            self.assertIn(path, completed.stdout)

    def test_draft_research_branch_may_hold_cross_venue_evidence(self):
        completed = self.run_policy(
            "research/cross-venue",
            ["config/cross_venue.json", "src/cross_venue.cpp"],
            draft=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("policy: `pass`", completed.stdout)

    def test_shadow_label_rejects_portfolio_and_credential_surfaces(self):
        completed = self.run_policy(
            "research/cross-venue-shadow",
            [
                "scripts/portfolio_supervisor.py",
                "scripts/install_cross_venue_credentials.sh",
            ],
            labels=["shadow-isolated"],
            draft=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("shadow-isolated code cannot modify", completed.stdout)
        self.assertIn("scripts/portfolio_supervisor.py", completed.stdout)
        self.assertIn("scripts/install_cross_venue_credentials.sh", completed.stdout)


if __name__ == "__main__":
    unittest.main()
