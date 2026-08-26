from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "research_pr_policy.py"


class CrossVenueResearchPolicyTest(unittest.TestCase):
    def run_policy(
        self,
        branch: str,
        changed_files: list[str],
        labels: list[str] | None = None,
        draft: bool = False,
        body: str = "cross-venue portfolio arbitrage research",
        source_research: dict | None = None,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            event = {
                "pull_request": {
                    "head": {"ref": branch},
                    "draft": draft,
                    "body": body,
                    "labels": [{"name": name} for name in (labels or [])],
                }
            }
            event_path = temp / "event.json"
            changed_path = temp / "changed.txt"
            report_path = temp / "report.md"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            changed_path.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
            command = [
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
            ]
            if source_research is not None:
                source_path = temp / "source.json"
                source_path.write_text(json.dumps(source_research), encoding="utf-8")
                command.extend(["--source-research-json", str(source_path)])
            return subprocess.run(
                command,
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
        self.assertIn("model/runtime work cannot change", completed.stdout)
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

    def test_normal_branch_cannot_bypass_versioned_model_specific_runtime_surfaces(self):
        changed = [
            "scripts/v6_external_bridge.py",
            "scripts/v6_hard_arb_guard.py",
            "scripts/v6_hard_arb_paper.py",
            "scripts/v6_intent_guard.py",
            "scripts/v6_local_factor_intents.py",
            "scripts/v6_materialize_configs.py",
            "scripts/v6_micro_taker.py",
            "scripts/v6_relation_intents.py",
        ]
        completed = self.run_policy("fix/v6-runtime-model", changed, body="V6 paper model runtime fix")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("model/runtime work cannot change", completed.stdout)
        for path in changed:
            self.assertIn(path, completed.stdout)

    def test_draft_research_may_hold_versioned_model_specific_runtime_surfaces(self):
        completed = self.run_policy(
            "research/v6-runtime-model",
            ["scripts/v6_micro_taker.py", "scripts/v6_local_factor_intents.py"],
            draft=True,
            body="V6 model research",
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("policy: `pass`", completed.stdout)

    def test_shadow_label_rejects_versioned_model_specific_runtime_surfaces(self):
        completed = self.run_policy(
            "research/v6-runtime-shadow",
            ["scripts/v6_external_bridge.py", "scripts/v6_hard_arb_guard.py", "scripts/v6_intent_guard.py"],
            labels=["shadow-isolated"],
            draft=False,
            body="V6 shadow model measurement",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("shadow-isolated code cannot modify", completed.stdout)
        self.assertIn("scripts/v6_external_bridge.py", completed.stdout)
        self.assertIn("scripts/v6_hard_arb_guard.py", completed.stdout)
        self.assertIn("scripts/v6_intent_guard.py", completed.stdout)

    def test_sensitive_integration_cannot_leave_draft_with_unapproved_source(self):
        source_sha = "a" * 40
        branch = "research/aggressive-v5-execution"
        completed = self.run_policy(
            "integration/aggressive-v5",
            ["src/engine.cpp", "config/live_champion.json"],
            draft=False,
            body=f"Source research PR/branch/commit: #154 / `{branch}` / `{source_sha}`\n",
            source_research={
                "number": 154,
                "headRefName": branch,
                "headRefOid": source_sha,
                "body": "research candidate",
                "comments": [
                    {
                        "createdAt": "2026-08-25T00:00:00Z",
                        "authorAssociation": "OWNER",
                        "body": "Research Governance — MORE_EVIDENCE_REQUIRED",
                    }
                ],
                "reviews": [],
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unapproved work must remain in research", completed.stdout)
        self.assertIn("MORE_EVIDENCE_REQUIRED", completed.stdout)

    def test_sensitive_draft_integration_rejects_unapproved_source(self):
        source_sha = "b" * 40
        branch = "research/aggressive-v5-execution"
        completed = self.run_policy(
            "integration/aggressive-v5-draft",
            ["src/engine.cpp", "config/live_champion.json"],
            draft=True,
            body=f"Source research PR/branch/commit: #154 / `{branch}` / `{source_sha}`\n",
            source_research={
                "number": 154,
                "headRefName": branch,
                "headRefOid": source_sha,
                "body": "research candidate",
                "comments": [
                    {
                        "createdAt": "2026-08-25T00:00:00Z",
                        "authorAssociation": "OWNER",
                        "body": "Research Governance — MORE_EVIDENCE_REQUIRED",
                    }
                ],
                "reviews": [],
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unapproved work must remain in research", completed.stdout)
        self.assertIn("MORE_EVIDENCE_REQUIRED", completed.stdout)

    def test_sensitive_draft_integration_requires_exact_source(self):
        completed = self.run_policy(
            "integration/unlinked-v5-draft",
            ["src/engine.cpp", "config/live_champion.json"],
            draft=True,
            body="staged model candidate without approved research provenance",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must bind exact source provenance", completed.stdout)
        self.assertIn("must provide trusted source research metadata", completed.stdout)

    def test_versioned_runtime_integration_requires_approved_source(self):
        completed = self.run_policy(
            "integration/v6-runtime-model",
            ["scripts/v6_micro_taker.py"],
            draft=True,
            body="staged V6 model runtime without approved research provenance",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must bind exact source provenance", completed.stdout)
        self.assertIn("must provide trusted source research metadata", completed.stdout)

    def test_sensitive_integration_accepts_explicit_approved_source_verdict(self):
        source_sha = "c" * 40
        source_branch = "research/approved-v5"
        completed = self.run_policy(
            "integration/approved-v5",
            ["src/engine.cpp", "config/live_champion.json"],
            draft=True,
            body=f"Source research PR/branch/commit: #200 / `{source_branch}` / `{source_sha}`\n",
            source_research={
                "number": 200,
                "headRefName": source_branch,
                "headRefOid": source_sha,
                "body": "research candidate",
                "comments": [
                    {
                        "createdAt": "2026-08-25T00:00:00Z",
                        "authorAssociation": "OWNER",
                        "body": f"Research Governance — APPROVED_FOR_INTEGRATION\n\nExact validated head: `{source_sha}`.",
                    }
                ],
                "reviews": [],
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("source_research_verdict: `APPROVED_FOR_INTEGRATION`", completed.stdout)
        self.assertIn(f"source_research_approved_sha: `{source_sha}`", completed.stdout)
        self.assertIn("policy: `pass`", completed.stdout)

    def test_latest_research_verdict_wins(self):
        source_sha = "d" * 40
        source_branch = "research/regressed-v5"
        completed = self.run_policy(
            "integration/regressed-v5",
            ["src/engine.cpp"],
            draft=True,
            body=f"Source research PR/branch/commit: #201 / `{source_branch}` / `{source_sha}`\n",
            source_research={
                "number": 201,
                "headRefName": source_branch,
                "headRefOid": source_sha,
                "body": "research candidate",
                "comments": [
                    {
                        "createdAt": "2026-08-25T00:00:00Z",
                        "authorAssociation": "OWNER",
                        "body": f"Research Governance — APPROVED_FOR_INTEGRATION\nExact validated head: `{source_sha}`.",
                    },
                    {
                        "createdAt": "2026-08-25T01:00:00Z",
                        "authorAssociation": "OWNER",
                        "body": "Research Governance — MORE_EVIDENCE_REQUIRED",
                    },
                ],
                "reviews": [],
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("MORE_EVIDENCE_REQUIRED", completed.stdout)


if __name__ == "__main__":
    unittest.main()
