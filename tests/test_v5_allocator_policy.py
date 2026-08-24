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


class V5AllocatorPolicyTest(unittest.TestCase):
    def test_normal_fix_cannot_modify_live_v5_allocator(self):
        event = {
            "pull_request": {
                "head": {"ref": "fix/change-v5-allocation"},
                "draft": False,
                "body": "adjust allocator behavior",
                "labels": [],
            }
        }
        errors, summary = policy.evaluate(
            event,
            {"scripts/multi_strategy_paper.py"},
            manifest_existed_on_base=True,
        )
        self.assertTrue(errors)
        self.assertIn("scripts/multi_strategy_paper.py", summary["model_surface_files"])
        self.assertTrue(any("unapproved model/runtime work" in error for error in errors))

    def test_draft_research_may_measure_allocator_change_without_promotion(self):
        event = {
            "pull_request": {
                "head": {"ref": "research/v5-allocation-challenger"},
                "draft": True,
                "body": "research allocator challenger",
                "labels": [],
            }
        }
        errors, summary = policy.evaluate(
            event,
            {"scripts/multi_strategy_paper.py"},
            manifest_existed_on_base=True,
        )
        self.assertEqual(errors, [])
        self.assertEqual(summary["policy"], "pass")

    def test_shadow_label_cannot_cover_live_allocator_surface(self):
        event = {
            "pull_request": {
                "head": {"ref": "research/v5-allocation-shadow"},
                "draft": False,
                "body": "shadow allocation instrumentation",
                "labels": [{"name": "shadow-isolated"}],
            }
        }
        errors, summary = policy.evaluate(
            event,
            {"scripts/multi_strategy_paper.py"},
            manifest_existed_on_base=True,
        )
        self.assertTrue(errors)
        self.assertIn("scripts/multi_strategy_paper.py", summary["shadow_forbidden_files"])
        self.assertTrue(any("shadow-isolated code cannot modify" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
