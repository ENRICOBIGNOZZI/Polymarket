from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "scripts" / "research_pr_policy.py"

spec = importlib.util.spec_from_file_location("research_pr_policy", POLICY_PATH)
assert spec is not None and spec.loader is not None
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ShadowIsolationPolicyTest(unittest.TestCase):
    def test_shadow_label_rejects_portfolio_risk_auth_and_model_surfaces(self):
        changed = {
            "config/portfolio_supervisor.json",
            "scripts/portfolio_supervisor.py",
            "scripts/position_sizing.py",
            "src/exposure_allocator.cpp",
            "scripts/install_cross_venue_credentials.sh",
            "src/cross_venue_auth_main.cpp",
            "ops/apply_risk_config.sh",
            "src/engine.cpp",
        }
        self.assertEqual(policy.shadow_forbidden_files(changed), sorted(changed))

    def test_shadow_label_allows_measurement_only_research_surfaces(self):
        changed = {
            "research/evidence/hf_forward_maker_fragility.json",
            "scripts/analyze_forward_maker_fragility.py",
            "tests/test_forward_maker_fragility.py",
        }
        self.assertEqual(policy.shadow_forbidden_files(changed), [])

    def test_shadow_labeled_research_pr_fails_on_portfolio_supervisor(self):
        event = {
            "pull_request": {
                "head": {"ref": "research/cross-venue-shadow"},
                "draft": True,
                "body": "research-only cross-venue measurement",
                "labels": [{"name": "shadow-isolated"}],
            }
        }
        errors, summary = policy.evaluate(
            event,
            {"scripts/portfolio_supervisor.py", "config/portfolio_supervisor.json"},
            manifest_existed_on_base=True,
        )
        self.assertNotEqual(errors, [])
        self.assertEqual(summary["policy"], "fail")
        self.assertIn("scripts/portfolio_supervisor.py", summary["shadow_forbidden_files"])
        self.assertIn("config/portfolio_supervisor.json", summary["shadow_forbidden_files"])

    def test_normal_fix_branch_cannot_change_live_model_config_by_wording_around_it(self):
        event = {
            "pull_request": {
                "head": {"ref": "fix/governance-rollback"},
                "draft": False,
                "body": "governance rollback with provenance checks",
                "labels": [],
            }
        }
        errors, summary = policy.evaluate(
            event,
            {"config/paper_v4.json", "scripts/integration_gate.py"},
            manifest_existed_on_base=True,
        )
        self.assertNotEqual(errors, [])
        self.assertEqual(summary["policy"], "fail")
        self.assertEqual(summary["model_surface_files"], ["config/paper_v4.json"])
        self.assertTrue(any("normal feature/fix branches" in error for error in errors))

    def test_normal_fix_branch_cannot_change_known_model_code_without_model_words(self):
        event = {
            "pull_request": {
                "head": {"ref": "fix/refactor"},
                "draft": False,
                "body": "small implementation cleanup",
                "labels": [],
            }
        }
        errors, summary = policy.evaluate(
            event,
            {"src/engine.cpp", "tests/test_core.cpp"},
            manifest_existed_on_base=True,
        )
        self.assertNotEqual(errors, [])
        self.assertEqual(summary["policy"], "fail")
        self.assertEqual(summary["model_surface_files"], ["src/engine.cpp"])

    def test_normal_non_model_fix_remains_allowed(self):
        event = {
            "pull_request": {
                "head": {"ref": "fix/api-parser"},
                "draft": False,
                "body": "fix public API parsing and add tests",
                "labels": [],
            }
        }
        errors, summary = policy.evaluate(
            event,
            {"src/event_api.cpp", "tests/test_core.cpp"},
            manifest_existed_on_base=True,
        )
        self.assertEqual(errors, [])
        self.assertEqual(summary["policy"], "pass")


if __name__ == "__main__":
    unittest.main()
