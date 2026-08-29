from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_canonical_convergence_policy",
    ROOT / "scripts" / "v7_canonical_convergence_policy.py",
)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class V7CanonicalConvergencePolicyTest(unittest.TestCase):
    def event(self) -> dict:
        return {
            "number": 740,
            "pull_request": {
                "number": 740,
                "head": {
                    "ref": "integration/v7-complete-20260827",
                    "sha": "a" * 40,
                },
                "base": {"ref": "main", "sha": "b" * 40},
                "body": (
                    "operator authorization change: latest explicit user instruction\n"
                    "Canonical convergence authority: user-v7-master-multi-agent-operating-prompt-20260827\n"
                ),
            },
        }

    def changed(self) -> set[str]:
        return {
            "scripts/hard_safety_policy.py",
            "scripts/validate_project_context.py",
            "scripts/v7_execution_ledger.py",
            "config/paper_v7.json",
        }

    def test_exact_canonical_vehicle_passes(self) -> None:
        errors, summary = policy.evaluate(self.event(), self.changed(), ROOT)
        self.assertEqual(errors, [], "\n".join(errors))
        self.assertEqual(summary["policy"], "pass")
        self.assertEqual(summary["canonical_pr"], 740)

    def test_wrong_pr_number_fails_closed(self) -> None:
        event = self.event()
        event["number"] = 741
        event["pull_request"]["number"] = 741
        errors, _ = policy.evaluate(event, self.changed(), ROOT)
        self.assertTrue(any("restricted to PR #740" in error for error in errors))

    def test_wrong_head_or_base_fails_closed(self) -> None:
        event = self.event()
        event["pull_request"]["head"]["ref"] = "integration/v7-other"
        event["pull_request"]["base"]["ref"] = "paper-validated"
        errors, _ = policy.evaluate(event, self.changed(), ROOT)
        self.assertTrue(any("canonical convergence head" in error for error in errors))
        self.assertTrue(any("canonical convergence base" in error for error in errors))

    def test_missing_direct_user_markers_fails_closed(self) -> None:
        event = self.event()
        event["pull_request"]["body"] = "canonical convergence"
        errors, _ = policy.evaluate(event, self.changed(), ROOT)
        self.assertTrue(any("operator authorization change" in error for error in errors))
        self.assertTrue(any("canonical convergence authority" in error for error in errors))

    def test_directive_or_extra_authority_mutation_is_forbidden(self) -> None:
        changed = self.changed() | {
            "config/operator_directives.json",
            "config/project_context.json",
        }
        errors, _ = policy.evaluate(self.event(), changed, ROOT)
        self.assertTrue(any("additional operator authority surfaces" in error for error in errors))
        self.assertTrue(any("consume, not rewrite" in error for error in errors))

    def test_invalid_exact_head_sha_fails_closed(self) -> None:
        event = self.event()
        event["pull_request"]["head"]["sha"] = "not-a-sha"
        errors, _ = policy.evaluate(event, self.changed(), ROOT)
        self.assertTrue(any("exact 40-character source head SHA" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
