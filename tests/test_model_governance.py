from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from scripts.integration_base_gate import validate_base

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class ModelGovernanceContractTest(unittest.TestCase):
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
        self.assertNotIn("v4-live-smoke.yml", post)
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


if __name__ == "__main__":
    unittest.main()
