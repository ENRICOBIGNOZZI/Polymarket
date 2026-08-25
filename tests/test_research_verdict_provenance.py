from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ResearchVerdictProvenanceTest(unittest.TestCase):
    def run_policy(self, source: dict) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            event = {
                "pull_request": {
                    "head": {"ref": "integration/test-alpha"},
                    "draft": True,
                    "body": "Source research PR/branch/commit: #321\n",
                    "labels": [],
                }
            }
            event_path = temp / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            changed_path = temp / "changed.txt"
            changed_path.write_text("src/engine.cpp\n", encoding="utf-8")
            source_path = temp / "source.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            report_path = temp / "report.md"
            return subprocess.run(
                [
                    "python3",
                    "scripts/research_pr_policy.py",
                    "--event",
                    str(event_path),
                    "--changed-files",
                    str(changed_path),
                    "--manifest-existed-on-base",
                    "true",
                    "--output",
                    str(report_path),
                    "--source-research-json",
                    str(source_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

    @staticmethod
    def source(*, body: str = "", comments: list[dict] | None = None, reviews: list[dict] | None = None) -> dict:
        return {
            "number": 321,
            "head": {"ref": "research/test-alpha"},
            "body": body,
            "comments": comments or [],
            "reviews": reviews or [],
        }

    def test_source_body_cannot_self_approve(self):
        result = self.run_policy(
            self.source(body="## Decision\nAPPROVED_FOR_INTEGRATION\n")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest trusted source verdict: none", result.stdout)

    def test_untrusted_comment_cannot_approve(self):
        result = self.run_policy(
            self.source(
                comments=[
                    {
                        "created_at": "2026-08-25T05:00:00Z",
                        "author_association": "NONE",
                        "body": "## Research Governance — APPROVED_FOR_INTEGRATION",
                    }
                ]
            )
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest trusted source verdict: none", result.stdout)

    def test_prospective_approval_mention_does_not_override_governance_state(self):
        result = self.run_policy(
            self.source(
                comments=[
                    {
                        "created_at": "2026-08-25T05:00:00Z",
                        "author_association": "OWNER",
                        "body": (
                            "## Research Governance — `MORE_EVIDENCE_REQUIRED`\n\n"
                            "Before any APPROVED_FOR_INTEGRATION decision, collect more OOS evidence."
                        ),
                    }
                ]
            )
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source_research_verdict: `MORE_EVIDENCE_REQUIRED`", result.stdout)
        self.assertIn("latest trusted source verdict: MORE_EVIDENCE_REQUIRED", result.stdout)

    def test_trusted_structured_governance_comment_can_approve(self):
        result = self.run_policy(
            self.source(
                body="This body mentions REJECTED and MORE_EVIDENCE_REQUIRED as historical states.",
                comments=[
                    {
                        "created_at": "2026-08-25T05:00:00Z",
                        "author_association": "OWNER",
                        "body": "## Research Governance — `APPROVED_FOR_INTEGRATION`\n\nObjective evidence gates passed.",
                    }
                ],
            )
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", result.stdout)
        self.assertIn("policy: `pass`", result.stdout)

    def test_latest_trusted_structured_verdict_wins(self):
        result = self.run_policy(
            self.source(
                comments=[
                    {
                        "created_at": "2026-08-25T04:00:00Z",
                        "author_association": "COLLABORATOR",
                        "body": "Research Governance — APPROVED_FOR_INTEGRATION",
                    },
                    {
                        "created_at": "2026-08-25T05:00:00Z",
                        "author_association": "OWNER",
                        "body": "Research Governance correction — MORE_EVIDENCE_REQUIRED",
                    },
                ]
            )
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source_research_verdict: `MORE_EVIDENCE_REQUIRED`", result.stdout)


if __name__ == "__main__":
    unittest.main()
