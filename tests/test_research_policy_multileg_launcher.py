from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "scripts" / "research_pr_policy.py"
SPEC = importlib.util.spec_from_file_location("research_pr_policy", POLICY_PATH)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class MultilegBrokerPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.changed = {"scripts/v7_multileg_broker.py"}

    def test_normal_fix_cannot_change_multileg_broker_model_surface(self) -> None:
        event = {
            "pull_request": {
                "head": {"ref": "fix/v7-multileg-broker"},
                "draft": False,
                "body": "runtime ownership fix",
                "labels": [],
            }
        }
        errors, summary = POLICY.evaluate(event, self.changed, manifest_existed_on_base=True)
        self.assertTrue(errors)
        self.assertIn("scripts/v7_multileg_broker.py", summary["model_surface_files"])
        self.assertTrue(any("unapproved model/runtime work" in error for error in errors))

    def test_draft_research_can_change_multileg_broker(self) -> None:
        event = {
            "pull_request": {
                "head": {"ref": "research/v7-multileg-broker"},
                "draft": True,
                "body": "state-integrity research",
                "labels": [],
            }
        }
        errors, summary = POLICY.evaluate(event, self.changed, manifest_existed_on_base=True)
        self.assertEqual(errors, [])
        self.assertEqual(summary["policy"], "pass")

    def test_shadow_isolated_cannot_change_multileg_broker(self) -> None:
        event = {
            "pull_request": {
                "head": {"ref": "research/v7-multileg-broker-shadow"},
                "draft": True,
                "body": "measurement-only runtime ownership research",
                "labels": [{"name": "shadow-isolated"}],
            }
        }
        errors, summary = POLICY.evaluate(event, self.changed, manifest_existed_on_base=True)
        self.assertTrue(errors)
        self.assertIn("scripts/v7_multileg_broker.py", summary["shadow_forbidden_files"])
        self.assertTrue(any("shadow-isolated code cannot modify" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
