from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


integration_gate = load_module("integration_gate", ROOT / "scripts" / "integration_gate.py")
research_pr_policy = load_module("research_pr_policy", ROOT / "scripts" / "research_pr_policy.py")


def successful_checks(names: tuple[str, ...]) -> list[dict]:
    return [{"__typename":"CheckRun","name":name,"status":"COMPLETED","conclusion":"SUCCESS"} for name in names]


class IntegrationResearchProvenanceTest(unittest.TestCase):
    def candidate(self) -> dict:
        return {
            "number":200,"headRefName":"integration/example","isDraft":False,
            "labels":[{"name":name} for name in sorted(integration_gate.REQUIRED_LABELS)],
            "mergeStateStatus":"CLEAN","statusCheckRollup":successful_checks(integration_gate.REQUIRED_CHECK_FRAGMENTS),
            "body":"Source research PR/branch/commit: #123\n- [x] Approved research integration into the single champion\n",
        }

    def source(self) -> dict:
        return {"number":123,"headRefName":"research/example","isDraft":True,"state":"OPEN","labels":[{"name":"research-approved"}],"statusCheckRollup":successful_checks(integration_gate.SOURCE_REQUIRED_CHECK_FRAGMENTS)}

    def test_approved_numbered_source_with_green_checks_passes(self):
        self.assertEqual(integration_gate.validate_candidate(self.candidate(), self.source()), [])

    def test_legacy_integration_gate_still_rejects_missing_approval(self):
        candidate = self.candidate(); candidate["labels"] = []
        errors = integration_gate.validate_candidate(candidate, self.source())
        self.assertTrue(any("candidate is missing labels" in error for error in errors))

    def test_legacy_integration_gate_still_rejects_missing_research_approval(self):
        source = self.source(); source["labels"] = []
        errors = integration_gate.validate_candidate(self.candidate(), source)
        self.assertIn("source research PR is not research-approved", errors)

    def test_source_failed_or_skipped_check_is_rejected(self):
        source = self.source(); source["statusCheckRollup"][0]["conclusion"] = "SKIPPED"
        errors = integration_gate.validate_candidate(self.candidate(), source)
        self.assertTrue(any("source research check" in error and "SKIPPED" in error for error in errors))

    def test_branch_only_source_cannot_be_selected(self):
        candidate = self.candidate(); candidate["body"] = candidate["body"].replace("#123", "experiment/example")
        self.assertEqual(integration_gate.select_candidates([candidate]), [])
        errors = integration_gate.validate_candidate(candidate, self.source())
        self.assertTrue(any("numbered PR" in error for error in errors))

    def test_policy_rejects_branch_only_integration_provenance(self):
        candidate = self.candidate(); candidate["body"] = candidate["body"].replace("#123", "experiment/example")
        event = {"pull_request":{"head":{"ref":candidate["headRefName"]},"draft":candidate["isDraft"],"body":candidate["body"],"labels":candidate["labels"]}}
        errors, summary = research_pr_policy.evaluate(event, {"config/paper_v5.json"}, manifest_existed_on_base=True)
        self.assertEqual(summary["policy"], "fail")
        self.assertTrue(any("bind exact source provenance" in error for error in errors))

    def test_mismatched_source_number_is_rejected(self):
        source = self.source(); source["number"] = 124
        errors = integration_gate.validate_candidate(self.candidate(), source)
        self.assertTrue(any("expected #123" in error for error in errors))

    def test_multiple_legacy_approved_candidates_are_selected_deterministically(self):
        first = self.candidate(); first["number"] = 201
        second = self.candidate(); second["number"] = 199
        selected = integration_gate.select_candidates([first, second])
        self.assertEqual([item["number"] for item in selected], [199, 201])

    def test_research_branch_rejects_legacy_integration_labels(self):
        event = {"pull_request":{"head":{"ref":"research/example"},"draft":True,"body":"research","labels":[{"name":"administrator-approved"}]}}
        errors, _ = research_pr_policy.evaluate(event, {"docs/research.md"}, True)
        self.assertTrue(any("cannot carry integration/administrator labels" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
