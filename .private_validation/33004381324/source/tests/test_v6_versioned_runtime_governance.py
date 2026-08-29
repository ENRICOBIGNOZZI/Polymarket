#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_policy():
    spec = importlib.util.spec_from_file_location("research_pr_policy_v6_versioned_test", ROOT / "scripts" / "research_pr_policy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class V6VersionedRuntimeGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_policy()

    def test_all_v6_execution_candidate_surfaces_are_sensitive(self):
        for path in (
            "scripts/paper_v6_loop_v2.sh",
            "scripts/v6_market_common.py",
            "scripts/v6_micro_maker.py",
            "scripts/v6_micro_taker_v2.py",
            "scripts/v6_hard_arb_paper_v2.py",
            "scripts/v6_local_factor_v2.py",
            "scripts/v6_queue_filter.py",
            "scripts/v6_runtime_status_v2.py",
            "scripts/v6_typed_structural.py",
            "scripts/v6_typed_structural_v2.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.policy.is_sensitive_model_surface(path), path)

    def test_normal_fix_branch_cannot_modify_versioned_v6_runtime(self):
        event = {"pull_request":{"head":{"ref":"fix/relax-v6-runtime"},"draft":False,"body":"Change V6 maker execution model.","labels":[]}}
        changed = {
            "scripts/v6_micro_maker.py",
            "scripts/v6_micro_taker_v2.py",
            "scripts/paper_v6_loop_v2.sh",
        }
        errors, summary = self.policy.evaluate(event, changed, manifest_existed_on_base=True)
        self.assertTrue(errors)
        self.assertEqual(summary["policy"], "fail")
        self.assertIn("scripts/v6_micro_maker.py", summary["model_surface_files"])
        self.assertIn("scripts/v6_micro_taker_v2.py", summary["model_surface_files"])
        self.assertIn("scripts/paper_v6_loop_v2.sh", summary["model_surface_files"])

    def test_research_branch_may_contain_versioned_runtime_only_as_draft(self):
        event = {"pull_request":{"head":{"ref":"research/v6-v2"},"draft":True,"body":"Research V6 maker execution model.","labels":[]}}
        changed = {"scripts/v6_micro_maker.py","scripts/v6_micro_taker_v2.py"}
        errors, summary = self.policy.evaluate(event, changed, manifest_existed_on_base=True)
        self.assertEqual(errors, [])
        self.assertEqual(summary["policy"], "pass")


if __name__ == "__main__":
    unittest.main()
