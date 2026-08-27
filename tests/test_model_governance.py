from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from scripts.integration_base_gate import validate_base
from scripts.research_pr_policy import evaluate, is_sensitive_model_surface, shadow_forbidden_files

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class ModelGovernanceContractTest(unittest.TestCase):
    @staticmethod
    def _policy_event(head: str, *, draft: bool, labels: list[str] | None = None, body: str = "") -> dict:
        return {
            "pull_request": {
                "head": {"ref": head},
                "body": body,
                "draft": draft,
                "labels": [{"name": name} for name in (labels or [])],
            }
        }

    def test_no_operational_champion_is_explicit_and_valid(self) -> None:
        manifest = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["enabled"])
        self.assertIsNone(manifest["version"])
        self.assertIsNone(manifest["loop"])
        self.assertIsNone(manifest["config"])
        self.assertIsNone(manifest["run_root"])
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertEqual(manifest["promotion_policy"], "automatic validated integration")
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])

    def test_registry_is_v7_only_and_has_no_deployment_authority(self) -> None:
        completed = subprocess.run(
            ["python3", "scripts/validate_scheduler_registry.py", "--root", ".", "--registry", "config/scheduler_registry.json"],
            cwd=ROOT, check=False, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        registry = json.loads((ROOT / "config" / "scheduler_registry.json").read_text(encoding="utf-8"))
        items = registry["schedulers"]
        ids = [item["id"] for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([item["id"] for item in items if item["merge_authority"]], ["integration-merge"])
        self.assertEqual([item["id"] for item in items if item["deploy_authority"]], [])
        self.assertEqual([item["id"] for item in items if item["validation_dispatch_authority"]], ["post-merge-validation"])
        self.assertFalse(any("v6" in sid or "v5" in sid or "v4" in sid or "v3" in sid for sid in ids))

    def test_post_merge_dispatches_only_static_exact_sha_validation(self) -> None:
        post = (WORKFLOWS / "post-merge-validation.yml").read_text(encoding="utf-8")
        self.assertIn("ci.yml monitoring.yml", post)
        self.assertIn("ci.yml monitoring.yml", post)
        self.assertIn('-f expected_sha="$EXPECTED_SHA"', post)
        self.assertNotIn("gh pr merge", post)

    def test_workflows_never_push_directly_to_main(self) -> None:
        offenders = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if "git push origin HEAD:main" in text or "git push origin main" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"direct main mutation workflows: {offenders}")

    def test_integration_base_allows_explicit_no_champion_cutover_divergence(self) -> None:
        champion = {
            "enabled": False,
            "version": None,
            "loop": None,
            "config": None,
            "run_root": None,
            "paper_only": True,
            "authenticated_execution": False,
        }
        directives = {"architecture": {"operational_champion_may_be_absent": True}}
        mode = validate_base(
            main_sha="b" * 40,
            validated_sha="a" * 40,
            checkout_sha="b" * 40,
            validated_is_ancestor=True,
            champion=champion,
            directives=directives,
        )
        self.assertEqual(mode, "v7_no_champion_cutover")

    def test_integration_base_requires_validated_incumbent_when_champion_enabled(self) -> None:
        champion = {
            "enabled": True,
            "version": 7,
            "loop": "scripts/paper_v7_loop.sh",
            "config": "config/paper_v7.json",
            "run_root": "runs/v7",
            "paper_only": True,
            "authenticated_execution": False,
        }
        directives = {"architecture": {"operational_champion_may_be_absent": True}}
        with self.assertRaisesRegex(ValueError, "main == paper-validated"):
            validate_base(
                main_sha="b" * 40,
                validated_sha="a" * 40,
                checkout_sha="b" * 40,
                validated_is_ancestor=True,
                champion=champion,
                directives=directives,
            )

    def test_integration_base_rejects_nonancestor_paper_validated_ref(self) -> None:
        champion = {
            "enabled": False,
            "version": None,
            "loop": None,
            "config": None,
            "run_root": None,
            "paper_only": True,
            "authenticated_execution": False,
        }
        directives = {"architecture": {"operational_champion_may_be_absent": True}}
        with self.assertRaisesRegex(ValueError, "not an ancestor"):
            validate_base(
                main_sha="b" * 40,
                validated_sha="a" * 40,
                checkout_sha="b" * 40,
                validated_is_ancestor=False,
                champion=champion,
                directives=directives,
            )

    def test_canonical_v7_runtime_ownership_surfaces_are_sensitive(self) -> None:
        paths = {
            "scripts/runtime_singleton_launcher.py",
            "scripts/runtime_plane_supervisor.py",
            "scripts/run_paper.sh",
            "scripts/v7_multileg_broker.py",
            "scripts/v7_execution_ledger.py",
            "scripts/v7_micro_taker_core.py",
            "scripts/v7_micro_taker_data.py",
            "scripts/v7_micro_target.py",
            "scripts/v7_micro_taker_worker.py",
        }
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(is_sensitive_model_surface(path))

    def test_normal_fix_cannot_change_v7_runtime_ownership_surface(self) -> None:
        path = "scripts/runtime_singleton_launcher.py"
        errors, summary = evaluate(
            self._policy_event("fix/runtime-singleton", draft=False),
            {path},
            manifest_existed_on_base=True,
        )
        self.assertEqual(summary["policy"], "fail")
        self.assertTrue(any("unapproved model/runtime work" in error for error in errors))

    def test_draft_research_can_change_v7_runtime_ownership_surface(self) -> None:
        errors, summary = evaluate(
            self._policy_event("research/runtime-singleton", draft=True),
            {"scripts/runtime_singleton_launcher.py"},
            manifest_existed_on_base=True,
        )
        self.assertEqual(errors, [])
        self.assertEqual(summary["policy"], "pass")

    def test_shadow_isolated_cannot_change_v7_runtime_ownership_surface(self) -> None:
        path = "scripts/v7_multileg_broker.py"
        self.assertIn(path, shadow_forbidden_files({path}))
        errors, summary = evaluate(
            self._policy_event("research/multileg-shadow", draft=True, labels=["shadow-isolated"]),
            {path},
            manifest_existed_on_base=True,
        )
        self.assertEqual(summary["policy"], "fail")
        self.assertTrue(any("shadow-isolated code cannot modify" in error for error in errors))

    def test_v7_runtime_integration_requires_trusted_exact_head_provenance(self) -> None:
        source_sha = "a" * 40
        source = {
            "number": 42,
            "headRefName": "research/runtime-owner",
            "headRefOid": source_sha,
            "comments": [
                {
                    "authorAssociation": "OWNER",
                    "createdAt": "2026-08-27T00:00:00Z",
                    "body": (
                        "Research Governance — APPROVED_FOR_INTEGRATION\n\n"
                        f"Exact validated head: {source_sha}"
                    ),
                }
            ],
            "reviews": [],
        }
        body = f"Source research PR/branch/commit: #42 / research/runtime-owner / {source_sha}"
        errors, summary = evaluate(
            self._policy_event("integration/runtime-owner", draft=False, body=body),
            {"scripts/runtime_singleton_launcher.py"},
            manifest_existed_on_base=True,
            source_research=source,
        )
        self.assertEqual(errors, [])
        self.assertEqual(summary["source_research_approved_sha"], source_sha)

        stale = dict(source)
        stale["headRefOid"] = "b" * 40
        errors, _ = evaluate(
            self._policy_event("integration/runtime-owner", draft=False, body=body),
            {"scripts/runtime_singleton_launcher.py"},
            manifest_existed_on_base=True,
            source_research=stale,
        )
        self.assertTrue(any("source research changed after approval" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
