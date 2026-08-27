from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POST = ROOT / ".github/workflows/post-merge-validation.yml"
POLICY = ROOT / ".github/workflows/research-policy.yml"
REGISTRY = ROOT / "config/scheduler_registry.json"


class V7PaperValidatedAdvanceContractTest(unittest.TestCase):
    def test_disabled_champion_remains_fail_closed(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        if manifest.get("enabled") is False:
            text = POST.read_text(encoding="utf-8")
            self.assertIn("paper_validated=unchanged champion_disabled", text)
            self.assertIn("steps.source.outputs.enabled == 'true'", text)
            self.assertTrue(manifest.get("paper_only"))
            self.assertFalse(manifest.get("authenticated_execution"))

    def test_exact_head_validators_are_required_before_ref_advance(self) -> None:
        text = POST.read_text(encoding="utf-8")
        for name in (
            "ci",
            "monitoring",
            "Polymarket Research Policy",
            "Private runtime single-writer validation",
        ):
            self.assertIn(name, text)
        self.assertIn("head_sha=${EXPECTED_SHA}", text)
        self.assertIn("status') != 'completed'", text)
        self.assertIn("conclusion') != 'success'", text)
        self.assertIn("steps.gates.outputs.ready == 'true'", text)

    def test_same_sha_paper_artifact_and_canonical_ledger_are_hard_gates(self) -> None:
        text = POST.read_text(encoding="utf-8")
        self.assertIn("v7-unified-paper-evidence.yml", text)
        self.assertIn("selection-meta.json", text)
        self.assertIn("check-gate.json", text)
        self.assertIn("evidence_router_status.json", text)
        self.assertIn("v7_execution_evidence.json", text)
        self.assertIn("ledger/execution.jsonl", text)
        self.assertIn("from v7_execution_ledger import LedgerEvent", text)
        self.assertIn("event.model_sha == expected", text)
        self.assertIn("event.paper_only is True", text)
        self.assertIn("event.authenticated_execution is False", text)
        self.assertIn("rows > 0", text)
        self.assertIn("no_same_sha_paper_artifact_with_canonical_ledger", text)

    def test_paper_validated_move_is_non_force_and_exact_current_main(self) -> None:
        text = POST.read_text(encoding="utf-8")
        self.assertIn("git/refs/heads/paper-validated", text)
        self.assertIn('-F force=false', text)
        self.assertIn('test "$MAIN_SHA" = "$EXPECTED_SHA"', text)
        self.assertIn('git merge-base --is-ancestor "$CURRENT_VALIDATED" "$EXPECTED_SHA"', text)
        self.assertIn('test "$(git rev-parse origin/paper-validated)" = "$EXPECTED_SHA"', text)
        self.assertNotIn("git push origin paper-validated", text)
        self.assertNotIn("--force", text)
        self.assertNotIn("gh pr merge", text)

    def test_post_merge_validator_remains_single_registered_owner(self) -> None:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        rows = registry["schedulers"]
        post = [row for row in rows if row["id"] == "post-merge-validation"]
        self.assertEqual(len(post), 1)
        self.assertTrue(post[0]["validation_dispatch_authority"])
        self.assertFalse(post[0]["merge_authority"])
        self.assertFalse(post[0]["deploy_authority"])
        self.assertIn("paper-validated", post[0]["responsibility"])
        self.assertIn("same-SHA PAPER", post[0]["responsibility"])

    def test_research_policy_recognizes_only_bounded_exact_fast_forward_prs(self) -> None:
        text = POLICY.read_text(encoding="utf-8")
        for required in (
            "integration/v7-",
            "autonomous-promotion-approved",
            "source research pr/branch/commit",
            "promotion evidence file",
            "main_exact_fast_forward_pr",
            "git merge-base --is-ancestor \"$validated_sha\" \"$base_sha\"",
            "git merge-base --is-ancestor \"$base_sha\" \"$GITHUB_SHA\"",
        ):
            self.assertIn(required, text)
        self.assertRegex(text, re.compile(r"head\.get\('sha'\)!=sha"))
        self.assertIn("ambiguous exact-head integration provenance", text)


if __name__ == "__main__":
    unittest.main()
