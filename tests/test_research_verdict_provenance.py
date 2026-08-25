from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40
SOURCE_BRANCH = "research/test-alpha"


class ResearchVerdictProvenanceTest(unittest.TestCase):
    def run_policy(self, source: dict, *, body: str | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            event = {
                "pull_request": {
                    "head": {"ref": "integration/test-alpha"},
                    "draft": True,
                    "body": body or f"Source research PR/branch/commit: #321 / `{SOURCE_BRANCH}` / `{SOURCE_SHA}`\n",
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
    def source(*, body: str = "", comments: list[dict] | None = None, reviews: list[dict] | None = None,
               sha: str = SOURCE_SHA, branch: str = SOURCE_BRANCH) -> dict:
        return {
            "number": 321,
            "head": {"ref": branch, "sha": sha},
            "body": body,
            "comments": comments or [],
            "reviews": reviews or [],
        }

    @staticmethod
    def approved_comment(sha: str = SOURCE_SHA, verdict: str = "APPROVED_FOR_INTEGRATION") -> dict:
        return {
            "created_at": "2026-08-25T05:00:00Z",
            "author_association": "OWNER",
            "body": f"Research Governance — {verdict}\n\nExact validated head: `{sha}`.\n",
        }

    def test_source_body_cannot_self_approve(self):
        result = self.run_policy(self.source(body="## Decision\nAPPROVED_FOR_INTEGRATION\n"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest trusted source verdict: none", result.stdout)

    def test_untrusted_comment_cannot_approve(self):
        result = self.run_policy(
            self.source(
                comments=[
                    {
                        "created_at": "2026-08-25T05:00:00Z",
                        "author_association": "NONE",
                        "body": f"Research Governance — APPROVED_FOR_INTEGRATION\nExact validated head: `{SOURCE_SHA}`.",
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
                            "Research Governance — MORE_EVIDENCE_REQUIRED\n\n"
                            "Before any APPROVED_FOR_INTEGRATION decision, collect more OOS evidence."
                        ),
                    }
                ]
            )
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source_research_verdict: `MORE_EVIDENCE_REQUIRED`", result.stdout)
        self.assertIn("latest trusted source verdict: MORE_EVIDENCE_REQUIRED", result.stdout)

    def test_trusted_structured_governance_comment_can_approve_exact_head(self):
        result = self.run_policy(
            self.source(
                body="This body mentions REJECTED and MORE_EVIDENCE_REQUIRED as historical states.",
                comments=[self.approved_comment()],
            )
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", result.stdout)
        self.assertIn(f"source_research_approved_sha: `{SOURCE_SHA}`", result.stdout)
        self.assertIn("policy: `pass`", result.stdout)

    def test_positive_verdict_without_exact_head_is_not_sufficient(self):
        result = self.run_policy(
            self.source(
                comments=[
                    {
                        "created_at": "2026-08-25T05:00:00Z",
                        "author_association": "OWNER",
                        "body": "Research Governance — APPROVED_FOR_INTEGRATION\nEvidence looks good.",
                    }
                ]
            )
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must bind an exact validated source head SHA", result.stdout)

    def test_source_change_after_approval_invalidates_integration(self):
        changed_sha = "b" * 40
        result = self.run_policy(
            self.source(sha=changed_sha, comments=[self.approved_comment(SOURCE_SHA)]),
            body=f"Source research PR/branch/commit: #321 / `{SOURCE_BRANCH}` / `{changed_sha}`\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source research changed after approval", result.stdout)

    def test_integration_cannot_cite_unapproved_source_commit(self):
        wrong_sha = "c" * 40
        result = self.run_policy(
            self.source(comments=[self.approved_comment()]),
            body=f"Source research PR/branch/commit: #321 / `{SOURCE_BRANCH}` / `{wrong_sha}`\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integration source commit mismatch", result.stdout)

    def test_integration_cannot_cite_wrong_source_branch(self):
        result = self.run_policy(
            self.source(comments=[self.approved_comment()]),
            body=f"Source research PR/branch/commit: #321 / `research/other-alpha` / `{SOURCE_SHA}`\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integration source branch mismatch", result.stdout)

    def test_integration_requires_full_number_branch_commit_provenance(self):
        result = self.run_policy(
            self.source(comments=[self.approved_comment()]),
            body="Source research PR/branch/commit: #321\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must bind exact source provenance", result.stdout)

    def test_latest_trusted_structured_verdict_wins(self):
        result = self.run_policy(
            self.source(
                comments=[
                    {
                        "created_at": "2026-08-25T04:00:00Z",
                        "author_association": "COLLABORATOR",
                        "body": f"Research Governance — APPROVED_FOR_INTEGRATION\nExact validated head: `{SOURCE_SHA}`.",
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
